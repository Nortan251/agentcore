"""Real mkdir effects, authority seams and deliberately ambiguous recovery."""

from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

import pytest

from agentcore.action_journal import (
    ActionAttemptStatus, ActionInvocationContext, ActionReconciliationStatus,
    ActionState, EffectVerdict, ReconciliationDisposition,
)
from agentcore.errors import PolicyLeaseBusyError
from agentcore.fs import mutation
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import EchoTool, default_tools
from agentcore.tools.builtin.create_directory import FilesystemDirectoryFoundation
from agentcore.tools.registry import ToolRegistryLeaseError
from test_fs_write import Harness as WriteHarness, _symlink


class Harness(WriteHarness):
    def __init__(self, base, **kwargs):
        super().__init__(base, **kwargs)
        self.write_foundation = self.foundation
        self.foundation = FilesystemDirectoryFoundation(self.guard)
        self.foundation.register(self.registry, self.executor._action_adapters)

    def invoke(self, path="sub/new", **kwargs):
        return self.executor.invoke("create_directory", {"path": path}, **kwargs)


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


def mkdir_hook(monkeypatch, hook):
    """Pause exactly inside the native create entry, after final authority checks."""
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        original = win.open_child
        def open_child(*args, **kwargs):
            if kwargs.get("create") and kwargs.get("directory"):
                created = []
                def create():
                    handle = original(*args, **kwargs)
                    created.append(handle)
                    return handle
                try:
                    return hook(create)
                except BaseException:
                    # The hook simulates lost return/crashed execution. Windows
                    # process teardown closes this otherwise unreturned handle.
                    for handle in created:
                        win.close(handle)
                    raise
            return original(*args, **kwargs)
        monkeypatch.setattr(win, "open_child", open_child)
    else:
        original = os.mkdir
        def mkdir(*args, **kwargs):
            if "dir_fd" in kwargs:
                return hook(lambda: original(*args, **kwargs))
            return original(*args, **kwargs)
        monkeypatch.setattr(mutation.os, "mkdir", mkdir)


def test_real_empty_directory_and_file_workflow(make):
    h = make()
    result = h.invoke()
    assert result.ok and (h.root / "sub/new").is_dir()
    assert list((h.root / "sub/new").iterdir()) == []
    record = h.actions.load(result.action_id)
    assert record.state is ActionState.SUCCEEDED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert len(record.attempts) == 1 and record.attempts[0].status is ActionAttemptStatus.INVOKED
    assert record.action_kind == "filesystem.create_directory.v1"
    assert h.foundation.tool.capabilities == frozenset({Capability.FS_WRITE})
    assert not h.foundation.tool.spec.read_only
    assert not {"write_file", "create_directory"} & {t.name for t in default_tools()}
    if os.name == "posix":
        assert (h.root / "sub/new").stat().st_mode & 0o777 == 0o700
    assert h.executor.invoke("write_file", {"path": "sub/new/note.txt", "content": "summary"}).ok
    assert (h.root / "sub/new/note.txt").read_text() == "summary"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling"])
def test_existing_target_refused_without_changes(make, kind):
    h = make()
    target = h.root / "sub/new"
    if kind == "directory":
        target.mkdir()
        (target / "keep").write_bytes(b"unchanged")
    elif kind == "file":
        target.write_bytes(b"unchanged")
    else:
        _symlink(target, h.base / ("workspace/sub" if kind == "symlink" else "absent"), True)
    result = h.invoke()
    assert not result.ok and result.result.metadata["effect_verdict"] == "confirmed_not_applied"
    assert not h.actions.list_actions()
    if kind == "directory":
        assert (target / "keep").read_bytes() == b"unchanged"
    elif kind == "file":
        assert target.read_bytes() == b"unchanged"
    assert not h.residue()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_native_exclusive_create_race_is_confirmed_not_applied(make, monkeypatch, kind):
    h = make()
    target = h.root / "sub/new"
    def race(create):
        if kind == "directory":
            target.mkdir()
        elif kind == "file":
            target.write_bytes(b"concurrent")
        else:
            _symlink(target, h.base / "workspace/sub", True)
        return create()
    mkdir_hook(monkeypatch, race)
    result = h.invoke()
    record = h.actions.load(result.action_id)
    assert not result.ok and record.state is ActionState.FAILED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
    if kind == "file":
        assert target.read_bytes() == b"concurrent"


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/absolute", "sub/../../escape",
    ".env", ".git/new", "sub/id_rsa", "sub/.env.local", "", ".", "sub\\new"])
def test_unsafe_paths_refused_before_attempt(make, path):
    h = make()
    assert not h.invoke(path).ok and not h.actions.list_actions()
    assert list((h.root / "sub").iterdir()) == []


@pytest.mark.parametrize("path", ["a/b/c", "missing/child"])
def test_missing_parents_never_created_recursively(make, path):
    h = make()
    assert not h.invoke(path).ok
    assert not (h.root / path.split("/")[0]).exists() and not h.actions.list_actions()


def test_parent_symlink_never_followed(make):
    h = make()
    outside = h.base / "outside"
    outside.mkdir()
    _symlink(h.root / "link", outside, True)
    assert not h.invoke("link/new").ok
    assert list(outside.iterdir()) == [] and not h.actions.list_actions()


@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_non_directory_parent_refused(make, kind):
    if kind == "fifo" and os.name != "posix":
        pytest.skip("POSIX FIFO parent")
    h = make()
    parent = h.root / "unsafe"
    if kind == "fifo":
        os.mkfifo(parent)
    else:
        parent.write_bytes(b"unchanged")
    assert not h.invoke("unsafe/new").ok and not h.actions.list_actions()


@pytest.mark.skipif(os.name != "nt", reason="real Windows junction and NT device names")
@pytest.mark.parametrize("leaf", [False, True])
def test_windows_junction_and_reserved_spelling_refused(make, leaf):
    h = make()
    outside = h.base / "outside"
    outside.mkdir()
    target = h.root / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                            capture_output=True, timeout=10)
    assert result.returncode == 0 and target.lstat().st_file_attributes & 0x400
    assert not h.invoke("junction" if leaf else "junction/new").ok
    for path in ("CON", "sub/NUL.txt", "sub/new:stream", "sub/trailing. ", "sub/COM¹"):
        assert not h.invoke(path).ok
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="real NTFS sensitive-parent short-name alias")
def test_windows_directory_sensitive_parent_alias_refused(make):
    from test_fs_write import test_windows_sensitive_parent_short_name_alias_refused
    test_windows_sensitive_parent_short_name_alias_refused(make, "host-sensitive-long-directory")


def test_parent_identity_pinned_and_substitution_refused(make, monkeypatch):
    h = make()
    original = h.foundation.prepare_intent
    def replace_parent(*args):
        prepared = original(*args)
        assert len(prepared.protected_payload["directories"]) == 2
        os.rename(h.root / "sub", h.root / "old")
        (h.root / "sub").mkdir()
        return prepared
    monkeypatch.setattr(h.foundation, "prepare_intent", replace_parent)
    result = h.invoke()
    assert not result.ok
    assert not (h.root / "sub/new").exists() and not (h.root / "old/new").exists()
    record = h.actions.load(result.action_id)
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


@pytest.mark.parametrize("level,approve,allowed", [
    (PermissionLevel.OBSERVE, False, False), (PermissionLevel.PROPOSE, False, False),
    (PermissionLevel.CONFIRM, False, False), (PermissionLevel.CONFIRM, True, True),
    (PermissionLevel.AUTONOMOUS, False, True)])
def test_current_permission_and_durable_approval_floor(make, level, approve, allowed):
    h = make(level=level, approve=approve)
    # A host override cannot lower the intrinsic mutation/approval floor.
    h.executor.policy = PermissionPolicy(PermissionConfig(level=level,
        capability_overrides={str(Capability.FS_WRITE): PermissionLevel.OBSERVE}))
    assert h.invoke().ok is allowed
    assert (h.root / "sub/new").exists() is allowed


@pytest.mark.parametrize("capability", [Capability.FS_READ, Capability.FS_WRITE])
def test_denial_before_preparation_and_reconciliation(make, monkeypatch, capability):
    h = make(level=PermissionLevel.CONFIRM, approve=True)
    h.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.CONFIRM,
        blocked_capabilities=(str(capability),)))
    monkeypatch.setattr(h.guard, "prepare_create", lambda *a: pytest.fail("denied observation"))
    assert h.invoke().result.metadata["kind"] == "permission_denied"
    assert not h.actions.list_actions()


def test_attempt_and_protected_intent_exist_before_native_mkdir(make, monkeypatch):
    h = make()
    def check(create):
        record = h.actions.load(h.actions.list_actions()[0])
        assert record.state is ActionState.IN_FLIGHT and record.intent_ref
        assert record.attempts[0].status is ActionAttemptStatus.CLAIMED
        payload = h.payloads.load(record.intent_ref, action_id=record.action_id).payload
        assert set(payload) == {"schema", "tool", "version", "mode", "workspace", "path", "directories", "pre_state"}
        assert payload["mode"] == "create_directory" and payload["pre_state"] == "absent"
        assert payload["path"] == "sub/new"
        return create()
    mkdir_hook(monkeypatch, check)
    assert h.invoke().ok


def test_direct_call_and_model_semantics_cannot_mutate(make):
    h = make()
    assert not h.foundation.tool.run({"path": "sub/new"}, ToolContext(workspace=h.guard)).ok
    for key in ("parents", "exist_ok", "mode", "permissions", "owner", "force", "replace", "action_id",
                "effect_class", "workspace", "reconciliation_strategy"):
        assert not h.executor.invoke("create_directory", {"path": "sub/new", key: True}).ok
    assert not (h.root / "sub/new").exists() and not h.actions.list_actions()


@pytest.mark.parametrize("source", ["write_file", "create_directory"])
def test_exact_context_cannot_be_consumed_by_other_tool_or_twice(make, monkeypatch, source):
    h = make()
    origin = h.registry.get(source)
    other = h.registry.get("create_directory" if source == "write_file" else "write_file")
    original = origin.run
    captured = []
    def intercept(args, context):
        execution = context.filesystem_mutation_execution
        captured.append(execution)
        # Even manually placing the context in both compatibility fields cannot
        # cross the exact tool object binding.
        both = replace(context, filesystem_write_execution=execution)
        other_args = {"path": "sub/cross"} if source == "write_file" else {"path": "sub/cross", "content": "x"}
        assert not other.run(other_args, both).ok
        assert not execution.used
        result = original(args, context)
        assert not original(args, context).ok
        return result
    monkeypatch.setattr(origin, "run", intercept)
    result = (h.invoke() if source == "create_directory" else
              h.executor.invoke("write_file", {"path": "sub/file", "content": "x"}))
    assert result.ok and captured[0].used and not captured[0].active
    assert not (h.root / "sub/cross").exists()


@pytest.mark.parametrize("interruption", [RuntimeError, CancellationRequested, KeyboardInterrupt, SystemExit])
def test_pre_native_interruption_has_no_effect(make, monkeypatch, interruption):
    h = make()
    def fail(*args):
        raise interruption("PRIVATE_EXCEPTION")
    monkeypatch.setattr(h.guard, "create_directory", fail)
    if interruption is RuntimeError:
        assert not h.invoke().ok
    else:
        with pytest.raises(interruption):
            h.invoke()
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert not (h.root / "sub/new").exists()


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("interruption", [TimeoutError, CancellationRequested, KeyboardInterrupt, SystemExit])
def test_possible_mkdir_is_uncertain_with_no_blind_retry(make, monkeypatch, published, interruption):
    h = make()
    attempts = []
    def interrupted(create):
        attempts.append(1)
        if published:
            create()
        raise interruption("PRIVATE_EXCEPTION")
    mkdir_hook(monkeypatch, interrupted)
    if interruption is TimeoutError:
        result = h.invoke()
        assert not result.ok
    else:
        with pytest.raises(interruption):
            h.invoke()
    action_id = h.actions.list_actions()[0]
    record = h.actions.load(action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert h.reconcile(action_id).status is ActionReconciliationStatus.UNRESOLVED
    record = h.actions.load(action_id)
    assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
    assert not h.invoke(action_context=ActionInvocationContext(action_id=action_id)).ok
    assert attempts == [1] and len(record.attempts) == 1
    assert (h.root / "sub/new").exists() is published
    public = repr(h.logger.events) + json.dumps(record.to_dict())
    assert "PRIVATE_EXCEPTION" not in public and str(h.root) not in public and "sub/new" not in public


def test_created_then_removed_cannot_prove_not_applied(make, monkeypatch):
    h = make()
    def interrupted(create):
        create()
        raise TimeoutError()
    mkdir_hook(monkeypatch, interrupted)
    result = h.invoke()
    (h.root / "sub/new").rmdir()
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.UNRESOLVED
    assert h.actions.load(result.action_id).reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED


def test_post_create_verification_failure_preserves_unknown_and_directory(make, monkeypatch):
    h = make()
    original = mutation._check
    calls = []
    def fail_after_create(*args):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("post-create failure")
        return original(*args)
    monkeypatch.setattr(mutation, "_check", fail_after_create)
    result = h.invoke()
    assert h.actions.load(result.action_id).state is ActionState.RECONCILE_REQUIRED
    assert (h.root / "sub/new").is_dir() and list((h.root / "sub/new").iterdir()) == []


def test_reconciliation_requires_current_fs_read_even_without_probe_io(make, monkeypatch):
    h = make()
    mkdir_hook(monkeypatch, lambda create: (_ for _ in ()).throw(TimeoutError()))
    result = h.invoke()
    h.executor.policy = PermissionPolicy(PermissionConfig(blocked_capabilities=(str(Capability.FS_READ),)))
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.PROBE_DENIED
    h.executor.policy = PermissionPolicy(PermissionConfig(blocked_capabilities=(str(Capability.FS_WRITE),)))
    assert h.reconcile(result.action_id).status is ActionReconciliationStatus.UNRESOLVED


def test_quarantine_waits_for_current_mkdir_and_denies_new_admission(make, monkeypatch):
    h = make()
    entered, release, waiting, completed = (threading.Event() for _ in range(4))
    results, errors = [], []
    def pause(create):
        entered.set()
        assert release.wait(15)
        return create()
    mkdir_hook(monkeypatch, pause)
    original_wait = h.executor._action_domain_health_condition.wait
    def wait(*args, **kwargs):
        waiting.set()
        return original_wait(*args, **kwargs)
    monkeypatch.setattr(h.executor._action_domain_health_condition, "wait", wait)
    def worker():
        try:
            results.append(h.invoke())
        except BaseException as exc:
            errors.append(exc)
    def quarantine():
        try:
            h.executor.quarantine_action_mutations("journal_corrupt")
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()
    thread = threading.Thread(target=worker)
    quarantiner = threading.Thread(target=quarantine)
    thread.start()
    try:
        assert entered.wait(15)
        quarantiner.start()
        assert waiting.wait(15) and not completed.is_set()
        assert not h.executor._mark_action_startup_healthy(h.actions)
        assert not h.invoke("sub/later").ok
    finally:
        release.set()
        thread.join(15)
        if quarantiner.ident is not None:
            quarantiner.join(15)
    assert not thread.is_alive() and not quarantiner.is_alive() and not errors
    assert len(results) == 1 and results[0].ok and completed.is_set()
    assert not (h.root / "sub/later").exists()


@pytest.mark.parametrize("seam", ["prepare", "mkdir", "reconcile"])
def test_policy_registry_leases_cover_seams_without_metadata_mutex(make, monkeypatch, seam):
    h = make()
    entered, release = threading.Event(), threading.Event()
    results = []
    def pause(call):
        entered.set()
        assert release.wait(15)
        return call()
    if seam == "prepare":
        original = h.guard.prepare_create
        monkeypatch.setattr(h.guard, "prepare_create", lambda *a: pause(lambda: original(*a)))
        operation = h.invoke
    elif seam == "mkdir":
        mkdir_hook(monkeypatch, pause)
        operation = h.invoke
    else:
        mkdir_hook(monkeypatch, lambda create: (_ for _ in ()).throw(TimeoutError()))
        result = h.invoke()
        original = h.foundation.probe
        monkeypatch.setattr(h.foundation, "probe", lambda *a: pause(lambda: original(*a)))
        operation = lambda: h.reconcile(result.action_id)
    def worker():
        try:
            results.append(operation())
        except BaseException as exc:
            results.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(15)
        with pytest.raises(PolicyLeaseBusyError):
            h.executor.policy = PermissionPolicy()
        with pytest.raises(ToolRegistryLeaseError):
            h.registry.unregister("create_directory")
        h.registry.register(EchoTool())  # Unrelated registrations remain independent.
        for lock in (h.executor._policy_lock, h.registry._lock, h.executor._action_domain_health_lock):
            assert lock.acquire(blocking=False)
            lock.release()
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and len(results) == 1 and not isinstance(results[0], BaseException)
    assert results[0].status is ActionReconciliationStatus.UNRESOLVED if seam == "reconcile" else results[0].ok


def test_owned_handles_released_after_success_and_postcheck_failure(make, monkeypatch):
    if os.environ.get("_AGENTCORE_MKDIR_HANDLE_PROBE") != "1":
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix=".pytest_mkdir_handles_", dir=repository) as probe:
            result = subprocess.run([sys.executable, "-m", "pytest", "-o", "addopts=", "-q",
                str(Path(__file__).resolve()) + "::test_owned_handles_released_after_success_and_postcheck_failure",
                "--basetemp", str(Path(probe) / "pytest")], cwd=repository,
                env={**os.environ, "_AGENTCORE_MKDIR_HANDLE_PROBE": "1"},
                capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
        return
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
    assert h.invoke("sub/warm").ok
    gc.collect()
    before = count()
    for index in range(8):
        assert h.invoke(f"sub/dir-{index}").ok
    assert count() == before
    original = mutation._check
    checks = []
    def postcheck(*args):
        checks.append(1)
        if len(checks) == 2:
            raise OSError("verification failed")
        return original(*args)
    monkeypatch.setattr(mutation, "_check", postcheck)
    assert not h.invoke("sub/uncertain").ok
    assert count() == before


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork authority fence")
def test_forked_child_cannot_reuse_live_context(make, monkeypatch):
    h = make()
    original = h.foundation.tool.run
    def at_context(args, context):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                result = original({"path": "sub/child"}, context)
                os.write(write_fd, b"safe" if not result.ok else b"unsafe")
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            import select
            if not select.select([read_fd], [], [], 15)[0]:
                import signal
                os.kill(pid, signal.SIGKILL)
                pytest.fail("fork context check did not finish")
            assert os.read(read_fd, 16) == b"safe"
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)
        return original(args, context)
    monkeypatch.setattr(h.foundation.tool, "run", at_context)
    assert h.invoke().ok and not (h.root / "sub/child").exists()
