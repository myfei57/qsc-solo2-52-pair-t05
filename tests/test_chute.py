"""The discharge door, the truck queue, quotas and loading tickets."""

from __future__ import annotations

import threading

import pytest

from mixplant.chute.fleet import truck_spec
from mixplant.chute.load import load_record
from mixplant.chute.manifest import LoadManifest
from mixplant.chute.policy import (
    MAKEUP_TRIP,
    STANDARD_TRIP,
    decide_initial,
    decide_makeup,
)
from mixplant.chute.queue import TruckQueue
from mixplant.errors import DuplicateError, InterlockBlockedError, NotFoundError, ValidationError
from mixplant.runtime import PlantRuntime
from tests.support import advance_to_mixed, run, run_full_cycle


def test_queue_is_first_in_first_out() -> None:
    queue = TruckQueue()
    queue.arrive("T-01")
    queue.arrive("T-02")
    assert queue.head() == "T-01"
    with pytest.raises(ValidationError):
        queue.serve("T-02")
    assert queue.serve("T-01") == "T-01"
    assert queue.order() == ["T-02"]


def test_queue_refuses_a_duplicate_truck() -> None:
    queue = TruckQueue()
    queue.arrive("T-01")
    with pytest.raises(DuplicateError):
        queue.arrive("T-01")


def test_queue_takes_a_returning_truck_back_after_service() -> None:
    queue = TruckQueue()
    queue.arrive("T-01")
    queue.serve("T-01")
    assert queue.arrive("T-01") == 1


def test_empty_queue_refuses_a_head() -> None:
    with pytest.raises(NotFoundError):
        TruckQueue().head()


def test_standard_trip_closes_the_valve_at_the_batch_plan() -> None:
    decision = decide_initial(
        trip_type=STANDARD_TRIP,
        requested_m3=8.0,
        truck_remaining_m3=8.0,
        batch_remaining_m3=6.0,
    )
    assert decision.accepted_m3 == 6.0
    assert decision.blocked_m3 == 2.0
    assert decision.plan_excess_m3 == 0.0
    assert decision.valve_open is False
    assert decision.reason == "clamped_to_batch_plan"


def test_standard_trip_never_merges_volume_beyond_either_quota() -> None:
    by_truck = decide_initial(
        trip_type=STANDARD_TRIP,
        requested_m3=12.0,
        truck_remaining_m3=8.0,
        batch_remaining_m3=12.0,
    )
    assert (by_truck.accepted_m3, by_truck.blocked_m3) == (8.0, 4.0)
    assert by_truck.valve_open is False

    within = decide_initial(
        trip_type=STANDARD_TRIP,
        requested_m3=5.0,
        truck_remaining_m3=8.0,
        batch_remaining_m3=6.0,
    )
    assert (within.accepted_m3, within.blocked_m3, within.valve_open) == (5.0, 0.0, True)


def test_makeup_trip_merges_beyond_the_plan_but_never_past_truck_capacity() -> None:
    initial = decide_initial(
        trip_type=MAKEUP_TRIP,
        requested_m3=7.0,
        truck_remaining_m3=10.0,
        batch_remaining_m3=6.0,
    )
    assert initial.accepted_m3 == 7.0
    assert initial.plan_excess_m3 == 1.0
    assert initial.blocked_m3 == 0.0
    assert initial.valve_open is True

    top_up = decide_makeup(
        requested_m3=9.0,
        truck_remaining_m3=3.0,
        batch_remaining_m3=0.0,
    )
    assert top_up.accepted_m3 == 3.0
    assert top_up.blocked_m3 == 6.0
    assert top_up.plan_excess_m3 == 3.0
    assert top_up.valve_open is False


def test_manifest_rejects_an_unknown_trip_type() -> None:
    with pytest.raises(ValidationError):
        truck_spec(truck_id="T-01", capacity_m3=10.0, trip_type="overnight")


def _line(
    manifest: LoadManifest,
    ticket,
    *,
    request_id: str,
    requested: float,
    accepted: float,
    excess: float = 0.0,
    blocked: float = 0.0,
    kind: str = "makeup",
    tick: int = 1,
):
    return manifest.record(
        ticket,
        load_record(
            line_id=request_id.upper(),
            ticket_id=ticket.ticket_id,
            truck_id=ticket.truck_id,
            batch_id=ticket.batch_id,
            kind=kind,
            trip_type=ticket.trip_type,
            request_id=request_id,
            requested_m3=requested,
            volume_m3=accepted,
            plan_excess_m3=excess,
            blocked_m3=blocked,
            valve_open=blocked == 0.0,
            reason="test",
            tick=tick,
        ),
    )


def test_manifest_accumulates_makeup_and_dedupes_request_ids() -> None:
    manifest = LoadManifest()
    spec = truck_spec(truck_id="T-01", capacity_m3=10.0, trip_type=MAKEUP_TRIP)
    ticket = manifest.open_ticket(truck_id="T-01", batch_id="B-1", spec=spec)
    _line(manifest, ticket, request_id="r-1", requested=6.0, accepted=6.0, kind="initial")
    _line(manifest, ticket, request_id="r-2", requested=5.0, accepted=4.0, excess=4.0)
    assert ticket.loaded_m3 == 10.0
    assert ticket.plan_excess_m3 == 4.0
    assert ticket.remaining_capacity_m3 == 0.0
    with pytest.raises(DuplicateError):
        _line(manifest, ticket, request_id="r-2", requested=5.0, accepted=4.0, excess=4.0)
    assert manifest.is_applied("r-2")
    assert manifest.applied_line("r-2").volume_m3 == 4.0


def test_standard_trip_getting_a_second_sheet_is_refused() -> None:
    manifest = LoadManifest()
    spec = truck_spec(truck_id="T-01", capacity_m3=8.0, trip_type=STANDARD_TRIP)
    manifest.open_ticket(truck_id="T-01", batch_id="B-1", spec=spec)
    with pytest.raises(DuplicateError):
        manifest.open_or_require(truck_id="T-01", batch_id="B-1", spec=spec, kind="initial")


def test_loads_follow_the_queue(runtime: PlantRuntime) -> None:
    run_full_cycle(runtime)
    assert runtime.chute.status()["door_open"] is False
    assert runtime.chute.manifest.describe()["trucks"] == ["T-01"]


def test_queue_reports_waiting_trucks(runtime: PlantRuntime) -> None:
    run(runtime, "chute.arrive", {"truck_id": "T-09"})
    run(runtime, "chute.arrive", {"truck_id": "T-10"})
    assert runtime.chute.status()["queue"]["order"] == ["T-09", "T-10"]


def _door_ready(runtime: PlantRuntime) -> None:
    run(
        runtime,
        "chute.register_truck",
        {"truck_id": "S-1", "capacity_m3": 8, "trip_type": "standard"},
    )
    run(
        runtime,
        "chute.register_truck",
        {"truck_id": "M-1", "capacity_m3": 10, "trip_type": "makeup"},
    )
    advance_to_mixed(runtime)
    run(runtime, "mix.schedule")
    run(runtime, "chute.open")


def test_over_plan_load_is_blocked_on_a_standard_trip(runtime: PlantRuntime) -> None:
    _door_ready(runtime)
    run(runtime, "chute.arrive", {"truck_id": "S-1"})
    result = run(runtime, "chute.load", {"truck_id": "S-1", "volume_m3": 8, "request_id": "r-1"})
    line = result["load"]
    assert line["volume_m3"] == 6.0
    assert line["blocked_m3"] == 2.0
    assert line["valve_open"] is False
    assert result["plan"]["complete"] is True
    assert runtime.alarms.count() == 1


def test_a_retried_request_is_idempotent(runtime: PlantRuntime) -> None:
    _door_ready(runtime)
    run(runtime, "chute.arrive", {"truck_id": "S-1"})
    first = run(runtime, "chute.load", {"truck_id": "S-1", "volume_m3": 8, "request_id": "r-1"})
    second = run(runtime, "chute.load", {"truck_id": "S-1", "volume_m3": 8, "request_id": "r-1"})
    assert second["idempotent"] is True
    assert second["load"]["line_id"] == first["load"]["line_id"]
    assert runtime.chute.manifest.describe()["total_m3"] == 6.0
    # the blocked retry does not raise a second alarm
    assert runtime.alarms.count() == 1


def test_concurrent_makeup_requests_meter_once(runtime: PlantRuntime) -> None:
    _door_ready(runtime)
    run(runtime, "chute.arrive", {"truck_id": "M-1"})
    run(runtime, "chute.makeup", {"truck_id": "M-1", "volume_m3": 4, "request_id": "c-1"})
    run(runtime, "chute.arrive", {"truck_id": "M-1"})

    outcomes: list[dict] = []

    def fire() -> None:
        outcomes.append(
            run(runtime, "chute.makeup", {"truck_id": "M-1", "volume_m3": 3, "request_id": "c-2"})
        )

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ticket = runtime.chute.manifest.for_truck("M-1")[0]
    assert len(outcomes) == 6
    assert all(outcome["load"]["line_id"] == outcomes[0]["load"]["line_id"] for outcome in outcomes)
    assert ticket.loaded_m3 == 7.0
    assert len(ticket.lines) == 2


def test_makeup_load_merges_onto_one_ticket_until_truck_capacity(runtime: PlantRuntime) -> None:
    _door_ready(runtime)
    run(runtime, "chute.arrive", {"truck_id": "M-1"})
    # the first 2 m3 still fit the 6 m3 batch plan
    first = run(runtime, "chute.makeup", {"truck_id": "M-1", "volume_m3": 2, "request_id": "m-1"})
    assert first["load"]["plan_excess_m3"] == 0.0
    run(runtime, "chute.arrive", {"truck_id": "M-1"})
    # the next 8 m3: 4 finish the plan, then 4 are merged beyond it, and the
    # truck legal capacity (10) leaves nothing blocked
    second = run(runtime, "chute.makeup", {"truck_id": "M-1", "volume_m3": 8, "request_id": "m-2"})
    assert second["load"]["volume_m3"] == 8.0
    assert second["load"]["blocked_m3"] == 0.0
    assert second["load"]["plan_excess_m3"] == 4.0
    ticket = second["ticket"]
    assert ticket["loaded_m3"] == 10.0
    assert ticket["plan_excess_m3"] == 4.0
    assert len(ticket["lines"]) == 2
    run(runtime, "chute.arrive", {"truck_id": "M-1"})
    third = run(runtime, "chute.makeup", {"truck_id": "M-1", "volume_m3": 1, "request_id": "m-3"})
    assert third["load"]["volume_m3"] == 0.0
    assert third["load"]["blocked_m3"] == 1.0
    assert third["load"]["valve_open"] is False


def test_a_standard_trip_can_never_take_a_makeup_load(runtime: PlantRuntime) -> None:
    _door_ready(runtime)
    with pytest.raises(InterlockBlockedError):
        run(runtime, "chute.makeup", {"truck_id": "S-1", "volume_m3": 1, "request_id": "m-x"})


def test_three_views_reconcile(runtime: PlantRuntime) -> None:
    _door_ready(runtime)
    run(runtime, "chute.arrive", {"truck_id": "S-1"})
    run(runtime, "chute.load", {"truck_id": "S-1", "volume_m3": 8, "request_id": "r-1"})
    run(runtime, "chute.arrive", {"truck_id": "M-1"})
    run(runtime, "chute.makeup", {"truck_id": "M-1", "volume_m3": 2, "request_id": "m-1"})
    report = run(runtime, "chute.reconcile")["report"]
    assert report["consistent"] is True
    assert report["volumes_match"] is True
    assert report["plan_respected"] is True
    assert report["totals"]["sheets_m3"] == 8.0
    assert report["totals"]["sheets_plan_excess_m3"] == 2.0
    assert report["totals"]["sheets_blocked_m3"] == 2.0


def test_reconciliation_detects_drift(runtime: PlantRuntime) -> None:
    from mixplant.chute.load import LoadRecord

    _door_ready(runtime)
    run(runtime, "chute.arrive", {"truck_id": "S-1"})
    run(runtime, "chute.load", {"truck_id": "S-1", "volume_m3": 6, "request_id": "r-1"})
    # tamper with the live sheet; the durable board and the audit ledger
    # still hold the metered 6.0 m3, so the three views must disagree
    ticket = runtime.chute.manifest.for_truck("S-1")[0]
    original = ticket.lines[0]
    ticket.lines[0] = LoadRecord(
        line_id=original.line_id,
        ticket_id=original.ticket_id,
        truck_id=original.truck_id,
        batch_id=original.batch_id,
        kind=original.kind,
        trip_type=original.trip_type,
        request_id=original.request_id,
        requested_m3=6.0,
        volume_m3=5.0,
        plan_excess_m3=0.0,
        blocked_m3=0.0,
        valve_open=True,
        reason="tampered",
        tick=original.tick,
    )
    report = run(runtime, "chute.reconcile")["report"]
    assert report["consistent"] is False
    assert report["volumes_match"] is False
    assert runtime.alarms.count() >= 1


def test_loads_tickets_and_idempotency_survive_restart(tmp_path) -> None:
    from mixplant.config import StationConfig
    from mixplant.runtime import PlantRuntime

    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    run(
        first,
        "chute.register_truck",
        {"truck_id": "M-9", "capacity_m3": 10, "trip_type": "makeup"},
    )
    advance_to_mixed(first, batch_id="B-001")
    run(first, "mix.schedule")
    run(first, "chute.arrive", {"truck_id": "M-9"})
    run(first, "chute.open")
    run(first, "chute.makeup", {"truck_id": "M-9", "volume_m3": 4, "request_id": "k-1"})
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    spec = second.chute.fleet.require("M-9")
    assert (spec.capacity_m3, spec.trip_type) == (10.0, MAKEUP_TRIP)
    ticket = second.chute.manifest.for_truck("M-9")[0]
    assert ticket.loaded_m3 == 4.0
    assert second.chute.manifest.is_applied("k-1")
    report = second.chute.reconcile_totals()["report"]
    assert report["consistent"] is True
    second.close()
