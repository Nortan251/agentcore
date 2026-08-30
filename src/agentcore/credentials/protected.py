"""Protected refresh storage and a separate, volatile access-only vault view.

One store domain belongs to one stable account and one OAuth client. Only its
attached host connection service can manage refresh material. The existing
CredentialRuntime gets the independent access-only lease interface.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from agentcore.credentials._protected_file import MAX_FILE_BYTES, ProtectedFile
from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import (
    AccountIdentity, CredentialInfo, CredentialState, RuntimeOnly, SecretMaterial,
    require_reference, require_scopes, require_time,
)
from agentcore.credentials.protection import CredentialProtector, WindowsDpapiProtector
from agentcore.credentials.runtime import _interruption_type, _metadata_call
from agentcore.credentials.vault import MAX_CREDENTIAL_RECORDS, CredentialLease
from agentcore.util.clock import Clock, SystemClock


class ConnectionState(str, Enum):
    PENDING = "pending_authorization"
    CURRENT = "current"
    REFRESH_REQUIRED = "refresh_required"
    RECONNECT_REQUIRED = "reconnect_required"
    DISCONNECTED = "locally_disconnected"


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    """Host-facing metadata only; never a credential or a permission receipt."""

    state: ConnectionState
    credential: CredentialInfo | None
    can_refresh: bool = False


def strict_json(data: bytes) -> dict[str, Any]:
    """Bounded strict JSON; duplicate keys and non-finite values are invalid."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CredentialError(CredentialFailure.INVALID)
            result[key] = value
        return result

    def invalid(_):
        raise CredentialError(CredentialFailure.INVALID)

    if type(data) is not bytes or not 1 <= len(data) <= MAX_FILE_BYTES:
        raise CredentialError(CredentialFailure.INVALID)
    result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    if type(result) is not dict:
        raise CredentialError(CredentialFailure.INVALID)
    return result


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 40:
        raise CredentialError(CredentialFailure.INVALID)
    return require_time(datetime.fromisoformat(value))


def _info(value: Any) -> CredentialInfo:
    keys = {"reference", "connection_id", "account_id", "provider", "scopes", "generation", "state", "expires_at"}
    if type(value) is not dict or set(value) != keys:
        raise CredentialError(CredentialFailure.INVALID)
    require_reference(value["reference"])
    for key, prefix in (("connection_id", "con_"), ("account_id", "acc_")):
        if type(value[key]) is not str or not re.fullmatch(prefix + r"[0-9a-f]{32}", value[key]):
            raise CredentialError(CredentialFailure.INVALID)
    scopes = value["scopes"]
    if type(scopes) is not list or any(type(s) is not str for s in scopes) or len(set(scopes)) != len(scopes):
        raise CredentialError(CredentialFailure.INVALID)
    if type(value["generation"]) is not int or not 1 <= value["generation"] <= MAX_CREDENTIAL_RECORDS:
        raise CredentialError(CredentialFailure.INVALID)
    expiry = _time(value["expires_at"])
    if expiry is None:
        raise CredentialError(CredentialFailure.INVALID)
    return CredentialInfo(value["reference"], value["connection_id"], value["account_id"],
                          value["provider"], require_scopes(frozenset(scopes)), value["generation"],
                          CredentialState(value["state"]), expiry)


class _Snapshot(RuntimeOnly):
    def __init__(self) -> None:
        self.client_id: str | None = None
        self.identity: AccountIdentity | None = None
        self.records: dict[str, CredentialInfo] = {}
        self.current: str | None = None
        self.refresh: SecretMaterial | None = None
        self.refresh_expires: datetime | None = None
        self.refresh_usable = True
        self.nonce: str | None = None
        self.auth_time: int | None = None

    def encode(self) -> bytes:
        identity = self.identity
        data = {
            "schema": 1, "client_id": self.client_id,
            "identity": ({"provider": identity.provider, "issuer": identity.issuer, "subject": identity.subject}
                         if identity else None),
            "records": [info.to_dict() for info in self.records.values()],
            "current": self.current,
            "refresh": self.refresh._read() if self.refresh else None,
            "refresh_expires": self.refresh_expires.isoformat() if self.refresh_expires else None,
            "refresh_usable": self.refresh_usable,
            "nonce": self.nonce, "auth_time": self.auth_time,
        }
        encoded = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > MAX_FILE_BYTES - 65_536:
            raise CredentialError(CredentialFailure.CAPACITY)
        return encoded

    @classmethod
    def decode(cls, encoded: bytes) -> _Snapshot:
        data = strict_json(encoded)
        if set(data) != {"schema", "client_id", "identity", "records", "current", "refresh", "refresh_expires", "refresh_usable", "nonce", "auth_time"}:
            raise CredentialError(CredentialFailure.INVALID)
        if type(data["schema"]) is not int or data["schema"] != 1 or type(data["refresh_usable"]) is not bool:
            raise CredentialError(CredentialFailure.INVALID)
        result = cls()
        client = data["client_id"]
        if client is not None and (type(client) is not str or not 1 <= len(client) <= 1024):
            raise CredentialError(CredentialFailure.INVALID)
        result.client_id = client
        identity = data["identity"]
        if identity is not None:
            if type(identity) is not dict or set(identity) != {"provider", "issuer", "subject"}:
                raise CredentialError(CredentialFailure.INVALID)
            result.identity = AccountIdentity(**identity)
        records = data["records"]
        if type(records) is not list or len(records) > MAX_CREDENTIAL_RECORDS:
            raise CredentialError(CredentialFailure.INVALID)
        account = None
        generations: dict[str, int] = {}
        for raw in records:
            info = _info(raw)
            if result.identity is None or info.provider != result.identity.provider or info.reference in result.records:
                raise CredentialError(CredentialFailure.INVALID)
            if account is not None and info.account_id != account:
                raise CredentialError(CredentialFailure.INVALID)
            account = info.account_id
            if info.generation != generations.get(info.connection_id, 0) + 1:
                raise CredentialError(CredentialFailure.INVALID)
            generations[info.connection_id] = info.generation
            result.records[info.reference] = info
        result.current = data["current"]
        if records:
            if client is None or type(result.current) is not str or result.current != next(reversed(result.records)):
                raise CredentialError(CredentialFailure.INVALID)
            current = result.records[result.current]
            if current.state is CredentialState.REPLACED or any(
                info.state is CredentialState.ACTIVE for ref, info in result.records.items() if ref != result.current
            ):
                raise CredentialError(CredentialFailure.INVALID)
            if (data["refresh"] is not None) != (current.state is CredentialState.ACTIVE):
                raise CredentialError(CredentialFailure.INVALID)
        elif any(data[key] is not None for key in ("current", "identity", "refresh", "refresh_expires")):
            raise CredentialError(CredentialFailure.INVALID)
        result.refresh = SecretMaterial(data["refresh"]) if data["refresh"] is not None else None
        result.refresh_expires = _time(data["refresh_expires"])
        if result.refresh is None and result.refresh_expires is not None:
            raise CredentialError(CredentialFailure.INVALID)
        result.refresh_usable = data["refresh_usable"]
        nonce, auth_time = data["nonce"], data["auth_time"]
        if nonce is not None and (type(nonce) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", nonce)):
            raise CredentialError(CredentialFailure.INVALID)
        if auth_time is not None and (type(auth_time) is not int or not 0 <= auth_time <= 253402300799):
            raise CredentialError(CredentialFailure.INVALID)
        if bool(records) != (nonce is not None):
            raise CredentialError(CredentialFailure.INVALID)
        result.nonce, result.auth_time = nonce, auth_time
        return result

    def successor(self) -> _Snapshot:
        result = _Snapshot()
        result.client_id, result.identity = self.client_id, self.identity
        result.records, result.current = dict(self.records), self.current
        result.refresh, result.refresh_expires = self.refresh, self.refresh_expires
        result.refresh_usable = self.refresh_usable
        result.nonce, result.auth_time = self.nonce, self.auth_time
        return result


class _Operation(RuntimeOnly):
    def __init__(self, kind: str, expected: CredentialInfo | None, snapshot: _Snapshot) -> None:
        self.kind, self.expected, self.identity = kind, expected, snapshot.identity
        self.refresh = SecretMaterial(snapshot.refresh._read()) if kind == "refresh" and snapshot.refresh else None
        self.refresh_expires = snapshot.refresh_expires
        self.nonce, self.auth_time = snapshot.nonce, snapshot.auth_time

    def discard(self) -> None:
        if self.refresh is not None:
            self.refresh._discard()
            self.refresh = None


class _AccessClock(RuntimeOnly):
    def __init__(self, vault: ProtectedCredentialVault, expires: datetime, deadline: float) -> None:
        self.vault, self.expires, self.deadline = vault, expires, deadline

    def now(self) -> datetime:
        now, mono = self.vault._time()
        return max(now, self.expires) if mono >= self.deadline else now


class ProtectedCredentialVault(RuntimeOnly):
    """A single-account protected backend; refresh secrets never enter leases.

    Default protection is real current-user Windows DPAPI. Other OSes fail
    closed unless a test explicitly injects a protector with test_only=True.
    The service's operation reservation spans network I/O, never this mutex.
    """

    def __init__(self, directory: str | Path, *, clock: Clock | None = None,
                 protector: CredentialProtector | None = None, test_only: bool = False) -> None:
        self._owner_pid = os.getpid()
        self._closed = False
        self._lock = threading.RLock()
        self._clock = clock or SystemClock()
        if type(test_only) is not bool or (protector is not None and not test_only):
            raise CredentialError(CredentialFailure.UNSUPPORTED)
        self._protector = protector if protector is not None else WindowsDpapiProtector()
        self._runtime_authority: object | None = None
        self._manager_authority: object | None = None
        self._operation: _Operation | None = None
        self._leased = False
        self._writing = False
        self._access: SecretMaterial | None = None
        self._access_deadline = 0.0
        self._file = _metadata_call(lambda: ProtectedFile(Path(directory)))
        self._purpose = hashlib.sha256(("AgentCore protected connection v1\0" +
                                      os.path.normcase(str(self._file.directory))).encode()).digest()
        failed = False
        interruption = None
        try:
            data = self._file.read_current()
            if data is None and self._file.had_owner:
                raise CredentialError(CredentialFailure.CORRUPT)
            self._data = _Snapshot() if data is None else _Snapshot.decode(
                self._protector.unprotect(data, purpose=self._purpose))
            if data is None:
                # Persist an empty protected domain so a missing record on
                # reopen cannot silently reset account/tombstone continuity.
                self._commit(self._data)
        except BaseException as exc:
            failed = True
            interruption = _interruption_type(exc)
        if failed:
            _metadata_call(self._file.close)
            self._closed = True
            if interruption is not None:
                raise interruption()
            raise CredentialError(CredentialFailure.CORRUPT)

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._closed or self._file.poisoned:
            raise CredentialError(CredentialFailure.UNAVAILABLE)

    @contextmanager
    def _locked(self):
        # Also fence exceptional cleanup when an injected OS/clock adapter
        # forks while the creating thread owns this mutex.
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        self._lock.acquire()
        try:
            yield
        finally:
            if self._owner_pid != os.getpid():
                raise CredentialError(CredentialFailure.OWNERSHIP)
            self._lock.release()

    def _time(self) -> tuple[datetime, float]:
        self._owner()
        now, mono = _metadata_call(lambda: (require_time(self._clock.now()), self._clock.monotonic()))
        self._owner()
        if type(mono) not in (float, int) or not math.isfinite(mono):
            raise CredentialError(CredentialFailure.INVALID)
        return now, mono

    def _check_file(self) -> None:
        _metadata_call(self._file.read_current)

    def _current(self) -> CredentialInfo | None:
        return self._data.records.get(self._data.current) if self._data.current else None

    def status(self) -> ConnectionStatus:
        self._owner()
        with self._locked():
            self._owner()
            self._check_file()
            now, mono = self._time()
            info = self._current()
            can_refresh = self._data.refresh is not None and self._data.refresh_usable and (
                self._data.refresh_expires is None or now < self._data.refresh_expires)
            if self._operation is not None and self._operation.kind == "authorize":
                state = ConnectionState.PENDING
            elif info is None or info.state is CredentialState.REVOKED:
                state = ConnectionState.DISCONNECTED
            elif self._access is not None and info.expires_at is not None and now < info.expires_at and mono < self._access_deadline:
                state = ConnectionState.CURRENT
            else:
                state = ConnectionState.REFRESH_REQUIRED if can_refresh else ConnectionState.RECONNECT_REQUIRED
            return ConnectionStatus(state, info, can_refresh)

    def describe(self, reference: str) -> CredentialInfo:
        self._owner()
        with self._locked():
            self._owner()
            self._check_file()
            info = self._data.records.get(require_reference(reference))
            if info is None:
                raise CredentialError(CredentialFailure.NOT_FOUND)
            return info

    def _bind_runtime(self, authority: object) -> None:
        self._owner()
        with self._locked():
            self._owner()
            if authority is None or self._runtime_authority is not None:
                raise CredentialError(CredentialFailure.AUTHORITY)
            self._runtime_authority = authority

    def _bind_manager(self, authority: object, client_id: str) -> None:
        self._owner()
        with self._locked():
            self._owner()
            if authority is None or self._manager_authority is not None:
                raise CredentialError(CredentialFailure.AUTHORITY)
            if self._data.client_id is not None and client_id != self._data.client_id:
                raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
            self._manager_authority = authority
            self._client_id = client_id

    def _manager(self, authority: object) -> None:
        self._owner()
        if authority is None or authority is not self._manager_authority:
            raise CredentialError(CredentialFailure.AUTHORITY)

    def _begin(self, kind: str, reference: str | None, *, authority: object) -> _Operation:
        self._manager(authority)
        with self._locked():
            self._manager(authority)
            self._check_file()
            current = self._current()
            if self._leased or self._writing:
                raise CredentialError(CredentialFailure.BUSY)
            if len(self._data.records) >= MAX_CREDENTIAL_RECORDS:
                raise CredentialError(CredentialFailure.CAPACITY)
            if kind == "refresh":
                if self._operation is not None:
                    raise CredentialError(CredentialFailure.BUSY)
                if current is None or current.reference != reference:
                    raise CredentialError(CredentialFailure.STALE)
                if current.state is not CredentialState.ACTIVE:
                    raise CredentialError(CredentialFailure.REVOKED)
                if not self.status().can_refresh:
                    raise CredentialError(CredentialFailure.RECONNECT)
            elif kind != "authorize":
                raise CredentialError(CredentialFailure.INVALID)
            # A new explicit authorization supersedes any older operation.
            # Its network response can still arrive, but cannot publish.
            operation = _Operation(kind, current, self._data)
            self._operation = operation
            return operation

    def _require_operation(self, operation: _Operation, authority: object) -> None:
        self._manager(authority)
        if self._operation is not operation or self._current() != operation.expected:
            raise CredentialError(CredentialFailure.STALE)

    def _check_operation(self, operation: _Operation, *, authority: object) -> None:
        self._manager(authority)
        with self._locked():
            self._require_operation(operation, authority)
            self._check_file()

    def _commit(self, candidate: _Snapshot) -> None:
        # Serialize and protect the complete replacement before changing the
        # file or live generation. No partial record ever contains plaintext.
        if self._writing:
            raise CredentialError(CredentialFailure.BUSY)
        self._writing = True
        failed = False
        interruption = None
        write_started = False
        previous = self._data
        try:
            previous_blob = self._file.read_current()
            blob = self._protector.protect(candidate.encode(), purpose=self._purpose)
            self._owner()
            if self._data is not previous:
                raise CredentialError(CredentialFailure.STALE)
            write_started = True
            self._file.replace(blob)
            if candidate.records.get(candidate.current) != previous.records.get(previous.current):
                # The new metadata must never coexist with the old access
                # secret, even if publication is interrupted after this commit.
                previous_access = self._access
                self._access = None
                if previous_access is not None:
                    previous_access._discard()
            self._data = candidate
        except BaseException as exc:
            failed = True
            interruption = _interruption_type(exc)
            if write_started and self._owner_pid == os.getpid():
                # A backend can finish replacement before raising (including
                # interruption during final cleanup). Never let _abort write
                # the previous snapshot over an already-rotated refresh token.
                try:
                    if self._file.read_current() != previous_blob:
                        self._file.poisoned = True
                except BaseException:
                    self._file.poisoned = True
        finally:
            if self._owner_pid != os.getpid():
                raise CredentialError(CredentialFailure.OWNERSHIP)
            self._writing = False
        if interruption is not None:
            raise interruption()
        if failed:
            raise CredentialError(CredentialFailure.COMMIT_FAILED)

    def _publish(self, operation: _Operation, *, identity: AccountIdentity, access: SecretMaterial,
                 refresh: SecretMaterial | None, scopes: frozenset[str], expires_at: datetime,
                 deadline: float, refresh_expires: datetime | None, nonce: str,
                 auth_time: int | None, authority: object) -> CredentialInfo:
        self._manager(authority)
        with self._locked():
            self._require_operation(operation, authority)
            self._check_file()
            now, mono = self._time()
            self._require_operation(operation, authority)
            if require_time(expires_at) <= now or not math.isfinite(deadline) or mono >= deadline:
                raise CredentialError(CredentialFailure.EXPIRED)
            if self._data.identity is not None and self._data.identity != identity:
                raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
            current = self._current()
            if operation.kind == "refresh" and (current is None or not scopes <= current.scopes):
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            require_scopes(scopes)
            owned_access = SecretMaterial(access._read())
            token = refresh or operation.refresh
            if token is None:
                raise CredentialError(CredentialFailure.RECONNECT)
            owned_refresh = SecretMaterial(token._read())
            if refresh_expires is not None and require_time(refresh_expires) <= now:
                raise CredentialError(CredentialFailure.RECONNECT)
            successor = self._data.successor()
            reference = "crd_" + secrets.token_hex(16)
            if reference in successor.records:
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            continuing = operation.kind == "refresh" and current is not None
            account_id = current.account_id if current else "acc_" + secrets.token_hex(16)
            connection_id = current.connection_id if continuing else "con_" + secrets.token_hex(16)
            if not continuing and any(record.connection_id == connection_id for record in successor.records.values()):
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            info = CredentialInfo(reference, connection_id,
                                  account_id, identity.provider, scopes,
                                  current.generation + 1 if continuing else 1, expires_at=expires_at)
            if current is not None and current.state is CredentialState.ACTIVE:
                successor.records[current.reference] = replace(current, state=CredentialState.REPLACED)
            successor.records[reference] = info
            successor.current, successor.identity, successor.client_id = reference, identity, self._client_id
            successor.refresh, successor.refresh_expires, successor.refresh_usable = owned_refresh, refresh_expires, True
            successor.nonce, successor.auth_time = nonce, auth_time
            old_refresh = self._data.refresh
            self._commit(successor)
            # Publication follows the durable commit under the same mutex.
            if old_refresh is not None:
                old_refresh._discard()
            # Local encryption/fsync can also consume the remaining lifetime.
            # A durable refresh token may survive, but expired access is never
            # published (nor paired with the previous generation's material).
            self._writing = True
            try:
                now, mono = self._time()
            finally:
                if self._owner_pid != os.getpid():
                    raise CredentialError(CredentialFailure.OWNERSHIP)
                self._writing = False
            if self._data is not successor or self._operation is not operation:
                owned_access._discard()
                raise CredentialError(CredentialFailure.STALE)
            if now >= expires_at or mono >= deadline:
                owned_access._discard()
                self._operation = None
                raise CredentialError(CredentialFailure.EXPIRED)
            self._access_deadline = deadline
            self._access = owned_access
            self._operation = None
            return info

    def _abort(self, operation: _Operation, *, authority: object, reconnect: bool = False) -> None:
        self._manager(authority)
        with self._locked():
            self._manager(authority)
            if self._writing:
                raise CredentialError(CredentialFailure.BUSY)
            if self._operation is operation:
                self._operation = None
                if reconnect and operation.kind == "refresh":
                    successor = self._data.successor()
                    successor.refresh_usable = False
                    self._commit(successor)
            operation.discard()

    def _disconnect(self, reference: str | None, *, authority: object) -> ConnectionStatus:
        self._manager(authority)
        with self._locked():
            self._manager(authority)
            self._check_file()
            current = self._current()
            if (current.reference if current else None) != reference:
                raise CredentialError(CredentialFailure.STALE)
            if self._leased or self._writing:
                raise CredentialError(CredentialFailure.BUSY)
            successor = self._data.successor()
            if current is not None:
                successor.records[current.reference] = replace(current, state=CredentialState.REVOKED)
            successor.refresh, successor.refresh_expires, successor.refresh_usable = None, None, False
            old_refresh = self._data.refresh
            self._commit(successor)
            self._operation = None
            if old_refresh is not None:
                old_refresh._discard()
            if self._access is not None:
                self._access._discard()
                self._access = None
            return self.status()

    @contextmanager
    def _lease(self, expected: CredentialInfo, required_scopes: frozenset[str], *, authority: object) -> Iterator[CredentialLease]:
        self._owner()
        if authority is None or authority is not self._runtime_authority:
            raise CredentialError(CredentialFailure.AUTHORITY)
        with self._locked():
            self._owner()
            if type(expected) is not CredentialInfo:
                raise CredentialError(CredentialFailure.INVALID)
            current = self.describe(expected.reference)
            now, mono = self._time()
            current.require_current(now)
            if current != expected or current != self._current():
                raise CredentialError(CredentialFailure.STALE)
            if not require_scopes(required_scopes) <= current.scopes:
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            if self._leased or self._writing or self._operation is not None:
                raise CredentialError(CredentialFailure.BUSY)
            if self._access is None or mono >= self._access_deadline:
                raise CredentialError(CredentialFailure.EXPIRED)
            assert current.expires_at is not None
            lease = CredentialLease(current, self._access, clock=_AccessClock(self, current.expires_at, self._access_deadline))
            self._leased = True
        try:
            yield lease
        finally:
            self._owner()
            lease._finish()
            with self._locked():
                self._owner()
                self._leased = False

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        with self._locked():
            if self._closed:
                return
            if self._leased or self._writing:
                raise CredentialError(CredentialFailure.BUSY)
            self._operation = None
            for secret in (self._access, self._data.refresh):
                if secret is not None:
                    secret._discard()
            self._access = None
            _metadata_call(self._file.close)
            self._closed = True

    def __enter__(self) -> ProtectedCredentialVault:
        self._owner()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
