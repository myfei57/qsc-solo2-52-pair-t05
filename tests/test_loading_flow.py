"""End to end loading control: policy, idempotency, reconciliation, restart."""

from __future__ import annotations

import concurrent.futures

import pytest

from mixplant.config import StationConfig
from mixplant.errors import OverfillBlockedError
from mixplant.runtime import PlantRuntime
from tests.support import advance_to_mixed, run


@pytest.fixture()
def loaded_runtime() -> PlantRuntime:
    runtime = PlantRuntime.in_memory()
    runtime.bootstrap()
    advance_to_mixed(runtime, batch_id="B-001")  # planned 6 m3
    return runtime


def test_primary_load_at_target_closes_the_fill_valve(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "T-01", "rated_capacity_m3": 8, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "T-01"})
    run(rt, "chute.open")
    result = run(rt, "chute.load", {"truck_id": "T-01", "volume_m3": 6, "request_id": "R1"})
    assert result["decision"]["valve_closed"] is True
    assert result["ticket"]["total_m3"] == 6.0
    # The batch door stays open; only the per-truck fill valve shuts.
    assert rt.chute.board()["door_open"] is True
    assert rt.chute.board()["fill_valve_open"] is False


def test_overflow_is_blocked_on_the_spot(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "TA", "rated_capacity_m3": 4, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "TA"})
    run(rt, "chute.open")
    with pytest.raises(OverfillBlockedError) as exc:
        rt.dispatch("chute.load", {"truck_id": "TA", "volume_m3": 6, "request_id": "RX"})
    decision = exc.value.context["result"]["decision"]
    assert decision["accepted_m3"] == 4.0
    assert decision["blocked_m3"] == 2.0
    assert decision["over_quantity_action"] == "close"
    assert rt.chute.tickets.open_ticket("TA").total_m3 == 4.0
    assert rt.alarms.count() >= 1


def test_topup_merges_onto_the_same_ticket(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "TT", "rated_capacity_m3": 8, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "TT"})
    run(rt, "chute.open")
    first = run(rt, "chute.load", {"truck_id": "TT", "volume_m3": 6, "request_id": "K1"})
    top = run(rt, "chute.topup", {"truck_id": "TT", "volume_m3": 1.5, "request_id": "K2"})
    assert top["ticket"]["ticket_id"] == first["ticket"]["ticket_id"]
    assert top["ticket"]["total_m3"] == 7.5
    assert top["decision"]["merged"] is True


def test_concurrent_topup_retries_are_idempotent(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "TT", "rated_capacity_m3": 8, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "TT"})
    run(rt, "chute.open")
    first = run(rt, "chute.load", {"truck_id": "TT", "volume_m3": 6, "request_id": "K1"})
    ticket_id = first["ticket"]["ticket_id"]

    def retry(_: int):
        return rt.dispatch("chute.topup", {"truck_id": "TT", "volume_m3": 0.5,
                                           "request_id": "K3-DUP"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(retry, range(8)))

    ticket = rt.chute.tickets.require(ticket_id)
    assert ticket.total_m3 <= 8.0 + 1e-6
    assert sum(1 for part in ticket.parts if part.request_id == "K3-DUP") == 1
    assert any(outcome["result"].get("idempotent_replay") for outcome in outcomes)


def test_topup_over_rated_capacity_is_blocked_never_overloaded(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "TC", "rated_capacity_m3": 8, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "TC"})
    run(rt, "chute.open")
    run(rt, "chute.load", {"truck_id": "TC", "volume_m3": 6, "request_id": "P1"})
    with pytest.raises(OverfillBlockedError) as exc:
        rt.dispatch("chute.topup", {"truck_id": "TC", "volume_m3": 5, "request_id": "P2"})
    decision = exc.value.context["result"]["decision"]
    assert decision["accepted_m3"] == 2.0
    assert decision["blocked_m3"] == 3.0
    assert exc.value.context["result"]["ticket"]["total_m3"] == 8.0


def test_ticket_board_and_audit_reconcile(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "TT", "rated_capacity_m3": 8, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "TT"})
    run(rt, "chute.open")
    run(rt, "chute.load", {"truck_id": "TT", "volume_m3": 6, "request_id": "K1"})
    run(rt, "chute.topup", {"truck_id": "TT", "volume_m3": 1.5, "request_id": "K2"})
    report = rt.chute.reconcile()
    assert report["consistent"] is True
    assert all(row["ticket_m3"] == row["board_m3"] == row["audit_m3"] for row in report["rows"])


def test_restart_rebuilds_tickets_and_audit(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    advance_to_mixed(first, batch_id="B-001")
    run(first, "chute.register", {"truck_id": "TR", "rated_capacity_m3": 8, "trip": "primary"})
    run(first, "chute.arrive", {"truck_id": "TR"})
    run(first, "chute.open")
    run(first, "chute.load", {"truck_id": "TR", "volume_m3": 6, "request_id": "Z1"})
    audit_count = first.audit.ledger.count()
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    ticket = second.chute.tickets.open_ticket("TR")
    assert ticket is not None and ticket.total_m3 == 6.0
    assert ticket.find("Z1") is not None
    assert second.chute.reconcile()["consistent"] is True
    assert second.audit.ledger.count() == audit_count
    second.close()


def test_a_full_exhausted_batch_blocks_the_next_truck_entirely(loaded_runtime) -> None:
    rt = loaded_runtime
    run(rt, "chute.register", {"truck_id": "C1", "rated_capacity_m3": 6, "trip": "primary"})
    run(rt, "chute.register", {"truck_id": "C2", "rated_capacity_m3": 8, "trip": "primary"})
    run(rt, "chute.arrive", {"truck_id": "C1"})
    run(rt, "chute.arrive", {"truck_id": "C2"})
    run(rt, "chute.open")
    run(rt, "chute.load", {"truck_id": "C1", "volume_m3": 6, "request_id": "Q1"})
    with pytest.raises(OverfillBlockedError) as exc:
        rt.dispatch("chute.load", {"truck_id": "C2", "volume_m3": 8, "request_id": "Q2"})
    decision = exc.value.context["result"]["decision"]
    assert decision["accepted_m3"] == 0.0
    assert decision["blocked_m3"] == 8.0
    # C2 never received a counted part or a manifest entry.
    assert rt.chute.tickets.open_ticket("C2").total_m3 == 0.0
    assert "C2" not in rt.chute.manifest.describe()["trucks"]
