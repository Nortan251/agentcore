"""Stage publication admission, with native second-principal proof when available."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentcore.action_journal import ActionAttemptStatus, EffectVerdict
from agentcore.fs import mutation
from test_fs_write import make  # noqa: F401 - real durable executor fixture


linux = pytest.mark.skipif(sys.platform != "linux", reason="requires native Linux directory DAC / ACL semantics")


@linux
@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755])
def test_safe_parent_preserves_bytes_and_effect_truth(make, mode):
    h = make()
    os.chmod(h.root / "sub", mode)
    assert (h.root / "sub").stat().st_uid == os.geteuid()
    result = h.invoke()
    assert result.ok and (h.root / "sub/new.txt").read_bytes() == "héllo\n".encode()
    record = h.actions.load(result.action_id)
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert record.attempts[-1].status is ActionAttemptStatus.INVOKED
    assert not h.residue()


@linux
@pytest.mark.parametrize("mode", [0o770, 0o775, 0o777, 0o1777])
@pytest.mark.parametrize("after_prepare", [False, True])
def test_shared_parent_refused_before_stage_or_physical_entry(make, monkeypatch, mode, after_prepare):
    h = make()
    if after_prepare:
        original_prepare = h.foundation.prepare_intent
        def prepare(*args):
            intent = original_prepare(*args)
            os.chmod(h.root / "sub", mode)
            return intent
        monkeypatch.setattr(h.foundation, "prepare_intent", prepare)
    else:
        os.chmod(h.root / "sub", mode)
    original_open = os.open
    def no_stage(path, *args, **kwargs):
        assert not str(path).startswith(".agentcore-stage-"), "unsafe admission reached physical staging"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(mutation.os, "open", no_stage)
    result = h.invoke()
    assert not result.ok and not (h.root / "sub/new.txt").exists() and not h.residue()
    record = h.actions.load(result.action_id)
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED


@linux
def test_authoritative_check_consumes_the_opened_parent(make, monkeypatch):
    h = make()
    original = mutation._safe_stage_parent
    original_open = os.open
    checked = []
    def check(fd):
        checked.append((fd, os.fstat(fd).st_ino))
        return original(fd)
    def opened(path, *args, **kwargs):
        if str(path).startswith(".agentcore-stage-"):
            assert checked[-1] == (kwargs["dir_fd"], os.fstat(kwargs["dir_fd"]).st_ino)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(mutation, "_safe_stage_parent", check)
    monkeypatch.setattr(mutation.os, "open", opened)
    assert h.invoke().ok and len(checked) == 1


@linux
@pytest.mark.parametrize("filesystem", [0x6969, 0xFF534D42, 0x65735546, 0x794C7630, -1])
def test_unproven_filesystem_refused_before_stage(make, monkeypatch, filesystem):
    h = make()
    monkeypatch.setattr(mutation, "_linux_parent_filesystem", lambda fd: filesystem)
    result = h.invoke()
    assert not result.ok and not h.residue() and not (h.root / "sub/new.txt").exists()
    assert h.actions.load(result.action_id).attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


@linux
@pytest.mark.parametrize("failure", ["missing_api", "native_error", "unknown_abi"])
def test_parent_inspection_unavailable_fails_closed(make, monkeypatch, failure):
    import ctypes
    import platform
    h = make()
    if failure == "unknown_abi":
        monkeypatch.setattr(platform, "machine", lambda: "unproven")
    elif failure == "missing_api":
        monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace())
    else:
        def failed(fd, buffer):
            return -1
        monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(fstatfs=failed))
    result = h.invoke()
    assert not result.ok and not h.residue() and not (h.root / "sub/new.txt").exists()
    assert h.actions.load(result.action_id).attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


@linux
def test_directory_create_does_not_inherit_stage_admission(make, monkeypatch):
    from agentcore.tools.builtin.create_directory import FilesystemDirectoryFoundation
    h = make()
    foundation = FilesystemDirectoryFoundation(h.guard)
    foundation.register(h.registry, h.executor._action_adapters)
    os.chmod(h.root / "sub", 0o777)
    def forbidden(fd):
        pytest.fail("Directory Create called file-stage admission")
    monkeypatch.setattr(mutation, "_safe_stage_parent", forbidden)
    assert h.executor.invoke("create_directory", {"path": "sub/new"}).ok
    assert (h.root / "sub/new").is_dir() and not h.residue()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handle publication")
def test_windows_publication_does_not_call_posix_admission(make, monkeypatch):
    from agentcore.fs import _windows_mutation as win
    h = make()
    original = win.publish
    calls = []
    def forbidden(fd):
        pytest.fail("Windows called POSIX stage admission")
    def publish(owned, parent, name):
        calls.append((owned, parent, name))
        return original(owned, parent, name)
    monkeypatch.setattr(mutation, "_safe_stage_parent", forbidden)
    monkeypatch.setattr(win, "publish", publish)
    assert h.invoke().ok and len(calls) == 1 and not h.residue()
    assert (h.root / "sub/new.txt").read_bytes() == "héllo\n".encode()


@linux
@pytest.mark.parametrize("scenario", ["shared", "safe", "acl-masked", "foreign"])
def test_native_linux_different_ordinary_principal(scenario):
    """sudo/root only sets up UIDs; AgentCore and attacker both drop privileges.

    No users/accounts are created. The helper selects two unused nonzero UIDs,
    clears supplementary groups and proves real/effective identities. Its
    socket rendezvous keeps the attack immediately inside os.link, after the
    owned/named inode check, even on the vulnerable base.
    """
    worker = Path(__file__).with_name("fs_stage_principal_worker.py")
    command = [sys.executable, str(worker), scenario]
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if sudo is None:
            pytest.skip("two-UID setup unavailable: neither root nor sudo")
        probe = subprocess.run([sudo, "-n", sys.executable, "-c", "import os; assert os.geteuid() == 0"],
                               capture_output=True, timeout=15)
        if probe.returncode != 0:
            pytest.skip("two-UID setup unavailable: noninteractive sudo cannot run the Python interpreter")
        command = [sudo, "-n", *command]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    assert facts["agent_uid"] != facts["attacker_uid"]
    assert facts["agent_uid"] > 0 and facts["attacker_uid"] > 0
    assert facts["agent_groups"] == [] and facts["attacker_groups"] == []
    assert facts["parent_uid"] == (facts["attacker_uid"] if scenario == "foreign" else facts["agent_uid"])
    assert facts["parent_mode"] == (0o777 if scenario == "shared" else 0o755)
    if scenario in {"safe", "acl-masked"}:
        assert facts["attack_reached"] and facts["attack_denied"]
        assert facts["ok"] and facts["bytes"] == "intended"
        assert facts["verdict"] == "confirmed_applied"
        assert facts["entries"] == ["new.txt"]
    else:
        if scenario == "shared":
            assert facts["namespace_probe_substituted"]
        assert not facts["attack_reached"] and not facts["ok"]
        assert facts["bytes"] is None and facts["entries"] == []
        assert facts["attempt"] == "not_invoked"
        assert facts["verdict"] == "confirmed_not_applied"
