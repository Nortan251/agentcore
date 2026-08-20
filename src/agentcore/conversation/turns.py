"""Opt-in bridge from the conversation runtime to the realtime turn foundation.

This module coordinates *lifecycle only*. It never executes a tool, never
resolves a capability, never touches the workspace guard and never consults
the permission policy: conversation tool calls keep flowing through the
existing ``ConversationToolRunner`` -> ``ToolExecutor.invoke()`` path
unchanged. What it adds is turn ownership::

    ConversationRuntime.send
      -> derive_session_key(raw session id)     [opaque, one-way]
      -> TurnCoordinator.admit(key)             [no stage lease held]
      -> StageLease.acquire                     [per-Agent, reentrant]
      -> QUEUED -> GENERATING                   [re-checked after acquisition]
      -> ConversationRuntime._run_turn(fence=…) [existing model/tool flow]
      -> terminalize                            [before releasing the lease]

The direction of the dependency is deliberate: ``agentcore.realtime`` stays a
foundation that knows nothing about conversation, and the conversation layer
opts into it. With no coordinator injected this module is never imported and
the pre-Task-42.3 path runs untouched.

Cancellation is cooperative only. A provider or tool that ignores cancellation
keeps the successor waiting until it physically returns; nothing here claims
barge-in latency, forced interruption, or preemption.
"""

from __future__ import annotations

import threading
from typing import Any, ContextManager

from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.realtime.cancellation import (
    CancellationReason,
    CancellationSource,
    CancellationToken,
)
from agentcore.realtime.errors import (
    TurnCoordinatorError,
    TurnTransitionError,
    TurnVersionConflictError,
)
from agentcore.realtime.store import reconcile_nonterminal_turns
from agentcore.realtime.turn import (
    Deadline,
    OperationContext,
    TurnController,
    TurnPhase,
    TurnRecord,
    derive_session_key,
    new_operation_id,
    terminal_for_reason,
)
from agentcore.util.clock import Clock, SystemClock

#: Bounded terminalization budget. Each iteration performs at most one
#: transition, so this only has to absorb CAS reclassification.
_MAX_TERMINALIZATION_STEPS = 32

#: Outward, content-free explanation for a turn that lost its lease. It is a
#: turn outcome, not a model failure, so no assistant message is fabricated.
SUPERSEDED_TURN_ERROR = "this turn was superseded by a newer message"


class TurnSuperseded(Exception):
    """Internal signal: this turn is no longer the current one.

    Never raised out of :meth:`TurnBridge.run`; it is converted into an honest
    non-``ok`` :class:`~agentcore.conversation.runtime.TurnResult`.
    """

    def __init__(self, reason: CancellationReason | None = None) -> None:
        super().__init__("turn is no longer current")
        self.reason = reason


class StageLease:
    """The per-``Agent`` reentrant stage lease approved for Task 42.3.

    Admission never waits for or holds this lease, so a successor can be
    admitted and durably queued while its predecessor is physically running.
    The stage owner acquires it before ``QUEUED -> GENERATING`` and holds it
    across model invocation, tool rounds, fence decisions, its own session
    mutations and its final durable transition, releasing it only once its
    record is terminal. That is what stops a successor from starting work
    against a predecessor that is still non-terminal.

    Reentrant so a nested call on the owning thread cannot self-deadlock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def __enter__(self) -> StageLease:
        self._lock.acquire()
        return self

    def __exit__(self, *_: object) -> bool:
        self._lock.release()
        return False


class _TurnFence:
    """Stale/cancellation fence and phase authority for one owned turn.

    Read-only boundaries use ``TurnController.accepts``. Mutating boundaries
    use the transition itself as the fence: a compare-and-swap that loses to a
    supersession is the proof that this turn may not proceed, so no tool round
    can start, and no assistant message can be committed, after the record has
    moved on.
    """

    def __init__(self, controller: TurnController, *, turn_id: str,
                 session_key: str, generation: int,
                 operation_id: str | None, token: CancellationToken,
                 session_lock: Any, store: Any,
                 log: StructuredLogger) -> None:
        self._controller = controller
        self._store = store
        self.turn_id = turn_id
        self._session_key = session_key
        self._generation = generation
        self._operation_id = operation_id
        self._token = token
        self._session_lock = session_lock
        self._log = log
        self._phase = TurnPhase.QUEUED
        self.committed = False

    # -- fencing -----------------------------------------------------------
    def _context(self) -> OperationContext:
        return OperationContext(
            turn_id=self.turn_id,
            session_id=self._session_key,
            generation=self._generation,
            operation_id=self._operation_id or "",
            cancellation=self._token,
            # Task 42.3 enforces no timeout. The unbounded deadline is the
            # named scaffolding the fence contract requires, nothing more.
            deadline=Deadline.unbounded(),
        )

    def check(self) -> None:
        """Fail closed unless this turn is still the current, live one."""
        if self._operation_id is None or \
                not self._controller.accepts(self._context(),
                                             expected_phase=self._phase):
            raise TurnSuperseded(self._token.reason)

    def _advance(self, expected: TurnPhase, target: TurnPhase,
                 **outcome: Any) -> None:
        if self._token.requested:
            raise TurnSuperseded(self._token.reason)
        record = self._store.load(self.turn_id)
        if record.phase is not expected:
            raise TurnSuperseded(record.cancellation_reason)
        operation_id = new_operation_id()
        try:
            self._controller.transition(
                expected, target, expected_version=record.version,
                operation_id=operation_id, **outcome)
        except (TurnVersionConflictError, TurnTransitionError) as exc:
            raise TurnSuperseded() from exc
        self._phase = target
        self._operation_id = operation_id

    # -- stage boundaries --------------------------------------------------
    def activate(self) -> None:
        self._advance(TurnPhase.QUEUED, TurnPhase.GENERATING)
        self._log.info(Events.TURN_MODEL_STARTED)

    def enter_tool_round(self) -> None:
        self._advance(TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL)
        self._log.info(Events.TURN_TOOL_STARTED)

    def leave_tool_round(self) -> None:
        self._advance(TurnPhase.EXECUTING_TOOL, TurnPhase.GENERATING)
        self._log.info(Events.TURN_TOOL_FINISHED)

    def commit(self) -> None:
        """Fix this turn's terminal outcome before any reply is persisted.

        Reaching ``FINALIZING`` is the linearization point for success: the
        coordinator will no longer cancel a finalizing record, so the outcome
        can no longer change underneath the assistant message about to be
        appended. A turn that loses this compare-and-swap never appends.
        """
        self._advance(TurnPhase.GENERATING, TurnPhase.FINALIZING,
                      requested_terminal=TurnPhase.COMPLETED)
        self.committed = True
        self._log.info(Events.TURN_FINALIZATION_STARTED)

    def session_lock(self) -> ContextManager[Any]:
        """Narrow lock, taken inside the stage lease, for append/save only."""
        return self._session_lock


class TurnBridge:
    """Runs one conversational turn under real realtime turn ownership."""

    def __init__(self, coordinator: Any, *,
                 stage_lease: StageLease | None = None,
                 session_lock: Any = None,
                 clock: Clock | None = None,
                 logger: StructuredLogger | None = None) -> None:
        self._coordinator = coordinator
        # The coordinator is the admission authority; its store is the durable
        # authority a stage owner needs to build its own controller.
        self._store = coordinator.store
        self.stage_lease = stage_lease or StageLease()
        self.session_lock = session_lock or threading.RLock()
        self._clock = clock or SystemClock()
        self._root = CancellationSource()
        self._log = logger or get_logger("conversation.turns")

    # -- host lifecycle ----------------------------------------------------
    def reconcile(self) -> tuple:
        """Explicit host-side restart reconciliation. Never implicit."""
        return reconcile_nonterminal_turns(self._store, clock=self._clock)

    def shutdown(self) -> None:
        """Request cooperative cancellation of every live turn."""
        self._root.cancel(CancellationReason.AGENT_SHUTDOWN)

    # -- one turn ----------------------------------------------------------
    def run(self, runtime: Any, session: Any, message: Any) -> Any:
        """Admit, own and terminalize exactly one conversational turn."""
        session_key = derive_session_key(session.id)
        admission = self._coordinator.admit(session_key)
        admitted = admission.admitted
        log = self._log.bind(turn_id=admitted.turn_id,
                             session_key=session_key,
                             generation=admitted.generation)
        log.info(Events.TURN_ADMITTED,
                 superseded_active=admission.superseded_active is not None,
                 superseded_before_start=(
                     admission.superseded_before_start is not None))

        controller = TurnController(self._store, admitted.turn_id,
                                    clock=self._clock)
        source = self._root.child()
        fence = _TurnFence(
            controller, turn_id=admitted.turn_id, session_key=session_key,
            generation=admitted.generation,
            operation_id=admitted.active_operation_id, token=source.token,
            session_lock=self.session_lock, store=self._store, log=log)
        try:
            # Admission is already complete and its lane released; no
            # coordinator mutex and no session lock is held while waiting here.
            with self.stage_lease:
                return self._run_owned(runtime, session, message,
                                       controller, fence, log)
        finally:
            source.close()

    def _run_owned(self, runtime: Any, session: Any, message: Any,
                   controller: TurnController, fence: _TurnFence,
                   log: StructuredLogger) -> Any:
        try:
            # Re-check eligibility now that the lease is held: the turn may
            # have been superseded while it waited.
            fence.activate()
        except TurnSuperseded as exc:
            log.info(Events.TURN_STALE_RESULT_REJECTED, stage="activate")
            record = self._terminalize(controller, fence.turn_id,
                                       success=False, reason=exc.reason,
                                       log=log)
            return self._superseded(runtime, session, fence, record)

        try:
            result = runtime._run_turn(session, message, fence=fence)
        except TurnSuperseded as exc:
            log.info(Events.TURN_STALE_RESULT_REJECTED, stage="generate")
            record = self._terminalize(controller, fence.turn_id,
                                       success=False, reason=exc.reason,
                                       log=log)
            return self._superseded(runtime, session, fence, record)
        except BaseException:
            # An admitted turn must never stay non-terminal once the
            # synchronous call returns, however it returns.
            self._terminalize(controller, fence.turn_id, success=False,
                              reason=CancellationReason.INTERNAL_FAILURE,
                              log=log)
            raise

        record = self._terminalize(controller, fence.turn_id,
                                   success=fence.committed,
                                   reason=CancellationReason.INTERNAL_FAILURE,
                                   log=log)
        if result.ok and record.phase is not TurnPhase.COMPLETED:
            # Defensive: never report success for a record that did not
            # actually complete.
            return self._superseded(runtime, session, fence, record)
        _annotate(result, fence, record)
        return result

    def _superseded(self, runtime: Any, session: Any, fence: _TurnFence,
                    record: TurnRecord) -> Any:
        result = runtime._superseded_result(session, SUPERSEDED_TURN_ERROR)
        _annotate(result, fence, record)
        return result

    # -- terminalization ---------------------------------------------------
    def _terminalize(self, controller: TurnController, turn_id: str, *,
                     success: bool, reason: CancellationReason | None,
                     log: StructuredLogger) -> TurnRecord:
        """Drive an owned record to a truthful terminal state, exactly once.

        Idempotent: a record that is already terminal is returned unchanged.
        A concurrent supersession simply reclassifies the next step, so a
        turn cancelled at the last moment terminalizes as cancelled rather
        than manufacturing the success it was about to claim.
        """
        chosen = reason or CancellationReason.INTERNAL_FAILURE
        for _ in range(_MAX_TERMINALIZATION_STEPS):
            record = self._store.load(turn_id)
            if record.is_terminal:
                log.info(
                    Events.TURN_FINALIZED, phase=record.phase.value,
                    cancellation_reason=(record.cancellation_reason.value
                                         if record.cancellation_reason
                                         else None))
                return record
            try:
                _terminalization_step(controller, record, success=success,
                                      reason=chosen)
            except (TurnVersionConflictError, TurnTransitionError):
                continue
        raise TurnCoordinatorError("turn could not be terminalized",
                                   turn_id=turn_id)


def _terminalization_step(controller: TurnController, record: TurnRecord, *,
                          success: bool,
                          reason: CancellationReason) -> None:
    operation_id = new_operation_id()
    if record.phase is TurnPhase.FINALIZING:
        target = record.requested_terminal
        if target is None:  # pragma: no cover - schema forbids this
            raise TurnCoordinatorError("finalizing turn lacks an outcome",
                                       turn_id=record.turn_id)
        if target is TurnPhase.COMPLETED and not success:
            # Interrupted success is evidence of failure, never permission to
            # manufacture the previously requested completion.
            controller.transition(
                TurnPhase.FINALIZING, TurnPhase.FAILED,
                expected_version=record.version, operation_id=operation_id,
                cancellation_reason=CancellationReason.INTERNAL_FAILURE)
            return
        controller.transition(TurnPhase.FINALIZING, target,
                              expected_version=record.version,
                              operation_id=operation_id)
        return
    if record.phase is TurnPhase.CANCELLING:
        controller.transition(
            TurnPhase.CANCELLING, TurnPhase.FINALIZING,
            expected_version=record.version, operation_id=operation_id,
            requested_terminal=record.requested_terminal,
            cancellation_reason=record.cancellation_reason)
        return
    if success and record.phase in {TurnPhase.GENERATING,
                                    TurnPhase.EXECUTING_TOOL}:
        controller.transition(
            record.phase, TurnPhase.FINALIZING,
            expected_version=record.version, operation_id=operation_id,
            requested_terminal=TurnPhase.COMPLETED)
        return
    controller.transition(
        record.phase, TurnPhase.CANCELLING,
        expected_version=record.version, operation_id=operation_id,
        cancellation_reason=reason,
        requested_terminal=terminal_for_reason(reason))


def _annotate(result: Any, fence: _TurnFence, record: TurnRecord) -> None:
    """Attach safe turn correlation to the outward result. Never the raw id."""
    result.turn_id = fence.turn_id
    result.turn_generation = record.generation
    result.turn_phase = record.phase.value
