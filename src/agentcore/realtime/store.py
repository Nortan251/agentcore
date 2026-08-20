"""CAS turn persistence with crash-safe JSON writes and process ownership."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.errors import (
    TurnAlreadyExistsError,
    TurnNotFoundError,
    TurnStoreError,
    TurnStoreOwnershipError,
    TurnValidationError,
    TurnVersionConflictError,
)
from agentcore.realtime.turn import (
    TurnController,
    TurnCounters,
    TurnPhase,
    TurnRecord,
    TurnSnapshot,
)
from agentcore.util.clock import Clock, SystemClock


@runtime_checkable
class TurnStore(Protocol):
    """Storage seam for immutable schema-v1 turn records."""

    def create(self, record: TurnRecord) -> TurnRecord: ...

    def load(self, turn_id: str) -> TurnRecord: ...

    def compare_and_swap(self, turn_id: str, expected_version: int,
                         next_record: TurnRecord) -> TurnRecord: ...

    def list_turns(self) -> list[str]: ...

    @property
    def admission_coordination(self) -> _AdmissionCoordination: ...

    def close(self) -> None: ...

    def __enter__(self) -> TurnStore: ...

    def __exit__(self, *_: object) -> None: ...


class InMemoryTurnStore:
    """Thread-safe snapshot store for tests and ephemeral coordination."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._admission_coordination = _AdmissionCoordination()
        self._closed = False

    @property
    def admission_coordination(self) -> _AdmissionCoordination:
        return self._admission_coordination

    def _require_open(self) -> None:
        if self._closed:
            raise TurnStoreError("turn store is closed")

    def create(self, record: TurnRecord) -> TurnRecord:
        with self._lock:
            self._require_open()
            _validate_fresh_record(record)
            if record.turn_id in self._data:
                raise TurnAlreadyExistsError("turn already exists",
                                             turn_id=record.turn_id)
            self._data[record.turn_id] = record.to_dict()
            return TurnRecord.from_dict(self._data[record.turn_id])

    def load(self, turn_id: str) -> TurnRecord:
        with self._lock:
            self._require_open()
            turn_id = _require_turn_id(turn_id)
            payload = self._data.get(turn_id)
            if payload is None:
                raise TurnNotFoundError("turn does not exist",
                                        turn_id=turn_id)
            return TurnRecord.from_dict(payload)

    def compare_and_swap(self, turn_id: str, expected_version: int,
                         next_record: TurnRecord) -> TurnRecord:
        with self._lock:
            self._require_open()
            turn_id, expected_version = _validate_cas_request(
                turn_id, expected_version, next_record)
            payload = self._data.get(turn_id)
            if payload is None:
                raise TurnNotFoundError("turn does not exist",
                                        turn_id=turn_id)
            current = TurnRecord.from_dict(payload)
            if current.version != expected_version:
                if current.version == expected_version + 1 and \
                        current == next_record:
                    return TurnRecord.from_dict(payload)
                raise _version_conflict(current, expected_version)
            _validate_cas_candidate(current, next_record)
            self._data[current.turn_id] = next_record.to_dict()
            return TurnRecord.from_dict(self._data[current.turn_id])

    def list_turns(self) -> list[str]:
        with self._lock:
            self._require_open()
            return sorted(self._data)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def __enter__(self) -> InMemoryTurnStore:
        with self._lock:
            self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class _CoordinationOwnership:
    """Immutable process identity for one admission coordination domain."""

    owner_pid: int


class _AdmissionCoordination:
    """Process-local admission lanes shared by one backing store domain."""

    def __init__(self) -> None:
        self._ownership = _CoordinationOwnership(os.getpid())
        self.lock = threading.Lock()
        self.lanes: dict[str, object] = {}

    @property
    def owner_pid(self) -> int:
        return self._ownership.owner_pid


@dataclass
class _StoreDomain:
    key: str
    handle: BinaryIO
    mutex: threading.RLock
    owner_pid: int
    references: int = 1
    admission_coordination: _AdmissionCoordination = field(
        default_factory=_AdmissionCoordination)


_DOMAINS: dict[str, _StoreDomain] = {}
_DOMAINS_LOCK = threading.Lock()
_OWNER_FILENAME = ".agentcore-turn-store.lock"
_TURN_ID = re.compile(r"^turn_[0-9a-f]{12}$")


def _domain_key(directory: Path) -> str:
    return os.path.normcase(str(directory.resolve()))


def _after_fork_in_child() -> None:
    """Drop inherited lock state without unlocking the parent's handle.

    A POSIX child inherits both Python mutexes and the parent's locked file
    description.  Reusing either would let two processes believe they own the
    store.  Closing only the child's descriptor leaves the parent's lock held;
    a subsequently constructed child store must open a fresh descriptor and
    contend for ownership normally.
    """
    global _DOMAINS, _DOMAINS_LOCK

    inherited = tuple(_DOMAINS.values())
    _DOMAINS = {}
    _DOMAINS_LOCK = threading.Lock()
    for domain in inherited:
        try:
            domain.handle.close()
        except BaseException:
            pass


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX-only API
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _acquire_domain(directory: Path) -> _StoreDomain:
    key = _domain_key(directory)
    owner_pid = os.getpid()
    with _DOMAINS_LOCK:
        existing = _DOMAINS.get(key)
        if existing is not None:
            if existing.owner_pid == owner_pid:
                existing.references += 1
                return existing
            # Defensive fallback if a host forks without running registered
            # handlers.  Never touch the inherited mutex or unlock the shared
            # file description; close this process's descriptor and acquire a
            # new lock below.
            _DOMAINS.pop(key, None)
            try:
                existing.handle.close()
            except BaseException:
                pass

        path = directory / _OWNER_FILENAME
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            _lock_owner_handle(handle)
        except BaseException:
            handle.close()
            raise
        domain = _StoreDomain(key, handle, threading.RLock(), owner_pid)
        _DOMAINS[key] = domain
        return domain


def _release_domain(domain: _StoreDomain) -> None:
    if domain.owner_pid != os.getpid():
        return
    with _DOMAINS_LOCK:
        current = _DOMAINS.get(domain.key)
        if current is not domain:
            return
        domain.references -= 1
        if domain.references > 0:
            return
        _DOMAINS.pop(domain.key, None)
        try:
            _unlock_owner_handle(domain.handle)
        finally:
            domain.handle.close()


def _lock_owner_handle(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        raise TurnStoreOwnershipError(
            "turn store is owned by another process") from exc


def _unlock_owner_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class JsonFileTurnStore:
    """One strict JSON record per turn under a store-lifetime OS lock.

    The held advisory lock enforces the version-1 single-owning-process rule.
    Same-process instances share a mutex and lock handle. Atomic replacement
    protects a record from partial writes; it is not the ownership mechanism.
    """

    def __init__(self, directory: str | Path) -> None:
        self._owner_pid = os.getpid()
        self._closed = True
        self._directory = Path(directory).resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._domain = _acquire_domain(self._directory)
        self._closed = False

    @property
    def admission_coordination(self) -> _AdmissionCoordination:
        return self._domain.admission_coordination

    def _require_owner_process(self) -> None:
        if self._owner_pid != os.getpid() or \
                self._domain.owner_pid != os.getpid():
            raise TurnStoreOwnershipError(
                "turn store instance belongs to another process")

    def _require_open(self) -> None:
        if self._closed:
            raise TurnStoreError("turn store is closed")

    def _path(self, turn_id: str) -> Path:
        return self._directory / f"{_require_turn_id(turn_id)}.json"

    def create(self, record: TurnRecord) -> TurnRecord:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            _validate_fresh_record(record)
            path = self._path(record.turn_id)
            if path.exists():
                raise TurnAlreadyExistsError("turn already exists",
                                             turn_id=record.turn_id)
            self._write_unlocked(path, record)
            return self._read_unlocked(path, record.turn_id)

    def load(self, turn_id: str) -> TurnRecord:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            return self._read_unlocked(self._path(turn_id), str(turn_id))

    def compare_and_swap(self, turn_id: str, expected_version: int,
                         next_record: TurnRecord) -> TurnRecord:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            turn_id, expected_version = _validate_cas_request(
                turn_id, expected_version, next_record)
            path = self._path(turn_id)
            current = self._read_unlocked(path, str(turn_id))
            if current.version != expected_version:
                if current.version == expected_version + 1 and \
                        current == next_record:
                    return current
                raise _version_conflict(current, expected_version)
            _validate_cas_candidate(current, next_record)
            self._write_unlocked(path, next_record)
            return self._read_unlocked(path, str(turn_id))

    def list_turns(self) -> list[str]:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            turn_ids = [path.stem for path in self._directory.glob("turn_*.json")]
            for turn_id in turn_ids:
                _require_turn_id(turn_id)
            return sorted(turn_ids)

    def _read_unlocked(self, path: Path, turn_id: str) -> TurnRecord:
        if not path.exists():
            raise TurnNotFoundError("turn does not exist", turn_id=turn_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return TurnRecord.from_dict(payload)
        except TurnValidationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                TypeError) as exc:
            raise TurnStoreError("saved turn record is corrupt",
                                 turn_id=turn_id,
                                 error=type(exc).__name__) from exc

    def _write_unlocked(self, path: Path, record: TurnRecord) -> None:
        try:
            payload = json.dumps(record.to_dict(), indent=2,
                                 sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TurnValidationError("turn record is not JSON serializable") from exc
        descriptor, temporary = tempfile.mkstemp(
            dir=self._directory, prefix=".turn-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8",
                           newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(self._directory)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def close(self) -> None:
        # A forked child must never wait on an inherited mutex or unlock the
        # parent's file description.  Mark only its local object closed.
        if self._owner_pid != os.getpid():
            self._closed = True
            return
        with self._domain.mutex:
            if self._closed:
                return
            self._closed = True
            _release_domain(self._domain)

    def __enter__(self) -> JsonFileTurnStore:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except BaseException:
            pass


def _fsync_directory(directory: Path) -> None:
    """Flush POSIX directory metadata; Windows makes no such durability claim.

    Record contents are flushed before atomic replacement on every platform.
    POSIX additionally flushes the containing directory for stronger metadata
    durability.  The standard-library Windows path deliberately does not claim
    that the replacement directory entry survives sudden power loss; this does
    not weaken CAS or concurrent-writer exclusion.
    """
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_cas_candidate(current: TurnRecord,
                            next_record: TurnRecord) -> None:
    if next_record.turn_id != current.turn_id or \
            next_record.session_id != current.session_id or \
            next_record.generation != current.generation or \
            next_record.created_at != current.created_at or \
            next_record.schema_version != current.schema_version or \
            next_record.run_id != current.run_id or \
            next_record.task_id != current.task_id or \
            next_record.subtask_id != current.subtask_id or \
            next_record.deadline_at != current.deadline_at or \
            next_record.counters != current.counters or \
            next_record.action_refs != current.action_refs:
        raise TurnValidationError("CAS cannot change non-transition fields",
                                  turn_id=current.turn_id)
    if next_record.version != current.version + 1:
        raise TurnValidationError("CAS must advance exactly one version",
                                  turn_id=current.turn_id,
                                  current_version=current.version,
                                  next_version=next_record.version)
    if next_record.transitions[:-1] != current.transitions or \
            len(next_record.transitions) != len(current.transitions) + 1:
        raise TurnValidationError("CAS must append exactly one transition",
                                  turn_id=current.turn_id)


def _validate_expected_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TurnValidationError("expected_version must be non-negative")


def _require_turn_id(value: object) -> str:
    if not isinstance(value, str) or not _TURN_ID.fullmatch(value):
        raise TurnValidationError("turn id is invalid")
    return value


def _validate_fresh_record(record: TurnRecord) -> None:
    if not isinstance(record, TurnRecord):
        raise TurnValidationError("turn record is invalid")
    record.validate()
    if record.phase is not TurnPhase.CREATED or record.version != 0 or \
            record.transitions or record.counters != TurnCounters():
        raise TurnValidationError(
            "turn store create requires a fresh CREATED record",
            turn_id=record.turn_id,
        )


def _validate_cas_request(
    turn_id: object,
    expected_version: int,
    next_record: TurnRecord,
) -> tuple[str, int]:
    validated_turn_id = _require_turn_id(turn_id)
    _validate_expected_version(expected_version)
    if not isinstance(next_record, TurnRecord):
        raise TurnValidationError("turn record is invalid")
    next_record.validate()
    if next_record.turn_id != validated_turn_id:
        raise TurnValidationError(
            "CAS record does not match requested turn",
            turn_id=validated_turn_id,
        )
    if next_record.version != expected_version + 1:
        raise TurnValidationError(
            "CAS candidate must be the canonical next version",
            turn_id=validated_turn_id,
            expected_version=expected_version,
            next_version=next_record.version,
        )
    return validated_turn_id, expected_version


def _version_conflict(current: TurnRecord,
                      expected_version: int) -> TurnVersionConflictError:
    return TurnVersionConflictError(
        "turn record version conflict",
        turn_id=current.turn_id,
        expected_version=expected_version,
        actual_version=current.version,
    )


def reconcile_nonterminal_turns(
    store: TurnStore,
    *,
    clock: Clock | None = None,
) -> tuple[TurnSnapshot, ...]:
    """Terminate interrupted turns without resuming stage execution.

    Active phases become ``FAILED`` through normal cancellation/finalizing
    transitions with ``INTERNAL_FAILURE`` evidence. A record already in
    ``CANCELLING`` keeps its primary reason. A record finalizing a cancellation
    or failure completes that fixed outcome. A record finalizing success fails
    safely because the crash prevents authoritative success acknowledgement;
    its requested ``COMPLETED`` outcome remains durable evidence. Terminal
    records are unchanged. Action references and prior evidence survive.
    """
    resolved_clock = clock or SystemClock()
    results: list[TurnSnapshot] = []
    for turn_id in store.list_turns():
        controller = TurnController(store, turn_id, clock=resolved_clock)
        while True:
            record = store.load(turn_id)
            if record.phase.is_terminal:
                results.append(record.snapshot())
                break
            if record.phase is TurnPhase.FINALIZING:
                target = record.requested_terminal
                if target is None:
                    raise TurnStoreError("finalizing turn lacks terminal outcome",
                                         turn_id=turn_id)
                reason = None
                if target is TurnPhase.COMPLETED:
                    target = TurnPhase.FAILED
                    reason = CancellationReason.INTERNAL_FAILURE
                controller.transition(
                    TurnPhase.FINALIZING,
                    target,
                    expected_version=record.version,
                    operation_id=_reconciliation_operation_id(
                        turn_id, record.version, target),
                    note="restart reconciliation",
                    cancellation_reason=reason,
                )
                continue
            if record.phase is TurnPhase.CANCELLING:
                reason = record.cancellation_reason
                if reason is None:
                    raise TurnStoreError("cancelling turn lacks a reason",
                                         turn_id=turn_id)
                controller.transition(
                    TurnPhase.CANCELLING,
                    TurnPhase.FINALIZING,
                    expected_version=record.version,
                    operation_id=_reconciliation_operation_id(
                        turn_id, record.version, TurnPhase.FINALIZING),
                    note="restart reconciliation",
                    requested_terminal=record.requested_terminal,
                    cancellation_reason=reason,
                )
                continue
            controller.transition(
                record.phase,
                TurnPhase.CANCELLING,
                expected_version=record.version,
                operation_id=_reconciliation_operation_id(
                    turn_id, record.version, TurnPhase.CANCELLING),
                note="restart reconciliation",
                requested_terminal=TurnPhase.FAILED,
                cancellation_reason=CancellationReason.INTERNAL_FAILURE,
            )
    return tuple(results)


def _reconciliation_operation_id(turn_id: str, version: int,
                                 target: TurnPhase) -> str:
    material = f"restart:{turn_id}:{version}:{target.value}".encode("utf-8")
    return "op_" + hashlib.sha256(material).hexdigest()[:12]
