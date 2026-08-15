"""Integration: planner reached through the Agent, feature flag and DI.

Demonstrates the headline outcome of this task — an objective now becomes a
validated, executable plan instead of ending `blocked`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.defaults import PassthroughPlanner
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.planning.errors import PlanValidationError
from agentcore.planning.planner import LLMPlanner
from agentcore.tasks.models import TaskStatus
from agentcore.tools.base import Tool, ToolResult, tool_spec


class FakeModel:
    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.calls.append({"prompt": prompt, "system": system,
                           "options": options})
        output = self.outputs.pop(0) if self.outputs else {}
        if isinstance(output, Exception):
            raise output
        return output if isinstance(output, str) else json.dumps(output)


def cfg(tmp_path, *, planning: bool = False, provider: str = "none",
        level: PermissionLevel = PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path),
        model=ModelConfig(provider=provider, name="m" if provider != "none"
                          else "none"),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        features=FeatureFlags(planning=planning),
    ).validate()


def plan_payload(*steps: dict[str, Any], objective: str = "goal"):
    return {"objective": objective, "steps": list(steps)}


def echo_step(step_id="s1", message="hi", **extra):
    step = {"id": step_id, "description": f"echo {message}",
            "tool_name": "echo", "tool_args": {"message": message}}
    step.update(extra)
    return step


# -- feature flag -----------------------------------------------------------

def test_planning_disabled_keeps_passthrough(tmp_path):
    """Default behaviour from earlier tasks must be preserved exactly."""
    agent = Agent(cfg(tmp_path, planning=False, provider="echo"))
    assert isinstance(agent.planner, PassthroughPlanner)


def test_planning_disabled_still_blocks(tmp_path):
    agent = Agent(cfg(tmp_path, planning=False, provider="echo"))
    assert agent.execute("some objective").status is TaskStatus.BLOCKED


def test_planning_enabled_with_model_uses_llm_planner(tmp_path):
    agent = Agent(cfg(tmp_path, planning=True, provider="echo"))
    assert isinstance(agent.planner, LLMPlanner)


def test_planning_enabled_without_model_falls_back(tmp_path):
    """Enabling the flag with no model must not break the agent."""
    agent = Agent(cfg(tmp_path, planning=True, provider="none"))
    assert agent.model is None
    assert isinstance(agent.planner, PassthroughPlanner)
    assert agent.execute("x").status is TaskStatus.BLOCKED


def test_injected_planner_wins_over_flag(tmp_path):
    model = FakeModel(plan_payload(echo_step()))
    agent = Agent(cfg(tmp_path, planning=False))
    planner = LLMPlanner(model, agent.registry)
    injected = Agent(cfg(tmp_path, planning=False),
                     AgentDependencies(planner=planner))
    assert injected.planner is planner


def test_planner_not_constructed_when_disabled(tmp_path):
    """No planning machinery should initialise when the flag is off."""
    agent = Agent(cfg(tmp_path, planning=False, provider="echo"))
    assert type(agent.planner).__name__ == "PassthroughPlanner"


# -- dependency injection ---------------------------------------------------

def test_planner_via_agent_dependencies(tmp_path):
    model = FakeModel(plan_payload(echo_step(), objective="say hi"))
    base = Agent(cfg(tmp_path))
    agent = Agent(cfg(tmp_path), AgentDependencies(
        planner=LLMPlanner(model, base.registry)))
    task = agent.create_task("say hi")
    assert len(task.subtasks) == 1
    assert task.subtasks[0].tool_name == "echo"


def test_planner_sees_agent_registry_including_runtime_tools(tmp_path):
    class Extra(Tool):
        @property
        def spec(self):
            return tool_spec("late_tool", "Registered after startup.")

        def run(self, args, context):
            return ToolResult.success("ok")

    model = FakeModel(plan_payload(echo_step()))
    agent = Agent(cfg(tmp_path))
    agent.planner = LLMPlanner(model, agent.registry)
    agent.register_tool(Extra())
    agent.create_task("x")
    assert "late_tool" in model.calls[0]["prompt"]


# -- end to end -------------------------------------------------------------

def test_objective_becomes_a_validated_plan_and_executes(tmp_path):
    """The headline case: objective -> plan -> real execution."""
    model = FakeModel(plan_payload(
        echo_step("s1", "hello"),
        echo_step("s2", "world", depends_on=["s1"]),
        objective="greet the world"))
    agent = Agent(cfg(tmp_path))
    agent.planner = LLMPlanner(model, agent.registry)

    report = agent.execute("greet the world")

    assert report.status is TaskStatus.COMPLETED
    assert report.executed == 2
    assert report.failed == 0
    assert not report.blocked

    task = agent.state.current_task
    assert [s.result for s in task.subtasks] == ["hello", "world"]
    assert all(s.status is TaskStatus.COMPLETED for s in task.subtasks)
    assert len(agent.state.tool_calls) == 2


def test_planned_execution_respects_dependency_order(tmp_path):
    model = FakeModel(plan_payload(
        echo_step("s2", "second", depends_on=["s1"]),
        echo_step("s1", "first")))
    agent = Agent(cfg(tmp_path))
    agent.planner = LLMPlanner(model, agent.registry)
    agent.execute("ordered")
    results = [s.result for s in agent.state.current_task.subtasks]
    assert results == ["first", "second"]


def test_state_records_planned_run(tmp_path):
    model = FakeModel(plan_payload(echo_step()))
    agent = Agent(cfg(tmp_path))
    agent.planner = LLMPlanner(model, agent.registry)
    agent.execute("x")
    summary = agent.state.summary()
    assert summary["subtasks_total"] == 1
    assert summary["subtasks_completed"] == 1
    assert summary["tool_calls"] == 1


# -- permission boundary ----------------------------------------------------

class MutatingTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("mutate", "Requires fs.write.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("mutated")


def test_planner_cannot_bypass_permissions(tmp_path):
    """A plan may propose a mutation; the policy still refuses it."""
    tool = MutatingTool()
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "write something", "tool_name": "mutate"}))
    agent = Agent(cfg(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(tools=[tool]))
    agent.planner = LLMPlanner(model, agent.registry)

    report = agent.execute("write something")

    assert report.status is TaskStatus.FAILED
    assert tool.runs == 0
    failure = agent.state.current_task.subtasks[0].errors[0]
    assert failure.type == "permission_denied"


def test_same_plan_succeeds_at_autonomous_level(tmp_path):
    """Confirms the refusal above came from policy, not the planner."""
    tool = MutatingTool()
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "write something", "tool_name": "mutate"}))
    agent = Agent(cfg(tmp_path, level=PermissionLevel.AUTONOMOUS),
                  AgentDependencies(tools=[tool]))
    agent.planner = LLMPlanner(model, agent.registry)
    assert agent.execute("write something").status is TaskStatus.COMPLETED
    assert tool.runs == 1


def test_malformed_plan_never_reaches_execution(tmp_path):
    """A rejected plan must leave the agent with nothing to run."""
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "x", "tool_name": "nonexistent_tool"}))
    agent = Agent(cfg(tmp_path))
    agent.planner = LLMPlanner(model, agent.registry)

    with pytest.raises(PlanValidationError):
        agent.execute("x")
    assert agent.state.current_task is None
    assert agent.state.tool_calls == []


def test_invalid_args_never_reach_the_executor(tmp_path):
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "x", "tool_name": "echo",
         "tool_args": {"message": 123}}))
    agent = Agent(cfg(tmp_path))
    agent.planner = LLMPlanner(model, agent.registry)
    with pytest.raises(PlanValidationError):
        agent.execute("x")
    assert agent.state.tool_calls == []
