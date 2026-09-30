"""The loading valve: it closes the moment a quota is reached."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALVE_KIND = "chute.valve"


@dataclass(frozen=True, slots=True)
class ValveState:
    """Whether the loading valve is metering concrete onto the truck."""

    open: bool
    batch_id: str
    truck_id: str
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "active": self.open,
            "open": self.open,
            "batch_id": self.batch_id,
            "truck_id": self.truck_id,
            "reason": self.reason,
        }


def valve_state(*, open: bool, batch_id: str, truck_id: str = "", reason: str = "") -> ValveState:
    """Build one valve state."""

    return ValveState(open=open, batch_id=batch_id, truck_id=truck_id, reason=reason)
