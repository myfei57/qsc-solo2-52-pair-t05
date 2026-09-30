"""Discharge service: the door, the queue, the tickets and the loading valve."""

from __future__ import annotations

import threading
from typing import Any

from ..errors import InterlockBlockedError, NotFoundError, ValidationError
from ..recipe.service import RecipeService
from ..service_base import PlantService
from ..statemachine.phases import MixPhase
from ..audit.audit import ENTRY_KIND as AUDIT_ENTRY_KIND
from .chute import (
    BOARD_KIND,
    CONFIRM_KIND,
    DOOR_KIND,
    FLEET_KIND,
    LOAD_KIND,
    MANIFEST_KIND,
    OPEN_KIND,
    QUEUE_KIND,
    RECONCILE_KIND,
    VALVE_KIND,
)
from .door import door_state
from .fleet import DEFAULT_CAPACITY_M3, TruckFleet, truck_spec
from .load import load_record
from .manifest import LoadManifest
from .open import open_payload, require_window_finished
from .plan import open_batch_plan, queue_capacity
from .policy import INITIAL_LOAD, MAKEUP_LOAD, STANDARD_TRIP, decide_initial, decide_makeup
from .queue import TruckQueue
from .reconcile import audit_totals, board_from_records, reconcile
from .valve import valve_state


class ChuteService(PlantService):
    """Opens the discharge door and meters trucks without crossing a quota.

    Every state changing call takes ``_lock`` so two applications that arrive
    concurrently for one returning truck cannot read the same sheet total and
    meter the same cubic metres twice.
    """

    origin = "chute"
    line = "mix"
    subject = "chute"

    def __init__(self, context, recipes: RecipeService, machine=None) -> None:
        super().__init__(context, machine)
        self.recipes = recipes
        self.queue = TruckQueue()
        self.manifest = LoadManifest()
        self.fleet = TruckFleet()
        self._open_batch: str | None = None
        self._lock = threading.RLock()

    def register_truck(self, *, truck_id: str, capacity_m3: float, trip_type: str) -> dict[str, Any]:
        """Declare the legal capacity and the trip type of one truck."""

        with self._lock:
            spec = truck_spec(
                truck_id=truck_id, capacity_m3=capacity_m3, trip_type=trip_type
            )
            self.fleet.register(spec)
            self.publish(FLEET_KIND, {"active": True, **self.fleet.describe()})
            self.emit("chute.truck_registered", spec.describe())
            return {"truck": spec.describe(), "fleet": self.fleet.describe()}

    def arrive(self, truck_id: str) -> dict[str, Any]:
        """Queue a truck for loading, registering a default spec when unknown."""

        with self._lock:
            if not self.fleet.known(truck_id):
                spec = truck_spec(
                    truck_id=truck_id,
                    capacity_m3=DEFAULT_CAPACITY_M3,
                    trip_type=STANDARD_TRIP,
                )
                self.fleet.adopt(spec)
                self.publish(FLEET_KIND, {"active": True, **self.fleet.describe()})
            position = self.queue.arrive(truck_id)
            self.publish(QUEUE_KIND, {"active": True, **self.queue.describe()})
            self.emit("chute.truck_queued", {"truck_id": truck_id, "position": position})
            return {"queue": self._queue_view(), "position": position}

    def open_door(self) -> dict[str, Any]:
        """Open the door; the mixing window has to be finished first."""

        with self._lock:
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
            self.publish(
                VALVE_KIND,
                valve_state(open=False, batch_id=batch_id, reason="door_open").describe(),
            )
            self.emit("chute.door_opened", {"batch_id": batch_id})
            return {
                "door": door_state(open=True, batch_id=batch_id).describe(),
                "queue": self._queue_view(),
            }

    def load(
        self, *, truck_id: str, volume_m3: float, request_id: str = ""
    ) -> dict[str, Any]:
        """Hand the head truck its first load, in queue order."""

        with self._lock:
            return self._apply(
                kind=INITIAL_LOAD, truck_id=truck_id, requested_m3=volume_m3, request_id=request_id
            )

    def makeup(
        self, *, truck_id: str, volume_m3: float, request_id: str
    ) -> dict[str, Any]:
        """Top up a returning makeup trip; the volume merges onto its sheet."""

        with self._lock:
            return self._apply(
                kind=MAKEUP_LOAD, truck_id=truck_id, requested_m3=volume_m3, request_id=request_id
            )

    def _apply(
        self, *, kind: str, truck_id: str, requested_m3: float, request_id: str
    ) -> dict[str, Any]:
        """Meter one application; shared by first loads and makeup top ups."""

        action = "chute.makeup" if kind == MAKEUP_LOAD else "chute.load"
        self.require(action)
        if self._open_batch is None:
            raise NotFoundError("the discharge door is not open")
        batch_id = self._open_batch
        if requested_m3 <= 0:
            raise ValidationError("a load needs a positive volume", truck=truck_id)

        # Idempotency wins over every other check: a retried request reports
        # the line it already produced and never meters volume a second time.
        if request_id and self.manifest.is_applied(request_id):
            existing = self.manifest.applied_line(request_id)
            self.emit(
                "chute.load_replayed",
                {"request_id": request_id, "line_id": existing.line_id, "truck_id": truck_id},
            )
            return self._load_result(existing, idempotent=True)

        spec = self.fleet.require(truck_id)
        if kind == MAKEUP_LOAD:
            if spec.trip_type != "makeup":
                raise InterlockBlockedError(
                    "standard trips close the valve at the quota and never merge a makeup load",
                    truck_id=truck_id,
                    trip_type=spec.trip_type,
                )
            ticket = self.manifest.for_truck_batch(truck_id=truck_id, batch_id=batch_id)
            if ticket is None:
                # A truck returning from the site opens its sheet with the
                # first top up; every later top up merges onto that sheet.
                ticket = self.manifest.open_ticket(
                    truck_id=truck_id, batch_id=batch_id, spec=spec
                )
            served = self.queue.serve(truck_id)
        else:
            ticket = self.manifest.open_or_require(
                truck_id=truck_id, batch_id=batch_id, spec=spec, kind=kind
            )
            served = self.queue.serve(truck_id)

        plan_before = open_batch_plan(self.context.batches, self.manifest, batch_id)
        truck_room = round(spec.capacity_m3 - ticket.loaded_m3, 4)
        if kind == MAKEUP_LOAD:
            decision = decide_makeup(
                requested_m3=requested_m3,
                truck_remaining_m3=truck_room,
                batch_remaining_m3=plan_before["remaining_m3"],
            )
        else:
            decision = decide_initial(
                trip_type=spec.trip_type,
                requested_m3=requested_m3,
                truck_remaining_m3=truck_room,
                batch_remaining_m3=plan_before["remaining_m3"],
            )

        line_id = self.context.ids.next("loadline")
        applied_request = request_id or f"auto-{line_id}"
        line = load_record(
            line_id=line_id,
            ticket_id=ticket.ticket_id,
            truck_id=served,
            batch_id=batch_id,
            kind=kind,
            trip_type=spec.trip_type,
            request_id=applied_request,
            requested_m3=decision.requested_m3,
            volume_m3=decision.accepted_m3,
            plan_excess_m3=decision.plan_excess_m3,
            blocked_m3=decision.blocked_m3,
            valve_open=decision.valve_open,
            reason=decision.reason,
            tick=self.context.clock.now(),
        )
        self.manifest.record(ticket, line)

        self.publish(LOAD_KIND, line.describe())
        self.publish(MANIFEST_KIND, {"active": True, **self.manifest.describe()})
        self.publish(
            VALVE_KIND,
            valve_state(
                open=decision.valve_open,
                batch_id=batch_id,
                truck_id=served,
                reason=decision.reason,
            ).describe(),
        )
        if kind == INITIAL_LOAD and self.machine.is_at(MixPhase.MIXED.value):
            self.advance(MixPhase.DISCHARGED.value, "truck loaded from the discharge door")
        self.emit("chute.loaded", line.describe())

        if line.blocked_m3 > 0.0:
            self.raise_alarm(
                "chute.load_blocked",
                {
                    "truck_id": served,
                    "batch_id": batch_id,
                    "ticket_id": ticket.ticket_id,
                    "trip_type": spec.trip_type,
                    "requested_m3": line.requested_m3,
                    "accepted_m3": line.volume_m3,
                    "blocked_m3": line.blocked_m3,
                    "reason": line.reason,
                },
            )
        plan_after = open_batch_plan(self.context.batches, self.manifest, batch_id)
        return self._load_result(line, idempotent=False, plan=plan_after)

    def confirm(self, *, truck_id: str, volume_m3: float) -> dict[str, Any]:
        """Confirm the volume on a truck's loading sheet; served in queue order."""

        with self._lock:
            self.require("chute.confirm")
            ticket = self.manifest.require(truck_id)
            if abs(ticket.loaded_m3 - volume_m3) > 0.5:
                self.raise_alarm(
                    "chute.volume_mismatch",
                    {
                        "truck_id": truck_id,
                        "recorded": ticket.loaded_m3,
                        "confirmed": volume_m3,
                    },
                )
            self.manifest.confirm(ticket, volume_m3)
            self.publish(
                CONFIRM_KIND,
                {
                    "active": True,
                    "ticket_id": ticket.ticket_id,
                    "truck_id": truck_id,
                    "batch_id": ticket.batch_id,
                    "confirmed_m3": round(volume_m3, 4),
                },
            )
            self.publish(MANIFEST_KIND, {"active": True, **self.manifest.describe()})
            self.emit(
                "chute.confirmed",
                {"truck_id": truck_id, "ticket_id": ticket.ticket_id},
            )
            return {"confirmed": ticket.describe(), "manifest": self.manifest.describe()}

    def close_door(self) -> dict[str, Any]:
        """Close the door and end the loading window."""

        with self._lock:
            self.require("chute.close")
            batch_id = self._open_batch or ""
            self._open_batch = None
            self.publish(DOOR_KIND, door_state(open=False, batch_id=batch_id).describe())
            self.publish(
                VALVE_KIND,
                valve_state(open=False, batch_id=batch_id, reason="door_closed").describe(),
            )
            self.emit("chute.door_closed", {"batch_id": batch_id})
            return {
                "door": door_state(open=False, batch_id=batch_id).describe(),
                "manifest": self.manifest.describe(),
            }

    def reconcile_totals(self) -> dict[str, Any]:
        """Compare loading sheets, dispatch board and audit ledger."""

        with self._lock:
            board = board_from_records(self.store.history(LOAD_KIND))
            audit = audit_totals(
                record.payload for record in self.store.history(AUDIT_ENTRY_KIND)
            )
            report = reconcile(
                manifest=self.manifest,
                board=board,
                audit=audit,
                batches=self.context.batches,
            )
            self.publish(BOARD_KIND, {"active": True, **board})
            self.publish(RECONCILE_KIND, {"active": True, **report})
            self.emit("chute.reconciled", {"consistent": report["consistent"]})
            if not report["consistent"]:
                self.raise_alarm("chute.reconcile_mismatch", {"reasons": report["reasons"], **report["totals"]})
            return {"report": report, "board": board, "audit": audit}

    def adopt_fleet(self, payload: dict[str, Any]) -> None:
        """Rebuild the fleet from one cumulative fleet snapshot."""

        for truck in payload.get("trucks", ()):
            self.fleet.adopt(
                truck_spec(
                    truck_id=str(truck["truck_id"]),
                    capacity_m3=float(truck["capacity_m3"]),
                    trip_type=str(truck["trip_type"]),
                )
            )

    def adopt_load(self, payload: dict[str, Any]) -> None:
        """Rebuild one ticket line from a committed load record."""

        self.manifest.replay(
            load_record(
                line_id=str(payload["line_id"]),
                ticket_id=str(payload["ticket_id"]),
                truck_id=str(payload["truck_id"]),
                batch_id=str(payload["batch_id"]),
                kind=str(payload["kind"]),
                trip_type=str(payload["trip_type"]),
                request_id=str(payload["request_id"]),
                requested_m3=float(payload["requested_m3"]),
                volume_m3=float(payload["volume_m3"]),
                plan_excess_m3=float(payload.get("plan_excess_m3", 0.0)),
                blocked_m3=float(payload.get("blocked_m3", 0.0)),
                valve_open=bool(payload.get("valve_open", False)),
                reason=str(payload.get("reason", "replayed")),
                tick=int(payload.get("tick", 0)),
            )
        )

    def adopt_confirmation(self, payload: dict[str, Any]) -> None:
        ticket = self.manifest.for_ticket(str(payload["ticket_id"]))
        if ticket is not None:
            self.manifest.confirm(ticket, float(payload["confirmed_m3"]))

    def adopt_door(self, payload: dict[str, Any]) -> None:
        """Reopen the in memory batch pointer when the door stayed open."""

        if payload.get("active", False):
            self._open_batch = str(payload.get("batch_id", "")) or None
        else:
            self._open_batch = None

    def _queue_view(self) -> dict[str, Any]:
        view = self.queue.describe()
        view["capacity"] = queue_capacity(
            waiting=self.queue.order(),
            fleet=self.fleet,
            manifest=self.manifest,
            batches=self.context.batches,
            batch_id=self._open_batch,
        )
        return view

    def _load_result(self, line, *, idempotent: bool, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        plan = plan or (
            open_batch_plan(self.context.batches, self.manifest, line.batch_id)
            if line.batch_id
            else None
        )
        return {
            "load": line.describe(),
            "ticket": self.manifest.require_ticket(line.ticket_id).describe(),
            "manifest": self.manifest.describe(),
            "plan": plan,
            "idempotent": idempotent,
            "blocked": line.blocked_m3 > 0.0,
        }

    def status(self) -> dict[str, Any]:
        door = self.payload_entry("chute", "door")
        load = self.payload_entry("chute", "load")
        valve = self.payload_entry("chute", "valve")
        manifest = self.manifest.describe()
        plan = None
        if self._open_batch:
            plan = open_batch_plan(self.context.batches, self.manifest, self._open_batch)
            manifest["batch_tickets"] = [
                item.describe() for item in self.manifest.for_batch(self._open_batch)
            ]
        return {
            "door_open": bool(door.get("active", False)) if door else False,
            "open_batch": None if door is None else door.get("batch_id"),
            "valve_open": bool(valve.get("open", False)) if valve else False,
            "valve": dict(valve) if valve else None,
            "queue": self._queue_view(),
            "fleet": self.fleet.describe(),
            "manifest": manifest,
            "plan": plan,
            "board": board_from_records(self.store.history(LOAD_KIND)),
            "last_load": None if load is None else dict(load),
        }
