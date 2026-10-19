"""Stage publication admission, with native second-principal proof when available."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agentcore.action_journal import ActionAttemptStatus, EffectVerdict
from agentcore.fs import mutation
from test_fs_write import make  # noqa: F401 - real durable executor fixture


linux = pytest.mark.skipif(sys.platform != "linux", reason="requires native Linux directory DAC / ACL semantics")


@linux
@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755, 0o770, 0o775, 0o777, 0o1777])
@pytest.mark.parametrize("after_prepare", [False, True])
def test_posix_refused_regardless_of_parent_mode(make, monkeypatch, mode, after_prepare):
    h = make()
    if after_prepare:
        original = h.foundation.prepare_intent
        def prepare(*args):
            intent = original(*args)
            os.chmod(h.root / "sub", mode)
            return intent
        monkeypatch.setattr(h.foundation, "prepare_intent", prepare)
    else:
        os.chmod(h.root / "sub", mode)
    result = h.invoke()
    assert not result.ok and not (h.root / "sub/new.txt").exists() and not h.residue()
    record = h.actions.load(result.action_id)
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED


@linux
def test_directory_create_does_not_inherit_file_publication_support(make, monkeypatch):
    from agentcore.tools.builtin.create_directory import FilesystemDirectoryFoundation
    h = make()
    foundation = FilesystemDirectoryFoundation(h.guard)
    foundation.register(h.registry, h.executor._action_adapters)
    os.chmod(h.root / "sub", 0o777)
    def forbidden():
        pytest.fail("Directory Create called file-publication admission")
    monkeypatch.setattr(mutation, "_require_file_publication", forbidden)
    assert h.executor.invoke("create_directory", {"path": "sub/new"}).ok
    assert (h.root / "sub/new").is_dir() and not h.residue()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handle publication")
def test_windows_publication_keeps_verified_owned_handle(make, monkeypatch):
    from agentcore.fs import _windows_mutation as win
    h = make()
    original = win.publish
    calls = []
    def publish(owned, parent, name):
        before = win.facts(owned)
        assert before[2] == len("héllo\n".encode())
        stage = h.root / "sub" / win.final_name(owned)
        with pytest.raises(PermissionError):
            stage.rename(stage.with_name("substitute"))
        result = original(owned, parent, name)
        assert win.facts(owned)[1] == before[1]
        assert win.final_name(owned) == name
        calls.append(1)
        return result
    monkeypatch.setattr(win, "publish", publish)
    assert h.invoke().ok and calls == [1] and not h.residue()
    assert (h.root / "sub/new.txt").read_bytes() == "héllo\n".encode()


def test_worker_loads_runtime_before_dropping_access():
    worker = Path(__file__).with_name("fs_stage_principal_worker.py")
    script = """
import os
import runpy
import sys
import types

# Exercise import ordering on Windows too, without changing any credentials.
if os.name == "nt":
    sys.modules["pwd"] = types.ModuleType("pwd")
    sys.modules["fcntl"] = types.ModuleType("fcntl")
worker = runpy.run_path(sys.argv[1])
def dropped(uid):
    assert callable(worker["agent"].__globals__.get("Harness"))
    assert all(name in sys.modules for name in
               ("ctypes", "platform", "fcntl", "pytest", "subprocess", "agentcore.fs.mutation"))
    raise SystemExit(0)  # Stop before any guard, journal or filesystem construction.
worker["agent"].__globals__["drop"] = dropped
worker["agent"](None, None, 60000, 60001, "safe")
"""
    result = subprocess.run([sys.executable, "-c", script, str(worker)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@linux
@pytest.mark.parametrize("private_runtime", [False, True], ids=["checkout", "private-runtime"])
@pytest.mark.parametrize("scenario", ["shared", "safe", "acl-masked", "foreign", "ancestor"])
def test_native_linux_different_ordinary_principal(scenario, private_runtime, tmp_path):
    """sudo/root only sets up UIDs; AgentCore and attacker both drop privileges.

    No users/accounts are created. The helper selects two unused nonzero UIDs,
    clears supplementary groups and proves real/effective identities. Its
    socket rendezvous probes real namespace authority. The retained link seam
    must never be reached on an unsupported POSIX host.
    """
    worker = Path(__file__).with_name("fs_stage_principal_worker.py")
    prefix = []
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if sudo is None:
            pytest.skip("two-UID setup unavailable: neither root nor sudo")
        probe = subprocess.run([sudo, "-n", sys.executable, "-c", "import os; assert os.geteuid() == 0"],
                               capture_output=True, timeout=15)
        if probe.returncode != 0:
            pytest.skip("two-UID setup unavailable: noninteractive sudo cannot run the Python interpreter")
        prefix = [sudo, "-n"]
    if private_runtime:
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        os.chmod(private, 0o700)
        shutil.copytree(worker.parents[1] / "src", private / "src",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (private / "tests").mkdir()
        for name in (worker.name, "test_fs_write.py", "test_task_43_2b_tool_executor_action_boundary.py"):
            shutil.copy2(worker.with_name(name), private / "tests" / name)
        worker = private / "tests" / worker.name
    command = [*prefix, sys.executable, str(worker), scenario]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result.stdout)
    if private_runtime:
        assert not facts["runtime_accessible"], "runtime ancestor remained searchable after dropping UID"
    assert facts["agent_uid"] != facts["attacker_uid"]
    assert facts["agent_uid"] > 0 and facts["attacker_uid"] > 0
    assert facts["agent_groups"] == [] and facts["attacker_groups"] == []
    assert facts["parent_uid"] == (facts["attacker_uid"] if scenario == "foreign" else facts["agent_uid"])
    assert facts["parent_mode"] == (0o777 if scenario == "shared" else 0o755)
    if scenario == "shared":
        assert facts["namespace_probe_substituted"]
    if scenario == "ancestor":
        assert facts["ancestor_probe_relocated"]
    assert not facts["attack_reached"] and not facts["ok"]
    assert facts["bytes"] is None and facts["entries"] == []
    assert facts["attempt"] == "not_invoked"
    assert facts["verdict"] == "confirmed_not_applied"
