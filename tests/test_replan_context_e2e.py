"""Enriched replan context driving the real autonomous loop.

Proves the point of this task: across repeated attempts the planner is told
*what was already tried and why it failed*, so it can choose differently.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from conftest import journal_for_tools

from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.replan import ReplanLedger
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.planning.planner import LLMPlanner
from agentcore.recovery.config import RecoveryConfig
from agentcore.state.models import AgentState
from agentcore.tasks.models import TaskStatus, VerificationStatus
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-live-must-not-leak"


class ScriptedModel:
    """Serves plans in order, recording every prompt and system message."""

    def __init__(self, *plans: str) -> None:
        self.plans = list(plans)
        self.prompts: list[str] = []
        self.systems: list[str | None] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.prompts.append(prompt)
        self.systems.append(system)
        if not self.plans:
            raise AssertionError("ScriptedModel exhausted")
        response = self.plans.pop(0)
        offers = re.findall(
            r"token=(wfb_[0-9a-f]{12}); prior_subtask_id=sub_[0-9a-f]{12}; "
            r"tool_name=([a-zA-Z0-9_.-]+)",
            prompt,
        )
        if offers:
            payload = json.loads(response)
            for step in payload.get("steps", []):
                matches = [
                    token for token, tool_name in offers
                    if tool_name == step.get("tool_name")
                ]
                if len(matches) == 1:
                    step["action_binding_token"] = matches[0]
            response = json.dumps(payload)
        return response


class WriteTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def spec(self):
        return tool_spec(
            "write_note", "Write a note and return its text.",
            parameters=[ToolParameter("text", ParamType.STRING),
                        ToolParameter("api_key", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.calls.append(dict(args))
        return ToolResult.success(args["text"])


class GuardedTool(WriteTool):
    """Same tool but requiring a capability, so OBSERVE level denies it."""

    @property
    def spec(self):
        return tool_spec(
            "write_note", "Write a note.",
            parameters=[ToolParameter("text", ParamType.STRING)],
            capabilities=[Capability.FS_WRITE], read_only=False)


def plan_json(description: str, text: str, expected: str, *,
              objective: str, extra_args: dict[str, Any] | None = None) -> str:
    args: dict[str, Any] = {"text": text}
    if extra_args:
        args.update(extra_args)
    return json.dumps({"objective": objective, "steps": [{
        "id": "s1", "description": description, "tool_name": "write_note",
        "tool_args": args, "expected_result": expected,
        "requires_verification": True}]})


def config(tmp_path, *, max_replans=2,
           level=PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(planning=True, verification=True, recovery=True),
    ).validate()


def build(tmp_path, model, tool, *, max_replans=2,
          level=PermissionLevel.AUTONOMOUS):
    """Wire an agent whose model serves *planning only*.

    Verification is left deterministic: a scripted planner model cannot also
    answer verification judgements, and mixing the two roles would make the
    prompt assertions ambiguous. The real model-assisted verifier is covered
    by the verification test suite.
    """
    cfg = config(tmp_path, max_replans=max_replans, level=level)
    from agentcore.verification.verifier import Verifier

    agent = Agent(cfg, AgentDependencies(
        tools=[tool], model=model, verifier=Verifier(model=None),
        **journal_for_tools([tool], expose_bounded_text_result=True)))
    agent.planner = LLMPlanner(model, agent.registry, max_replans=max_replans)
    return agent


OBJECTIVE = "record the launch date"
EXPECTED = "the launch date is 2026-03-01"


# ===========================================================================
# The behavioural requirement: distinguish attempt 1 from attempt 2
# ===========================================================================

def test_prompt_distinguishes_two_earlier_failures(tmp_path):
    """Attempt 1 (approach A, reason X) vs attempt 2 (approach B, reason Y).

    The third planning call must show *both*, and mark which one just failed.
    """
    model = ScriptedModel(
        plan_json("approach A: write a placeholder", "TODO placeholder",
                  EXPECTED, objective=OBJECTIVE),
        plan_json("approach B: write the year only", "2026",
                  EXPECTED, objective=OBJECTIVE),
        plan_json("approach C: write the full date", EXPECTED,
                  EXPECTED, objective=OBJECTIVE),
    )
    tool = WriteTool()
    agent = build(tmp_path, model, tool, max_replans=2)

    agent.execute(OBJECTIVE)

    assert len(model.prompts) == 3, "initial plan + two replans"
    third = model.prompts[2]

    # the latest failure (approach B) is clearly marked as the most recent
    latest_section = third[third.index("LATEST FAILED ATTEMPT"):
                           third.index("EARLIER FAILED ATTEMPTS")]
    assert "2026" in latest_section
    assert "approach B" in latest_section

    # the older failure (approach A) appears in the historical section
    earlier_section = third[third.index("EARLIER FAILED ATTEMPTS"):]
    assert "TODO placeholder" in earlier_section
    assert "approach A" in earlier_section

    # both attempts carry their own verification evidence
    assert "verification:" in latest_section
    assert "verification:" in earlier_section
    assert third.count("verification:") >= 2
    # and the model is told this is a replan
    assert "REPLANNING" in model.systems[2]


def test_second_replan_sees_first_replan_reason(tmp_path):
    model = ScriptedModel(
        plan_json("approach A", "A", EXPECTED, objective=OBJECTIVE),
        plan_json("approach B", "B", EXPECTED, objective=OBJECTIVE),
        plan_json("approach C", EXPECTED, EXPECTED, objective=OBJECTIVE),
    )
    agent = build(tmp_path, model, WriteTool(), max_replans=2)
    agent.execute(OBJECTIVE)

    third = model.prompts[2]
    assert "replan reason:" in third
    assert "RECOVERY DECISIONS ALREADY TAKEN" in third
    assert "verification_failed" in third


# ===========================================================================
# End-to-end: history-informed replan preserves action continuity
# ===========================================================================

def test_full_loop_replan_uses_history_without_repeating_mutation(tmp_path):
    """Changed same-tool text cannot fabricate a second logical action."""
    model = ScriptedModel(
        plan_json("write a placeholder", "TODO later", EXPECTED,
                  objective=OBJECTIVE),
        plan_json("write the real date", EXPECTED, EXPECTED,
                  objective=OBJECTIVE),
    )
    tool = WriteTool()
    agent = build(tmp_path, model, tool, max_replans=1)

    report = agent.execute(OBJECTIVE)

    assert report.status is not TaskStatus.COMPLETED
    assert [c["text"] for c in tool.calls] == ["TODO later"]
    assert len(model.prompts) == 2

    # the replan prompt contained the first failure
    assert "TODO later" in model.prompts[1]
    assert "LATEST FAILED ATTEMPT" in model.prompts[1]

    subtask = agent.state.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.FAILED
    assert ReplanLedger.load(agent.state).count == 1


def test_first_plan_has_no_history_and_no_replan_guidance(tmp_path):
    """No unnecessary context on the initial planning call."""
    model = ScriptedModel(plan_json("do it", EXPECTED, EXPECTED,
                                    objective=OBJECTIVE))
    agent = build(tmp_path, model, WriteTool(), max_replans=1)
    agent.execute(OBJECTIVE)

    assert len(model.prompts) == 1
    assert "LATEST FAILED ATTEMPT" not in model.prompts[0]
    assert "REPLANNING" not in (model.systems[0] or "")


def test_no_extra_model_calls_for_history(tmp_path):
    """History must ride along with the existing planning call."""
    model = ScriptedModel(
        plan_json("a", "A", EXPECTED, objective=OBJECTIVE),
        plan_json("b", EXPECTED, EXPECTED, objective=OBJECTIVE),
    )
    agent = build(tmp_path, model, WriteTool(), max_replans=1)
    agent.execute(OBJECTIVE)
    # exactly one call per planning cycle: no summarisation call
    assert len(model.prompts) == 2


# ===========================================================================
# Safety
# ===========================================================================

def test_secrets_from_history_never_reach_the_model(tmp_path):
    """A secret-shaped tool arg from a failed attempt must be redacted."""
    model = ScriptedModel(
        plan_json("first", "attempt one", EXPECTED, objective=OBJECTIVE,
                  extra_args={"api_key": SECRET}),
        plan_json("second", EXPECTED, EXPECTED, objective=OBJECTIVE),
    )
    tool = WriteTool()
    agent = build(tmp_path, model, tool)
    agent.execute(OBJECTIVE)

    assert tool.calls[0]["api_key"] == SECRET      # really was executed with it
    replan_prompt = model.prompts[1]
    assert SECRET not in replan_prompt
    assert "***" in replan_prompt


def test_permission_denial_context_reaches_planner(tmp_path):
    """A blocked action is reported as such, with no workaround suggested."""
    plan = plan_json("write", "anything", EXPECTED, objective=OBJECTIVE)
    model = ScriptedModel(plan, plan, plan)
    tool = GuardedTool()
    agent = build(tmp_path, model, tool, max_replans=2,
                  level=PermissionLevel.OBSERVE)

    agent.execute(OBJECTIVE)

    assert tool.calls == []                       # never executed
    assert agent.policy.level is PermissionLevel.OBSERVE
    if len(model.prompts) > 1:
        replan_prompt = model.prompts[1]
        assert "permission" in replan_prompt.lower()
        assert "circumvent" in (model.systems[1] or "")


def test_history_context_survives_resume(tmp_path):
    """A resumed run still gives the planner the earlier failures."""
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    model = ScriptedModel(
        plan_json("approach A", "A output", EXPECTED, objective=OBJECTIVE),
        plan_json("approach B", "B output", EXPECTED, objective=OBJECTIVE),
    )
    tool = WriteTool()
    agent = Agent(config(tmp_path, max_replans=1),
                  AgentDependencies(
                      tools=[tool], model=model, state_store=store,
                      **journal_for_tools(
                          [tool], expose_bounded_text_result=True)))
    agent.planner = LLMPlanner(model, agent.registry, max_replans=1)
    agent.execute(OBJECTIVE)
    run_id = agent.state.run_id
    agent.shutdown()

    restored = Agent(config(tmp_path, max_replans=1),
                     AgentDependencies(state_store=store)).resume(run_id)

    from agentcore.planning.failure_context import build_failure_context

    context = build_failure_context(restored.current_task, restored,
                                    max_replans=1)
    assert context.previous, "earlier attempts survive serialisation"
    assert context.recovery_decisions
    assert context.replan_count == 1


def test_planner_recovers_when_history_is_corrupt(tmp_path):
    """A corrupt scratch must degrade context, not break replanning."""
    model = ScriptedModel(
        plan_json("a", "A", EXPECTED, objective=OBJECTIVE),
        plan_json("b", EXPECTED, EXPECTED, objective=OBJECTIVE),
    )
    agent = build(tmp_path, model, WriteTool(), max_replans=1)

    original_run = agent.runner.run

    def corrupting_run(task, state):
        report = original_run(task, state)
        state.scratch["replan"] = "corrupted"
        state.scratch["recovery"] = 12345
        return report

    agent.runner.run = corrupting_run
    report = agent.execute(OBJECTIVE)

    assert report.status is not TaskStatus.COMPLETED
    assert len(model.prompts) == 3


def test_validation_still_applies_to_replanned_plans(tmp_path):
    """Enriched context must not weaken plan validation."""
    bad_plan = json.dumps({"objective": OBJECTIVE, "steps": [{
        "id": "s1", "description": "use a tool that does not exist",
        "tool_name": "definitely_not_registered", "tool_args": {}}]})
    model = ScriptedModel(
        plan_json("a", "A", EXPECTED, objective=OBJECTIVE),
        bad_plan,
    )
    tool = WriteTool()
    agent = build(tmp_path, model, tool, max_replans=1)

    report = agent.execute(OBJECTIVE)

    assert len(tool.calls) == 1                   # invalid plan never ran
    assert report.status is not TaskStatus.COMPLETED
