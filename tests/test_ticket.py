"""The loading ticket accumulates idempotently and never overloads a truck."""

from __future__ import annotations

import concurrent.futures

import pytest

from mixplant.chute.ticket import LoadingTicket
from mixplant.errors import DuplicateError, ValidationError


def test_parts_accumulate() -> None:
    ticket = LoadingTicket(ticket_id="W-1", truck_id="T-1", rated_capacity_m3=8.0, batch_id="B-1")
    ticket.append(request_id="a", batch_id="B-1", trip="primary", volume_m3=6.0, blocked_m3=0.0, tick=1)
    ticket.append(request_id="b", batch_id="B-1", trip="topup", volume_m3=1.5, blocked_m3=0.0, tick=2)
    assert ticket.total_m3 == 7.5
    assert ticket.remaining_m3 == 0.5
    assert ticket.trips == ("primary", "topup")


def test_a_repeated_request_id_counts_once() -> None:
    ticket = LoadingTicket(ticket_id="W-1", truck_id="T-1", rated_capacity_m3=8.0, batch_id="B-1")
    first = ticket.append(request_id="dup", batch_id="B-1", trip="topup",
                          volume_m3=1.0, blocked_m3=0.0, tick=1)
    second = ticket.append(request_id="dup", batch_id="B-1", trip="topup",
                           volume_m3=1.0, blocked_m3=0.0, tick=2)
    assert second is first
    assert ticket.total_m3 == 1.0
    assert len(ticket.parts) == 1


def test_the_ticket_refuses_a_cumulative_overload() -> None:
    ticket = LoadingTicket(ticket_id="W-1", truck_id="T-1", rated_capacity_m3=8.0, batch_id="B-1")
    ticket.append(request_id="a", batch_id="B-1", trip="primary", volume_m3=7.0, blocked_m3=0.0, tick=1)
    with pytest.raises(ValidationError):
        ticket.append(request_id="b", batch_id="B-1", trip="topup",
                      volume_m3=2.0, blocked_m3=0.0, tick=2)
    assert ticket.total_m3 == 7.0


def test_a_closed_ticket_takes_no_more_parts() -> None:
    ticket = LoadingTicket(ticket_id="W-1", truck_id="T-1", rated_capacity_m3=8.0, batch_id="B-1")
    ticket.append(request_id="a", batch_id="B-1", trip="primary", volume_m3=6.0, blocked_m3=0.0, tick=1)
    ticket.mark_closed()
    with pytest.raises(DuplicateError):
        ticket.append(request_id="b", batch_id="B-1", trip="topup",
                      volume_m3=1.0, blocked_m3=0.0, tick=2)


def test_concurrent_retries_never_count_twice() -> None:
    ticket = LoadingTicket(ticket_id="W-1", truck_id="T-1", rated_capacity_m3=100.0, batch_id="B-1")

    def append_once(_: int) -> None:
        ticket.append(request_id="same", batch_id="B-1", trip="topup",
                      volume_m3=1.0, blocked_m3=0.0, tick=1)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(append_once, range(64)))
    assert ticket.total_m3 == 1.0
    assert len(ticket.parts) == 1
