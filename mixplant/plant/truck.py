"""Read model behind the discharge page."""

from __future__ import annotations

from typing import Any

from ..chute.reconcile import board_from_records
from ..chute.chute import LOAD_KIND


def truck_view(*, chute_service: Any, pump_service: Any, sampler_service: Any) -> dict[str, Any]:
    """Build the dispatch board: queue capacity, tickets, valve and totals."""

    return {
        "chute": chute_service.status(),
        "queue": chute_service.status()["queue"],
        "fleet": chute_service.fleet.describe(),
        "manifest": chute_service.manifest.describe(),
        "board": board_from_records(chute_service.store.history(LOAD_KIND)),
        "pump": pump_service.status(),
        "sampler": sampler_service.status(),
        "samples": [dict(sample) for sample in sampler_service.samples()],
    }
