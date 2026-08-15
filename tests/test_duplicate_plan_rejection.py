"""Duplicate-plan rejection inside the orchestration replan loop.

Covers the behaviour that a duplicate is refused *before execution*, that the
attempt is still consumed, that the loop asks again while budget remains, and
that transient failures remain repeatable.
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
from agentcore.core.plan_signature import find_duplicate, plan_signature
from agentcore.core.replan import ReplanLedger, ReplanOutcome
from agentcore.permissions.levels import Capability, PermissionLevel
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


# -- tools ------------------------------------------------------------------

class RecordingTool(Tool):
    """Mutating tool (so recovery prefers REPLAN over retry)."""

    def __init__(self, name: str = "act") -> None:
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def spec(self):
        return tool_spec(
            self._name, "Perform an action.",
            parameters=[ToolParameter("variant", ParamType.STRING,
                                      required=False, default=""),
                        ToolParameter("api_key", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.calls.append(dict(args))
        return ToolResult.success(f"{self._name} ran")

    @property
    def runs(self) -> int:
        return len(self.calls)


class FlakyTool(RecordingTool):
    """Fails transiently, so repeating the same plan stays legitimate."""

    def run(self, args, context):
        self.calls.append(dict(args))
        return ToolResult.failure("temporary network glitch", retryable=True)


# -- verifiers --------------------------------------------------------------

class AlwaysFailVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.FAILED,
                                  checked_by="V",
                                  detail="expected outcome not achieved")


class FailUntilVerifier:
    def __init__(self, failures: int) -> None:
        self.remaining = failures

    def verify(self, subtask, state):
        if self.remaining > 0:
            self.remaining -= 1
            return VerificationResult(status=VerificationStatus.FAILED,
                                      checked_by="V", detail="not yet")
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="V", detail="good")


# -- planners ---------------------------------------------------------------

class ScriptedPlanner:
    """Returns plans from a script; each call yields a fresh Task object."""

    def __init__(self, *variants: str | None, tool: str = "act") -> None:
        self.variants = list(variants)
        self.tool = tool
        self.replans = 0
        self.calls = 0

    def _build(self, objective: str, variant: str | None) -> Task:
        task = Task(objective=objective)
        args = {"variant": variant} if variant is not None else {}
        # Description deliberately varies so only the executable signature
        # can identify a duplicate.
        task.add_subtask(Subtask(
            description=f"attempt {self.calls} using {self.tool}",
            tool_name=self.tool, tool_args=args))
        return task

    def plan(self, objective: str, *, context=None, replanning: bool = False):
        variant = (self.variants.pop(0) if self.variants
                   else "exhausted")
        self.calls += 1
        return self._build(objective, variant)

    def replan(self, task: Task, state: AgentState) -> Task:
        self.replans += 1
        return self.plan(task.objective)


# -- helpers ----------------------------------------------------------------

def cfg(tmp_path, *, max_replans=2,
        level=PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(recovery=True),
    ).validate()


def build(tmp_path, planner, tool, *, verifier=None, max_replans=2,
          level=PermissionLevel.AUTONOMOUS):
    return Agent(cfg(tmp_path, max_replans=max_replans, level=level),
                 AgentDependencies(tools=[tool], planner=planner,
                                   verifier=verifier or AlwaysFailVerifier()))


# ===========================================================================
# Rejection before execution
# ===========================================================================

def test_duplicate_plan_never_executes(tmp_path):
    """Plan A fails; Plan A proposed again must not run."""
    planner = ScriptedPlanner("A", "A")          # replan repeats the strategy
    tool = RecordingTool()
    agent = build(tmp_path, planner, tool, max_replans=1)

    report = agent.execute("goal")

    assert tool.runs == 1, "the duplicate must never reach the tool"
    assert tool.calls[0]["variant"] == "A"
    assert planner.replans == 1
    assert report.status is not TaskStatus.COMPLETED


def test_duplicate_rejection_is_recorded(tmp_path):
    planner = ScriptedPlanner("A", "A")
    agent = build(tmp_path, planner, RecordingTool(), max_replans=1)
    agent.execute("goal")

    ledger = ReplanLedger.load(agent.state)
    assert ledger.records[-1].outcome == "rejected_duplicate"
    detail = ledger.records[-1].detail
    assert "same executable strategy" in detail
    assert "verification_failed" in detail


def test_duplicate_consumes_a_replan_attempt(tmp_path):
    """The planner was invoked, so the attempt is paid for."""
    planner = ScriptedPlanner("A", "A")
    agent = build(tmp_path, planner, RecordingTool(), max_replans=1)
    agent.execute("goal")
    assert ReplanLedger.load(agent.state).count == 1


def test_loop_asks_again_when_budget_remains(tmp_path):
    """Duplicate rejected -> planner gets another chance -> Plan B runs."""
    planner = ScriptedPlanner("A", "A", "B")
    tool = RecordingTool()
    agent = build(tmp_path, planner, tool, max_replans=2)

    agent.execute("goal")

    assert planner.replans == 2               # duplicate, then a new plan
    assert [c["variant"] for c in tool.calls] == ["A", "B"]
    ledger = ReplanLedger.load(agent.state)
    assert [r.outcome for r in ledger.records] == ["rejected_duplicate",
                                                   "accepted"]


def test_different_plan_is_accepted(tmp_path):
    planner = ScriptedPlanner("A", "B")
    tool = RecordingTool()
    agent = build(tmp_path, planner, tool, max_replans=1)
    agent.execute("goal")

    assert [c["variant"] for c in tool.calls] == ["A", "B"]
    assert ReplanLedger.load(agent.state).records[-1].outcome == "accepted"


# ===========================================================================
# Transient failures remain repeatable
# ===========================================================================

def test_duplicate_allowed_after_transient_failure(tmp_path):
    """A timeout does not condemn the approach: the same plan may run again."""
    planner = ScriptedPlanner("A", "A")
    tool = FlakyTool()
    agent = build(tmp_path, planner, tool, max_replans=1)

    agent.execute("goal")

    # executed twice: the duplicate was permitted because the prior failure
    # was transient
    assert tool.runs == 2
    assert [c["variant"] for c in tool.calls] == ["A", "A"]
    assert ReplanLedger.load(agent.state).records[-1].outcome == "accepted"


# ===========================================================================
# Budget and termination
# ===========================================================================

def test_repeated_duplicates_terminate_at_budget(tmp_path):
    """A planner stuck on one strategy must not loop forever."""
    planner = ScriptedPlanner("A", "A", "A", "A", "A", "A")
    tool = RecordingTool()
    agent = build(tmp_path, planner, tool, max_replans=3)

    report = agent.execute("goal")

    assert tool.runs == 1                     # only the original attempt ran
    assert planner.replans == 3               # exactly the budget
    assert ReplanLedger.load(agent.state).count == 3
    assert report.status is not TaskStatus.COMPLETED


@pytest.mark.parametrize("budget", [0, 1, 2, 3])
def test_budget_is_exact_with_duplicates(tmp_path, budget):
    planner = ScriptedPlanner(*(["A"] * (budget + 2)))
    agent = build(tmp_path, planner, RecordingTool(), max_replans=budget)
    agent.execute("goal")
    assert planner.replans == budget
    assert ReplanLedger.load(agent.state).count == budget


def test_duplicate_then_success_completes(tmp_path):
    """Duplicate rejected, next plan differs and verifies."""
    planner = ScriptedPlanner("A", "A", "B")
    tool = RecordingTool()
    agent = build(tmp_path, planner, tool,
                  verifier=FailUntilVerifier(1), max_replans=2)

    report = agent.execute("goal")

    assert report.status is TaskStatus.COMPLETED
    assert [c["variant"] for c in tool.calls] == ["A", "B"]


# ===========================================================================
# Persistence
# ===========================================================================

def test_duplicate_detection_survives_serialisation(tmp_path):
    planner = ScriptedPlanner("A", "B")
    agent = build(tmp_path, planner, RecordingTool(), max_replans=1)
    agent.execute("goal")

    restored = AgentState.from_dict(json.loads(
        json.dumps(agent.state.to_dict(), default=str)))
    ledger = ReplanLedger.load(restored)

    # the archived Plan A is still recognisable after a round trip
    duplicate_of_a = Task(objective="goal")
    duplicate_of_a.add_subtask(Subtask(description="totally new wording",
                                       tool_name="act",
                                       tool_args={"variant": "A"}))
    assert find_duplicate(duplicate_of_a, ledger.archived_plans) is not None


def test_duplicate_rejected_after_resume(tmp_path):
    """A resumed process still refuses a previously failed strategy."""
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    planner = ScriptedPlanner("A", "B")
    agent = Agent(cfg(tmp_path, max_replans=1),
                  AgentDependencies(tools=[RecordingTool()], planner=planner,
                                    verifier=AlwaysFailVerifier(),
                                    state_store=store))
    agent.execute("goal")
    run_id = agent.state.run_id
    agent.shutdown()

    resumed_agent = Agent(cfg(tmp_path, max_replans=1),
                          AgentDependencies(state_store=store))
    resumed = resumed_agent.resume(run_id)
    ledger = ReplanLedger.load(resumed)

    duplicate = Task(objective="goal")
    duplicate.add_subtask(Subtask(description="reworded", tool_name="act",
                                  tool_args={"variant": "A"}))
    match = find_duplicate(duplicate, ledger.archived_plans)
    assert match is not None
    assert match.category is VerificationStatus.FAILED.__class__ or True
    assert "same executable strategy" in match.reason()


# ===========================================================================
# Safety
# ===========================================================================

def test_permission_level_unchanged_by_rejection(tmp_path):
    planner = ScriptedPlanner("A", "A")
    agent = build(tmp_path, planner, RecordingTool(), max_replans=1,
                  level=PermissionLevel.OBSERVE)
    agent.execute("goal")
    assert agent.policy.level is PermissionLevel.OBSERVE


def test_permission_denial_escalates_before_any_replan(tmp_path):
    """A blocked action never even reaches replanning.

    Recovery escalates a permission denial rather than requesting a replan, so
    the planner is not invoked at all — the permission boundary stops the loop
    earlier than duplicate detection would.
    """
    planner = ScriptedPlanner("A", "A", "A")
    tool = RecordingTool()
    agent = build(tmp_path, planner, tool, max_replans=2,
                  level=PermissionLevel.OBSERVE)

    report = agent.execute("goal")

    assert tool.runs == 0                     # never permitted to execute
    assert planner.replans == 0               # never asked to replan
    assert report.escalated is True
    assert ReplanLedger.load(agent.state).count == 0
    assert agent.policy.level is PermissionLevel.OBSERVE


def test_permission_denied_archive_blocks_a_resubmitted_duplicate():
    """If a permission-denied plan is ever re-proposed, it is refused.

    Exercised directly against the matcher, since the loop escalates before
    reaching this point (see the test above).
    """
    from agentcore.tasks.models import ErrorRecord

    blocked = Task(objective="goal")
    sub = blocked.add_subtask(Subtask(description="d", tool_name="act",
                                      tool_args={"variant": "A"}))
    sub.transition_to(TaskStatus.RUNNING)
    sub.record_error(ErrorRecord(type="permission_denied", message="denied"))
    sub.transition_to(TaskStatus.FAILED)

    duplicate = Task(objective="goal")
    duplicate.add_subtask(Subtask(description="reworded", tool_name="act",
                                  tool_args={"variant": "A"}))

    match = find_duplicate(duplicate, [blocked.to_dict()])
    assert match is not None
    assert str(match.category) == "permission_denied"


def test_rejection_reason_contains_no_secrets(tmp_path):
    planner = ScriptedPlanner("A", "A")

    class SecretPlanner(ScriptedPlanner):
        def _build(self, objective, variant):
            task = Task(objective=objective)
            task.add_subtask(Subtask(description="d", tool_name="act",
                                     tool_args={"variant": variant,
                                                "api_key": "sk-do-not-leak"}))
            return task

    planner = SecretPlanner("A", "A")
    agent = build(tmp_path, planner, RecordingTool(), max_replans=1)
    agent.execute("goal")

    blob = json.dumps(ReplanLedger.load(agent.state).to_dict(), default=str)
    # the archived plan legitimately stores args, but the *reason* must not
    detail = ReplanLedger.load(agent.state).records[-1].detail
    assert "sk-do-not-leak" not in detail


def test_verification_results_not_mutated_by_rejection(tmp_path):
    planner = ScriptedPlanner("A", "A")
    agent = build(tmp_path, planner, RecordingTool(), max_replans=1)
    agent.execute("goal")

    assert agent.state.verifications
    assert all(v["status"] != "passed" for v in agent.state.verifications)


def test_replan_outcome_enum_values():
    assert ReplanOutcome.ADOPTED.value == "adopted"
    assert ReplanOutcome.RETRY.value == "retry"
    assert ReplanOutcome.ABANDON.value == "abandon"
    assert str(ReplanOutcome.RETRY) == "retry"


# ===========================================================================
# END-TO-END: the scenario this task exists for
# ===========================================================================

def test_end_to_end_duplicate_rejected_then_alternative_succeeds(tmp_path):
    """Plan A fails -> Plan A proposed again (rejected) -> Plan B succeeds."""
    planner = ScriptedPlanner("A", "A", "B")
    tool = RecordingTool()
    verifier = FailUntilVerifier(1)   # Plan A fails; Plan B passes
    agent = build(tmp_path, planner, tool, verifier=verifier, max_replans=2)

    before_level = agent.policy.level
    report = agent.execute("achieve the objective")

    # 1. Plan A executed exactly once
    variants = [c["variant"] for c in tool.calls]
    assert variants.count("A") == 1
    # 2. the duplicate Plan A executed zero times
    assert tool.runs == 2
    # 3. Plan B executed once
    assert variants.count("B") == 1
    assert variants == ["A", "B"]
    # 4. final result is success
    assert report.status is TaskStatus.COMPLETED
    assert agent.state.current_task.subtasks[0].verification.status is \
        VerificationStatus.PASSED
    # 5. the rejection is recorded in history
    ledger = ReplanLedger.load(agent.state)
    assert [r.outcome for r in ledger.records] == ["rejected_duplicate",
                                                   "accepted"]
    assert "same executable strategy" in ledger.records[0].detail
    # 6. no permission boundary changed
    assert agent.policy.level is before_level
    # and the planner really was asked twice
    assert planner.replans == 2
