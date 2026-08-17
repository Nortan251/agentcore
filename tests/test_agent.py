"""End-to-end wiring tests: config -> agent -> task -> tool -> state."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from agentcore.config.schema import (
    AgentConfig,
    LoggingConfig,
    PermissionConfig,
    ToolConfig,
)
from agentcore.core.agent import Agent, AgentDependencies, build_agent
from agentcore.core.defaults import BoundedRetryStrategy, PassthroughPlanner
from agentcore.core.runner import TaskRunner
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.state.models import AgentPhase
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import Tool, ToolContext, ToolResult, ToolSpec, tool_spec
from agentcore.tools.builtin import default_tools


class FlakyTool(Tool):
    """Fails with a retryable error until the nth attempt."""

    def __init__(self, fail_times: int = 1) -> None:
        self.remaining = fail_times
        self.calls = 0

    @property
    def spec(self) -> ToolSpec:
        return tool_spec("flaky", "Fails then succeeds.")

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return ToolResult.failure("transient failure", retryable=True)
        return ToolResult.success("recovered")


class MutatingTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return tool_spec("mutate", "Declares fs.write.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("ok")


class PassingVerifier:
    def verify(self, subtask, state) -> VerificationResult:
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="PassingVerifier")


@pytest.fixture
def agent(autonomous_config) -> Agent:
    return Agent(autonomous_config)


# -- construction -----------------------------------------------------------

def test_agent_boots_with_defaults(agent):
    status = agent.status()
    assert status["permission_level"] == "autonomous"
    assert set(status["tools"]) == {"echo", "workspace_info", "read_file",
                                    "list_directory", "inspect"}
    assert agent.state.phase is AgentPhase.IDLE
    # optional seams are intentionally unwired
    assert agent.memory is None and agent.model is None


def test_agent_defaults_to_observe_level():
    a = Agent(AgentConfig(logging=LoggingConfig(level="CRITICAL")))
    assert a.policy.level is PermissionLevel.OBSERVE


def test_build_agent_factory(autonomous_config):
    a = build_agent(autonomous_config, tools=[])
    assert a.registry.names() == []


def test_config_disabled_tools_never_register(tmp_path):
    cfg = AgentConfig(
        workspace_dir=str(tmp_path),
        tools=ToolConfig(disabled=("echo",)),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"))
    assert Agent(cfg).registry.names() == ["inspect", "list_directory",
                                           "read_file", "workspace_info"]


# -- tool calls -------------------------------------------------------------

def test_call_tool_records_state(agent):
    inv = agent.call_tool("echo", {"message": "hi"})
    assert inv.ok and inv.result.output == "hi"
    assert len(agent.state.tool_calls) == 1
    assert agent.state.history[-1].kind == "tool_call"


def test_runtime_tool_registration_is_the_extension_point(agent):
    """A new capability plugs in without touching the agent class."""
    assert agent.register_tool(MutatingTool())
    inv = agent.call_tool("mutate")
    assert inv.ok
    assert "mutate" in agent.registry.names()


# -- task execution ---------------------------------------------------------

def test_execute_runs_a_tool_backed_task(agent):
    task = Task(objective="say hello")
    task.add_subtask(Subtask(description="echo hello", tool_name="echo",
                             tool_args={"message": "hello"}))
    report = agent.run_task(task)

    assert report.status is TaskStatus.COMPLETED
    assert report.executed == 1 and report.failed == 0
    assert task.subtasks[0].result == "hello"
    assert agent.state.phase is AgentPhase.DONE
    assert agent.state.summary()["subtasks_completed"] == 1


def test_dependencies_execute_in_order(agent):
    task = Task(objective="chain")
    first = task.add_subtask(Subtask(description="a", tool_name="echo",
                                     tool_args={"message": "a"}))
    task.add_subtask(Subtask(description="b", tool_name="echo",
                             tool_args={"message": "b"},
                             depends_on=[first.id]))
    report = agent.run_task(task)
    assert report.executed == 2
    assert [s.result for s in task.subtasks] == ["a", "b"]


def test_toolless_subtask_becomes_a_pending_action(agent):
    report = agent.execute("do something vague")  # PassthroughPlanner, no tool
    assert report.status is TaskStatus.BLOCKED
    assert report.blocked and report.skipped == 1
    assert len(agent.state.pending_actions) == 1
    assert agent.state.pending_actions[0].reason == "subtask has no tool assigned"


def test_failing_subtask_fails_the_task(agent):
    task = Task(objective="fail")
    task.add_subtask(Subtask(description="bad args", tool_name="echo",
                             tool_args={}))
    report = agent.run_task(task)
    assert report.status is TaskStatus.FAILED
    assert report.failed == 1
    assert agent.state.phase is AgentPhase.ERROR
    assert task.subtasks[0].errors[0].details["kind"] == "validation"


def test_unknown_tool_fails_subtask_without_crashing(agent):
    task = Task(objective="missing tool")
    task.add_subtask(Subtask(description="x", tool_name="nope"))
    report = agent.run_task(task)
    assert report.status is TaskStatus.FAILED
    assert task.subtasks[0].errors[0].type == "ToolNotFoundError"


def test_permission_denial_surfaces_as_task_failure(tmp_path):
    cfg = AgentConfig(workspace_dir=str(tmp_path),
                      permissions=PermissionConfig(level=PermissionLevel.OBSERVE),
                      logging=LoggingConfig(level="CRITICAL"))
    a = Agent(cfg, AgentDependencies(tools=[*default_tools(), MutatingTool()]))
    task = Task(objective="write")
    task.add_subtask(Subtask(description="w", tool_name="mutate"))
    report = a.run_task(task)
    assert report.status is TaskStatus.FAILED
    assert task.subtasks[0].errors[0].type == "permission_denied"


def test_recovery_strategy_retries_retryable_failures(autonomous_config):
    flaky = FlakyTool(fail_times=1)
    a = Agent(autonomous_config, AgentDependencies(
        tools=[flaky], recovery=BoundedRetryStrategy(max_attempts=3)))
    task = Task(objective="retry")
    task.add_subtask(Subtask(description="f", tool_name="flaky"))
    report = a.run_task(task)

    assert report.status is TaskStatus.COMPLETED
    assert flaky.calls == 2
    assert task.subtasks[0].attempts == 2
    assert any(h.data.get("to") == "recovering" for h in a.state.history
               if h.kind == "transition")


def test_no_retry_by_default(autonomous_config):
    flaky = FlakyTool(fail_times=1)
    a = Agent(autonomous_config, AgentDependencies(tools=[flaky]))
    task = Task(objective="no retry")
    task.add_subtask(Subtask(description="f", tool_name="flaky"))
    assert a.run_task(task).status is TaskStatus.FAILED
    assert flaky.calls == 1


def test_verifier_result_is_recorded(autonomous_config):
    a = Agent(autonomous_config, AgentDependencies(verifier=PassingVerifier()))
    task = Task(objective="verify")
    task.add_subtask(Subtask(description="e", tool_name="echo",
                             tool_args={"message": "x"}))
    a.run_task(task)
    assert task.subtasks[0].verification.passed
    assert a.state.verifications[0]["status"] == "passed"


def test_default_verifier_skips(agent):
    task = Task(objective="v")
    task.add_subtask(Subtask(description="e", tool_name="echo",
                             tool_args={"message": "x"}))
    agent.run_task(task)
    assert task.subtasks[0].verification.status is VerificationStatus.SKIPPED


def test_run_task_without_assignment_raises(agent):
    with pytest.raises(ValueError):
        agent.run_task()


def test_runner_respects_max_steps(executor):
    runner = TaskRunner(executor, max_steps=1)
    from agentcore.state.models import AgentState

    task = Task(objective="two steps")
    a = task.add_subtask(Subtask(description="a", tool_name="echo",
                                 tool_args={"message": "a"}))
    task.add_subtask(Subtask(description="b", tool_name="echo",
                             tool_args={"message": "b"}, depends_on=[a.id]))
    report = runner.run(task, AgentState())
    assert report.executed == 1
    assert report.status is TaskStatus.BLOCKED


# -- planner ----------------------------------------------------------------

def test_passthrough_planner_does_not_decompose():
    task = PassthroughPlanner().plan("one goal", context={"k": "v"})
    assert len(task.subtasks) == 1
    assert task.subtasks[0].description == "one goal"
    assert task.metadata == {"k": "v"}


# -- state persistence ------------------------------------------------------

def test_checkpoint_and_resume_across_agents(autonomous_config, tmp_path):
    store = JsonFileStateStore(tmp_path / "runs")
    first = Agent(autonomous_config, AgentDependencies(state_store=store))
    task = Task(objective="persist")
    task.add_subtask(Subtask(description="e", tool_name="echo",
                             tool_args={"message": "hi"}))
    first.run_task(task)
    run_id = first.state.run_id
    first.shutdown()

    second = Agent(autonomous_config, AgentDependencies(state_store=store))
    resumed = second.resume(run_id)
    assert resumed.run_id == run_id
    assert resumed.current_task.objective == "persist"
    assert resumed.current_task.status is TaskStatus.COMPLETED
    assert resumed.resume_point.task_id == task.id
    assert len(resumed.tool_calls) == 1


def test_reset_starts_a_new_run(agent):
    old = agent.state.run_id
    assert agent.reset().run_id != old
