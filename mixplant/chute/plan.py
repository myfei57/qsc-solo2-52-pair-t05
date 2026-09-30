"""Batch loading plan: how much of "这盘" is still allowed onto trucks."""

from __future__ import annotations

from typing import Any

from ..decision.batch import BatchRegistry
from .manifest import LoadManifest
from .policy import ROUND_DIGITS


def batch_plan(*, batch_id: str, planned_m3: float, loaded_planned_m3: float) -> dict[str, Any]:
    """How much plan volume one batch still has."""

    remaining = round(max(0.0, planned_m3 - loaded_planned_m3), ROUND_DIGITS)
    return {
        "batch_id": batch_id,
        "planned_m3": round(planned_m3, ROUND_DIGITS),
        "loaded_planned_m3": round(loaded_planned_m3, ROUND_DIGITS),
        "remaining_m3": remaining,
        "complete": remaining == 0.0,
    }


def open_batch_plan(batches: BatchRegistry, manifest: LoadManifest, batch_id: str) -> dict[str, Any]:
    """Fold the declared batch and the metered sheets into one plan view."""

    batch = batches.require(batch_id)
    return batch_plan(
        batch_id=batch_id,
        planned_m3=batch.quantity,
        loaded_planned_m3=manifest.planned_for_batch(batch_id),
    )


def queue_capacity(
    *,
    waiting: list[str],
    fleet,
    manifest: LoadManifest,
    batches: BatchRegistry,
    batch_id: str | None,
) -> list[dict[str, Any]]:
    """For each queued truck report how much more it can legally take."""

    if batch_id is None:
        return [
            {"truck_id": truck_id, "capacity_m3": fleet.require(truck_id).capacity_m3}
            for truck_id in waiting
        ]
    batch = batches.require(batch_id)
    rows: list[dict[str, Any]] = []
    for truck_id in waiting:
        spec = fleet.require(truck_id)
        already = sum(
            ticket.loaded_m3 for ticket in manifest.for_truck(truck_id) if ticket.batch_id == batch_id
        )
        truck_room = round(max(0.0, spec.capacity_m3 - already), ROUND_DIGITS)
        batch_room = round(
            max(0.0, batch.quantity - manifest.planned_for_batch(batch_id)), ROUND_DIGITS
        )
        rows.append(
            {
                "truck_id": truck_id,
                "trip_type": spec.trip_type,
                "capacity_m3": spec.capacity_m3,
                "loaded_m3": round(already, ROUND_DIGITS),
                "truck_remaining_m3": truck_room,
                "batch_remaining_m3": batch_room,
                "can_load_m3": round(min(truck_room, batch_room), ROUND_DIGITS),
            }
        )
    return rows
