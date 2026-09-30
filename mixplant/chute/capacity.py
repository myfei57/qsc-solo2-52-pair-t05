"""Trip kinds, the fixed over-quantity policy and the remaining-capacity sum.

The over-quantity behaviour is *decided once, here*, per trip kind.  Nothing
else in the codebase may choose between closing the valve and allowing a top
up: a truck presents a trip kind and the policy below is the answer.

Two trip kinds exist:

``primary``
    A first, scheduled load against the batch plan.  The truck may take at
    most ``min(remaining truck rated capacity, remaining batch plan)``.  When
    it asks for more, nothing extra is delivered: the requested overflow is
    blocked on the spot and the valve is closed at the target.

``topup``
    A return trip by the same truck, carrying one open ticket.  It may be
    completed onto that same ticket as long as the truck's rated capacity is
    not exceeded.  Overflow past the rated capacity is still blocked; a top
    up is never allowed to overload the truck.

The invariant both policies share is therefore unconditional: the cumulative
volume of a ticket never exceeds the truck's rated capacity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from ..errors import ValidationError

PRIMARY = "primary"
TOPUP = "topup"
TRIP_KINDS = (PRIMARY, TOPUP)

# What happens to quantity past the trip's target.
CLOSE = "close"   # clamp to the target, block the overflow, shut the valve
MERGE = "merge"   # accept onto the same ticket, still capped at rated capacity
ACTIONS = (CLOSE, MERGE)

# A small guard so an accepted float can never sit a rounding epsilon over cap.
EPSILON_M3 = 1e-6


class TripKind(str, Enum):
    """The two ways a truck arrives at the chute."""

    PRIMARY = PRIMARY
    TOPUP = TOPUP

    @property
    def action(self) -> str:
        """Return the fixed over-quantity action for this trip kind."""

        return OVER_QUANTITY_POLICY[self.value]


def normalize_trip(trip: str | None) -> str:
    """Return a known trip kind, defaulting an empty value to ``primary``."""

    value = PRIMARY if trip in (None, "") else str(trip)
    if value not in TRIP_KINDS:
        raise ValidationError("unknown trip kind", trip=value, known=list(TRIP_KINDS))
    return value


#: The policy, fixed by trip kind.  ``plan_bounded`` says whether this trip is
#: capped by the remaining batch plan (primary) or only by rated capacity
#: (a top up is allowed to draw beyond the original plan onto one ticket).
OVER_QUANTITY_POLICY: Mapping[str, str] = {
    PRIMARY: CLOSE,
    TOPUP: MERGE,
}


@dataclass(frozen=True, slots=True)
class TruckProfile:
    """A truck's rated capacity and the kind of trip it is making."""

    truck_id: str
    rated_capacity_m3: float
    trip: str = PRIMARY

    def __post_init__(self) -> None:
        if not self.truck_id:
            raise ValidationError("a truck profile needs a truck identity")
        if self.rated_capacity_m3 <= 0:
            raise ValidationError(
                "rated capacity must be positive", truck=self.truck_id, capacity=self.rated_capacity_m3
            )
        object.__setattr__(self, "trip", normalize_trip(self.trip))

    @property
    def action(self) -> str:
        return OVER_QUANTITY_POLICY[self.trip]

    def describe(self) -> dict[str, Any]:
        return {
            "truck_id": self.truck_id,
            "rated_capacity_m3": self.rated_capacity_m3,
            "trip": self.trip,
            "over_quantity_action": self.action,
        }


@dataclass(frozen=True, slots=True)
class CapacityHeadroom:
    """How much more one truck may take under its trip policy."""

    truck_id: str
    trip: str
    action: str
    loaded_m3: float
    truck_remaining_m3: float
    batch_remaining_m3: float | None
    allowed_m3: float
    closed: bool

    def describe(self) -> dict[str, Any]:
        return {
            "truck_id": self.truck_id,
            "trip": self.trip,
            "over_quantity_action": self.action,
            "loaded_m3": round(self.loaded_m3, 4),
            "truck_remaining_m3": round(self.truck_remaining_m3, 4),
            "batch_remaining_m3": None if self.batch_remaining_m3 is None else round(self.batch_remaining_m3, 4),
            "allowed_m3": round(self.allowed_m3, 4),
            "valve_closed": self.closed,
        }


def truck_remaining(profile: TruckProfile, loaded_m3: float) -> float:
    """Return the rated capacity the truck has not used yet (never negative)."""

    return round(max(0.0, profile.rated_capacity_m3 - loaded_m3), 4)


def headroom(
    profile: TruckProfile,
    *,
    loaded_m3: float,
    batch_remaining_m3: float | None,
    valve_open: bool,
) -> CapacityHeadroom:
    """Compute how much more concrete this truck may take right now.

    For a primary trip the allowance is bounded by both the truck and the
    remaining batch plan.  A top up is bounded only by rated capacity so an
    allowed completion can be merged onto the same ticket.
    """

    trip = normalize_trip(profile.trip)
    truck_left = truck_remaining(profile, loaded_m3)
    if trip == PRIMARY:
        plan_left = None if batch_remaining_m3 is None else max(0.0, batch_remaining_m3)
        allowed = truck_left if plan_left is None else min(truck_left, plan_left)
    else:
        plan_left = batch_remaining_m3
        allowed = truck_left
    closed = (not valve_open) or allowed <= EPSILON_M3
    return CapacityHeadroom(
        truck_id=profile.truck_id,
        trip=trip,
        action=OVER_QUANTITY_POLICY[trip],
        loaded_m3=loaded_m3,
        truck_remaining_m3=truck_left,
        batch_remaining_m3=plan_left,
        allowed_m3=round(allowed, 4),
        closed=closed,
    )
