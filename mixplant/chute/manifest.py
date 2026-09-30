"""Loading tickets: one sheet per truck per batch, makeup loads merge onto it.

A ticket is the "装车单": it opens with the first load a truck takes from a
batch and accumulates every later makeup top up the trip type allows.  The
book keeps the sheets in write order and, crucially, indexes every applied
``request_id`` so a concurrently triggered or retried application returns the
original line instead of metering the volume twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import DuplicateError, NotFoundError, ValidationError
from .fleet import TruckSpec
from .load import LoadRecord
from .policy import MAKEUP_TRIP, ROUND_DIGITS, STANDARD_TRIP


@dataclass(slots=True)
class LoadTicket:
    """One loading sheet covering every line a truck took from one batch."""

    ticket_id: str
    truck_id: str
    batch_id: str
    trip_type: str
    capacity_m3: float
    lines: list[LoadRecord] = field(default_factory=list)
    request_ids: set[str] = field(default_factory=set)
    confirmed: bool = False
    confirmed_m3: float | None = None

    def add(self, line: LoadRecord) -> LoadTicket:
        if line.ticket_id != self.ticket_id:
            raise ValidationError("line belongs to another ticket", line=line.ticket_id, ticket=self.ticket_id)
        if line.request_id in self.request_ids:
            raise DuplicateError(
                "this loading request was already applied",
                request_id=line.request_id,
                truck_id=self.truck_id,
            )
        self.lines.append(line)
        self.request_ids.add(line.request_id)
        return self

    def line_for(self, request_id: str) -> LoadRecord | None:
        for line in self.lines:
            if line.request_id == request_id:
                return line
        return None

    @property
    def loaded_m3(self) -> float:
        return round(sum(line.volume_m3 for line in self.lines), ROUND_DIGITS)

    @property
    def plan_excess_m3(self) -> float:
        return round(sum(line.plan_excess_m3 for line in self.lines), ROUND_DIGITS)

    @property
    def blocked_m3(self) -> float:
        return round(sum(line.blocked_m3 for line in self.lines), ROUND_DIGITS)

    @property
    def planned_m3(self) -> float:
        """The accepted volume counted against the batch plan."""

        return round(self.loaded_m3 - self.plan_excess_m3, ROUND_DIGITS)

    @property
    def remaining_capacity_m3(self) -> float:
        return round(max(0.0, self.capacity_m3 - self.loaded_m3), ROUND_DIGITS)

    def confirm(self, volume_m3: float) -> None:
        self.confirmed = True
        self.confirmed_m3 = round(volume_m3, ROUND_DIGITS)

    def describe(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "truck_id": self.truck_id,
            "batch_id": self.batch_id,
            "trip_type": self.trip_type,
            "capacity_m3": self.capacity_m3,
            "loaded_m3": self.loaded_m3,
            "planned_m3": self.planned_m3,
            "plan_excess_m3": self.plan_excess_m3,
            "blocked_m3": self.blocked_m3,
            "remaining_capacity_m3": self.remaining_capacity_m3,
            "confirmed": self.confirmed,
            "confirmed_m3": self.confirmed_m3,
            "lines": [line.describe() for line in self.lines],
        }


def ticket_id_for(*, batch_id: str, truck_id: str) -> str:
    """A truck holds one open sheet per batch it is loaded from."""

    return f"{batch_id}:{truck_id}"


class LoadManifest:
    """The book of loading sheets, in the order they were opened."""

    __slots__ = ("_tickets", "_requests")

    def __init__(self) -> None:
        self._tickets: dict[str, LoadTicket] = {}
        self._requests: dict[str, str] = {}

    def open_ticket(self, *, truck_id: str, batch_id: str, spec: TruckSpec) -> LoadTicket:
        """Open a fresh sheet, refusing a reused one."""

        ticket_id = ticket_id_for(batch_id=batch_id, truck_id=truck_id)
        if ticket_id in self._tickets:
            raise DuplicateError(
                "truck already holds a loading sheet for this batch",
                truck_id=truck_id,
                batch_id=batch_id,
                ticket_id=ticket_id,
            )
        ticket = LoadTicket(
            ticket_id=ticket_id,
            truck_id=truck_id,
            batch_id=batch_id,
            trip_type=spec.trip_type,
            capacity_m3=spec.capacity_m3,
        )
        self._tickets[ticket_id] = ticket
        return ticket

    def open_or_require(
        self,
        *,
        truck_id: str,
        batch_id: str,
        spec: TruckSpec,
        kind: str,
    ) -> LoadTicket:
        """Open the sheet for a first load, or refuse a repeated first load."""

        ticket_id = ticket_id_for(batch_id=batch_id, truck_id=truck_id)
        existing = self._tickets.get(ticket_id)
        if existing is not None:
            raise DuplicateError(
                "truck already holds a loading sheet for this batch; use a makeup load",
                truck_id=truck_id,
                batch_id=batch_id,
                ticket_id=ticket_id,
            )
        return self.open_ticket(truck_id=truck_id, batch_id=batch_id, spec=spec)

    def for_truck_batch(self, *, truck_id: str, batch_id: str) -> LoadTicket | None:
        return self._tickets.get(ticket_id_for(batch_id=batch_id, truck_id=truck_id))

    def require_makeup(self, *, truck_id: str, batch_id: str) -> LoadTicket:
        """Return the open sheet a makeup top up merges onto."""

        ticket = self.for_truck_batch(truck_id=truck_id, batch_id=batch_id)
        if ticket is None:
            raise NotFoundError(
                "a makeup load needs an existing loading sheet",
                truck_id=truck_id,
                batch_id=batch_id,
            )
        if ticket.trip_type != MAKEUP_TRIP:
            raise ValidationError(
                "only makeup trips merge extra volume onto their loading sheet",
                truck_id=truck_id,
                trip_type=STANDARD_TRIP,
            )
        return ticket

    def record(self, ticket: LoadTicket, line: LoadRecord) -> LoadRecord:
        """Append one line; a repeated request id never meters volume twice."""

        if line.request_id in self._requests:
            raise DuplicateError(
                "this loading request was already applied",
                request_id=line.request_id,
                ticket_id=self._requests[line.request_id],
            )
        ticket.add(line)
        self._requests[line.request_id] = ticket.ticket_id
        return line

    def replay(self, line: LoadRecord) -> LoadRecord:
        """Adopt one line read back from the committed stream."""

        ticket = self._tickets.get(line.ticket_id)
        if ticket is None:
            ticket = LoadTicket(
                ticket_id=line.ticket_id,
                truck_id=line.truck_id,
                batch_id=line.batch_id,
                trip_type=line.trip_type,
                capacity_m3=0.0,
            )
            self._tickets[line.ticket_id] = ticket
        if line.request_id not in ticket.request_ids:
            ticket.lines.append(line)
            ticket.request_ids.add(line.request_id)
            self._requests[line.request_id] = ticket.ticket_id
        return line

    def confirm(self, ticket: LoadTicket, volume_m3: float) -> None:
        ticket.confirm(volume_m3)

    def is_applied(self, request_id: str) -> bool:
        return request_id in self._requests

    def applied_line(self, request_id: str) -> LoadRecord:
        ticket_id = self._requests[request_id]
        line = self._tickets[ticket_id].line_for(request_id)
        assert line is not None
        return line

    def require(self, truck_id: str) -> LoadTicket:
        for ticket in self._tickets.values():
            if ticket.truck_id == truck_id:
                return ticket
        raise NotFoundError("truck never received a load", truck_id=truck_id)

    def require_ticket(self, ticket_id: str) -> LoadTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise NotFoundError("loading sheet does not exist", ticket_id=ticket_id)
        return ticket

    def for_ticket(self, ticket_id: str) -> LoadTicket | None:
        return self._tickets.get(ticket_id)

    def for_truck(self, truck_id: str) -> tuple[LoadTicket, ...]:
        return tuple(ticket for ticket in self._tickets.values() if ticket.truck_id == truck_id)

    def for_batch(self, batch_id: str) -> tuple[LoadTicket, ...]:
        return tuple(ticket for ticket in self._tickets.values() if ticket.batch_id == batch_id)

    def tickets(self) -> tuple[LoadTicket, ...]:
        return tuple(self._tickets.values())

    def planned_for_batch(self, batch_id: str) -> float:
        return round(sum(ticket.planned_m3 for ticket in self.for_batch(batch_id)), ROUND_DIGITS)

    def describe(self) -> dict[str, Any]:
        tickets = list(self._tickets.values())
        return {
            "tickets": [ticket.describe() for ticket in tickets],
            "loads": [line.describe() for ticket in tickets for line in ticket.lines],
            "trucks": sorted({ticket.truck_id for ticket in tickets}),
            "total_m3": round(sum(ticket.loaded_m3 for ticket in tickets), ROUND_DIGITS),
            "plan_excess_m3": round(sum(ticket.plan_excess_m3 for ticket in tickets), ROUND_DIGITS),
            "blocked_m3": round(sum(ticket.blocked_m3 for ticket in tickets), ROUND_DIGITS),
        }
