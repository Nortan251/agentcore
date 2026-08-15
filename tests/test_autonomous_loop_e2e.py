"""The full autonomous loop, end to end.

    objective -> plan -> execute -> verify (FAIL)
              -> recover -> replan -> execute -> verify (PASS) -> success

Uses the *real* LLMPlanner, real deterministic Verifier, real RecoveryPolicy
and real permission enforcement, driven by a scripted fake model — so every
layer built across tasks 1-6 participates, with no network or credential.
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
from agentcore.core.replan import ReplanLedger
from agentcore.permissions.levels import PermissionLevel
from agentcore.planning.planner import LLMPlanner
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.history import RecoveryHistory
from agentcore.state.models import AgentPhase, AgentState
from agentcore.tasks.models import TaskStatus, VerificationStatus
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec


class ScriptedModel:
    """Returns planning JSON in order and records every prompt."""

    def __init__(self, *plans: str) -> None:
        self.plans = list(plans)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.prompts.append(prompt)
        if not self.plans:
            raise AssertionError("ScriptedModel exhausted")
        return self.plans.pop(0)


class WriteTool(Tool):
    """Mutating tool: retrying is not provably safe, so recovery replans."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def spec(self):
        return tool_spec(
            "write_note", "Write a note and return what was written.",
            parameters=[ToolParameter("text", ParamType.STRING,
                                      "The note contents.")],
            capabilities=[],           # no capability => allowed, still mutating
            read_only=False)

    def run(self, args, context):
        self.calls.append(args["text"])
        return ToolResult.success(args["text"])


def plan_json(text: str, expected: str, *, objective: str) -> str:
    return json.dumps({"objective": objective, "steps": [{
        "id": "s1",
        "description": "write the note",
        "tool_name": "write_note",
        "tool_args": {"text": text},
        "expected_result": expected,
        "requires_verification": True,
    }]})


def full_config(tmp_path, *, max_replans: int = 2) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        # every capability layer built so far, enabled together
        features=FeatureFlags(planning=True, verification=True, recovery=True),
    ).validate()


def build(tmp_path, model: ScriptedModel, tool: Tool, *, max_replans=2,
          level=PermissionLevel.AUTONOMOUS):
    config = full_config(tmp_path, max_replans=max_replans)
    if level is not PermissionLevel.AUTONOMOUS:
        config = AgentConfig(
            environment="test", workspace_dir=str(tmp_path),
            model=ModelConfig(),
            permissions=PermissionConfig(level=level),
            logging=LoggingConfig(level="CRITICAL"),
            recovery=RecoveryConfig(max_replans=max_replans,
                                    base_delay_seconds=0.0),
            features=FeatureFlags(planning=True, verification=True,
                                  recovery=True),
        ).validate()
    agent = Agent(config, AgentDependencies(tools=[tool], model=model))
    agent.planner = LLMPlanner(model, agent.registry)
    return agent


OBJECTIVE = "record the launch date"


# ===========================================================================
# THE headline test for this task
# ===========================================================================

def test_full_autonomous_loop_recovers_via_replan(tmp_path):
    """Plan -> execute -> verify FAIL -> recover -> replan -> execute -> PASS."""
    model = ScriptedModel(
        # first approach writes the wrong thing
        plan_json("todo: figure this out later",
                  "the launch date 2026-03-01 is recorded",
                  objective=OBJECTIVE),
        # replanned approach writes what was actually expected
        plan_json("the launch date 2026-03-01 is recorded",
                  "the launch date 2026-03-01 is recorded",
                  objective=OBJECTIVE),
    )
    tool = WriteTool()
    agent = build(tmp_path, model, tool)

    report = agent.execute(OBJECTIVE)

    # --- final outcome -----------------------------------------------------
    assert report.status is TaskStatus.COMPLETED
    assert agent.state.phase is AgentPhase.DONE

    # --- the loop actually ran twice, with different work ------------------
    assert tool.calls == ["todo: figure this out later",
                          "the launch date 2026-03-01 is recorded"]
    assert len(model.prompts) == 2          # one plan + one replan

    # --- verification told the truth at each step --------------------------
    statuses = [v["status"] for v in agent.state.verifications]
    assert statuses[0] == "failed"
    assert statuses[-1] == "passed"
    assert agent.state.current_task.subtasks[0].verification.status is \
        VerificationStatus.PASSED

    # --- recovery drove the replan -----------------------------------------
    ledger = ReplanLedger.load(agent.state)
    assert ledger.count == 1
    assert ledger.records[0].outcome == "accepted"
    assert ledger.archived_plans, "the failed plan is preserved"

    # --- phases tell the story ---------------------------------------------
    phases = [h.data["to"] for h in agent.state.history
              if h.kind == "transition"]
    assert "recovering" in phases and "planning" in phases

    # --- the replan prompt carried failure context -------------------------
    assert "replanning" in model.prompts[1]

    # --- objective preserved throughout ------------------------------------
    assert agent.state.current_task.objective == OBJECTIVE


def test_full_loop_succeeds_first_time_without_replanning(tmp_path):
    """The control case: no failure means no recovery and no replan."""
    model = ScriptedModel(plan_json("2026-03-01", "2026-03-01",
                                    objective=OBJECTIVE))
    tool = WriteTool()
    agent = build(tmp_path, model, tool)

    report = agent.execute(OBJECTIVE)

    assert report.status is TaskStatus.COMPLETED
    assert len(tool.calls) == 1
    assert len(model.prompts) == 1
    assert ReplanLedger.load(agent.state).count == 0
    assert report.decisions == []


def test_full_loop_gives_up_within_budget(tmp_path):
    """Every approach fails: the loop terminates at the configured budget.

    Each replan proposes a *materially different* plan, so all three reach
    execution. (A planner that repeats the same executable strategy is
    rejected before execution — see the duplicate-plan suite.)
    """
    expected = "the launch date 2026-03-01 is recorded"
    model = ScriptedModel(
        plan_json("wrong content A", expected, objective=OBJECTIVE),
        plan_json("wrong content B", expected, objective=OBJECTIVE),
        plan_json("wrong content C", expected, objective=OBJECTIVE),
    )
    tool = WriteTool()
    agent = build(tmp_path, model, tool, max_replans=2)

    report = agent.execute(OBJECTIVE)

    assert report.status is not TaskStatus.COMPLETED
    assert len(tool.calls) == 3               # initial + 2 replanned attempts
    assert ReplanLedger.load(agent.state).count == 2
    assert not model.plans, "exactly the scripted number of model calls"
    # nothing was fabricated as successful
    assert all(v["status"] != "passed" for v in agent.state.verifications)


def test_full_loop_rejects_repeated_identical_strategy(tmp_path):
    """A planner that keeps proposing the same failed plan is stopped early."""
    bad = plan_json("wrong content", "the launch date 2026-03-01 is recorded",
                    objective=OBJECTIVE)
    model = ScriptedModel(bad, bad, bad)
    tool = WriteTool()
    agent = build(tmp_path, model, tool, max_replans=2)

    report = agent.execute(OBJECTIVE)

    assert report.status is not TaskStatus.COMPLETED
    # executed once; the two duplicate proposals never ran
    assert len(tool.calls) == 1
    assert ReplanLedger.load(agent.state).count == 2


def test_full_loop_respects_permissions_across_replans(tmp_path):
    """A denied action stays denied no matter how many times it is replanned."""
    class GuardedTool(WriteTool):
        @property
        def spec(self):
            from agentcore.permissions.levels import Capability

            return tool_spec(
                "write_note", "Write a note.",
                parameters=[ToolParameter("text", ParamType.STRING)],
                capabilities=[Capability.FS_WRITE], read_only=False)

    plan = plan_json("anything", "something recorded", objective=OBJECTIVE)
    model = ScriptedModel(plan, plan, plan)
    tool = GuardedTool()
    agent = build(tmp_path, model, tool, max_replans=2,
                  level=PermissionLevel.OBSERVE)

    report = agent.execute(OBJECTIVE)

    assert tool.calls == []                   # never executed
    assert agent.policy.level is PermissionLevel.OBSERVE
    assert report.status is not TaskStatus.COMPLETED


def test_full_loop_state_is_fully_recoverable(tmp_path):
    """The whole story survives a serialise/restore round trip."""
    model = ScriptedModel(
        plan_json("wrong", "the launch date 2026-03-01 is recorded",
                  objective=OBJECTIVE),
        plan_json("the launch date 2026-03-01 is recorded",
                  "the launch date 2026-03-01 is recorded",
                  objective=OBJECTIVE),
    )
    agent = build(tmp_path, model, WriteTool())
    agent.execute(OBJECTIVE)

    restored = AgentState.from_dict(json.loads(
        json.dumps(agent.state.to_dict(), default=str)))

    assert restored.current_task.objective == OBJECTIVE
    assert restored.current_task.status is TaskStatus.COMPLETED
    assert ReplanLedger.load(restored).count == 1
    assert RecoveryHistory.load(restored).to_dict()
    assert len(restored.verifications) >= 2
    assert len(restored.tool_calls) == 2
