"""Real create-only filesystem effects through the durable executor boundary."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from agentcore.action_journal import (
    ActionAdapterRegistry, ActionAttemptStatus, ActionInvocationContext,
    ActionReconciliationStatus, ActionState, JsonFileActionStore,
    JsonFileProtectedActionPayloadStore, StaticInstallationKeyProvider,
)
from agentcore.approvals import DurableApprovalAuthority, InMemoryApprovalStore, LegacyApprovalGateAdapter
from agentcore.fs.config import FilesystemConfig, MAX_WRITE_FILE_BYTES
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs import mutation
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, PermissionPolicy
from agentcore.realtime.errors import CancellationRequested
from agentcore.recovery import ActionRecoveryCoordinator
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from agentcore.tools.builtin.write_file import FilesystemWriteFoundation
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError
from agentcore.errors import PolicyLeaseBusyError
from test_task_43_2b_tool_executor_action_boundary import RecordingLogger


class Harness:
    def __init__(self, base, *, limit=MAX_WRITE_FILE_BYTES, level=PermissionLevel.AUTONOMOUS, approve=False):
        self.base, self.root = base, base / "workspace"
        (self.root / "sub").mkdir(parents=True)
        self.guard = WorkspaceGuard(self.root, config=FilesystemConfig(max_write_bytes=limit))
        self.foundation = FilesystemWriteFoundation(self.guard)
        self.registry, adapters = ToolRegistry(), ActionAdapterRegistry()
        self.foundation.register(self.registry, adapters)
        self.actions = JsonFileActionStore(base / "actions")
        self.payloads = JsonFileProtectedActionPayloadStore(base / "payloads", StaticInstallationKeyProvider(b"w" * 32))
        self.logger = RecordingLogger()
        authority = None
        if approve:
            authority = DurableApprovalAuthority(InMemoryApprovalStore(), StaticInstallationKeyProvider(b"a" * 32),
                principal_ref="principal:host", scope_ref="scope:workspace",
                gate=LegacyApprovalGateAdapter(AlwaysApproveGate()))
        self.executor = ToolExecutor(self.registry, PermissionPolicy(PermissionConfig(level=level)),
            workspace=self.guard, action_store=self.actions, action_adapters=adapters,
            protected_payload_store=self.payloads, approval_authority=authority, logger=self.logger)
        self.recovery = ActionRecoveryCoordinator(self.actions, self.executor)
        self.recovery.recover_startup()

    def invoke(self, path="sub/new.txt", content="héllo\n", **kwargs):
        return self.executor.invoke("write_file", {"path": path, "content": content}, **kwargs)

    def reconcile(self, action_id):
        claim = self.recovery.claim(action_id, expected_version=self.actions.load(action_id).version)
        return self.recovery.reconcile(claim)

    def residue(self):
        return list(self.root.rglob(".agentcore-stage-*"))

    def close(self):
        self.payloads.close()
        self.actions.close()


@pytest.fixture
def make(tmp_path):
    opened = []
    def build(**kwargs):
        h = Harness(tmp_path / str(len(opened)), **kwargs)
        opened.append(h)
        return h
    yield build
    for h in reversed(opened):
        h.close()


def test_create_through_real_durable_executor_and_default_stays_read_only(make):
    h = make()
    result = h.invoke()
    assert result.ok
    assert (h.root / "sub/new.txt").read_bytes() == "héllo\n".encode()
    record = h.actions.load(result.action_id)
    assert record.state is ActionState.SUCCEEDED and len(record.attempts) == 1
    assert record.attempts[0].status is ActionAttemptStatus.INVOKED
    assert record.intent_ref and not h.residue()
    assert h.foundation.tool.capabilities == frozenset({Capability.FS_WRITE})
    assert all(tool.spec.read_only for tool in default_tools())


@pytest.mark.parametrize("kind", ["regular", "directory", "hardlink"])
def test_existing_target_never_overwritten(make, kind):
    h = make()
    target = h.root / "sub/new.txt"
    if kind == "directory":
        target.mkdir()
    elif kind == "hardlink":
        source = h.base / "outside.txt"
        source.write_bytes(b"original")
        os.link(source, target)
    else:
        target.write_bytes(b"original")
    assert not h.invoke().ok and not h.actions.list_actions() and not h.residue()
    if kind != "directory":
        assert target.read_bytes() == b"original"


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/outside", "sub/../../escape",
                                 ".env", "sub/.env.local", ".git/new", "sub/id_rsa"])
def test_malicious_paths_refused_before_any_attempt(make, path):
    h = make()
    assert not h.invoke(path=path).ok
    assert not h.actions.list_actions() and not h.residue()


@pytest.mark.parametrize("exists", [False, True])
def test_sensitive_refused_independent_of_existence_and_read_relaxations(make, exists):
    h = make()
    if exists:
        (h.root / ".env").write_bytes(b"unchanged")
    h.guard._config = FilesystemConfig(deny_sensitive_paths=False, allow_internal_symlinks=True)
    assert not h.invoke(path=".env").ok and not h.residue()
    assert not h.actions.list_actions()


def _symlink(link, target, directory=False):
    if os.name == "nt":
        cmd = ["cmd", "/c", "mklink"] + (["/D"] if directory else []) + [str(link), str(target)]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        assert result.returncode == 0
    else:
        link.symlink_to(target, target_is_directory=directory)


@pytest.mark.parametrize("leaf", [False, True])
def test_preexisting_symlinks_never_followed(make, leaf):
    h = make()
    outside = h.base / "outside"
    outside.mkdir()
    if leaf:
        (outside / "data").write_bytes(b"outside unchanged")
        _symlink(h.root / "sub/new.txt", outside / "data")
    else:
        _symlink(h.root / "link", outside, True)
    assert not h.invoke(path="sub/new.txt" if leaf else "link/new.txt").ok
    assert not (outside / "new.txt").exists() and not h.residue()


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows reparse points")
def test_windows_junction_and_reserved_names(make):
    h = make()
    outside = h.base / "outside"
    outside.mkdir()
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(h.root / "junction"), str(outside)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0 and (h.root / "junction").lstat().st_file_attributes & 0x400
    assert not h.invoke(path="junction/new.txt").ok
    for path in ("CON", "sub/NUL.txt", "sub/new.txt:stream", "sub/trailing. "):
        assert not h.invoke(path=path).ok
    assert not list(outside.iterdir()) and not h.residue()


@pytest.mark.skipif(os.name != "posix", reason="requires real POSIX FIFO")
def test_fifo_target_refused_without_blocking(make):
    h = make()
    os.mkfifo(h.root / "sub/new.txt")
    assert not h.invoke().ok and not h.actions.list_actions()


@pytest.mark.parametrize("content", [b"binary", "\ud800", "é" * 9, "a" * 17])
def test_invalid_or_oversized_payload_has_no_effect(make, content):
    h = make(limit=16)
    assert not h.invoke(content=content).ok
    assert not h.actions.list_actions() and not h.residue()


@pytest.mark.parametrize("content", ["", "a" * 16, "é" * 8])
def test_exact_small_bound_and_empty_text(make, content):
    h = make(limit=16)
    assert h.invoke(content=content).ok
    assert (h.root / "sub/new.txt").read_bytes() == content.encode()


def test_maximum_worst_escaping_fits_real_protected_record_and_n_plus_one_refused(make):
    h = make()
    assert h.invoke(content="\x00" * MAX_WRITE_FILE_BYTES).ok
    assert not h.invoke(path="sub/too-large", content="\x00" * (MAX_WRITE_FILE_BYTES + 1)).ok
    assert not (h.root / "sub/too-large").exists()
    assert len(h.actions.list_actions()) == 1


@pytest.mark.parametrize("level,approve,allowed", [
    (PermissionLevel.OBSERVE, False, False), (PermissionLevel.PROPOSE, False, False),
    (PermissionLevel.CONFIRM, False, False), (PermissionLevel.CONFIRM, True, True),
    (PermissionLevel.AUTONOMOUS, False, True),
])
def test_permission_and_existing_approval_path(make, level, approve, allowed):
    h = make(level=level, approve=approve)
    result = h.invoke()
    assert result.ok is allowed
    assert (h.root / "sub/new.txt").exists() is allowed
    assert not h.residue()


def test_current_capability_block_deny(make):
    h = make()
    h.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS,
        blocked_capabilities=(str(Capability.FS_WRITE),)))
    assert not h.invoke().ok and not (h.root / "sub/new.txt").exists()


@pytest.mark.parametrize("level", [PermissionLevel.OBSERVE, PermissionLevel.PROPOSE,
                                  PermissionLevel.AUTONOMOUS])
def test_denied_write_cannot_probe_filesystem_even_on_resume(make, monkeypatch, level):
    h = make(level=level)
    h.executor.policy = PermissionPolicy(PermissionConfig(level=level,
        blocked_capabilities=(str(Capability.FS_READ), str(Capability.FS_WRITE))))
    (h.root / "sub/existing").write_bytes(b"private")
    def forbidden(*args, **kwargs):
        pytest.fail("denied write inspected the filesystem")
    monkeypatch.setattr(h.guard, "prepare_create", forbidden)
    for path in ("sub/absent", "sub/existing", "missing-parent/file"):
        for context in (None, ActionInvocationContext(action_id="act_" + "1" * 12)):
            result = h.invoke(path=path, action_context=context)
            assert not result.ok and result.result.metadata["kind"] == "permission_denied"
            assert result.result.error == "Permission was not granted for this action."
    assert not h.actions.list_actions() and not h.residue()


@pytest.mark.skipif(os.name != "nt", reason="requires real NTFS DOS short-name aliases")
@pytest.mark.parametrize("directory", [".docker", "host-sensitive-long-directory"])
def test_windows_sensitive_parent_short_name_alias_refused(make, directory):
    import win32file
    import win32con
    h = make()
    sensitive = h.root / directory
    sensitive.mkdir()
    h.guard._sensitive = h.guard.sensitive_policy.extend(directories={directory})
    handle = win32file.CreateFile(str(sensitive), win32con.GENERIC_WRITE | 0x10000, 7,
        None, win32con.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
    try:
        win32file.SetFileShortName(handle, "ACSEC~1")
    finally:
        handle.Close()
    # Prove this is a real alias to the sensitive object, not a mocked API.
    assert os.path.samefile(h.root / "ACSEC~1", sensitive)
    assert h.guard.sensitive_policy.reason("ACSEC~1/new.txt") is None
    assert not h.invoke(path="ACSEC~1/new.txt").ok
    assert not list(sensitive.iterdir()) and not h.actions.list_actions() and not h.residue()


@pytest.mark.parametrize("level", [PermissionLevel.OBSERVE, PermissionLevel.PROPOSE, PermissionLevel.CONFIRM])
def test_host_capability_override_cannot_erase_write_level_or_approval_floor(make, level):
    h = make(level=level)
    h.executor.policy = PermissionPolicy(PermissionConfig(level=level,
        capability_overrides={str(Capability.FS_WRITE): PermissionLevel.OBSERVE}))
    assert not h.invoke().ok and not (h.root / "sub/new.txt").exists()


def test_host_write_limit_loads_from_configuration(tmp_path):
    from agentcore.config.loader import load_config
    from agentcore.errors import ConfigError
    path = tmp_path / "agent.toml"
    path.write_text('[filesystem]\nmax_write_bytes = 64\n', encoding="utf-8")
    assert load_config(path).filesystem.max_write_bytes == 64
    path.write_text(f'[filesystem]\nmax_write_bytes = {MAX_WRITE_FILE_BYTES + 1}\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def _publication_hook(monkeypatch, hook):
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        original = win.publish
        def publish(handle, parent, name):
            return hook(lambda: original(handle, parent, name))
        monkeypatch.setattr(win, "publish", publish)
    else:
        original = os.link
        def link(*args, **kwargs):
            if "src_dir_fd" not in kwargs:
                return original(*args, **kwargs)
            return hook(lambda: original(*args, **kwargs))
        monkeypatch.setattr(mutation.os, "link", link)


def test_appearing_target_refused_at_native_publication_without_overwrite(make, monkeypatch):
    h = make()
    def interfere(publish):
        (h.root / "sub/new.txt").write_bytes(b"concurrent original")
        return publish()
    _publication_hook(monkeypatch, interfere)
    result = h.invoke()
    assert not result.ok
    assert (h.root / "sub/new.txt").read_bytes() == b"concurrent original"
    assert h.actions.load(result.action_id).state is ActionState.FAILED
    assert not h.residue()


@pytest.mark.parametrize("outcome", ["applied", "absent", "changed", "directory", "hardlink", "symlink"])
def test_uncertainty_and_real_read_only_reconciliation(make, monkeypatch, outcome):
    h = make()
    def uncertain(publish):
        if outcome != "absent":
            publish()
        raise TimeoutError("PRIVATE_EXCEPTION_SENTINEL")
    _publication_hook(monkeypatch, uncertain)
    result = h.invoke(content="PRIVATE_CONTENT_SENTINEL")
    assert h.actions.load(result.action_id).state is ActionState.RECONCILE_REQUIRED
    target = h.root / "sub/new.txt"
    if outcome == "changed":
        target.write_bytes(b"different")
    elif outcome == "directory":
        target.unlink()
        target.mkdir()
    elif outcome == "hardlink":
        os.link(target, h.base / "alias")
    elif outcome == "symlink":
        target.unlink()
        external = h.base / "external"
        external.write_bytes(b"PRIVATE_CONTENT_SENTINEL")
        _symlink(target, external)
    report = h.reconcile(result.action_id)
    expected = (ActionReconciliationStatus.SETTLED_SUCCEEDED if outcome == "applied"
                else ActionReconciliationStatus.UNRESOLVED)
    assert report.status is expected
    public = repr(h.logger.events) + json.dumps(result.to_dict()) + repr(h.actions.load(result.action_id))
    assert "PRIVATE_CONTENT_SENTINEL" not in public and "PRIVATE_EXCEPTION_SENTINEL" not in public
    assert str(h.root) not in public


def test_staging_failure_cleans_partial_and_reconciliation_ignores_staging(make, monkeypatch):
    h = make()
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        original = win.write
        def fail(handle, data):
            original(handle, data[:2])
            raise OSError("staging failure")
        monkeypatch.setattr(win, "write", fail)
    else:
        original = os.write
        def fail(fd, data):
            original(fd, data[:2])
            raise OSError("staging failure")
        monkeypatch.setattr(mutation.os, "write", fail)
    result = h.invoke()
    assert not result.ok and not (h.root / "sub/new.txt").exists() and not h.residue()
    (h.root / "sub/.agentcore-stage-crash-residue").write_text("héllo\n", encoding="utf-8")
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.UNRESOLVED


def test_published_then_removed_after_interruption_remains_unknown_without_retry(make, monkeypatch):
    from agentcore.action_journal import ReconciliationDisposition
    h = make()
    calls = []
    def interrupted(publish):
        calls.append(1)
        publish()
        raise TimeoutError("post-publication interruption")
    _publication_hook(monkeypatch, interrupted)
    result = h.invoke()
    assert h.actions.load(result.action_id).state is ActionState.RECONCILE_REQUIRED
    target = h.root / "sub/new.txt"
    assert target.read_bytes() == "héllo\n".encode()
    target.unlink()  # A cooperating process removes the proven published file.
    report = h.reconcile(result.action_id)
    assert report.status is ActionReconciliationStatus.UNRESOLVED
    record = h.actions.load(result.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
    h.invoke(action_context=ActionInvocationContext(action_id=result.action_id))
    assert calls == [1] and not target.exists() and len(record.attempts) == 1


def test_same_action_cannot_blindly_repeat_after_uncertainty(make, monkeypatch):
    h = make()
    calls = []
    def uncertain(publish):
        calls.append(1)
        publish()
        raise TimeoutError()
    _publication_hook(monkeypatch, uncertain)
    first = h.invoke()
    second = h.invoke(action_context=ActionInvocationContext(action_id=first.action_id))
    assert not second.ok and calls == [1]
    assert h.reconcile(first.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    h.invoke(action_context=ActionInvocationContext(action_id=first.action_id))
    assert calls == [1]


def test_direct_run_and_model_authority_arguments_fail_closed(make):
    h = make()
    args = {"path": "sub/new.txt", "content": "data"}
    assert not h.foundation.tool.run(args, ToolContext(workspace=h.guard)).ok
    for key in ("workspace", "action_id", "replace", "force", "mode", "temp_path", "pre_state"):
        assert not h.executor.invoke("write_file", {**args, key: "model"}).ok
    h.executor.workspace = WorkspaceGuard(h.base)
    result = h.invoke()
    assert not result.ok and not (h.root / "sub/new.txt").exists()


def test_cancellation_after_claim_before_physical_seam_is_not_invoked(make):
    h = make()
    def checkpoint():
        ids = h.actions.list_actions()
        if ids and h.actions.load(ids[0]).state is ActionState.IN_FLIGHT:
            raise CancellationRequested("synthetic cancellation")
    with pytest.raises(CancellationRequested):
        h.invoke(checkpoint=checkpoint)
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.state is ActionState.CANCELLED
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert not (h.root / "sub/new.txt").exists() and not h.residue()


def test_cancellation_at_guard_final_pre_staging_check_is_not_invoked(make, monkeypatch):
    h = make()
    at_guard = []
    original = h.guard.create_file
    def guard(*args):
        at_guard.append(1)
        return original(*args)
    def checkpoint():
        if at_guard:
            raise CancellationRequested("final pre-staging cancellation")
    monkeypatch.setattr(h.guard, "create_file", guard)
    with pytest.raises(CancellationRequested):
        h.invoke(checkpoint=checkpoint)
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.state is ActionState.CANCELLED
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert not (h.root / "sub/new.txt").exists() and not h.residue()


@pytest.mark.parametrize("interruption", [CancellationRequested, KeyboardInterrupt, SystemExit])
def test_interruption_after_publish_keeps_final_and_uncertain_truth(make, monkeypatch, interruption):
    h = make()
    def interrupted(publish):
        publish()
        raise interruption("synthetic interruption")
    _publication_hook(monkeypatch, interrupted)
    with pytest.raises(interruption):
        h.invoke()
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert (h.root / "sub/new.txt").read_bytes() == "héllo\n".encode()
    assert h.reconcile(record.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED


def test_permission_and_registration_leases_at_publication_hold_without_mutex_io(make, monkeypatch):
    h = make()
    entered, release = threading.Event(), threading.Event()
    results = []
    def wait(publish):
        entered.set()
        assert release.wait(15)
        return publish()
    _publication_hook(monkeypatch, wait)
    def worker():
        try:
            results.append(h.invoke())
        except BaseException as exc:
            results.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(15)
        with pytest.raises(PolicyLeaseBusyError):
            h.executor.policy = PermissionPolicy()
        with pytest.raises(ToolRegistryLeaseError):
            h.registry.unregister("write_file")
        for lock in (h.executor._policy_lock, h.registry._lock):
            assert lock.acquire(blocking=False)
            lock.release()
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and len(results) == 1 and results[0].ok


def test_attempt_and_protected_intent_are_durable_before_staging(make, monkeypatch):
    h = make()
    original = h.guard.create_file
    def check(intent, data, validate):
        record = h.actions.load(h.actions.list_actions()[0])
        assert record.state is ActionState.IN_FLIGHT and len(record.attempts) == 1
        assert record.intent_ref and record.attempts[0].status is ActionAttemptStatus.CLAIMED
        assert h.payloads.load(record.intent_ref, action_id=record.action_id).payload["content"] == "héllo\n"
        return original(intent, data, validate)
    monkeypatch.setattr(h.guard, "create_file", check)
    assert h.invoke().ok


def test_startup_pending_and_corrupt_protected_payload_fail_closed(make, monkeypatch):
    h = make()
    h.executor.quarantine_action_mutations("synthetic.corrupt")
    assert not h.invoke().ok and not (h.root / "sub/new.txt").exists()
    other = make()
    def corrupt(*args, **kwargs):
        raise ValueError("protected integrity failure")
    monkeypatch.setattr(other.payloads, "load", corrupt)
    assert not other.invoke().ok and not (other.root / "sub/new.txt").exists()
    assert not other.actions.list_actions()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX descriptor namespace semantics")
def test_excluded_hostile_ancestor_relocation_is_characterized_not_claimed_safe(make, monkeypatch):
    h = make()
    moved = h.base / "hostile-outside"
    def relocate(publish):
        os.rename(h.root / "sub", moved)
        (h.root / "sub").mkdir()
        return publish()
    _publication_hook(monkeypatch, relocate)
    # The explicit host exclusion: descriptor-relative publication follows the
    # originally authorized directory object after a hostile same-user move.
    result = h.invoke()
    assert (moved / "new.txt").read_bytes() == "héllo\n".encode()
    assert not (h.root / "sub/new.txt").exists()
    assert result.ok


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX parent substitution")
def test_detectable_parent_swap_before_publish_refused(make, monkeypatch):
    h = make()
    original = mutation._check
    calls = []
    def check(*args):
        calls.append(1)
        if len(calls) == 2:
            os.rename(h.root / "sub", h.root / "old")
            (h.root / "sub").mkdir()
        return original(*args)
    monkeypatch.setattr(mutation, "_check", check)
    result = h.invoke()
    assert not result.ok and not (h.root / "sub/new.txt").exists()
    assert not (h.root / "old/new.txt").exists()
    assert not h.residue()


def test_parent_substitution_after_preparation_never_creates_in_replacement(make, monkeypatch):
    h = make()
    original = h.foundation.prepare_intent
    def changed(*args):
        intent = original(*args)
        os.rename(h.root / "sub", h.root / "old")
        (h.root / "sub").mkdir()
        return intent
    monkeypatch.setattr(h.foundation, "prepare_intent", changed)
    assert not h.invoke().ok
    assert not (h.root / "sub/new.txt").exists() and not (h.root / "old/new.txt").exists()
    assert not h.residue()


def test_policy_change_during_protected_resolution_is_not_invoked(make, monkeypatch):
    h = make()
    original = h.payloads.load
    changed = []
    def load(*args, **kwargs):
        result = original(*args, **kwargs)
        ids = h.actions.list_actions()
        if ids and h.actions.load(ids[0]).state is ActionState.IN_FLIGHT and not changed:
            changed.append(1)
            h.executor.policy = PermissionPolicy()
        return result
    monkeypatch.setattr(h.payloads, "load", load)
    result = h.invoke()
    assert changed and not result.ok and not h.residue()
    assert h.actions.load(result.action_id).attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert not (h.root / "sub/new.txt").exists()


def test_two_cooperating_actions_cannot_overwrite_each_other(make, monkeypatch):
    h = make()
    ready = threading.Barrier(2, timeout=15)
    results = []
    def concurrent(publish):
        ready.wait()
        return publish()
    _publication_hook(monkeypatch, concurrent)
    def worker(content):
        try:
            results.append(h.invoke(content=content))
        except BaseException as exc:
            results.append(exc)
    threads = [threading.Thread(target=worker, args=(value,)) for value in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2 and not any(isinstance(result, BaseException) for result in results)
    assert sum(result.ok for result in results) == 1
    assert (h.root / "sub/new.txt").read_text() in {"first", "second"}
    assert not h.residue()


def test_reconciliation_needs_only_current_fs_read(make, monkeypatch):
    h = make()
    def uncertain(publish):
        publish()
        raise TimeoutError()
    _publication_hook(monkeypatch, uncertain)
    result = h.invoke()
    h.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.OBSERVE,
        blocked_capabilities=(str(Capability.FS_WRITE),)))
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED


def test_owned_handles_are_released_after_success_and_stage_failure(make, monkeypatch):
    h = make()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        get_count = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessHandleCount
        get_count.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        def count():
            value = wintypes.DWORD()
            assert get_count(wintypes.HANDLE(-1), ctypes.byref(value))
            return value.value
    elif Path("/proc/self/fd").exists():
        def count():
            return len(os.listdir("/proc/self/fd"))
    else:
        pytest.skip("descriptor inventory unavailable")
    # Warm lazy imports, logging, and native API bindings before measuring.
    assert h.invoke(path="sub/warm").ok
    before = count()
    for index in range(8):
        assert h.invoke(path=f"sub/file-{index}").ok
    assert count() == before and not h.residue()
    def fail(publish):
        raise OSError("synthetic pre-publication failure")
    _publication_hook(monkeypatch, fail)
    assert not h.invoke(path="sub/failed").ok
    assert count() == before


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX hard-link staging publication")
def test_staging_alias_after_cleanup_failure_is_unknown_not_success(make, monkeypatch):
    h = make()
    original = os.unlink
    def fail(path, *args, **kwargs):
        if str(path).startswith(".agentcore-stage-"):
            raise OSError("owned staging cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(mutation.os, "unlink", fail)
    result = h.invoke()
    assert not result.ok and h.residue()
    assert (h.root / "sub/new.txt").stat().st_nlink == 2
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.UNRESOLVED
