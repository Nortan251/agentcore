from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agentcore.realtime.store as store_module
from agentcore.realtime import (
    CancellationReason,
    InMemoryTurnStore,
    JsonFileTurnStore,
    TurnAlreadyExistsError,
    TurnController,
    TurnCounters,
    TurnNotFoundError,
    TurnPhase,
    TurnStore,
    TurnStoreError,
    TurnStoreOwnershipError,
    TurnValidationError,
    TurnVersionConflictError,
    new_turn,
    reconcile_nonterminal_turns,
)
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _op(number: int) -> str:
    return f"op_{number:012x}"


def _ids(number: int):
    return lambda prefix: f"{prefix}_{number:012x}"


@pytest.fixture(params=["memory", "json"])
def turn_store(request, tmp_path):
    store = (InMemoryTurnStore() if request.param == "memory"
             else JsonFileTurnStore(tmp_path))
    try:
        yield store
    finally:
        store.close()


def _new_record(number: int = 1, *, action_refs=()):
    return new_turn(
        f"conv_{number:012x}", 1, clock=FixedClock(NOW),
        id_factory=_ids(number), action_refs=tuple(action_refs))


def _queued_candidate(record, operation=1):
    with InMemoryTurnStore() as shadow:
        shadow.create(record)
        controller = TurnController(
            shadow, record.turn_id, clock=FixedClock(NOW))
        controller.transition(
            TurnPhase.CREATED, TurnPhase.QUEUED,
            expected_version=0, operation_id=_op(operation))
        return shadow.load(record.turn_id)


def test_stores_satisfy_protocol_and_snapshot_records(turn_store):
    assert isinstance(turn_store, TurnStore)
    record = _new_record()
    created = turn_store.create(record)
    assert created == record
    assert created is not record
    assert turn_store.load(record.turn_id) == record
    assert turn_store.list_turns() == [record.turn_id]


@pytest.mark.parametrize("kind", ["memory", "json"])
def test_store_context_lifecycle_is_explicit_and_close_is_idempotent(
        kind, tmp_path):
    store = (InMemoryTurnStore() if kind == "memory"
             else JsonFileTurnStore(tmp_path))
    with store as entered:
        assert entered is store
        record = entered.create(_new_record())
        queued = _queued_candidate(record)
    store.close()
    operations = (
        lambda: store.load(record.turn_id),
        lambda: store.create(_new_record(2)),
        lambda: store.compare_and_swap(record.turn_id, 0, queued),
        store.list_turns,
        store.__enter__,
    )
    for operation in operations:
        with pytest.raises(TurnStoreError):
            operation()


def test_duplicate_create_and_missing_load_are_categorical(turn_store):
    record = turn_store.create(_new_record())
    with pytest.raises(TurnAlreadyExistsError):
        turn_store.create(record)
    with pytest.raises(TurnNotFoundError):
        turn_store.load("turn_ffffffffffff")


def test_create_accepts_only_a_strict_fresh_record(turn_store):
    record = _new_record()
    with pytest.raises(TurnValidationError):
        turn_store.create(_queued_candidate(record))
    counted = replace(record, counters=TurnCounters(model_requests=1))
    counted.validate()
    with pytest.raises(TurnValidationError):
        turn_store.create(counted)
    assert turn_store.list_turns() == []


@pytest.mark.parametrize("turn_id", ["../turn_000000000001", "", None])
def test_all_store_operations_validate_requested_turn_ids(turn_store, turn_id):
    record = turn_store.create(_new_record())
    queued = _queued_candidate(record)
    with pytest.raises(TurnValidationError):
        turn_store.load(turn_id)
    with pytest.raises(TurnValidationError):
        turn_store.compare_and_swap(turn_id, 0, queued)
    assert turn_store.load(record.turn_id) == record


def test_compare_and_swap_advances_exactly_one_version(turn_store):
    record = turn_store.create(_new_record())
    queued = _queued_candidate(record)
    accepted = turn_store.compare_and_swap(record.turn_id, 0, queued)
    assert accepted.phase is TurnPhase.QUEUED
    assert accepted.version == 1
    assert len(accepted.transitions) == 1
    # Retrying the exact persisted value is idempotent.
    assert turn_store.compare_and_swap(record.turn_id, 0, queued) == accepted


def test_version_conflict_never_overwrites_newer_record(turn_store):
    record = turn_store.create(_new_record())
    queued = _queued_candidate(record)
    turn_store.compare_and_swap(record.turn_id, 0, queued)
    competing = _queued_candidate(record, operation=2)
    with pytest.raises(TurnVersionConflictError):
        turn_store.compare_and_swap(record.turn_id, 0, competing)
    assert turn_store.load(record.turn_id) == queued


def test_cas_idempotence_accepts_only_the_canonical_next_version(turn_store):
    record = turn_store.create(_new_record())
    queued = _queued_candidate(record)
    accepted = turn_store.compare_and_swap(record.turn_id, 0, queued)
    assert turn_store.compare_and_swap(record.turn_id, 0, queued) == accepted
    with pytest.raises(TurnValidationError):
        turn_store.compare_and_swap(record.turn_id, 0, record)
    with pytest.raises(TurnValidationError):
        turn_store.compare_and_swap(record.turn_id, 1, queued)
    assert turn_store.load(record.turn_id) == accepted


def test_cas_rejects_identity_rewrite(turn_store):
    record = turn_store.create(_new_record())
    queued = _queued_candidate(record)
    forged = replace(queued, session_id="conv_ffffffffffff")
    forged.validate()
    with pytest.raises(TurnValidationError):
        turn_store.compare_and_swap(record.turn_id, 0, forged)
    assert turn_store.load(record.turn_id) == record


@pytest.mark.parametrize("changes", [
    {"generation": 2},
    {"created_at": NOW - timedelta(seconds=1)},
    {"run_id": "run_ffffffffffff"},
    {"task_id": "task_ffffffffffff"},
    {"task_id": "task_ffffffffffff",
     "subtask_id": "sub_ffffffffffff"},
    {"deadline_at": NOW + timedelta(minutes=1)},
    {"counters": TurnCounters(tool_calls=1)},
    {"action_refs": ("act_ffffffffffff",)},
])
def test_cas_pins_every_non_transition_field(turn_store, changes):
    record = turn_store.create(_new_record())
    forged = replace(_queued_candidate(record), **changes)
    forged.validate()
    with pytest.raises(TurnValidationError):
        turn_store.compare_and_swap(record.turn_id, 0, forged)
    assert turn_store.load(record.turn_id) == record


def test_listing_is_deterministic(turn_store):
    turn_store.create(_new_record(3))
    turn_store.create(_new_record(1))
    turn_store.create(_new_record(2))
    assert turn_store.list_turns() == [
        "turn_000000000001",
        "turn_000000000002",
        "turn_000000000003",
    ]


def test_two_same_operation_writers_commit_one_ledger_entry(turn_store):
    record = turn_store.create(_new_record())
    barrier = threading.Barrier(2)
    snapshots = []
    errors = []
    output_lock = threading.Lock()

    def write(clock):
        controller = TurnController(
            turn_store, record.turn_id, clock=clock)
        barrier.wait(timeout=5)
        try:
            result = controller.transition(
                TurnPhase.CREATED, TurnPhase.QUEUED,
                expected_version=0, operation_id=_op(1))
            with output_lock:
                snapshots.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(FixedClock(NOW),)),
        threading.Thread(
            target=write, args=(FixedClock(NOW + timedelta(seconds=1)),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(snapshots) == 2 and snapshots[0] == snapshots[1]
    persisted = turn_store.load(record.turn_id)
    assert persisted.version == 1
    assert len(persisted.transitions) == 1


def test_two_different_operation_writers_have_one_winner(turn_store):
    record = turn_store.create(_new_record())
    barrier = threading.Barrier(2)
    successes = []
    errors = []
    output_lock = threading.Lock()

    def write(operation):
        controller = TurnController(
            turn_store, record.turn_id, clock=FixedClock(NOW))
        barrier.wait(timeout=5)
        try:
            result = controller.transition(
                TurnPhase.CREATED, TurnPhase.QUEUED,
                expected_version=0, operation_id=operation)
            with output_lock:
                successes.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    threads = [threading.Thread(target=write, args=(_op(number),))
               for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not [thread for thread in threads if thread.is_alive()]
    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], TurnVersionConflictError)
    assert turn_store.load(record.turn_id).version == 1


def test_json_store_round_trips_across_instances(tmp_path):
    first = JsonFileTurnStore(tmp_path)
    record = first.create(_new_record())
    first.close()
    second = JsonFileTurnStore(tmp_path)
    try:
        assert second.load(record.turn_id) == record
    finally:
        second.close()


def test_same_process_json_instances_share_one_cas_domain(tmp_path):
    first = JsonFileTurnStore(tmp_path)
    second = JsonFileTurnStore(tmp_path)
    try:
        record = first.create(_new_record())
        controller = TurnController(second, record.turn_id, clock=FixedClock(NOW))
        controller.transition(
            TurnPhase.CREATED, TurnPhase.QUEUED,
            expected_version=0, operation_id=_op(1))
        assert first.load(record.turn_id).phase is TurnPhase.QUEUED
    finally:
        second.close()
        first.close()


def test_same_process_json_instances_linearize_competing_cas(tmp_path):
    first = JsonFileTurnStore(tmp_path)
    second = JsonFileTurnStore(tmp_path)
    try:
        record = first.create(_new_record())
        candidates = (
            _queued_candidate(record, operation=1),
            _queued_candidate(record, operation=2),
        )
        barrier = threading.Barrier(2)
        successes = []
        errors = []
        output_lock = threading.Lock()

        def write(store, candidate):
            barrier.wait(timeout=5)
            try:
                result = store.compare_and_swap(record.turn_id, 0, candidate)
                with output_lock:
                    successes.append(result)
            except BaseException as exc:
                with output_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=write, args=(first, candidates[0])),
            threading.Thread(target=write, args=(second, candidates[1])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not [thread for thread in threads if thread.is_alive()]
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], TurnVersionConflictError)
        assert first.load(record.turn_id) == successes[0]
    finally:
        second.close()
        first.close()


def _ownership_probe(directory: Path) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\n"
        "from agentcore.realtime import JsonFileTurnStore, "
        "TurnStoreOwnershipError\n"
        "try:\n"
        "    store = JsonFileTurnStore(sys.argv[1])\n"
        "except TurnStoreOwnershipError:\n"
        "    print('blocked')\n"
        "    raise SystemExit(23)\n"
        "else:\n"
        "    print('acquired')\n"
        "    store.close()\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    return subprocess.run(
        [sys.executable, "-c", code, str(directory)],
        capture_output=True, text=True, timeout=15, env=environment,
        check=False)


def test_json_store_process_ownership_conflict_and_release(tmp_path):
    owner = JsonFileTurnStore(tmp_path)
    try:
        blocked = _ownership_probe(tmp_path)
        assert blocked.returncode == 23, (blocked.stdout, blocked.stderr)
        assert blocked.stdout.strip() == "blocked"
    finally:
        owner.close()
    acquired = _ownership_probe(tmp_path)
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)
    assert acquired.stdout.strip() == "acquired"


def test_json_store_last_reference_controls_process_ownership(tmp_path):
    first = JsonFileTurnStore(tmp_path)
    second = JsonFileTurnStore(tmp_path)
    try:
        first.close()
        blocked = _ownership_probe(tmp_path)
        assert blocked.returncode == 23, (blocked.stdout, blocked.stderr)
        assert second.list_turns() == []
    finally:
        second.close()
        first.close()
    acquired = _ownership_probe(tmp_path)
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_rejects_inherited_store_then_acquires_fresh_lock(
        tmp_path):
    owner = JsonFileTurnStore(tmp_path)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child_pid = None
    child_reaped = False
    try:
        owner.create(_new_record())
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - assertions run in child
            status = 1
            try:
                os.close(ready_read)
                os.close(release_write)
                try:
                    owner.list_turns()
                except TurnStoreOwnershipError:
                    pass
                else:
                    os._exit(2)
                os.write(ready_write, b"1")
                if os.read(release_read, 1) != b"1":
                    os._exit(3)
                child_store = JsonFileTurnStore(tmp_path)
                try:
                    if child_store.list_turns() != ["turn_000000000001"]:
                        os._exit(4)
                    status = 0
                finally:
                    child_store.close()
            finally:
                os._exit(status)
        os.close(ready_write)
        os.close(release_read)
        readable, _, _ = select.select([ready_read], [], [], 5)
        assert ready_read in readable, "forked child did not signal readiness"
        assert os.read(ready_read, 1) == b"1"
        owner.close()
        os.write(release_write, b"1")
        exited, _, _ = select.select([ready_read], [], [], 5)
        assert ready_read in exited, "forked child did not exit"
        assert os.read(ready_read, 1) == b""
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        assert waited_pid == child_pid
        child_reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        owner.close()
        if child_pid not in (None, 0) and not child_reaped:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
        for descriptor in (ready_read, ready_write,
                           release_read, release_write):
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_abrupt_exec_owner_exit_releases_store_lock(tmp_path):
    code = (
        "import os, sys\n"
        "from agentcore.realtime import JsonFileTurnStore\n"
        "store = JsonFileTurnStore(sys.argv[1])\n"
        "print('ready', flush=True)\n"
        "sys.stdin.buffer.read(1)\n"
        "os._exit(17)\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=environment)
    assert process.stdout is not None
    ready_lines = []
    reader = threading.Thread(
        target=lambda: ready_lines.append(process.stdout.readline()),
        daemon=True)
    reader.start()
    try:
        reader.join(timeout=5)
        assert not reader.is_alive(), "owner process did not signal readiness"
        assert ready_lines == ["ready\n"]
        blocked = _ownership_probe(tmp_path)
        assert blocked.returncode == 23, (blocked.stdout, blocked.stderr)
        assert process.stdin is not None
        process.stdin.write("x")
        process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=15) == 17
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        reader.join(timeout=5)
    acquired = _ownership_probe(tmp_path)
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)


def test_json_atomic_failure_preserves_prior_record_and_cleans_temp(
        tmp_path, monkeypatch):
    store = JsonFileTurnStore(tmp_path)
    try:
        record = store.create(_new_record())
        queued = _queued_candidate(record)

        def fail_replace(*_):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(store_module.os, "replace", fail_replace)
        with pytest.raises(OSError):
            store.compare_and_swap(record.turn_id, 0, queued)
        assert store.load(record.turn_id) == record
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        store.close()


def test_json_retry_after_replace_before_directory_fsync_is_idempotent(
        tmp_path, monkeypatch):
    store = JsonFileTurnStore(tmp_path)
    try:
        record = store.create(_new_record())
        queued = _queued_candidate(record)
        calls = 0

        def fail_directory_fsync(_):
            nonlocal calls
            calls += 1
            raise OSError("simulated directory fsync failure")

        monkeypatch.setattr(store_module, "_fsync_directory",
                            fail_directory_fsync)
        with pytest.raises(OSError):
            store.compare_and_swap(record.turn_id, 0, queued)
        assert store.load(record.turn_id) == queued
        assert store.compare_and_swap(record.turn_id, 0, queued) == queued
        assert calls == 1
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        store.close()


def test_json_corrupt_and_future_schema_records_fail_closed(tmp_path):
    store = JsonFileTurnStore(tmp_path)
    try:
        record = store.create(_new_record())
        path = tmp_path / f"{record.turn_id}.json"
        path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(TurnStoreError):
            store.load(record.turn_id)

        payload = record.to_dict()
        payload["schema_version"] = 2
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(TurnValidationError):
            store.load(record.turn_id)
    finally:
        store.close()


def test_json_invalid_utf8_is_categorized_as_corrupt_record(tmp_path):
    store = JsonFileTurnStore(tmp_path)
    try:
        record = store.create(_new_record())
        path = tmp_path / f"{record.turn_id}.json"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(TurnStoreError) as captured:
            store.load(record.turn_id)
        assert captured.value.details["error"] == "UnicodeDecodeError"
        assert captured.value.details["turn_id"] == record.turn_id
    finally:
        store.close()


def test_json_listing_rejects_malformed_record_filename(tmp_path):
    store = JsonFileTurnStore(tmp_path)
    try:
        (tmp_path / "turn_not-an-id.json").write_text("{}", encoding="utf-8")
        with pytest.raises(TurnValidationError):
            store.list_turns()
    finally:
        store.close()


def test_json_store_rejects_path_traversal_and_closed_use(tmp_path):
    store = JsonFileTurnStore(tmp_path)
    with pytest.raises(TurnValidationError):
        store.load("../turn_000000000001")
    store.close()
    store.close()
    with pytest.raises(TurnStoreError):
        store.list_turns()


def test_json_store_pins_relative_directory_across_cwd_changes(
        tmp_path, monkeypatch):
    original_cwd = tmp_path / "original"
    alternate_cwd = tmp_path / "alternate"
    original_cwd.mkdir()
    alternate_cwd.mkdir()
    (alternate_cwd / "turns").mkdir()
    monkeypatch.chdir(original_cwd)
    store = JsonFileTurnStore("turns")
    record = None
    try:
        monkeypatch.chdir(alternate_cwd)
        record = store.create(_new_record())
        queued = _queued_candidate(record)
        store.compare_and_swap(record.turn_id, 0, queued)

        assert store.load(record.turn_id) == queued
        assert (original_cwd / "turns" / f"{record.turn_id}.json").exists()
        assert not (alternate_cwd / "turns" / f"{record.turn_id}.json").exists()
        assert list((alternate_cwd / "turns").glob(".turn-*.tmp")) == []
    finally:
        store.close()
    assert record is not None
    reopened = JsonFileTurnStore(original_cwd / "turns")
    try:
        assert reopened.load(record.turn_id) == queued
    finally:
        reopened.close()


def _transition(controller, record, target, number, **kwargs):
    controller.transition(
        record.phase, target, expected_version=record.version,
        operation_id=_op(number), **kwargs)
    return controller._store.load(record.turn_id)  # internal test seam only


def _create_at(store, phase: TurnPhase, number: int,
               *, cancellation_reason=CancellationReason.USER_INTERRUPT,
               action_refs=()):
    clock = FixedClock(NOW)
    record = store.create(new_turn(
        f"conv_{number:012x}", 1, clock=clock, id_factory=_ids(number),
        action_refs=tuple(action_refs)))
    controller = TurnController(store, record.turn_id, clock=clock)
    operation = number * 10
    paths = {
        TurnPhase.CREATED: (),
        TurnPhase.QUEUED: (TurnPhase.QUEUED,),
        TurnPhase.GENERATING: (TurnPhase.QUEUED, TurnPhase.GENERATING),
        TurnPhase.EXECUTING_TOOL: (
            TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL),
        TurnPhase.WAITING_APPROVAL: (
            TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.WAITING_APPROVAL),
        TurnPhase.VERIFYING: (
            TurnPhase.QUEUED, TurnPhase.GENERATING,
            TurnPhase.EXECUTING_TOOL, TurnPhase.VERIFYING),
        TurnPhase.RECOVERING: (
            TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL,
            TurnPhase.VERIFYING, TurnPhase.RECOVERING),
    }
    if phase in paths:
        for target in paths[phase]:
            record = _transition(controller, record, target, operation)
            operation += 1
        return store.load(record.turn_id)
    if phase is TurnPhase.CANCELLING:
        if cancellation_reason is CancellationReason.DEADLINE_EXCEEDED:
            terminal = TurnPhase.TIMED_OUT
        elif cancellation_reason is CancellationReason.INTERNAL_FAILURE:
            terminal = TurnPhase.FAILED
        else:
            terminal = TurnPhase.CANCELLED
        return _transition(
            controller, record, TurnPhase.CANCELLING, operation,
            cancellation_reason=cancellation_reason,
            requested_terminal=terminal)
    if phase is TurnPhase.FINALIZING or phase.is_terminal:
        record = _transition(controller, record, TurnPhase.QUEUED, operation)
        record = _transition(controller, record, TurnPhase.GENERATING,
                             operation + 1)
        record = _transition(
            controller, record, TurnPhase.FINALIZING, operation + 2,
            requested_terminal=TurnPhase.COMPLETED)
        if phase is TurnPhase.COMPLETED:
            record = _transition(controller, record, TurnPhase.COMPLETED,
                                 operation + 3)
        return record
    raise AssertionError(f"unsupported fixture phase {phase}")


def _terminal_at(store, terminal: TurnPhase, number: int):
    if terminal is TurnPhase.COMPLETED:
        return _create_at(store, terminal, number)
    reasons = {
        TurnPhase.CANCELLED: CancellationReason.USER_INTERRUPT,
        TurnPhase.TIMED_OUT: CancellationReason.DEADLINE_EXCEEDED,
        TurnPhase.FAILED: CancellationReason.INTERNAL_FAILURE,
    }
    reason = reasons[terminal]
    cancelling = _create_at(
        store, TurnPhase.CANCELLING, number, cancellation_reason=reason)
    controller = TurnController(
        store, cancelling.turn_id, clock=FixedClock(NOW))
    finalizing = _transition(
        controller, cancelling, TurnPhase.FINALIZING, number * 10 + 1,
        cancellation_reason=reason, requested_terminal=terminal)
    return _transition(
        controller, finalizing, terminal, number * 10 + 2)


def test_restart_reconciliation_terminates_every_nonterminal_phase():
    store = InMemoryTurnStore()
    active = [
        TurnPhase.CREATED,
        TurnPhase.QUEUED,
        TurnPhase.GENERATING,
        TurnPhase.EXECUTING_TOOL,
        TurnPhase.WAITING_APPROVAL,
        TurnPhase.VERIFYING,
        TurnPhase.RECOVERING,
    ]
    records = [_create_at(store, phase, number + 1,
                          action_refs=(f"act_{number + 1:012x}",))
               for number, phase in enumerate(active)]
    cancelled = _create_at(store, TurnPhase.CANCELLING, 20)
    timed_out = _create_at(
        store, TurnPhase.CANCELLING, 21,
        cancellation_reason=CancellationReason.DEADLINE_EXCEEDED)
    finalizing = _create_at(store, TurnPhase.FINALIZING, 22)
    completed = _create_at(store, TurnPhase.COMPLETED, 23)
    completed_before = completed.to_dict()

    first = reconcile_nonterminal_turns(store, clock=FixedClock(NOW))
    assert len(first) == len(store.list_turns())
    for original in records:
        result = store.load(original.turn_id)
        assert result.phase is TurnPhase.FAILED
        assert result.cancellation_reason is CancellationReason.INTERNAL_FAILURE
        assert result.action_refs == original.action_refs
    assert store.load(cancelled.turn_id).phase is TurnPhase.CANCELLED
    assert store.load(timed_out.turn_id).phase is TurnPhase.TIMED_OUT
    recovered_success = store.load(finalizing.turn_id)
    assert recovered_success.phase is TurnPhase.FAILED
    assert recovered_success.cancellation_reason is \
        CancellationReason.INTERNAL_FAILURE
    assert recovered_success.requested_terminal is TurnPhase.COMPLETED
    assert recovered_success.final_terminal is TurnPhase.FAILED
    assert store.load(completed.turn_id).to_dict() == completed_before

    versions = {turn_id: store.load(turn_id).version
                for turn_id in store.list_turns()}
    second = reconcile_nonterminal_turns(store, clock=FixedClock(NOW))
    assert [snapshot.turn_id for snapshot in second] == store.list_turns()
    assert {turn_id: store.load(turn_id).version
            for turn_id in store.list_turns()} == versions


@pytest.mark.parametrize("terminal", [
    TurnPhase.COMPLETED,
    TurnPhase.FAILED,
    TurnPhase.CANCELLED,
    TurnPhase.TIMED_OUT,
])
def test_restart_reconciliation_never_rewrites_terminal_records(terminal):
    store = InMemoryTurnStore()
    record = _terminal_at(store, terminal, 1)
    before = record.to_dict()

    snapshots = reconcile_nonterminal_turns(store, clock=FixedClock(NOW))

    assert len(snapshots) == 1
    assert store.load(record.turn_id).to_dict() == before
    assert snapshots[0] == record.snapshot()


@pytest.mark.parametrize(("reason", "terminal"), [
    (CancellationReason.USER_INTERRUPT, TurnPhase.CANCELLED),
    (CancellationReason.DEADLINE_EXCEEDED, TurnPhase.TIMED_OUT),
    (CancellationReason.INTERNAL_FAILURE, TurnPhase.FAILED),
])
def test_restart_reconciliation_preserves_fixed_non_success_outcome(
        reason, terminal):
    store = InMemoryTurnStore()
    cancelling = _create_at(
        store, TurnPhase.CANCELLING, 1, cancellation_reason=reason)
    controller = TurnController(
        store, cancelling.turn_id, clock=FixedClock(NOW))
    finalizing = _transition(
        controller, cancelling, TurnPhase.FINALIZING, 11,
        cancellation_reason=reason, requested_terminal=terminal)

    reconcile_nonterminal_turns(store, clock=FixedClock(NOW))

    result = store.load(finalizing.turn_id)
    assert result.phase is terminal
    assert result.cancellation_reason is reason
    assert result.requested_terminal is terminal
    assert result.final_terminal is terminal


def test_reconciliation_is_deterministic_after_interrupted_step():
    store = InMemoryTurnStore()
    cancelling = _create_at(
        store, TurnPhase.CANCELLING, 1,
        cancellation_reason=CancellationReason.DEADLINE_EXCEEDED)
    reconcile_nonterminal_turns(store, clock=FixedClock(NOW))
    result = store.load(cancelling.turn_id)
    assert result.phase is TurnPhase.TIMED_OUT
    operations = [entry.operation_id for entry in result.transitions]
    assert len(operations) == len(set(operations))
