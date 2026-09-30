"""Per-trip over-quantity policy and remaining-capacity arithmetic."""

from __future__ import annotations

import pytest

from mixplant.chute.allocation import decide_load
from mixplant.chute.capacity import (
    CLOSE,
    MERGE,
    TruckProfile,
    headroom,
    normalize_trip,
)
from mixplant.errors import ValidationError


def test_policy_is_fixed_per_trip_kind() -> None:
    assert TruckProfile("T", 8.0, "primary").action == CLOSE
    assert TruckProfile("T", 8.0, "topup").action == MERGE
    assert normalize_trip(None) == "primary"
    with pytest.raises(ValidationError):
        normalize_trip("overnight")


def test_primary_headroom_is_min_of_truck_and_plan() -> None:
    profile = TruckProfile("T", 8.0, "primary")
    assert headroom(profile, loaded_m3=5.0, batch_remaining_m3=2.0, valve_open=True).allowed_m3 == 2.0
    assert headroom(profile, loaded_m3=5.0, batch_remaining_m3=10.0, valve_open=True).allowed_m3 == 3.0


def test_topup_headroom_ignores_the_plan_but_never_the_truck_cap() -> None:
    profile = TruckProfile("T", 8.0, "topup")
    room = headroom(profile, loaded_m3=5.0, batch_remaining_m3=0.0, valve_open=True)
    assert room.allowed_m3 == 3.0
    assert room.action == MERGE


def test_primary_over_target_is_clamped_blocked_and_valve_closed() -> None:
    decision = decide_load(
        TruckProfile("T", 8.0, "primary"),
        batch_id="B",
        loaded_m3=5.0,
        requested_m3=4.0,
        batch_remaining_m3=2.0,
        valve_open=True,
        merged=False,
    )
    assert decision.accepted_m3 == 2.0
    assert decision.blocked_m3 == 2.0
    assert decision.close_valve is True
    assert decision.action == CLOSE


def test_topup_within_capacity_merges_without_closing_for_the_plan() -> None:
    decision = decide_load(
        TruckProfile("T", 8.0, "topup"),
        batch_id="B",
        loaded_m3=6.0,
        requested_m3=1.5,
        batch_remaining_m3=0.0,
        valve_open=True,
        merged=True,
    )
    assert decision.accepted_m3 == 1.5
    assert decision.blocked_m3 == 0.0
    assert decision.merged is True


def test_topup_over_rated_capacity_is_blocked() -> None:
    decision = decide_load(
        TruckProfile("T", 8.0, "topup"),
        batch_id="B",
        loaded_m3=6.0,
        requested_m3=5.0,
        batch_remaining_m3=10.0,
        valve_open=True,
        merged=True,
    )
    assert decision.accepted_m3 == 2.0
    assert decision.blocked_m3 == 3.0
    assert decision.overloaded is True
    assert decision.close_valve is True


def test_load_requires_an_open_valve_and_positive_volume() -> None:
    with pytest.raises(ValidationError):
        decide_load(
            TruckProfile("T", 8.0),
            batch_id="B",
            loaded_m3=0.0,
            requested_m3=1.0,
            batch_remaining_m3=1.0,
            valve_open=False,
            merged=False,
        )
    with pytest.raises(ValidationError):
        decide_load(
            TruckProfile("T", 8.0),
            batch_id="B",
            loaded_m3=0.0,
            requested_m3=0.0,
            batch_remaining_m3=1.0,
            valve_open=True,
            merged=False,
        )
