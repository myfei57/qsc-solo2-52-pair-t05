"""Audit service: it keeps the shift ledger and the production roll-up."""

from __future__ import annotations

from typing import Any, Mapping

from ..service_base import PlantService
from ..event.bus import Event
from .audit import ENTRY_KIND, ROLLUP_KIND
from .log import AuditLedger
from .query import by_name, recent
from .record import audit_entry
from .summary import production_summary


class AuditService(PlantService):
    """Records named production events and reports the shift totals."""

    origin = "audit"
    line = "batch"
    subject = "audit"

    def __init__(self, context, machine=None, *, alarms=None) -> None:
        super().__init__(context, machine)
        self.ledger = AuditLedger()
        self._alarms = alarms
        self._samples = 0

    def note(self, name: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Write one named event to the ledger and the stream."""

        entry = audit_entry(name=name, origin=self.origin, tick=self.context.clock.now(), detail=detail or {})
        self.ledger.record(entry)
        self.publish(ENTRY_KIND, entry.describe())
        return {"entry": entry.describe(), "count": self.ledger.count()}

    def note_sample(self, *, batch_id: str, sample_id: str) -> dict[str, Any]:
        """Record that a laboratory sample was filed."""

        self._samples += 1
        return self.note("sample.filed", {"batch_id": batch_id, "sample_id": sample_id})

    def handle_alarm(self, event: Event) -> None:
        """Copy an alarm into the ledger so the shift report can list it."""

        self.ledger.record(
            audit_entry(name=event.name, origin=event.origin, tick=event.tick, detail=dict(event.payload))
        )

    def handle_chute_event(self, event: Event) -> None:
        """Copy metered loads into the ledger for the three way reconciliation.

        The entry carries the full load line so the audit side can total the
        metered volume independently of the chute's live loading sheets.  It
        is published durably, like every ledger entry, so the third
        reconciliation source survives a restart.
        """

        if event.name != "chute.loaded":
            return
        entry = audit_entry(
            name=event.name, origin=event.origin, tick=event.tick, detail=dict(event.payload)
        )
        self.ledger.record(entry)
        self.publish(ENTRY_KIND, entry.describe())

    def replay_entry(self, payload: Mapping[str, Any]) -> None:
        """Adopt one ledger entry read back from the committed stream."""

        self.ledger.record(
            audit_entry(
                name=str(payload["name"]),
                origin=str(payload.get("origin", self.origin)),
                tick=int(payload.get("tick", 0)),
                detail=dict(payload.get("detail") or {}),
            )
        )
        if payload.get("name") == "sample.filed":
            self._samples += 1

    def rollup(self) -> dict[str, Any]:
        """Write and return the current production roll-up."""

        summary = production_summary(
            state=self.state(),
            batches=self.context.batches,
            alarms=0 if self._alarms is None else self._alarms.count(),
            samples=self._samples,
            now=self.context.clock.now(),
        )
        self.publish(ROLLUP_KIND, {"active": True, **summary})
        return summary

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return recent(self.ledger, limit)

    def counts(self) -> dict[str, int]:
        return by_name(self.ledger)

    def status(self) -> dict[str, Any]:
        return {
            "entries": self.ledger.count(),
            "origins": dict(sorted(self.ledger.origins().items())),
            "events": self.counts(),
            "samples": self._samples,
        }
