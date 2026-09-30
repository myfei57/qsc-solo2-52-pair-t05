"""Discharge service: the door, the queue, the tickets and the confirmations.

The service is the only writer on the chute line.  For every load it:

1. resolves the truck's rated capacity and trip kind,
2. asks ``allocation.decide_load`` how much is accepted and how much blocked
   (the per-trip over-quantity policy is fixed there),
3. appends the accepted volume to the truck's ticket idempotently under a
   per-truck lock, so concurrent retries can never count volume twice,
4. writes the same numbers to the record stream (dispatch board) and to the
   audit ledger, and closes the valve when the decision says so.
"""

from __future__ import annotations

from typing import Any

from ..errors import NotFoundError, OverfillBlockedError
from ..recipe.service import RecipeService
from ..service_base import PlantService
from ..statemachine.phases import MixPhase
from .allocation import decide_load
from .allocation_ledger import BatchAllocation
from .chute import (
    ALLOCATION_KIND,
    DOOR_KIND,
    LOAD_KIND,
    MANIFEST_KIND,
    OPEN_KIND,
    PART_KIND,
    QUEUE_KIND,
    TICKET_KIND,
    TRUCK_KIND,
    VALVE_KIND,
)
from .door import door_state
from .fleet import TruckFleet, profile_document
from .guards import TruckLocks, require_request_id, synthetic_request_id
from .load import load_record
from .manifest import LoadManifest
from .open import open_payload, require_window_finished
from .queue import TruckQueue
from .reconcile import (
    TICKET_BLOCKED_AUDIT,
    TICKET_CLOSED_AUDIT,
    TICKET_LOAD_AUDIT,
    TICKET_OPEN_AUDIT,
    reconcile,
)
from .ticket import LoadingTicket, TicketBook, ticket_document
from .valve import valve_state


class ChuteService(PlantService):
    """Opens the discharge door only after the mixing window has finished."""

    origin = "chute"
    line = "mix"
    subject = "chute"

    def __init__(self, context, recipes: RecipeService, machine=None) -> None:
        super().__init__(context, machine)
        self.recipes = recipes
        self.queue = TruckQueue()
        self.manifest = LoadManifest()
        self.fleet = TruckFleet(
            default_capacity_m3=float(context.config.limits.truck_default_capacity_m3),
        )
        self.tickets = TicketBook()
        self.allocations = BatchAllocation(context.batches)
        self.locks = TruckLocks()
        self._open_batch: str | None = None
        self._auto_seq = 0
        self._audit = None

    def bind_audit(self, audit) -> None:
        """Wire the audit service after both services are constructed."""

        self._audit = audit

    # -- fleet -------------------------------------------------------------

    def register_truck(
        self, *, truck_id: str, rated_capacity_m3: float, trip: str = "primary"
    ) -> dict[str, Any]:
        """Register a truck's rated capacity and the kind of trip it makes."""

        profile = self.fleet.register(
            truck_id, rated_capacity_m3=rated_capacity_m3, trip=trip
        )
        self.publish(TRUCK_KIND, {"active": True, **profile.describe()})
        self.emit("chute.truck_registered", profile.describe())
        return {"truck": profile.describe()}

    def arrive(self, truck_id: str, *, trip: str | None = None) -> dict[str, Any]:
        """Queue a truck for loading (optionally declaring its trip kind)."""

        # A truck identity alone is enough for the manual chute; an unknown
        # truck gets the configured default profile, possibly restated here.
        if not self.fleet.is_registered(truck_id) and trip is not None:
            self.register_truck(
                truck_id=truck_id,
                rated_capacity_m3=float(self.context.config.limits.truck_default_capacity_m3),
                trip=trip,
            )
        position = self.queue.arrive(truck_id)
        self.publish(QUEUE_KIND, {"active": True, **self.queue.describe()})
        profile = self.fleet.profile_for(truck_id, trip=trip)
        self.emit("chute.truck_queued", {"truck_id": truck_id, "position": position, "trip": profile.trip})
        return {"queue": self.queue.describe(), "position": position, "truck": profile.describe()}

    # -- door --------------------------------------------------------------

    def open_door(self) -> dict[str, Any]:
        """Open the door; the mixing window has to be finished first."""

        self.require("chute.open", required_phase=MixPhase.MIXED.value)
        timer = self.payload_entry("mix", "timer")
        batch = self.context.batches.latest()
        batch_id = "" if batch is None else batch.batch_id
        require_window_finished(
            finished=bool(timer.get("finished", False)) if timer else False,
            remaining=int(timer.get("remaining", 0)) if timer else 0,
            batch_id=batch_id,
        )
        length = int(timer.get("length", 0)) if timer else 0
        self._open_batch = batch_id
        self.publish(DOOR_KIND, door_state(open=True, batch_id=batch_id).describe())
        self.publish(OPEN_KIND, open_payload(batch_id=batch_id, window_ticks=length))
        self.emit("chute.door_opened", {"batch_id": batch_id})
        return {"door": door_state(open=True, batch_id=batch_id).describe(), "queue": self.queue.describe()}

    # -- loading -----------------------------------------------------------

    def load(
        self,
        *,
        truck_id: str,
        volume_m3: float,
        request_id: str | None = None,
        trip: str | None = None,
    ) -> dict[str, Any]:
        """Hand the head truck its load, in queue order, under the trip policy."""

        self.require("chute.load")
        if self._open_batch is None:
            raise NotFoundError("the discharge door is not open")
        with self.locks.serialise(truck_id):
            served = self.queue.serve(truck_id)
            return self._apply_load(
                truck_id=served,
                requested_m3=volume_m3,
                request_id=request_id,
                trip=trip,
                allow_new_ticket=True,
                queued=True,
            )

    def topup(
        self,
        *,
        truck_id: str,
        volume_m3: float,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Complete a returning truck onto its open ticket (same bill)."""

        self.require("chute.load")
        if self._open_batch is None:
            raise NotFoundError("the discharge door is not open")
        with self.locks.serialise(truck_id):
            return self._apply_load(
                truck_id=truck_id,
                requested_m3=volume_m3,
                request_id=request_id,
                trip="topup",
                allow_new_ticket=False,
                queued=False,
            )

    def _apply_load(
        self,
        *,
        truck_id: str,
        requested_m3: float,
        request_id: str | None,
        trip: str | None,
        allow_new_ticket: bool,
        queued: bool,
    ) -> dict[str, Any]:
        batch_id = self._open_batch or ""
        profile = self.fleet.profile_for(truck_id, trip=trip)
        ticket = self._resolve_ticket(
            profile=profile,
            batch_id=batch_id,
            allow_new_ticket=allow_new_ticket,
        )

        # Idempotent replay: a repeated key returns the first result verbatim.
        prior = ticket.find(request_id) if request_id else None
        if prior is not None:
            result = {
                "load": self._load_payload(ticket, prior),
                "ticket": ticket.describe(),
                "manifest": self.manifest.describe(),
                "idempotent_replay": True,
            }
            return result

        # Open the per-truck fill valve; the batch discharge door is already
        # open for the whole window and stays open for the next truck.
        self.publish(
            VALVE_KIND,
            valve_state(open=True, truck_id=truck_id, ticket_id=ticket.ticket_id, batch_id=batch_id).describe(),
        )

        loaded = ticket.total_m3
        delivered = self.tickets.total_for_batch(batch_id)
        batch_remaining = None
        if batch_id:
            batch_remaining = self.allocations.remaining(batch_id, delivered)
        decision = decide_load(
            profile,
            batch_id=batch_id,
            loaded_m3=loaded,
            requested_m3=requested_m3,
            batch_remaining_m3=batch_remaining,
            valve_open=True,
            merged=profile.trip == "topup",
        )

        # Nothing at all fits (batch plan exhausted or truck already full):
        # refuse the whole request on the spot and count no part.
        if decision.accepted_m3 <= 0:
            self.raise_alarm(
                "chute.overfill_blocked",
                {
                    "ticket_id": ticket.ticket_id,
                    "truck_id": truck_id,
                    "trip": profile.trip,
                    "requested_m3": decision.requested_m3,
                    "accepted_m3": 0.0,
                    "blocked_m3": decision.blocked_m3,
                    "over_quantity_action": profile.action,
                },
            )
            self._audit_note(
                TICKET_BLOCKED_AUDIT,
                {
                    "ticket_id": ticket.ticket_id,
                    "truck_id": truck_id,
                    "trip": profile.trip,
                    "requested_m3": decision.requested_m3,
                    "accepted_m3": 0.0,
                    "blocked_m3": decision.blocked_m3,
                },
            )
            self._close_fill_valve(batch_id, ticket)
            raise OverfillBlockedError(
                "no remaining allowance; the whole load was blocked",
                **{k: v for k, v in decision.describe().items() if k != "reason"},
                result={
                    "load": None,
                    "decision": decision.describe(),
                    "ticket": ticket.describe(),
                    "manifest": self.manifest.describe(),
                    "allocation": self._allocation_view(batch_id),
                    "idempotent_replay": False,
                },
            )

        key = (
            require_request_id(request_id)
            if request_id is not None
            else synthetic_request_id(self._next_auto_index())
        )
        part = ticket.append(
            request_id=key,
            batch_id=batch_id,
            trip=profile.trip,
            volume_m3=decision.accepted_m3,
            blocked_m3=decision.blocked_m3,
            tick=self.context.clock.now(),
        )

        is_first_primary = profile.trip == "primary" and len(ticket.parts) == 1
        if is_first_primary:
            # Keep the legacy one-load-per-truck manifest identical for the
            # existing discharge view; the ticket book is the authority.
            self.manifest.record(
                load_record(
                    truck_id=truck_id,
                    batch_id=batch_id,
                    volume_m3=decision.accepted_m3,
                    tick=self.context.clock.now(),
                )
            )

        self._publish_load(ticket, part, decision, is_first_primary=is_first_primary)
        if self.machine.is_at(MixPhase.MIXED.value) and queued:
            self.advance(MixPhase.DISCHARGED.value, "truck loaded from the discharge door")
        self.emit("chute.loaded", self._load_payload(ticket, part))

        self._audit_note(
            TICKET_LOAD_AUDIT,
            {
                "ticket_id": ticket.ticket_id,
                "truck_id": truck_id,
                "batch_id": batch_id,
                "trip": profile.trip,
                "request_id": key,
                "volume_m3": part.volume_m3,
                "total_m3": ticket.total_m3,
            },
        )
        if decision.blocked:
            self.raise_alarm(
                "chute.overfill_blocked",
                {
                    "ticket_id": ticket.ticket_id,
                    "truck_id": truck_id,
                    "trip": profile.trip,
                    "requested_m3": decision.requested_m3,
                    "accepted_m3": decision.accepted_m3,
                    "blocked_m3": decision.blocked_m3,
                    "over_quantity_action": profile.action,
                },
            )
            self._audit_note(
                TICKET_BLOCKED_AUDIT,
                {
                    "ticket_id": ticket.ticket_id,
                    "truck_id": truck_id,
                    "trip": profile.trip,
                    "requested_m3": decision.requested_m3,
                    "accepted_m3": decision.accepted_m3,
                    "blocked_m3": decision.blocked_m3,
                },
            )

        if decision.close_valve:
            self._close_fill_valve(batch_id, ticket)

        result = {
            "load": self._load_payload(ticket, part),
            "decision": decision.describe(),
            "ticket": ticket.describe(),
            "manifest": self.manifest.describe(),
            "allocation": self._allocation_view(batch_id),
            "idempotent_replay": False,
        }
        if decision.blocked:
            # The in-target volume is already committed; signal the refusal so
            # the operator sees what was stopped on the spot.
            raise OverfillBlockedError(
                "requested volume exceeded the trip allowance; the overflow was blocked",
                **{k: v for k, v in decision.describe().items() if k != "reason"},
                result=result,
            )
        return result

    def _resolve_ticket(self, *, profile, batch_id: str, allow_new_ticket: bool) -> LoadingTicket:
        open_ticket = self.tickets.open_ticket(profile.truck_id)
        if profile.trip == "topup":
            if open_ticket is None:
                raise NotFoundError(
                    "a top up needs an open ticket for the truck", truck_id=profile.truck_id
                )
            return open_ticket
        if open_ticket is not None:
            # A primary trip against an open ticket is completed onto it rather
            # than opening a parallel bill for the same truck.
            return open_ticket
        if not allow_new_ticket:
            raise NotFoundError("no open ticket to complete", truck_id=profile.truck_id)
        ticket_id = self.context.ids.next("ticket")
        ticket = self.tickets.open(
            ticket_id=ticket_id,
            truck_id=profile.truck_id,
            rated_capacity_m3=profile.rated_capacity_m3,
            batch_id=batch_id,
        )
        self.publish(TICKET_KIND, {"active": True, **ticket.describe()})
        self._audit_note(
            TICKET_OPEN_AUDIT,
            {"ticket_id": ticket_id, "truck_id": profile.truck_id, "batch_id": batch_id},
        )
        return ticket

    def _publish_load(self, ticket: LoadingTicket, part, decision, *, is_first_primary: bool) -> None:
        payload = self._load_payload(ticket, part, decision=decision)
        self.publish(PART_KIND, {"active": True, **payload})
        self.publish(TICKET_KIND, {"active": True, **ticket.describe()})
        self.publish(LOAD_KIND, payload)
        if is_first_primary:
            self.publish(MANIFEST_KIND, {"active": True, **self.manifest.describe()})
        self.publish(ALLOCATION_KIND, {"active": True, **self._allocation_view(ticket.batch_id)})

    def _load_payload(self, ticket: LoadingTicket, part, *, decision=None) -> dict[str, Any]:
        payload = {
            "active": True,
            "ticket_id": ticket.ticket_id,
            "truck_id": ticket.truck_id,
            "batch_id": part.batch_id,
            "request_id": part.request_id,
            "trip": part.trip,
            "volume_m3": part.volume_m3,
            "total_m3": ticket.total_m3,
            "remaining_m3": ticket.remaining_m3,
            "tick": part.tick,
        }
        if decision is not None:
            payload["blocked_m3"] = decision.blocked_m3
            payload["valve_closed"] = decision.close_valve
        return payload

    def _allocation_view(self, batch_id: str) -> dict[str, Any]:
        if not batch_id:
            return {"batch_id": "", "planned_m3": 0.0, "delivered_m3": 0.0, "remaining_m3": 0.0}
        planned = self.allocations.plan_m3(batch_id)
        delivered = self.tickets.total_for_batch(batch_id)
        return {
            "batch_id": batch_id,
            "planned_m3": planned,
            "delivered_m3": delivered,
            "remaining_m3": round(planned - delivered, 4),
        }

    # -- confirm / close ---------------------------------------------------

    def confirm(self, *, truck_id: str, volume_m3: float) -> dict[str, Any]:
        """Confirm a truck load against its ticket; served in arrival order."""

        self.require("chute.confirm")
        ticket = self.tickets.open_ticket(truck_id)
        recorded_m3 = ticket.total_m3 if ticket is not None else self.manifest.require(truck_id).volume_m3
        if abs(recorded_m3 - volume_m3) > 0.5:
            self.raise_alarm(
                "chute.volume_mismatch",
                {"truck_id": truck_id, "recorded": recorded_m3, "confirmed": volume_m3},
            )
        payload = {
            "truck_id": truck_id,
            "active": True,
            "confirmed": True,
            "confirmed_m3": round(volume_m3, 4),
            "total_m3": recorded_m3,
        }
        if ticket is not None:
            payload["ticket_id"] = ticket.ticket_id
        self.publish(LOAD_KIND, payload)
        self.emit("chute.confirmed", {"truck_id": truck_id})
        return {"confirmed": payload, "manifest": self.manifest.describe()}

    def close_ticket(self, *, truck_id: str) -> dict[str, Any]:
        """Close a truck's open ticket once it leaves the chute."""

        ticket = self.tickets.open_ticket(truck_id)
        if ticket is None:
            raise NotFoundError("truck has no open ticket", truck_id=truck_id)
        self.tickets.close(ticket.ticket_id)
        self.publish(TICKET_KIND, {"active": True, **ticket.describe()})
        self._audit_note(
            TICKET_CLOSED_AUDIT,
            {"ticket_id": ticket.ticket_id, "truck_id": truck_id, "total_m3": ticket.total_m3},
        )
        self.emit("chute.ticket_closed", {"ticket_id": ticket.ticket_id, "truck_id": truck_id})
        return {"ticket": ticket.describe()}

    def close_door(self) -> dict[str, Any]:
        """Close the batch discharge door and end the loading window."""

        self.require("chute.close")
        batch_id = self._open_batch or ""
        self._open_batch = None
        self.publish(VALVE_KIND, valve_state(open=False, batch_id=batch_id).describe())
        self.publish(DOOR_KIND, door_state(open=False, batch_id=batch_id).describe())
        self.emit("chute.door_closed", {"batch_id": batch_id})
        return {"door": door_state(open=False, batch_id=batch_id).describe(), "manifest": self.manifest.describe()}

    def _close_fill_valve(self, batch_id: str, ticket: LoadingTicket) -> None:
        """Stop concrete flowing into one truck that reached its target.

        The batch discharge door stays open so the queue keeps moving; only the
        per-truck fill valve shuts.
        """

        self.publish(
            VALVE_KIND,
            valve_state(
                open=False, truck_id=ticket.truck_id, ticket_id=ticket.ticket_id, batch_id=batch_id
            ).describe(),
        )
        self.emit(
            "chute.valve_closed",
            {"batch_id": batch_id, "truck_id": ticket.truck_id, "ticket_id": ticket.ticket_id},
        )

    def _audit_note(self, name: str, detail: dict[str, Any]) -> None:
        if self._audit is not None:
            self._audit.note(name, detail)

    def _next_auto_index(self) -> int:
        """Return a process-local monotonically increasing synthetic index."""

        self._auto_seq += 1
        return self._auto_seq

    # -- reads -------------------------------------------------------------

    def headroom_for(self, truck_id: str, *, trip: str | None = None) -> dict[str, Any]:
        """Answer how much more the head/named truck can still take."""

        from .capacity import headroom as compute_headroom

        profile = self.fleet.profile_for(truck_id, trip=trip)
        ticket = self.tickets.open_ticket(truck_id)
        loaded = 0.0 if ticket is None else ticket.total_m3
        batch_id = self._open_batch or (ticket.batch_id if ticket is not None else "")
        batch_remaining = None
        if batch_id:
            batch_remaining = self.allocations.remaining(
                batch_id, self.tickets.total_for_batch(batch_id)
            )
        room = compute_headroom(
            profile,
            loaded_m3=loaded,
            batch_remaining_m3=batch_remaining,
            valve_open=self._open_batch is not None,
        )
        return room.describe()

    def board(self) -> dict[str, Any]:
        """Build the dispatch board: queue, head headroom, tickets, plans."""

        head_truck = self.queue.head_safe()
        waiting = []
        for index, truck_id in enumerate(self.queue.order(), start=1):
            profile = self.fleet.profile_for(truck_id)
            ticket = self.tickets.open_ticket(truck_id)
            waiting.append(
                {
                    "position": index,
                    "truck_id": truck_id,
                    "trip": profile.trip,
                    "rated_capacity_m3": profile.rated_capacity_m3,
                    "loaded_m3": 0.0 if ticket is None else ticket.total_m3,
                    "headroom_m3": self.headroom_for(truck_id)["allowed_m3"],
                }
            )
        open_batch = self._open_batch or ""
        valve = self.payload_entry("chute", "valve")
        return {
            "door_open": self._open_batch is not None,
            "fill_valve_open": bool(valve.get("active", False)) if valve else False,
            "batch": None if not open_batch else self._allocation_view(open_batch),
            "head_truck": head_truck,
            "queue": waiting,
            "tickets": self.tickets.describe()["tickets"],
            "fleet": self.fleet.describe(),
        }

    def reconcile(self) -> dict[str, Any]:
        """Cross-check ticket book, audit ledger and dispatch board."""

        return reconcile(list(self.store.committed_records()), self.tickets.all())

    def hydrate_from_stream(self) -> None:
        """Rebuild the fleet and ticket book from the committed stream."""

        latest_ticket: dict[str, dict[str, Any]] = {}
        for record in self.store.committed_records():
            if record.kind == TRUCK_KIND:
                self.fleet.adopt(profile_document(record.payload))
            elif record.kind == TICKET_KIND and record.payload.get("ticket_id"):
                latest_ticket[str(record.payload["ticket_id"])] = dict(record.payload)
        for document in latest_ticket.values():
            self.tickets.adopt(ticket_document(document))
        # The legacy manifest records the first primary load per truck.
        seen = {load.truck_id for load in self.manifest.loads()}
        for record in self.store.history(LOAD_KIND):
            truck_id = record.payload.get("truck_id")
            if truck_id and truck_id not in seen:
                self.manifest.adopt(
                    load_record(
                        truck_id=str(truck_id),
                        batch_id=str(record.payload.get("batch_id", "")),
                        volume_m3=float(record.payload.get("volume_m3", 0.0)),
                        tick=int(record.payload.get("tick", record.tick)),
                    )
                )
                seen.add(truck_id)

    def status(self) -> dict[str, Any]:
        door = self.payload_entry("chute", "door")
        load = self.payload_entry("chute", "load")
        manifest = self.manifest.describe()
        if self._open_batch:
            manifest["batch_loads"] = [
                item.describe() for item in self.manifest.for_batch(self._open_batch)
            ]
        return {
            "door_open": bool(door.get("active", False)) if door else False,
            "open_batch": None if door is None else door.get("batch_id"),
            "queue": self.queue.describe(),
            "manifest": manifest,
            "tickets": self.tickets.describe(),
            "fleet": self.fleet.describe(),
            "last_load": None if load is None else dict(load),
        }
