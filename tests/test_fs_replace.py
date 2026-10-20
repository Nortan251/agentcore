"""Trusted revisions, real atomic replacement, final seams and recovery truth."""

import json
import os
import sys
import threading

import pytest

from agentcore.action_journal import ActionAttemptStatus, ActionReconciliationStatus, ActionState, EffectVerdict
from agentcore.errors import PolicyLeaseBusyError
from agentcore.fs import replacement
from agentcore.fs.revisions import FileRevisionRegistry
from agentcore.fs.replacement import RevisionRejectedError
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from agentcore.tools.builtin.create_directory import FilesystemDirectoryFoundation
from agentcore.tools.builtin.replace_file import FilesystemReplaceFoundation
from agentcore.tools.builtin.read_file_for_update import UPDATE_RESULT_PREFIX
from agentcore.tools.registry import ToolRegistryLeaseError
from test_fs_write import Harness as WriteHarness, _symlink


@pytest.fixture(scope="module")
def native_security():
    # Test-only synthetic fixtures may exercise already-assigned Windows
    # privileges on CI. Production never enables any privilege. An ordinary
    # token lacking this privilege is tested for categorical refusal below.
    if os.name != "nt":
        yield True
        return
    import win32api, win32con, win32security
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY | win32con.TOKEN_ADJUST_PRIVILEGES)
    luid = win32security.LookupPrivilegeValue(None, "SeSecurityPrivilege")
    privileges = dict(win32security.GetTokenInformation(token, win32security.TokenPrivileges))
    available = luid in privileges
    try:
        if available:
            win32security.AdjustTokenPrivileges(token, False, [(luid, win32security.SE_PRIVILEGE_ENABLED)])
        yield available
    finally:
        if available:
            win32security.AdjustTokenPrivileges(token, False, [(luid, privileges[luid])])
        token.Close()


class Harness(WriteHarness):
    def __init__(self, base, *, result_limit=2000, capacity=96, native_available=True, **kwargs):
        super().__init__(base, **kwargs)
        self.write_foundation = self.foundation
        self.directory = FilesystemDirectoryFoundation(self.guard)
        self.directory.register(self.registry, self.executor._action_adapters)
        self.revisions = FileRevisionRegistry(self.guard, capacity=capacity)
        self.foundation = FilesystemReplaceFoundation(self.guard, self.revisions, max_result_chars=result_limit)
        self.foundation.register(self.registry, self.executor._action_adapters)
        self.target = self.root / "sub/old.txt"
        self.target.write_bytes("héllo\n".encode())
        self.native_available = native_available

    def read(self, path="sub/old.txt"):
        return self.executor.invoke("read_file_for_update", {"path": path})

    def revision(self):
        if os.name != "nt":
            pytest.skip("replacement requires native Windows handles; POSIX refusal tested separately")
        result = self.read()
        if not result.ok and os.name == "nt" and not self.native_available:
            pytest.skip("current Windows token cannot snapshot full security descriptor")
        assert result.ok, result.result
        return json.loads(result.result.output)["revision"]

    def invoke(self, revision, *, path="sub/old.txt", content="new complete content", **kwargs):
        return self.executor.invoke("replace_file", {"path": path, "revision": revision,
                                                      "content": content}, **kwargs)

    def close(self):
        self.revisions.close()
        super().close()


@pytest.fixture
def make(tmp_path, native_security):
    opened = []
    def build(**kwargs):
        h = Harness(tmp_path / str(len(opened)), native_available=native_security, **kwargs)
        opened.append(h)
        return h
    yield build
    for h in reversed(opened):
        h.close()


def publication_hook(monkeypatch, hook):
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        original = win.publish_update
        monkeypatch.setattr(win, "publish_update", lambda *a: hook(lambda: original(*a)))


def test_complete_utf8_snapshot_and_structured_host_revision(make):
    h = make()
    result = h.read()
    if os.name != "nt":
        assert not result.ok and result.result.metadata["kind"] == "filesystem_publication_unsupported"
        assert not h.revisions._snapshots and not h.actions.list_actions() and not h.residue()
        return
    if os.name == "nt" and not result.ok and not h.native_available:
        assert result.result.metadata["kind"] == "update_read_refused"
        assert not h.revisions._snapshots and not h.actions.list_actions()
        return
    assert result.ok
    body = json.loads(result.result.output)
    assert set(body) == {"revision", "content"} and body["content"] == "héllo\n"
    assert len(body["revision"]) == 68 and body["revision"] in h.revisions._snapshots
    assert not h.actions.list_actions()
    assert h.foundation.read_tool.spec.read_only
    assert h.foundation.read_tool.capabilities == frozenset({Capability.FS_READ})
    assert not {"read_file_for_update", "replace_file", "write_file", "create_directory"} & {t.name for t in default_tools()}


@pytest.mark.skipif(os.name != "nt", reason="native Windows revision snapshot")
def test_windows_native_revision_snapshot(make, native_security):
    if not native_security:
        pytest.skip("test token lacks assigned SeSecurityPrivilege")
    import traceback
    h = make()
    try:
        text, snapshot = h.guard.snapshot_for_update("sub/old.txt", 1000)
    except Exception as exc:
        # Keep CI diagnostics categorical: no paths, descriptors, SIDs or content.
        frames = traceback.extract_tb(exc.__traceback__)
        location = ",".join(f"{frame.name}:{frame.lineno}" for frame in frames)
        pytest.fail(f"native snapshot: {type(exc).__name__}, "
                    f"winerror={getattr(exc, 'winerror', None)}, calls={location}", pytrace=False)
    assert text == "héllo\n" and snapshot["size"] == len(text.encode())
    assert snapshot["metadata"]["security"]
    assert not h.revisions._snapshots and not h.actions.list_actions()


@pytest.mark.skipif(os.name != "nt", reason="native Windows security privilege")
def test_windows_update_read_without_security_privilege_refused(make, native_security):
    import win32api, win32con, win32security
    h = make()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY | win32con.TOKEN_ADJUST_PRIVILEGES)
    luid = win32security.LookupPrivilegeValue(None, "SeSecurityPrivilege")
    try:
        if native_security:
            previous = dict(win32security.GetTokenInformation(token, win32security.TokenPrivileges))[luid]
            win32security.AdjustTokenPrivileges(token, False, [(luid, 0)])
        try:
            result = h.read()
            assert not result.ok and result.result.metadata["kind"] == "update_read_refused"
            assert not h.revisions._snapshots and not h.actions.list_actions() and not h.residue()
            assert h.target.read_bytes() == "héllo\n".encode()
        finally:
            if native_security:
                win32security.AdjustTokenPrivileges(token, False, [(luid, previous)])
    finally:
        token.Close()


@pytest.mark.skipif(os.name != "nt", reason="native Windows staging metadata")
def test_windows_native_stage_metadata(make, native_security):
    if not native_security:
        pytest.skip("test token lacks assigned SeSecurityPrivilege")
    import traceback
    from agentcore.fs import _windows_mutation as win
    h = make()
    metadata = h.revisions._snapshots[h.revision()]["metadata"]
    relative = replacement.relative_path(h.guard, "sub/old.txt")
    try:
        with replacement.parent(h.guard, relative) as (parent, _):
            stage = win.create_update_stage(parent, ".agentcore-stage-native-test", metadata)
            try:
                win.write(stage, b"complete synthetic content")
                win.flush(stage)
                win.preserve_update_metadata(stage, metadata)
                assert win.update_facts(stage)["metadata"] == metadata
            finally:
                try:
                    win.discard(stage)
                finally:
                    win.close(stage)
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        location = ",".join(f"{frame.name}:{frame.lineno}" for frame in frames)
        pytest.fail(f"native stage: {type(exc).__name__}, "
                    f"winerror={getattr(exc, 'winerror', None)}, calls={location}", pytrace=False)
    assert not h.residue() and h.target.read_bytes() == "héllo\n".encode()
    assert not h.actions.list_actions()


@pytest.mark.skipif(os.name != "nt", reason="native Windows inherited SACL")
def test_windows_inherited_parent_sacl_refused_before_publication(make, native_security, monkeypatch):
    if not native_security:
        pytest.skip("test token lacks assigned SeSecurityPrivilege")
    import win32con, win32file, win32security
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    old = h.revisions._snapshots[revision]["metadata"]
    source = win32security.SECURITY_DESCRIPTOR(bytes.fromhex(old["security"]))
    assert not source.GetSecurityDescriptorControl()[0] & 0x10
    audit = win32security.ACL()
    audit.AddAuditAccessAceEx(win32security.ACL_REVISION,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE, win32con.GENERIC_READ,
        win32security.CreateWellKnownSid(win32security.WinWorldSid, None), True, True)
    parent = win32file.CreateFile(str(h.target.parent), win32con.READ_CONTROL | win32con.ACCESS_SYSTEM_SECURITY,
        3, None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
    try:
        descriptor = win32security.GetKernelObjectSecurity(parent, 0x10000)
        descriptor.SetSecurityDescriptorSacl(True, audit, False)
        # The kernel setter changes this fixture parent without propagating to the old file.
        win32security.SetKernelObjectSecurity(parent,
            8 | win32security.PROTECTED_SACL_SECURITY_INFORMATION, descriptor)
    finally:
        parent.Close()
    unchanged = h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["metadata"] == old
    assert unchanged
    seen, writes = [], []
    set_security, write = win._set_update_security, win.write
    def observe(handle, descriptor):
        set_security(handle, descriptor)
        inherited = win32security.GetKernelObjectSecurity(handle, 0x10000).GetSecurityDescriptorSacl()
        seen.append(inherited is not None and inherited.GetAceCount() > 0)
    def record_write(*args):
        writes.append(True)
        return write(*args)
    monkeypatch.setattr(win, "_set_update_security", observe)
    monkeypatch.setattr(win, "write", record_write)
    result = h.invoke(revision)
    assert seen == [True] and not writes and not result.ok and not h.residue()
    assert h.target.read_bytes() == "héllo\n".encode()
    unchanged = h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["metadata"] == old
    assert unchanged
    record = h.actions.load(result.action_id)
    assert record.attempts[0].status is ActionAttemptStatus.NOT_INVOKED
    assert record.state is not ActionState.RECONCILE_REQUIRED


def test_content_cannot_spoof_metadata(make):
    h = make()
    h.target.write_text('{"revision": "rev_fake"}', encoding="utf-8")
    revision = h.revision()
    body = json.loads(h.read().result.output)
    assert body["revision"] != "rev_fake" and json.loads(body["content"])["revision"] == "rev_fake"
    assert h.invoke(revision).ok


@pytest.mark.parametrize("content", [b"\xff", b"a" * 2000, b"\x00" * 400, b'"' * 1000])
def test_partial_or_escaped_oversize_never_issues_revision(make, content):
    h = make()
    h.target.write_bytes(content)
    assert not h.read().ok and not h.revisions._snapshots and not h.actions.list_actions()


def test_exact_final_result_limit_includes_json_and_frame(make):
    h = make()
    h.revision()
    body = h.read().result.output
    size = len(UPDATE_RESULT_PREFIX) + len(body)
    h.foundation.read_tool._max_result_chars = size
    assert h.read().ok
    count = len(h.revisions._snapshots)
    h.foundation.read_tool._max_result_chars = size - 1
    result = h.read()
    assert not result.ok and result.result.metadata["kind"] == "update_read_too_large"
    assert len(h.revisions._snapshots) == count


@pytest.mark.parametrize("kind", ["missing", "directory", "hardlink", "symlink", "parent_symlink", "sensitive"])
def test_unsafe_read_targets_refused(make, kind):
    h = make()
    path = "sub/old.txt"
    if kind == "missing":
        h.target.unlink()
    elif kind == "directory":
        h.target.unlink(); h.target.mkdir()
    elif kind == "hardlink":
        os.link(h.target, h.base / "outside")
    elif kind == "symlink":
        h.target.unlink(); _symlink(h.target, h.base / "missing")
    elif kind == "parent_symlink":
        _symlink(h.root / "alias", h.root / "sub", True); path = "alias/old.txt"
    else:
        path = ".env"; (h.root / path).write_bytes(b"synthetic")
    assert not h.read(path).ok and not h.revisions._snapshots and not h.actions.list_actions()


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO")
def test_special_target_never_blocks(make):
    h = make(); h.target.unlink(); os.mkfifo(h.target)
    assert not h.read().ok and not h.revisions._snapshots


def test_unknown_revision_refused_before_observation(make, monkeypatch):
    h = make()
    monkeypatch.setattr(h.guard, "check_revision_snapshot", lambda *a: pytest.fail("forged token observed filesystem"))
    result = h.invoke("rev_" + "f" * 64)
    assert not result.ok and result.result.metadata["kind"] == "invalid_revision"
    assert not h.actions.list_actions() and not h.residue()


def test_bounded_registry_and_no_content_retention(make):
    h = make(capacity=2)
    h.revision(); h.revision()
    assert not h.read().ok and len(h.revisions._snapshots) == 2
    assert all("content" not in s for s in h.revisions._snapshots.values())


@pytest.mark.parametrize("source", ["other_host", "path", "workspace", "consumed", "closed"])
def test_revisions_do_not_cross_authority_domains(make, source):
    h = make(); revision = h.revision()
    receiver, path = h, "sub/old.txt"
    if source in {"other_host", "workspace"}:
        receiver = make()
        if source == "other_host":
            receiver.revisions.guard = h.guard
    elif source == "path":
        path = "sub/other.txt"; (h.root / path).write_bytes(b"other")
    elif source == "consumed":
        assert h.invoke(revision).ok
    else:
        h.revisions.close()
    count = len(receiver.actions.list_actions())
    assert not receiver.invoke(revision, path=path).ok
    assert len(receiver.actions.list_actions()) == count


def test_thread_registry_authority_cannot_be_reused(make):
    h = make()
    errors = []
    def worker():
        try: h.revisions.candidate()
        except RevisionRejectedError: errors.append(True)
    t = threading.Thread(target=worker); t.start(); t.join(10)
    assert not t.is_alive() and errors == [True]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork ownership")
def test_fork_registry_refuses_inherited_authority(make):
    h = make(); revision = h.revisions.candidate()
    h.revisions.issue(revision, {"path": "sub/old.txt"})
    pid = os.fork()
    if pid == 0:
        try: h.revisions.consume(revision, "sub/old.txt")
        except RevisionRejectedError: os._exit(0)
        os._exit(1)
    assert os.waitpid(pid, 0)[1] == 0


@pytest.mark.parametrize("change", ["content", "identity", "missing", "hardlink", "symlink", "directory", "parent"])
def test_stale_read_never_admitted(make, change):
    h = make(); revision = h.revision()
    if change == "content": h.target.write_bytes(b"changed")
    elif change == "identity":
        other = h.root / "sub/other"; other.write_bytes(h.target.read_bytes()); os.replace(other, h.target)
    elif change == "missing": h.target.unlink()
    elif change == "hardlink": os.link(h.target, h.base / "alias")
    elif change == "symlink": h.target.unlink(); _symlink(h.target, h.base / "missing")
    elif change == "directory": h.target.unlink(); h.target.mkdir()
    else:
        os.rename(h.root / "sub", h.root / "old"); (h.root / "sub").mkdir()
    result = h.invoke(revision)
    assert not result.ok and result.result.metadata["kind"] == "stale_revision"
    assert not h.actions.list_actions() and not h.residue()


def test_real_replacement_has_journal_truth_and_preserves_metadata(make, monkeypatch):
    h = make(); revision = h.revision()
    old = h.revisions._snapshots[revision]["metadata"]
    def check(publish):
        record = h.actions.load(h.actions.list_actions()[0])
        assert record.state is ActionState.IN_FLIGHT and record.intent_ref
        assert record.attempts[0].status is ActionAttemptStatus.CLAIMED
        payload = h.payloads.load(record.intent_ref, action_id=record.action_id).payload
        assert payload["old"]["metadata"] == old and "content" not in payload["old"]
        assert h.target.read_bytes() == "héllo\n".encode()
        return publish()
    publication_hook(monkeypatch, check)
    result = h.invoke(revision)
    assert result.ok and h.target.read_bytes() == b"new complete content"
    assert h.actions.load(result.action_id).terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["metadata"] == old
    assert not h.residue()


@pytest.mark.parametrize("late", ["content", "missing", "hardlink", "directory", "symlink"])
def test_second_stale_check_definite_no_effect(make, monkeypatch, late):
    h = make(); revision = h.revision()
    original = h.guard.replace_file
    def change(*args):
        if late == "content": h.target.write_bytes(b"late changed")
        elif late == "missing": h.target.unlink()
        elif late == "hardlink": os.link(h.target, h.base / "alias")
        elif late == "directory": h.target.unlink(); h.target.mkdir()
        else: h.target.unlink(); _symlink(h.target, h.base / "absent")
        return original(*args)
    monkeypatch.setattr(h.guard, "replace_file", change)
    result = h.invoke(revision)
    record = h.actions.load(result.action_id)
    assert not result.ok and record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
    assert not h.residue() and (h.target.exists() is False if late in {"missing", "symlink"} else True)


def test_stale_change_after_staging_is_caught_at_final_comparison(make, monkeypatch):
    h = make(); revision = h.revision()
    original = replacement.check_snapshot
    count = []
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        flush = win.flush
        def after_staging(handle):
            flush(handle)
            assert h.residue() and len(count) == 2
            # Mutate before the final target handle excludes other writers.
            h.target.write_bytes(b"changed after staging")
        monkeypatch.setattr(win, "flush", after_staging)
    def interfere(guard, expected):
        count.append(True)
        # preparation, then initial execution comparison, then final check.
        if len(count) == 3:
            assert h.residue()
            if os.name != "nt":
                h.target.write_bytes(b"changed after staging")
        return original(guard, expected)
    monkeypatch.setattr(replacement, "check_snapshot", interfere)
    result = h.invoke(revision)
    assert not result.ok and len(count) == 3 and not h.residue()
    assert h.target.read_bytes() == b"changed after staging"
    assert h.actions.load(result.action_id).terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED


def test_worst_escaping_new_content_fits_protected_record(make):
    from agentcore.fs.config import MAX_WRITE_FILE_BYTES
    h = make(); revision = h.revision()
    assert h.invoke(revision, content="\x00" * MAX_WRITE_FILE_BYTES).ok
    assert h.target.stat().st_size == MAX_WRITE_FILE_BYTES
    # Complete new body intentionally cannot be update-read under 2000 chars.
    assert not h.read().ok


def test_reconciliation_requires_current_read_and_never_reexecutes(make, monkeypatch):
    h = make(); revision = h.revision()
    def uncertain(publish): publish(); raise TimeoutError()
    publication_hook(monkeypatch, uncertain)
    result = h.invoke(revision)
    h.executor.policy = PermissionPolicy(PermissionConfig(blocked_capabilities=(str(Capability.FS_READ),)))
    monkeypatch.setattr(h.guard, "observe_replace", lambda *a: pytest.fail("denied probe observed"))
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.PROBE_DENIED


@pytest.mark.parametrize("seam", ["write", "flush", "authority"])
def test_prepublication_failure_old_intact_and_stage_cleaned(make, monkeypatch, seam):
    h = make(); revision = h.revision()
    if seam == "authority":
        monkeypatch.setattr(h.guard, "replace_file", lambda *a: (_ for _ in ()).throw(ValueError("safe failure")))
    elif os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        monkeypatch.setattr(win, "write" if seam == "write" else "flush",
                            lambda *a: (_ for _ in ()).throw(OSError("safe failure")))
    else:
        # Affect only stage I/O, not the protected/journal storage fsyncs.
        original = h.guard.replace_file
        def fail(*args):
            with monkeypatch.context() as patch:
                patch.setattr(replacement.os, "write" if seam == "write" else "fsync",
                              lambda *a: (_ for _ in ()).throw(OSError("safe failure")))
                return original(*args)
        monkeypatch.setattr(h.guard, "replace_file", fail)
    result = h.invoke(revision)
    assert not result.ok and h.target.read_bytes() == "héllo\n".encode() and not h.residue()
    assert h.actions.load(result.action_id).attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


@pytest.mark.parametrize("state", ["new", "old", "absent", "other", "directory", "hardlink"])
def test_uncertain_publication_reconciles_only_exact_new_state(make, monkeypatch, state):
    h = make(); revision = h.revision()
    old_mode = h.target.stat().st_mode & 0o777
    def uncertain(publish):
        if state != "old":
            publish()
            if os.name == "posix":
                # Model a lost return after complete metadata restoration.
                os.chmod(h.target, old_mode)
        raise TimeoutError("PRIVATE_ERROR_SENTINEL")
    publication_hook(monkeypatch, uncertain)
    result = h.invoke(revision, content="PRIVATE_NEW_BODY")
    assert h.actions.load(result.action_id).state is ActionState.RECONCILE_REQUIRED
    if state == "absent": h.target.unlink()
    elif state == "other": h.target.write_bytes(b"different")
    elif state == "directory": h.target.unlink(); h.target.mkdir()
    elif state == "hardlink": os.link(h.target, h.base / "alias")
    report = h.reconcile(result.action_id)
    assert report.status is (ActionReconciliationStatus.SETTLED_SUCCEEDED if state == "new"
                             else ActionReconciliationStatus.UNRESOLVED)
    assert not h.invoke(revision).ok
    public = json.dumps(result.to_dict()) + repr(h.actions.load(result.action_id)) + repr(h.logger.events)
    for secret in ("PRIVATE_NEW_BODY", "PRIVATE_ERROR_SENTINEL", revision, "sub/old.txt", str(h.root)):
        assert secret not in public


@pytest.mark.parametrize("capability", [Capability.FS_READ, Capability.FS_WRITE])
def test_current_revocation_prevents_preparation(make, monkeypatch, capability):
    h = make(); revision = h.revision()
    h.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS,
        blocked_capabilities=(str(capability),)))
    monkeypatch.setattr(h.guard, "check_revision_snapshot", lambda *a: pytest.fail("denied target observation"))
    assert h.invoke(revision).result.metadata["kind"] == "permission_denied"
    assert not h.actions.list_actions()


@pytest.mark.skipif(os.name != "nt", reason="native Windows cross-tool publication")
def test_direct_and_cross_tool_contexts_cannot_replace(make, monkeypatch):
    h = make()
    args = {"path": "sub/old.txt", "revision": "rev_" + "f" * 64, "content": "changed"}
    assert not h.foundation.tool.run(args, ToolContext(workspace=h.guard)).ok
    original = h.write_foundation.tool.run
    def cross(cleaned, context):
        assert not h.foundation.tool.run(args, context).ok
        return original(cleaned, context)
    monkeypatch.setattr(h.write_foundation.tool, "run", cross)
    assert h.executor.invoke("write_file", {"path": "sub/new.txt", "content": "create"}).ok
    assert h.target.read_bytes() == "héllo\n".encode()


@pytest.mark.skipif(os.name != "posix", reason="native POSIX refusal preserves modes")
@pytest.mark.parametrize("mode", [0o600, 0o644, 0o700, 0o755])
def test_posix_modes_categorically_refused_without_effect(make, mode):
    h = make(); h.target.chmod(mode)
    before = h.target.read_bytes(), h.target.stat()
    result = h.read()
    assert not result.ok and result.result.metadata["kind"] == "filesystem_publication_unsupported"
    assert h.target.read_bytes() == before[0] and h.target.stat() == before[1]
    assert not h.revisions._snapshots and not h.actions.list_actions() and not h.residue()


@pytest.mark.skipif(os.name != "posix", reason="POSIX unusual modes/xattrs")
@pytest.mark.parametrize("kind", ["setuid", "setgid", "xattr"])
def test_unsupported_posix_metadata_refused(make, kind):
    h = make()
    if kind == "xattr": os.setxattr(h.target, "user.synthetic", b"fixture")
    else: h.target.chmod(0o4644 if kind == "setuid" else 0o2644)
    assert not h.read().ok and not h.revisions._snapshots


def test_registry_and_policy_leased_at_exact_native_publication(make, monkeypatch):
    h = make(); revision = h.revision()
    entered, release = threading.Event(), threading.Event()
    failures = []
    def actor():
        try:
            assert entered.wait(15)
            with pytest.raises(ToolRegistryLeaseError): h.registry.unregister("replace_file")
            with pytest.raises(ToolRegistryLeaseError): h.registry.register(h.foundation.tool, replace=True)
            with pytest.raises(PolicyLeaseBusyError):
                h.executor.policy = PermissionPolicy(PermissionConfig())
            for lock in (h.registry._lock, h.executor._policy_lock):
                assert lock.acquire(blocking=False); lock.release()
        except BaseException as exc: failures.append(exc)
        finally: release.set()
    def paused(publish):
        entered.set(); assert release.wait(15); return publish()
    publication_hook(monkeypatch, paused)
    t = threading.Thread(target=actor); t.start()
    try: result = h.invoke(revision)
    finally: release.set(); t.join(15)
    assert result.ok and not failures and not t.is_alive()


def test_update_read_handles_close_on_success_and_refusal(make):
    h = make()
    for _ in range(5):
        h.read()
    moved = h.root / "moved"
    (h.root / "sub").rename(moved)
    moved.rename(h.root / "sub")
    assert not h.residue()


def test_update_read_paths_never_enter_generic_executor_logs(make):
    h = make()
    result = h.read()
    assert "private_arguments" in h.foundation.read_tool.spec.tags
    assert "sub/old.txt" not in repr(h.logger.events)
    assert "sub/old.txt" not in repr(result.args)


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive staging share")
def test_windows_partial_stage_cannot_be_opened_for_read_by_another_handle(make, monkeypatch):
    import pywintypes, win32con, win32file
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    write = win.write
    open_child = win.open_child
    observed, sharing = [], []
    def initial(*args, **kwargs):
        opened = open_child(*args, **kwargs)
        if kwargs.get("create") and kwargs.get("_update"):
            # Check zero sharing before the empty stage's DACL is locked.
            try:
                reader = win32file.CreateFile(str(h.residue()[0]), win32con.GENERIC_READ, 7,
                    None, win32con.OPEN_EXISTING, 0, None)
            except pywintypes.error as error:
                sharing.append(error.winerror)
            else:
                reader.Close()
                sharing.append(None)
        return opened
    def partial(handle, data):
        write(handle, data[:3])
        stage = h.residue()[0]
        with pytest.raises(pywintypes.error) as error:
            opened = win32file.CreateFile(str(stage), win32con.GENERIC_READ, 7,
                None, win32con.OPEN_EXISTING, 0, None)
            opened.Close()
        assert error.value.winerror == 5
        observed.append(True)
        raise OSError("partial private stage interrupted")
    monkeypatch.setattr(win, "open_child", initial)
    monkeypatch.setattr(win, "write", partial)
    result = h.invoke(revision)
    assert sharing == [32] and observed and not result.ok and not h.residue()
    assert h.target.read_bytes() == "héllo\n".encode()
    assert h.actions.load(result.action_id).attempts[0].status is ActionAttemptStatus.NOT_INVOKED


@pytest.mark.skipif(os.name != "nt", reason="native Windows private residual DACL")
def test_windows_private_dacl_denies_data_read_after_owned_handle_closes(tmp_path):
    import pywintypes, win32con, win32file, win32security
    from agentcore.fs import _windows_mutation as win
    path = tmp_path / "private-residue"
    opened = win32file.CreateFile(str(path), win32file.FILE_ALL_ACCESS, 0,
        None, win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_NORMAL, None)
    original = win32security.GetSecurityInfo(opened, win32security.SE_FILE_OBJECT, 1 | 2 | 4)
    closed = False
    try:
        initial = win.private_stage_descriptor({"security": bytes(original).hex()}, initial=True)
        assert initial.GetSecurityDescriptorDacl().GetAceCount() == 1
        assert initial.GetSecurityDescriptorControl()[0] & 0x1000
        private = win.private_stage_descriptor({"security": bytes(original).hex()})
        win32security.SetKernelObjectSecurity(opened, 4 | win32security.PROTECTED_DACL_SECURITY_INFORMATION, private)
        win32file.WriteFile(opened, b"synthetic partial fixture")
        opened.Close()
        closed = True
        with pytest.raises(pywintypes.error) as error:
            reader = win32file.CreateFile(str(path), win32con.GENERIC_READ, 7,
                None, win32con.OPEN_EXISTING, 0, None)
            reader.Close()
        assert error.value.winerror == 5
        # Owner READ_CONTROL remains available but does not grant data access.
        observed = win32security.GetFileSecurity(str(path), 4)
        assert observed.GetSecurityDescriptorDacl().GetAceCount() == 0
        assert observed.GetSecurityDescriptorControl()[0] & 0x1000
    finally:
        if not closed:
            opened.Close()
        win32security.SetFileSecurity(str(path), 4 | win32security.PROTECTED_DACL_SECURITY_INFORMATION, original)
        path.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows failed-write and failed-delete stage privacy")
def test_windows_failed_write_and_delete_leave_unreadable_residue(make, monkeypatch):
    import pywintypes, win32con, win32file, win32security
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    write = win.write
    def partial(handle, data):
        write(handle, data[:3]); raise OSError("partial private stage interrupted")
    monkeypatch.setattr(win, "write", partial)
    monkeypatch.setattr(win, "discard", lambda *a: (_ for _ in ()).throw(OSError("cleanup interrupted")))
    result = h.invoke(revision)
    stage = h.residue()[0]
    try:
        assert not result.ok and h.target.read_bytes() == "héllo\n".encode()
        assert h.actions.load(result.action_id).attempts[0].status is ActionAttemptStatus.NOT_INVOKED
        with pytest.raises(pywintypes.error) as error:
            reader = win32file.CreateFile(str(stage), win32con.GENERIC_READ, 7,
                None, win32con.OPEN_EXISTING, 0, None)
            reader.Close()
        assert error.value.winerror == 5
    finally:
        # Synthetic fixture teardown only; production never reopens a residue
        # to grant authority or retries a possibly published action.
        source = win32security.GetFileSecurity(str(h.target), 4)
        win32security.SetFileSecurity(str(stage), 4 | win32security.PROTECTED_DACL_SECURITY_INFORMATION, source)
        stage.unlink()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native inode flags")
def test_linux_nodump_flag_refused_without_replacement_or_flag_loss(make):
    import ctypes, fcntl, struct
    h = make()
    old = h.target.read_bytes()
    identity = h.target.stat().st_ino
    opened = os.open(h.target, os.O_RDONLY | os.O_NOFOLLOW)
    size = ctypes.sizeof(ctypes.c_long)
    flags = bytearray(size)
    fcntl.ioctl(opened, 0x80006601 | (size << 16), flags, True)
    saved = flags[:]
    struct.pack_into("=I", flags, 0, struct.unpack_from("=I", flags)[0] | 0x40)
    try:
        fcntl.ioctl(opened, 0x40006602 | (size << 16), flags, True)
        result = h.read()
        assert not result.ok and result.result.metadata["kind"] == "filesystem_publication_unsupported"
        assert not h.revisions._snapshots and not h.actions.list_actions() and not h.residue()
        assert h.target.stat().st_ino == identity and h.target.read_bytes() == old
        fcntl.ioctl(opened, 0x80006601 | (size << 16), flags, True)
        assert struct.unpack_from("=I", flags)[0] & 0x40
    finally:
        try:
            fcntl.ioctl(opened, 0x40006602 | (size << 16), saved, True)
        finally:
            os.close(opened)


@pytest.mark.skipif(os.name != "nt", reason="native Windows security and alternate-stream semantics")
@pytest.mark.parametrize("kind", ["readonly", "stream", "compressed"])
def test_windows_unsupported_metadata_and_data_refused(make, kind):
    import win32con, win32file
    h = make()
    if kind == "readonly":
        win32file.SetFileAttributes(str(h.target), win32con.FILE_ATTRIBUTE_READONLY)
    elif kind == "stream":
        with open(str(h.target) + ":synthetic", "wb") as stream:
            stream.write(b"must not discard")
    else:
        opened = win32file.CreateFile(str(h.target), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0, None, win32con.OPEN_EXISTING, 0, None)
        try:
            win32file.DeviceIoControl(opened, 0x9C040, b"\x01\x00", None)
        finally:
            opened.Close()
    try:
        assert not h.read().ok and not h.revisions._snapshots and not h.actions.list_actions()
        assert h.target.read_bytes() == "héllo\n".encode()
    finally:
        if kind == "readonly": win32file.SetFileAttributes(str(h.target), win32con.FILE_ATTRIBUTE_NORMAL)


@pytest.mark.skipif(os.name != "nt", reason="native Windows descriptor preservation, including audit SACL")
def test_windows_restricted_dacl_and_audit_sacl_preserved(make, native_security):
    if not native_security:
        pytest.skip("test token lacks assigned SeSecurityPrivilege")
    import win32api, win32con, win32file, win32security
    h = make()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32file.FILE_ALL_ACCESS, user)
    audit = win32security.ACL()
    audit.AddAuditAccessAce(win32security.ACL_REVISION, win32con.GENERIC_READ, user, True, True)
    sd = win32security.GetFileSecurity(str(h.target), 1 | 2 | 4 | 8)
    sd.SetSecurityDescriptorDacl(True, acl, False)
    sd.SetSecurityDescriptorSacl(True, audit, False)
    win32security.SetFileSecurity(str(h.target), 4 | 8 | win32security.PROTECTED_DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_SACL_SECURITY_INFORMATION, sd)
    revision = h.revision()
    captured = h.revisions._snapshots[revision]["metadata"]["security"]
    assert h.invoke(revision).ok
    assert h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["metadata"]["security"] == captured


@pytest.mark.skipif(os.name != "nt", reason="native Windows source and namespace protection")
def test_windows_replacement_publishes_owned_stage_and_pins_ancestors(make, monkeypatch):
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    old_identity = h.revisions._snapshots[revision]["identity"]
    original = win.publish_update
    stages = []
    def publish(owned, parent, name):
        stages.append(list(win.facts(owned)[1]))
        stage = h.residue()[0]
        for path in (stage, h.target.parent, h.root):
            with pytest.raises(PermissionError):
                path.rename(path.with_name(path.name + "-moved"))
        return original(owned, parent, name)
    monkeypatch.setattr(win, "publish_update", publish)
    assert h.invoke(revision).ok and len(stages) == 1 and stages[0] != old_identity
    assert h.guard.snapshot_for_update("sub/old.txt", 1000)[1]["identity"] == stages[0]
    assert h.target.read_bytes() == b"new complete content" and not h.residue()
    for path in (h.target.parent, h.root):
        moved = path.with_name(path.name + "-moved")
        path.rename(moved)
        moved.rename(path)


@pytest.mark.parametrize("level,approve,allowed", [
    (PermissionLevel.OBSERVE, False, False), (PermissionLevel.PROPOSE, False, False),
    (PermissionLevel.CONFIRM, False, False), (PermissionLevel.CONFIRM, True, True),
    (PermissionLevel.AUTONOMOUS, False, True),
])
def test_replacement_permission_approval_and_exact_action_identity(make, level, approve, allowed):
    h = make(level=level, approve=approve); revision = h.revision()
    result = h.invoke(revision)
    assert result.ok is allowed and not h.residue()
    assert h.target.read_bytes() == (b"new complete content" if allowed else "héllo\n".encode())
    if allowed:
        record = h.actions.load(result.action_id)
        assert record.tool_name == "replace_file" and record.intent_ref and len(record.attempts) == 1
        assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
        assert not h.invoke(revision).ok and len(h.actions.list_actions()) == 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows cancellation boundary")
@pytest.mark.parametrize("entered", [False, True])
@pytest.mark.parametrize("error", [TimeoutError, CancellationRequested])
def test_windows_replace_interruption_and_failed_cleanup_preserve_effect_truth(make, monkeypatch, entered, error):
    from agentcore.fs import _windows_mutation as win
    h = make(); revision = h.revision()
    seam = "publish_update" if entered else "write"
    def interrupted(*args):
        raise error("synthetic interruption")
    monkeypatch.setattr(win, seam, interrupted)
    if entered:
        # Restoration already broadened the completed stage; failed discard
        # must remain uncertain even though the final target is still old.
        monkeypatch.setattr(win, "discard", lambda *args: (_ for _ in ()).throw(OSError()))
    if error is CancellationRequested and not entered:
        with pytest.raises(CancellationRequested):
            h.invoke(revision)
    else:
        assert not h.invoke(revision).ok
    record = h.actions.load(h.actions.list_actions()[0])
    assert h.target.read_bytes() == "héllo\n".encode()
    if entered:
        assert record.state is ActionState.RECONCILE_REQUIRED
        assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
        assert h.reconcile(record.action_id).status is ActionReconciliationStatus.UNRESOLVED
        assert len(h.residue()) == 1
        h.residue()[0].unlink()
    else:
        assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED and not h.residue()
