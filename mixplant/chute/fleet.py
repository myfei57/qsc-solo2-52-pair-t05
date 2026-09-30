"""The fleet: which truck identities are known and what they can carry.

A truck is registered once with its rated capacity and the trip kind it is
making.  The registry also hands back a default profile for an unknown truck
so the manual chute stays usable; production deployments register trucks
explicitly through ``chute.register``.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import DuplicateError, ValidationError
from .capacity import PRIMARY, TruckProfile, normalize_trip


class TruckFleet:
    """Every truck the chute may load, keyed by truck identity."""

    __slots__ = ("_trucks", "_default_capacity_m3", "_default_trip")

    def __init__(self, *, default_capacity_m3: float = 8.0, default_trip: str = PRIMARY) -> None:
        if default_capacity_m3 <= 0:
            raise ValidationError("default capacity must be positive", capacity=default_capacity_m3)
        self._trucks: dict[str, TruckProfile] = {}
        self._default_capacity_m3 = float(default_capacity_m3)
        self._default_trip = normalize_trip(default_trip)

    def register(
        self,
        truck_id: str,
        *,
        rated_capacity_m3: float,
        trip: str = PRIMARY,
    ) -> TruckProfile:
        """Register a truck and refuse a re-registration with a new capacity."""

        if not truck_id:
            raise ValidationError("truck identity must not be empty")
        if rated_capacity_m3 <= 0:
            raise ValidationError("rated capacity must be positive", truck=truck_id)
        existing = self._trucks.get(truck_id)
        profile = TruckProfile(
            truck_id=truck_id,
            rated_capacity_m3=float(rated_capacity_m3),
            trip=normalize_trip(trip),
        )
        if existing is not None:
            if existing != profile:
                raise DuplicateError(
                    "truck is already registered with another profile",
                    truck_id=truck_id,
                    registered=existing.describe(),
                )
            return existing
        self._trucks[truck_id] = profile
        return profile

    def adopt(self, profile: TruckProfile) -> TruckProfile:
        """Adopt a profile read back from the record stream after a restart."""

        self._trucks.setdefault(profile.truck_id, profile)
        return profile

    def profile_for(self, truck_id: str, *, trip: str | None = None) -> TruckProfile:
        """Return the registered profile, or an implicit default for a truck."""

        profile = self._trucks.get(truck_id)
        wanted = normalize_trip(trip) if trip is not None else None
        if profile is None:
            return TruckProfile(
                truck_id=truck_id,
                rated_capacity_m3=self._default_capacity_m3,
                trip=wanted or self._default_trip,
            )
        if wanted is not None and wanted != profile.trip:
            # A returning truck flips from its first load to a top up.
            return TruckProfile(
                truck_id=truck_id,
                rated_capacity_m3=profile.rated_capacity_m3,
                trip=wanted,
            )
        return profile

    def is_registered(self, truck_id: str) -> bool:
        return truck_id in self._trucks

    def all(self) -> tuple[TruckProfile, ...]:
        return tuple(self._trucks.values())

    def describe(self) -> dict[str, Any]:
        return {
            "default_capacity_m3": self._default_capacity_m3,
            "default_trip": self._default_trip,
            "trucks": [truck.describe() for truck in self._trucks.values()],
        }


def profile_document(document: Mapping[str, Any]) -> TruckProfile:
    """Rebuild a truck profile from a stored document."""

    return TruckProfile(
        truck_id=str(document["truck_id"]),
        rated_capacity_m3=float(document["rated_capacity_m3"]),
        trip=str(document.get("trip", PRIMARY)),
    )
