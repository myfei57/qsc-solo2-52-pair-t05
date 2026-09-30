"""Per-truck serialisation and idempotency-key handling.

Field devices retry a "load" call when they do not see an answer fast enough,
so the same truck's top up can be triggered concurrently.  The journal append
is already serial, but the read/decide/write sequence is not; these locks make
the whole decision for one truck atomic.  Different trucks stay independent
and can still be served by different console threads.

The idempotency key is the device's job: a stable ``request_id`` repeated
verbatim returns the first result instead of counting the volume twice.  When
a caller omits one we derive a *non* idempotent key so it can never collide
with a real retried key.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from ..errors import ValidationError


class TruckLocks:
    """One lock per truck identity, created on first use."""

    __slots__ = ("_locks", "_guard")

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def for_truck(self, truck_id: str) -> threading.Lock:
        lock = self._locks.get(truck_id)
        if lock is not None:
            return lock
        with self._guard:
            lock = self._locks.get(truck_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[truck_id] = lock
            return lock

    @contextmanager
    def serialise(self, truck_id: str) -> Iterator[None]:
        """Run one truck's read/decide/write block with no concurrent twin."""

        lock = self.for_truck(truck_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


def require_request_id(value: object) -> str:
    """Validate a caller supplied idempotency key."""

    if value in (None, ""):
        raise ValidationError("a load needs an idempotency key (request_id)")
    text = str(value)
    if not text.strip():
        raise ValidationError("the idempotency key must not be blank")
    return text


def synthetic_request_id(sequence: int) -> str:
    """Build a key that cannot collide with a device supplied key."""

    return f"auto-{sequence:08d}"
