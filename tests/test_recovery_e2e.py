"""Full loop: Plan -> Execute -> Verify -> Recover.

Exercises every layer built so far together, with a fake model so no
credential or network is required.
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
from agentcore.permissions.levels import PermissionLevel
from agentcore.planning.planner import LLMPlanner
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.history import RecoveryHistory
from agentcore.tasks.models import TaskStatus, VerificationStatus
from agentcore.tools.base import Tool, ToolResult, tool_spec


class FakeModel:
    """Serves planning JSON, then verification judgements."""

    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.calls.append(prompt)
        if not self.outputs:
            raise AssertionError("FakeModel exhausted")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)


class FlakyEcho(Tool):
    """Read-only echo that fails the first N attempts."""

    def __init__(self, fails: int = 1) -> None:
        self.remaining = fails
        self.runs = 0

    @property
    def spec(self):
        from agentcore.tools.base import ParamType, ToolParameter

        return tool_spec("echo", "Return the supplied message.",
                         parameters=[ToolParameter("message", ParamType.STRING)],
                         read_only=True)

    def run(self, args, context):
        self.runs += 1
        if self.remaining > 0:
            self.remaining -= 1
            return ToolResult.failure("transient network blip", retryable=True)
        return ToolResult.success(args["message"])


def plan_for(message: str, expected: str, *, objective="say the phrase"):
    return json.dumps({"objective": objective, "steps": [{
        "id": "s1", "description": "say the phrase", "tool_name": "echo",
        "tool_args": {"message": message},
        "expected_result": expected,
        "requires_verification": True}]})


def full_config(tmp_path, **recovery_kwargs: Any) -> AgentConfig:
    recovery_kwargs.setdefault("base_delay_seconds", 0.0)
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(**recovery_kwargs),
        # every capability layer built so far, enabled together
        features=FeatureFlags(planning=True, verification=True, recovery=True),
    ).validate()


def build(tmp_path, model: FakeModel, tool: Tool, **recovery_kwargs: Any):
    agent = Agent(full_config(tmp_path, **recovery_kwargs),
                  AgentDependencies(tools=[tool], model=model))
    agent.planner = LLMPlanner(model, agent.registry)
    return agent


# ===========================================================================

def test_full_loop_transient_failure_recovers_and_verifies(tmp_path):
    """Plan -> execute (fails) -> RETRY -> execute -> verify PASSED."""
    model = FakeModel(plan_for("hello world", "hello world"))
    tool = FlakyEcho(fails=1)
    agent = build(tmp_path, model, tool)

    report = agent.execute("say the phrase")

    assert report.status is TaskStatus.COMPLETED
    assert tool.runs == 2
    assert report.retried == 1
    assert [d["action"] for d in report.decisions] == ["retry"]

    subtask = agent.state.current_task.subtasks[0]
    assert subtask.result == "hello world"
    assert subtask.verification.status is VerificationStatus.PASSED
    # planning consumed one model call; verification was deterministic
    assert len(model.calls) == 1


def test_full_loop_verification_failure_drives_recovery(tmp_path):
    """Plan -> execute (ok) -> verify FAILED -> recovery reacts."""
    model = FakeModel(plan_for("goodbye", "a greeting containing hello"))
    tool = FlakyEcho(fails=0)
    agent = build(tmp_path, model, tool, max_attempts=2, max_replans=0)

    report = agent.execute("say the phrase")

    subtask = agent.state.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.FAILED
    assert report.decisions, "verification failure must reach recovery"
    # echo is read-only, so a retry was safe and was attempted
    assert tool.runs == 2
    assert report.retried == 1
    assert report.status is not TaskStatus.COMPLETED


def test_full_loop_records_history_for_diagnosis(tmp_path):
    model = FakeModel(plan_for("hello", "hello"))
    tool = FlakyEcho(fails=1)
    agent = build(tmp_path, model, tool)
    agent.execute("say the phrase")

    subtask_id = agent.state.current_task.subtasks[0].id
    history = RecoveryHistory.load(agent.state)
    assert history.retries(subtask_id) == 1
    summary = history.summary(subtask_id)
    assert summary and summary[0].startswith("retry:")


def test_full_loop_state_survives_resume(tmp_path):
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    model = FakeModel(plan_for("hello", "hello"))
    tool = FlakyEcho(fails=1)
    agent = Agent(full_config(tmp_path),
                  AgentDependencies(tools=[tool], model=model,
                                    state_store=store))
    agent.planner = LLMPlanner(model, agent.registry)
    agent.execute("say the phrase")
    run_id = agent.state.run_id
    agent.shutdown()

    resumed = Agent(full_config(tmp_path),
                    AgentDependencies(state_store=store)).resume(run_id)
    subtask = resumed.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.PASSED
    assert RecoveryHistory.load(resumed).retries(subtask.id) == 1


def test_all_layers_disabled_preserves_original_behaviour(tmp_path):
    """With every flag off the agent behaves exactly as in Task 1."""
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        features=FeatureFlags(),
    ).validate()
    agent = Agent(config)
    report = agent.execute("do something vague")

    assert report.status is TaskStatus.BLOCKED
    assert report.decisions == []
    assert report.retried == 0
    assert type(agent.planner).__name__ == "PassthroughPlanner"
    assert type(agent.verifier).__name__ == "NullVerifier"
    assert type(agent.recovery).__name__ == "NoRetryStrategy"
