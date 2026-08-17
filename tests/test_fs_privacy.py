"""Task 39: filesystem-path privacy across logs and persisted state.

Two disclosure findings are closed here, each with a test over the real
machinery (not a mock):

* F-03 — ``ToolExecutor.invoke()`` logged raw tool arguments, so a
  model-supplied sensitive path such as ``{"path": ".env"}`` reached the
  structured log. The executor now redacts sensitive-path-shaped argument
  values using the same policy the guard enforces, and suppresses the raw
  error message for sensitive rejections (its reason string names the file).

* F-04 — a ``SENSITIVE_TARGET`` rejection carried ``target=<resolved path>``
  in its details, which flowed through the tool result into ``AgentState``
  and the JSON state store. The resolved sensitive location is no longer
  included.

The tests inspect the actual formatter output and the actual on-disk state
file, so a regression in either boundary fails loudly.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

from agentcore.config.schema import LoggingConfig, PermissionConfig
from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.state.models import AgentState
from agentcore.state.store import JsonFileStateStore
from agentcore.tools.builtin import default_tools
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


def _build_executor(workspace, *, allow_internal_symlinks=False):
    registry = ToolRegistry()
    registry.register_all(default_tools())
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    guard = WorkspaceGuard(
        workspace,
        config=FilesystemConfig(
            allow_internal_symlinks=allow_internal_symlinks))
    return ToolExecutor(registry, policy, workspace=guard)


def _log_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ===========================================================================
# F-03 — sensitive paths must not reach the structured log
# ===========================================================================

def test_sensitive_path_argument_is_not_logged(tmp_path):
    """A model-supplied ``.env`` must not appear in the log; ``notes.txt`` may."""
    logfile = tmp_path / "agent.log"
    configure_logging(LoggingConfig(level="INFO", format="json",
                                    file_path=str(logfile)))
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / ".env").write_text("API_KEY=sk-log-test", encoding="utf-8")
        (workspace / "notes.txt").write_text("plain notes", encoding="utf-8")

        executor = _build_executor(workspace)
        executor.invoke("read_file", {"path": ".env"})
        executor.invoke("read_file", {"path": "notes.txt"})

        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
        raw = logfile.read_text()
    finally:
        configure_logging(LoggingConfig(level="CRITICAL"))

    # The sensitive path stays out of the log, in both the args and the
    # failure message.
    assert ".env" not in raw
    # The ordinary path remains visible for operational debugging.
    assert "notes.txt" in raw
    # No file content may ever reach the log.
    assert "sk-log-test" not in raw
    assert "plain notes" not in raw


def test_ordinary_operational_logging_still_works(tmp_path):
    """The fix must not gut logging: tool/event/duration survive."""
    logfile = tmp_path / "agent.log"
    configure_logging(LoggingConfig(level="INFO", format="json",
                                    file_path=str(logfile)))
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "notes.txt").write_text("plain notes", encoding="utf-8")
        executor = _build_executor(workspace)
        executor.invoke("read_file", {"path": "notes.txt"})
        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
    finally:
        configure_logging(LoggingConfig(level="CRITICAL"))

    events = [e["event"] for e in _log_lines(logfile)]
    assert "tool.invoked" in events
    assert "tool.succeeded" in events
    invoked = next(e for e in _log_lines(logfile) if e["event"] == "tool.invoked")
    assert invoked["tool"] == "read_file"
    assert invoked["args"] == {"path": "notes.txt"}
    succeeded = [e for e in _log_lines(logfile)
                 if e["event"] == "tool.succeeded"]
    assert any("duration_ms" in e for e in succeeded)


@POSIX_ONLY
def test_sensitive_symlink_target_is_not_logged(tmp_path):
    """An innocuous symlink to a sensitive target must not leak the target."""
    logfile = tmp_path / "agent.log"
    configure_logging(LoggingConfig(level="INFO", format="json",
                                    file_path=str(logfile)))
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        hidden = workspace / "hidden"
        hidden.mkdir()
        (hidden / ".env").write_text("API_KEY=sk-symlink-test", encoding="utf-8")
        (workspace / "innocent.txt").symlink_to(hidden / ".env")

        executor = _build_executor(workspace, allow_internal_symlinks=True)
        executor.invoke("read_file", {"path": "innocent.txt"})
        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
        raw = logfile.read_text()
    finally:
        configure_logging(LoggingConfig(level="CRITICAL"))

    # Neither the resolved location nor the secret content reach the log.
    assert "hidden" not in raw
    assert ".env" not in raw
    assert "sk-symlink-test" not in raw
    # The categorical reason is still logged.
    assert "sensitive_target" in raw


# ===========================================================================
# F-04 — resolved sensitive target must not be persisted in run state
# ===========================================================================

@POSIX_ONLY
def test_sensitive_target_location_is_not_persisted(tmp_path):
    """The resolved sensitive target never reaches the JSON state file."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    hidden = workspace / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("API_KEY=sk-state-test", encoding="utf-8")
    (workspace / "innocent.txt").symlink_to(hidden / ".env")

    executor = _build_executor(workspace, allow_internal_symlinks=True)
    invocation = executor.invoke("read_file", {"path": "innocent.txt"})
    assert invocation.ok is False
    assert invocation.result.metadata["reason"] == "sensitive_target"

    state = AgentState()
    state.record_tool_call(invocation)

    store = JsonFileStateStore(tmp_path / "state")
    store.save(state)
    raw = (tmp_path / "state" / f"{state.run_id}.json").read_text()

    # The resolved location of the protected target is not persisted — neither
    # the full relative path nor its directory component.
    assert "hidden/.env" not in raw
    assert "hidden" not in raw
    # The secret content is not persisted (it was never read).
    assert "sk-state-test" not in raw
    # The categorical rejection reason IS preserved, so recovery/replanning
    # can still branch on it.
    assert "sensitive_target" in raw

    # The persisted state remains valid and round-trips.
    loaded = store.load(state.run_id)
    assert loaded.tool_calls[0]["result"]["metadata"]["reason"] == "sensitive_target"
    assert loaded.tool_calls[0]["result"]["metadata"]["path"] == "innocent.txt"


@POSIX_ONLY
def test_model_and_memory_never_see_the_resolved_target(tmp_path):
    """The rejection result, as the model-facing/memory path consumes it,
    carries no resolved sensitive location."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    hidden = workspace / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("API_KEY=sk-model-test", encoding="utf-8")
    (workspace / "innocent.txt").symlink_to(hidden / ".env")

    executor = _build_executor(workspace, allow_internal_symlinks=True)
    invocation = executor.invoke("read_file", {"path": "innocent.txt"})

    # Everything the model/memory could receive is bounded to the result's
    # output/error/metadata — none of which may contain the resolved target.
    blob = json.dumps(invocation.to_dict(), default=str)
    assert "hidden" not in blob
    assert "hidden/.env" not in blob
    assert "sk-model-test" not in blob
    assert "sensitive_target" in blob
