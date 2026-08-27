"""Durable CAS store for independent ActionJournal schema-v1 records.

The JSON backend is deliberately single-host and single-owning-process.  Its
general mutex protects only bounded reads, validation, CAS, and writes; every
method releases that mutex before returning, so future effect I/O never needs
to run under the store lock.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ContextManager, Iterator, Protocol, runtime_checkable

from agentcore.action_journal.errors import (
    ActionAlreadyExistsError,
    ActionDeduplicationConflictError,
    ActionNotFoundError,
    ActionStoreError,
    ActionStoreOwnershipError,
    ActionValidationError,
    ActionVersionConflictError,
)
from agentcore.action_journal.models import (
    ActionRecord,
    ActionState,
    deduplication_compatible,
    require_action_id,
    validate_action_successor,
)
from agentcore.util.clock import Clock, SystemClock


# Defense in depth beyond the per-field/history bounds in ``models``.
MAX_ACTION_RECORD_BYTES = 1_048_576

_ACTION_ID = re.compile(r"^act_[0-9a-f]{12}$")
_OWNER_FILENAME = ".agentcore-action-store.lock"


class _DuplicateJsonKey(ValueError):
    """Internal categorical signal for a non-canonical JSON object."""


@runtime_checkable
class ActionStore(Protocol):
    """Atomic storage seam for immutable ActionJournal records."""

    def create(self, record: ActionRecord) -> ActionRecord: ...

    def load(self, action_id: str) -> ActionRecord: ...

    def compare_and_swap(self, action_id: str, expected_version: int,
                         next_record: ActionRecord) -> ActionRecord: ...

    def claim_attempt(
        self,
        action_id: str,
        expected_version: int,
        *,
        attempt_id: str,
        invocation_id: str,
        owner_id: str,
        owner_epoch: int,
        transition_id: str,
    ) -> ActionRecord: ...

    def list_nonterminal(self) -> list[ActionRecord]: ...

    def list_actions(self) -> list[str]: ...

    def execution_guard(self, action_id: str) -> ContextManager[None]: ...

    def close(self) -> None: ...

    def __enter__(self) -> ActionStore: ...

    def __exit__(self, *_: object) -> None: ...


class InMemoryActionStore:
    """Thread-safe serialized snapshot store for deterministic tests."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._data: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._action_guards_lock = threading.Lock()
        self._action_guards: dict[str, _ActionGuard] = {}
        self._clock = clock or SystemClock()
        self._owner_pid = os.getpid()
        self._closed = False

    def _require_owner_process(self) -> None:
        if self._owner_pid != os.getpid():
            raise ActionStoreOwnershipError(
                "action store instance belongs to another process")

    def _require_open(self) -> None:
        if self._closed:
            raise ActionStoreError("action store is closed")

    def _audit_unlocked(self) -> tuple[list[ActionRecord],
                                       dict[tuple[str, str], ActionRecord]]:
        records: list[ActionRecord] = []
        for action_id in sorted(self._data):
            validated_id = require_action_id(action_id)
            record = ActionRecord.from_dict(self._data[action_id])
            if record.action_id != validated_id:
                raise ActionStoreError(
                    "saved action record identity does not match store key",
                    action_id=validated_id)
            records.append(record)
        return records, _dedup_index(records)

    def create(self, record: ActionRecord) -> ActionRecord:
        self._require_owner_process()
        with self._lock:
            self._require_open()
            _validate_fresh_record(record)
            records, dedup = self._audit_unlocked()
            attached = _existing_create_owner(record, records, dedup)
            if attached is not None:
                return _snapshot(attached)
            _validate_candidate_links(records, record)
            self._data[record.action_id] = record.to_dict()
            return ActionRecord.from_dict(self._data[record.action_id])

    def load(self, action_id: str) -> ActionRecord:
        self._require_owner_process()
        with self._lock:
            self._require_open()
            action_id = require_action_id(action_id)
            records, _ = self._audit_unlocked()
            for record in records:
                if record.action_id == action_id:
                    return _snapshot(record)
            raise ActionNotFoundError("action does not exist",
                                      action_id=action_id)

    def compare_and_swap(self, action_id: str, expected_version: int,
                         next_record: ActionRecord) -> ActionRecord:
        self._require_owner_process()
        action_id, expected_version = _validate_cas_request(
            action_id, expected_version, next_record)
        with self._lock:
            self._require_open()
            records, _ = self._audit_unlocked()
            current = _find_record(records, action_id)
            if current.version != expected_version:
                if current.version == expected_version + 1 and \
                        current == next_record:
                    return _snapshot(current)
                raise _version_conflict(current, expected_version)
            validate_action_successor(current, next_record)
            _validate_cross_record_attempts(records, next_record)
            self._data[action_id] = next_record.to_dict()
            return ActionRecord.from_dict(self._data[action_id])

    def claim_attempt(
        self,
        action_id: str,
        expected_version: int,
        *,
        attempt_id: str,
        invocation_id: str,
        owner_id: str,
        owner_epoch: int,
        transition_id: str,
    ) -> ActionRecord:
        self._require_owner_process()
        action_id = require_action_id(action_id)
        _validate_expected_version(expected_version)
        with self._lock:
            self._require_open()
            records, _ = self._audit_unlocked()
            current = _find_record(records, action_id)
            if current.version != expected_version:
                if _matches_claim_retry(
                    current,
                    expected_version=expected_version,
                    attempt_id=attempt_id,
                    invocation_id=invocation_id,
                    owner_id=owner_id,
                    owner_epoch=owner_epoch,
                    transition_id=transition_id,
                ):
                    return _snapshot(current)
                raise _version_conflict(current, expected_version)
            candidate = current.claim_attempt(
                attempt_id=attempt_id,
                invocation_id=invocation_id,
                owner_id=owner_id,
                owner_epoch=owner_epoch,
                transition_id=transition_id,
                at=self._clock.now(),
            )
            validate_action_successor(current, candidate)
            _validate_cross_record_attempts(records, candidate)
            self._data[action_id] = candidate.to_dict()
            return ActionRecord.from_dict(self._data[action_id])

    def list_nonterminal(self) -> list[ActionRecord]:
        self._require_owner_process()
        with self._lock:
            self._require_open()
            records, _ = self._audit_unlocked()
            return [_snapshot(record) for record in records
                    if not record.is_terminal]

    def list_actions(self) -> list[str]:
        self._require_owner_process()
        with self._lock:
            self._require_open()
            records, _ = self._audit_unlocked()
            return [record.action_id for record in records]

    @contextmanager
    def execution_guard(self, action_id: str) -> Iterator[None]:
        """Serialize one logical action without holding the store mutex."""
        self._require_owner_process()
        action_id = require_action_id(action_id)
        with self._lock:
            self._require_open()
        guard = _retain_action_guard(
            self._action_guards_lock, self._action_guards, action_id)
        try:
            guard.lock.acquire()
            try:
                self._require_owner_process()
                with self._lock:
                    self._require_open()
                yield
            finally:
                guard.lock.release()
        finally:
            _release_action_guard(
                self._action_guards_lock, self._action_guards,
                action_id, guard)

    def close(self) -> None:
        # A child must not touch a mutex inherited from a running parent.
        if self._owner_pid != os.getpid():
            self._closed = True
            return
        with self._lock:
            self._closed = True

    def __enter__(self) -> InMemoryActionStore:
        self._require_owner_process()
        with self._lock:
            self._require_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class _ActionGuard:
    lock: threading.Lock
    references: int = 1


def _retain_action_guard(
    registry_lock: threading.Lock,
    registry: dict[str, _ActionGuard],
    action_id: str,
) -> _ActionGuard:
    with registry_lock:
        guard = registry.get(action_id)
        if guard is None:
            guard = _ActionGuard(lock=threading.Lock())
            registry[action_id] = guard
        else:
            guard.references += 1
        return guard


def _release_action_guard(
    registry_lock: threading.Lock,
    registry: dict[str, _ActionGuard],
    action_id: str,
    guard: _ActionGuard,
) -> None:
    with registry_lock:
        current = registry.get(action_id)
        if current is not guard:
            return
        guard.references -= 1
        if guard.references == 0:
            registry.pop(action_id, None)


@dataclass
class _StoreDomain:
    key: str
    handle: BinaryIO
    mutex: threading.RLock
    action_guards_lock: threading.Lock
    action_guards: dict[str, _ActionGuard]
    owner_pid: int
    references: int = 1


_DOMAINS: dict[str, _StoreDomain] = {}
_DOMAINS_LOCK = threading.Lock()


def _domain_key(directory: Path) -> str:
    return os.path.normcase(str(directory.resolve()))


def _after_fork_in_child() -> None:
    """Discard inherited mutexes/descriptors without releasing parent locks."""
    global _DOMAINS, _DOMAINS_LOCK

    inherited = tuple(_DOMAINS.values())
    _DOMAINS = {}
    _DOMAINS_LOCK = threading.Lock()
    for domain in inherited:
        try:
            domain.handle.close()
        except BaseException:
            pass


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX only
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
        domain = _StoreDomain(
            key=key,
            handle=handle,
            mutex=threading.RLock(),
            action_guards_lock=threading.Lock(),
            action_guards={},
            owner_pid=owner_pid,
        )
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
        raise ActionStoreOwnershipError(
            "action store is owned by another process") from exc


def _unlock_owner_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class JsonFileActionStore:
    """One strict JSON record per action under a store-lifetime OS lock.

    Each record is encoded and file-fsynced before atomic replacement.  POSIX
    additionally fsyncs directory metadata.  The standard-library Windows
    path intentionally makes no stronger sudden-power-loss claim for the
    directory entry; CAS and concurrent-writer exclusion remain deterministic.
    """

    def __init__(self, directory: str | Path, *,
                 clock: Clock | None = None) -> None:
        self._owner_pid = os.getpid()
        self._closed = True
        self._directory = Path(directory).resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self._domain = _acquire_domain(self._directory)
        self._closed = False

    def _require_owner_process(self) -> None:
        if self._owner_pid != os.getpid() or \
                self._domain.owner_pid != os.getpid():
            raise ActionStoreOwnershipError(
                "action store instance belongs to another process")

    def _require_open(self) -> None:
        if self._closed:
            raise ActionStoreError("action store is closed")

    def _path(self, action_id: str) -> Path:
        return self._directory / f"{require_action_id(action_id)}.json"

    def _audit_unlocked(self) -> tuple[list[ActionRecord],
                                       dict[tuple[str, str], ActionRecord]]:
        records: list[ActionRecord] = []
        for path in sorted(self._directory.glob("*.json")):
            action_id = require_action_id(path.stem)
            records.append(self._read_unlocked(path, action_id))
        return records, _dedup_index(records)

    def create(self, record: ActionRecord) -> ActionRecord:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            _validate_fresh_record(record)
            records, dedup = self._audit_unlocked()
            attached = _existing_create_owner(record, records, dedup)
            if attached is not None:
                return attached
            _validate_candidate_links(records, record)
            path = self._path(record.action_id)
            if path.exists():
                # An audited matching path would already have been returned.
                raise ActionAlreadyExistsError(
                    "action id already exists", action_id=record.action_id)
            self._write_unlocked(path, record)
            return self._read_unlocked(path, record.action_id)

    def load(self, action_id: str) -> ActionRecord:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            action_id = require_action_id(action_id)
            records, _ = self._audit_unlocked()
            return _find_record(records, action_id)

    def compare_and_swap(self, action_id: str, expected_version: int,
                         next_record: ActionRecord) -> ActionRecord:
        self._require_owner_process()
        action_id, expected_version = _validate_cas_request(
            action_id, expected_version, next_record)
        with self._domain.mutex:
            self._require_open()
            records, _ = self._audit_unlocked()
            current = _find_record(records, action_id)
            if current.version != expected_version:
                if current.version == expected_version + 1 and \
                        current == next_record:
                    return current
                raise _version_conflict(current, expected_version)
            validate_action_successor(current, next_record)
            _validate_cross_record_attempts(records, next_record)
            path = self._path(action_id)
            self._write_unlocked(path, next_record)
            return self._read_unlocked(path, action_id)

    def claim_attempt(
        self,
        action_id: str,
        expected_version: int,
        *,
        attempt_id: str,
        invocation_id: str,
        owner_id: str,
        owner_epoch: int,
        transition_id: str,
    ) -> ActionRecord:
        self._require_owner_process()
        action_id = require_action_id(action_id)
        _validate_expected_version(expected_version)
        with self._domain.mutex:
            self._require_open()
            records, _ = self._audit_unlocked()
            current = _find_record(records, action_id)
            if current.version != expected_version:
                if _matches_claim_retry(
                    current,
                    expected_version=expected_version,
                    attempt_id=attempt_id,
                    invocation_id=invocation_id,
                    owner_id=owner_id,
                    owner_epoch=owner_epoch,
                    transition_id=transition_id,
                ):
                    return current
                raise _version_conflict(current, expected_version)
            candidate = current.claim_attempt(
                attempt_id=attempt_id,
                invocation_id=invocation_id,
                owner_id=owner_id,
                owner_epoch=owner_epoch,
                transition_id=transition_id,
                at=self._clock.now(),
            )
            validate_action_successor(current, candidate)
            _validate_cross_record_attempts(records, candidate)
            path = self._path(action_id)
            self._write_unlocked(path, candidate)
            return self._read_unlocked(path, action_id)

    def list_nonterminal(self) -> list[ActionRecord]:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            records, _ = self._audit_unlocked()
            return [record for record in records if not record.is_terminal]

    def list_actions(self) -> list[str]:
        self._require_owner_process()
        with self._domain.mutex:
            self._require_open()
            records, _ = self._audit_unlocked()
            return [record.action_id for record in records]

    @contextmanager
    def execution_guard(self, action_id: str) -> Iterator[None]:
        """Serialize one action across stores sharing this JSON domain."""
        self._require_owner_process()
        action_id = require_action_id(action_id)
        with self._domain.mutex:
            self._require_open()
        guard = _retain_action_guard(
            self._domain.action_guards_lock,
            self._domain.action_guards,
            action_id,
        )
        try:
            guard.lock.acquire()
            try:
                self._require_owner_process()
                with self._domain.mutex:
                    self._require_open()
                yield
            finally:
                guard.lock.release()
        finally:
            _release_action_guard(
                self._domain.action_guards_lock,
                self._domain.action_guards,
                action_id,
                guard,
            )

    def _read_unlocked(self, path: Path, action_id: str) -> ActionRecord:
        if not path.exists():
            raise ActionNotFoundError("action does not exist",
                                      action_id=action_id)
        try:
            if path.stat().st_size > MAX_ACTION_RECORD_BYTES:
                raise ActionStoreError(
                    "saved action record exceeds the public record bound",
                    action_id=action_id,
                    maximum=MAX_ACTION_RECORD_BYTES)
            encoded = path.read_bytes()
            if len(encoded) > MAX_ACTION_RECORD_BYTES:
                raise ActionStoreError(
                    "saved action record exceeds the public record bound",
                    action_id=action_id,
                    maximum=MAX_ACTION_RECORD_BYTES)
            payload = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
            )
            record = ActionRecord.from_dict(payload)
            if record.action_id != action_id:
                raise ActionStoreError(
                    "saved action record identity does not match filename",
                    action_id=action_id)
            return record
        except (ActionValidationError, ActionStoreError):
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                _DuplicateJsonKey, TypeError) as exc:
            raise ActionStoreError(
                "saved action record is corrupt",
                action_id=action_id,
                error=type(exc).__name__,
            ) from exc

    def _write_unlocked(self, path: Path, record: ActionRecord) -> None:
        try:
            encoded = json.dumps(
                record.to_dict(),
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ActionValidationError(
                "action record is not JSON serializable") from exc
        if len(encoded) > MAX_ACTION_RECORD_BYTES:
            raise ActionValidationError(
                "action record exceeds serialized size bound",
                maximum=MAX_ACTION_RECORD_BYTES)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._directory, prefix=".action-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(self._directory)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            self._closed = True
            return
        with self._domain.mutex:
            if self._closed:
                return
            self._closed = True
            _release_domain(self._domain)

    def __enter__(self) -> JsonFileActionStore:
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
    """Flush replacement metadata on POSIX; make no false Windows claim."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot(record: ActionRecord) -> ActionRecord:
    return ActionRecord.from_dict(record.to_dict())


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("duplicate JSON object key")
        result[key] = value
    return result


def _validate_fresh_record(record: ActionRecord) -> None:
    if not isinstance(record, ActionRecord):
        raise ActionValidationError("action record is invalid")
    record.validate()
    if record.state is not ActionState.AUTHORIZED or record.version != 0 or \
            record.attempts or record.transitions or record.evidence_refs or \
            record.terminal_outcome is not None:
        raise ActionValidationError(
            "action store create requires a fresh AUTHORIZED record",
            action_id=record.action_id)


def _existing_create_owner(
    requested: ActionRecord,
    records: list[ActionRecord],
    dedup: dict[tuple[str, str], ActionRecord],
) -> ActionRecord | None:
    for existing in records:
        if existing.action_id == requested.action_id:
            if deduplication_compatible(existing, requested):
                return existing
            raise ActionAlreadyExistsError(
                "action id owns a different immutable intent",
                action_id=requested.action_id)
    identity = requested.deduplication_identity
    if identity is None:
        return None
    existing = dedup.get(identity)
    if existing is None:
        return None
    if not deduplication_compatible(existing, requested):
        raise ActionDeduplicationConflictError(
            "deduplication identity owns a different immutable intent",
            scope=identity[0])
    return existing


def _dedup_index(records: list[ActionRecord]) -> dict[
        tuple[str, str], ActionRecord]:
    index: dict[tuple[str, str], ActionRecord] = {}
    action_ids: set[str] = set()
    attempt_owners: dict[str, str] = {}
    idempotency_owners: dict[tuple[str, str, str], str] = {}
    for record in records:
        record.validate()
        if record.action_id in action_ids:
            raise ActionStoreError("duplicate action identity in store",
                                   action_id=record.action_id)
        action_ids.add(record.action_id)
        for attempt in record.attempts:
            owner = attempt_owners.get(attempt.attempt_id)
            if owner is not None and owner != record.action_id:
                raise ActionStoreError(
                    "action attempt identity has multiple action owners",
                    attempt_id=attempt.attempt_id,
                    owner_count=2)
            attempt_owners[attempt.attempt_id] = record.action_id
        if record.idempotency is not None:
            idempotency_identity = (
                record.idempotency.scope,
                record.idempotency.contract,
                record.idempotency.key,
            )
            owner = idempotency_owners.get(idempotency_identity)
            if owner is not None and owner != record.action_id:
                raise ActionStoreError(
                    "external idempotency identity has multiple action owners",
                    owner_count=2)
            idempotency_owners[idempotency_identity] = record.action_id
        identity = record.deduplication_identity
        if identity is None:
            continue
        owner = index.get(identity)
        if owner is not None and owner.action_id != record.action_id:
            raise ActionDeduplicationConflictError(
                "deduplication identity has multiple action owners",
                scope=identity[0],
                owner_count=2)
        index[identity] = record
    _validate_predecessor_graph(records)
    return index


def _validate_cross_record_attempts(
    records: list[ActionRecord],
    candidate: ActionRecord,
) -> None:
    candidate_ids = {attempt.attempt_id for attempt in candidate.attempts}
    for record in records:
        if record.action_id == candidate.action_id:
            continue
        if candidate_ids.intersection(
                attempt.attempt_id for attempt in record.attempts):
            raise ActionStoreError(
                "action attempt identity is already owned by another action",
                action_id=candidate.action_id)


def _validate_candidate_links(records: list[ActionRecord],
                              candidate: ActionRecord) -> None:
    if candidate.predecessor_action_id is not None and \
            candidate.predecessor_action_id not in {
                record.action_id for record in records
            }:
        raise ActionStoreError(
            "predecessor action does not exist",
            action_id=candidate.action_id)
    if candidate.idempotency is None:
        return
    identity = (
        candidate.idempotency.scope,
        candidate.idempotency.contract,
        candidate.idempotency.key,
    )
    for record in records:
        if record.idempotency is None:
            continue
        existing = (
            record.idempotency.scope,
            record.idempotency.contract,
            record.idempotency.key,
        )
        if existing == identity and record.action_id != candidate.action_id:
            raise ActionStoreError(
                "external idempotency identity is already owned",
                action_id=candidate.action_id)


def _validate_predecessor_graph(records: list[ActionRecord]) -> None:
    by_id = {record.action_id: record for record in records}
    for record in records:
        predecessor = record.predecessor_action_id
        if predecessor is not None and predecessor not in by_id:
            raise ActionStoreError(
                "predecessor action does not exist",
                action_id=record.action_id)
    for record in records:
        visited: set[str] = set()
        current: ActionRecord | None = record
        while current is not None:
            if current.action_id in visited:
                raise ActionStoreError(
                    "predecessor action graph contains a cycle",
                    action_id=record.action_id)
            visited.add(current.action_id)
            predecessor = current.predecessor_action_id
            current = by_id.get(predecessor) if predecessor is not None else None


def _find_record(records: list[ActionRecord], action_id: str) -> ActionRecord:
    for record in records:
        if record.action_id == action_id:
            return record
    raise ActionNotFoundError("action does not exist", action_id=action_id)


def _validate_expected_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActionValidationError(
            "expected_version must be a non-negative integer")


def _validate_cas_request(
    action_id: object,
    expected_version: int,
    next_record: ActionRecord,
) -> tuple[str, int]:
    validated_action_id = require_action_id(action_id)
    _validate_expected_version(expected_version)
    if not isinstance(next_record, ActionRecord):
        raise ActionValidationError("action record is invalid")
    next_record.validate()
    if next_record.action_id != validated_action_id:
        raise ActionValidationError(
            "CAS record does not match requested action",
            action_id=validated_action_id)
    if next_record.version != expected_version + 1:
        raise ActionValidationError(
            "CAS candidate must be the canonical next version",
            action_id=validated_action_id,
            expected_version=expected_version,
            next_version=next_record.version)
    if next_record.transitions[-1].to_state is ActionState.IN_FLIGHT:
        raise ActionValidationError(
            "IN_FLIGHT must use the atomic claim_attempt store primitive",
            action_id=validated_action_id)
    return validated_action_id, expected_version


def _version_conflict(current: ActionRecord,
                      expected_version: int) -> ActionVersionConflictError:
    return ActionVersionConflictError(
        "action record version conflict",
        action_id=current.action_id,
        expected_version=expected_version,
        actual_version=current.version)


def _matches_claim_retry(
    current: ActionRecord,
    *,
    expected_version: int,
    attempt_id: str,
    invocation_id: str,
    owner_id: str,
    owner_epoch: int,
    transition_id: str,
) -> bool:
    if current.version != expected_version + 1 or \
            current.state is not ActionState.IN_FLIGHT or \
            not current.attempts or not current.transitions:
        return False
    attempt = current.attempts[-1]
    transition = current.transitions[-1]
    return (
        attempt.attempt_id == attempt_id and
        attempt.invocation_id == invocation_id and
        attempt.owner_id == owner_id and
        attempt.owner_epoch == owner_epoch and
        transition.transition_id == transition_id and
        transition.attempt_id == attempt_id and
        transition.to_state is ActionState.IN_FLIGHT
    )
