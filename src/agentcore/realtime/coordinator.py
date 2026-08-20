"""Internal per-session turn admission and supersession ownership.

This module creates and orders durable turn records only.  It does not start
model or tool work, mutate conversation history, or grant any capability.
Admissions for one session are serialized by acquisition order while all
store I/O and injected callbacks execute outside coordinator locks.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.errors import (
    TurnAlreadyExistsError,
    TurnCoordinatorError,
    TurnStoreOwnershipError,
    TurnVersionConflictError,
)
from agentcore.realtime.store import TurnStore
from agentcore.realtime.turn import (
    Deadline,
    TurnController,
    TurnPhase,
    TurnRecord,
    TurnSnapshot,
    new_operation_id,
    new_turn,
    next_generation,
    require_session_id,
)
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


_MAX_SUPERSESSION_RECLASSIFICATIONS = 8


@dataclass(frozen=True)
class TurnAdmission:
    """Immutable result of one durably accepted session admission."""

    admitted: TurnSnapshot
    active: TurnSnapshot
    queued: TurnSnapshot | None
    superseded_active: TurnSnapshot | None = None
    superseded_before_start: TurnSnapshot | None = None


@dataclass
class _AdmissionLane:
    """A fair logical lane whose mutex is never held during store I/O."""

    condition: threading.Condition = field(
        default_factory=threading.Condition)
    busy: bool = False
    owner_thread_id: int | None = None
    waiters: list[object] = field(default_factory=list)
    users: int = 0

    def enter(self) -> None:
        marker = object()
        thread_id = threading.get_ident()
        with self.condition:
            if self.busy and self.owner_thread_id == thread_id:
                raise TurnCoordinatorError(
                    "reentrant admission for one session is unsupported")
            self.waiters.append(marker)
            try:
                while self.busy or self.waiters[0] is not marker:
                    self.condition.wait()
            except BaseException:
                if marker in self.waiters:
                    self.waiters.remove(marker)
                    self.condition.notify_all()
                raise
            self.waiters.pop(0)
            self.busy = True
            self.owner_thread_id = thread_id

    def leave(self) -> None:
        with self.condition:
            self.busy = False
            self.owner_thread_id = None
            self.condition.notify_all()


@dataclass(frozen=True)
class _SessionRecords:
    maximum_generation: int
    active: TurnRecord | None
    queued: TurnRecord | None


class TurnCoordinator:
    """Sole internal authority for ordered per-session turn admission.

    The coordinator owns only admission ordering.  The turn store remains the
    durable authority and ``TurnController`` remains the transition authority.
    Different sessions have independent logical lanes; the store supplies its
    own CAS synchronization.
    """

    def __init__(
        self,
        store: TurnStore,
        *,
        clock: Clock | None = None,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        if not isinstance(store, TurnStore):
            raise TurnCoordinatorError("turn store contract is invalid")
        if not callable(id_factory):
            raise TurnCoordinatorError("turn id factory is invalid")
        self._store = store
        self._clock = clock or SystemClock()
        self._id_factory = id_factory
        self._admission_coordination = store.admission_coordination
        self._owner_pids = (
            os.getpid(), self._admission_coordination.owner_pid)

    def admit(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        subtask_id: str | None = None,
        deadline: Deadline | None = None,
        action_refs: tuple[str, ...] = (),
    ) -> TurnAdmission:
        """Create the newest turn and supersede older session work safely."""
        self._require_owner_process()
        session_id = require_session_id(session_id)
        lane = self._lane(session_id)
        entered = False
        try:
            lane.enter()
            entered = True
            return self._admit_owned(
                session_id,
                run_id=run_id,
                task_id=task_id,
                subtask_id=subtask_id,
                deadline=deadline,
                action_refs=action_refs,
            )
        finally:
            if entered:
                lane.leave()
            self._release_lane(session_id, lane)

    def _lane(self, session_id: str) -> _AdmissionLane:
        self._require_owner_process()
        coordination = self._admission_coordination
        with coordination.lock:
            lane = coordination.lanes.get(session_id)
            if lane is None:
                lane = _AdmissionLane()
                coordination.lanes[session_id] = lane
            elif not isinstance(lane, _AdmissionLane):
                raise TurnCoordinatorError(
                    "store admission coordination domain is invalid")
            lane.users += 1
            return lane

    def _release_lane(self, session_id: str, lane: _AdmissionLane) -> None:
        self._require_owner_process()
        coordination = self._admission_coordination
        with coordination.lock:
            lane.users -= 1
            if lane.users == 0 and coordination.lanes.get(session_id) is lane:
                coordination.lanes.pop(session_id, None)

    def _require_owner_process(self) -> None:
        """Reject inherited coordination before touching cached mutexes."""
        current_pid = os.getpid()
        if self._owner_pids != (current_pid, current_pid):
            raise TurnStoreOwnershipError(
                "turn coordinator instance belongs to another process")

    def _admit_owned(
        self,
        session_id: str,
        *,
        run_id: str | None,
        task_id: str | None,
        subtask_id: str | None,
        deadline: Deadline | None,
        action_refs: tuple[str, ...],
    ) -> TurnAdmission:
        state = self._read_session(session_id)
        generation = next_generation(
            state.maximum_generation or None)
        turn_id = self._next_id("turn")
        prepared = new_turn(
            session_id,
            generation,
            run_id=run_id,
            task_id=task_id,
            subtask_id=subtask_id,
            deadline=deadline,
            action_refs=action_refs,
            clock=self._clock,
            id_factory=lambda prefix: turn_id,
        )
        # Creation is inside the guarded region because an atomic replace may
        # succeed even if the following directory fsync reports an error.  A
        # categorical collision, however, proves this attempt created nothing
        # and must never authorize cleanup of the pre-existing identity.
        try:
            self._store.create(prepared)
        except TurnAlreadyExistsError:
            raise
        except BaseException:
            try:
                self._fail_prepared(prepared, allow_progressed=False)
            except BaseException:
                pass
            raise

        try:
            active = state.active
            superseded_active: TurnSnapshot | None = None
            if active is not None:
                active, superseded_active = self._supersede_active(active)

            replaced: TurnSnapshot | None = None
            if state.queued is not None:
                replaced = self._cancel_before_start(state.queued)

            admitted = self._transition(
                prepared, TurnPhase.QUEUED).snapshot()
        except BaseException:
            # The prepared identity is already durable.  Best-effort failure
            # finalization prevents a recoverable admission error from leaving
            # runnable work behind; any cleanup failure remains explicit for
            # the restart reconciler and never masks the original exception.
            try:
                self._fail_prepared(prepared, allow_progressed=True)
            except BaseException:
                pass
            raise

        if active is None:
            return TurnAdmission(admitted, admitted, None)
        return TurnAdmission(
            admitted=admitted,
            active=active.snapshot(),
            queued=admitted,
            superseded_active=superseded_active,
            superseded_before_start=replaced,
        )

    def _read_session(self, session_id: str) -> _SessionRecords:
        records = [
            self._store.load(turn_id)
            for turn_id in self._store.list_turns()
        ]
        records = [record for record in records
                   if record.session_id == session_id]
        records.sort(key=lambda record: (record.generation, record.turn_id))

        generations = [record.generation for record in records]
        if len(generations) != len(set(generations)):
            raise TurnCoordinatorError(
                "session contains duplicate turn generations",
                session_id=session_id,
            )
        maximum = generations[-1] if generations else 0
        nonterminal = [record for record in records if not record.is_terminal]
        if len(nonterminal) > 2:
            raise TurnCoordinatorError(
                "session contains too many nonterminal turns",
                session_id=session_id,
                count=len(nonterminal),
            )
        if any(record.phase is TurnPhase.CREATED for record in nonterminal):
            raise TurnCoordinatorError(
                "session has an interrupted prepared turn; reconcile first",
                session_id=session_id,
            )
        if not nonterminal:
            return _SessionRecords(maximum, None, None)
        if len(nonterminal) == 1:
            return _SessionRecords(maximum, nonterminal[0], None)

        active, queued = nonterminal
        if active.phase not in {TurnPhase.CANCELLING, TurnPhase.FINALIZING} or \
                queued.phase is not TurnPhase.QUEUED:
            raise TurnCoordinatorError(
                "session nonterminal turn ordering is inconsistent",
                session_id=session_id,
            )
        return _SessionRecords(maximum, active, queued)

    def _supersede_active(
        self,
        record: TurnRecord,
    ) -> tuple[TurnRecord | None, TurnSnapshot | None]:
        for _ in range(_MAX_SUPERSESSION_RECLASSIFICATIONS):
            current = self._store.load(record.turn_id)
            if current.is_terminal:
                return None, None
            if current.phase in {TurnPhase.CANCELLING, TurnPhase.FINALIZING}:
                return current, None
            try:
                updated = self._transition(
                    current,
                    TurnPhase.CANCELLING,
                    cancellation_reason=CancellationReason.SUPERSEDED,
                    requested_terminal=TurnPhase.CANCELLED,
                )
                return updated, updated.snapshot()
            except TurnVersionConflictError:
                # The stage owner advanced the record after our classification.
                # Reload and classify its newer legal state; never reuse the
                # stale candidate or overwrite the accepted transition.
                continue
        raise TurnCoordinatorError(
            "active turn changed during every supersession attempt",
            turn_id=record.turn_id,
            attempts=_MAX_SUPERSESSION_RECLASSIFICATIONS,
        )

    def _cancel_before_start(self, record: TurnRecord) -> TurnSnapshot:
        cancelling = self._transition(
            record,
            TurnPhase.CANCELLING,
            cancellation_reason=CancellationReason.SUPERSEDED_BEFORE_START,
            requested_terminal=TurnPhase.CANCELLED,
        )
        finalizing = self._transition(
            cancelling,
            TurnPhase.FINALIZING,
            cancellation_reason=CancellationReason.SUPERSEDED_BEFORE_START,
            requested_terminal=TurnPhase.CANCELLED,
        )
        return self._transition(
            finalizing, TurnPhase.CANCELLED).snapshot()

    def _fail_prepared(
        self,
        prepared: TurnRecord,
        *,
        allow_progressed: bool,
    ) -> None:
        cleanup_authorized = False
        while True:
            current = self._store.load(prepared.turn_id)
            if not _is_current_admission_record(
                    current, prepared,
                    allow_progressed=(allow_progressed
                                      if not cleanup_authorized else True)):
                return
            cleanup_authorized = True
            if current.is_terminal:
                return
            if current.phase is TurnPhase.FINALIZING:
                target = current.requested_terminal
                reason = None
                if target is TurnPhase.COMPLETED:
                    target = TurnPhase.FAILED
                    reason = CancellationReason.INTERNAL_FAILURE
                if target is None:
                    raise TurnCoordinatorError(
                        "prepared turn finalization lacks an outcome")
                self._transition(
                    current, target, cancellation_reason=reason)
                continue
            if current.phase is TurnPhase.CANCELLING:
                self._transition(
                    current,
                    TurnPhase.FINALIZING,
                    cancellation_reason=current.cancellation_reason,
                    requested_terminal=current.requested_terminal,
                )
                continue
            self._transition(
                current,
                TurnPhase.CANCELLING,
                cancellation_reason=CancellationReason.INTERNAL_FAILURE,
                requested_terminal=TurnPhase.FAILED,
            )

    def _transition(
        self,
        record: TurnRecord,
        target: TurnPhase,
        *,
        cancellation_reason: CancellationReason | None = None,
        requested_terminal: TurnPhase | None = None,
    ) -> TurnRecord:
        controller = TurnController(
            self._store, record.turn_id, clock=self._clock)
        controller.transition(
            record.phase,
            target,
            expected_version=record.version,
            operation_id=self._operation_id(),
            cancellation_reason=cancellation_reason,
            requested_terminal=requested_terminal,
        )
        return self._store.load(record.turn_id)

    def _next_id(self, prefix: str) -> str:
        # The injected callback runs outside coordinator/session/lane locks.
        # Callers that admit independent sessions concurrently must supply a
        # thread-safe factory, as the production ``new_id`` implementation is.
        return self._id_factory(prefix)

    def _operation_id(self) -> str:
        value = self._next_id("op")
        return new_operation_id(id_factory=lambda prefix: value)


def _is_current_admission_record(
    current: TurnRecord,
    prepared: TurnRecord,
    *,
    allow_progressed: bool,
) -> bool:
    """Authorize cleanup only for the identity created by this admission."""
    if not allow_progressed:
        return current == prepared
    return (
        current.turn_id == prepared.turn_id
        and current.session_id == prepared.session_id
        and current.generation == prepared.generation
        and current.created_at == prepared.created_at
        and current.schema_version == prepared.schema_version
        and current.run_id == prepared.run_id
        and current.task_id == prepared.task_id
        and current.subtask_id == prepared.subtask_id
        and current.deadline_at == prepared.deadline_at
        and current.counters == prepared.counters
        and current.action_refs == prepared.action_refs
        and current.version >= prepared.version
    )
