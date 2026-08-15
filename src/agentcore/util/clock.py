"""Time source abstraction.

Injected rather than called directly so tests can freeze or advance time
without patching module globals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """Default clock backed by the OS."""

    def now(self) -> datetime:
        return utc_now()

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FixedClock:
    """Deterministic clock for tests; advances only when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds
