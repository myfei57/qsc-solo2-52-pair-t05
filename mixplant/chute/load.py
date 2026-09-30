"""One metered load line taken from the discharge door.

A line is what the scale/flow meter reported for one ``chute.load`` or
``chute.makeup`` application.  Several lines can share one loading ticket
when a makeup trip comes back for a top up; ``request_id`` keeps a retried or
concurrently triggered application from being metered twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError
from .policy import INITIAL_LOAD, MAKEUP_LOAD, ROUND_DIGITS, require_trip_type

LOAD_KINDS = frozenset({INITIAL_LOAD, MAKEUP_LOAD})


@dataclass(frozen=True, slots=True)
class LoadRecord:
    """One metered line handed to a truck."""

    line_id: str
    ticket_id: str
    truck_id: str
    batch_id: str
    kind: str
    trip_type: str
    request_id: str
    requested_m3: float
    volume_m3: float
    plan_excess_m3: float
    blocked_m3: float
    valve_open: bool
    reason: str
    tick: int

    def __post_init__(self) -> None:
        if not self.truck_id:
            raise ValidationError("a load needs a truck identity")
        if not self.batch_id:
            raise ValidationError("a load needs a batch identity")
        if self.kind not in LOAD_KINDS:
            raise ValidationError("unknown load kind", kind=self.kind)
        require_trip_type(self.trip_type)
        if self.requested_m3 <= 0:
            raise ValidationError("a load needs a positive requested volume", truck=self.truck_id)
        if self.volume_m3 < 0:
            raise ValidationError("a metered volume must not be negative", truck=self.truck_id)
        if self.volume_m3 > self.requested_m3 + 1e-9:
            raise ValidationError("a load cannot meter more than was requested", truck=self.truck_id)

    @property
    def is_makeup(self) -> bool:
        return self.kind == MAKEUP_LOAD

    def describe(self) -> dict[str, Any]:
        return {
            "active": True,
            "line_id": self.line_id,
            "ticket_id": self.ticket_id,
            "truck_id": self.truck_id,
            "batch_id": self.batch_id,
            "kind": self.kind,
            "trip_type": self.trip_type,
            "request_id": self.request_id,
            "requested_m3": self.requested_m3,
            "volume_m3": self.volume_m3,
            "plan_excess_m3": self.plan_excess_m3,
            "blocked_m3": self.blocked_m3,
            "valve_open": self.valve_open,
            "reason": self.reason,
            "confirmed": False,
            "confirmed_m3": None,
            "tick": self.tick,
        }


def load_record(
    *,
    line_id: str,
    ticket_id: str,
    truck_id: str,
    batch_id: str,
    kind: str,
    trip_type: str,
    request_id: str,
    requested_m3: float,
    volume_m3: float,
    plan_excess_m3: float,
    blocked_m3: float,
    valve_open: bool,
    reason: str,
    tick: int,
) -> LoadRecord:
    """Build one metered load line."""

    return LoadRecord(
        line_id=line_id,
        ticket_id=ticket_id,
        truck_id=truck_id,
        batch_id=batch_id,
        kind=kind,
        trip_type=trip_type,
        request_id=request_id,
        requested_m3=round(requested_m3, ROUND_DIGITS),
        volume_m3=round(volume_m3, ROUND_DIGITS),
        plan_excess_m3=round(plan_excess_m3, ROUND_DIGITS),
        blocked_m3=round(blocked_m3, ROUND_DIGITS),
        valve_open=bool(valve_open),
        reason=reason,
        tick=tick,
    )
