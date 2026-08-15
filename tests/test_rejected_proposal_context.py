"""Rejected proposals surfaced in replanning context.

A refused proposal is a different fact from an executed failure: it never ran.
These tests cover extraction from the existing ledger, rendering as a distinct
prompt section, safety, bounds, and the duplicate-rejection feedback loop.
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
from agentcore.core.replan import ReplanLedger, ReplanRecord
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.planning.failure_context import (
    MAX_REJECTED_PROPOSALS,
    RejectedProposal,
    build_failure_context,
)
from agentcore.planning.prompt import (
    build_system_prompt,
    build_user_prompt,
)
from agentcore.recovery.config import RecoveryConfig
from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-must-not-leak"


# -- helpers ----------------------------------------------------------------

def task_of(objective="goal", tool="act", variant="A") -> Task:
    task = Task(objective=objective)
    task.add_subtask(Subtask(description=f"try {variant}", tool_name=tool,
                             tool_args={"variant": variant}))
    return task


def state_with_rejections(*records: ReplanRecord) -> AgentState:
    state = AgentState()
    state.set_task(task_of())
    ledger = ReplanLedger()
    for record in records:
        ledger.count += 1
        ledger.records.append(record)
    ledger.save(state)
    return state


def rejection(attempt=1, *, kind="rejected_duplicate", detail="same strategy",
              signature="act") -> ReplanRecord:
    return ReplanRecord(attempt=attempt, reason="verification failed",
                        outcome=kind, detail=detail, signature=signature)


def prompt_for(state: AgentState) -> str:
    context = build_failure_context(state.current_task, state)
    return build_user_prompt("goal", [{"name": "act"}],
                             context=context.to_dict())


# ===========================================================================
# Extraction
# ===========================================================================

def test_rejected_proposal_is_extracted():
    state = state_with_rejections(rejection())
    context = build_failure_context(state.current_task, state)

    assert len(context.rejected) == 1
    entry = context.rejected[0]
    assert entry.kind == "rejected_duplicate"
    assert entry.strategy == "act"
    assert entry.executed is False
    assert context.has_rejections is True


def test_multiple_rejections_are_extracted():
    state = state_with_rejections(rejection(1, signature="act"),
                                  rejection(2, signature="act -> other"))
    context = build_failure_context(state.current_task, state)
    assert len(context.rejected) == 2


def test_structural_rejection_kind_preserved():
    state = state_with_rejections(rejection(kind="rejected",
                                            detail="no subtasks"))
    entry = build_failure_context(state.current_task, state).rejected[0]
    assert entry.kind == "rejected"
    assert "no subtasks" in entry.reason


def test_accepted_records_are_not_rejections():
    accepted = ReplanRecord(attempt=1, reason="r", outcome="accepted")
    state = state_with_rejections(accepted)
    context = build_failure_context(state.current_task, state)
    assert context.rejected == []
    assert context.has_rejections is False


def test_error_records_are_not_rejections():
    state = state_with_rejections(
        ReplanRecord(attempt=1, reason="r", outcome="error", detail="boom"))
    assert build_failure_context(state.current_task, state).rejected == []


def test_rejection_is_not_counted_as_an_executed_attempt():
    """A refused proposal must never appear among executed history."""
    state = state_with_rejections(rejection())
    context = build_failure_context(state.current_task, state)
    assert context.rejected
    assert context.previous == [], "nothing was executed for a rejection"


def test_no_history_yields_no_rejections():
    state = AgentState()
    state.set_task(task_of())
    context = build_failure_context(state.current_task, state)
    assert context.rejected == []
    assert context.has_rejections is False


# ===========================================================================
# Robustness
# ===========================================================================

def test_missing_scratch_is_safe():
    state = AgentState()
    state.set_task(task_of())
    state.scratch.clear()
    assert build_failure_context(state.current_task, state).rejected == []


@pytest.mark.parametrize("corrupt", [
    "not a dict", 12345, [], {"records": "nope"}, {"records": [None, 5, "x"]},
    {"records": [{"outcome": None}]},
])
def test_corrupt_ledger_does_not_crash(corrupt):
    state = AgentState()
    state.set_task(task_of())
    state.scratch["replan"] = corrupt
    context = build_failure_context(state.current_task, state)
    assert context.rejected == []
    assert context.latest is not None      # the rest of the context survives


def test_legacy_record_without_signature_is_handled():
    """Records written before the signature field existed still load."""
    legacy = {"attempt": 1, "reason": "r", "outcome": "rejected_duplicate",
              "detail": "same strategy"}
    record = ReplanRecord.from_dict(legacy)
    assert record.signature == ""

    state = state_with_rejections(record)
    entry = build_failure_context(state.current_task, state).rejected[0]
    assert entry.strategy == ""
    assert "same strategy" in entry.reason


def test_record_round_trip_preserves_signature():
    record = rejection(signature="act -> other")
    assert ReplanRecord.from_dict(record.to_dict()).signature == "act -> other"


def test_rejected_property_on_record():
    assert rejection().rejected is True
    assert ReplanRecord(attempt=1, reason="r", outcome="accepted").rejected \
        is False


# ===========================================================================
# Ordering and bounds
# ===========================================================================

def test_newest_rejection_appears_first():
    state = state_with_rejections(
        rejection(1, signature="oldest"),
        rejection(2, signature="middle"),
        rejection(3, signature="newest"))
    context = build_failure_context(state.current_task, state)
    assert [r.strategy for r in context.rejected] == ["newest", "middle",
                                                      "oldest"]


def test_rejections_are_bounded():
    state = state_with_rejections(
        *(rejection(i, signature=f"s{i}")
          for i in range(MAX_REJECTED_PROPOSALS * 3)))
    context = build_failure_context(state.current_task, state)
    assert len(context.rejected) == MAX_REJECTED_PROPOSALS


def test_long_reason_is_truncated():
    state = state_with_rejections(rejection(detail="X" * 2000))
    entry = build_failure_context(state.current_task, state).rejected[0]
    assert len(entry.reason) <= 201


# ===========================================================================
# Prompt rendering
# ===========================================================================

def test_section_appears_and_is_labelled_not_executed():
    prompt = prompt_for(state_with_rejections(rejection()))
    assert "REJECTED PROPOSALS" in prompt
    assert "NEVER EXECUTED" in prompt
    assert "executed: NO — refused before execution" in prompt


def test_section_absent_when_nothing_rejected():
    state = AgentState()
    state.set_task(task_of())
    assert "REJECTED PROPOSALS" not in prompt_for(state)


def test_section_shows_reason_and_strategy():
    prompt = prompt_for(state_with_rejections(
        rejection(detail="the same executable strategy as attempt 'x'",
                  signature="act -> other")))
    assert "act -> other" in prompt
    assert "same executable strategy" in prompt
    assert "replan attempt: 1" in prompt


def test_duplicate_and_structural_rejections_are_distinguished():
    duplicate = prompt_for(state_with_rejections(
        rejection(kind="rejected_duplicate")))
    structural = prompt_for(state_with_rejections(
        rejection(kind="rejected", detail="no subtasks")))
    assert "duplicate of an already-failed strategy" in duplicate
    assert "invalid proposal" in structural


def test_rejected_section_is_separate_from_executed_sections():
    """The model must not confuse a refusal with an execution failure."""
    state = AgentState()
    task = task_of()
    # an executed failure
    sub = task.subtasks[0]
    sub.transition_to(TaskStatus.RUNNING)
    sub.result = "output"
    sub.transition_to(TaskStatus.COMPLETED)
    sub.verification = VerificationResult(status=VerificationStatus.FAILED,
                                          checked_by="V", detail="bad")
    state.set_task(task)
    ledger = ReplanLedger()
    ledger.count = 1
    ledger.records.append(rejection())
    ledger.save(state)

    prompt = prompt_for(state)
    assert "LATEST FAILED ATTEMPT" in prompt
    assert "REJECTED PROPOSALS" in prompt
    # the refusal is not inside the executed-attempt section
    latest = prompt[prompt.index("LATEST FAILED ATTEMPT"):
                    prompt.index("REJECTED PROPOSALS")]
    assert "refused before execution" not in latest


def test_ordinary_planning_has_no_rejected_section():
    assert "REJECTED PROPOSALS" not in build_user_prompt(
        "goal", [{"name": "act"}])


# ===========================================================================
# Guidance
# ===========================================================================

def test_guidance_explains_rejected_proposals():
    guidance = build_system_prompt(replanning=True)
    assert "REJECTED PROPOSALS" in guidance
    assert "NOT executed" in guidance
    assert "Do not resubmit a proposal that was already refused" in guidance


def test_guidance_asks_for_a_different_executable_strategy():
    guidance = build_system_prompt(replanning=True)
    assert "change the executable strategy" in guidance
    assert "not merely the wording" in guidance


def test_base_prompt_unchanged_without_replanning():
    assert "REJECTED PROPOSALS" not in build_system_prompt()


# ===========================================================================
# Safety
# ===========================================================================

def test_unsafe_detail_is_withheld():
    """A detail quoting a credential-shaped key is not rendered."""
    state = state_with_rejections(
        rejection(detail=f'invalid argument api_key="{SECRET}"'))
    entry = build_failure_context(state.current_task, state).rejected[0]
    assert SECRET not in entry.reason
    assert "withheld" in entry.reason
    assert SECRET not in prompt_for(state)


@pytest.mark.parametrize("marker", ["api_key", "token", "password", "secret",
                                    "authorization", "APIKEY"])
def test_all_credential_markers_are_withheld(marker):
    state = state_with_rejections(rejection(detail=f"bad {marker} value xyz"))
    entry = build_failure_context(state.current_task, state).rejected[0]
    assert "withheld" in entry.reason


def test_safe_detail_is_preserved():
    state = state_with_rejections(rejection(detail="plan had no subtasks"))
    entry = build_failure_context(state.current_task, state).rejected[0]
    assert entry.reason == "plan had no subtasks"


def test_strategy_holds_tool_names_only():
    proposal = RejectedProposal(attempt=1, reason="r", strategy="act -> other")
    payload = proposal.to_dict()
    assert payload["strategy"] == "act -> other"
    assert payload["executed"] is False


def test_context_dict_omits_empty_rejections():
    from agentcore.planning.failure_context import FailureContext

    assert "rejected_proposals" not in FailureContext(objective="o").to_dict()


# ===========================================================================
# END-TO-END: the feedback loop this task exists to close
# ===========================================================================

class ActTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def spec(self):
        return tool_spec(
            "act", "Perform an action.",
            parameters=[ToolParameter("variant", ParamType.STRING,
                                      required=False, default=""),
                        ToolParameter("api_key", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.calls.append(dict(args))
        return ToolResult.success("done")

    @property
    def variants(self) -> list[str]:
        return [c.get("variant") for c in self.calls]


class FailUntilVerifier:
    def __init__(self, failures: int) -> None:
        self.remaining = failures

    def verify(self, subtask, state):
        if self.remaining > 0:
            self.remaining -= 1
            return VerificationResult(status=VerificationStatus.FAILED,
                                      checked_by="V", detail="not achieved")
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="V", detail="good")


class CapturingPlanner:
    """Proposes A, then A again (a duplicate), then B — recording prompts."""

    def __init__(self, *variants: str) -> None:
        self.variants = list(variants)
        self.replans = 0
        self.prompts: list[str] = []

    def plan(self, objective: str, *, context=None, replanning: bool = False):
        if context:
            self.prompts.append(build_user_prompt(
                objective, [{"name": "act"}], context=context))
        variant = self.variants.pop(0) if self.variants else "Z"
        task = Task(objective=objective)
        task.add_subtask(Subtask(
            description=f"an attempt described as {variant}",
            tool_name="act",
            tool_args={"variant": variant, "api_key": SECRET}))
        return task

    def replan(self, task: Task, state: AgentState) -> Task:
        self.replans += 1
        context = build_failure_context(task, state, max_replans=2).to_dict()
        return self.plan(task.objective, context=context)


def agent_for(tmp_path, planner, tool, verifier, *, max_replans=2) -> Agent:
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(recovery=True),
    ).validate()
    return Agent(config, AgentDependencies(tools=[tool], planner=planner,
                                           verifier=verifier))


def test_end_to_end_rejection_is_fed_back_to_the_planner(tmp_path):
    """A -> duplicate A rejected (never run) -> planner told -> B runs."""
    planner = CapturingPlanner("A", "A", "B")
    tool = ActTool()
    agent = agent_for(tmp_path, planner, tool, FailUntilVerifier(1))

    report = agent.execute("achieve the objective")

    # Plan A ran once; the duplicate never ran; Plan B ran once.
    assert tool.variants == ["A", "B"]
    assert tool.variants.count("A") == 1
    assert tool.variants.count("B") == 1
    assert planner.replans == 2

    # The rejection was recorded, then a new plan accepted.
    ledger = ReplanLedger.load(agent.state)
    assert [r.outcome for r in ledger.records] == ["rejected_duplicate",
                                                   "accepted"]
    assert ledger.records[0].signature == "act"

    # The second planning call was told its proposal had been refused.
    second_prompt = planner.prompts[-1]
    assert "REJECTED PROPOSALS" in second_prompt
    assert "NEVER EXECUTED" in second_prompt
    assert "refused before execution" in second_prompt
    assert "same executable strategy" in second_prompt

    # ...and no secret argument value leaked into that context.
    assert SECRET not in second_prompt

    assert report.status is TaskStatus.COMPLETED


def test_first_replan_prompt_has_no_rejections_yet(tmp_path):
    """Nothing has been refused before the first replan."""
    planner = CapturingPlanner("A", "B")
    agent = agent_for(tmp_path, planner, ActTool(), FailUntilVerifier(1))
    agent.execute("goal")

    assert planner.prompts, "the replan prompt was captured"
    assert "REJECTED PROPOSALS" not in planner.prompts[0]


def test_rejection_context_survives_resume(tmp_path):
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    planner = CapturingPlanner("A", "A", "B")
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=2, base_delay_seconds=0.0),
        features=FeatureFlags(recovery=True),
    ).validate()
    agent = Agent(config, AgentDependencies(tools=[ActTool()], planner=planner,
                                            verifier=FailUntilVerifier(1),
                                            state_store=store))
    agent.execute("goal")
    run_id = agent.state.run_id
    agent.shutdown()

    resumed = Agent(config, AgentDependencies(
        state_store=store)).resume(run_id)
    context = build_failure_context(resumed.current_task, resumed,
                                    max_replans=2)
    assert context.rejected, "the refusal is still known after resume"
    assert context.rejected[0].executed is False
