"""Time source abstraction.

Injected rather than called directly so tests can freeze or advance time
without patching module globals.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Protocol

#: Strictly-increasing guard: the previous value returned by :func:`utc_now`.
#: On Windows before Python 3.13 the wall clock is coarse (``time.time()`` is
#: quantised to 15.6 ms and jitters through the float conversion), so two
#: calls made in quick succession can return *equal* — or even slightly
#: *earlier* — timestamps. Recorded event times (run history, recovery,
#: replan, state ``updated_at``) are ordered by these timestamps, so a
#: non-monotonic clock can render later events before earlier ones
#: (Task 40). Guarding here keeps every recorded time a true sequence.
_last_now: datetime | None = None
_last_now_lock = threading.Lock()


def utc_now() -> datetime:
    """Current UTC time, guaranteed strictly monotonic within this process.

    Returns ``datetime.now(timezone.utc)``, bumped by one microsecond when the
    wall clock has not advanced since the previous call. The wall clock may
    legitimately jump *backward* (NTP, VM migration); recorded times never
    will, which is the property ordering code depends on.
    """
    global _last_now
    now = datetime.now(timezone.utc)
    with _last_now_lock:
        if _last_now is not None and now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
    return now


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
