"""Turning a requested volume into a delivered volume under a trip policy.

This is the single decision point the chute consults before handing concrete
to a truck.  It never mutates anything; the service applies the returned
decision and records it.  Keeping the arithmetic pure makes the over-quantity
policy testable and identical on a live run and after a replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError
from .capacity import CLOSE, EPSILON_M3, MERGE, TruckProfile, headroom, truck_remaining


@dataclass(frozen=True, slots=True)
class LoadDecision:
    """What the chute must do for one ``chute.load`` request.

    ``accepted_m3`` is delivered and counted once.  ``blocked_m3`` is refused
    on the spot and never enters the ticket.  ``close_valve`` tells the door
    whether this load reaches the trip's target.  ``merged`` marks a top up
    completed onto an already open ticket instead of opening a new one.
    """

    truck_id: str
    batch_id: str
    trip: str
    action: str
    requested_m3: float
    accepted_m3: float
    blocked_m3: float
    overloaded: bool
    close_valve: bool
    merged: bool
    reason: str

    @property
    def blocked(self) -> bool:
        return self.blocked_m3 > EPSILON_M3

    def describe(self) -> dict[str, Any]:
        return {
            "truck_id": self.truck_id,
            "batch_id": self.batch_id,
            "trip": self.trip,
            "over_quantity_action": self.action,
            "requested_m3": round(self.requested_m3, 4),
            "accepted_m3": round(self.accepted_m3, 4),
            "blocked_m3": round(self.blocked_m3, 4),
            "overloaded": self.overloaded,
            "valve_closed": self.close_valve,
            "merged": self.merged,
            "reason": self.reason,
        }


def decide_load(
    profile: TruckProfile,
    *,
    batch_id: str,
    loaded_m3: float,
    requested_m3: float,
    batch_remaining_m3: float | None,
    valve_open: bool,
    merged: bool,
) -> LoadDecision:
    """Decide how much of ``requested_m3`` may be delivered.

    Both trip kinds are bounded by rated capacity (no overload, ever).  A
    primary trip is additionally bounded by the remaining batch plan and, on
    any overflow, clamps to the target and closes the valve.  A top up is
    merged onto its open ticket and only blocked when it would overload.
    """

    if requested_m3 <= 0:
        raise ValidationError("a load needs a positive volume", truck=profile.truck_id)
    if not valve_open:
        raise ValidationError("the discharge door is not open", truck=profile.truck_id)

    room = headroom(
        profile,
        loaded_m3=loaded_m3,
        batch_remaining_m3=batch_remaining_m3,
        valve_open=valve_open,
    )
    allowed = room.allowed_m3
    requested = round(requested_m3, 4)

    if requested <= allowed + EPSILON_M3:
        accepted = min(requested, allowed)
        # A primary trip that lands on its target closes the valve at the plan.
        used_room = allowed - accepted
        closes = profile.trip == "primary" and used_room <= EPSILON_M3
        return LoadDecision(
            truck_id=profile.truck_id,
            batch_id=batch_id,
            trip=profile.trip,
            action=profile.action,
            requested_m3=requested,
            accepted_m3=round(accepted, 4),
            blocked_m3=0.0,
            overloaded=False,
            close_valve=closes,
            merged=merged,
            reason="at_target" if closes else "within_target",
        )

    accepted = allowed
    blocked = round(requested - allowed, 4)
    truck_left = truck_remaining(profile, loaded_m3)
    overloaded = allowed >= truck_left - EPSILON_M3

    if profile.action == CLOSE:
        # Primary: deliver up to the target, block the rest, close the valve.
        return LoadDecision(
            truck_id=profile.truck_id,
            batch_id=batch_id,
            trip=profile.trip,
            action=CLOSE,
            requested_m3=requested,
            accepted_m3=round(accepted, 4),
            blocked_m3=blocked,
            overloaded=overloaded,
            close_valve=True,
            merged=False,
            reason="over_target_blocked_and_valve_closed",
        )

    # Top up: only rated capacity blocks it; the remainder merges on the ticket.
    return LoadDecision(
        truck_id=profile.truck_id,
        batch_id=batch_id,
        trip=profile.trip,
        action=MERGE,
        requested_m3=requested,
        accepted_m3=round(accepted, 4),
        blocked_m3=blocked,
        overloaded=overloaded,
        close_valve=True,
        merged=True,
        reason="over_capacity_blocked_rest_merged" if not overloaded else "over_capacity_blocked",
    )
