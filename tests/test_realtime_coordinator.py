from __future__ import annotations

import os
import select
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import agentcore.realtime.coordinator as coordinator_module

from agentcore.realtime import (
    CancellationReason,
    Deadline,
    InMemoryTurnStore,
    JsonFileTurnStore,
    TurnAlreadyExistsError,
    TurnController,
    TurnCoordinator,
    TurnCoordinatorError,
    TurnPhase,
    TurnStore,
    TurnStoreError,
    TurnStoreOwnershipError,
    TurnVersionConflictError,
    new_turn,
    reconcile_nonterminal_turns,
)
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


class _Ids:
    def __init__(self, start: int = 0) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {
            "turn": start,
            "op": start,
        }

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
        @property
        def admission_coordination(self):
            return delegate.admission_coordination

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
    with pytest.raises(TurnCoordinatorError):
        coordinator.admit("conv_000000000001")

    records = sorted(
        (delegate.load(turn_id) for turn_id in delegate.list_turns()),
        key=lambda record: record.generation,
    )
    assert records[0].phase is TurnPhase.QUEUED
    assert records[1].phase is TurnPhase.FAILED
    assert records[1].cancellation_reason is \
        CancellationReason.INTERNAL_FAILURE


class _ReadBarrierStore:
    """Force concurrent coordinators to finish the same initial store scan."""

    def __init__(self, delegate, barrier):
        self._delegate = delegate
        self._barrier = barrier

    def create(self, record):
        return self._delegate.create(record)

    def load(self, turn_id):
        return self._delegate.load(turn_id)

    def compare_and_swap(self, turn_id, expected_version, next_record):
        return self._delegate.compare_and_swap(
            turn_id, expected_version, next_record)

    def list_turns(self):
        result = self._delegate.list_turns()
        try:
            self._barrier.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            pass
        return result

    @property
    def admission_coordination(self):
        return self._delegate.admission_coordination

    def close(self):
        return self._delegate.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.mark.parametrize("backend", ["memory", "json"])
def test_two_coordinators_share_store_domain_admission_lane(backend, tmp_path):
    stores = ([InMemoryTurnStore()] * 2 if backend == "memory" else [
        JsonFileTurnStore(tmp_path), JsonFileTurnStore(tmp_path)])
    barrier = threading.Barrier(2)
    wrapped = [_ReadBarrierStore(store, barrier) for store in stores]
    coordinators = [
        _coordinator(wrapped[0], _Ids(0)),
        _coordinator(wrapped[1], _Ids(0x100)),
    ]
    results = []
    errors = []
    output_lock = threading.Lock()

    def admit(coordinator):
        try:
            result = coordinator.admit("conv_000000000010")
            with output_lock:
                results.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    threads = [threading.Thread(target=admit, args=(coordinator,))
               for coordinator in coordinators]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not [thread for thread in threads if thread.is_alive()]
        assert errors == []
        assert sorted(result.admitted.generation for result in results) == [1, 2]
        records = [stores[0].load(turn_id)
                   for turn_id in stores[0].list_turns()]
        assert sorted(record.generation for record in records) == [1, 2]
        assert sorted((record.phase for record in records),
                      key=lambda phase: phase.value) == sorted(
                          [TurnPhase.CANCELLING, TurnPhase.QUEUED],
                          key=lambda phase: phase.value)
        assert stores[0].admission_coordination.lanes == {}
    finally:
        if backend == "json":
            stores[1].close()
        stores[0].close()


def test_inherited_pid_fence_precedes_every_cached_lane_access(monkeypatch):
    store = InMemoryTurnStore()
    coordinator = _coordinator(store, _Ids())
    lane = coordinator._lane("conv_0000000000ef")
    entered = False
    left = False

    def record_enter():
        nonlocal entered
        entered = True

    def record_leave():
        nonlocal left
        left = True

    monkeypatch.setattr(lane, "enter", record_enter)
    monkeypatch.setattr(lane, "leave", record_leave)
    original_pid = os.getpid()
    monkeypatch.setattr(coordinator_module.os, "getpid",
                        lambda: original_pid + 1)
    try:
        with pytest.raises(TurnStoreOwnershipError):
            coordinator.admit("conv_0000000000ef")
        with pytest.raises(TurnStoreOwnershipError):
            coordinator._lane("conv_0000000000ef")
        with pytest.raises(TurnStoreOwnershipError):
            coordinator._enter_lane(lane)
        with pytest.raises(TurnStoreOwnershipError):
            coordinator._leave_lane(lane)
        with pytest.raises(TurnStoreOwnershipError):
            coordinator._release_lane("conv_0000000000ef", lane)
        assert not entered
        assert not left
    finally:
        monkeypatch.setattr(coordinator_module.os, "getpid",
                            lambda: original_pid)
        coordinator._release_lane("conv_0000000000ef", lane)
        store.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_during_admission_fences_inherited_lane_cleanup(tmp_path):
    store = JsonFileTurnStore(tmp_path)
    coordination = store.admission_coordination
    parent_pid = os.getpid()
    hold_requested = threading.Event()
    condition_held = threading.Event()
    release_condition = threading.Event()
    forked = threading.Event()
    child_read, child_write = os.pipe()
    child_pid = []
    child_reaped = False
    parent_results = []
    parent_errors = []

    class ForkingIds(_Ids):
        def __init__(self):
            super().__init__()
            self._forked_once = False

        def __call__(self, prefix):
            if prefix == "turn" and not self._forked_once:
                self._forked_once = True
                hold_requested.set()
                if not condition_held.wait(timeout=5):
                    raise AssertionError("lane condition was not held")
                pid = os.fork()
                if pid == 0:  # pragma: no cover - assertions run in child
                    os.close(child_read)
                else:
                    child_pid.append(pid)
                    forked.set()
            return super().__call__(prefix)

    coordinator = _coordinator(store, ForkingIds())

    def hold_lane_condition() -> None:
        assert hold_requested.wait(timeout=5)
        with coordination.lock:
            assert len(coordination.lanes) == 1
            lane = next(iter(coordination.lanes.values()))
        with lane.condition:
            condition_held.set()
            release_condition.wait()

    def run_admission() -> None:
        try:
            result = coordinator.admit("conv_0000000000ee")
        except TurnStoreOwnershipError:
            if os.getpid() == parent_pid:
                parent_errors.append("parent ownership failure")
                return
            os.write(child_write, b"ownership")
            os._exit(0)
        except BaseException as exc:
            if os.getpid() == parent_pid:
                parent_errors.append(exc)
                return
            os.write(child_write, b"unexpected")
            os._exit(2)
        if os.getpid() != parent_pid:  # pragma: no cover - child failure
            os.write(child_write, b"admitted")
            os._exit(3)
        parent_results.append(result)

    holder = threading.Thread(target=hold_lane_condition)
    admission = threading.Thread(target=run_admission)
    holder.start()
    admission.start()
    try:
        assert forked.wait(timeout=5), "admission did not fork"
        os.close(child_write)
        child_write = -1
        readable, _, _ = select.select([child_read], [], [], 5)
        assert child_read in readable, (
            "forked child hung while leaving its inherited lane")
        assert os.read(child_read, 32) == b"ownership"
        exited, _, _ = select.select([child_read], [], [], 5)
        assert child_read in exited, "forked child did not exit"
        assert os.read(child_read, 1) == b""
        deadline = time.monotonic() + 5
        waited_pid = 0
        status = 0
        while waited_pid == 0 and time.monotonic() < deadline:
            waited_pid, status = os.waitpid(child_pid[0], os.WNOHANG)
            if waited_pid == 0:
                time.sleep(0.01)
        assert waited_pid == child_pid[0]
        child_reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        release_condition.set()
        admission.join(timeout=5)
        holder.join(timeout=5)
        assert not admission.is_alive()
        assert not holder.is_alive()
        if child_pid and not child_reaped:
            try:
                os.kill(child_pid[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid[0], 0)
            except ChildProcessError:
                pass
        for descriptor in (child_read, child_write):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass
        store.close()

    assert parent_errors == []
    assert len(parent_results) == 1
    assert store.admission_coordination.lanes == {}


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_rejects_inherited_coordinator_before_cached_lock(
        tmp_path):
    store = JsonFileTurnStore(tmp_path)
    coordinator = _coordinator(store, _Ids())
    coordination = store.admission_coordination
    lock_held = threading.Event()
    release_lock = threading.Event()
    child_read, child_write = os.pipe()
    child_pid = None
    child_reaped = False

    def hold_admission_lock() -> None:
        with coordination.lock:
            lock_held.set()
            release_lock.wait()

    holder = threading.Thread(target=hold_admission_lock)
    holder.start()
    try:
        assert lock_held.wait(timeout=5), "parent admission lock was not held"
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - assertions run in child
            os.close(child_read)
            try:
                coordinator.admit("conv_0000000000f0")
            except TurnStoreOwnershipError:
                os.write(child_write, b"ownership")
                os._exit(0)
            except BaseException:
                os.write(child_write, b"unexpected")
                os._exit(2)
            else:
                os.write(child_write, b"admitted")
                os._exit(3)

        os.close(child_write)
        child_write = -1
        readable, _, _ = select.select([child_read], [], [], 5)
        assert child_read in readable, (
            "forked child hung on the inherited admission lock")
        assert os.read(child_read, 32) == b"ownership"
        waited_pid, status = os.waitpid(child_pid, 0)
        child_reaped = True
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        release_lock.set()
        holder.join(timeout=5)
        assert not holder.is_alive()
        if child_pid not in (None, 0) and not child_reaped:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
        for descriptor in (child_read, child_write):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass
        store.close()


class _AdvanceOnCreateStore:
    def __init__(self, delegate, advance):
        self._delegate = delegate
        self._advance = advance
        self.enabled = False

    @property
    def admission_coordination(self):
        return self._delegate.admission_coordination

    def create(self, record):
        if self.enabled:
            self.enabled = False
            self._advance()
        return self._delegate.create(record)

    def load(self, turn_id):
        return self._delegate.load(turn_id)

    def compare_and_swap(self, turn_id, expected_version, next_record):
        return self._delegate.compare_and_swap(
            turn_id, expected_version, next_record)

    def list_turns(self):
        return self._delegate.list_turns()

    def close(self):
        return self._delegate.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.mark.parametrize("advance_to", [
    "generating",
    "finalizing",
    "completed",
    "cancelling",
])
def test_admission_reclassifies_active_after_stage_transition(advance_to):
    delegate = InMemoryTurnStore()
    clock = FixedClock(NOW)
    active_id = []

    def advance():
        record = delegate.load(active_id[0])
        controller = TurnController(delegate, record.turn_id, clock=clock)
        if advance_to == "generating":
            controller.transition(
                TurnPhase.QUEUED, TurnPhase.GENERATING,
                expected_version=record.version,
                operation_id="op_eeeeeeeeeee1")
        elif advance_to == "cancelling":
            controller.transition(
                TurnPhase.QUEUED, TurnPhase.CANCELLING,
                expected_version=record.version,
                operation_id="op_eeeeeeeeeee2",
                cancellation_reason=CancellationReason.USER_INTERRUPT,
                requested_terminal=TurnPhase.CANCELLED)
        else:
            generating = controller.transition(
                TurnPhase.QUEUED, TurnPhase.GENERATING,
                expected_version=record.version,
                operation_id="op_eeeeeeeeeee3")
            finalizing = controller.transition(
                TurnPhase.GENERATING, TurnPhase.FINALIZING,
                expected_version=generating.version,
                operation_id="op_eeeeeeeeeee4",
                requested_terminal=TurnPhase.COMPLETED)
            if advance_to == "completed":
                controller.transition(
                    TurnPhase.FINALIZING, TurnPhase.COMPLETED,
                    expected_version=finalizing.version,
                    operation_id="op_eeeeeeeeeee5")

    store = _AdvanceOnCreateStore(delegate, advance)
    coordinator = _coordinator(store, _Ids())
    first = coordinator.admit("conv_000000000020")
    active_id.append(first.admitted.turn_id)
    store.enabled = True

    successor = coordinator.admit("conv_000000000020")

    current = delegate.load(first.admitted.turn_id)
    if advance_to == "completed":
        assert current.phase is TurnPhase.COMPLETED
        assert successor.active == successor.admitted
        assert successor.queued is None
    elif advance_to == "finalizing":
        assert current.phase is TurnPhase.FINALIZING
        assert successor.active == current.snapshot()
        assert successor.queued == successor.admitted
    else:
        assert current.phase is TurnPhase.CANCELLING
        expected_reason = (CancellationReason.SUPERSEDED
                           if advance_to == "generating"
                           else CancellationReason.USER_INTERRUPT)
        assert current.cancellation_reason is expected_reason
        assert successor.queued == successor.admitted


class _AdvanceInsideActiveCasStore:
    """Persist a stage-owner transition at the coordinator CAS boundary."""

    def __init__(self, delegate, active_id, target, clock):
        self._delegate = delegate
        self._active_id = active_id
        self._target = target
        self._clock = clock
        self.injected = None

    @property
    def admission_coordination(self):
        return self._delegate.admission_coordination

    def create(self, record):
        return self._delegate.create(record)

    def load(self, turn_id):
        return self._delegate.load(turn_id)

    def compare_and_swap(self, turn_id, expected_version, next_record):
        if self.injected is None and turn_id == self._active_id and \
                next_record.phase is TurnPhase.CANCELLING and \
                next_record.cancellation_reason is \
                CancellationReason.SUPERSEDED:
            self.injected = self._advance_active()
        return self._delegate.compare_and_swap(
            turn_id, expected_version, next_record)

    def list_turns(self):
        return self._delegate.list_turns()

    def close(self):
        return self._delegate.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _advance_active(self):
        record = self._delegate.load(self._active_id)
        controller = TurnController(
            self._delegate, record.turn_id, clock=self._clock)
        operation = 0xD00

        def transition(target, **kwargs):
            nonlocal record, operation
            controller.transition(
                record.phase,
                target,
                expected_version=record.version,
                operation_id=f"op_{operation:012x}",
                **kwargs,
            )
            record = self._delegate.load(self._active_id)
            operation += 1

        if self._target is TurnPhase.COMPLETED:
            transition(TurnPhase.GENERATING)
            transition(
                TurnPhase.FINALIZING,
                requested_terminal=TurnPhase.COMPLETED,
            )
            transition(TurnPhase.COMPLETED)
            return record

        outcomes = {
            TurnPhase.CANCELLING: (
                CancellationReason.USER_INTERRUPT, TurnPhase.CANCELLED),
            TurnPhase.FINALIZING: (
                CancellationReason.USER_INTERRUPT, TurnPhase.CANCELLED),
            TurnPhase.CANCELLED: (
                CancellationReason.USER_INTERRUPT, TurnPhase.CANCELLED),
            TurnPhase.TIMED_OUT: (
                CancellationReason.DEADLINE_EXCEEDED, TurnPhase.TIMED_OUT),
            TurnPhase.FAILED: (
                CancellationReason.INTERNAL_FAILURE, TurnPhase.FAILED),
        }
        reason, terminal = outcomes[self._target]
        transition(
            TurnPhase.CANCELLING,
            cancellation_reason=reason,
            requested_terminal=terminal,
        )
        if self._target is TurnPhase.CANCELLING:
            return record
        transition(
            TurnPhase.FINALIZING,
            cancellation_reason=reason,
            requested_terminal=terminal,
        )
        if self._target is TurnPhase.FINALIZING:
            return record
        transition(terminal)
        return record


@pytest.mark.parametrize("competing_phase", [
    TurnPhase.CANCELLING,
    TurnPhase.FINALIZING,
    TurnPhase.CANCELLED,
    TurnPhase.TIMED_OUT,
    TurnPhase.FAILED,
    TurnPhase.COMPLETED,
])
def test_admission_reclassifies_transition_inside_active_cas(competing_phase):
    delegate = InMemoryTurnStore()
    clock = FixedClock(NOW)
    ids = _Ids()
    first = _coordinator(delegate, ids).admit("conv_000000000021")
    store = _AdvanceInsideActiveCasStore(
        delegate, first.admitted.turn_id, competing_phase, clock)

    successor = _coordinator(store, ids).admit("conv_000000000021")

    assert store.injected is not None
    assert delegate.load(first.admitted.turn_id) == store.injected
    persisted_successor = delegate.load(successor.admitted.turn_id)
    assert persisted_successor.phase is TurnPhase.QUEUED
    assert persisted_successor.generation == 2
    if competing_phase.is_terminal:
        assert successor.active == persisted_successor.snapshot()
        assert successor.queued is None
    else:
        assert successor.active == store.injected.snapshot()
        assert successor.queued == persisted_successor.snapshot()


def test_independent_store_domains_keep_independent_admission_lanes():
    barrier = threading.Barrier(2)

    class BarrierIds(_Ids):
        def __call__(self, prefix):
            if prefix == "turn":
                barrier.wait(timeout=5)
            return super().__call__(prefix)

    stores = [InMemoryTurnStore(), InMemoryTurnStore()]
    coordinators = [
        _coordinator(stores[0], BarrierIds(0)),
        _coordinator(stores[1], BarrierIds(0x100)),
    ]
    results = []
    errors = []
    output_lock = threading.Lock()

    def admit(coordinator, session):
        try:
            result = coordinator.admit(session)
            with output_lock:
                results.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    threads = [
        threading.Thread(
            target=admit,
            args=(coordinator, f"conv_{number:012x}"),
        )
        for number, coordinator in enumerate(coordinators, start=1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(results) == 2
    assert all(store.admission_coordination.lanes == {} for store in stores)


class _ContinuallyAdvanceStore(_AdvanceOnCreateStore):
    def __init__(self, delegate, active_id, clock):
        super().__init__(delegate, lambda: None)
        self._active_id = active_id
        self._clock = clock
        self.enabled = False
        self.conflicts = 0

    def create(self, record):
        return self._delegate.create(record)

    def compare_and_swap(self, turn_id, expected_version, next_record):
        if self.enabled and turn_id == self._active_id:
            current = self._delegate.load(turn_id)
            target = (TurnPhase.EXECUTING_TOOL
                      if current.phase is TurnPhase.GENERATING
                      else TurnPhase.GENERATING)
            TurnController(
                self._delegate, turn_id, clock=self._clock).transition(
                    current.phase, target,
                    expected_version=current.version,
                    operation_id=f"op_{0xf000 + self.conflicts:012x}")
            self.conflicts += 1
        return self._delegate.compare_and_swap(
            turn_id, expected_version, next_record)


def test_admission_conflict_reclassification_is_bounded_and_fails_safe():
    delegate = InMemoryTurnStore()
    clock = FixedClock(NOW)
    store = _ContinuallyAdvanceStore(delegate, "", clock)
    coordinator = _coordinator(store, _Ids())
    first = coordinator.admit("conv_000000000030")
    store._active_id = first.admitted.turn_id
    current = delegate.load(first.admitted.turn_id)
    TurnController(delegate, current.turn_id, clock=clock).transition(
        TurnPhase.QUEUED, TurnPhase.GENERATING,
        expected_version=current.version,
        operation_id="op_eeeeeeeeeeef")
    store.enabled = True

    with pytest.raises(TurnCoordinatorError):
        coordinator.admit("conv_000000000030")

    assert 1 < store.conflicts < 100
    records = [delegate.load(turn_id) for turn_id in delegate.list_turns()]
    assert len([record for record in records if not record.is_terminal]) == 1
    assert any(record.phase is TurnPhase.FAILED for record in records)


@pytest.mark.parametrize("advance_to", [
    "generating",
    "executing_tool",
    "finalizing_committed",
])
def test_admission_reclassifies_queued_successor_that_started(advance_to):
    delegate = InMemoryTurnStore()
    clock = FixedClock(NOW)
    ids = _Ids()
    coordinator = _coordinator(delegate, ids)
    first = coordinator.admit("conv_000000000031")
    second = coordinator.admit("conv_000000000031")
    queued_id = second.admitted.turn_id

    def advance():
        record = delegate.load(queued_id)
        controller = TurnController(delegate, record.turn_id, clock=clock)
        controller.transition(
            TurnPhase.QUEUED, TurnPhase.GENERATING,
            expected_version=record.version,
            operation_id="op_e00000000001")
        if advance_to == "executing_tool":
            generating = delegate.load(queued_id)
            controller.transition(
                TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL,
                expected_version=generating.version,
                operation_id="op_e00000000002")
        elif advance_to == "finalizing_committed":
            generating = delegate.load(queued_id)
            controller.transition(
                TurnPhase.GENERATING, TurnPhase.FINALIZING,
                expected_version=generating.version,
                operation_id="op_e00000000003",
                requested_terminal=TurnPhase.COMPLETED)

    # The queued successor's stage owner activates it while the newer
    # admission sits between its session read and its replacement attempt:
    # _AdvanceOnCreateStore fires the activation at exactly that boundary.
    store = _AdvanceOnCreateStore(delegate, advance)
    store.enabled = True

    # Before the fix this raised TurnVersionConflictError: the stale
    # QUEUED snapshot lost the CAS against the successor that started.
    third = _coordinator(store, ids).admit("conv_000000000031")

    started = delegate.load(queued_id)
    newest = delegate.load(third.admitted.turn_id)
    if advance_to == "finalizing_committed":
        # A committed, finalizing turn is never superseded or regressed.
        assert started.phase is TurnPhase.FINALIZING
        assert started.requested_terminal is TurnPhase.COMPLETED
        assert started.cancellation_reason is None
        assert third.superseded_active is None
        assert third.superseded_before_start is None
    else:
        # It started, so its replacement is active-work supersession with
        # the truthful SUPERSEDED vocabulary, never before-start.
        assert started.phase is TurnPhase.CANCELLING
        assert started.cancellation_reason is CancellationReason.SUPERSEDED
        assert started.requested_terminal is TurnPhase.CANCELLED
        assert third.superseded_active == started.snapshot()
        assert third.superseded_before_start is None
    assert newest.phase is TurnPhase.QUEUED
    assert newest.generation == 3
    active = delegate.load(first.admitted.turn_id)
    assert active.cancellation_reason is CancellationReason.SUPERSEDED


class _OwnerTerminalizeInsideReplacementCasStore:
    """The queued record's owner terminalizes during its own replacement.

    Fires once, at the compare-and-swap that would carry the queued
    successor's replacement from CANCELLING to FINALIZING: at that instant
    the record's own stage owner (which acquired the stage lease after the
    first replacement CAS) drives the very same cancellation to terminal
    first. The admission's CAS loses and must reclassify, not leak.
    """

    def __init__(self, delegate, queued_id, clock):
        self._delegate = delegate
        self._queued_id = queued_id
        self._clock = clock
        self.injected = False

    @property
    def admission_coordination(self):
        return self._delegate.admission_coordination

    def create(self, record):
        return self._delegate.create(record)

    def load(self, turn_id):
        return self._delegate.load(turn_id)

    def compare_and_swap(self, turn_id, expected_version, next_record):
        if not self.injected and turn_id == self._queued_id and \
                next_record.phase is TurnPhase.FINALIZING and \
                next_record.cancellation_reason is \
                CancellationReason.SUPERSEDED_BEFORE_START:
            self.injected = True
            record = self._delegate.load(turn_id)
            controller = TurnController(
                self._delegate, turn_id, clock=self._clock)
            controller.transition(
                TurnPhase.CANCELLING, TurnPhase.FINALIZING,
                expected_version=record.version,
                operation_id="op_e00000000004",
                cancellation_reason=record.cancellation_reason,
                requested_terminal=record.requested_terminal)
            record = self._delegate.load(turn_id)
            controller.transition(
                TurnPhase.FINALIZING, TurnPhase.CANCELLED,
                expected_version=record.version,
                operation_id="op_e00000000005")
        return self._delegate.compare_and_swap(
            turn_id, expected_version, next_record)

    def list_turns(self):
        return self._delegate.list_turns()

    def close(self):
        return self._delegate.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_admission_survives_owner_terminalizing_the_replacement_in_parallel():
    delegate = InMemoryTurnStore()
    clock = FixedClock(NOW)
    ids = _Ids()
    coordinator = _coordinator(delegate, ids)
    coordinator.admit("conv_000000000032")
    second = coordinator.admit("conv_000000000032")
    store = _OwnerTerminalizeInsideReplacementCasStore(
        delegate, second.admitted.turn_id, clock)

    # Before the fix the second replacement transition raised
    # TurnVersionConflictError and destroyed the newer admission.
    third = _coordinator(store, ids).admit("conv_000000000032")

    assert store.injected
    replaced = delegate.load(second.admitted.turn_id)
    assert replaced.phase is TurnPhase.CANCELLED
    assert replaced.cancellation_reason is \
        CancellationReason.SUPERSEDED_BEFORE_START
    assert replaced.final_terminal is TurnPhase.CANCELLED
    assert third.superseded_before_start == replaced.snapshot()
    assert third.superseded_active is None
    newest = delegate.load(third.admitted.turn_id)
    assert newest.phase is TurnPhase.QUEUED
    assert newest.generation == 3


@pytest.mark.parametrize("existing_kind", [
    "active",
    "terminal",
    "cross_session",
])
def test_turn_id_collision_never_mutates_existing_record(existing_kind):
    store = InMemoryTurnStore()
    clock = FixedClock(NOW)
    session_id = ("conv_000000000042" if existing_kind != "cross_session"
                  else "conv_000000000099")
    record = new_turn(
        session_id, 1, clock=clock,
        id_factory=lambda prefix: "turn_000000000042")
    store.create(record)
    controller = TurnController(store, record.turn_id, clock=clock)
    queued = controller.transition(
        TurnPhase.CREATED, TurnPhase.QUEUED,
        expected_version=0, operation_id="op_000000000042")
    if existing_kind == "terminal":
        generating = controller.transition(
            TurnPhase.QUEUED, TurnPhase.GENERATING,
            expected_version=queued.version,
            operation_id="op_000000000043")
        finalizing = controller.transition(
            TurnPhase.GENERATING, TurnPhase.FINALIZING,
            expected_version=generating.version,
            operation_id="op_000000000044",
            requested_terminal=TurnPhase.COMPLETED)
        controller.transition(
            TurnPhase.FINALIZING, TurnPhase.COMPLETED,
            expected_version=finalizing.version,
            operation_id="op_000000000045")
    before = store.load(record.turn_id).to_dict()

    class CollisionIds:
        def __init__(self):
            self.operation = 0x500

        def __call__(self, prefix):
            if prefix == "turn":
                return record.turn_id
            self.operation += 1
            return f"op_{self.operation:012x}"

    coordinator = _coordinator(store, CollisionIds())
    with pytest.raises(TurnAlreadyExistsError):
        coordinator.admit("conv_000000000042")

    assert store.load(record.turn_id).to_dict() == before


def test_ambiguous_create_cleanup_only_fails_exact_written_record():
    delegate = InMemoryTurnStore()

    class AmbiguousCreate:
        @property
        def admission_coordination(self):
            return delegate.admission_coordination

        def create(self, record):
            delegate.create(record)
            raise TurnStoreError("simulated post-write ambiguity")

        def load(self, turn_id):
            return delegate.load(turn_id)

        def compare_and_swap(self, turn_id, expected_version, next_record):
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

    with pytest.raises(TurnStoreError):
        _coordinator(AmbiguousCreate(), _Ids()).admit("conv_000000000050")
    stored = delegate.load(delegate.list_turns()[0])
    assert stored.phase is TurnPhase.FAILED
    assert stored.cancellation_reason is CancellationReason.INTERNAL_FAILURE


@pytest.mark.parametrize("boundary", [
    "prepared",
    "active_superseded",
    "queued_cancelling",
    "successor_queued",
])
def test_restart_during_admission_boundaries_never_leaves_live_work(boundary):
    store = InMemoryTurnStore()
    clock = FixedClock(NOW)
    coordinator = _coordinator(store, _Ids())
    first = coordinator.admit("conv_000000000060")
    if boundary in {"queued_cancelling", "successor_queued"}:
        second = coordinator.admit("conv_000000000060")
    if boundary == "prepared":
        prepared = new_turn(
            "conv_000000000060", 2, clock=clock,
            id_factory=lambda prefix: "turn_0000000000f1")
        store.create(prepared)
    elif boundary == "active_superseded":
        prepared = new_turn(
            "conv_000000000060", 2, clock=clock,
            id_factory=lambda prefix: "turn_0000000000f2")
        store.create(prepared)
        record = store.load(first.admitted.turn_id)
        TurnController(store, record.turn_id, clock=clock).transition(
            record.phase, TurnPhase.CANCELLING,
            expected_version=record.version,
            operation_id="op_0000000000f2",
            cancellation_reason=CancellationReason.SUPERSEDED,
            requested_terminal=TurnPhase.CANCELLED)
    elif boundary == "queued_cancelling":
        prepared = new_turn(
            "conv_000000000060", 3, clock=clock,
            id_factory=lambda prefix: "turn_0000000000f3")
        store.create(prepared)
        record = store.load(second.admitted.turn_id)
        TurnController(store, record.turn_id, clock=clock).transition(
            record.phase, TurnPhase.CANCELLING,
            expected_version=record.version,
            operation_id="op_0000000000f3",
            cancellation_reason=CancellationReason.SUPERSEDED_BEFORE_START,
            requested_terminal=TurnPhase.CANCELLED)
    else:
        prepared = store.load(second.admitted.turn_id)

    reconcile_nonterminal_turns(store, clock=clock)
    records = [store.load(turn_id) for turn_id in store.list_turns()]
    assert records
    assert all(record.is_terminal for record in records)
    assert all(record.final_terminal is not TurnPhase.COMPLETED
               for record in records)


class _FailAfterJsonAdmissionBoundary:
    """Raise once after the selected real JSON write has completed."""

    def __init__(self, delegate, boundary):
        self._delegate = delegate
        self._boundary = boundary
        self.fired = False

    @property
    def admission_coordination(self):
        return self._delegate.admission_coordination

    def create(self, record):
        persisted = self._delegate.create(record)
        if self._boundary == "prepared_create" and not self.fired:
            self._fail()
        return persisted

    def load(self, turn_id):
        return self._delegate.load(turn_id)

    def compare_and_swap(self, turn_id, expected_version, next_record):
        persisted = self._delegate.compare_and_swap(
            turn_id, expected_version, next_record)
        if not self.fired and self._matches(persisted):
            self._fail()
        return persisted

    def list_turns(self):
        return self._delegate.list_turns()

    def close(self):
        return self._delegate.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _matches(self, record):
        return {
            "active_superseded": (
                record.phase is TurnPhase.CANCELLING
                and record.cancellation_reason is
                CancellationReason.SUPERSEDED
            ),
            "queued_cancelling": (
                record.phase is TurnPhase.CANCELLING
                and record.cancellation_reason is
                CancellationReason.SUPERSEDED_BEFORE_START
            ),
            "queued_finalizing": (
                record.phase is TurnPhase.FINALIZING
                and record.cancellation_reason is
                CancellationReason.SUPERSEDED_BEFORE_START
            ),
            "queued_terminal": (
                record.phase is TurnPhase.CANCELLED
                and record.cancellation_reason is
                CancellationReason.SUPERSEDED_BEFORE_START
            ),
            "successor_queued": record.phase is TurnPhase.QUEUED,
        }.get(self._boundary, False)

    def _fail(self):
        self.fired = True
        raise TurnStoreError(
            "injected failure after JSON admission persistence",
            boundary=self._boundary,
        )


@pytest.mark.parametrize(("boundary", "existing_admissions"), [
    ("prepared_create", 2),
    ("active_superseded", 1),
    ("queued_cancelling", 2),
    ("queued_finalizing", 2),
    ("queued_terminal", 2),
    ("successor_queued", 2),
])
def test_json_restart_after_each_admission_persistence_boundary(
        tmp_path, boundary, existing_admissions):
    directory = tmp_path / boundary
    ids = _Ids()
    store = JsonFileTurnStore(directory)
    coordinator = _coordinator(store, ids)
    for _ in range(existing_admissions):
        coordinator.admit("conv_000000000061")

    faulting = _FailAfterJsonAdmissionBoundary(store, boundary)
    try:
        with pytest.raises(TurnStoreError) as captured:
            _coordinator(faulting, ids).admit("conv_000000000061")
        assert captured.value.details["boundary"] == boundary
        assert faulting.fired
        assert len(store.list_turns()) == existing_admissions + 1
    finally:
        store.close()

    reopened = JsonFileTurnStore(directory)
    try:
        before = [reopened.load(turn_id)
                  for turn_id in reopened.list_turns()]
        generations = [record.generation for record in before]
        assert len(generations) == len(set(generations))
        assert len([record for record in before if not record.is_terminal]) <= 2

        reconcile_nonterminal_turns(reopened, clock=FixedClock(NOW))

        after = [reopened.load(turn_id)
                 for turn_id in reopened.list_turns()]
        assert len(after) == existing_admissions + 1
        assert all(record.is_terminal for record in after)
        assert not [record for record in after
                    if record.phase in {TurnPhase.CREATED, TurnPhase.QUEUED}]
        assert all(record.final_terminal is not TurnPhase.COMPLETED
                   for record in after)
    finally:
        reopened.close()
