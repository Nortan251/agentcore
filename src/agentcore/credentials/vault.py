"""Process-owned in-memory vault and the private execution lease interface.

All mutations linearize under a short mutex. A successful rotation, revocation,
or close cannot overlap a credential lease. Busy operations fail without a state
change; callers must not report revocation until it succeeds. No metadata mutex
is held during provider I/O. This backend is for development/tests, not durable
production credential storage.
"""

from __future__ import annotations

import base64
import json
import math
import os
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import ContextManager, Protocol
from urllib.parse import quote, quote_plus

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import (
    AccountIdentity,
    CredentialInfo,
    CredentialState,
    RuntimeOnly,
    SecretMaterial,
    require_reference,
    require_scopes,
    require_time,
)
from agentcore.util.clock import Clock, SystemClock


MAX_CREDENTIAL_RECORDS = 4096


class CredentialLease(RuntimeOnly):
    """Synchronous provider-only view, invalid outside its owner and lifetime.

    Providers may reveal the secret only to build the immediate transport
    request. They must not retain plaintext, log request headers/bodies, or
    transfer the lease to a worker. Trusted Python is not a sandbox boundary.
    """

    __slots__ = ("info", "_material", "_owner_pid", "_thread_id", "_active", "_clock")

    def __init__(
        self, info: CredentialInfo, material: SecretMaterial, *, clock: Clock,
    ) -> None:
        self.info = info
        self._material: SecretMaterial | None = material
        self._owner_pid = os.getpid()
        self._thread_id = threading.get_ident()
        self._active = True
        self._clock = clock

    def __repr__(self) -> str:
        return "<CredentialLease redacted>"

    def _require_owner(self) -> None:
        if self._owner_pid != os.getpid() or self._thread_id != threading.get_ident():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if not self._active:
            raise CredentialError(CredentialFailure.AUTHORITY)

    def _check_current(self) -> None:
        self._require_owner()
        now = require_time(self._clock.now())
        self._require_owner()
        self.info.require_current(now)

    def reveal(self) -> str:
        self._check_current()
        if self._material is None:
            raise CredentialError(CredentialFailure.AUTHORITY)
        return self._material._read()

    def _secret_variants(self) -> tuple[str, ...]:
        # Expiry during a read does not stop result scrubbing. This internal
        # access is for redaction only, never a fresh provider invocation.
        self._require_owner()
        if self._material is None:
            raise CredentialError(CredentialFailure.AUTHORITY)
        value = self._material._read()
        encoded = value.encode("utf-8")
        variants = {
            value,
            json.dumps(value, ensure_ascii=True)[1:-1],
            quote(value, safe=""),
            quote_plus(value, safe=""),
            base64.b64encode(encoded).decode("ascii"),
            base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("="),
        }
        # A provider may accidentally coerce opaque text to a JSON number.
        # Include canonical decimal casts, which lose signs/leading zeros,
        # exponent formatting, or precision. These are redaction-only values.
        try:
            variants.add(str(int(value)))
        except ValueError:
            pass
        try:
            numeric = float(value)
        except ValueError:
            pass
        else:
            if math.isfinite(numeric):
                variants.add(str(numeric))
                if numeric.is_integer():
                    variants.add(str(int(numeric)))
        return tuple(variants)

    def _finish(self) -> None:
        self._require_owner()
        self._active = False
        self._material = None


class CredentialVault(Protocol):
    """Trusted backend seam, not a model/tool credential-lookup API.

    ``describe`` returns inert metadata. Only the credential runtime beneath
    ToolExecutor uses ``_lease``. Backends must preserve exact identity, state,
    scope, process ownership, and non-overlap across the yielded call. No
    backend is constructed from config, history, or journal records.
    """

    def describe(self, reference: str) -> CredentialInfo: ...

    def _bind_runtime(self, authority: object) -> None: ...

    def _lease(
        self,
        expected: CredentialInfo,
        required_scopes: frozenset[str],
        *,
        authority: object,
    ) -> ContextManager[CredentialLease]: ...


class _Record(RuntimeOnly):
    __slots__ = ("identity", "info", "material", "leased")

    def __init__(
        self, identity: AccountIdentity, info: CredentialInfo, material: SecretMaterial,
    ) -> None:
        self.identity = identity
        self.info = info
        self.material: SecretMaterial | None = material
        self.leased = False


class InMemoryCredentialVault(RuntimeOnly):
    """A bounded, thread-safe development vault with explicit host lifecycle."""

    __slots__ = ("_owner_pid", "_clock", "_lock", "_records", "_accounts",
                 "_connections", "_closed", "_authority")

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._owner_pid = os.getpid()
        self._clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._records: dict[str, _Record] = {}
        self._accounts: dict[AccountIdentity, str] = {}
        self._connections: dict[AccountIdentity, str] = {}
        self._closed = False
        self._authority: object | None = None

    def _bind_runtime(self, authority: object) -> None:
        self._require_owner()
        with self._lock:
            self._require_owner()
            if authority is None or self._authority is not None:
                raise CredentialError(CredentialFailure.AUTHORITY)
            self._authority = authority

    def _require_owner(self) -> None:
        # Every inherited-lock path, including cleanup, checks PID first.
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._closed:
            raise CredentialError(CredentialFailure.UNAVAILABLE)

    def _now(self) -> datetime:
        self._require_owner()
        now = require_time(self._clock.now())
        self._require_owner()  # injected clock may fork/reenter
        return now

    def _get(self, reference: str) -> _Record:
        record = self._records.get(require_reference(reference))
        if record is None:
            raise CredentialError(CredentialFailure.NOT_FOUND)
        return record

    def _capacity(self) -> None:
        if len(self._records) >= MAX_CREDENTIAL_RECORDS:
            raise CredentialError(CredentialFailure.CAPACITY)

    def register(
        self,
        identity: AccountIdentity,
        material: SecretMaterial,
        *,
        scopes: frozenset[str],
        expires_at: datetime | None = None,
    ) -> CredentialInfo:
        self._require_owner()
        if type(identity) is not AccountIdentity or type(material) is not SecretMaterial:
            raise CredentialError(CredentialFailure.INVALID)
        require_scopes(scopes)
        now = self._now()
        if expires_at is not None and require_time(expires_at) <= now:
            raise CredentialError(CredentialFailure.EXPIRED)
        owned = SecretMaterial(material._read())
        reference = "crd_" + secrets.token_hex(16)
        connection_id = "con_" + secrets.token_hex(16)
        account_id = "acc_" + secrets.token_hex(16)
        self._require_owner()
        with self._lock:
            self._require_owner()
            self._capacity()
            if identity in self._connections:
                raise CredentialError(CredentialFailure.ALREADY_CONNECTED)
            if reference in self._records:
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            account_id = self._accounts.setdefault(identity, account_id)
            info = CredentialInfo(
                reference, connection_id, account_id, identity.provider, scopes,
                generation=1, expires_at=expires_at,
            )
            self._records[reference] = _Record(identity, info, owned)
            self._connections[identity] = reference
            return info

    def describe(self, reference: str) -> CredentialInfo:
        self._require_owner()
        with self._lock:
            self._require_owner()
            return self._get(reference).info

    def rotate(
        self,
        reference: str,
        identity: AccountIdentity,
        material: SecretMaterial,
        *,
        scopes: frozenset[str],
        expires_at: datetime | None = None,
    ) -> CredentialInfo:
        """CAS on the exact reference; identity stays fixed, scopes may shrink.

        No refresh/network call occurs here. Trusted host authentication must
        verify the refreshed token's issuer/subject before calling this method.
        New consent or a different identity requires a new explicit connection
        and tool binding.
        """
        self._require_owner()
        if type(identity) is not AccountIdentity or type(material) is not SecretMaterial:
            raise CredentialError(CredentialFailure.INVALID)
        require_scopes(scopes)
        now = self._now()
        if expires_at is not None and require_time(expires_at) <= now:
            raise CredentialError(CredentialFailure.EXPIRED)
        owned = SecretMaterial(material._read())
        replacement = "crd_" + secrets.token_hex(16)
        self._require_owner()
        with self._lock:
            self._require_owner()
            current = self._get(reference)
            # An expired current token may be refreshed, but a revoked or
            # replaced reference can never regain authority through rotation.
            if current.info.state is not CredentialState.ACTIVE:
                current.info.require_current(now)
            if current.identity != identity:
                raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
            if not scopes <= current.info.scopes:
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            if current.leased:
                raise CredentialError(CredentialFailure.BUSY)
            self._capacity()
            if replacement in self._records:
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            info = replace(
                current.info, reference=replacement, scopes=scopes,
                generation=current.info.generation + 1, expires_at=expires_at,
            )
            current.info = replace(current.info, state=CredentialState.REPLACED)
            assert current.material is not None
            current.material._discard()
            current.material = None
            self._records[replacement] = _Record(identity, info, owned)
            self._connections[identity] = replacement
            return info

    def revoke(self, reference: str) -> CredentialInfo:
        self._require_owner()
        with self._lock:
            self._require_owner()
            current = self._get(reference)
            if current.info.state is CredentialState.REVOKED:
                return current.info
            if current.info.state is CredentialState.REPLACED:
                raise CredentialError(CredentialFailure.STALE)
            if current.leased:
                raise CredentialError(CredentialFailure.BUSY)
            current.info = replace(current.info, state=CredentialState.REVOKED)
            assert current.material is not None
            current.material._discard()
            current.material = None
            del self._connections[current.identity]
            return current.info

    @contextmanager
    def _lease(
        self,
        expected: CredentialInfo,
        required_scopes: frozenset[str],
        *,
        authority: object,
    ) -> Iterator[CredentialLease]:
        self._require_owner()
        if self._authority is None or authority is not self._authority:
            raise CredentialError(CredentialFailure.AUTHORITY)
        if type(expected) is not CredentialInfo:
            raise CredentialError(CredentialFailure.INVALID)
        require_scopes(required_scopes)
        now = self._now()
        with self._lock:
            self._require_owner()
            current = self._get(expected.reference)
            current.info.require_current(now)
            if current.info != expected:
                raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
            if not required_scopes <= current.info.scopes:
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            if current.leased:
                raise CredentialError(CredentialFailure.BUSY)
            if current.material is None:
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            lease = CredentialLease(current.info, current.material, clock=self._clock)
            current.leased = True
        try:
            yield lease
        finally:
            # No inherited lock or parent cleanup after a provider-side fork.
            self._require_owner()
            lease._finish()
            with self._lock:
                self._require_owner()
                current.leased = False

    def close(self) -> None:
        self._require_owner()
        with self._lock:
            self._require_owner()
            if any(record.leased for record in self._records.values()):
                raise CredentialError(CredentialFailure.BUSY)
            for record in self._records.values():
                if record.material is not None:
                    record.material._discard()
                    record.material = None
            self._closed = True
