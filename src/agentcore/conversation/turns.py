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

#: Outward explanation when the turn's own deadline expired instead. The
#: durable record already says TIMED_OUT/DEADLINE_EXCEEDED; the outward
#: error must not claim supersession.
TIMED_OUT_TURN_ERROR = "this turn exceeded its deadline"


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
    """Stale/cancellation/deadline fence and phase authority for one turn.

    Read-only boundaries decide through :meth:`_rejection_reason` against the
    freshly loaded record. Mutating boundaries use the transition itself as
    the fence: a compare-and-swap that loses to a supersession is the proof
    that this turn may not proceed, so no tool round can start, and no
    assistant message can be committed, after the record has moved on.
    """

    def __init__(self, controller: TurnController, *, turn_id: str,
                 session_key: str, generation: int,
                 operation_id: str | None, token: CancellationToken,
                 deadline: Deadline, clock: Clock,
                 session_lock: Any, store: Any,
                 log: StructuredLogger) -> None:
        self._controller = controller
        self._store = store
        self.turn_id = turn_id
        self._session_key = session_key
        self._generation = generation
        self._operation_id = operation_id
        self._token = token
        # The turn's own bounded deadline (Task 42.4a): created once by the
        # bridge at admission and never recomputed, so the ephemeral context
        # and the durable ``deadline_at`` projection cannot drift apart.
        self._deadline = deadline
        self._clock = clock
        self._session_lock = session_lock
        self._log = log
        self._phase = TurnPhase.QUEUED
        self._staged: list[Any] = []
        self.committed = False

    # -- fencing -----------------------------------------------------------
    def _context(self) -> OperationContext:
        return OperationContext(
            turn_id=self.turn_id,
            session_id=self._session_key,
            generation=self._generation,
            operation_id=self._operation_id or "",
            cancellation=self._token,
            deadline=self._deadline,
        )

    def operation_context(self) -> OperationContext | None:
        """Trusted realtime context for model transport (Task 42.4b).

        Returns ``None`` until the turn has been activated and owns an
        operation id, so callers without a valid operation never fabricate
        a deadline. When active, the returned context carries the exact
        deadline object created at admission, so the ephemeral and durable
        views never drift.
        """
        if self._operation_id is None:
            return None
        try:
            return self._context()
        except Exception:
            return None

    @property
    def clock(self) -> Clock:
        return self._clock

    def _rejection_reason(self, record: TurnRecord,
                          expected_phase: TurnPhase,
                          ) -> CancellationReason | None:
        """Why this turn may not proceed against ``record``, or ``None``.

        The order is deterministic and defers to durable truth:

        1. an already-requested cancellation wins first and keeps its reason
           (shutdown or any other token-cancelled cause);
        2. the durable record is authoritative: a turn that already moved on
           — superseded, cancelling, terminal — keeps the reason that record
           carries and is never relabelled as a timeout;
        3. only a still-current turn can observe its own deadline expiry,
           and that observation is the timeout's truthful reason.
        """
        if self._token.requested:
            return self._token.reason
        if record.is_terminal or record.phase is not expected_phase or \
                record.generation != self._generation or \
                record.session_id != self._session_key or \
                record.active_operation_id != self._operation_id:
            return record.cancellation_reason
        if self._deadline.expired(self._clock):
            return CancellationReason.DEADLINE_EXCEEDED
        return None

    def check(self) -> None:
        """Fail closed unless this turn is still the current, live one."""
        if self._operation_id is None:
            raise TurnSuperseded(self._token.reason)
        reason = self._rejection_reason(self._store.load(self.turn_id),
                                        self._phase)
        if reason is not None:
            raise TurnSuperseded(reason)

    def _advance(self, expected: TurnPhase, target: TurnPhase,
                 **outcome: Any) -> None:
        record = self._store.load(self.turn_id)
        reason = self._rejection_reason(record, expected)
        if reason is not None:
            raise TurnSuperseded(reason)
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
        """Fix this turn's terminal outcome before any transcript is published.

        Reaching ``FINALIZING`` is the linearization point for success: the
        coordinator will no longer cancel a finalizing record, so neither the
        staged tool traffic nor the assistant message about to be published can
        be overtaken by a successor. A turn that loses this compare-and-swap
        publishes nothing at all.
        """
        self._advance(TurnPhase.GENERATING, TurnPhase.FINALIZING,
                      requested_terminal=TurnPhase.COMPLETED)
        self.committed = True
        self._log.info(Events.TURN_FINALIZATION_STARTED,
                       staged_messages=len(self._staged))

    def stage_tool_message(self, session: Any, message: Any, *,
                           max_messages: int) -> None:
        """Hold this turn's tool traffic out of shared conversation history.

        Admission deliberately takes neither the stage lease nor the session
        lock, so a successor may supersede this turn at any point after the
        ``EXECUTING_TOOL -> GENERATING`` fence. Appending here would therefore
        always leave a check-then-append window. Staging closes it: the message
        exists only inside this turn until :meth:`flush_staged` runs behind the
        commit point. ``ConversationRuntime`` still puts the same message into
        its ``extra`` list, which is the model-context source for the rest of
        this turn, so nothing the model sees changes.
        """
        self._staged.append(message)

    def flush_staged(self, session: Any, *, max_messages: int) -> None:
        """Publish staged tool traffic. Only legal after a successful commit."""
        if not self.committed:  # pragma: no cover - guarded by the call site
            raise TurnCoordinatorError(
                "staged transcript may only be published after finalizing",
                turn_id=self.turn_id)
        for message in self._staged:
            session.append(message, max_messages=max_messages)
        self._staged.clear()

    def session_lock(self) -> ContextManager[Any]:
        """Narrow lock, taken inside the stage lease, for append/save only."""
        return self._session_lock


class TurnBridge:
    """Runs one conversational turn under real realtime turn ownership."""

    def __init__(self, coordinator: Any, *,
                 stage_lease: StageLease | None = None,
                 session_lock: Any = None,
                 clock: Clock | None = None,
                 turn_deadline_seconds: float = 120.0,
                 logger: StructuredLogger | None = None) -> None:
        self._coordinator = coordinator
        # The coordinator is the admission authority; its store is the durable
        # authority a stage owner needs to build its own controller.
        self._store = coordinator.store
        self.stage_lease = stage_lease or StageLease()
        self.session_lock = session_lock or threading.RLock()
        self._clock = clock or SystemClock()
        # Root budget for every admitted turn (Task 42.4a). Validated by
        # ``Deadline.after`` at each admission; a future trusted API deadline
        # can only shorten this through ``Deadline.child``, never lengthen it.
        self._turn_deadline_seconds = turn_deadline_seconds
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
        # One bounded root deadline per admission, from the trusted bridge
        # clock, created *before* admission: its UTC projection becomes the
        # immutable durable ``deadline_at``, and the fence below carries this
        # exact object so the ephemeral and durable views never drift.
        deadline = Deadline.after(self._turn_deadline_seconds,
                                  clock=self._clock)
        admission = self._coordinator.admit(session_key, deadline=deadline)
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
            deadline=deadline, clock=self._clock,
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
        # The durable terminal record owns the truthful outward vocabulary:
        # a turn that timed out must not claim it was superseded.
        message = (TIMED_OUT_TURN_ERROR
                   if record.phase is TurnPhase.TIMED_OUT
                   else SUPERSEDED_TURN_ERROR)
        result = runtime._superseded_result(session, message)
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
                if record.phase is TurnPhase.TIMED_OUT:
                    # Categorical timeout evidence only; the finalized event
                    # below still reports the terminal phase once.
                    log.info(Events.TURN_TIMED_OUT,
                             cancellation_reason=(
                                 record.cancellation_reason.value
                                 if record.cancellation_reason else None))
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
