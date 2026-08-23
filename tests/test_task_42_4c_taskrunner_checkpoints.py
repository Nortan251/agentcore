"""Task 42.4c — autonomous cooperative cancellation + deadline checkpoints.

Deterministic tests using Events, Conditions, FixedClock, injected sleep,
gated fakes. No sleep-based race proofs.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from typing import Any

import pytest

from agentcore.config.schema import AgentConfig, LoggingConfig, ModelConfig, PermissionConfig, ToolConfig
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.runner import RunControl, RunReport, TaskRunner
from agentcore.errors import StateError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.realtime.cancellation import CancellationReason, CancellationSource
from agentcore.realtime.errors import CancellationRequested, TurnValidationError
from agentcore.realtime.turn import Deadline
from agentcore.state.models import AgentPhase, AgentState
from agentcore.state.store import InMemoryStateStore
from agentcore.tasks.models import Task, Subtask, TaskStatus, VerificationResult, VerificationStatus
from agentcore.tools.base import Tool, ToolResult, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from agentcore.permissions.policy import PermissionPolicy, ApprovalGate
from agentcore.fs.guard import WorkspaceGuard
from agentcore.util.clock import FixedClock

WAIT = 30.0
T0 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
DEADLINE_SECONDS = 10.0
EXPIRE_BY = 11.0


def _config(tmp_path, *, level=PermissionLevel.AUTONOMOUS):
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path / "workspace"),
        model=ModelConfig(),
        tools=ToolConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
    ).validate()


class GateTool(Tool):
    def __init__(self):
        self.runs = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec("gate", "Blocks until released.", parameters=[],
                         capabilities=[Capability.FS_READ], read_only=True)

    def run(self, args, context):
        with self._lock:
            self.runs += 1
        self.entered.set()
        assert self.release.wait(WAIT), "gate tool never released"
        return ToolResult.success("gated")


class CountingTool(Tool):
    def __init__(self):
        self.runs = 0
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec("count", "Counts runs.", parameters=[],
                         capabilities=[Capability.FS_READ], read_only=True)

    def run(self, args, context):
        with self._lock:
            self.runs += 1
            return ToolResult.success(f"count-{self.runs}")


class FailTool(Tool):
    @property
    def spec(self):
        return tool_spec("fail", "Fails.", parameters=[], capabilities=[], read_only=True)

    def run(self, args, context):
        return ToolResult.failure("failed")


class WriteTool(Tool):
    def __init__(self):
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("write", "Write.", parameters=[], capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("wrote")


class GatedVerifier:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = VerificationResult(status=VerificationStatus.PASSED, checked_by="test")

    def verify(self, subtask, state):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(WAIT), "verifier gate never released"
        return self.result


class GatedRecovery:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory
        self.decision = RecoveryDecision(action=RecoveryAction.STOP, reason="stop", category=FailureCategory.NONE)

    def should_retry(self, subtask, state):
        self.calls += 1
        return False

    def decide(self, subtask, state):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(WAIT), "recovery gate never released"
        return self.decision


class GatedPlanner:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.next_task: Task | None = None

    def plan(self, objective, *, context=None):
        self.calls += 1
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="step", tool_name="count", tool_args={}))
        return task

    def replan(self, task, state):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(WAIT), "planner gate never released"
        if self.next_task is None:
            raise RuntimeError("no next task")
        return self.next_task


class GatedApprovalGate(ApprovalGate):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.decision = True

    def request(self, capability, description):
        self.entered.set()
        assert self.release.wait(WAIT), "approval gate never released"
        return self.decision


class GatedStateStore(InMemoryStateStore):
    """State store whose save() can be gated for deterministic TOCTOU proof."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.save_calls: list[str] = []
        self._first_gated = True

    def save(self, state):
        self.save_calls.append(state.phase.value)
        if self._first_gated:
            self._first_gated = False
            self.entered.set()
            assert self.release.wait(WAIT), "state store save gate never released"
        return super().save(state)


def make_task_with_tools(tool_names: list[str]) -> Task:
    task = Task(objective="test objective")
    for name in tool_names:
        task.add_subtask(Subtask(description=f"run {name}", tool_name=name, tool_args={}))
    return task


def make_agent(tmp_path, tools, *, verifier=None, recovery=None, planner=None, approval_gate=None, level=PermissionLevel.AUTONOMOUS, state_store=None):
    config = _config(tmp_path, level=level)
    deps = AgentDependencies(
        tools=tools,
        verifier=verifier,
        recovery=recovery,
        planner=planner,
        approval_gate=approval_gate,
        state_store=state_store,
    )
    agent = Agent(config, deps)
    return agent


# ---------------------------------------------------------------------------
# 1. cancellation before runner starts
# ---------------------------------------------------------------------------
def test_cancellation_before_runner_starts_no_tool(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])

    source = CancellationSource()
    source.cancel(CancellationReason.OPERATOR_CANCEL)
    control = RunControl(cancellation=source.token)

    # For standalone runner, need valid EXECUTING phase for recovery path, but this test has no recovery, so IDLE is okay if runner doesn't require EXECUTING
    # However after P2-2 fix, runner does NOT transition phase itself, so we set phase explicitly to EXECUTING to satisfy invariants
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")

    report = agent.runner.run(task, agent.state, control=control)

    assert report.status is TaskStatus.CANCELLED
    assert report.cancellation_reason == CancellationReason.OPERATOR_CANCEL.value
    assert tool.runs == 0
    assert task.status is TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 2. deadline expired before runner starts
# ---------------------------------------------------------------------------
def test_deadline_expired_before_runner_starts(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])

    clock = FixedClock(T0)
    deadline = Deadline.after(1.0, clock=clock)
    clock.advance(2.0)
    control = RunControl(deadline=deadline, clock=clock)

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")

    report = agent.runner.run(task, agent.state, control=control)

    assert report.status is TaskStatus.CANCELLED
    assert report.cancellation_reason == CancellationReason.DEADLINE_EXCEEDED.value
    assert tool.runs == 0


# ---------------------------------------------------------------------------
# 3. deadline expires between subtasks
# ---------------------------------------------------------------------------
def test_deadline_expires_between_subtasks(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count", "count"])

    clock = FixedClock(T0)
    deadline = Deadline.after(5.0, clock=clock)

    original_run_subtask = agent.runner._run_subtask

    def wrapped_run_subtask(task, subtask, state, report, log, control):
        result = original_run_subtask(task, subtask, state, report, log, control=control)
        if len(state.tool_calls) == 1:
            clock.advance(EXPIRE_BY)
        return result

    agent.runner._run_subtask = wrapped_run_subtask
    control = RunControl(deadline=deadline, clock=clock)

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")

    report = agent.runner.run(task, agent.state, control=control)

    assert tool.runs == 1
    assert task.subtasks[0].status is TaskStatus.COMPLETED
    assert report.status is TaskStatus.CANCELLED
    assert report.cancellation_reason == CancellationReason.DEADLINE_EXCEEDED.value


# ---------------------------------------------------------------------------
# 4. shutdown before permission evaluation
# ---------------------------------------------------------------------------
def test_shutdown_before_permission(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])

    source = CancellationSource()
    source.cancel(CancellationReason.AGENT_SHUTDOWN)
    control = RunControl(cancellation=source.token)

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")

    report = agent.runner.run(task, agent.state, control=control)

    assert tool.runs == 0
    assert report.status is TaskStatus.CANCELLED
    assert report.cancellation_reason == CancellationReason.AGENT_SHUTDOWN.value


# ---------------------------------------------------------------------------
# 5. shutdown DURING approval wait — deterministic, no sleep
# ---------------------------------------------------------------------------
def test_shutdown_during_approval_wait_blocks_tool(tmp_path):
    approval_gate = GatedApprovalGate()
    approval_gate.decision = True

    write_tool = WriteTool()
    agent = make_agent(tmp_path, [write_tool], approval_gate=approval_gate, level=PermissionLevel.CONFIRM)
    task = make_task_with_tools(["write"])

    results: list[RunReport] = []

    def run_task():
        results.append(agent.run_task(task))

    t = threading.Thread(target=run_task, daemon=True)
    t.start()
    assert approval_gate.entered.wait(WAIT), "approval gate never entered"

    agent.shutdown()
    approval_gate.release.set()
    t.join(WAIT)

    assert not t.is_alive()
    assert write_tool.runs == 0
    assert results[0].status is TaskStatus.CANCELLED
    assert results[0].cancellation_reason == CancellationReason.AGENT_SHUTDOWN.value


# ---------------------------------------------------------------------------
# 6. cancellation while Tool.run is physically blocked — success variant
# ---------------------------------------------------------------------------
def test_cancellation_while_tool_blocked_records_invocation_success_counter(tmp_path):
    gate_tool = GateTool()
    verifier = GatedVerifier()
    recovery = GatedRecovery()

    agent = make_agent(tmp_path, [gate_tool], verifier=verifier, recovery=recovery)
    task = make_task_with_tools(["gate"])

    clock = FixedClock(T0)
    source = CancellationSource()
    control = RunControl(cancellation=source.token, clock=clock)

    results: list[RunReport] = []

    def run():
        agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
        results.append(agent.runner.run(task, agent.state, control=control))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert gate_tool.entered.wait(WAIT), "tool never entered"

    source.cancel(CancellationReason.OPERATOR_CANCEL)
    gate_tool.release.set()
    t.join(WAIT)

    assert gate_tool.runs == 1
    assert len(agent.state.tool_calls) == 1
    assert agent.state.tool_calls[0]["tool"] == "gate"
    # P1-2: counters must reflect physical outcome BEFORE checkpoint
    assert results[0].executed == 1
    assert results[0].failed == 0
    assert verifier.calls == 0
    assert recovery.calls == 0
    assert task.subtasks[0].status is TaskStatus.CANCELLED
    assert results[0].status is TaskStatus.CANCELLED


def test_cancellation_while_tool_blocked_failure_counter(tmp_path):
    class FailGateTool(Tool):
        def __init__(self):
            self.runs = 0
            self.entered = threading.Event()
            self.release = threading.Event()
        @property
        def spec(self):
            return tool_spec("failgate", "Fails after gate.", parameters=[], capabilities=[], read_only=True)
        def run(self, args, context):
            self.runs += 1
            self.entered.set()
            assert self.release.wait(WAIT)
            return ToolResult.failure("failed")

    fail_tool = FailGateTool()
    agent = make_agent(tmp_path, [fail_tool])
    task = make_task_with_tools(["failgate"])

    source = CancellationSource()
    control = RunControl(cancellation=source.token)

    results: list[RunReport] = []

    def run():
        agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
        results.append(agent.runner.run(task, agent.state, control=control))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert fail_tool.entered.wait(WAIT)

    source.cancel(CancellationReason.OPERATOR_CANCEL)
    fail_tool.release.set()
    t.join(WAIT)

    assert fail_tool.runs == 1
    assert len(agent.state.tool_calls) == 1
    assert results[0].failed == 1
    assert results[0].executed == 0
    assert task.subtasks[0].status is TaskStatus.CANCELLED
    assert results[0].status is TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 7. late verifier result — strengthened
# ---------------------------------------------------------------------------
def test_late_verifier_result_not_committed(tmp_path):
    tool = CountingTool()
    verifier = GatedVerifier()
    verifier.result = VerificationResult(status=VerificationStatus.PASSED, checked_by="gated")

    agent = make_agent(tmp_path, [tool], verifier=verifier)
    task = make_task_with_tools(["count"])

    source = CancellationSource()
    control = RunControl(cancellation=source.token)

    results: list[RunReport] = []

    def run():
        agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
        results.append(agent.runner.run(task, agent.state, control=control))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert verifier.entered.wait(WAIT), "verifier never entered"

    source.cancel(CancellationReason.OPERATOR_CANCEL)
    verifier.release.set()
    t.join(WAIT)

    assert tool.runs == 1
    # Required exactly per audit:
    assert task.subtasks[0].status is TaskStatus.COMPLETED
    assert results[0].status is TaskStatus.CANCELLED
    # state.verifications contains NO late result
    assert len(agent.state.verifications) == 0
    # subtask.verification remains original/uncommitted (UNVERIFIED)
    assert task.subtasks[0].verification.status is VerificationStatus.UNVERIFIED
    # recovery not run
    assert results[0].replan_requested is False
    assert results[0].escalated is False


# ---------------------------------------------------------------------------
# 8. late recovery decision — strengthened
# ---------------------------------------------------------------------------
def test_late_recovery_decision_not_acted_on(tmp_path):
    class FailTool(Tool):
        @property
        def spec(self):
            return tool_spec("fail", "Fails.", parameters=[], capabilities=[], read_only=True)
        def run(self, args, context):
            return ToolResult.failure("failed")

    recovery = GatedRecovery()
    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory
    recovery.decision = RecoveryDecision(action=RecoveryAction.RETRY, reason="retry", category=FailureCategory.NONE, delay_seconds=0.0)

    agent = make_agent(tmp_path, [FailTool()], recovery=recovery)
    task = make_task_with_tools(["fail"])

    source = CancellationSource()
    control = RunControl(cancellation=source.token)

    results: list[RunReport] = []

    def run():
        agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
        results.append(agent.runner.run(task, agent.state, control=control))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert recovery.entered.wait(WAIT), "recovery never entered"

    source.cancel(CancellationReason.OPERATOR_CANCEL)
    recovery.release.set()
    t.join(WAIT)

    assert results[0].status is TaskStatus.CANCELLED
    # Required independently per audit, no OR
    assert results[0].decisions == []
    assert results[0].retried == 0
    assert results[0].replan_requested is False
    assert results[0].escalated is False
    # No next tool invocation — only one tool call recorded
    assert len(agent.state.tool_calls) == 1


# ---------------------------------------------------------------------------
# 9. retry backoff > remaining deadline
# ---------------------------------------------------------------------------
def test_retry_backoff_exceeds_remaining_deadline(tmp_path):
    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory

    class RetryRecovery:
        def __init__(self):
            self.calls = 0
        def should_retry(self, subtask, state):
            return False
        def decide(self, subtask, state):
            self.calls += 1
            return RecoveryDecision(action=RecoveryAction.RETRY, reason="retry", category=FailureCategory.NONE, delay_seconds=5.0)

    recovery = RetryRecovery()

    class FailOnceTool(Tool):
        def __init__(self):
            self.runs = 0
        @property
        def spec(self):
            return tool_spec("failonce", "Fails once.", parameters=[], capabilities=[], read_only=True)
        def run(self, args, context):
            self.runs += 1
            if self.runs == 1:
                return ToolResult.failure("first fail")
            return ToolResult.success("ok")

    fail_tool = FailOnceTool()
    agent = make_agent(tmp_path, [fail_tool], recovery=recovery)
    task = make_task_with_tools(["failonce"])

    clock = FixedClock(T0)
    deadline = Deadline.after(2.0, clock=clock)
    control = RunControl(deadline=deadline, clock=clock)

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")

    report = agent.runner.run(task, agent.state, control=control)

    assert report.status is TaskStatus.CANCELLED
    assert report.cancellation_reason == CancellationReason.DEADLINE_EXCEEDED.value
    assert fail_tool.runs == 1
    assert report.retried == 0


# ---------------------------------------------------------------------------
# 10. cancellation/deadline during injected retry sleep
# ---------------------------------------------------------------------------
def test_cancellation_during_retry_sleep_stops(tmp_path):
    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory

    class RetryRecovery:
        def decide(self, subtask, state):
            return RecoveryDecision(action=RecoveryAction.RETRY, reason="retry", category=FailureCategory.NONE, delay_seconds=1.0)

    recovery = RetryRecovery()

    class FailTool(Tool):
        @property
        def spec(self):
            return tool_spec("fail", "Fails.", parameters=[], capabilities=[], read_only=True)
        def run(self, args, context):
            return ToolResult.failure("fail")

    clock = FixedClock(T0)
    source = CancellationSource()

    def sleep_cancel(delay: float):
        source.cancel(CancellationReason.OPERATOR_CANCEL)
        clock.advance(delay)

    agent = make_agent(tmp_path, [FailTool()], recovery=recovery)
    agent.runner._sleep = sleep_cancel
    task = make_task_with_tools(["fail"])

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
    control = RunControl(cancellation=source.token, clock=clock, deadline=Deadline.after(10.0, clock=clock))

    report = agent.runner.run(task, agent.state, control=control)

    assert report.status is TaskStatus.CANCELLED
    assert task.subtasks[0].status is not TaskStatus.PENDING


# ---------------------------------------------------------------------------
# 11. shutdown during planner.replan
# ---------------------------------------------------------------------------
def test_shutdown_during_replan_rejects_new_plan(tmp_path):
    count_tool = CountingTool()
    planner = GatedPlanner()
    replacement = Task(objective="replacement")
    replacement.add_subtask(Subtask(description="new step", tool_name="count", tool_args={}))
    planner.next_task = replacement

    class FailTool(Tool):
        @property
        def spec(self):
            return tool_spec("fail", "Fails.", parameters=[], capabilities=[], read_only=True)
        def run(self, args, context):
            return ToolResult.failure("fail")

    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory

    class ReplanRecovery:
        def decide(self, subtask, state):
            return RecoveryDecision(action=RecoveryAction.REPLAN, reason="replan", category=FailureCategory.NONE)

    recovery = ReplanRecovery()

    agent = make_agent(tmp_path, [FailTool()], recovery=recovery, planner=planner)
    task = make_task_with_tools(["fail"])

    results: list[RunReport] = []

    def run_task():
        results.append(agent.run_task(task))

    t = threading.Thread(target=run_task, daemon=True)
    t.start()
    assert planner.entered.wait(WAIT), "planner never entered"

    agent.shutdown()
    planner.release.set()
    t.join(WAIT)

    assert agent.state.current_task.id != replacement.id
    assert count_tool.runs == 0
    assert results[0].status is TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 12. cancellation reason winner: shutdown first, deadline later
# ---------------------------------------------------------------------------
def test_cancellation_reason_winner_shutdown_first(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])

    clock = FixedClock(T0)
    source = CancellationSource()
    source.cancel(CancellationReason.AGENT_SHUTDOWN)
    deadline = Deadline.after(1.0, clock=clock)
    clock.advance(2.0)

    control = RunControl(cancellation=source.token, deadline=deadline, clock=clock)

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")

    report = agent.runner.run(task, agent.state, control=control)

    assert report.cancellation_reason == CancellationReason.AGENT_SHUTDOWN.value


# ---------------------------------------------------------------------------
# 13. completion wins first
# ---------------------------------------------------------------------------
def test_completion_wins_over_later_shutdown(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
    report = agent.runner.run(task, agent.state)

    assert report.status is TaskStatus.COMPLETED

    agent.shutdown()

    assert task.status is TaskStatus.COMPLETED
    assert report.status is TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 14. legacy TaskRunner.run with no control unchanged
# ---------------------------------------------------------------------------
def test_legacy_runner_no_control_unchanged(tmp_path):
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
    report = agent.runner.run(task, agent.state)

    assert report.status is TaskStatus.COMPLETED
    assert tool.runs == 1


# ---------------------------------------------------------------------------
# 15. shutdown while NO autonomous run
# ---------------------------------------------------------------------------
def test_shutdown_while_no_autonomous_run_checkpoints(tmp_path):
    agent = make_agent(tmp_path, [CountingTool()])
    agent.state.set_task(make_task_with_tools(["count"]))

    saves = []
    original_save = agent.state_store.save
    def tracking_save(state):
        saves.append(1)
        return original_save(state)
    agent.state_store.save = tracking_save

    agent.shutdown()

    assert len(saves) >= 1


# ---------------------------------------------------------------------------
# 16. shutdown while autonomous run active — no competing save (deterministic, no sleep)
# ---------------------------------------------------------------------------
def test_shutdown_while_active_no_competing_save(tmp_path):
    gate_tool = GateTool()
    agent = make_agent(tmp_path, [gate_tool])
    task = make_task_with_tools(["gate"])

    saves_during_active: list[int] = []
    original_save = agent.state_store.save
    def tracking_save(state):
        if gate_tool.entered.is_set() and not gate_tool.release.is_set():
            saves_during_active.append(1)
        return original_save(state)
    agent.state_store.save = tracking_save

    results: list[RunReport] = []

    def run():
        results.append(agent.run_task(task))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert gate_tool.entered.wait(WAIT)

    agent.shutdown()
    # Deterministic: shutdown must NOT have saved concurrently while active
    assert saves_during_active == []

    gate_tool.release.set()
    t.join(WAIT)

    assert results[0].status is TaskStatus.CANCELLED


def test_shutdown_save_gated_run_attempt_blocked_until_save_completes(tmp_path):
    """P1-1 B: no active run, shutdown save starts gated, run attempts blocked."""
    gated_store = GatedStateStore()
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool], state_store=gated_store)
    task = make_task_with_tools(["count"])

    assert not agent._active_run

    shutdown_done = threading.Event()
    run_results: list[RunReport] = []
    run_started = threading.Event()

    def do_shutdown():
        agent.shutdown()
        shutdown_done.set()

    def do_run():
        run_started.set()
        try:
            run_results.append(agent.run_task(task))
        except Exception as exc:
            run_results.append(exc)

    t_shutdown = threading.Thread(target=do_shutdown, daemon=True)
    t_shutdown.start()
    assert gated_store.entered.wait(WAIT), "shutdown save never entered"

    t_run = threading.Thread(target=do_run, daemon=True)
    t_run.start()
    assert run_started.wait(WAIT)

    assert t_run.is_alive()
    assert tool.runs == 0

    gated_store.release.set()
    t_shutdown.join(WAIT)
    assert shutdown_done.is_set()

    t_run.join(WAIT)

    assert len(run_results) == 1
    result = run_results[0]
    assert isinstance(result, RunReport)
    assert result.status is TaskStatus.CANCELLED
    assert result.cancellation_reason == CancellationReason.AGENT_SHUTDOWN.value
    assert tool.runs == 0
    assert agent.state.tool_calls == []


def test_second_concurrent_run_rejected(tmp_path):
    gate_tool = GateTool()
    agent = make_agent(tmp_path, [gate_tool])
    task1 = make_task_with_tools(["gate"])
    task2 = make_task_with_tools(["gate"])

    results1: list[RunReport] = []

    def run1():
        results1.append(agent.run_task(task1))

    t1 = threading.Thread(target=run1, daemon=True)
    t1.start()
    assert gate_tool.entered.wait(WAIT)

    with pytest.raises(StateError):
        agent.run_task(task2)

    gate_tool.release.set()
    t1.join(WAIT)


# ---------------------------------------------------------------------------
# 18. existing workspace identity unchanged
# ---------------------------------------------------------------------------
def test_workspace_identity_unchanged(tmp_path):
    agent = make_agent(tmp_path, [CountingTool()])
    assert agent.workspace.identity is not None
    task = make_task_with_tools(["count"])
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
    agent.runner.run(task, agent.state)
    assert agent.workspace.identity is not None
    assert agent.state.workspace == agent.workspace.identity


# ---------------------------------------------------------------------------
# 19. permission deny — strengthened (FS_WRITE at OBSERVE)
# ---------------------------------------------------------------------------
def test_permission_deny_unchanged(tmp_path):
    write_tool = WriteTool()
    agent = make_agent(tmp_path, [write_tool], level=PermissionLevel.OBSERVE)
    task = make_task_with_tools(["write"])

    agent.state.transition_phase(AgentPhase.EXECUTING, note="test setup")
    report = agent.runner.run(task, agent.state)

    assert write_tool.runs == 0
    assert len(agent.state.tool_calls) == 1

    invocation = agent.state.tool_calls[0]

    assert invocation["result"]["ok"] is False
    assert invocation["result"]["metadata"]["kind"] == "permission_denied"

    assert report.failed == 1
    assert task.subtasks[0].status is TaskStatus.FAILED
    assert task.status is TaskStatus.FAILED


# ---------------------------------------------------------------------------
# 20. ToolExecutor without checkpoint behaves same as before + checkpoint cannot grant
# ---------------------------------------------------------------------------
def test_toolexecutor_without_checkpoint_same_semantics(tmp_path):
    tool = CountingTool()
    config = _config(tmp_path)
    registry = ToolRegistry(config.tools)
    registry.register(tool)
    policy = PermissionPolicy(config.permissions)
    workspace = WorkspaceGuard(config.canonical_workspace(), config=config.filesystem)
    executor = ToolExecutor(registry, policy, workspace=workspace)

    invocation = executor.invoke("count", {}, run_id="run_test")

    assert invocation.ok
    assert tool.runs == 1
    assert invocation.result.output.startswith("count-")


def test_toolexecutor_checkpoint_cannot_grant_denied(tmp_path):
    """Checkpoint may STOP, never GRANT: denied stays denied even if checkpoint allows."""
    write_tool = WriteTool()
    config = _config(tmp_path, level=PermissionLevel.OBSERVE)
    registry = ToolRegistry(config.tools)
    registry.register(write_tool)
    policy = PermissionPolicy(config.permissions)
    workspace = WorkspaceGuard(config.canonical_workspace(), config=config.filesystem)
    executor = ToolExecutor(registry, policy, workspace=workspace)

    def checkpoint_ok():
        return None

    invocation = executor.invoke("write", {}, checkpoint=checkpoint_ok, run_id="run_test")

    assert invocation.ok is False
    assert invocation.result.metadata["kind"] == "permission_denied"
    assert write_tool.runs == 0


# ---------------------------------------------------------------------------
# Additional: RunControl validation
# ---------------------------------------------------------------------------
def test_runcontrol_deadline_requires_clock():
    clock = FixedClock(T0)
    deadline = Deadline.after(10.0, clock=clock)
    with pytest.raises(TurnValidationError):
        RunControl(deadline=deadline, clock=None)

def test_runcontrol_remaining_inf_without_deadline():
    control = RunControl()
    assert control.remaining() == math.inf

def test_runcontrol_checkpoint_no_control_no_raise():
    control = RunControl()
    control.checkpoint()

def test_runcontrol_malformed_token_fails_closed():
    class BadToken:
        def __init__(self):
            self._calls = 0
        @property
        def requested(self):
            self._calls += 1
            if self._calls == 1:
                return False
            raise RuntimeError("bad token")
        @property
        def reason(self):
            return CancellationReason.OPERATOR_CANCEL

    token = BadToken()
    control = RunControl(cancellation=token)
    with pytest.raises(CancellationRequested):
        control.checkpoint()

def test_runcontrol_malformed_deadline_fails_closed():
    clock = FixedClock(T0)
    class BrokenDeadline(Deadline):
        def __init__(self):
            # Create a valid bounded deadline via parent after, then break methods
            # We bypass parent __init__ validation by using object.__setattr__
            base = Deadline.after(10.0, clock=clock)
            object.__setattr__(self, "monotonic_expires_at", base.monotonic_expires_at)
            object.__setattr__(self, "expires_at_utc", base.expires_at_utc)

        def expired(self, clock):
            raise RuntimeError("broken")

        def remaining(self, clock):
            raise RuntimeError("broken")

    source = CancellationSource()
    control = RunControl(cancellation=source.token, deadline=BrokenDeadline(), clock=clock)
    with pytest.raises(CancellationRequested) as exc:
        control.checkpoint()
    assert exc.value.reason is CancellationReason.DEADLINE_EXCEEDED

def test_replan_ledger_not_consumed_when_cancelled_before_begin(tmp_path):
    """P2-1: cancellation before ledger.begin must not consume attempt."""
    from agentcore.core.replan import ReplanLedger
    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory

    # Setup: task already failed and requests replan, ledger empty
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = Task(objective="original")
    task.add_subtask(Subtask(description="fail", tool_name="count", tool_args={}))
    task.transition_to(TaskStatus.RUNNING)
    task.subtasks[0].transition_to(TaskStatus.RUNNING)
    task.subtasks[0].transition_to(TaskStatus.FAILED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")
    agent.state.transition_phase(AgentPhase.RECOVERING, note="test")
    agent.state.transition_phase(AgentPhase.PLANNING, note="test")

    report = RunReport(task_id=task.id, status=TaskStatus.FAILED, replan_requested=True)

    ledger_before = ReplanLedger.load(agent.state)
    count_before = ledger_before.count
    assert count_before == 0

    clock = FixedClock(T0)
    source = CancellationSource()
    source.cancel(CancellationReason.OPERATOR_CANCEL)
    control = RunControl(cancellation=source.token, clock=clock)

    ledger = ReplanLedger.load(agent.state)
    with pytest.raises(CancellationRequested):
        agent._replan(ledger, report, control=control)

    ledger_after = ReplanLedger.load(agent.state)
    assert ledger_after.count == count_before
    # No new archive
    assert len(ledger_after.records) == len(ledger_before.records)

def test_replan_ledger_consumed_when_planner_entered_then_cancelled(tmp_path):
    """P2-1: planner entered then cancelled — attempt consumed, cancelled record."""
    from agentcore.core.replan import ReplanLedger
    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory

    planner = GatedPlanner()
    replacement = Task(objective="replacement")
    replacement.add_subtask(Subtask(description="new", tool_name="count", tool_args={}))
    planner.next_task = replacement

    tool = CountingTool()
    agent = make_agent(tmp_path, [tool], planner=planner)
    task = Task(objective="original")
    task.add_subtask(Subtask(description="fail", tool_name="count", tool_args={}))
    task.transition_to(TaskStatus.RUNNING)
    task.subtasks[0].transition_to(TaskStatus.RUNNING)
    task.subtasks[0].transition_to(TaskStatus.FAILED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")
    agent.state.transition_phase(AgentPhase.RECOVERING, note="test")
    agent.state.transition_phase(AgentPhase.PLANNING, note="test")

    report = RunReport(task_id=task.id, status=TaskStatus.FAILED, replan_requested=True)

    clock = FixedClock(T0)
    source = CancellationSource()
    control = RunControl(cancellation=source.token, clock=clock)

    results: list = []

    def do_replan():
        ledger = ReplanLedger.load(agent.state)
        try:
            results.append(agent._replan(ledger, report, control=control))
        except CancellationRequested as exc:
            results.append(exc)

    t = threading.Thread(target=do_replan, daemon=True)
    t.start()
    assert planner.entered.wait(WAIT), "planner never entered"

    source.cancel(CancellationReason.AGENT_SHUTDOWN)
    planner.release.set()
    t.join(WAIT)

    assert isinstance(results[0], CancellationRequested)
    ledger = ReplanLedger.load(agent.state)
    assert ledger.count == 1
    assert ledger.records[0].outcome == "cancelled"
    # Replacement plan NOT adopted
    assert agent.state.current_task.id == task.id

# ---------------------------------------------------------------------------
# Lifecycle finalization regressions (final cleanup)
# ---------------------------------------------------------------------------
def test_finalise_completed_plus_error_raises_and_stays_error(tmp_path):
    """COMPLETED + ERROR must raise StateError and NOT become DONE."""
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])
    task.subtasks[0].transition_to(TaskStatus.RUNNING)
    task.subtasks[0].transition_to(TaskStatus.COMPLETED)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.COMPLETED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")
    agent.state.transition_phase(AgentPhase.ERROR, note="test error")

    report = RunReport(task_id=task.id, status=TaskStatus.COMPLETED)

    with pytest.raises(StateError):
        agent._finalise_run(report)

    assert agent.state.phase is AgentPhase.ERROR


def test_finalise_cancelled_plus_error_raises_and_stays_error(tmp_path):
    """CANCELLED + ERROR must raise StateError and remain ERROR."""
    tool = CountingTool()
    agent = make_agent(tmp_path, [tool])
    task = make_task_with_tools(["count"])
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.CANCELLED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")
    agent.state.transition_phase(AgentPhase.ERROR, note="test error")

    report = RunReport(task_id=task.id, status=TaskStatus.CANCELLED, cancellation_reason=CancellationReason.AGENT_SHUTDOWN.value)

    with pytest.raises(StateError):
        agent._finalise_run(report)

    assert agent.state.phase is AgentPhase.ERROR


def test_finalise_cancelled_plus_planning_succeeds_to_done(tmp_path):
    """CANCELLED + PLANNING must succeed via IDLE bridge to DONE with categorical reason."""
    agent = make_agent(tmp_path, [CountingTool()])
    task = make_task_with_tools(["count"])
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.CANCELLED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")
    agent.state.transition_phase(AgentPhase.RECOVERING, note="test")
    agent.state.transition_phase(AgentPhase.PLANNING, note="test planning")

    report = RunReport(task_id=task.id, status=TaskStatus.CANCELLED, cancellation_reason=CancellationReason.DEADLINE_EXCEEDED.value)

    agent._finalise_run(report)

    assert agent.state.phase is AgentPhase.DONE
    # Note contains categorical reason
    assert "deadline_exceeded" in agent.state.history[-1].data.get("note", "") or "deadline_exceeded" in str(agent.state.resume_point.note)


def test_finalise_completed_plus_executing_to_done(tmp_path):
    agent = make_agent(tmp_path, [CountingTool()])
    task = make_task_with_tools(["count"])
    task.subtasks[0].transition_to(TaskStatus.RUNNING)
    task.subtasks[0].transition_to(TaskStatus.COMPLETED)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.COMPLETED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")

    report = RunReport(task_id=task.id, status=TaskStatus.COMPLETED)

    agent._finalise_run(report)

    assert agent.state.phase is AgentPhase.DONE


def test_finalise_cancelled_plus_recovering_to_done(tmp_path):
    agent = make_agent(tmp_path, [CountingTool()])
    task = make_task_with_tools(["count"])
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.CANCELLED)
    agent.state.set_task(task)
    agent.state.transition_phase(AgentPhase.EXECUTING, note="test")
    agent.state.transition_phase(AgentPhase.RECOVERING, note="test")

    report = RunReport(task_id=task.id, status=TaskStatus.CANCELLED, cancellation_reason=CancellationReason.AGENT_SHUTDOWN.value)

    agent._finalise_run(report)

    assert agent.state.phase is AgentPhase.DONE
