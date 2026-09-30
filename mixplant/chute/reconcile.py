"""Three way reconciliation: loading sheets, dispatch board and audit ledger.

The dispatch board is rebuilt independently from the committed ``chute.load``
records rather than read from the live sheet book, so the comparison can
catch a sheet that drifted from the journal.  The audit side sums the
``chute.loaded`` ledger entries the auditor wrote while the loads happened.
Both totals have to match the sheets, and the plan columns have to respect
the declared batch quantities with excess possible only on makeup trips.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..decision.batch import BatchRegistry
from ..store.records import Record
from .manifest import LoadManifest, LoadTicket
from .policy import MAKEUP_TRIP, ROUND_DIGITS, STANDARD_TRIP

LOAD_KIND = "chute.load"
AUDIT_LOADED_EVENT = "chute.loaded"


def board_from_records(records: Iterable[Record]) -> dict[str, Any]:
    """Fold the committed load records into the dispatch board totals."""

    lines = tuple(record for record in records if record.kind == LOAD_KIND and not record.is_tombstone)
    by_batch: dict[str, dict[str, float]] = {}
    total = 0.0
    plan_excess = 0.0
    blocked = 0.0
    for record in lines:
        payload = record.payload
        volume = float(payload.get("volume_m3", 0.0))
        excess = float(payload.get("plan_excess_m3", 0.0))
        refused = float(payload.get("blocked_m3", 0.0))
        total = round(total + volume, ROUND_DIGITS)
        plan_excess = round(plan_excess + excess, ROUND_DIGITS)
        blocked = round(blocked + refused, ROUND_DIGITS)
        row = by_batch.setdefault(
            str(payload.get("batch_id", "")),
            {"loaded_m3": 0.0, "plan_excess_m3": 0.0, "blocked_m3": 0.0},
        )
        row["loaded_m3"] = round(row["loaded_m3"] + volume, ROUND_DIGITS)
        row["plan_excess_m3"] = round(row["plan_excess_m3"] + excess, ROUND_DIGITS)
        row["blocked_m3"] = round(row["blocked_m3"] + refused, ROUND_DIGITS)
    return {
        "load_lines": len(lines),
        "total_m3": total,
        "plan_excess_m3": plan_excess,
        "blocked_m3": blocked,
        "batches": dict(sorted(by_batch.items())),
    }


def audit_totals(entries: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """Sum the chute.loaded entries the auditor copied into the ledger."""

    total = 0.0
    plan_excess = 0.0
    blocked = 0.0
    count = 0
    for entry in entries:
        detail = entry.get("detail") if isinstance(entry, Mapping) else None
        if not isinstance(detail, Mapping) or entry.get("name") != AUDIT_LOADED_EVENT:
            continue
        total = round(total + float(detail.get("volume_m3", 0.0)), ROUND_DIGITS)
        plan_excess = round(plan_excess + float(detail.get("plan_excess_m3", 0.0)), ROUND_DIGITS)
        blocked = round(blocked + float(detail.get("blocked_m3", 0.0)), ROUND_DIGITS)
        count += 1
    return {
        "load_events": float(count),
        "total_m3": total,
        "plan_excess_m3": plan_excess,
        "blocked_m3": blocked,
    }


def _plan_check(tickets: Iterable[LoadTicket], batches: BatchRegistry) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_batch: dict[str, list[LoadTicket]] = {}
    for ticket in tickets:
        by_batch.setdefault(ticket.batch_id, []).append(ticket)
    for batch_id, sheets in sorted(by_batch.items()):
        batch = batches.find(batch_id)
        planned = round(
            batch.quantity if batch is not None else 0.0,
            ROUND_DIGITS,
        )
        loaded_planned = round(sum(sheet.planned_m3 for sheet in sheets), ROUND_DIGITS)
        excess = round(sum(sheet.plan_excess_m3 for sheet in sheets), ROUND_DIGITS)
        offenders = sorted(
            sheet.truck_id for sheet in sheets if sheet.plan_excess_m3 > 0.0 and sheet.trip_type != MAKEUP_TRIP
        )
        rows.append(
            {
                "batch_id": batch_id,
                "planned_m3": planned,
                "loaded_planned_m3": loaded_planned,
                "plan_excess_m3": excess,
                "plan_respected": loaded_planned <= planned + 1e-9 and not offenders,
                "excess_on_standard_trips": offenders,
            }
        )
    return rows


def reconcile(
    *,
    manifest: LoadManifest,
    board: Mapping[str, Any],
    audit: Mapping[str, Any],
    batches: BatchRegistry,
) -> dict[str, Any]:
    """Compare the three places a loaded volume lives and report drift."""

    sheets_total = round(manifest.describe()["total_m3"], ROUND_DIGITS)
    board_total = round(float(board.get("total_m3", 0.0)), ROUND_DIGITS)
    audit_total = round(float(audit.get("total_m3", 0.0)), ROUND_DIGITS)
    sheets_excess = round(manifest.describe()["plan_excess_m3"], ROUND_DIGITS)
    sheets_blocked = round(manifest.describe()["blocked_m3"], ROUND_DIGITS)

    totals = {
        "sheets_m3": sheets_total,
        "board_m3": board_total,
        "audit_m3": audit_total,
        "sheets_plan_excess_m3": sheets_excess,
        "board_plan_excess_m3": round(float(board.get("plan_excess_m3", 0.0)), ROUND_DIGITS),
        "audit_plan_excess_m3": round(float(audit.get("plan_excess_m3", 0.0)), ROUND_DIGITS),
        "sheets_blocked_m3": sheets_blocked,
        "board_blocked_m3": round(float(board.get("blocked_m3", 0.0)), ROUND_DIGITS),
        "audit_blocked_m3": round(float(audit.get("blocked_m3", 0.0)), ROUND_DIGITS),
    }
    volumes_match = sheets_total == board_total == audit_total
    excess_match = (
        totals["sheets_plan_excess_m3"] == totals["board_plan_excess_m3"] == totals["audit_plan_excess_m3"]
        and sheets_blocked == totals["board_blocked_m3"] == totals["audit_blocked_m3"]
    )
    plan_rows = _plan_check(manifest.tickets(), batches)
    plan_ok = all(row["plan_respected"] for row in plan_rows)

    consistent = bool(volumes_match and excess_match and plan_ok)
    reasons: list[str] = []
    if not volumes_match:
        reasons.append("loaded volume differs between sheets, board and audit")
    if not excess_match:
        reasons.append("plan excess or blocked volume differs between the three views")
    if not plan_ok:
        reasons.append("a batch was loaded past its plan by a standard trip")
    return {
        "consistent": consistent,
        "volumes_match": volumes_match,
        "excess_match": excess_match,
        "plan_respected": plan_ok,
        "totals": totals,
        "trips": {STANDARD_TRIP: "close_valve_on_quota", MAKEUP_TRIP: "merge_makeup_onto_sheet"},
        "batches": plan_rows,
        "reasons": reasons,
    }
