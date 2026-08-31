"""Deterministic authority changes across actual production HTTP boundaries."""

import os
import signal
import threading
from dataclasses import replace

import pytest

from agentcore.action_journal import ActionAttemptStatus, ActionReconciliationStatus, ActionState
from agentcore.approvals import HumanApprovalDecision
from agentcore.credentials import CredentialError, CredentialFailure
from agentcore.integrations.gmail.send import GmailProbeRequest
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import FunctionTool, tool_spec
from agentcore.tools.registry import ToolRegistryLeaseError
from gmail_mutation_http_fakes import PROFILE_PATH, LiveHarness, Reply, boundary, live, no_network
from gmail_send_fakes import DecisionGate, send_policy


def worker_call(operation):
    results = []
    def call():
        try:
            results.append(operation())
        except BaseException as exc:
            results.append(exc)
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    return worker, results


@pytest.mark.parametrize("change", ["approval_expiry", "credential_expiry", "cancellation", "claim_changed"])
def test_profile_io_cannot_make_a_stale_approval_lease_or_attempt_reach_post(live, boundary, change):
    h = live
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()

    def profile(request):
        assert request.path == PROFILE_PATH and request.method == "GET"
        entered.set()
        assert release.wait(5)

    def checkpoint():
        if cancel.is_set():
            raise CancellationRequested("synthetic host cancellation")

    boundary.on_response = profile
    worker, results = worker_call(lambda: h.invoke(checkpoint=checkpoint))
    try:
        assert entered.wait(3) and not boundary.posts
        claimed = h.actions.load(h.actions.list_actions()[0])
        assert claimed.state is ActionState.IN_FLIGHT
        if change == "approval_expiry":
            approval = h.approvals.load(h.approvals.list_requests()[0])
            h.clock.advance((approval.expires_at - h.clock.now()).total_seconds() + 1)
            assert h.clock.now() < h.info.expires_at
        elif change == "credential_expiry":
            h.clock.advance((h.info.expires_at - h.clock.now()).total_seconds() + 1)
        elif change == "cancellation":
            cancel.set()
        else:
            h.executor._publish_claimed_cancellation(h.actions, claimed)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and len(results) == 1
    assert len(boundary.requests) == 1 and not boundary.posts
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    if change == "cancellation":
        assert isinstance(results[0], CancellationRequested) and record.state is ActionState.CANCELLED
    else:
        assert not isinstance(results[0], BaseException) and not results[0].ok
        assert record.state is (ActionState.CANCELLED if change == "claim_changed" else ActionState.FAILED)


@pytest.mark.parametrize("phase", ["profile", "post", "probe"])
def test_current_leases_span_profile_post_and_probe_without_holding_metadata_mutexes(live, boundary, phase):
    h = live
    if phase == "probe":
        boundary.send = Reply(error=TimeoutError())
        first = h.invoke()
        boundary.template_mime = boundary.mime
        boundary.requests.clear()
    entered, release = threading.Event(), threading.Event()

    def at_response(request):
        if phase == "post" and request.method != "POST":
            return
        entered.set()
        assert release.wait(5)

    boundary.on_response = at_response
    worker, results = worker_call(h.invoke if phase != "probe" else lambda: h.reconcile(first.action_id))
    try:
        assert entered.wait(3)
        assert len(boundary.posts) == (phase == "post")
        with pytest.raises(CredentialError) as error:
            h.executor.policy = send_policy(send=False, probe=False)
        assert error.value.reason is CredentialFailure.BUSY
        with pytest.raises(ToolRegistryLeaseError):
            h.registry.unregister(h.foundation.tool.name)
        unrelated = FunctionTool(tool_spec("unrelated_read", "safe local read"), lambda *_: "safe")
        h.registry.register(unrelated)
        h.registry.unregister(unrelated.name)
        for mutation in (
            lambda: h.foundation.bind(h.runtime, h.info.reference, replace_existing=True),
            lambda: h.google.service.refresh(h.info.reference),
            lambda: h.google.service.disconnect(h.info.reference),
        ):
            with pytest.raises(CredentialError) as error:
                mutation()
            assert error.value.reason is CredentialFailure.BUSY
        for lock in (h.runtime._lock, h.executor._policy_lock, h.registry._lock, h.google.vault._lock,
                     h.actions._domain.mutex, h.approvals._domain.mutex):
            assert lock.acquire(blocking=False)
            lock.release()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and len(results) == 1 and not isinstance(results[0], BaseException)
    assert results[0].ok if phase != "probe" else results[0].status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    h.executor.policy = send_policy(send=False, probe=False)
    h.registry.unregister(h.foundation.tool.name)
    h.google.service.disconnect(h.info.reference)


def test_sender_change_while_waiting_for_human_approval_cannot_rewrite_prepared_sender(tmp_path, boundary):
    h = LiveHarness(tmp_path, gate=DecisionGate(HumanApprovalDecision.PENDING))
    try:
        pending = h.invoke()
        exact = h.foundation.review(h.preparation)
        assert pending.approval_request_id and not boundary.requests
        boundary.profile = Reply({"emailAddress": "changed@sample.invalid"})
        h.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
        result = h.invoke(action_id=pending.action_id, approval=pending.approval_request_id)
        record = h.actions.load(result.action_id)
        assert record.state is ActionState.FAILED and record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
        assert h.foundation.review(h.preparation) == exact
        assert len(boundary.requests) == 1 and not boundary.posts
    finally:
        h.close()


def test_cancellation_observed_after_confirmed_post_cannot_erase_durable_success(live, boundary):
    cancel = threading.Event()
    def at_response(request):
        if request.method == "POST":
            cancel.set()
    def checkpoint():
        if cancel.is_set():
            raise CancellationRequested("synthetic logical cancellation")
    boundary.on_response = at_response
    result = live.invoke(checkpoint=checkpoint)
    assert result.ok and cancel.is_set()
    with pytest.raises(CancellationRequested):
        checkpoint()
    assert live.actions.load(result.action_id).state is ActionState.SUCCEEDED
    assert len(boundary.posts) == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_cannot_reuse_production_transport_or_verified_sender_before_inherited_locks(live, boundary, monkeypatch):
    import select
    h = live
    transport = h.foundation._provider._transport
    verify = transport.verify_sender

    def verified(submission, credential):
        proof = verify(submission, credential)
        probe = GmailProbeRequest(submission._payload, replace(submission.identity, claim_epoch=1))
        locked, release = threading.Event(), threading.Event()
        def hold():
            with h.runtime._lock, h.foundation._lock, h.executor._policy_lock, h.google.vault._lock:
                locked.set()
                assert release.wait(10)
        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        assert locked.wait(3)
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            rejected = 0
            operations = (
                lambda: transport.verify_sender(submission, credential),
                lambda: transport.send(submission, credential, verification=proof),
                lambda: transport.probe(probe, credential),
                lambda: proof.consume(transport, submission, credential),
                lambda: submission.mime_bytes,
            )
            for operation in operations:
                try:
                    operation()
                except CredentialError as exc:
                    rejected += exc.reason is CredentialFailure.OWNERSHIP
            os.write(write_fd, str(rejected).encode("ascii"))
            os._exit(0)
        os.close(write_fd)
        reaped = False
        try:
            assert select.select([read_fd], [], [], 5)[0], "child touched an inherited lock"
            assert os.read(read_fd, 16) == b"5"
            assert os.waitpid(child, 0)[1] == 0
            reaped = True
        finally:
            if not reaped:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
            os.close(read_fd)
            release.set()
            worker.join(3)
        return proof

    monkeypatch.setattr(transport, "verify_sender", verified)
    assert h.invoke().ok and len(boundary.posts) == 1
