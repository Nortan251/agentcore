"""Deterministic tests at final validation/credential/provider boundaries."""

import copy
import os
import pickle
import signal
import threading
from contextlib import contextmanager

import pytest

from agentcore.action_journal import ActionAttemptStatus, ActionState, ActionReconciliationStatus
from agentcore.credentials import CredentialError, CredentialFailure
from agentcore.credentials.action import CredentialActionExecution, CredentialActionIdentity, action_snapshot
from agentcore.integrations.gmail.send import GmailSendSubmission
from agentcore.permissions.levels import Capability
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import FunctionTool, tool_spec
from agentcore.tools.registry import ToolRegistryLeaseError
from gmail_send_fakes import ARGS, WORKER_WATCHDOG, Harness, WorkerRendezvous, send_policy


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    h.prepare()
    yield h
    h.close()


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


@pytest.mark.parametrize("operation", ["send", "probe"])
@pytest.mark.parametrize("aba", [False, True])
def test_policy_replacement_after_final_evaluation_before_lease_cannot_dispatch(harness, monkeypatch, operation, aba):
    h = harness
    if operation == "probe":
        h.fake.after_effect = "timeout"
        first = h.invoke()
    current = h.executor.policy
    method = "evaluate_authenticated_send" if operation == "send" else "check_authenticated_reconciliation"
    original_check = getattr(current, method)
    original_lease = h.google.vault._lease
    in_lookup = False
    rendezvous = WorkerRendezvous()

    @contextmanager
    def lookup(*args, **kwargs):
        nonlocal in_lookup
        with original_lease(*args, **kwargs) as lease:
            in_lookup = True
            yield lease

    def check(*args, **kwargs):
        decision = original_check(*args, **kwargs)
        if in_lookup:
            rendezvous.enter()
        return decision

    monkeypatch.setattr(h.google.vault, "_lease", lookup)
    monkeypatch.setattr(current, method, check)
    with rendezvous.worker(h.invoke if operation == "send" else lambda: h.reconcile(first.action_id)) as (worker, results):
        rendezvous.wait_for_entry()
        h.executor.policy = send_policy(send=False, probe=False)
        if aba:
            h.executor.policy = current
    assert not worker.is_alive() and len(results) == 1 and not isinstance(results[0], BaseException)
    if operation == "send":
        assert not results[0].ok and not h.fake.sends
        record = h.actions.load(results[0].action_id)
        assert record.state is ActionState.FAILED and record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    else:
        assert results[0].status is ActionReconciliationStatus.PROBE_FAILED and not h.fake.probes
        assert h.actions.load(first.action_id).reconciliation.probe_count == 0
        assert len(h.fake.sends) == 1


@pytest.mark.parametrize("change", ["send_permission", "credential_permission", "approval_expiry", "credential_expiry", "cancellation"])
def test_authority_changes_while_vault_lookup_is_paused_are_rechecked_before_send(harness, monkeypatch, change):
    h = harness
    original_lease = h.google.vault._lease
    rendezvous, cancel = WorkerRendezvous(), threading.Event()

    @contextmanager
    def lookup(*args, **kwargs):
        with original_lease(*args, **kwargs) as lease:
            rendezvous.enter()
            yield lease

    def checkpoint():
        if cancel.is_set():
            raise CancellationRequested("synthetic host cancellation")

    monkeypatch.setattr(h.google.vault, "_lease", lookup)
    with rendezvous.worker(lambda: h.invoke(checkpoint=checkpoint)) as (worker, results):
        rendezvous.wait_for_entry()
        assert not h.fake.sends
        if change == "send_permission":
            h.executor.policy = send_policy(send=False)
        elif change == "credential_permission":
            h.executor.policy = send_policy(blocked=(str(Capability.CREDENTIAL_ACCESS),))
        elif change == "approval_expiry":
            approval = h.approvals.load(h.approvals.list_requests()[0])
            h.clock.advance((approval.expires_at - h.clock.now()).total_seconds() + 1)
            assert h.clock.now() < h.info.expires_at  # This specifically proves approval expiry.
        elif change == "credential_expiry":
            h.clock.advance((h.info.expires_at - h.clock.now()).total_seconds() + 1)
        else:
            cancel.set()
    assert not worker.is_alive() and len(results) == 1 and not h.fake.sends
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    if change == "cancellation":
        assert isinstance(results[0], CancellationRequested) and record.state is ActionState.CANCELLED
    else:
        assert not isinstance(results[0], BaseException) and not results[0].ok
        assert record.state is ActionState.FAILED


@pytest.mark.parametrize("operation", ["send", "probe"])
def test_final_policy_registry_and_credential_leases_block_mutation_but_release_metadata_locks(harness, monkeypatch, operation):
    import agentcore.credentials.action as action_module
    h = harness
    if operation == "probe":
        h.fake.after_effect = "timeout"
        first = h.invoke()
    original_identity = action_module._provider_identity
    rendezvous = WorkerRendezvous()
    host_thread = threading.get_ident()

    def identity(provider):
        result = original_identity(provider)
        if threading.get_ident() != host_thread and h.executor._credential_policy_leases:
            rendezvous.enter()
        return result

    monkeypatch.setattr(action_module, "_provider_identity", identity)
    with rendezvous.worker(h.invoke if operation == "send" else lambda: h.reconcile(first.action_id)) as (worker, results):
        rendezvous.wait_for_entry()
        assert len(h.fake.sends) == (operation == "probe") and not h.fake.probes
        with pytest.raises(CredentialError) as error:
            h.executor.policy = send_policy(send=False, probe=False)
        assert error.value.reason is CredentialFailure.BUSY
        with pytest.raises(ToolRegistryLeaseError):
            h.registry.unregister(h.foundation.tool.name)
        # Unrelated tool registration remains available.
        unrelated = FunctionTool(tool_spec("unrelated_read", "safe local read"), lambda *_: "safe")
        h.registry.register(unrelated)
        h.registry.unregister(unrelated.name)
        for mutate in (
            lambda: h.foundation.bind(h.runtime, h.info.reference, replace_existing=True),
            lambda: h.google.service.refresh(h.info.reference),
            lambda: h.google.service.disconnect(h.info.reference),
        ):
            with pytest.raises(CredentialError) as error:
                mutate()
            assert error.value.reason is CredentialFailure.BUSY
        assert h.runtime.action_info(h.foundation.tool) == h.info
        for lock in (h.runtime._lock, h.executor._policy_lock, h.registry._lock, h.google.vault._lock,
                     h.actions._domain.mutex, h.approvals._domain.mutex):
            assert lock.acquire(blocking=False)
            lock.release()
    assert not worker.is_alive() and len(results) == 1 and not isinstance(results[0], BaseException)
    assert results[0].ok if operation == "send" else results[0].status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    h.executor.policy = send_policy(send=False, probe=False)
    h.registry.unregister(h.foundation.tool.name)
    h.google.service.disconnect(h.info.reference)


def test_permit_cannot_be_forged_reused_moved_to_another_thread_or_serialized(harness, monkeypatch):
    h = harness
    binding = action_snapshot(h.runtime, h.foundation.tool)
    payload, _ = h.foundation._load_preparation(h.preparation.preparation_id)
    with pytest.raises(CredentialError) as error:
        CredentialActionExecution(h.runtime, binding, ARGS, payload,
                                  CredentialActionIdentity("act_111111111111", "aat_111111111111", h.preparation.intent_digest),
                                  lambda: None, authority=object())
    assert error.value.reason is CredentialFailure.AUTHORITY
    original = h.foundation.tool.run
    captured = []

    def run(args, context):
        permit = context.credential_execution
        captured.append(permit)
        for serialize in (pickle.dumps, copy.copy, copy.deepcopy):
            with pytest.raises(TypeError):
                serialize(permit)
        worker, results = worker_call(lambda: permit.invoke(h.foundation.tool, args))
        worker.join(WORKER_WATCHDOG)
        assert not worker.is_alive() and isinstance(results[0], CredentialError)
        assert results[0].reason is CredentialFailure.OWNERSHIP
        with pytest.raises(CredentialError):
            permit.invoke(h.foundation.tool, {**args, "subject": "changed"})
        with pytest.raises(CredentialError):
            permit.invoke_probe(h.foundation.reconciler)
        with pytest.raises(CredentialError):
            permit._dispatch()
        result = original(args, context)
        with pytest.raises(CredentialError):
            original(args, context)
        return result

    monkeypatch.setattr(h.foundation.tool, "run", run)
    assert h.invoke().ok and len(h.fake.sends) == 1
    with pytest.raises(CredentialError):
        captured[0].invoke(h.foundation.tool, ARGS)
    with pytest.raises(CredentialError):
        h.fake.lease.reveal()


def test_durable_attempt_changed_during_credential_lookup_cannot_reach_provider(harness, monkeypatch):
    h = harness
    original = h.google.vault._lease

    @contextmanager
    def lookup(*args, **kwargs):
        with original(*args, **kwargs) as lease:
            claimed = h.actions.load(h.actions.list_actions()[0])
            assert claimed.state is ActionState.IN_FLIGHT
            h.executor._publish_claimed_cancellation(h.actions, claimed)
            yield lease

    monkeypatch.setattr(h.google.vault, "_lease", lookup)
    result = h.invoke()
    assert not result.ok and not h.fake.sends
    assert h.actions.load(result.action_id).state is ActionState.CANCELLED


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_real_fork_rejects_inherited_send_permit_and_provider_before_poisoned_locks(harness, monkeypatch):
    import select
    h = harness
    original = h.foundation.tool.run

    def run(args, context):
        payload, _ = h.foundation._load_preparation(h.preparation.preparation_id)
        submission = GmailSendSubmission(payload, context.credential_execution._identity)
        rendezvous = WorkerRendezvous()
        def hold():
            with h.runtime._lock, h.foundation._lock, h.executor._policy_lock:
                rendezvous.enter()
        with rendezvous.worker(hold):
            rendezvous.wait_for_entry()
            read_fd, write_fd = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(read_fd)
                rejected = 0
                operations = (
                    lambda: action_snapshot(h.runtime, h.foundation.tool),
                    lambda: h.foundation.prepare(args),
                    lambda: h.foundation.release(h.preparation),
                    lambda: context.credential_execution.invoke(h.foundation.tool, args),
                    context.credential_execution._finish,
                    lambda: h.foundation._provider.validate_intent(payload, h.info, reconcile=False),
                    lambda: h.foundation.reconciler.probe(None),
                    lambda: submission.mime_bytes,
                    lambda: h.executor.policy,
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
                assert os.read(read_fd, 16) == b"9"
                assert os.waitpid(child, 0)[1] == 0
                reaped = True
            finally:
                if not reaped:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                os.close(read_fd)
        return original(args, context)

    monkeypatch.setattr(h.foundation.tool, "run", run)
    assert h.invoke().ok and len(h.fake.sends) == 1
