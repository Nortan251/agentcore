"""Deterministic cleanup evidence; no Chromium, waits, or psutil dependency."""

import sys
from types import SimpleNamespace

import pytest

import test_browser_playwright_integration as oracle


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


@pytest.mark.parametrize("retained_alive", [True, False])
@pytest.mark.parametrize("fresh,wait,should_fail", [
    ("matching", 0, False),
    ("absent", None, False),
    ("reused", 258, False),
    ("matching", 258, True),
    ("denied", 0, True),
    ("matching", 0xFFFFFFFF, True),
    ("matching", None, True),
    ("unknown", 0, True),
    ("nan", 0, True),
    ("matching", "query_failure", True),
])
def test_windows_cleanup_evidence(monkeypatch, retained_alive, fresh, wait, should_fail):
    queries = []
    retained = SimpleNamespace(is_running=lambda: retained_alive)
    backend = SimpleNamespace(_observed={(42, 10.0): retained}, _root=None,
                              _worker=None, _closed=True, _cleanup_failed=False)

    def lookup(pid):
        queries.append(("fresh", pid))
        if fresh == "absent":
            raise NoSuchProcess()
        if fresh == "denied":
            raise AccessDenied()
        return SimpleNamespace(create_time=lambda: {
            "matching": 10.0, "reused": 11.0, "unknown": None, "nan": float("nan")}[fresh])

    def native(pid):
        queries.append(("native", pid))
        if wait == "query_failure":
            raise OSError("synthetic native query failed")
        return {"open_process_succeeded": wait is not None, "wait_result": wait}

    monkeypatch.setattr(oracle, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(oracle, "_windows_process_diagnostics", native)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lookup, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied))
    if should_fail:
        with pytest.raises(pytest.fail.Exception) as error:
            oracle._assert_owned_processes_terminated(backend)
        assert "observed_identity" in str(error.value)
        if fresh == "matching" and wait == 258:
            assert "live_matching_identity" in str(error.value)
        else:
            assert "inconclusive" in str(error.value)
    else:
        if retained_alive:
            # The original retained-only oracle falsely rejects these states.
            with pytest.raises(AssertionError):
                assert all(not process.is_running() for process in backend._observed.values())
        oracle._assert_owned_processes_terminated(backend)
    assert queries == [("fresh", 42), ("native", 42)]


@pytest.mark.parametrize("alive", [True, False])
def test_non_windows_preserves_strict_retained_liveness(monkeypatch, alive):
    backend = SimpleNamespace(_observed={(42, 10.0): SimpleNamespace(is_running=lambda: alive)},
        _root=None, _worker=None, _closed=True, _cleanup_failed=False)
    monkeypatch.setattr(oracle, "os", SimpleNamespace(name="posix"))
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied))
    monkeypatch.setattr(oracle, "_windows_process_diagnostics",
                        lambda *a: pytest.fail("non-Windows native query"))
    if alive:
        with pytest.raises(pytest.fail.Exception):
            oracle._assert_owned_processes_terminated(backend)
    else:
        oracle._assert_owned_processes_terminated(backend)
