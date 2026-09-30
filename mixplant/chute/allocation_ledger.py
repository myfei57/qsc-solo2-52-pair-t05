"""How much of each declared batch plan has been handed to trucks.

The batch registry owns the *planned* quantity (this盘计划量).  This ledger
owns how much of it has been committed to tickets, so the chute can answer
"how much of this batch is left".  It is rebuilt from the stored load parts on
a restart, which is why it takes no direct persistence of its own.
"""

from __future__ import annotations

from ..decision.batch import BatchRegistry
from .ticket import LoadPart


class BatchAllocation:
    """Planned and delivered cubic metres per batch."""

    __slots__ = ("_batches",)

    def __init__(self, batches: BatchRegistry) -> None:
        self._batches = batches

    def plan_m3(self, batch_id: str) -> float:
        return self._batches.require(batch_id).quantity

    def remaining(self, batch_id: str, delivered_m3: float) -> float:
        """Return how much of one batch plan is still unallocated."""

        return round(max(0.0, self.plan_m3(batch_id) - delivered_m3), 4)


def delivered_per_batch(parts: list[LoadPart]) -> dict[str, float]:
    """Fold stored parts into ``batch_id -> delivered cubic metres``."""

    totals: dict[str, float] = {}
    for part in parts:
        if not part.batch_id:
            continue
        totals[part.batch_id] = round(totals.get(part.batch_id, 0.0) + part.volume_m3, 4)
    return totals
