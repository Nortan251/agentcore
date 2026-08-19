from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from agentcore.realtime import (
    CancellationReason,
    Deadline,
    InMemoryTurnStore,
    JsonFileTurnStore,
    TurnCoordinator,
    TurnCoordinatorError,
    TurnPhase,
    TurnStore,
    TurnVersionConflictError,
)
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


class _Ids:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        with self._lock:
            value = self._counters.get(prefix, 0) + 1
            self._counters[prefix] = value
        return f"{prefix}_{value:012x}"


@pytest.fixture(params=["memory", "json"])
def turn_store(request, tmp_path):
    store = (InMemoryTurnStore() if request.param == "memory"
             else JsonFileTurnStore(tmp_path))
    try:
        yield store
    finally:
        store.close()


def _coordinator(store: TurnStore, ids: _Ids | None = None):
    return TurnCoordinator(
        store,
        clock=FixedClock(NOW),
        id_factory=ids or _Ids(),
    )


def test_first_admission_uses_injected_identity_clock_and_deadline(turn_store):
    ids = _Ids()
    clock = FixedClock(NOW)
    deadline = Deadline.after(30, clock=clock)
    coordinator = TurnCoordinator(
        turn_store, clock=clock, id_factory=ids)

    result = coordinator.admit(
        "conv_000000000001",
        run_id="run_000000000001",
        task_id="task_000000000001",
        subtask_id="sub_000000000001",
        deadline=deadline,
        action_refs=("act_000000000001",),
    )

    assert result.admitted == result.active
    assert result.queued is None
    assert result.superseded_active is None
    assert result.superseded_before_start is None
    assert result.admitted.turn_id == "turn_000000000001"
    assert result.admitted.generation == 1
    assert result.admitted.phase is TurnPhase.QUEUED
    assert result.admitted.created_at == NOW
    assert result.admitted.updated_at == NOW
    assert result.admitted.deadline_at == NOW + timedelta(seconds=30)
    persisted = turn_store.load(result.admitted.turn_id)
    assert persisted.snapshot() == result.admitted
    assert persisted.run_id == "run_000000000001"
    assert persisted.task_id == "task_000000000001"
    assert persisted.subtask_id == "sub_000000000001"
    assert persisted.action_refs == ("act_000000000001",)


def test_successor_replacement_is_durable_and_generation_ordered(turn_store):
    coordinator = _coordinator(turn_store)
    first = coordinator.admit("conv_000000000001")
    second = coordinator.admit("conv_000000000001")
    third = coordinator.admit("conv_000000000001")

    assert [item.admitted.generation for item in (first, second, third)] == [
        1, 2, 3]
    active = turn_store.load(first.admitted.turn_id)
    replaced = turn_store.load(second.admitted.turn_id)
    newest = turn_store.load(third.admitted.turn_id)

    assert active.phase is TurnPhase.CANCELLING
    assert active.cancellation_reason is CancellationReason.SUPERSEDED
    assert active.requested_terminal is TurnPhase.CANCELLED
    assert replaced.phase is TurnPhase.CANCELLED
    assert replaced.cancellation_reason is \
        CancellationReason.SUPERSEDED_BEFORE_START
    assert replaced.requested_terminal is TurnPhase.CANCELLED
    assert replaced.final_terminal is TurnPhase.CANCELLED
    assert newest.phase is TurnPhase.QUEUED
    assert third.active == active.snapshot()
    assert third.queued == newest.snapshot()
    assert third.superseded_before_start == replaced.snapshot()
    assert third.superseded_active is None


@pytest.mark.parametrize("phase", [
    TurnPhase.GENERATING,
    TurnPhase.EXECUTING_TOOL,
    TurnPhase.WAITING_APPROVAL,
    TurnPhase.VERIFYING,
    TurnPhase.RECOVERING,
])
def test_admission_supersedes_each_active_execution_phase(turn_store, phase):
    from agentcore.realtime import TurnController

    ids = _Ids()
    coordinator = _coordinator(turn_store, ids)
    first = coordinator.admit("conv_000000000001")
    record = turn_store.load(first.admitted.turn_id)
    controller = TurnController(
        turn_store, record.turn_id, clock=FixedClock(NOW))
    paths = {
        TurnPhase.GENERATING: (TurnPhase.GENERATING,),
        TurnPhase.EXECUTING_TOOL: (
            TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL),
        TurnPhase.WAITING_APPROVAL: (
            TurnPhase.GENERATING, TurnPhase.WAITING_APPROVAL),
        TurnPhase.VERIFYING: (
            TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL,
            TurnPhase.VERIFYING),
        TurnPhase.RECOVERING: (
            TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL,
            TurnPhase.VERIFYING, TurnPhase.RECOVERING),
    }
    for number, target in enumerate(paths[phase], start=0x100):
        controller.transition(
            record.phase,
            target,
            expected_version=record.version,
            operation_id=f"op_{number:012x}",
        )
        record = turn_store.load(record.turn_id)

    successor = coordinator.admit("conv_000000000001")

    active = turn_store.load(first.admitted.turn_id)
    assert active.phase is TurnPhase.CANCELLING
    assert active.cancellation_reason is CancellationReason.SUPERSEDED
    assert successor.superseded_active == active.snapshot()
    assert successor.queued == successor.admitted


def test_new_coordinator_reconstructs_generation_and_slots(turn_store):
    ids = _Ids()
    first = _coordinator(turn_store, ids)
    admissions = [first.admit("conv_000000000001") for _ in range(3)]

    restarted = _coordinator(turn_store, ids)
    fourth = restarted.admit("conv_000000000001")

    assert fourth.admitted.generation == 4
    assert fourth.active.turn_id == admissions[0].admitted.turn_id
    assert fourth.active.phase is TurnPhase.CANCELLING
    third = turn_store.load(admissions[2].admitted.turn_id)
    assert third.phase is TurnPhase.CANCELLED
    assert third.cancellation_reason is \
        CancellationReason.SUPERSEDED_BEFORE_START
    assert fourth.queued == turn_store.load(
        fourth.admitted.turn_id).snapshot()


def test_simultaneous_same_session_admissions_follow_lane_order(turn_store):
    coordinator = _coordinator(turn_store)
    barrier = threading.Barrier(3)
    results = []
    errors = []
    output_lock = threading.Lock()

    def admit() -> None:
        barrier.wait(timeout=5)
        try:
            result = coordinator.admit("conv_000000000001")
            with output_lock:
                results.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    threads = [threading.Thread(target=admit) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    ordered = sorted(results, key=lambda item: item.admitted.generation)
    assert [item.admitted.generation for item in ordered] == [1, 2, 3]
    records = sorted(
        (turn_store.load(turn_id) for turn_id in turn_store.list_turns()),
        key=lambda record: record.generation,
    )
    assert [record.phase for record in records] == [
        TurnPhase.CANCELLING,
        TurnPhase.CANCELLED,
        TurnPhase.QUEUED,
    ]
    assert records[0].cancellation_reason is CancellationReason.SUPERSEDED
    assert records[1].cancellation_reason is \
        CancellationReason.SUPERSEDED_BEFORE_START


def test_independent_sessions_keep_separate_generations_and_slots(turn_store):
    coordinator = _coordinator(turn_store)
    barrier = threading.Barrier(2)
    first_results = []
    errors = []
    output_lock = threading.Lock()

    def admit(session_id: str) -> None:
        barrier.wait(timeout=5)
        try:
            result = coordinator.admit(session_id)
            with output_lock:
                first_results.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    sessions = ("conv_000000000001", "conv_000000000002")
    threads = [threading.Thread(target=admit, args=(session,))
               for session in sessions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert {result.admitted.session_id for result in first_results} == \
        set(sessions)
    assert {result.admitted.generation for result in first_results} == {1}

    second_results = [coordinator.admit(session) for session in sessions]
    assert {result.admitted.generation for result in second_results} == {2}
    records = [turn_store.load(turn_id)
               for turn_id in turn_store.list_turns()]
    for session in sessions:
        own = sorted((record for record in records
                      if record.session_id == session),
                     key=lambda record: record.generation)
        assert [record.generation for record in own] == [1, 2]
        assert [record.phase for record in own] == [
            TurnPhase.CANCELLING, TurnPhase.QUEUED]


def test_inconsistent_durable_slots_fail_closed_before_new_identity(turn_store):
    ids = _Ids()
    first = _coordinator(turn_store, ids)
    admitted = first.admit("conv_000000000001")

    # A second independently created QUEUED record bypasses the coordinator
    # contract.  Reopening must reject it rather than guess which is active.
    from agentcore.realtime import TurnController, new_turn

    record = new_turn(
        "conv_000000000001",
        2,
        clock=FixedClock(NOW),
        id_factory=lambda prefix: "turn_ffffffffffff",
    )
    turn_store.create(record)
    controller = TurnController(
        turn_store, record.turn_id, clock=FixedClock(NOW))
    controller.transition(
        TurnPhase.CREATED,
        TurnPhase.QUEUED,
        expected_version=0,
        operation_id="op_ffffffffffff",
    )

    reopened = _coordinator(turn_store, ids)
    with pytest.raises(TurnCoordinatorError):
        reopened.admit("conv_000000000001")
    assert turn_store.load(admitted.admitted.turn_id).phase is TurnPhase.QUEUED
    assert len(turn_store.list_turns()) == 2


def test_prepared_identity_fails_safely_when_supersession_cas_fails():
    delegate = InMemoryTurnStore()
    ids = _Ids()
    first = _coordinator(delegate, ids)
    active = first.admit("conv_000000000001")

    class FailActiveCas:
        def create(self, record):
            return delegate.create(record)

        def load(self, turn_id):
            return delegate.load(turn_id)

        def compare_and_swap(self, turn_id, expected_version, next_record):
            if turn_id == active.admitted.turn_id:
                raise TurnVersionConflictError(
                    "injected active CAS failure")
            return delegate.compare_and_swap(
                turn_id, expected_version, next_record)

        def list_turns(self):
            return delegate.list_turns()

        def close(self):
            delegate.close()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    coordinator = _coordinator(FailActiveCas(), ids)
    with pytest.raises(TurnVersionConflictError):
        coordinator.admit("conv_000000000001")

    records = sorted(
        (delegate.load(turn_id) for turn_id in delegate.list_turns()),
        key=lambda record: record.generation,
    )
    assert records[0].phase is TurnPhase.QUEUED
    assert records[1].phase is TurnPhase.FAILED
    assert records[1].cancellation_reason is \
        CancellationReason.INTERNAL_FAILURE
