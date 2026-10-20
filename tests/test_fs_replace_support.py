"""Unsupported replacement cannot stage, issue revisions or settle legacy effects."""

import hashlib
import sys

import pytest

from agentcore.action_journal import (
    ActionAttemptStatus, ActionInvocationContext, ActionReconciliationStatus, ActionState,
    EffectVerdict, ReconciliationDisposition,
)
from agentcore.fs import replacement
from agentcore.fs.paths import PathRejectedError, RejectionReason
from test_fs_replace import make, native_security  # noqa: F401


def prior_revision(h, monkeypatch):
    # Synthetic trusted old snapshot exercises admitted/legacy effects without
    # granting unsupported production update reads or requiring Windows rights.
    old = {"path": "sub/old.txt", "directories": [], "identity": [1, 2],
           "workspace": hashlib.sha256(h.guard.identity.encode()).hexdigest(),
           "size": h.target.stat().st_size, "digest": hashlib.sha256(h.target.read_bytes()).hexdigest(),
           "metadata": {}, "change": [1, 2], "revision_schema": 1,
           "read_tool": "read_file_for_update", "replace_tool": "replace_file", "tool_version": "1.0.0"}
    revision = h.revisions.candidate()
    h.revisions.issue(revision, old)
    monkeypatch.setattr(h.guard, "check_revision_snapshot", lambda snapshot: None)
    return revision


@pytest.mark.parametrize("same_identity", [False, True])
@pytest.mark.parametrize("identical_content", [False, True])
def test_reconciliation_excludes_original_identity(make, monkeypatch, same_identity, identical_content):
    # Synthetic snapshot facts test the predicate/durable contract on every OS;
    # native Windows marker/rename interruptions are tested separately.
    h = make()
    revision = prior_revision(h, monkeypatch)
    old = h.revisions._snapshots[revision]
    content = h.target.read_text(encoding="utf-8") if identical_content else "NEW"
    calls = []
    def interrupted(intent, data, validate, publication):
        publication()
        calls.append(1)
        h.target.write_bytes(data)
        raise TimeoutError()
    monkeypatch.setattr(h.guard, "replace_file", interrupted)
    result = h.invoke(revision, content=content)
    observed = dict(old, identity=old["identity"] if same_identity else [1, 3],
                    size=len(content.encode()), digest=hashlib.sha256(content.encode()).hexdigest())
    monkeypatch.setattr(replacement, "snapshot", lambda *args: (content, observed))
    report = h.reconcile(result.action_id)
    record = h.actions.load(result.action_id)
    assert report.status is (ActionReconciliationStatus.UNRESOLVED if same_identity
                             else ActionReconciliationStatus.SETTLED_SUCCEEDED)
    if same_identity:
        assert record.state is ActionState.RECONCILE_REQUIRED and record.terminal_outcome is None
        assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
    else:
        assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert record.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert not h.invoke(revision, content=content,
                        action_context=ActionInvocationContext(action_id=result.action_id)).ok
    assert calls == [1] and len(h.actions.load(result.action_id).attempts) == 1


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14", "unknown"])
@pytest.mark.parametrize("operation", ["snapshot", "replace", "observe"])
def test_unsupported_replacement_refuses_before_traversal_and_callbacks(make, monkeypatch, platform, operation):
    h = make()
    before = h.target.read_bytes(), h.target.stat().st_ino
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported replacement reached traversal, staging or publication")
    monkeypatch.setattr(replacement, "parent", forbidden)
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(PathRejectedError) as exc:
        if operation == "snapshot":
            h.guard.snapshot_for_update("sub/old.txt", 1000)
        elif operation == "replace":
            h.guard.replace_file({"path": "sub/old.txt", "old": {}}, b"new", forbidden, forbidden)
        else:
            h.guard.observe_replace({"path": "sub/old.txt"})
    assert exc.value.reason is RejectionReason.UNSUPPORTED_PUBLICATION
    assert (h.target.read_bytes(), h.target.stat().st_ino) == before
    assert not h.residue() and not h.actions.list_actions()


def test_unsupported_update_read_has_no_revision_and_catalogue_is_accurate(make, monkeypatch):
    h = make()
    monkeypatch.setattr(sys, "platform", "linux")
    result = h.read()
    assert not result.ok and result.result.metadata["kind"] == "filesystem_publication_unsupported"
    for tool in (h.foundation.tool, h.foundation.read_tool):
        assert "native Windows" in tool.spec.description and "Linux and macOS" in tool.spec.description
        assert h.registry.get(tool.name) is tool
    assert not h.revisions._snapshots and not h.actions.list_actions() and not h.residue()


def test_admitted_unsupported_replace_is_not_invoked_and_cannot_retry(make, monkeypatch):
    h = make()
    revision = prior_revision(h, monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    result = h.invoke(revision)
    assert not result.ok and result.result.metadata["kind"] == "filesystem_publication_unsupported"
    record = h.actions.load(result.action_id)
    assert record.intent_ref and len(record.attempts) == 1
    assert record.attempts[0].status is ActionAttemptStatus.NOT_INVOKED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
    assert record.terminal_outcome.result_class == "filesystem.unsupported_publication"
    assert not h.invoke(revision, action_context=ActionInvocationContext(action_id=result.action_id)).ok
    assert len(h.actions.load(result.action_id).attempts) == 1
    assert h.target.read_bytes() == "héllo\n".encode() and not h.residue()


@pytest.mark.parametrize("entered", [False, True])
def test_replacement_support_error_preserves_entry_truth(make, monkeypatch, entered):
    h = make()
    revision = prior_revision(h, monkeypatch)
    def refused(intent, data, validate, publication):
        if entered:
            publication()
        raise PathRejectedError(RejectionReason.UNSUPPORTED_PUBLICATION, "private diagnostic")
    monkeypatch.setattr(h.guard, "replace_file", refused)
    result = h.invoke(revision)
    record = h.actions.load(result.action_id)
    assert not result.ok and "private diagnostic" not in str(result.result.to_dict())
    if entered:
        assert record.terminal_outcome is None
        assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
        assert result.result.metadata["kind"] != "filesystem_publication_unsupported"
    else:
        assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
        assert record.terminal_outcome.result_class == "filesystem.unsupported_publication"
    assert h.target.read_bytes() == "héllo\n".encode() and not h.residue()


@pytest.mark.parametrize("state", ["new", "old", "absent"])
def test_legacy_uncertain_posix_replacement_never_settled_or_retried(make, monkeypatch, state):
    h = make()
    revision = prior_revision(h, monkeypatch)
    calls = []
    def historical_attempt(intent, data, validate, publication):
        publication()
        calls.append(1)
        if state == "new":
            h.target.write_bytes(data)
        elif state == "absent":
            h.target.unlink()
        raise TimeoutError("historical uncertain replacement")
    monkeypatch.setattr(h.guard, "replace_file", historical_attempt)
    result = h.invoke(revision)
    monkeypatch.setattr(sys, "platform", "linux")
    report = h.reconcile(result.action_id)
    record = h.actions.load(result.action_id)
    assert report.status is ActionReconciliationStatus.UNRESOLVED
    assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
    assert not h.invoke(revision, action_context=ActionInvocationContext(action_id=result.action_id)).ok
    assert calls == [1] and len(record.attempts) == 1 and not h.residue()
