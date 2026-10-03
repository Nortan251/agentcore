"""Deterministic worker outcome, watchdog, and cleanup characterization."""

import threading

import pytest

from agentcore.approvals import HumanApprovalDecision
from gmail_mutation_http_fakes import PROFILE_PATH, boundary, live, no_network
from gmail_send_fakes import WORKER_WATCHDOG, WorkerRendezvous


def test_boundary_entry_wakes_waiter_and_cleanup_releases_worker():
    rendezvous = WorkerRendezvous()
    with rendezvous.worker(rendezvous.enter) as (worker, results):
        rendezvous.wait_for_entry()
        assert worker.is_alive() and not results and not rendezvous.release.is_set()
    assert rendezvous.release.is_set() and not worker.is_alive() and results == [None]


@pytest.mark.parametrize("error", [RuntimeError("synthetic worker failure"), SystemExit("synthetic exit")])
def test_early_worker_exception_is_reraised_without_waiting_for_watchdog(error):
    rendezvous = WorkerRendezvous()
    def fail():
        raise error
    with pytest.raises(type(error)) as caught:
        with rendezvous.worker(fail) as (worker, results):
            worker.join(WORKER_WATCHDOG)
            assert not worker.is_alive()
            rendezvous.wait_for_entry(timeout=0)
    assert caught.value is error and results == [error]
    assert rendezvous.release.is_set() and not worker.is_alive()


def test_successful_exit_without_required_entry_reports_actual_result():
    rendezvous = WorkerRendezvous()
    with pytest.raises(AssertionError, match="exited before required boundary entry: 'synthetic result'"):
        with rendezvous.worker(lambda: "synthetic result") as (worker, results):
            worker.join(WORKER_WATCHDOG)
            assert not worker.is_alive()
            rendezvous.wait_for_entry(timeout=0)
    assert rendezvous.release.is_set() and not worker.is_alive() and results == ["synthetic result"]


def test_no_entry_or_exit_hits_bounded_watchdog_and_still_cleans_up():
    rendezvous = WorkerRendezvous()
    with pytest.raises(AssertionError, match="watchdog expired after 0.01s: no entry or worker exit"):
        with rendezvous.worker(lambda: rendezvous.release.wait(WORKER_WATCHDOG)) as (worker, results):
            rendezvous.wait_for_entry(timeout=0.01)
    assert rendezvous.release.is_set() and not worker.is_alive() and results == [True]


def test_host_assertion_failure_at_boundary_still_releases_and_joins_worker():
    rendezvous = WorkerRendezvous()
    with pytest.raises(AssertionError, match="synthetic host failure"):
        with rendezvous.worker(rendezvous.enter) as (worker, results):
            rendezvous.wait_for_entry()
            raise AssertionError("synthetic host failure")
    assert rendezvous.release.is_set() and not worker.is_alive() and results == [None]


def test_callback_watchdog_cannot_be_masked_by_a_scrubbed_worker_result(monkeypatch):
    rendezvous = WorkerRendezvous()
    monkeypatch.setattr(rendezvous.release, "wait", lambda timeout: False)
    def scrubbed():
        try:
            rendezvous.enter()
        except AssertionError:
            return "synthetic scrubbed failure"
    with pytest.raises(AssertionError, match="worker boundary release watchdog expired"):
        with rendezvous.worker(scrubbed) as (worker, results):
            rendezvous.wait_for_entry()
    assert not worker.is_alive() and rendezvous.release.is_set()
    assert results == ["synthetic scrubbed failure"]


def test_worker_exit_notifies_an_already_waiting_host(monkeypatch):
    rendezvous = WorkerRendezvous()
    waiting = threading.Event()
    original = rendezvous._condition.wait_for
    def wait_for(predicate, timeout):
        # The host owns the condition until original starts waiting. The worker
        # cannot publish completion before then, so this exercises notification.
        waiting.set()
        return original(predicate, timeout)
    monkeypatch.setattr(rendezvous._condition, "wait_for", wait_for)
    error = RuntimeError("synthetic asynchronous failure")
    def fail():
        assert waiting.wait(WORKER_WATCHDOG)
        raise error
    with pytest.raises(RuntimeError) as caught:
        with rendezvous.worker(fail) as (worker, results):
            rendezvous.wait_for_entry()
    assert caught.value is error and results == [error] and not worker.is_alive()


def test_live_harness_exception_before_profile_is_not_masked(live, boundary, monkeypatch):
    rendezvous = WorkerRendezvous()
    error = RuntimeError("synthetic preparation failure")
    def fail(*args):
        raise error
    monkeypatch.setattr(live, "prepare", fail)
    boundary.on_response = lambda request: rendezvous.enter()
    with pytest.raises(RuntimeError) as caught:
        with rendezvous.worker(live.invoke) as (worker, results):
            rendezvous.wait_for_entry()
    assert caught.value is error and results == [error] and not boundary.requests
    assert not worker.is_alive() and rendezvous.release.is_set()


def test_live_pending_approval_exits_before_profile_with_diagnostic_result(live, boundary):
    rendezvous = WorkerRendezvous()
    live.gate.decision = HumanApprovalDecision.PENDING
    boundary.on_response = lambda request: rendezvous.enter()
    with pytest.raises(AssertionError, match="worker exited before required boundary entry:"):
        with rendezvous.worker(live.invoke) as (worker, results):
            rendezvous.wait_for_entry()
    assert len(results) == 1 and results[0].approval_request_id and not results[0].ok
    assert not boundary.requests and not worker.is_alive() and rendezvous.release.is_set()


def test_live_tls_setup_delay_is_distinct_from_missing_profile_entry(live, boundary, monkeypatch):
    import agentcore.integrations.gmail.mutation_http as http_module
    tls, profile = WorkerRendezvous(), WorkerRendezvous()
    original = http_module._oauth_tls_context
    def paused_tls():
        tls.enter()
        return original()
    def at_profile(request):
        if request.path == PROFILE_PATH:
            profile.enter()
    monkeypatch.setattr(http_module, "_oauth_tls_context", paused_tls)
    boundary.on_response = at_profile
    with profile.worker(live.invoke) as (worker, results):
        try:
            tls.wait_for_entry()
            # A fixed window can expire while a valid worker is still in local
            # setup. No sleeps or scheduler guesses are needed to prove it.
            with pytest.raises(AssertionError, match="no entry or worker exit"):
                profile.wait_for_entry(timeout=0)
            assert worker.is_alive() and not results and not boundary.requests
        finally:
            tls.release.set()
        profile.wait_for_entry()
        assert len(boundary.requests) == 1 and not boundary.posts
    assert not worker.is_alive() and results[0].ok and len(boundary.posts) == 1
