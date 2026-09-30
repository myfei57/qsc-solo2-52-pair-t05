"""A loading ticket: the cumulative, idempotent account of one truck's load.

A ticket is the unit that the dispatch board and the audit ledger reconcile
against.  Every concrete quantity the truck receives is appended as a ``part``.
A part carries the idempotency key the field device supplied; a repeated key
returns the earlier part without adding volume again, even under concurrent
calls (the service serialises calls for one truck).

A top up does not open a new ticket: it is appended to the truck's still open
ticket, so one return trip stays on one bill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, ValidationError
from .capacity import EPSILON_M3, PRIMARY


@dataclass(frozen=True, slots=True)
class LoadPart:
    """One counted delivery of concrete onto a ticket."""

    request_id: str
    batch_id: str
    trip: str
    volume_m3: float
    blocked_m3: float
    tick: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValidationError("a load part needs an idempotency key")
        if self.volume_m3 <= 0:
            raise ValidationError("a load part needs a positive volume", request_id=self.request_id)
        if self.blocked_m3 < 0:
            raise ValidationError("a blocked volume cannot be negative", request_id=self.request_id)

    def describe(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "trip": self.trip,
            "volume_m3": self.volume_m3,
            "blocked_m3": self.blocked_m3,
            "tick": self.tick,
        }


@dataclass(slots=True)
class LoadingTicket:
    """The running bill for one truck.

    ``rated_capacity_m3`` is the hard ceiling the aggregate itself enforces so
    an overload can never be appended regardless of the caller.
    """

    ticket_id: str
    truck_id: str
    rated_capacity_m3: float
    batch_id: str
    parts: list[LoadPart] = field(default_factory=list)
    _seen: dict[str, int] = field(default_factory=dict)
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.ticket_id:
            raise ValidationError("a ticket needs an identity")
        if not self.truck_id:
            raise ValidationError("a ticket needs a truck identity")
        if self.rated_capacity_m3 <= 0:
            raise ValidationError("a ticket needs a positive rated capacity")
        if not self._seen:
            self._seen = {part.request_id: index for index, part in enumerate(self.parts)}

    @property
    def total_m3(self) -> float:
        return round(sum(part.volume_m3 for part in self.parts), 4)

    @property
    def blocked_m3(self) -> float:
        return round(sum(part.blocked_m3 for part in self.parts), 4)

    @property
    def remaining_m3(self) -> float:
        return round(max(0.0, self.rated_capacity_m3 - self.total_m3), 4)

    @property
    def trips(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(part.trip for part in self.parts))

    def find(self, request_id: str) -> LoadPart | None:
        """Return the part an idempotency key already produced, if any."""

        index = self._seen.get(request_id)
        return None if index is None else self.parts[index]

    def append(
        self,
        *,
        request_id: str,
        batch_id: str,
        trip: str,
        volume_m3: float,
        blocked_m3: float,
        tick: int,
    ) -> LoadPart:
        """Append a part, idempotently, refusing any cumulative overload."""

        repeat = self.find(request_id)
        if repeat is not None:
            return repeat
        if self.closed:
            raise DuplicateError("the ticket is closed and takes no more loads", ticket_id=self.ticket_id)
        if volume_m3 <= 0:
            raise ValidationError("a counted load needs a positive volume", request_id=request_id)
        if self.total_m3 + volume_m3 > self.rated_capacity_m3 + EPSILON_M3:
            # Defence in depth: the allocation decision caps first, the
            # aggregate refuses too, so the rated ceiling is never breached.
            raise ValidationError(
                "the load would exceed the truck's rated capacity",
                ticket_id=self.ticket_id,
                truck_id=self.truck_id,
                total_m3=self.total_m3,
                requested_m3=volume_m3,
                rated_capacity_m3=self.rated_capacity_m3,
            )
        part = LoadPart(
            request_id=request_id,
            batch_id=batch_id,
            trip=trip,
            volume_m3=round(volume_m3, 4),
            blocked_m3=round(blocked_m3, 4),
            tick=tick,
        )
        self._seen[request_id] = len(self.parts)
        self.parts.append(part)
        return part

    def mark_closed(self) -> None:
        self.closed = True

    def describe(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "truck_id": self.truck_id,
            "batch_id": self.batch_id,
            "rated_capacity_m3": self.rated_capacity_m3,
            "total_m3": self.total_m3,
            "remaining_m3": self.remaining_m3,
            "blocked_m3": self.blocked_m3,
            "trips": list(self.trips),
            "parts": [part.describe() for part in self.parts],
            "closed": self.closed,
        }


class TicketBook:
    """Every open and closed ticket, with the open ticket of each truck."""

    __slots__ = ("_tickets", "_open_by_truck")

    def __init__(self) -> None:
        self._tickets: dict[str, LoadingTicket] = {}
        self._open_by_truck: dict[str, str] = {}

    def open(
        self,
        *,
        ticket_id: str,
        truck_id: str,
        rated_capacity_m3: float,
        batch_id: str,
    ) -> LoadingTicket:
        """Open a ticket for a truck that does not already hold an open one."""

        if ticket_id in self._tickets:
            raise DuplicateError("ticket identity was reused", ticket_id=ticket_id)
        open_id = self._open_by_truck.get(truck_id)
        if open_id is not None:
            raise DuplicateError(
                "truck already has an open ticket", truck_id=truck_id, ticket_id=open_id
            )
        ticket = LoadingTicket(
            ticket_id=ticket_id,
            truck_id=truck_id,
            rated_capacity_m3=rated_capacity_m3,
            batch_id=batch_id,
        )
        self._tickets[ticket_id] = ticket
        self._open_by_truck[truck_id] = ticket_id
        return ticket

    def require(self, ticket_id: str) -> LoadingTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise NotFoundError("no such loading ticket", ticket_id=ticket_id)
        return ticket

    def open_ticket(self, truck_id: str) -> LoadingTicket | None:
        ticket_id = self._open_by_truck.get(truck_id)
        return None if ticket_id is None else self._tickets[ticket_id]

    def for_truck(self, truck_id: str) -> tuple[LoadingTicket, ...]:
        return tuple(ticket for ticket in self._tickets.values() if ticket.truck_id == truck_id)

    def for_batch(self, batch_id: str) -> tuple[LoadingTicket, ...]:
        return tuple(ticket for ticket in self._tickets.values() if ticket.batch_id == batch_id)

    def close(self, ticket_id: str) -> LoadingTicket:
        ticket = self.require(ticket_id)
        ticket.mark_closed()
        if self._open_by_truck.get(ticket.truck_id) == ticket_id:
            self._open_by_truck.pop(ticket.truck_id)
        return ticket

    def total_for_batch(self, batch_id: str) -> float:
        return round(sum(ticket.total_m3 for ticket in self.for_batch(batch_id)), 4)

    def adopt(self, ticket: LoadingTicket) -> LoadingTicket:
        """Put a rebuilt ticket back after a restart."""

        self._tickets[ticket.ticket_id] = ticket
        if not ticket.closed:
            self._open_by_truck[ticket.truck_id] = ticket.ticket_id
        return ticket

    def all(self) -> tuple[LoadingTicket, ...]:
        return tuple(self._tickets.values())

    def describe(self) -> dict[str, Any]:
        return {
            "tickets": [ticket.describe() for ticket in self._tickets.values()],
            "total_m3": round(sum(ticket.total_m3 for ticket in self._tickets.values()), 4),
            "open": {truck: ticket for truck, ticket in sorted(self._open_by_truck.items())},
        }


def part_document(document: Mapping[str, Any]) -> LoadPart:
    """Rebuild one load part from a stored document."""

    return LoadPart(
        request_id=str(document["request_id"]),
        batch_id=str(document.get("batch_id", "")),
        trip=str(document.get("trip", PRIMARY)),
        volume_m3=float(document["volume_m3"]),
        blocked_m3=float(document.get("blocked_m3", 0.0)),
        tick=int(document.get("tick", 0)),
    )


def ticket_document(document: Mapping[str, Any]) -> LoadingTicket:
    """Rebuild a ticket from a stored cumulative document."""

    parts = [part_document(item) for item in document.get("parts", [])]
    ticket = LoadingTicket(
        ticket_id=str(document["ticket_id"]),
        truck_id=str(document["truck_id"]),
        rated_capacity_m3=float(document["rated_capacity_m3"]),
        batch_id=str(document.get("batch_id", "")),
        parts=parts,
    )
    if bool(document.get("closed", False)):
        ticket.mark_closed()
    return ticket
