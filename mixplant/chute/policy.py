"""Trip type policy: what happens the moment a load would cross a quota.

Two quotas bound every load: the legal capacity of the truck and the planned
volume of the batch under the discharge door.  What happens when a requested
volume crosses either bound is fixed per trip type and must never be decided
by the driver or the operator on the spot:

* ``standard`` trips close the loading valve at the quota.  The portion past
  the quota is blocked at the chute and is never merged into a later load.
* ``makeup`` trips may pass the batch plan (a truck that came back short is
  topped up onto the same loading sheet), but the legal truck capacity stays
  a hard ceiling: past it the valve closes just like a standard trip.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ValidationError

STANDARD_TRIP = "standard"
MAKEUP_TRIP = "makeup"
TRIP_TYPES: frozenset[str] = frozenset({STANDARD_TRIP, MAKEUP_TRIP})

INITIAL_LOAD = "initial"
MAKEUP_LOAD = "makeup"

ROUND_DIGITS = 4


@dataclass(frozen=True, slots=True)
class LoadDecision:
    """How much of one requested load is accepted and why the valve moved.

    ``accepted_m3`` is metered onto the truck; ``blocked_m3`` is the portion
    refused at the chute; ``plan_excess_m3`` is the accepted portion that
    sits beyond the batch plan and is only possible on makeup trips.
    """

    kind: str
    trip_type: str
    requested_m3: float
    accepted_m3: float
    blocked_m3: float
    plan_excess_m3: float
    valve_open: bool
    reason: str

    def describe(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "trip_type": self.trip_type,
            "requested_m3": self.requested_m3,
            "accepted_m3": self.accepted_m3,
            "blocked_m3": self.blocked_m3,
            "plan_excess_m3": self.plan_excess_m3,
            "valve_open": self.valve_open,
            "reason": self.reason,
        }


def require_trip_type(trip_type: str) -> str:
    """Refuse a trip type the policy table does not know."""

    if trip_type not in TRIP_TYPES:
        raise ValidationError("unknown trip type", trip_type=trip_type, allowed=sorted(TRIP_TYPES))
    return trip_type


def decide_initial(
    *,
    trip_type: str,
    requested_m3: float,
    truck_remaining_m3: float,
    batch_remaining_m3: float,
) -> LoadDecision:
    """Decide the first load a truck takes from an open batch."""

    require_trip_type(trip_type)
    requested = round(requested_m3, ROUND_DIGITS)
    truck_room = max(0.0, round(truck_remaining_m3, ROUND_DIGITS))
    batch_room = max(0.0, round(batch_remaining_m3, ROUND_DIGITS))

    if trip_type == STANDARD_TRIP:
        accepted = min(requested, truck_room, batch_room)
        plan_excess = 0.0
        if accepted == requested:
            reason = "within_quota"
        elif truck_room <= batch_room:
            reason = "clamped_to_truck_capacity"
        else:
            reason = "clamped_to_batch_plan"
    else:
        accepted = min(requested, truck_room)
        plan_excess = max(0.0, round(accepted - batch_room, ROUND_DIGITS))
        if accepted == requested and plan_excess == 0.0:
            reason = "within_quota"
        elif accepted == requested:
            reason = "merged_beyond_plan"
        else:
            reason = "clamped_to_truck_capacity"

    return _decision(INITIAL_LOAD, trip_type, requested, accepted, plan_excess, reason)


def decide_makeup(
    *,
    requested_m3: float,
    truck_remaining_m3: float,
    batch_remaining_m3: float,
) -> LoadDecision:
    """Decide a later top up; callers must only reach this for makeup trips."""

    requested = round(requested_m3, ROUND_DIGITS)
    truck_room = max(0.0, round(truck_remaining_m3, ROUND_DIGITS))
    batch_room = max(0.0, round(batch_remaining_m3, ROUND_DIGITS))
    accepted = min(requested, truck_room)
    plan_excess = max(0.0, round(accepted - batch_room, ROUND_DIGITS))
    if accepted < requested:
        reason = "clamped_to_truck_capacity"
    elif plan_excess > 0.0:
        reason = "makeup_beyond_plan"
    else:
        reason = "makeup_within_plan"
    return _decision(MAKEUP_LOAD, MAKEUP_TRIP, requested, accepted, plan_excess, reason)


def _decision(
    kind: str,
    trip_type: str,
    requested: float,
    accepted: float,
    plan_excess: float,
    reason: str,
) -> LoadDecision:
    accepted = round(accepted, ROUND_DIGITS)
    blocked = round(requested - accepted, ROUND_DIGITS)
    plan_excess = round(plan_excess, ROUND_DIGITS)
    # The valve stays open only while the whole requested volume flowed.
    valve_open = blocked == 0.0 and accepted > 0.0
    return LoadDecision(
        kind=kind,
        trip_type=trip_type,
        requested_m3=requested,
        accepted_m3=accepted,
        blocked_m3=blocked,
        plan_excess_m3=plan_excess,
        valve_open=valve_open,
        reason=reason,
    )
