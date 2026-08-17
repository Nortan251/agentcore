"""Tool-level tests for the ``inspect`` builtin (Task 41.2).

The tool owns no security logic — every filesystem decision belongs to
``WorkspaceGuard.inspect_path``. These tests prove that boundary: the tool
must reject the same paths the guard rejects, expose only the safe whitelist,
and leak nothing (paths, targets, content) into logs or persisted state.
"""

from __future__ import annotations

import ast
import inspect as _inspect_module
import json
import logging
import os
import textwrap

import pytest

from agentcore.config.schema import LoggingConfig, PermissionConfig
from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.state.models import AgentState
from agentcore.state.store import JsonFileStateStore
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from agentcore.tools.builtin.inspect import InspectTool
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


def ctx(ws, **config):
    return ToolContext(workspace=WorkspaceGuard(ws, config=FilesystemConfig(
        **config)))


def executor_for(ws, **config):
    registry = ToolRegistry()
    registry.register_all(default_tools())
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    return ToolExecutor(registry, policy, workspace=WorkspaceGuard(
        ws, config=FilesystemConfig(**config)))


# ===========================================================================
# Basic behavior + output/metadata
# ===========================================================================

def test_inspect_regular_file_via_tool(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_bytes(b"hello world\n")
    r = InspectTool().run({"path": "notes.txt"}, ctx(ws))
    assert r.ok is True
    assert "regular" in r.output
    assert "12 bytes" in r.output
    assert "modified" in r.output
    # no content, ever
    assert "hello world" not in r.output
    assert r.metadata["path"] == "notes.txt"
    assert r.metadata["kind"] == "regular"
    assert r.metadata["size"] == 12
    assert "device" not in r.metadata and "inode" not in r.metadata


def test_inspect_root_via_tool(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = InspectTool().run({"path": "."}, ctx(ws))
    assert r.ok is True
    assert r.metadata["kind"] == "directory"
    assert r.metadata["path"] == "."


def test_inspect_symlink_renders_categorically(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.txt").write_text("x", encoding="utf-8")
    (ws / "alias").symlink_to(ws / "real.txt")
    r = InspectTool().run({"path": "alias"}, ctx(ws))
    assert r.ok is True
    assert "symlink" in r.output and "internal" in r.output
    assert r.metadata["link_target"] == "internal"
    assert "real.txt" not in json.dumps(r.to_dict())


def test_inspect_missing_is_a_failure(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = InspectTool().run({"path": "nope.txt"}, ctx(ws))
    assert r.ok is False
    assert r.metadata["reason"] == "not_found"
    assert r.metadata["kind"] == "path_rejected"


def test_inspect_no_workspace_is_a_failure():
    r = InspectTool().run({"path": "x"}, ToolContext())
    assert r.ok is False
    assert r.metadata["kind"] == "no_workspace"


# ===========================================================================
# Registry / executor / permissions
# ===========================================================================

def test_inspect_is_registered_and_read_only(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = ToolRegistry()
    reg.register_all(default_tools())
    assert "inspect" in reg.names()
    spec = reg.get("inspect").spec
    assert spec.read_only is True
    from agentcore.permissions.levels import Capability
    assert Capability.FS_READ in spec.capabilities


def test_inspect_runs_at_observe_level(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hi", encoding="utf-8")
    reg = ToolRegistry()
    reg.register_all(default_tools())
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.OBSERVE))
    ex = ToolExecutor(reg, policy, workspace=WorkspaceGuard(ws))
    inv = ex.invoke("inspect", {"path": "notes.txt"})
    assert inv.ok is True


@pytest.mark.parametrize("bad", [
    {},                                             # missing path
    {"path": 3},                                    # wrong type
    {"path": "x", "recursive": True},               # unknown arg
    {"path": "x", "base_dir": "y"},
    {"path": "x", "workspace_dir": "y"},
    {"path": "x", "follow": True},
    {"path": "x", "target": "z"},
    {"path": "x", "depth": 3},
])
def test_malformed_args_never_reach_the_tool(tmp_path, bad):
    ws = tmp_path / "ws"
    ws.mkdir()
    ex = executor_for(ws)
    inv = ex.invoke("inspect", bad)
    assert inv.ok is False
    assert inv.result.metadata["kind"] == "validation"


# ===========================================================================
# Privacy: logs and persisted state (real machinery, no mocks)
# ===========================================================================

def test_sensitive_path_argument_is_not_logged(tmp_path):
    logfile = tmp_path / "agent.log"
    configure_logging(LoggingConfig(level="INFO", format="json",
                                    file_path=str(logfile)))
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".env").write_text("API_KEY=sk-inspect-log", encoding="utf-8")
        ex = executor_for(ws)
        ex.invoke("inspect", {"path": ".env"})
        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
        raw = logfile.read_text()
    finally:
        configure_logging(LoggingConfig(level="CRITICAL"))

    assert ".env" not in raw
    assert "sk-inspect-log" not in raw


@POSIX_ONLY
def test_sensitive_target_never_reaches_logs(tmp_path):
    logfile = tmp_path / "agent.log"
    configure_logging(LoggingConfig(level="INFO", format="json",
                                    file_path=str(logfile)))
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        hidden = ws / "hidden"
        hidden.mkdir()
        (hidden / ".env").write_text("API_KEY=sk-symlink-inspect",
                                      encoding="utf-8")
        (ws / "friendly").symlink_to(hidden / ".env")
        ex = executor_for(ws)
        ex.invoke("inspect", {"path": "friendly"})
        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
        raw = logfile.read_text()
    finally:
        configure_logging(LoggingConfig(level="CRITICAL"))

    assert "hidden" not in raw
    assert "sk-symlink-inspect" not in raw
    assert "sensitive_target" in raw  # categorical reason is still logged


@POSIX_ONLY
def test_sensitive_target_never_persists_to_state(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    hidden = ws / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("API_KEY=sk-state-inspect", encoding="utf-8")
    (ws / "friendly").symlink_to(hidden / ".env")

    ex = executor_for(ws)
    invocation = ex.invoke("inspect", {"path": "friendly"})
    assert invocation.ok is False
    assert invocation.result.metadata["reason"] == "sensitive_target"

    state = AgentState()
    state.record_tool_call(invocation)
    store = JsonFileStateStore(tmp_path / "state")
    store.save(state)
    raw = (tmp_path / "state" / f"{state.run_id}.json").read_text()

    assert "hidden" not in raw
    assert ".env" not in raw
    assert "sk-state-inspect" not in raw
    assert "sensitive_target" in raw

    loaded = store.load(state.run_id)
    assert loaded.tool_calls[0]["result"]["metadata"]["reason"] == \
        "sensitive_target"


# ===========================================================================
# Architectural boundary: the tool owns no filesystem security logic
# ===========================================================================

def test_inspect_tool_source_has_no_filesystem_logic():
    """AST scan: the tool must not touch security-sensitive fs APIs.

    It may only call the guard's inspection primitive; type/classification,
    containment and link handling live in the ``fs`` package.
    """
    source = textwrap.dedent(_inspect_module.getsource(InspectTool))
    tree = ast.parse(source)
    forbidden = {"open", "read", "read_text", "read_bytes", "resolve",
                 "stat", "lstat", "scandir", "listdir"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            offenders.append(f"{node.lineno}:{node.attr}")
        if isinstance(node, ast.Name) and node.id in forbidden:
            offenders.append(f"{node.lineno}:{node.id}")
    assert offenders == [], offenders
