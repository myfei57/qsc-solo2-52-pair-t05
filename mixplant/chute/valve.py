"""The per truck fill valve, distinct from the batch discharge door.

The discharge door (``chute.door``) stays open for the whole batch window so a
queue of trucks, and a returning top up, can be served in turn.  The fill
valve (``chute.valve``) is what stops concrete flowing into *one* truck the
moment that truck reaches its allowed quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ValveState:
    """Whether concrete is currently flowing into one truck."""

    open: bool
    truck_id: str
    ticket_id: str
    batch_id: str

    def describe(self) -> dict[str, Any]:
        return {
            "active": self.open,
            "truck_id": self.truck_id,
            "ticket_id": self.ticket_id,
            "batch_id": self.batch_id,
        }


def valve_state(*, open: bool, truck_id: str = "", ticket_id: str = "", batch_id: str = "") -> ValveState:
    """Build a fill valve state."""

    return ValveState(open=open, truck_id=truck_id, ticket_id=ticket_id, batch_id=batch_id)
