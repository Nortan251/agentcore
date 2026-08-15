"""Task runner: executes a task's subtasks against the tool layer.

Scope note — this is dependency-ordered sequential execution, not autonomous
planning. A subtask runs only if it names a tool; otherwise it is surfaced as a
pending action for a human or a future planner to resolve. That keeps the
foundation useful without smuggling in agency it hasn't earned.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from agentcore.core.defaults import NoRetryStrategy, NullVerifier
from agentcore.core.interfaces import RecoveryStrategy, Verifier
from agentcore.errors import ToolNotFoundError
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.recovery.decision import RecoveryAction
from agentcore.state.models import AgentPhase, AgentState, PendingAction
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    Task,
    TaskStatus,
    VerificationStatus,
)
from agentcore.tools.executor import ToolExecutor

#: Verification verdicts that mean "executed, but the goal was not shown to be
#: achieved". These route into recovery instead of being accepted silently.
_ADVERSE_VERDICTS = frozenset({VerificationStatus.FAILED,
                               VerificationStatus.UNVERIFIED})


@dataclass
class RunReport:
    """Outcome of one :meth:`TaskRunner.run` call."""

    task_id: str
    status: TaskStatus
    executed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: bool = False
    retried: int = 0
    #: Recovery asked for a new plan; the orchestration layer decides whether
    #: to act on it. The runner never replans by itself.
    replan_requested: bool = False
    #: Recovery handed off to a human.
    escalated: bool = False
    decisions: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "executed": self.executed,
            "failed": self.failed,
            "skipped": self.skipped,
            "blocked": self.blocked,
            "retried": self.retried,
            "replan_requested": self.replan_requested,
            "escalated": self.escalated,
            "decisions": list(self.decisions),
        }


class TaskRunner:
    def __init__(
        self,
        executor: ToolExecutor,
        *,
        verifier: Verifier | None = None,
        recovery: RecoveryStrategy | None = None,
        logger: StructuredLogger | None = None,
        max_steps: int = 100,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.executor = executor
        self.verifier = verifier or NullVerifier()
        self.recovery = recovery or NoRetryStrategy()
        self.max_steps = max_steps
        # Injected so tests never actually sleep during backoff.
        self._sleep = sleep if sleep is not None else time.sleep
        self._log = logger or get_logger("core.runner")

    def run(self, task: Task, state: AgentState) -> RunReport:
        """Execute ready subtasks until the task finishes or stalls."""
        task.validate()
        # The runner is usable standalone, so it owns this invariant rather than
        # trusting the caller to have attached the task first.
        if state.current_task is not task:
            state.set_task(task)
        log = self._log.bind(run_id=state.run_id, task_id=task.id)
        report = RunReport(task_id=task.id, status=task.status)

        # A task may re-enter the runner after being blocked (e.g. the
        # orchestration layer resumed it). BLOCKED -> RUNNING is a legal
        # transition and is required for _finalise to settle the task again.
        if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
            task.transition_to(TaskStatus.RUNNING)
        log.info(Events.TASK_STARTED, objective=task.objective,
                 subtasks=len(task.subtasks))

        steps = 0
        while steps < self.max_steps:
            steps += 1
            subtask = task.next_subtask()
            if subtask is None:
                break
            self._run_subtask(task, subtask, state, report, log)
            if task.has_failure():
                break
            # A replan or escalation needs a decision from outside this loop;
            # continuing would execute steps that recovery just questioned.
            if report.replan_requested or report.escalated:
                log.info(Events.RECOVERY_STOPPED,
                         reason="awaiting replan or human input")
                break

        status = self._finalise(task, state, report, log)
        report.status = status
        return report

    # -- internals ---------------------------------------------------------
    def _run_subtask(self, task: Task, subtask: Subtask, state: AgentState,
                     report: RunReport, log: StructuredLogger) -> None:
        slog = log.bind(subtask_id=subtask.id)

        if subtask.tool_name is None:
            # Nothing executable: surface it rather than silently succeeding.
            subtask.transition_to(TaskStatus.BLOCKED)
            state.add_pending_action(PendingAction(
                description=subtask.description,
                reason="subtask has no tool assigned",
                subtask_id=subtask.id,
            ))
            report.skipped += 1
            report.blocked = True
            slog.info(Events.TASK_BLOCKED, reason="no tool assigned")
            return

        state.set_active_subtask(subtask.id)
        subtask.transition_to(TaskStatus.RUNNING)
        subtask.attempts += 1
        slog.info(Events.SUBTASK_STARTED, tool=subtask.tool_name,
                  attempt=subtask.attempts)

        try:
            invocation = self.executor.invoke(
                subtask.tool_name, subtask.tool_args,
                run_id=state.run_id, task_id=task.id, subtask_id=subtask.id)
        except ToolNotFoundError as exc:
            subtask.record_error(exc)
            subtask.transition_to(TaskStatus.FAILED)
            state.record_error(exc)
            report.failed += 1
            slog.error(Events.SUBTASK_FINISHED, outcome="failed",
                       error=exc.message)
            return

        state.record_tool_call(invocation)

        if invocation.ok:
            subtask.result = invocation.result.output
            subtask.transition_to(TaskStatus.COMPLETED)
            report.executed += 1
            self._verify(subtask, state, slog)
            # Executing successfully is not the same as achieving the goal:
            # an adverse verdict is a recovery trigger, not a silent pass.
            if subtask.verification.status in _ADVERSE_VERDICTS:
                self._recover(task, subtask, state, report, slog)
            else:
                slog.info(Events.SUBTASK_FINISHED, outcome="completed")
            state.set_active_subtask(None)
            return

        subtask.record_error(ErrorRecord(
            type=str(invocation.result.metadata.get("kind", "ToolFailure")),
            message=invocation.result.error or "tool failed",
            retryable=bool(invocation.result.metadata.get("retryable", False)),
            details=dict(invocation.result.metadata),
        ))
        subtask.transition_to(TaskStatus.FAILED)
        report.failed += 1
        slog.warning(Events.SUBTASK_FINISHED, outcome="failed",
                     error=invocation.result.error)

        self._recover(task, subtask, state, report, slog)
        state.set_active_subtask(None)

    def _recover(self, task: Task, subtask: Subtask, state: AgentState,
                 report: RunReport, log: StructuredLogger) -> None:
        """Consult the recovery strategy and apply its decision.

        Strategies exposing only the ``should_retry`` Protocol keep working
        unchanged; those exposing ``decide`` additionally drive replan and
        escalation outcomes.
        """
        decide = getattr(self.recovery, "decide", None)
        if decide is None:
            if self.recovery.should_retry(subtask, state):
                self._schedule_retry(subtask, state, report, log, delay=0.0)
            return

        decision = decide(subtask, state)
        report.decisions.append(decision.to_dict())

        if decision.action is RecoveryAction.RETRY:
            self._schedule_retry(subtask, state, report, log,
                                 delay=decision.delay_seconds)
        elif decision.action is RecoveryAction.REPLAN:
            report.replan_requested = True
            log.info(Events.RECOVERY_REPLAN_REQUESTED, reason=decision.reason,
                     category=str(decision.category))
        elif decision.action is RecoveryAction.ESCALATE:
            report.escalated = True
            log.warning(Events.RECOVERY_ESCALATED, reason=decision.reason,
                        category=str(decision.category))
        elif decision.action is RecoveryAction.STOP:
            log.info(Events.RECOVERY_STOPPED, reason=decision.reason,
                     category=str(decision.category))

    def _schedule_retry(self, subtask: Subtask, state: AgentState,
                        report: RunReport, log: StructuredLogger, *,
                        delay: float) -> None:
        """Return a subtask to PENDING so the loop picks it up again."""
        was_failed = subtask.status is TaskStatus.FAILED
        state.transition_phase(AgentPhase.RECOVERING, note="retrying subtask")
        log.info(Events.RECOVERY_ATTEMPTED, attempts=subtask.attempts,
                 delay_seconds=round(delay, 3))
        if delay > 0:
            self._sleep(delay)
        # FAILED -> PENDING, or COMPLETED -> PENDING after an adverse verdict.
        subtask.transition_to(TaskStatus.PENDING)
        subtask.result = None
        if was_failed:
            report.failed -= 1
        else:
            report.executed -= 1
        report.retried += 1
        log.info(Events.RECOVERY_RETRY_SCHEDULED, attempts=subtask.attempts)
        state.transition_phase(AgentPhase.EXECUTING, note="retry scheduled")

    def _verify(self, subtask: Subtask, state: AgentState,
                log: StructuredLogger) -> None:
        result = self.verifier.verify(subtask, state)
        subtask.verification = result
        state.record_verification(result, subtask_id=subtask.id)
        log.debug(Events.VERIFICATION_RECORDED, status=str(result.status),
                  checked_by=result.checked_by)

    def _finalise(self, task: Task, state: AgentState, report: RunReport,
                  log: StructuredLogger) -> TaskStatus:
        if task.has_failure():
            task.transition_to(TaskStatus.FAILED)
            log.warning(Events.TASK_FAILED, **report.to_dict())
        elif report.replan_requested or report.escalated:
            # Work stopped pending an external decision: BLOCKED, not complete.
            task.transition_to(TaskStatus.BLOCKED)
            report.blocked = True
            log.info(Events.TASK_BLOCKED, **report.to_dict())
        elif task.is_complete():
            task.transition_to(TaskStatus.COMPLETED)
            log.info(Events.TASK_COMPLETED, **report.to_dict())
        else:
            task.transition_to(TaskStatus.BLOCKED)
            report.blocked = True
            log.info(Events.TASK_BLOCKED, **report.to_dict())
        return task.status
