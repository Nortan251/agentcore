"""Strict local durable store for independent approval records."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from agentcore.approvals.errors import (
    ApprovalAlreadyExistsError,
    ApprovalNotFoundError,
    ApprovalStoreError,
    ApprovalStoreOwnershipError,
    ApprovalValidationError,
    ApprovalVersionConflictError,
)
from agentcore.approvals.models import (
    ApprovalRequest,
    ApprovalState,
    require_approval_request_id,
    validate_approval_successor,
)


MAX_APPROVAL_RECORD_BYTES = 65_536
MAX_APPROVAL_RECORDS = 10_000
_OWNER_FILENAME = ".agentcore-approval-store.lock"


class _DuplicateJsonKey(ValueError):
    pass


@runtime_checkable
class ApprovalStore(Protocol):
    def create(self, request: ApprovalRequest) -> ApprovalRequest: ...

    def load(self, request_id: str) -> ApprovalRequest: ...

    def compare_and_swap(
        self,
        request_id: str,
        expected_version: int,
        candidate: ApprovalRequest,
    ) -> ApprovalRequest: ...

    def list_requests(self) -> list[str]: ...

    def close(self) -> None: ...


class InMemoryApprovalStore:
    """Thread-safe process-fenced approval store for deterministic tests."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = False

    def _require_owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise ApprovalStoreOwnershipError(
                "approval store instance belongs to another process")
        if self._closed:
            raise ApprovalStoreError("approval store is closed")

    def _audit_unlocked(self) -> list[ApprovalRequest]:
        records: list[ApprovalRequest] = []
        receipt_ids: set[str] = set()
        for request_id in sorted(self._records):
            request = ApprovalRequest.from_dict(self._records[request_id])
            if request.request_id != require_approval_request_id(request_id):
                raise ApprovalStoreError(
                    "approval request identity does not match store key")
            if request.receipt is not None:
                if request.receipt.receipt_id in receipt_ids:
                    raise ApprovalStoreError(
                        "approval receipt identity has multiple owners")
                receipt_ids.add(request.receipt.receipt_id)
            records.append(request)
        if len(records) > MAX_APPROVAL_RECORDS:
            raise ApprovalStoreError("approval record bound exceeded")
        return records

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        self._require_owner()
        _fresh_request(request)
        with self._lock:
            self._require_owner()
            records = self._audit_unlocked()
            for current in records:
                if current.request_id == request.request_id:
                    if current == request:
                        return _snapshot(current)
                    raise ApprovalAlreadyExistsError(
                        "approval request id already exists")
            if len(records) >= MAX_APPROVAL_RECORDS:
                raise ApprovalStoreError("approval record bound reached")
            self._records[request.request_id] = request.to_dict()
            return _snapshot(request)

    def load(self, request_id: str) -> ApprovalRequest:
        self._require_owner()
        request_id = require_approval_request_id(request_id)
        with self._lock:
            self._require_owner()
            for request in self._audit_unlocked():
                if request.request_id == request_id:
                    return _snapshot(request)
        raise ApprovalNotFoundError("approval request does not exist")

    def compare_and_swap(
        self,
        request_id: str,
        expected_version: int,
        candidate: ApprovalRequest,
    ) -> ApprovalRequest:
        request_id, expected_version = _cas_request(
            request_id, expected_version, candidate)
        self._require_owner()
        with self._lock:
            self._require_owner()
            records = self._audit_unlocked()
            current = _find(records, request_id)
            if current.version != expected_version:
                if current == candidate:
                    return _snapshot(current)
                raise _version_conflict(current, expected_version)
            validate_approval_successor(current, candidate)
            _unique_receipt(records, candidate)
            self._records[request_id] = candidate.to_dict()
            return _snapshot(candidate)

    def list_requests(self) -> list[str]:
        self._require_owner()
        with self._lock:
            self._require_owner()
            return [request.request_id for request in self._audit_unlocked()]

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            self._closed = True
            return
        with self._lock:
            self._closed = True


@dataclass
class _StoreDomain:
    key: str
    handle: BinaryIO
    mutex: threading.RLock
    owner_pid: int
    references: int = 1


_DOMAINS: dict[str, _StoreDomain] = {}
_DOMAINS_LOCK = threading.Lock()


def _after_fork_in_child() -> None:
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


def _domain_key(directory: Path) -> str:
    return os.path.normcase(str(directory.resolve()))


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
        handle = (directory / _OWNER_FILENAME).open("a+b")
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
            key=key, handle=handle, mutex=threading.RLock(),
            owner_pid=owner_pid)
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
        if domain.references:
            return
        _DOMAINS.pop(domain.key, None)
        try:
            _unlock_owner_handle(domain.handle)
        finally:
            domain.handle.close()


class JsonFileApprovalStore:
    """One strict JSON record per request under a store-lifetime OS lock."""

    def __init__(self, directory: str | Path) -> None:
        self._owner_pid = os.getpid()
        self._closed = True
        self._directory = Path(directory).resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self._domain = _acquire_domain(self._directory)
        self._closed = False

    def _require_owner(self) -> None:
        if self._owner_pid != os.getpid() or (
                self._domain.owner_pid != os.getpid()):
            raise ApprovalStoreOwnershipError(
                "approval store instance belongs to another process")
        if self._closed:
            raise ApprovalStoreError("approval store is closed")

    def _path(self, request_id: str) -> Path:
        return self._directory / f"{require_approval_request_id(request_id)}.json"

    def _audit_unlocked(self) -> list[ApprovalRequest]:
        paths = sorted(self._directory.glob("*.json"))
        if len(paths) > MAX_APPROVAL_RECORDS:
            raise ApprovalStoreError("approval record bound exceeded")
        records = [self._read_unlocked(path, require_approval_request_id(path.stem))
                   for path in paths]
        receipt_ids: set[str] = set()
        for request in records:
            if request.receipt is not None:
                if request.receipt.receipt_id in receipt_ids:
                    raise ApprovalStoreError(
                        "approval receipt identity has multiple owners")
                receipt_ids.add(request.receipt.receipt_id)
        return records

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        self._require_owner()
        _fresh_request(request)
        with self._domain.mutex:
            self._require_owner()
            records = self._audit_unlocked()
            for current in records:
                if current.request_id == request.request_id:
                    if current == request:
                        return current
                    raise ApprovalAlreadyExistsError(
                        "approval request id already exists")
            if len(records) >= MAX_APPROVAL_RECORDS:
                raise ApprovalStoreError("approval record bound reached")
            path = self._path(request.request_id)
            if path.exists():
                raise ApprovalAlreadyExistsError(
                    "approval request id already exists")
            self._write_unlocked(path, request)
            return self._read_unlocked(path, request.request_id)

    def load(self, request_id: str) -> ApprovalRequest:
        self._require_owner()
        request_id = require_approval_request_id(request_id)
        with self._domain.mutex:
            self._require_owner()
            return _find(self._audit_unlocked(), request_id)

    def compare_and_swap(
        self,
        request_id: str,
        expected_version: int,
        candidate: ApprovalRequest,
    ) -> ApprovalRequest:
        request_id, expected_version = _cas_request(
            request_id, expected_version, candidate)
        self._require_owner()
        with self._domain.mutex:
            self._require_owner()
            records = self._audit_unlocked()
            current = _find(records, request_id)
            if current.version != expected_version:
                if current == candidate:
                    return current
                raise _version_conflict(current, expected_version)
            validate_approval_successor(current, candidate)
            _unique_receipt(records, candidate)
            self._write_unlocked(self._path(request_id), candidate)
            return self._read_unlocked(self._path(request_id), request_id)

    def list_requests(self) -> list[str]:
        self._require_owner()
        with self._domain.mutex:
            self._require_owner()
            return [request.request_id for request in self._audit_unlocked()]

    def _read_unlocked(self, path: Path, request_id: str) -> ApprovalRequest:
        try:
            # Open once and bound the read on that handle.  A separate stat()
            # followed by Path.read_bytes() leaves a TOCTOU window in which a
            # replaced or growing file can bypass the size check.
            with path.open("rb") as handle:
                encoded = handle.read(MAX_APPROVAL_RECORD_BYTES + 1)
            if len(encoded) > MAX_APPROVAL_RECORD_BYTES:
                raise ApprovalStoreError("approval record exceeds size bound")
            payload = json.loads(
                encoded.decode("utf-8"), object_pairs_hook=_strict_json_object,
                parse_constant=lambda _: _raise_invalid_constant())
            if not isinstance(payload, dict):
                raise ApprovalStoreError("approval record root is invalid")
            request = ApprovalRequest.from_dict(payload)
            if request.request_id != request_id:
                raise ApprovalStoreError(
                    "approval request identity does not match filename")
            return request
        except (ApprovalStoreError, ApprovalValidationError):
            raise
        except FileNotFoundError as exc:
            raise ApprovalNotFoundError(
                "approval request does not exist") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                _DuplicateJsonKey, TypeError, ValueError) as exc:
            raise ApprovalStoreError(
                "approval record is corrupt", error=type(exc).__name__) from exc

    def _write_unlocked(self, path: Path, request: ApprovalRequest) -> None:
        try:
            encoded = json.dumps(
                request.to_dict(), sort_keys=True, ensure_ascii=True,
                separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ApprovalValidationError(
                "approval record is not canonical JSON") from exc
        if len(encoded) > MAX_APPROVAL_RECORD_BYTES:
            raise ApprovalValidationError("approval record exceeds size bound")
        descriptor, temporary = tempfile.mkstemp(
            dir=self._directory, prefix=".approval-", suffix=".tmp")
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

    def __enter__(self) -> JsonFileApprovalStore:
        self._require_owner()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            self.close()
        except BaseException:
            pass


def _fresh_request(request: ApprovalRequest) -> None:
    if not isinstance(request, ApprovalRequest):
        raise ApprovalValidationError("approval request is invalid")
    request.validate()
    if request.state is not ApprovalState.PENDING or request.version != 0:
        raise ApprovalValidationError(
            "approval store create requires a fresh pending request")


def _cas_request(request_id: str, expected_version: int,
                 candidate: ApprovalRequest) -> tuple[str, int]:
    request_id = require_approval_request_id(request_id)
    _expected_version(expected_version)
    if not isinstance(candidate, ApprovalRequest):
        raise ApprovalValidationError("approval CAS candidate is invalid")
    candidate.validate()
    if candidate.request_id != request_id or (
            candidate.version != expected_version + 1):
        raise ApprovalValidationError(
            "approval CAS candidate is not the exact next version")
    return request_id, expected_version


def _expected_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApprovalValidationError(
            "approval expected version is invalid")


def _find(records: list[ApprovalRequest],
          request_id: str) -> ApprovalRequest:
    for request in records:
        if request.request_id == request_id:
            return request
    raise ApprovalNotFoundError("approval request does not exist")


def _unique_receipt(records: list[ApprovalRequest],
                    candidate: ApprovalRequest) -> None:
    if candidate.receipt is None:
        return
    for request in records:
        if request.request_id == candidate.request_id or request.receipt is None:
            continue
        if request.receipt.receipt_id == candidate.receipt.receipt_id:
            raise ApprovalStoreError(
                "approval receipt identity is already owned")


def _version_conflict(current: ApprovalRequest,
                      expected_version: int) -> ApprovalVersionConflictError:
    return ApprovalVersionConflictError(
        "approval request version conflict",
        request_id=current.request_id,
        expected_version=expected_version,
        actual_version=current.version)


def _snapshot(request: ApprovalRequest) -> ApprovalRequest:
    return ApprovalRequest.from_dict(request.to_dict())


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("duplicate JSON key")
        result[key] = value
    return result


def _raise_invalid_constant() -> None:
    raise ValueError("non-finite JSON constant")


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
        raise ApprovalStoreOwnershipError(
            "approval store is owned by another process") from exc


def _unlock_owner_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MAX_APPROVAL_RECORD_BYTES",
    "MAX_APPROVAL_RECORDS",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "JsonFileApprovalStore",
]
