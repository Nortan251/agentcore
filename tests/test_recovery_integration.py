"""Recovery integrated with the runner and Agent: the four required scenarios,
safety boundaries, feature flag, and persistence.
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
from agentcore.core.agent import Agent, AgentDependencies as _AgentDependencies
from agentcore.core.defaults import NoRetryStrategy
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.decision import RecoveryAction
from agentcore.recovery.history import RecoveryHistory
from agentcore.recovery.policy import RecoveryPolicy
from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import Tool, ToolResult, tool_spec


def AgentDependencies(*, tools=None, **kwargs):
    """Build explicit journal semantics for this module's fake mutations."""
    resolved = list(tools) if tools is not None else None
    return _AgentDependencies(
        tools=resolved,
        **kwargs,
        **journal_for_tools(resolved or []),
    )


# -- tools ------------------------------------------------------------------

class FlakyTool(Tool):
    """Read-only tool that fails a set number of times, then succeeds."""

    def __init__(self, fails: int = 1, *, distinct: bool = False) -> None:
        self.remaining = fails
        self.runs = 0
        self.distinct = distinct

    @property
    def spec(self):
        return tool_spec("flaky", "A flaky read-only tool.", read_only=True)

    def run(self, args, context):
        self.runs += 1
        if self.remaining > 0:
            self.remaining -= 1
            suffix = f" #{self.runs}" if self.distinct else ""
            return ToolResult.failure(f"temporary glitch{suffix}",
                                      retryable=True)
        return ToolResult.success("worked")


class DestructiveTool(Tool):
    """Irreversible tool: retrying is unsafe by declaration."""

    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("wipe", "Deletes things.",
                         capabilities=[Capability.FS_DELETE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.failure("disk error", retryable=True)


class MutatingTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("mutate", "Writes things.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("written")


class GoodTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("good", "Always succeeds.", read_only=True)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("fine")


# -- verifiers --------------------------------------------------------------

class AlwaysFailVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.FAILED,
                                  checked_by="TestVerifier",
                                  detail="expected outcome not achieved")


class FailThenPassVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, subtask, state):
        self.calls += 1
        status = (VerificationStatus.FAILED if self.calls == 1
                  else VerificationStatus.PASSED)
        return VerificationResult(status=status, checked_by="TestVerifier",
                                  detail=f"call {self.calls}")


class UnverifiedVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.UNVERIFIED,
                                  checked_by="TestVerifier",
                                  detail="insufficient evidence")


# -- helpers ----------------------------------------------------------------

def cfg(tmp_path, *, recovery=True, level=PermissionLevel.AUTONOMOUS,
        **recovery_kwargs: Any) -> AgentConfig:
    recovery_kwargs.setdefault("base_delay_seconds", 0.0)
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(**recovery_kwargs),
        features=FeatureFlags(recovery=recovery),
    ).validate()


def one_step_task(tool: str, objective: str = "goal") -> Task:
    task = Task(objective=objective)
    task.add_subtask(Subtask(description=f"run {tool}", tool_name=tool))
    return task


def actions(report) -> list[str]:
    return [d["action"] for d in report.decisions]


# ===========================================================================
# Scenario 1: transient failure -> RETRY -> success
# ===========================================================================

def test_scenario_1_transient_failure_retries_then_succeeds(tmp_path):
    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))

    assert report.status is TaskStatus.COMPLETED
    assert tool.runs == 2
    assert report.retried == 1
    assert actions(report) == ["retry"]
    subtask = agent.state.current_task.subtasks[0]
    assert subtask.status is TaskStatus.COMPLETED
    assert subtask.result == "worked"
    assert subtask.attempts == 2


def test_retry_uses_backoff_without_real_sleeping(tmp_path):
    """Delay is computed and passed to an injected sleep, not time.sleep."""
    slept: list[float] = []
    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path, base_delay_seconds=2.0),
                  AgentDependencies(tools=[tool]))
    agent.runner._sleep = slept.append
    report = agent.run_task(one_step_task("flaky"))

    assert report.status is TaskStatus.COMPLETED
    assert slept == [2.0]


# ===========================================================================
# Scenario 2: verification failure -> REPLAN
# ===========================================================================

def test_scenario_2_verification_failure_requests_replan(tmp_path):
    """A mutating tool whose verification fails cannot be safely retried.

    Asserted at the runner level: the runner is what *emits* the signal.
    Consuming it is the orchestration layer's job (see the replan-loop tests),
    so going through ``Agent.run_task`` here would service the request and
    clear the flag before this test could observe it.
    """
    tool = MutatingTool()
    agent = Agent(cfg(tmp_path),
                  AgentDependencies(tools=[tool],
                                    verifier=AlwaysFailVerifier()))
    task = one_step_task("mutate")
    report = agent.runner.run(task, agent.state)

    assert report.replan_requested is True
    assert "replan" in actions(report)
    assert report.status is TaskStatus.BLOCKED
    assert tool.runs == 1  # not repeated


def test_verification_failure_on_safe_tool_retries_then_passes(tmp_path):
    """Read-only work that fails verification is retried, then verified."""
    tool = GoodTool()
    verifier = FailThenPassVerifier()
    agent = Agent(cfg(tmp_path),
                  AgentDependencies(tools=[tool], verifier=verifier))
    report = agent.run_task(one_step_task("good"))

    assert tool.runs == 2
    assert verifier.calls == 2
    assert report.retried == 1
    assert report.status is TaskStatus.COMPLETED
    subtask = agent.state.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.PASSED


def test_unverified_outcome_is_not_treated_as_success(tmp_path):
    tool = GoodTool()
    agent = Agent(cfg(tmp_path),
                  AgentDependencies(tools=[tool],
                                    verifier=UnverifiedVerifier()))
    report = agent.run_task(one_step_task("good"))

    assert report.status is not TaskStatus.COMPLETED
    assert report.replan_requested or report.escalated
    subtask = agent.state.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.UNVERIFIED


# ===========================================================================
# Scenario 3: journaled mutation remains first-attempt-only
# ===========================================================================

def test_scenario_3_journaled_mutation_is_not_retried(tmp_path):
    tool = DestructiveTool()
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("wipe"))

    assert report.escalated is True
    assert actions(report) == ["replan"]
    assert tool.runs == 1  # irreversible action never repeated
    assert not agent.state.pending_actions


def test_permission_denial_escalates_and_never_reruns(tmp_path):
    tool = MutatingTool()
    agent = Agent(cfg(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("mutate"))

    assert report.escalated is True
    assert tool.runs == 0            # blocked before execution
    assert report.retried == 0
    assert agent.policy.level is PermissionLevel.OBSERVE  # unchanged


# ===========================================================================
# Scenario 4: repeated failures exhaust attempts -> STOP / ESCALATE
# ===========================================================================

def test_scenario_4_exhausted_attempts_escalate(tmp_path):
    tool = FlakyTool(fails=99, distinct=True)  # distinct so it is not "repeated"
    agent = Agent(cfg(tmp_path, max_attempts=3, max_replans=0),
                  AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))

    assert tool.runs == 3, "retries must stop at max_attempts"
    assert report.escalated is True
    assert actions(report) == ["retry", "retry", "escalate"]
    # The subtask really did fail, so FAILED (not BLOCKED) is the honest
    # status; the escalation is carried alongside it on the report.
    assert report.status is TaskStatus.FAILED
    assert agent.state.pending_actions


def test_exhaustion_can_stop_without_escalating(tmp_path):
    tool = FlakyTool(fails=99, distinct=True)
    agent = Agent(cfg(tmp_path, max_attempts=2, max_replans=0,
                      escalate_on_exhaustion=False),
                  AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))

    assert tool.runs == 2
    assert report.escalated is False
    assert actions(report)[-1] == "stop"
    assert report.status is TaskStatus.FAILED


def test_identical_repeated_failure_stops_early(tmp_path):
    """The same error twice triggers a rethink rather than burning attempts."""
    tool = FlakyTool(fails=99, distinct=False)
    agent = Agent(cfg(tmp_path, max_attempts=10),
                  AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))

    assert tool.runs < 10, "must not consume the whole budget"
    assert report.replan_requested or report.escalated


def test_no_infinite_retry_even_with_large_budget(tmp_path):
    tool = FlakyTool(fails=10_000, distinct=True)
    agent = Agent(cfg(tmp_path, max_attempts=4, max_replans=0),
                  AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))
    assert tool.runs == 4
    assert report.status in (TaskStatus.BLOCKED, TaskStatus.FAILED)


# -- multi-step behaviour ---------------------------------------------------

def test_replan_request_halts_further_steps(tmp_path):
    """Later steps must not run while recovery is questioning the approach."""
    mutate, good = MutatingTool(), GoodTool()
    agent = Agent(cfg(tmp_path),
                  AgentDependencies(tools=[mutate, good],
                                    verifier=AlwaysFailVerifier()))
    task = Task(objective="two steps")
    first = task.add_subtask(Subtask(description="a", tool_name="mutate"))
    task.add_subtask(Subtask(description="b", tool_name="good",
                             depends_on=[first.id]))
    # Runner-level: the loop must stop at the questioned step.
    report = agent.runner.run(task, agent.state)

    assert report.replan_requested is True
    assert good.runs == 0


def test_successful_verified_work_triggers_no_recovery(tmp_path):
    tool = GoodTool()
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("good"))

    assert report.status is TaskStatus.COMPLETED
    assert report.decisions == []
    assert report.retried == 0
    assert tool.runs == 1


# -- feature flag -----------------------------------------------------------

def test_flag_disabled_uses_no_retry_strategy(tmp_path):
    agent = Agent(cfg(tmp_path, recovery=False))
    assert isinstance(agent.recovery, NoRetryStrategy)


def test_flag_enabled_uses_real_policy(tmp_path):
    agent = Agent(cfg(tmp_path, recovery=True))
    assert isinstance(agent.recovery, RecoveryPolicy)


def test_flag_disabled_preserves_previous_behaviour(tmp_path):
    """Without the flag, a failure is simply a failure — no retry."""
    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path, recovery=False),
                  AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))

    assert report.status is TaskStatus.FAILED
    assert tool.runs == 1
    assert report.retried == 0
    assert report.decisions == []


def test_injected_strategy_wins_over_flag(tmp_path):
    custom = NoRetryStrategy()
    for flag in (True, False):
        agent = Agent(cfg(tmp_path, recovery=flag),
                      AgentDependencies(recovery=custom))
        assert agent.recovery is custom


def test_legacy_boolean_strategy_still_works(tmp_path):
    """A strategy exposing only should_retry must keep functioning."""
    class LegacyRetryOnce:
        def should_retry(self, subtask, state):
            return subtask.attempts < 2

    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path, recovery=False),
                  AgentDependencies(tools=[tool],
                                    recovery=LegacyRetryOnce()))
    report = agent.run_task(one_step_task("flaky"))

    assert report.status is TaskStatus.COMPLETED
    assert tool.runs == 2
    assert report.decisions == []  # legacy path records no structured decisions


# -- safety -----------------------------------------------------------------

def test_recovery_holds_no_executor(tmp_path):
    agent = Agent(cfg(tmp_path))
    assert "executor" not in vars(agent.recovery)
    assert not any("Executor" in type(v).__name__
                   for v in vars(agent.recovery).values())


def test_recovery_does_not_execute_tools_itself(tmp_path):
    """Every run of the tool must come from the runner, one per attempt."""
    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    report = agent.run_task(one_step_task("flaky"))
    assert tool.runs == report.retried + 1


def test_recovery_cannot_fabricate_verification_success(tmp_path):
    tool = MutatingTool()
    agent = Agent(cfg(tmp_path),
                  AgentDependencies(tools=[tool],
                                    verifier=AlwaysFailVerifier()))
    agent.run_task(one_step_task("mutate"))
    subtask = agent.state.current_task.subtasks[0]
    assert subtask.verification.status is VerificationStatus.FAILED
    assert subtask.verification.checked_by == "TestVerifier"


def test_recovery_does_not_alter_permission_level(tmp_path):
    agent = Agent(cfg(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(tools=[MutatingTool()]))
    agent.run_task(one_step_task("mutate"))
    assert agent.policy.level is PermissionLevel.OBSERVE
    assert agent.policy.config.level is PermissionLevel.OBSERVE


def test_escalation_records_no_secrets(tmp_path):
    tool = DestructiveTool()
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    task = Task(objective="wipe")
    task.add_subtask(Subtask(description="w", tool_name="wipe",
                             tool_args={"api_key": "sk-should-not-leak"}))
    report = agent.run_task(task)
    blob = json.dumps(report.to_dict()) + json.dumps(
        [a.to_dict() for a in agent.state.pending_actions])
    assert "sk-should-not-leak" not in blob


# -- persistence ------------------------------------------------------------

def test_recovery_history_persists_and_resumes(tmp_path):
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path),
                  AgentDependencies(tools=[tool], state_store=store))
    agent.run_task(one_step_task("flaky"))
    run_id = agent.state.run_id
    agent.shutdown()

    resumed = Agent(cfg(tmp_path),
                    AgentDependencies(state_store=store)).resume(run_id)
    history = RecoveryHistory.load(resumed)
    subtask_id = resumed.current_task.subtasks[0].id
    assert history.retries(subtask_id) == 1


def test_history_survives_state_round_trip(tmp_path):
    tool = DestructiveTool()
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    agent.run_task(one_step_task("wipe"))

    restored = AgentState.from_dict(agent.state.to_dict())
    history = RecoveryHistory.load(restored)
    subtask_id = restored.current_task.subtasks[0].id
    assert history.replans(subtask_id) == 1
    assert history.escalations(subtask_id) == 0
    assert not restored.pending_actions


def test_report_serialises_decisions(tmp_path):
    tool = FlakyTool(fails=1)
    agent = Agent(cfg(tmp_path), AgentDependencies(tools=[tool]))
    payload = agent.run_task(one_step_task("flaky")).to_dict()
    json.dumps(payload)
    assert payload["retried"] == 1
    assert payload["decisions"][0]["action"] == "retry"
