"""Read model behind the discharge page."""

from __future__ import annotations

from typing import Any


def truck_view(*, chute_service: Any, pump_service: Any, sampler_service: Any) -> dict[str, Any]:
    """Build the discharge view: the door, the queue, tickets and delivery."""

    return {
        "chute": chute_service.status(),
        "queue": chute_service.queue.describe(),
        "manifest": chute_service.manifest.describe(),
        "board": chute_service.board(),
        "reconcile": chute_service.reconcile(),
        "pump": pump_service.status(),
        "sampler": sampler_service.status(),
        "samples": [dict(sample) for sample in sampler_service.samples()],
    }
