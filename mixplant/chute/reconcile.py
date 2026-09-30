"""Cross-checking the ticket book, the audit ledger and the dispatch board.

The committed record stream is the single source of truth, but the three
places an operator reads the figures are derived independently:

* the ticket book aggregates ``chute.part`` rows into a per-truck cumulative
  total (this is what the loading ticket prints),
* the audit ledger carries an ``audit.entry`` per counted part, written by the
  audit service rather than by the chute,
* the dispatch board projects the same ``chute.part`` rows for the live view.

Reconciliation folds each source from scratch and compares every ticket.  It
never repairs a difference; it reports one row per mismatch so a shift can be
stopped and investigated instead of quietly shipping inconsistent numbers.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..store.records import Record
from .chute import PART_KIND
from .ticket import LoadingTicket

TICKET_LOAD_AUDIT = "chute.ticket_load"
TICKET_BLOCKED_AUDIT = "chute.load_blocked"
TICKET_OPEN_AUDIT = "chute.ticket_opened"
TICKET_CLOSED_AUDIT = "chute.ticket_closed"

ENTRY_KIND = "audit.entry"
_TOLERANCE_M3 = 1e-6


def _parts_by_ticket(rows: list[Record]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in rows:
        if record.kind != PART_KIND:
            continue
        ticket_id = str(record.payload.get("ticket_id", ""))
        if not ticket_id:
            continue
        totals[ticket_id] = round(totals.get(ticket_id, 0.0) + float(record.payload.get("volume_m3", 0.0)), 4)
    return totals


def _audit_by_ticket(rows: list[Record]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in rows:
        if record.kind != ENTRY_KIND:
            continue
        if str(record.payload.get("name", "")) != TICKET_LOAD_AUDIT:
            continue
        detail = record.payload.get("detail")
        if not isinstance(detail, Mapping):
            continue
        ticket_id = str(detail.get("ticket_id", ""))
        if ticket_id:
            totals[ticket_id] = round(totals.get(ticket_id, 0.0) + float(detail.get("volume_m3", 0.0)), 4)
    return totals


def reconcile(records: list[Record], tickets: tuple[LoadingTicket, ...]) -> dict[str, Any]:
    """Recompute every source and return per-ticket comparison rows."""

    board = _parts_by_ticket(records)
    audit = _audit_by_ticket(records)
    ticket_ids = {ticket.ticket_id for ticket in tickets} | board.keys() | audit.keys()

    rows: list[dict[str, Any]] = []
    mismatches = 0
    for ticket_id in sorted(ticket_ids):
        ticket = next((item for item in tickets if item.ticket_id == ticket_id), None)
        ticket_total = 0.0 if ticket is None else ticket.total_m3
        board_total = board.get(ticket_id, 0.0)
        audit_total = audit.get(ticket_id, 0.0)
        consistent = (
            abs(ticket_total - board_total) <= _TOLERANCE_M3
            and abs(ticket_total - audit_total) <= _TOLERANCE_M3
        )
        if not consistent:
            mismatches += 1
        rows.append(
            {
                "ticket_id": ticket_id,
                "ticket_m3": round(ticket_total, 4),
                "board_m3": round(board_total, 4),
                "audit_m3": round(audit_total, 4),
                "consistent": consistent,
            }
        )
    return {
        "consistent": mismatches == 0,
        "mismatches": mismatches,
        "tickets_checked": len(rows),
        "rows": rows,
    }
