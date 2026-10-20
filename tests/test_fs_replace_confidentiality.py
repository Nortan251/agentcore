"""Native access checks across failed cleanup and abrupt replacement process exit."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentcore.action_journal import (
    ActionAttemptStatus, ActionInvocationContext, ActionReconciliationStatus,
    ActionState, EffectVerdict, ReconciliationDisposition,
)
from test_fs_replace import Harness, make, native_security


pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows stage confidentiality")


def assert_unreadable(path):
    import pywintypes, win32con, win32file
    try:
        reader = win32file.CreateFile(str(path), win32con.GENERIC_READ, 7,
            None, win32con.OPEN_EXISTING, 0, None)
    except pywintypes.error as error:
        assert error.winerror == 5  # ACL denial, not the now-closed handle's sharing policy.
    else:
        try:
            assert win32file.ReadFile(reader, 100)[1] != b"replacement fixture", "readable replacement residue"
            pytest.fail("unexpected data-read access to private replacement")
        finally:
            reader.Close()


def restore_fixture_access(path, metadata):
    import win32security
    descriptor = win32security.SECURITY_DESCRIPTOR(bytes.fromhex(metadata["security"]))
    win32security.SetFileSecurity(str(path), 4 | win32security.PROTECTED_DACL_SECURITY_INFORMATION, descriptor)


@pytest.mark.parametrize("seam", ["restored_flush", "rename"])
def test_windows_failed_publication_and_discard_never_leave_readable_stage(make, monkeypatch, seam):
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    old = h.revisions._snapshots[revision]
    restore, flush, close = win.preserve_update_metadata, win.flush, win.close
    restored, stages, closed, discards = [], [], [], []
    def restoring(*args):
        restore(*args)
        restored.append(True)
    def flushing(*args):
        if restored and seam == "restored_flush":
            raise OSError("synthetic flush failure")
        return flush(*args)
    def publishing(*args):
        raise OSError("synthetic native publication failure")
    def discarding(handle):
        stages.append(handle)
        discards.append(True)
        raise OSError("synthetic discard failure")
    def closing(handle):
        close(handle)
        closed.append(handle)
    monkeypatch.setattr(win, "preserve_update_metadata", restoring)
    monkeypatch.setattr(win, "flush", flushing)
    monkeypatch.setattr(win, "discard", discarding)
    monkeypatch.setattr(win, "close", closing)
    if seam == "rename":
        monkeypatch.setattr(win, "publish_update", publishing)
    result = h.invoke(revision, content="replacement fixture")
    residue = h.residue()
    try:
        assert not result.ok
        record = h.actions.load(result.action_id)
        assert record.state is ActionState.RECONCILE_REQUIRED and record.terminal_outcome is None
        assert record.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
        if residue:
            assert discards == [True] and len(residue) == 1 and stages[0] in closed
            assert h.guard.snapshot_for_update("sub/old.txt", 1000)[1] == old
            assert_unreadable(residue[0])
        else:
            assert seam == "restored_flush" and restored == [True] and not discards
            assert h.target.read_bytes() == b"replacement fixture"
            assert h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["metadata"] == old["metadata"]
    finally:
        for stage in residue:
            restore_fixture_access(stage, old["metadata"])
            stage.unlink()


@pytest.mark.parametrize("seam", ["before_rename", "renamed", "before_restore", "restored"])
def test_windows_process_exit_cannot_expose_unpublished_stage(tmp_path, native_security, seam):
    if not native_security:
        pytest.skip("test token lacks assigned SeSecurityPrivilege")
    from agentcore.action_journal import JsonFileActionStore
    base = tmp_path / "crash"
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(base), seam],
        env=env, capture_output=True, timeout=60)
    assert result.returncode == 93, (result.returncode, result.stderr.decode(errors="replace"))
    old = json.loads((base / "fixture-metadata.json").read_text())
    residue = list((base / "workspace").rglob(".agentcore-stage-*"))
    target = base / "workspace/sub/old.txt"
    try:
        store = JsonFileActionStore(base / "actions")
        try:
            record = store.load(store.list_actions()[0])
            assert record.state is ActionState.IN_FLIGHT and record.terminal_outcome is None
            assert len(record.attempts) == 1 and record.attempts[0].status is ActionAttemptStatus.CLAIMED
        finally:
            store.close()
        if residue:
            assert len(residue) == 1
            assert target.read_bytes() == "héllo\n".encode()
            assert_unreadable(residue[0])
        else:
            assert seam != "before_rename"
            if seam in {"renamed", "before_restore"}:
                assert_unreadable(target)
            else:
                assert target.read_bytes() == b"replacement fixture"
    finally:
        for path in [*residue, target]:
            restore_fixture_access(path, old)


@pytest.mark.parametrize("seam", ["before", "after", "mismatch"])
def test_windows_metadata_failure_after_rename_never_reports_success(make, monkeypatch, seam):
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    old = h.revisions._snapshots[revision]
    restore, set_security = win.preserve_update_metadata, win._set_update_security
    entries = []
    def restoring(handle, metadata):
        entries.append(list(win.facts(handle)[1]))
        assert entries[0] != old["identity"] and win.final_name(handle) == h.target.name
        assert not h.residue()
        if seam == "before":
            raise TimeoutError("PRIVATE_RESTORATION_FAILURE")
        if seam == "mismatch":
            # Real native metadata mismatch must fail the production equality check.
            monkeypatch.setattr(win, "_set_update_security",
                lambda opened, sd: set_security(opened, win.private_stage_descriptor(metadata)))
        restore(handle, metadata)
        raise TimeoutError("PRIVATE_RESTORATION_FAILURE")
    monkeypatch.setattr(win, "preserve_update_metadata", restoring)
    result = h.invoke(revision, content="replacement fixture")
    try:
        record = h.actions.load(result.action_id)
        assert not result.ok and record.state is ActionState.RECONCILE_REQUIRED
        assert record.terminal_outcome is None and not h.residue() and len(entries) == 1
        assert record.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
        if seam == "after":
            assert h.target.read_bytes() == b"replacement fixture"
            assert h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["metadata"] == old["metadata"]
        else:
            assert_unreadable(h.target)
        assert h.reconcile(result.action_id).status is ActionReconciliationStatus.UNRESOLVED
        record = h.actions.load(result.action_id)
        assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
        h.recovery.recover_startup()
        assert not h.invoke(revision, action_context=ActionInvocationContext(action_id=result.action_id)).ok
        assert len(entries) == len(h.actions.load(result.action_id).attempts) == 1
        public = json.dumps(result.to_dict()) + repr(record) + repr(h.logger.events)
        for secret in ("PRIVATE_RESTORATION_FAILURE", "replacement fixture", revision, "sub/old.txt", str(h.root)):
            assert secret not in public
    finally:
        restore_fixture_access(h.target, old["metadata"])


@pytest.mark.parametrize("attributes", [0x20, 0x22, 0x2020])
@pytest.mark.parametrize("identical", [False, True])
def test_windows_private_publication_then_exact_metadata_success(make, monkeypatch, attributes, identical):
    import win32file, win32security
    from agentcore.fs import _windows_mutation as win
    h = make()
    win32file.SetFileAttributes(str(h.target), attributes)
    revision = h.revision()
    old = h.revisions._snapshots[revision]
    content = h.target.read_text(encoding="utf-8") if identical else "replacement fixture"
    publish, restore = win.publish_update, win.preserve_update_metadata
    events, identity = [], []
    def private(handle):
        sd = win32security.GetKernelObjectSecurity(handle, 4)
        assert sd.GetSecurityDescriptorDacl().GetAceCount() == 0
        assert sd.GetSecurityDescriptorControl()[0] & 0x1000
    def publishing(handle, parent, name):
        private(handle)
        identity.append(list(win.facts(handle)[1]))
        publish(handle, parent, name)
        private(handle)
        assert win.final_name(handle) == h.target.name and not h.residue()
        events.append("renamed")
    def restoring(handle, metadata):
        assert events == ["renamed"]
        restore(handle, metadata)
        assert win.update_facts(handle)["metadata"] == old["metadata"]
        events.append("restored")
    monkeypatch.setattr(win, "publish_update", publishing)
    monkeypatch.setattr(win, "preserve_update_metadata", restoring)
    result = h.invoke(revision, content=content)
    assert result.ok and events == ["renamed", "restored"] and not h.residue()
    text, observed = h.guard.snapshot_for_update("sub/old.txt", 1000)
    assert text == content and observed["identity"] == identity[0] != old["identity"]
    assert observed["metadata"] == old["metadata"]
    assert h.actions.load(result.action_id).terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED


def crash_worker(base, seam):
    from agentcore.fs import _windows_mutation as win
    security = native_security.__wrapped__()
    assert next(security)
    h = Harness(base)
    revision = h.revision()
    (base / "fixture-metadata.json").write_text(json.dumps(h.revisions._snapshots[revision]["metadata"]))
    restore, publish = win.preserve_update_metadata, win.publish_update
    def restoring(*args):
        if seam == "before_restore":
            os._exit(93)
        restore(*args)
        if seam == "restored":
            os._exit(93)
    def publishing(*args):
        if seam == "before_rename":
            os._exit(93)
        publish(*args)
        if seam == "renamed":
            os._exit(93)
    win.preserve_update_metadata, win.publish_update = restoring, publishing
    h.invoke(revision, content="replacement fixture")
    os._exit(94)


if __name__ == "__main__":
    crash_worker(Path(sys.argv[1]), sys.argv[2])
