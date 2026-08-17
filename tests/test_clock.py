"""Time source guarantees.

``utc_now()`` must be strictly monotonic within a process. The Windows wall
clock on Python < 3.13 is coarse and float-jittered, so ``datetime.now()``
can return equal or earlier timestamps for calls made in quick succession;
recorded event times (run history, recovery, replan) rely on monotonicity for
correct chronological ordering (Task 40). This test pins the guard.
"""

from __future__ import annotations

import agentcore.util.clock as clock_mod


class _FrozenDatetime:
    """Stands in for ``datetime`` with a frozen ``now()``."""

    def __init__(self, fixed):
        self._fixed = fixed

    def now(self, tz=None):
        return self._fixed


def test_utc_now_is_strictly_monotonic_when_wall_clock_stalls(monkeypatch):
    frozen = clock_mod.datetime(2026, 1, 1, 12, 0, 0,
                                tzinfo=clock_mod.timezone.utc)
    monkeypatch.setattr(clock_mod, "datetime", _FrozenDatetime(frozen))
    monkeypatch.setattr(clock_mod, "_last_now", None)

    first = clock_mod.utc_now()
    second = clock_mod.utc_now()
    third = clock_mod.utc_now()

    assert first == frozen
    assert second == first + clock_mod.timedelta(microseconds=1)
    assert third == second + clock_mod.timedelta(microseconds=1)


def test_utc_now_advances_with_the_wall_clock(monkeypatch):
    """When the wall clock does advance, the returned time follows it."""
    steps = [
        clock_mod.datetime(2026, 1, 1, 12, 0, 0,
                           tzinfo=clock_mod.timezone.utc),
        clock_mod.datetime(2026, 1, 1, 12, 0, 1,
                           tzinfo=clock_mod.timezone.utc),
    ]
    state = {"i": 0}

    class _SteppingDatetime:
        def now(self, tz=None):
            v = steps[state["i"] % len(steps)]
            state["i"] += 1
            return v

    monkeypatch.setattr(clock_mod, "datetime", _SteppingDatetime())
    monkeypatch.setattr(clock_mod, "_last_now", None)

    a = clock_mod.utc_now()
    b = clock_mod.utc_now()
    assert b > a
    assert (b - a).total_seconds() >= 1
