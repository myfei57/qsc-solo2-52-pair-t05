"""The truck fleet: legal capacity and trip type are declared per truck."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import DuplicateError, NotFoundError, ValidationError
from .policy import STANDARD_TRIP, TRIP_TYPES, require_trip_type

DEFAULT_CAPACITY_M3 = 8.0
FLEET_KIND = "chute.fleet"


@dataclass(frozen=True, slots=True)
class TruckSpec:
    """What the plant knows about one truck before it reaches the queue."""

    truck_id: str
    capacity_m3: float
    trip_type: str

    def __post_init__(self) -> None:
        if not self.truck_id:
            raise ValidationError("a truck needs an identity")
        if self.capacity_m3 <= 0:
            raise ValidationError("a truck needs a positive legal capacity", truck=self.truck_id)
        require_trip_type(self.trip_type)

    def describe(self) -> dict[str, Any]:
        return {
            "truck_id": self.truck_id,
            "capacity_m3": self.capacity_m3,
            "trip_type": self.trip_type,
        }


def truck_spec(*, truck_id: str, capacity_m3: float, trip_type: str = STANDARD_TRIP) -> TruckSpec:
    """Build one truck specification."""

    return TruckSpec(
        truck_id=truck_id,
        capacity_m3=round(capacity_m3, 4),
        trip_type=require_trip_type(trip_type),
    )


class TruckFleet:
    """Registered trucks and their declared loading policy."""

    __slots__ = ("_trucks",)

    def __init__(self) -> None:
        self._trucks: dict[str, TruckSpec] = {}

    def register(self, spec: TruckSpec) -> TruckSpec:
        if spec.truck_id in self._trucks:
            raise DuplicateError("truck is already registered", truck_id=spec.truck_id)
        self._trucks[spec.truck_id] = spec
        return spec

    def adopt(self, spec: TruckSpec) -> TruckSpec:
        """Adopt a truck specification read back from the stream."""

        self._trucks[spec.truck_id] = spec
        return spec

    def require(self, truck_id: str) -> TruckSpec:
        spec = self._trucks.get(truck_id)
        if spec is None:
            raise NotFoundError("truck was never registered", truck_id=truck_id)
        return spec

    def known(self, truck_id: str) -> bool:
        return truck_id in self._trucks

    def describe(self) -> dict[str, Any]:
        trucks = sorted(self._trucks.values(), key=lambda spec: spec.truck_id)
        return {
            "trucks": [spec.describe() for spec in trucks],
            "trip_types": sorted(TRIP_TYPES),
        }
