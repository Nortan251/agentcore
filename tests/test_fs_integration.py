"""Integration tests for the filesystem security foundation.

Covers the parts that cross layer boundaries: workspace identity reaching
tools through the real executor, workspace/run pinning across resume, and
bounded tool output in persisted run state.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from agentcore.cli import main
from agentcore.config.loader import load_config
from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    FilesystemConfig,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.errors import ConfigError
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import (
    PathRejectedError,
    RejectionReason,
    WorkspaceMismatchError,
)
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.state.models import (
    MAX_STORED_OUTPUT_CHARS,
    STORED_TRUNCATION_MARKER,
    AgentState,
    bound_tool_payload,
)
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import Subtask, Task
from agentcore.tools.base import (
    ParamType,
    Tool,
    ToolContext,
    ToolParameter,
    ToolResult,
    tool_spec,
)


def config_for(tmp_path, **kw) -> AgentConfig:
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        **kw,
    ).validate()


# ===========================================================================
# Config integration
# ===========================================================================

def test_agent_config_exposes_canonical_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    config = AgentConfig(workspace_dir="./proj").validate()
    assert config.canonical_workspace() == (tmp_path / "proj").resolve()


def test_invalid_workspace_fails_config_validation(tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    with pytest.raises(PathRejectedError):
        AgentConfig(workspace_dir=str(afile)).validate()


def test_empty_workspace_fails_config_validation():
    with pytest.raises(PathRejectedError):
        AgentConfig(workspace_dir="   ").validate()


def test_filesystem_config_loads_from_toml(tmp_path):
    cfg = tmp_path / "agent.toml"
    cfg.write_text(
        "[filesystem]\n"
        "max_file_bytes = 2048\n"
        "read_chunk_bytes = 512\n"
        "max_directory_entries = 7\n"
        "allow_internal_symlinks = true\n"
        "deny_sensitive_paths = false\n",
        encoding="utf-8")
    config = load_config(cfg)
    assert config.filesystem.max_file_bytes == 2048
    assert config.filesystem.max_directory_entries == 7
    assert config.filesystem.allow_internal_symlinks is True
    assert config.filesystem.deny_sensitive_paths is False


def test_filesystem_config_loads_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCORE_FILESYSTEM__MAX_FILE_BYTES", "4096")
    monkeypatch.setenv("AGENTCORE_WORKSPACE_DIR", str(tmp_path))
    config = load_config()
    assert config.filesystem.max_file_bytes == 4096


def test_bad_filesystem_config_is_rejected(tmp_path):
    cfg = tmp_path / "agent.toml"
    cfg.write_text("[filesystem]\nmax_file_bytes = 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(cfg)


# ===========================================================================
# ToolContext immutability
# ===========================================================================

class WorkspacePeekTool(Tool):
    """Records what the context handed it, and tries to tamper with it."""

    def __init__(self) -> None:
        self.seen_workspace: Any = None
        self.tamper_error: str | None = None

    @property
    def spec(self):
        return tool_spec(name="peek", description="inspect the context",
                         parameters=[], capabilities=[Capability.FS_READ])

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.seen_workspace = context.workspace
        try:
            context.workspace_dir = "/etc"  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - the point of the test
            self.tamper_error = type(exc).__name__
        return ToolResult.success("ok")


def test_tool_context_is_frozen():
    ctx = ToolContext()
    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.workspace_dir = "/etc"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.workspace = None  # type: ignore[misc]


def test_tool_cannot_mutate_workspace_for_a_later_call(tmp_path):
    tool = WorkspacePeekTool()
    agent = Agent(config_for(tmp_path), AgentDependencies(tools=[tool]))

    first = agent.executor.invoke("peek")
    assert first.ok
    assert tool.tamper_error == "FrozenInstanceError"

    # The next invocation still sees the original canonical workspace.
    agent.executor.invoke("peek")
    assert tool.seen_workspace.root == tmp_path.resolve()
    assert agent.executor.workspace_dir == str(tmp_path.resolve())


def test_executor_hands_tools_the_guard(tmp_path):
    tool = WorkspacePeekTool()
    agent = Agent(config_for(tmp_path), AgentDependencies(tools=[tool]))
    agent.executor.invoke("peek")
    assert isinstance(tool.seen_workspace, WorkspaceGuard)
    assert tool.seen_workspace.root == tmp_path.resolve()


def test_agent_workspace_is_canonical(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    agent = Agent(AgentConfig(environment="test", workspace_dir="./proj",
                              logging=LoggingConfig(level="CRITICAL")))
    assert agent.workspace.root == (tmp_path / "proj").resolve()
    assert Path(agent.workspace.identity).is_absolute()


def test_guard_is_shared_not_recreated_per_call(tmp_path):
    agent = Agent(config_for(tmp_path))
    assert agent.executor.workspace is agent.workspace


# ===========================================================================
# Workspace / run consistency
# ===========================================================================

def test_new_run_records_the_canonical_workspace(tmp_path):
    agent = Agent(config_for(tmp_path))
    assert agent.state.workspace == str(tmp_path.resolve())


def test_workspace_survives_serialisation(tmp_path):
    agent = Agent(config_for(tmp_path))
    restored = AgentState.from_dict(json.loads(json.dumps(
        agent.state.to_dict())))
    assert restored.workspace == agent.state.workspace


def test_matching_resume_succeeds(tmp_path):
    store = JsonFileStateStore(tmp_path / "runs")
    agent = Agent(config_for(tmp_path), AgentDependencies(state_store=store))
    agent.assign(Task(objective="do a thing",
                      subtasks=[Subtask(description="step")]))
    agent.checkpoint()
    run_id = agent.state.run_id

    other = Agent(config_for(tmp_path), AgentDependencies(state_store=store))
    state = other.resume(run_id)
    assert state.run_id == run_id
    assert state.workspace == str(tmp_path.resolve())


def test_resume_from_a_different_workspace_is_refused(tmp_path):
    ws_a = tmp_path / "project_a"
    ws_b = tmp_path / "project_b"
    ws_a.mkdir()
    ws_b.mkdir()
    runs = tmp_path / "runs"

    store = JsonFileStateStore(runs)
    agent = Agent(config_for(ws_a), AgentDependencies(state_store=store))
    agent.assign(Task(objective="delete the archive",
                      subtasks=[Subtask(description="step")]))
    agent.checkpoint()
    run_id = agent.state.run_id

    stranger = Agent(config_for(ws_b), AgentDependencies(state_store=store))
    with pytest.raises(WorkspaceMismatchError) as exc:
        stranger.resume(run_id)
    assert exc.value.details["run_workspace"] == str(ws_a.resolve())
    assert exc.value.details["current_workspace"] == str(ws_b.resolve())


def test_no_tools_run_on_workspace_mismatch(tmp_path):
    """A refused resume must leave the agent's own state untouched."""
    ws_a, ws_b = tmp_path / "a", tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    store = JsonFileStateStore(tmp_path / "runs")

    agent = Agent(config_for(ws_a), AgentDependencies(state_store=store))
    agent.assign(Task(objective="x", subtasks=[Subtask(description="s")]))
    agent.checkpoint()
    run_id = agent.state.run_id

    tool = WorkspacePeekTool()
    stranger = Agent(config_for(ws_b),
                     AgentDependencies(state_store=store, tools=[tool]))
    original_run_id = stranger.state.run_id
    with pytest.raises(WorkspaceMismatchError):
        stranger.resume(run_id)
    assert stranger.state.run_id == original_run_id
    assert tool.seen_workspace is None


def test_legacy_state_without_workspace_loads(tmp_path):
    """Runs recorded before pinning must keep working."""
    runs = tmp_path / "runs"
    runs.mkdir()
    agent = Agent(config_for(tmp_path),
                  AgentDependencies(state_store=JsonFileStateStore(runs)))
    agent.assign(Task(objective="legacy", subtasks=[Subtask(description="s")]))
    agent.checkpoint()
    run_id = agent.state.run_id

    # Strip the field, exactly as an older version would have written it.
    path = runs / f"{run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["workspace"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = AgentState.from_dict(payload)
    assert restored.workspace is None

    other = Agent(config_for(tmp_path),
                  AgentDependencies(state_store=JsonFileStateStore(runs)))
    state = other.resume(run_id)
    # Adopted, not invented: it now pins to the workspace it resumed in.
    assert state.workspace == str(tmp_path.resolve())


def test_legacy_state_is_adopted_by_any_workspace(tmp_path):
    """None means 'unknown', which must not be read as 'mismatch'."""
    ws_a, ws_b = tmp_path / "a", tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()

    agent = Agent(config_for(ws_a),
                  AgentDependencies(state_store=JsonFileStateStore(runs)))
    agent.assign(Task(objective="legacy", subtasks=[Subtask(description="s")]))
    agent.checkpoint()
    run_id = agent.state.run_id
    path = runs / f"{run_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["workspace"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    other = Agent(config_for(ws_b),
                  AgentDependencies(state_store=JsonFileStateStore(runs)))
    assert other.resume(run_id).workspace == str(ws_b.resolve())


def test_guard_matches_treats_none_as_match(tmp_path):
    guard = WorkspaceGuard(tmp_path)
    assert guard.matches(None) is True
    assert guard.matches(guard.identity) is True
    assert guard.matches("/somewhere/else") is False


def test_cli_reports_workspace_mismatch_cleanly(tmp_path, capsys, monkeypatch):
    ws_a, ws_b = tmp_path / "a", tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    runs = tmp_path / "runs"

    monkeypatch.setenv("AGENTCORE_WORKSPACE_DIR", str(ws_a))
    agent = Agent(config_for(ws_a),
                  AgentDependencies(state_store=JsonFileStateStore(runs)))
    agent.assign(Task(objective="x", subtasks=[Subtask(description="s")]))
    agent.checkpoint()
    run_id = agent.state.run_id

    monkeypatch.setenv("AGENTCORE_WORKSPACE_DIR", str(ws_b))
    code = main(["--runs-dir", str(runs), "run", "--resume", run_id])
    captured = capsys.readouterr()
    assert code == 2
    assert "different workspace" in captured.err
    assert "Traceback" not in captured.err
    assert str(ws_a.resolve()) in captured.err


# ===========================================================================
# Run-state tool output bounding
# ===========================================================================

def test_small_output_is_stored_verbatim():
    payload = {"tool": "echo", "args": {},
               "result": {"ok": True, "output": "short", "error": None,
                          "metadata": {}}}
    assert bound_tool_payload(payload) == payload


def test_large_output_is_bounded():
    big = "x" * 50_000
    payload = {"tool": "read", "args": {},
               "result": {"ok": True, "output": big, "error": None,
                          "metadata": {}}}
    bounded = bound_tool_payload(payload)
    stored = bounded["result"]["output"]
    assert len(stored) == MAX_STORED_OUTPUT_CHARS
    assert stored.endswith(STORED_TRUNCATION_MARKER)
    assert bounded["result"]["metadata"]["output_truncated"] is True
    assert bounded["result"]["metadata"]["output_original_chars"] == 50_000


def test_bounding_does_not_mutate_the_original():
    payload = {"tool": "t", "args": {},
               "result": {"ok": True, "output": "y" * 50_000, "error": None,
                          "metadata": {}}}
    bound_tool_payload(payload)
    assert len(payload["result"]["output"]) == 50_000


def test_bounding_preserves_verification_fields():
    """ok/error/decision drive verification and recovery; they must survive."""
    payload = {"tool": "t", "args": {"a": 1}, "duration_ms": 1.5,
               "decision": {"allowed": False},
               "result": {"ok": False, "output": "z" * 50_000,
                          "error": "it failed",
                          "metadata": {"kind": "ToolFailure",
                                       "retryable": True}}}
    bounded = bound_tool_payload(payload)
    assert bounded["result"]["ok"] is False
    assert bounded["result"]["error"] == "it failed"
    assert bounded["result"]["metadata"]["kind"] == "ToolFailure"
    assert bounded["result"]["metadata"]["retryable"] is True
    assert bounded["decision"] == {"allowed": False}
    assert bounded["args"] == {"a": 1}
    assert bounded["duration_ms"] == 1.5


def test_large_non_string_output_is_bounded():
    payload = {"tool": "t", "args": {},
               "result": {"ok": True, "output": list(range(50_000)),
                          "error": None, "metadata": {}}}
    bounded = bound_tool_payload(payload)
    assert isinstance(bounded["result"]["output"], str)
    assert len(bounded["result"]["output"]) == MAX_STORED_OUTPUT_CHARS


def test_small_structured_output_keeps_its_type():
    payload = {"tool": "t", "args": {},
               "result": {"ok": True, "output": {"a": 1}, "error": None,
                          "metadata": {}}}
    assert bound_tool_payload(payload)["result"]["output"] == {"a": 1}


def test_none_output_untouched():
    payload = {"tool": "t", "args": {},
               "result": {"ok": True, "output": None, "error": None,
                          "metadata": {}}}
    assert bound_tool_payload(payload) is payload


def test_malformed_payload_is_passed_through():
    for payload in ({"tool": "t"}, {"tool": "t", "result": "nonsense"},
                    {"tool": "t", "result": {}}):
        assert bound_tool_payload(payload) is payload


def test_state_bounds_recorded_tool_output():
    state = AgentState()
    state.record_tool_call({"tool": "read", "args": {},
                            "result": {"ok": True, "output": "b" * 40_000,
                                       "error": None, "metadata": {}}})
    stored = state.tool_calls[0]["result"]["output"]
    assert len(stored) == MAX_STORED_OUTPUT_CHARS
    # The history entry mirrors the same bounded payload.
    entry = [h for h in state.history if h.kind == "tool_call"][0]
    assert len(entry.data["result"]["output"]) == MAX_STORED_OUTPUT_CHARS


class BigOutputTool(Tool):
    @property
    def spec(self):
        return tool_spec(name="big", description="emit a lot",
                         parameters=[], capabilities=[])

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("m" * 100_000)


def test_persisted_run_state_stays_bounded(tmp_path):
    """End to end: a huge tool output must not bloat the state file."""
    store = JsonFileStateStore(tmp_path / "runs")
    agent = Agent(config_for(tmp_path),
                  AgentDependencies(tools=[BigOutputTool()],
                                    state_store=store))
    invocation = agent.executor.invoke("big")
    # The live result is complete — only the stored copy is bounded.
    assert len(invocation.result.output) == 100_000

    agent.state.record_tool_call(invocation)
    agent.checkpoint()

    path = tmp_path / "runs" / f"{agent.state.run_id}.json"
    assert path.stat().st_size < 60_000
    reloaded = store.load(agent.state.run_id)
    assert len(reloaded.tool_calls[0]["result"]["output"]) == \
        MAX_STORED_OUTPUT_CHARS


def test_runner_still_sees_the_full_output(tmp_path):
    """Bounding storage must not change what the task actually produced."""
    agent = Agent(config_for(tmp_path),
                  AgentDependencies(tools=[BigOutputTool()]))
    task = Task(objective="emit",
                subtasks=[Subtask(description="emit", tool_name="big")])
    agent.assign(task)
    agent.run_task()
    assert len(task.subtasks[0].result) == 100_000


# ===========================================================================
# Security regression
# ===========================================================================

def test_fs_package_contains_no_tools():
    """This task must not have shipped a filesystem tool."""
    import agentcore.fs as fs_pkg
    from agentcore.tools.base import Tool as ToolBase

    package_dir = Path(fs_pkg.__file__).parent
    import ast
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {getattr(b, "id", getattr(b, "attr", ""))
                         for b in node.bases}
                assert "Tool" not in bases, f"{path.name}:{node.name}"


def test_no_read_file_or_list_directory_tool_is_registered(tmp_path):
    agent = Agent(config_for(tmp_path))
    names = set(agent.registry.names())
    assert "read_file" not in names
    assert "list_directory" not in names
    assert names == {"echo", "workspace_info"}


def test_permission_behaviour_is_unchanged(tmp_path):
    """FS_READ remains allowed at OBSERVE: this task changed no permissions."""
    agent = Agent(AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.OBSERVE),
        logging=LoggingConfig(level="CRITICAL")).validate())
    decision = agent.policy.check(frozenset({Capability.FS_READ}))
    assert decision.allowed is True


def test_guard_does_not_consult_the_permission_policy():
    """The two layers are deliberately independent.

    Sensitive-path decisions must not depend on the permission level, because
    FS_READ is allowed at OBSERVE.
    """
    import ast
    import agentcore.fs as fs_pkg

    package_dir = Path(fs_pkg.__file__).parent
    for path in package_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = [a.name for a in node.names]
                assert "permissions" not in mod, f"{path.name}: {mod}"
                assert not any("permission" in n.lower() for n in names)


def test_sensitive_policy_never_reads_file_contents():
    """Deciding by content would require reading the bytes being protected."""
    import ast
    from agentcore.fs import sensitive

    tree = ast.parse(Path(sensitive.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"read_text", "read_bytes", "open"}
        if isinstance(node, ast.Name):
            assert node.id != "open"


def test_guard_writes_no_memory(tmp_path):
    """The safety layer must not touch durable memory."""
    import ast
    import agentcore.fs as fs_pkg

    package_dir = Path(fs_pkg.__file__).parent
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                assert "memory" not in mod, f"{path.name}: {mod}"


def test_no_secret_reaches_the_log(tmp_path, caplog):
    """A denied credential path must not put its contents into logs."""
    (tmp_path / ".env").write_text("API_KEY=sk-must-never-be-logged\n",
                                   encoding="utf-8")
    guard = WorkspaceGuard(tmp_path)
    with caplog.at_level("DEBUG"):
        with pytest.raises(PathRejectedError):
            guard.resolve_relative(".env")
    assert "sk-must-never-be-logged" not in caplog.text


def test_tool_execution_path_is_still_the_executor(tmp_path):
    """No bypass was introduced: tools still run through ToolExecutor."""
    import ast
    import agentcore.core.agent as agent_module

    tree = ast.parse(Path(agent_module.__file__).read_text(encoding="utf-8"))
    direct_runs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "tool"
    ]
    assert direct_runs == []
