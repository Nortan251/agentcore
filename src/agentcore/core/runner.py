"""Task runner: executes a task's subtasks against the tool layer.

Scope note — this is dependency-ordered sequential execution, not autonomous
planning. A subtask runs only if it names a tool; otherwise it is surfaced as a
pending action for a human or a future planner to resolve. That keeps the
foundation useful without smuggling in agency it hasn't earned.

Task 42.4c adds cooperative cancellation and deadline checkpoints via
RunControl, without physical preemption.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agentcore.core.defaults import NoRetryStrategy, NullVerifier
from agentcore.core.interfaces import RecoveryStrategy, Verifier
from agentcore.errors import ToolNotFoundError
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.recovery.decision import RecoveryAction
from agentcore.realtime.cancellation import CancellationReason, CancellationToken
from agentcore.realtime.errors import CancellationRequested, TurnValidationError
from agentcore.realtime.turn import Deadline
from agentcore.state.models import AgentPhase, AgentState, PendingAction
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    Task,
    TaskStatus,
    VerificationStatus,
)
from agentcore.tools.executor import ToolExecutor
from agentcore.util.clock import Clock, SystemClock

#: Verification verdicts that mean "executed, but the goal was not shown to be
#: achieved". These route into recovery instead of being accepted silently.
_ADVERSE_VERDICTS = frozenset({VerificationStatus.FAILED,
                               VerificationStatus.UNVERIFIED})


@dataclass(frozen=True)
class RunControl:
    """Trusted autonomous control: cancellation + deadline + clock.

    Contains only trusted runtime primitives, never model or user content.
    No synthetic far-future deadline. No-control ``RunControl()`` preserves
    legacy autonomous behavior.
    """

    cancellation: CancellationToken | None = None
    deadline: Deadline | None = None
    clock: Clock | None = None

    def __post_init__(self) -> None:
        # Validation: deadline requires clock, malformed fails loudly/closed.
        if self.deadline is not None:
            if self.clock is None:
                raise TurnValidationError("deadline requires a clock")
            if not isinstance(self.deadline, Deadline):
                raise TurnValidationError("deadline is invalid")
            # Clock must have required methods
            for attr in ("now", "monotonic"):
                if not hasattr(self.clock, attr):
                    raise TurnValidationError("clock is invalid")
        if self.cancellation is not None:
            # Token must have requested/reason attributes
            for attr in ("requested", "reason"):
                if not hasattr(self.cancellation, attr):
                    raise TurnValidationError("cancellation token is invalid")
        if self.clock is not None and not hasattr(self.clock, "now"):
            raise TurnValidationError("clock is invalid")

    def remaining(self) -> float:
        """Remaining seconds until deadline, or inf if none."""
        if self.deadline is None:
            return math.inf
        if self.deadline.is_unbounded:
            return math.inf
        if self.clock is None:
            raise TurnValidationError("deadline requires a clock")
        try:
            return self.deadline.remaining(self.clock)
        except Exception as exc:
            # Malformed deadline fails closed as expired
            raise TurnValidationError("deadline remaining check failed") from exc

    def checkpoint(self) -> None:
        """Raise CancellationRequested if cancelled or deadline expired.

        Winner order:
        1. cancellation token FIRST, preserving its existing reason
        2. only if not cancelled, evaluate deadline -> DEADLINE_EXCEEDED
        Malformed token/deadline fails closed, never continues.
        """
        # 1. cancellation token first
        if self.cancellation is not None:
            try:
                requested = bool(self.cancellation.requested)
            except Exception:
                # Malformed token fails closed
                raise CancellationRequested(CancellationReason.INTERNAL_FAILURE,
                                            detail="malformed cancellation token")
            if requested:
                try:
                    reason = self.cancellation.reason
                except Exception:
                    raise CancellationRequested(CancellationReason.INTERNAL_FAILURE,
                                                detail="malformed cancellation reason")
                if reason is None or not isinstance(reason, CancellationReason):
                    # Malformed reason fails closed
                    raise CancellationRequested(CancellationReason.INTERNAL_FAILURE,
                                                detail="malformed cancellation reason")
                raise CancellationRequested(reason)

        # 2. deadline
        if self.deadline is not None:
            if self.deadline.is_unbounded:
                return
            if self.clock is None:
                raise TurnValidationError("deadline requires a clock")
            try:
                expired = self.deadline.expired(self.clock)
            except Exception:
                # Broken deadline fails closed as expired
                raise CancellationRequested(CancellationReason.DEADLINE_EXCEEDED)
            if expired:
                raise CancellationRequested(CancellationReason.DEADLINE_EXCEEDED)


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
    #: Categorical cancellation reason when status is CANCELLED (Task 42.4c)
    cancellation_reason: str | None = None

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
            "cancellation_reason": self.cancellation_reason,
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

    def run(self, task: Task, state: AgentState,
            *, control: RunControl | None = None) -> RunReport:
        """Execute ready subtasks until the task finishes or stalls.

        Task 42.4c: accepts optional trusted RunControl for cooperative
        cancellation and deadline. Legacy callers with no control retain old
        behavior.
        """
        control = control or RunControl()
        task.validate()
        if state.current_task is not task:
            state.set_task(task)
        log = self._log.bind(run_id=state.run_id, task_id=task.id)
        report = RunReport(task_id=task.id, status=task.status)

        try:
            # A. after attaching/validating task but BEFORE starting executable work
            control.checkpoint()

            # Ensure AgentState phase is EXECUTING for standalone runner usage
            # (Agent._execute_once also does this, but direct runner usage starts IDLE)
            if state.phase is not AgentPhase.EXECUTING:
                try:
                    state.transition_phase(AgentPhase.EXECUTING, note="run")
                except Exception:
                    # If already in a compatible phase (e.g., RECOVERING from previous retry),
                    # ignore; _schedule_retry will handle its own transitions.
                    pass

            if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                task.transition_to(TaskStatus.RUNNING)
            log.info(Events.TASK_STARTED, objective=task.objective,
                     subtasks=len(task.subtasks))

            steps = 0
            while steps < self.max_steps:
                steps += 1
                # B. before each loop iteration / before choosing next subtask
                control.checkpoint()

                subtask = task.next_subtask()
                if subtask is None:
                    break

                # C. before a subtask is made executable
                control.checkpoint()

                self._run_subtask(task, subtask, state, report, log, control=control)
                if task.has_failure():
                    break
                if report.replan_requested or report.escalated:
                    log.info(Events.RECOVERY_STOPPED,
                             reason="awaiting replan or human input")
                    break

            # Checkpoint immediately BEFORE final task transition (completion linearization)
            try:
                control.checkpoint()
            except CancellationRequested as exc:
                # Cancellation observed before final transition wins
                self._settle_cancellation(task, state, report, exc, log)
                return report

            status = self._finalise(task, state, report, log)
            report.status = status
            return report

        except CancellationRequested as exc:
            # Any checkpoint that raised cancellation
            self._settle_cancellation(task, state, report, exc, log)
            return report

    # -- cancellation settlement -------------------------------------------
    def _settle_cancellation(self, task: Task, state: AgentState,
                             report: RunReport, exc: CancellationRequested,
                             log: StructuredLogger) -> None:
        """Settle Task as CANCELLED preserving evidence."""
        reason = exc.reason if isinstance(exc.reason, CancellationReason) else CancellationReason.INTERNAL_FAILURE
        # Active subtask RUNNING -> CANCELLED if still RUNNING
        try:
            active_id = state.active_subtask_id
            if active_id is not None:
                # Find subtask by id
                for sub in task.subtasks:
                    if sub.id == active_id and sub.status is TaskStatus.RUNNING:
                        try:
                            sub.transition_to(TaskStatus.CANCELLED)
                        except Exception:
                            pass
                        break
        except Exception:
            pass

        # Task itself to CANCELLED if still nonterminal
        if not task.status.is_terminal:
            try:
                task.transition_to(TaskStatus.CANCELLED)
            except Exception:
                # If transition fails because already terminal, keep existing
                pass

        state.set_active_subtask(None)
        report.status = TaskStatus.CANCELLED
        report.cancellation_reason = reason.value
        report.replan_requested = False
        log.info(Events.TASK_CANCELLED if hasattr(Events, "TASK_CANCELLED") else "task.cancelled",
                 reason=reason.value, executed=report.executed, failed=report.failed)

    # -- internals ---------------------------------------------------------
    def _run_subtask(self, task: Task, subtask: Subtask, state: AgentState,
                     report: RunReport, log: StructuredLogger,
                     *, control: RunControl) -> None:
        slog = log.bind(subtask_id=subtask.id)

        if subtask.tool_name is None:
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

        # C already checked in run(), but also check before making executable
        control.checkpoint()

        state.set_active_subtask(subtask.id)
        subtask.transition_to(TaskStatus.RUNNING)
        subtask.attempts += 1
        slog.info(Events.SUBTASK_STARTED, tool=subtask.tool_name,
                  attempt=subtask.attempts)

        # Prepare checkpoint callback for ToolExecutor
        def checkpoint_cb() -> None:
            control.checkpoint()

        try:
            invocation = self.executor.invoke(
                subtask.tool_name, subtask.tool_args,
                checkpoint=checkpoint_cb,
                run_id=state.run_id, task_id=task.id, subtask_id=subtask.id)
        except CancellationRequested as exc:
            # Cancellation before physical tool execution (pre-permission or post-permission)
            # RUNNING -> CANCELLED, no tool invocation recorded
            try:
                if subtask.status is TaskStatus.RUNNING:
                    subtask.transition_to(TaskStatus.CANCELLED)
            except Exception:
                pass
            state.set_active_subtask(None)
            raise exc
        except ToolNotFoundError as exc:
            subtask.record_error(exc)
            subtask.transition_to(TaskStatus.FAILED)
            state.record_error(exc)
            report.failed += 1
            slog.error(Events.SUBTASK_FINISHED, outcome="failed",
                       error=exc.message)
            return

        # Tool returned, record evidence FIRST
        state.record_tool_call(invocation)

        # E. immediately AFTER recording, checkpoint before interpretation
        try:
            control.checkpoint()
        except CancellationRequested as exc:
            # Cancellation while tool was physically executing: tool evidence preserved,
            # active RUNNING subtask -> CANCELLED before result interpretation
            try:
                if subtask.status is TaskStatus.RUNNING:
                    subtask.transition_to(TaskStatus.CANCELLED)
            except Exception:
                pass
            state.set_active_subtask(None)
            raise exc

        if invocation.ok:
            subtask.result = invocation.result.output
            subtask.transition_to(TaskStatus.COMPLETED)
            report.executed += 1
            self._verify(subtask, state, slog, control=control)
            if subtask.verification.status in _ADVERSE_VERDICTS:
                self._recover(task, subtask, state, report, slog, control=control)
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

        self._recover(task, subtask, state, report, slog, control=control)
        state.set_active_subtask(None)

    def _recover(self, task: Task, subtask: Subtask, state: AgentState,
                 report: RunReport, log: StructuredLogger,
                 *, control: RunControl) -> None:
        """Consult the recovery strategy and apply its decision.

        Strategies exposing only the ``should_retry`` Protocol keep working
        unchanged; those exposing ``decide`` additionally drive replan and
        escalation outcomes. Cancellation/deadline fencing added.
        """
        # Checkpoint before recovery decision
        try:
            control.checkpoint()
        except CancellationRequested:
            raise

        decide = getattr(self.recovery, "decide", None)
        if decide is None:
            if self.recovery.should_retry(subtask, state):
                self._schedule_retry(subtask, state, report, log, delay=0.0, control=control)
            return

        # Recovery decision may block; checkpoint before and after
        try:
            decision = decide(subtask, state)
        except CancellationRequested:
            raise

        try:
            control.checkpoint()
        except CancellationRequested:
            # Late decision discarded
            raise

        # Only then mutate report/state
        report.decisions.append(decision.to_dict())

        if decision.action is RecoveryAction.RETRY:
            self._schedule_retry(subtask, state, report, log,
                                 delay=decision.delay_seconds, control=control)
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
                        delay: float, control: RunControl) -> None:
        """Return a subtask to PENDING so the loop picks it up again.

        Deadline/backoff aware: before sleep checkpoint, if delay >= remaining
        deadline, terminate as DEADLINE_EXCEEDED. After sleep checkpoint again.
        """
        # Checkpoint before scheduling retry
        control.checkpoint()

        # Deadline/backoff handling
        if delay > 0:
            remaining = control.remaining()
            if math.isfinite(remaining) and delay >= remaining:
                raise CancellationRequested(CancellationReason.DEADLINE_EXCEEDED)

        was_failed = subtask.status is TaskStatus.FAILED
        try:
            state.transition_phase(AgentPhase.RECOVERING, note="retrying subtask")
        except Exception:
            # If current phase cannot go directly to RECOVERING (e.g., IDLE from standalone usage),
            # ensure we are in EXECUTING first, then RECOVERING.
            try:
                if state.phase is not AgentPhase.EXECUTING:
                    state.transition_phase(AgentPhase.EXECUTING, note="run")
            except Exception:
                pass
            try:
                state.transition_phase(AgentPhase.RECOVERING, note="retrying subtask")
            except Exception:
                # If still fails, proceed without phase transition (best-effort, preserve evidence)
                pass
        log.info(Events.RECOVERY_ATTEMPTED, attempts=subtask.attempts,
                 delay_seconds=round(delay, 3))
        if delay > 0:
            self._sleep(delay)
            # Checkpoint immediately after sleep
            control.checkpoint()

        subtask.transition_to(TaskStatus.PENDING)
        subtask.result = None
        if was_failed:
            report.failed -= 1
        else:
            report.executed -= 1
        report.retried += 1
        log.info(Events.RECOVERY_RETRY_SCHEDULED, attempts=subtask.attempts)
        try:
            state.transition_phase(AgentPhase.EXECUTING, note="retry scheduled")
        except Exception:
            pass

    def _verify(self, subtask: Subtask, state: AgentState,
                log: StructuredLogger, *, control: RunControl) -> None:
        # Checkpoint before verifier
        control.checkpoint()

        try:
            result = self.verifier.verify(subtask, state)
        except CancellationRequested:
            raise

        # Checkpoint after verifier returns, before committing
        try:
            control.checkpoint()
        except CancellationRequested:
            # Late verifier result discarded
            raise

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
