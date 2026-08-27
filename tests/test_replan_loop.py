"""Orchestration replan loop: budget, validation, history and safety.

The loop under test is::

    execute -> recovery says REPLAN -> Planner.replan -> validate -> execute

All fixtures are deterministic; no model or network is involved.
"""

from __future__ import annotations

import json
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
from agentcore.core.replan import ReplanLedger, ReplanRecord
from agentcore.errors import AgentError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.history import RecoveryHistory
from agentcore.state.models import AgentPhase, AgentState
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import (
    ParamType,
    Tool,
    ToolParameter,
    ToolResult,
    tool_spec,
)


# -- tools ------------------------------------------------------------------

class MutatingTool(Tool):
    """Mutating => retry is not provably safe => recovery prefers REPLAN."""

    def __init__(self, name: str = "mutate") -> None:
        self._name = name
        self.runs = 0

    @property
    def spec(self):
        # An optional declared parameter lets test planners propose materially
        # different plans (task 8 rejects byte-identical executable strategies).
        return tool_spec(self._name, "Writes something.",
                         parameters=[ToolParameter("variant", ParamType.STRING,
                                                   required=False, default="")],
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success(f"{self._name}-done")


# -- verifiers --------------------------------------------------------------

class AlwaysFailVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.FAILED,
                                  checked_by="TestVerifier",
                                  detail="expected outcome not achieved")


class FailUntilVerifier:
    """Fails the first ``failures`` verifications, then passes."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    def verify(self, subtask, state):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return VerificationResult(status=VerificationStatus.FAILED,
                                      checked_by="TestVerifier",
                                      detail="not yet")
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="TestVerifier", detail="good")


# -- planners ---------------------------------------------------------------

class CountingPlanner:
    """Produces a fresh single-step task each time and counts replans."""

    def __init__(self, tool: str = "mutate") -> None:
        self.tool = tool
        self._variant = 0
        self.replans = 0
        self.plans = 0
        self.contexts: list[dict[str, Any]] = []
        self.replan_states: list[AgentState] = []

    def _build(self, objective: str) -> Task:
        """Build a *materially different* plan each call.

        A real planner proposes a new approach when asked to replan; the
        varying argument models that, so these loop-mechanics tests are not
        tripped by duplicate-plan rejection (covered in its own suite).
        """
        self._variant += 1
        task = Task(objective=objective)
        task.add_subtask(Subtask(description=f"use {self.tool}",
                                 tool_name=self.tool,
                                 tool_args={"variant": f"v{self._variant}"}))
        return task

    def plan(self, objective: str, *, context: dict[str, Any] | None = None):
        self.plans += 1
        self.contexts.append(dict(context or {}))
        return self._build(objective)

    def replan(
        self,
        task: Task,
        state: AgentState,
        *,
        action_bindings=(),
    ) -> Task:
        self.replans += 1
        self.replan_states.append(state)
        replacement = self._build(task.objective)
        for offer in action_bindings:
            subtask = replacement.subtasks[0]
            if offer.tool_name == subtask.tool_name:
                replacement.metadata["plan"] = {
                    "annotations": {
                        subtask.id: {"action_binding_token": offer.token},
                    },
                }
                break
        return replacement


class RaisingPlanner(CountingPlanner):
    def replan(self, task, state):
        self.replans += 1
        raise RuntimeError("planner exploded")


class PlanningErrorPlanner(CountingPlanner):
    def replan(self, task, state):
        self.replans += 1
        from agentcore.planning.errors import PlanValidationError

        raise PlanValidationError("model proposed an unregistered tool")


class SameTaskPlanner(CountingPlanner):
    """Returns the very task that just failed — must be rejected."""

    def replan(self, task, state):
        self.replans += 1
        return task


class EmptyPlanPlanner(CountingPlanner):
    def replan(self, task, state):
        self.replans += 1
        return Task(objective=task.objective)  # no subtasks


class NonePlanner(CountingPlanner):
    def replan(self, task, state):
        self.replans += 1
        return None


class WrongTypePlanner(CountingPlanner):
    def replan(self, task, state):
        self.replans += 1
        return {"objective": "not a task"}


class BadDependencyPlanner(CountingPlanner):
    """Returns a task whose dependency graph is invalid."""

    def replan(self, task, state):
        self.replans += 1
        broken = Task(objective=task.objective)
        sub = Subtask(description="x", tool_name=self.tool)
        broken.subtasks.append(sub)          # bypass add_subtask checks
        sub.depends_on.append("ghost-id")
        return broken


class UnknownToolPlanner(CountingPlanner):
    def replan(self, task, state):
        self.replans += 1
        new = Task(objective=task.objective)
        new.add_subtask(Subtask(description="x", tool_name="does_not_exist"))
        return new


# -- helpers ----------------------------------------------------------------

def cfg(tmp_path, *, max_replans: int = 1,
        level: PermissionLevel = PermissionLevel.AUTONOMOUS,
        **recovery_kwargs: Any) -> AgentConfig:
    recovery_kwargs.setdefault("base_delay_seconds", 0.0)
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans, **recovery_kwargs),
        features=FeatureFlags(recovery=True),
    ).validate()


def build(tmp_path, planner, *, verifier=None, tools=None, max_replans=1,
          **kw):
    tools = tools if tools is not None else [MutatingTool()]
    agent = Agent(cfg(tmp_path, max_replans=max_replans, **kw),
                  AgentDependencies(tools=tools, planner=planner,
                                    verifier=verifier or AlwaysFailVerifier(),
                                    **journal_for_tools(tools)))
    return agent


# ===========================================================================
# Basic replan
# ===========================================================================

def test_replan_called_exactly_once_and_new_plan_executed(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)

    report = agent.execute("do the thing")

    assert planner.replans == 1
    assert tool.runs == 1          # durable action is observed, not rerun
    assert report.status is not TaskStatus.COMPLETED
    ledger = ReplanLedger.load(agent.state)
    assert ledger.count == 1
    assert ledger.records[-1].outcome == "accepted"


def test_no_replan_when_work_succeeds(tmp_path):
    """A verified success must never invoke the planner again."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool],
                  verifier=FailUntilVerifier(0), max_replans=3)

    report = agent.execute("do the thing")

    assert report.status is TaskStatus.COMPLETED
    assert planner.replans == 0
    assert tool.runs == 1
    assert ReplanLedger.load(agent.state).count == 0


def test_successful_replan_completes_the_objective(tmp_path):
    """Initial plan fails verification; the replanned plan passes."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool],
                  verifier=FailUntilVerifier(1), max_replans=2)

    report = agent.execute("do the thing")

    assert planner.replans == 1
    assert tool.runs == 1
    assert report.status is TaskStatus.COMPLETED
    assert agent.state.phase is AgentPhase.DONE
    subtask = agent.state.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.PASSED


def test_multiple_replans_until_success(tmp_path):
    """Two failed approaches, third succeeds; counts must be exact."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool],
                  verifier=FailUntilVerifier(2), max_replans=5)

    report = agent.execute("do the thing")

    assert planner.replans == 2
    assert tool.runs == 1
    assert report.status is TaskStatus.COMPLETED
    ledger = ReplanLedger.load(agent.state)
    assert ledger.count == 2
    assert [r.outcome for r in ledger.records] == ["accepted", "accepted"]


# ===========================================================================
# Budget semantics
# ===========================================================================

@pytest.mark.parametrize("budget", [0, 1, 2, 3])
def test_planner_called_exactly_budget_times(tmp_path, budget):
    """Never more than max_replans, and never fewer when failures persist."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=budget)

    agent.execute("do the thing")

    assert planner.replans == budget
    assert tool.runs == 1                   # replans observe durable truth
    assert ReplanLedger.load(agent.state).count == budget


def test_zero_budget_never_calls_planner(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=0)

    report = agent.execute("do the thing")

    assert planner.replans == 0
    assert tool.runs == 1
    assert ReplanLedger.load(agent.state).count == 0
    assert report.status is not TaskStatus.COMPLETED


def test_initial_planning_is_not_a_replan(tmp_path):
    planner = CountingPlanner()
    agent = build(tmp_path, planner, max_replans=0)
    agent.create_task("objective")
    assert planner.plans == 1
    assert ReplanLedger.load(agent.state).count == 0


def test_budget_is_not_reset_between_cycles(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=2)
    agent.execute("do the thing")
    # exactly at the limit, not beyond
    assert planner.replans == 2
    assert ReplanLedger.load(agent.state).exhausted(2)


def test_exhaustion_stops_calling_planner(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)
    agent.execute("do the thing")
    before = planner.replans

    # A second run on the same state must respect the spent budget.
    agent.run_task()
    assert planner.replans == before


def test_ledger_exhausted_semantics():
    ledger = ReplanLedger()
    assert ledger.exhausted(0) is True
    assert ledger.exhausted(1) is False
    ledger.begin("r", None)
    assert ledger.exhausted(1) is True
    assert ledger.remaining(3) == 2


# ===========================================================================
# Invalid replans
# ===========================================================================

@pytest.mark.parametrize("planner_cls,fragment", [
    (SameTaskPlanner, "same task"),
    (EmptyPlanPlanner, "no subtasks"),
    (NonePlanner, "no task"),
    (WrongTypePlanner, "not a Task"),
    (BadDependencyPlanner, "validation"),
])
def test_invalid_replan_is_rejected(tmp_path, planner_cls, fragment):
    planner, tool = planner_cls(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=3)

    report = agent.execute("do the thing")

    assert planner.replans == 1          # attempted once, then abandoned
    assert tool.runs == 1                # the bad plan never executed
    ledger = ReplanLedger.load(agent.state)
    assert ledger.records[-1].outcome == "rejected"
    assert fragment in ledger.records[-1].detail
    assert report.status is not TaskStatus.COMPLETED
    assert report.escalated is True


def test_rejected_replan_preserves_previous_task_and_history(tmp_path):
    planner, tool = SameTaskPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=2)
    original = planner.plan("do the thing")
    original_id = original.id

    agent.run_task(original)

    # the failed task is still the current one; nothing was wiped
    assert agent.state.current_task.id == original_id
    assert agent.state.current_task.subtasks[0].verification.status is \
        VerificationStatus.FAILED
    assert agent.state.verifications, "verification history retained"


def test_unregistered_tool_in_replan_does_not_execute(tmp_path):
    """A plan naming a missing tool must not silently run other work."""
    planner, tool = UnknownToolPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)

    report = agent.execute("do the thing")

    # The task validates structurally, so it is adopted, but the runner fails
    # it on the unknown tool rather than executing anything.
    assert tool.runs == 1
    assert report.status is not TaskStatus.COMPLETED


# ===========================================================================
# Planner errors
# ===========================================================================

def test_planner_exception_does_not_crash_or_fake_success(tmp_path):
    planner, tool = RaisingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=2)

    report = agent.execute("do the thing")

    assert planner.replans == 1
    assert report.status is not TaskStatus.COMPLETED
    assert report.escalated is True
    ledger = ReplanLedger.load(agent.state)
    assert ledger.records[-1].outcome == "error"
    assert "RuntimeError" in ledger.records[-1].detail
    # the failure is recorded on state, not swallowed
    assert any(e.type == "RuntimeError" for e in agent.state.errors)


def test_planning_error_is_handled_the_same_way(tmp_path):
    planner = PlanningErrorPlanner()
    agent = build(tmp_path, planner, max_replans=2)

    report = agent.execute("do the thing")

    assert planner.replans == 1
    assert report.escalated is True
    assert ReplanLedger.load(agent.state).records[-1].outcome == "error"


def test_failed_replan_still_consumes_budget(tmp_path):
    """A planner that always raises must not be retried forever."""
    planner, tool = RaisingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=3)

    agent.execute("do the thing")

    assert planner.replans == 1   # loop abandons after the failure
    assert ReplanLedger.load(agent.state).count == 1


def test_agent_state_remains_coherent_after_planner_error(tmp_path):
    planner = RaisingPlanner()
    agent = build(tmp_path, planner, max_replans=1)
    agent.execute("do the thing")

    assert agent.state.current_task is not None
    assert agent.state.phase in (AgentPhase.PLANNING, AgentPhase.ERROR,
                                 AgentPhase.EXECUTING, AgentPhase.RECOVERING)
    # state still serialises cleanly
    json.dumps(agent.state.to_dict(), default=str)


# ===========================================================================
# No duplicate execution
# ===========================================================================

def test_old_plan_is_not_executed_after_replacement(tmp_path):
    """Only the new plan runs after a replan; the old one is retired."""
    first, second = MutatingTool("mutate"), MutatingTool("other")

    class SwitchingPlanner(CountingPlanner):
        def replan(self, task, state):
            self.replans += 1
            new = Task(objective=task.objective)
            new.add_subtask(Subtask(description="use other",
                                    tool_name="other"))
            return new

    planner = SwitchingPlanner()
    agent = build(tmp_path, planner, tools=[first, second], max_replans=1)
    agent.execute("do the thing")

    assert first.runs == 1     # original plan ran once, never replayed
    assert second.runs == 1    # new plan ran once


def test_failed_subtask_not_re_executed_without_a_retry(tmp_path):
    """Recovery asked to replan, not retry: no second run of the same plan."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=0)
    agent.execute("do the thing")
    assert tool.runs == 1


def test_completed_run_is_not_replanned(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool],
                  verifier=FailUntilVerifier(0), max_replans=3)
    agent.execute("do the thing")
    assert planner.replans == 0
    assert tool.runs == 1


def test_one_replan_per_recovery_event(tmp_path):
    """Each execute cycle triggers at most one planner call."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=3)
    agent.execute("do the thing")
    # 3 replans for 4 execution cycles: never two calls for one failure
    assert planner.replans == 3
    assert tool.runs == 1


# ===========================================================================
# Phase transitions
# ===========================================================================

def test_phase_sequence_includes_recovering_then_planning(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)
    agent.execute("do the thing")

    phases = [h.data["to"] for h in agent.state.history
              if h.kind == "transition"]
    assert "recovering" in phases
    assert "planning" in phases
    # RECOVERING must precede PLANNING, which must precede the next EXECUTING
    rec = phases.index("recovering")
    plan_i = phases.index("planning", rec)
    assert "executing" in phases[plan_i:]


def test_illegal_phase_transitions_still_rejected():
    from agentcore.errors import StateError

    state = AgentState()
    state.transition_phase(AgentPhase.EXECUTING)
    state.transition_phase(AgentPhase.DONE)
    with pytest.raises(StateError):
        state.transition_phase(AgentPhase.PLANNING)


def test_successful_replan_ends_in_done_phase(tmp_path):
    planner = CountingPlanner()
    agent = build(tmp_path, planner, verifier=FailUntilVerifier(1),
                  max_replans=2)
    agent.execute("do the thing")
    assert agent.state.phase is AgentPhase.DONE


# ===========================================================================
# History preservation & persistence
# ===========================================================================

def test_previous_plan_is_archived(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)
    original = planner.plan("do the thing")
    original_id = original.id
    agent.run_task(original)

    ledger = ReplanLedger.load(agent.state)
    assert ledger.archived_plans
    assert ledger.archived_plans[0]["id"] == original_id
    assert ledger.records[0].previous_task_id == original_id
    assert ledger.records[0].new_task_id == agent.state.current_task.id


def test_verification_and_recovery_history_survive_replan(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)
    agent.execute("do the thing")

    # verifications from both cycles are retained
    assert len(agent.state.verifications) >= 2
    history = RecoveryHistory.load(agent.state)
    assert history.to_dict(), "recovery history retained across replans"


def test_replan_state_survives_serialisation(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=2)
    agent.execute("do the thing")

    restored = AgentState.from_dict(json.loads(
        json.dumps(agent.state.to_dict(), default=str)))
    ledger = ReplanLedger.load(restored)
    assert ledger.count == 2
    assert len(ledger.records) == 2
    assert ledger.archived_plans
    assert restored.current_task is not None
    assert RecoveryHistory.load(restored).to_dict()


def test_replan_state_survives_resume_via_store(tmp_path):
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    planner, tool = CountingPlanner(), MutatingTool()
    agent = Agent(cfg(tmp_path, max_replans=1),
                  AgentDependencies(tools=[tool], planner=planner,
                                    verifier=AlwaysFailVerifier(),
                                    state_store=store,
                                    **journal_for_tools([tool])))
    agent.execute("do the thing")
    run_id = agent.state.run_id
    agent.shutdown()

    resumed = Agent(cfg(tmp_path, max_replans=1),
                    AgentDependencies(state_store=store)).resume(run_id)
    assert ReplanLedger.load(resumed).count == 1
    assert resumed.current_task is not None


def test_resumed_run_respects_spent_budget(tmp_path):
    """Budget must not silently reset when a run is resumed."""
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    planner, tool = CountingPlanner(), MutatingTool()
    agent = Agent(cfg(tmp_path, max_replans=1),
                  AgentDependencies(tools=[tool], planner=planner,
                                    verifier=AlwaysFailVerifier(),
                                    state_store=store,
                                    **journal_for_tools([tool])))
    agent.execute("do the thing")
    run_id = agent.state.run_id
    agent.shutdown()

    planner2 = CountingPlanner()
    resumed_tool = MutatingTool()
    resumed_agent = Agent(cfg(tmp_path, max_replans=1),
                          AgentDependencies(tools=[resumed_tool],
                                            planner=planner2,
                                            verifier=AlwaysFailVerifier(),
                                            state_store=store,
                                            **journal_for_tools([resumed_tool])))
    resumed_agent.resume(run_id)
    resumed_agent.run_task()
    assert planner2.replans == 0, "spent budget must carry across resume"


def test_ledger_archive_is_bounded():
    from agentcore.core.replan import MAX_ARCHIVED_PLANS

    ledger = ReplanLedger()
    for i in range(MAX_ARCHIVED_PLANS * 2):
        task = Task(objective=f"o{i}")
        task.add_subtask(Subtask(description="x"))
        ledger.begin("reason", task)
        ledger.finish(reason="reason", outcome="accepted")
    assert len(ledger.archived_plans) == MAX_ARCHIVED_PLANS
    assert len(ledger.records) == MAX_ARCHIVED_PLANS
    assert ledger.count == MAX_ARCHIVED_PLANS * 2   # counter stays exact


def test_ledger_tolerates_missing_or_corrupt_scratch():
    assert ReplanLedger.load(AgentState()).count == 0
    state = AgentState()
    state.scratch["replan"] = "not a dict"
    assert ReplanLedger.load(state).count == 0


def test_replan_record_round_trip():
    record = ReplanRecord(attempt=2, reason="why", outcome="accepted",
                          previous_task_id="a", new_task_id="b")
    assert ReplanRecord.from_dict(record.to_dict()).attempt == 2


# ===========================================================================
# Replan context
# ===========================================================================

def test_planner_receives_state_with_failure_context(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)
    agent.execute("do the thing")

    assert planner.replan_states, "replan must receive the agent state"
    state = planner.replan_states[0]
    assert state.current_task is not None
    assert state.verifications, "verification results are visible to replan"
    assert state.tool_calls, "execution history is visible to replan"


def test_objective_is_preserved_across_replans(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=2)
    agent.execute("the original objective")
    assert agent.state.current_task.objective == "the original objective"


# ===========================================================================
# Safety
# ===========================================================================

def test_replan_does_not_bypass_permissions(tmp_path):
    """A replanned mutating action is still refused at OBSERVE level."""
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=2,
                  level=PermissionLevel.OBSERVE)

    report = agent.execute("do the thing")

    assert tool.runs == 0                     # never executed
    assert agent.policy.level is PermissionLevel.OBSERVE
    assert report.status is not TaskStatus.COMPLETED


def test_replan_cannot_fabricate_verification_success(tmp_path):
    planner, tool = CountingPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=1)
    agent.execute("do the thing")
    for record in agent.state.verifications:
        assert record["status"] != "passed"


def test_replan_loop_terminates_with_permanently_failing_planner(tmp_path):
    """The strongest infinite-loop guard: nothing ever succeeds."""
    planner, tool = SameTaskPlanner(), MutatingTool()
    agent = build(tmp_path, planner, tools=[tool], max_replans=50)
    report = agent.execute("do the thing")
    assert planner.replans == 1     # rejected immediately, loop abandoned
    assert tool.runs == 1
    assert report.status is not TaskStatus.COMPLETED


def test_report_is_serialisable_after_replan(tmp_path):
    planner = CountingPlanner()
    agent = build(tmp_path, planner, max_replans=1)
    payload = agent.execute("do the thing").to_dict()
    json.dumps(payload, default=str)
