"""Synthetic native boundary: ordering and durable truth, not Windows API proof."""

from contextlib import contextmanager
import os
import sys
from types import SimpleNamespace

import pytest

import agentcore.fs
from agentcore.action_journal import (
    ActionAttemptStatus, ActionInvocationContext, ActionReconciliationStatus,
    ActionState, EffectVerdict, ReconciliationDisposition,
)
from agentcore.fs import replacement
from agentcore.fs.replacement import RevisionRejectedError
from agentcore.errors import PolicyLeaseBusyError
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.registry import ToolRegistryLeaseError
from test_fs_replace import make, native_security  # noqa: F401
from test_fs_replace_support import prior_revision


@pytest.mark.parametrize("seam", [
    "success", "write", "private_flush", "stale", "marker", "old_close",
    "rename", "lost_return", "restore", "restored_flush", "observe_name",
    "discard", "owned_close", "parent_close",
])
@pytest.mark.parametrize("error", [TimeoutError, CancellationRequested])
def test_private_replace_lifecycle_and_durable_truth(make, monkeypatch, seam, error):
    h = make(); revision = prior_revision(h, monkeypatch)
    events, names = [], []
    owned, parent = object(), object()
    renamed = restored = False

    def step(name):
        events.append(name)
        assert h.executor._filesystem_policy_leases and h.executor._filesystem_health_owners
        with pytest.raises(ToolRegistryLeaseError):
            h.registry.unregister("replace_file")
        with pytest.raises(PolicyLeaseBusyError):
            h.executor.policy = h.executor.policy
        if seam == name:
            raise error("PRIVATE_LIFECYCLE_FAILURE")

    @contextmanager
    def parents(*args):
        try:
            yield parent, []
        finally:
            step("parent_close")

    def create(opened, name, metadata):
        assert opened is parent
        names.append(name)
        step("create")
        return owned

    def write(opened, data):
        assert opened is owned and data == b"new complete content"
        step("write")
        if seam == "discard":
            raise OSError("private write failure")

    def flush(opened):
        assert opened is owned
        step("restored_flush" if restored else "private_flush")

    def publish(opened, directory, name):
        nonlocal renamed
        assert opened is owned and directory is parent and name == "old.txt"
        assert not restored and events[-1] == "old_close"
        step("rename")
        if seam == "observe_name":
            raise OSError("private rename failure")
        renamed = True
        step("lost_return")

    def restore(opened, metadata):
        nonlocal restored
        assert opened is owned and renamed and not restored
        step("restore")
        restored = True

    def has_name(opened, name):
        assert opened is owned and names == [name]
        step("observe_name")
        return not renamed

    def close(opened):
        step("owned_close" if opened is owned else "old_close")

    def check(*args):
        if "create" in events:
            events.append("stale")
            if seam == "stale":
                raise RevisionRejectedError()

    native = SimpleNamespace(create_update_stage=create, write=write, flush=flush,
        open_update_target=lambda *args: "old", publish_update=publish,
        preserve_update_metadata=restore, has_staging_name=has_name,
        discard=lambda opened: step("discard"), close=close)
    # No native I/O is available through this fixture, including on POSIX.
    monkeypatch.setitem(sys.modules, "agentcore.fs._windows_mutation", native)
    monkeypatch.setattr(agentcore.fs, "_windows_mutation", native, raising=False)
    monkeypatch.setattr(replacement, "_require_file_publication", lambda: None)
    monkeypatch.setattr(replacement, "parent", parents)
    monkeypatch.setattr(replacement, "_check", lambda *args: None)
    monkeypatch.setattr(replacement, "check_snapshot", check)
    original = h.guard.replace_file
    def attempt(intent, data, validate, publication):
        def marker():
            step("marker")
            publication()
        return original(intent, data, validate, marker)
    monkeypatch.setattr(h.guard, "replace_file", attempt)

    if error is CancellationRequested and seam not in {"success", "stale"}:
        with pytest.raises(CancellationRequested):
            h.invoke(revision)
    else:
        assert h.invoke(revision).ok is (seam == "success")
    record = h.actions.load(h.actions.list_actions()[0])
    entered = "marker" in events and seam != "marker"
    assert events[-2:] == ["owned_close", "parent_close"]
    assert ("discard" in events) is (not renamed and seam != "observe_name")
    assert restored is (seam in {"success", "restored_flush", "owned_close", "parent_close"})
    if seam == "success":
        assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    elif entered:
        assert record.state is ActionState.RECONCILE_REQUIRED and record.terminal_outcome is None
        assert record.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
        assert h.reconcile(record.action_id).status is ActionReconciliationStatus.UNRESOLVED
        assert h.actions.load(record.action_id).reconciliation.disposition is (
            ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
    else:
        if seam == "stale":
            assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
        else:
            assert record.attempts[0].status is ActionAttemptStatus.NOT_INVOKED
    before = list(events)
    assert not h.invoke(revision, action_context=ActionInvocationContext(action_id=record.action_id)).ok
    assert events == before and len(h.actions.load(record.action_id).attempts) == 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows attributes after rename and close")
@pytest.mark.parametrize("attributes", [0x80, 0x2, 0x2000])
@pytest.mark.parametrize("identical", [False, True])
def test_windows_replacement_preserves_archive_clear_attributes(make, attributes, identical):
    import win32file
    h = make()
    win32file.SetFileAttributes(str(h.target), attributes)
    revision = h.revision()
    old = h.revisions._snapshots[revision]
    assert old["metadata"]["attributes"] == attributes
    content = h.target.read_text(encoding="utf-8") if identical else "NEW"
    result = h.invoke(revision, content=content)
    assert result.ok and not h.residue()
    text, observed = h.guard.snapshot_for_update("sub/old.txt", 1000)
    assert text == content and observed["identity"] != old["identity"]
    assert observed["metadata"] == old["metadata"]
    assert h.actions.load(result.action_id).terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED


@pytest.mark.skipif(os.name != "nt", reason="native Windows partial metadata restoration")
def test_windows_security_restored_before_attribute_failure_remains_uncertain(make, monkeypatch):
    import win32file
    from agentcore.fs import _windows_mutation as win
    h = make()
    win32file.SetFileAttributes(str(h.target), 0x22)
    revision = h.revision()
    old = h.revisions._snapshots[revision]
    setter, restored = win._set_update_security, []
    def interrupted(opened, descriptor):
        setter(opened, descriptor)
        if win.final_name(opened) == h.target.name:
            restored.append(True)
            raise TimeoutError("private partial restoration failure")
    monkeypatch.setattr(win, "_set_update_security", interrupted)
    result = h.invoke(revision)
    text, observed = h.guard.snapshot_for_update("sub/old.txt", 1000)
    assert restored == [True] and not result.ok and not h.residue()
    assert text == "new complete content" and observed["identity"] != old["identity"]
    assert observed["metadata"]["security"] == old["metadata"]["security"]
    assert observed["metadata"]["attributes"] != old["metadata"]["attributes"]
    record = h.actions.load(result.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and record.terminal_outcome is None
    assert record.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert h.reconcile(record.action_id).status is ActionReconciliationStatus.UNRESOLVED
