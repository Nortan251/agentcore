"""File publication support is host policy; refusal precedes physical entry."""

import os
import sys

import pytest

from agentcore.action_journal import (
    ActionAttemptStatus, ActionInvocationContext, ActionReconciliationStatus,
    EffectVerdict, ReconciliationDisposition,
)
from agentcore.fs import mutation
from agentcore.fs.paths import PathRejectedError, RejectionReason
from test_fs_write import make  # noqa: F401


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14", "unknown"])
def test_unsupported_publication_refuses_before_traversal_or_callback(make, monkeypatch, platform):
    h = make()
    intent = h.guard.prepare_create("sub/new.txt")
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported publication reached traversal or physical-entry callback")
    monkeypatch.setattr(mutation, "parent", forbidden)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(PathRejectedError) as exc:
        h.guard.create_file(intent, b"intended", forbidden)
    assert exc.value.reason is RejectionReason.UNSUPPORTED_PUBLICATION
    assert not (h.root / "sub/new.txt").exists() and not h.residue()


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o777, 0o1777])
def test_unsupported_attempt_is_durable_not_invoked_and_cannot_resume(make, monkeypatch, mode):
    h = make()
    if os.name == "posix":
        os.chmod(h.root / "sub", mode)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        result = h.invoke()
        assert not result.ok
        record = h.actions.load(result.action_id)
        assert record.intent_ref and len(record.attempts) == 1
        assert record.attempts[0].status is ActionAttemptStatus.NOT_INVOKED
        assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
        assert not h.invoke(action_context=ActionInvocationContext(action_id=result.action_id)).ok
    assert len(h.actions.load(result.action_id).attempts) == 1
    assert not (h.root / "sub/new.txt").exists() and not h.residue()


@pytest.mark.parametrize("present", [False, True])
def test_legacy_uncertain_posix_attempt_never_promoted_or_retried(make, monkeypatch, present):
    h = make()
    target = h.root / "sub/new.txt"
    calls = []
    def historical_attempt(intent, data, validate):
        # Seed old durable uncertainty, not evidence of native publication safety.
        validate()
        calls.append(1)
        if present:
            target.write_bytes(data)
        raise TimeoutError("historical uncertain publication")
    monkeypatch.setattr(h.guard, "create_file", historical_attempt)
    result = h.invoke()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        report = h.reconcile(result.action_id)
        assert report.status is ActionReconciliationStatus.UNRESOLVED
        record = h.actions.load(result.action_id)
        assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
        assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
        assert not h.invoke(action_context=ActionInvocationContext(action_id=result.action_id)).ok
    assert calls == [1] and len(record.attempts) == 1
    assert target.exists() is present and not h.residue()
