"""Segregated integrity-protected storage for action payloads and evidence.

``Protected`` means separated from public journal/model/observability domains
and authenticated with an installation-local HMAC key.  The JSON backends in
this module deliberately do **not** claim encryption at rest: protected
content is plaintext on the local filesystem and relies on host filesystem
access control for confidentiality.

Objects are immutable.  The only lifecycle implemented here is trusted
creation followed by detached, integrity-checked reads.  There is no delete or
garbage-collection API in Task 43.2d, so orphan objects produced by a later
journal/CAS failure remain inert and are retained for operator-managed storage
lifecycle work.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

from agentcore.util.domain_finalizers import DomainFinalizers

from agentcore.action_journal.errors import (
    InstallationKeyError,
    ProtectedObjectAlreadyExistsError,
    ProtectedObjectCorruptError,
    ProtectedObjectNotFoundError,
    ProtectedObjectTooLargeError,
    ProtectedReferenceError,
    ProtectedStorageError,
    ProtectedStoreOwnershipError,
)
from agentcore.action_journal.models import (
    INTENT_DIGEST_HEX_CHARS,
    MAX_COUNTER_VALUE,
    require_action_id,
)
from agentcore.util.ids import new_id


PROTECTED_STORAGE_SCHEMA_VERSION = 1
INSTALLATION_KEY_SCHEMA_VERSION = 1
PROTECTED_STORAGE_ENCRYPTED_AT_REST = False

INSTALLATION_KEY_BYTES = 32
MAX_INSTALLATION_KEY_FILE_BYTES = 1_024
MAX_PROTECTED_ACTION_PAYLOAD_BYTES = 1_048_576
MAX_PROTECTED_EVIDENCE_BYTES = 262_144
MAX_PROTECTED_JSON_DEPTH = 32
MAX_PROTECTED_JSON_ITEMS = 10_000

_PAYLOAD_REF = re.compile(r"^pap_[0-9a-f]{12}$")
_EVIDENCE_REF = re.compile(r"^pev_[0-9a-f]{12}$")
_ACTION_ATTEMPT_ID = re.compile(r"^aat_[0-9a-f]{12}$")
_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")

_FORBIDDEN_PROTECTED_KEY_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "oauth",
    "password",
    "privatekey",
    "secret",
    "token",
)
_FORBIDDEN_PROTECTED_KEYS = frozenset({"apikey", "auth", "bearer"})


class _DuplicateJsonKey(ValueError):
    """Internal content-free signal for duplicate JSON object keys."""


class ProtectedPayloadPurpose(str, Enum):
    ACTION_INTENT = "action_intent"

    def __str__(self) -> str:
        return self.value


class EvidencePurpose(str, Enum):
    ATTEMPT_EFFECT = "attempt_effect"
    RECONCILIATION_PROBE = "reconciliation_probe"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class InstallationKeyMaterial:
    """Opaque in-memory installation key material with a redacted repr."""

    key: bytes = field(repr=False)

    def validate(self) -> InstallationKeyMaterial:
        if not isinstance(self.key, bytes) or len(self.key) != INSTALLATION_KEY_BYTES:
            raise InstallationKeyError("installation integrity key is invalid")
        return self


@runtime_checkable
class InstallationKeyProvider(Protocol):
    def load(self) -> InstallationKeyMaterial: ...


class StaticInstallationKeyProvider:
    """Explicit key provider for tests or a host-owned secure key seam."""

    def __init__(self, key: bytes) -> None:
        self._material = InstallationKeyMaterial(bytes(key)).validate()

    def load(self) -> InstallationKeyMaterial:
        return self._material


class JsonFileInstallationKeyProvider:
    """Explicit local key lifecycle; missing keys are never auto-regenerated.

    Hosts call :meth:`create` once during installation bootstrap.  Protected
    stores call only :meth:`load`, so deleting a key after protected objects
    exist fails closed rather than silently creating a key that authenticates
    a different installation.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).resolve()

    def create(self) -> InstallationKeyMaterial:
        """Create the installation key once, or load the existing exact key."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            return self.load()
        material = InstallationKeyMaterial(
            secrets.token_bytes(INSTALLATION_KEY_BYTES)
        ).validate()
        encoded = _encode_json({
            "schema_version": INSTALLATION_KEY_SCHEMA_VERSION,
            "key": base64.b64encode(material.key).decode("ascii"),
        })
        if len(encoded) > MAX_INSTALLATION_KEY_FILE_BYTES:
            raise InstallationKeyError("installation integrity key is invalid")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor: int | None = None
        try:
            descriptor = os.open(self._path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(self._path.parent)
        except FileExistsError:
            return self.load()
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return self.load()

    def load(self) -> InstallationKeyMaterial:
        try:
            if not self._path.exists():
                raise InstallationKeyError(
                    "installation integrity key is missing")
            encoded = _read_bounded_file(
                self._path, MAX_INSTALLATION_KEY_FILE_BYTES)
            if encoded is None:
                raise InstallationKeyError(
                    "installation integrity key is corrupt")
            payload = json.loads(
                encoded.decode("utf-8"), object_pairs_hook=_strict_json_object)
            _require_exact_keys(payload, {"schema_version", "key"},
                                "installation key")
            if payload["schema_version"] != INSTALLATION_KEY_SCHEMA_VERSION or not (
                    isinstance(payload["key"], str)):
                raise InstallationKeyError(
                    "installation integrity key is corrupt")
            key = base64.b64decode(payload["key"], validate=True)
            return InstallationKeyMaterial(key).validate()
        except InstallationKeyError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                _DuplicateJsonKey, ValueError, TypeError) as exc:
            raise InstallationKeyError(
                "installation integrity key is corrupt") from exc


@dataclass(frozen=True)
class StoredProtectedActionPayload:
    reference: str
    action_id: str
    purpose: ProtectedPayloadPurpose
    intent_digest: str
    payload: Mapping[str, Any] = field(repr=False)

    def validate(self) -> StoredProtectedActionPayload:
        require_protected_payload_reference(self.reference)
        require_action_id(self.action_id)
        if not isinstance(self.purpose, ProtectedPayloadPurpose):
            raise ProtectedObjectCorruptError(
                "protected action payload is corrupt")
        _require_digest(self.intent_digest)
        _canonical_content(self.payload, MAX_PROTECTED_ACTION_PAYLOAD_BYTES)
        return self


@dataclass(frozen=True)
class StoredEvidence:
    reference: str
    action_id: str
    purpose: EvidencePurpose
    intent_digest: str
    evidence_digest: str
    evidence: Mapping[str, Any] = field(repr=False)
    attempt_id: str | None = None
    claim_epoch: int | None = None

    def validate(self) -> StoredEvidence:
        require_evidence_reference(self.reference)
        require_action_id(self.action_id)
        if not isinstance(self.purpose, EvidencePurpose):
            raise ProtectedObjectCorruptError("protected evidence is corrupt")
        _require_digest(self.intent_digest)
        _require_digest(self.evidence_digest)
        if self.purpose is EvidencePurpose.ATTEMPT_EFFECT:
            if not isinstance(self.attempt_id, str) or not (
                    _ACTION_ATTEMPT_ID.fullmatch(self.attempt_id)) or (
                    self.claim_epoch is not None):
                raise ProtectedObjectCorruptError("protected evidence is corrupt")
        elif self.attempt_id is not None or not _positive_int(self.claim_epoch):
            raise ProtectedObjectCorruptError("protected evidence is corrupt")
        _canonical_content(self.evidence, MAX_PROTECTED_EVIDENCE_BYTES)
        return self


@runtime_checkable
class ProtectedActionPayloadStore(Protocol):
    def create(
        self,
        action_id: str,
        payload: Mapping[str, Any],
        *,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
    ) -> StoredProtectedActionPayload: ...

    def load(
        self,
        reference: str,
        *,
        action_id: str,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
        expected_intent_digest: str | None = None,
    ) -> StoredProtectedActionPayload: ...

    def intent_digest(
        self,
        payload: Mapping[str, Any],
        *,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
    ) -> str: ...

    def close(self) -> None: ...


@runtime_checkable
class EvidenceStore(Protocol):
    def create_attempt_evidence(
        self,
        action_id: str,
        attempt_id: str,
        intent_digest: str,
        evidence: Mapping[str, Any],
    ) -> StoredEvidence: ...

    def create_reconciliation_evidence(
        self,
        action_id: str,
        claim_epoch: int,
        intent_digest: str,
        evidence: Mapping[str, Any],
    ) -> StoredEvidence: ...

    def load_attempt_evidence(
        self,
        reference: str,
        *,
        action_id: str,
        attempt_id: str,
        intent_digest: str,
    ) -> StoredEvidence: ...

    def load_reconciliation_evidence(
        self,
        reference: str,
        *,
        action_id: str,
        claim_epoch: int,
        intent_digest: str,
    ) -> StoredEvidence: ...

    def close(self) -> None: ...


class InMemoryProtectedActionPayloadStore:
    """Process-fenced immutable payload store for deterministic tests."""

    def __init__(
        self,
        key_provider: InstallationKeyProvider,
        *,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._key = _load_key(key_provider)
        self._id_factory = id_factory
        self._objects: dict[str, bytes] = {}
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = False

    def _require_open_owner(self) -> None:
        _require_owner(self._owner_pid)
        if self._closed:
            raise ProtectedStorageError("protected payload store is closed")

    def intent_digest(
        self,
        payload: Mapping[str, Any],
        *,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
    ) -> str:
        self._require_open_owner()
        return _payload_digest(self._key, purpose, payload)

    def create(
        self,
        action_id: str,
        payload: Mapping[str, Any],
        *,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
    ) -> StoredProtectedActionPayload:
        self._require_open_owner()
        action_id = require_action_id(action_id)
        purpose = _payload_purpose(purpose)
        reference = require_protected_payload_reference(self._id_factory("pap"))
        record = _payload_record(
            self._key, reference, action_id, purpose, payload)
        encoded = _bounded_record_bytes(
            record, MAX_PROTECTED_ACTION_PAYLOAD_BYTES, "protected payload")
        with self._lock:
            self._require_open_owner()
            if reference in self._objects:
                raise ProtectedObjectAlreadyExistsError(
                    "protected payload reference already exists")
            self._objects[reference] = encoded
            return _decode_payload(
                self._key, _strict_loads(encoded), reference=reference,
                action_id=action_id, purpose=purpose,
                expected_intent_digest=record["intent_digest"])

    def load(
        self,
        reference: str,
        *,
        action_id: str,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
        expected_intent_digest: str | None = None,
    ) -> StoredProtectedActionPayload:
        self._require_open_owner()
        reference = require_protected_payload_reference(reference)
        action_id = require_action_id(action_id)
        purpose = _payload_purpose(purpose)
        if expected_intent_digest is not None:
            _require_digest(expected_intent_digest)
        with self._lock:
            self._require_open_owner()
            try:
                encoded = self._objects[reference]
            except KeyError as exc:
                raise ProtectedObjectNotFoundError(
                    "protected payload is missing") from exc
            return _decode_payload(
                self._key, _strict_loads(encoded), reference=reference,
                action_id=action_id, purpose=purpose,
                expected_intent_digest=expected_intent_digest)

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            self._closed = True
            return
        with self._lock:
            self._closed = True


class InMemoryEvidenceStore:
    """Process-fenced immutable protected-evidence store for tests."""

    def __init__(
        self,
        key_provider: InstallationKeyProvider,
        *,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._key = _load_key(key_provider)
        self._id_factory = id_factory
        self._objects: dict[str, bytes] = {}
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = False

    def _require_open_owner(self) -> None:
        _require_owner(self._owner_pid)
        if self._closed:
            raise ProtectedStorageError("evidence store is closed")

    def create_attempt_evidence(
        self,
        action_id: str,
        attempt_id: str,
        intent_digest: str,
        evidence: Mapping[str, Any],
    ) -> StoredEvidence:
        return self._create(
            action_id, EvidencePurpose.ATTEMPT_EFFECT, intent_digest, evidence,
            attempt_id=attempt_id)

    def create_reconciliation_evidence(
        self,
        action_id: str,
        claim_epoch: int,
        intent_digest: str,
        evidence: Mapping[str, Any],
    ) -> StoredEvidence:
        return self._create(
            action_id, EvidencePurpose.RECONCILIATION_PROBE, intent_digest,
            evidence, claim_epoch=claim_epoch)

    def _create(
        self,
        action_id: str,
        purpose: EvidencePurpose,
        intent_digest: str,
        evidence: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
        claim_epoch: int | None = None,
    ) -> StoredEvidence:
        self._require_open_owner()
        reference = require_evidence_reference(self._id_factory("pev"))
        record = _evidence_record(
            self._key, reference, action_id, purpose, intent_digest, evidence,
            attempt_id=attempt_id, claim_epoch=claim_epoch)
        encoded = _bounded_record_bytes(
            record, MAX_PROTECTED_EVIDENCE_BYTES, "protected evidence")
        with self._lock:
            self._require_open_owner()
            if reference in self._objects:
                raise ProtectedObjectAlreadyExistsError(
                    "protected evidence reference already exists")
            self._objects[reference] = encoded
            return _decode_evidence(
                self._key, _strict_loads(encoded), reference=reference,
                action_id=action_id, purpose=purpose,
                intent_digest=intent_digest, attempt_id=attempt_id,
                claim_epoch=claim_epoch)

    def load_attempt_evidence(
        self,
        reference: str,
        *,
        action_id: str,
        attempt_id: str,
        intent_digest: str,
    ) -> StoredEvidence:
        return self._load(
            reference, action_id=action_id,
            purpose=EvidencePurpose.ATTEMPT_EFFECT,
            intent_digest=intent_digest, attempt_id=attempt_id)

    def load_reconciliation_evidence(
        self,
        reference: str,
        *,
        action_id: str,
        claim_epoch: int,
        intent_digest: str,
    ) -> StoredEvidence:
        return self._load(
            reference, action_id=action_id,
            purpose=EvidencePurpose.RECONCILIATION_PROBE,
            intent_digest=intent_digest, claim_epoch=claim_epoch)

    def _load(
        self,
        reference: str,
        *,
        action_id: str,
        purpose: EvidencePurpose,
        intent_digest: str,
        attempt_id: str | None = None,
        claim_epoch: int | None = None,
    ) -> StoredEvidence:
        self._require_open_owner()
        reference = require_evidence_reference(reference)
        with self._lock:
            self._require_open_owner()
            try:
                encoded = self._objects[reference]
            except KeyError as exc:
                raise ProtectedObjectNotFoundError(
                    "protected evidence is missing") from exc
            return _decode_evidence(
                self._key, _strict_loads(encoded), reference=reference,
                action_id=action_id, purpose=purpose,
                intent_digest=intent_digest, attempt_id=attempt_id,
                claim_epoch=claim_epoch)

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
_DOMAINS_LOCK = threading.RLock()


def _after_fork_in_child() -> None:
    global _DOMAINS, _DOMAINS_LOCK, _DOMAIN_FINALIZERS
    inherited = tuple(_DOMAINS.values())
    _DOMAINS = {}
    _DOMAINS_LOCK = threading.RLock()
    _DOMAIN_FINALIZERS = DomainFinalizers(_DOMAINS_LOCK, _release_domain)
    for domain in inherited:
        try:
            domain.handle.close()
        except BaseException:
            pass


def _before_fork() -> None:
    _DOMAIN_FINALIZERS.before_fork()


def _after_fork_in_parent() -> None:
    _DOMAIN_FINALIZERS.after_fork_parent()


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX only
    os.register_at_fork(
        before=_before_fork, after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child)


def _acquire_domain(directory: Path, namespace: str) -> _StoreDomain:
    key = f"{namespace}:{os.path.normcase(str(directory.resolve()))}"
    owner_pid = os.getpid()
    with _DOMAIN_FINALIZERS.guard():
        existing = _DOMAINS.get(key)
        if existing is not None:
            if existing.owner_pid == owner_pid:
                if existing.references == 0:
                    raise ProtectedStoreOwnershipError("domain ownership is closing")
                existing.references += 1
                return existing
            _DOMAINS.pop(key, None)
            try:
                existing.handle.close()
            except BaseException:
                pass
        handle = (directory / f".agentcore-{namespace}-store.lock").open("a+b")
        domain = None
        try:
            if owner_pid != os.getpid():
                raise ProtectedStoreOwnershipError("domain construction belongs to another process")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            _lock_owner_handle(handle)
            domain = _StoreDomain(
                key=key, handle=handle, mutex=threading.RLock(), owner_pid=owner_pid)
            if owner_pid != os.getpid():
                raise ProtectedStoreOwnershipError("domain construction belongs to another process")
            _DOMAINS[key] = domain
        except BaseException:
            if domain is not None and _DOMAINS.get(key) is domain:
                _DOMAINS.pop(key, None)
            handle.close()
            raise
        return domain


def _release_domain(domain: _StoreDomain) -> None:
    if domain.owner_pid != os.getpid():
        return
    with _DOMAIN_FINALIZERS.guard():
        current = _DOMAINS.get(domain.key)
        if current is not domain:
            return
        domain.references -= 1
        if domain.references:
            return
        # Keep last-close descriptors tracked through fork until fully closed.
        try:
            _unlock_owner_handle(domain.handle, owner_pid=domain.owner_pid)
        finally:
            try:
                if not domain.handle.closed:
                    domain.handle.close()
            finally:
                if _DOMAINS.get(domain.key) is domain:
                    _DOMAINS.pop(domain.key, None)


_DOMAIN_FINALIZERS = DomainFinalizers(_DOMAINS_LOCK, _release_domain)


class _JsonProtectedObjectStore:
    def __init__(
        self,
        directory: str | Path,
        key_provider: InstallationKeyProvider,
        *,
        namespace: str,
        reference_prefix: str,
        maximum_bytes: int,
        id_factory: Callable[[str], str],
    ) -> None:
        self._owner_pid = os.getpid()
        self._closed = True
        self._directory = Path(directory).resolve()
        self._directory.mkdir(parents=True, exist_ok=True)
        self.key = _load_key(key_provider)
        self.id_factory = id_factory
        self.reference_prefix = reference_prefix
        self.maximum_bytes = maximum_bytes
        if self._owner_pid != os.getpid():
            raise ProtectedStoreOwnershipError("store construction belongs to another process")
        domain = _acquire_domain(self._directory, namespace)
        try:
            if self._owner_pid != os.getpid() or domain.owner_pid != os.getpid():
                raise ProtectedStoreOwnershipError("store construction belongs to another process")
            self._domain = domain
            self._closed = False
        except BaseException:
            self._closed = True
            _release_domain(domain)
            raise

    def require_open_owner(self) -> None:
        _require_owner(self._owner_pid)
        if self._domain.owner_pid != os.getpid():
            raise ProtectedStoreOwnershipError(
                "protected store instance belongs to another process")
        if self._closed:
            raise ProtectedStorageError("protected store is closed")

    def new_reference(self) -> str:
        self.require_open_owner()
        return self.id_factory(self.reference_prefix)

    def write_new(self, reference: str, record: Mapping[str, Any]) -> None:
        self.require_open_owner()
        encoded = _bounded_record_bytes(
            record, self.maximum_bytes, "protected object")
        with self._domain.mutex:
            self.require_open_owner()
            path = self._directory / f"{reference}.json"
            if path.exists():
                raise ProtectedObjectAlreadyExistsError(
                    "protected object reference already exists")
            descriptor, temporary = tempfile.mkstemp(
                dir=self._directory, prefix=f".{self.reference_prefix}-",
                suffix=".tmp")
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

    def read(self, reference: str) -> Mapping[str, Any]:
        self.require_open_owner()
        with self._domain.mutex:
            self.require_open_owner()
            path = self._directory / f"{reference}.json"
            if not path.exists():
                raise ProtectedObjectNotFoundError(
                    "protected object is missing")
            try:
                encoded = _read_bounded_file(path, self.maximum_bytes)
                if encoded is None:
                    raise ProtectedObjectTooLargeError(
                        "protected object exceeds its size bound")
                return _strict_loads(encoded)
            except (ProtectedStorageError,):
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                    _DuplicateJsonKey, TypeError, ValueError) as exc:
                raise ProtectedObjectCorruptError(
                    "protected object is corrupt") from exc

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            self._closed = True
            return
        with self._domain.mutex:
            if self._closed:
                return
            self._closed = True
            _release_domain(self._domain)

    def __del__(self) -> None:  # pragma: no cover - defensive cleanup
        try:
            if self._owner_pid != os.getpid():
                self._closed = True
                return
            if self._closed:
                return
            self._closed = True
            # A reachable method/explicit close keeps self alive. A finalizer
            # owns only its lease; never wait on a shared domain's mutex here.
            _DOMAIN_FINALIZERS.release_later(self._domain)
        except BaseException:
            pass


class JsonFileProtectedActionPayloadStore:
    """Plaintext local payload files authenticated by installation-local HMAC."""

    def __init__(
        self,
        directory: str | Path,
        key_provider: InstallationKeyProvider,
        *,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._store = _JsonProtectedObjectStore(
            directory, key_provider, namespace="protected-payload",
            reference_prefix="pap",
            maximum_bytes=MAX_PROTECTED_ACTION_PAYLOAD_BYTES,
            id_factory=id_factory)

    def intent_digest(
        self,
        payload: Mapping[str, Any],
        *,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
    ) -> str:
        self._store.require_open_owner()
        return _payload_digest(self._store.key, purpose, payload)

    def create(
        self,
        action_id: str,
        payload: Mapping[str, Any],
        *,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
    ) -> StoredProtectedActionPayload:
        action_id = require_action_id(action_id)
        purpose = _payload_purpose(purpose)
        reference = require_protected_payload_reference(
            self._store.new_reference())
        record = _payload_record(
            self._store.key, reference, action_id, purpose, payload)
        self._store.write_new(reference, record)
        return self.load(
            reference, action_id=action_id, purpose=purpose,
            expected_intent_digest=record["intent_digest"])

    def load(
        self,
        reference: str,
        *,
        action_id: str,
        purpose: ProtectedPayloadPurpose = ProtectedPayloadPurpose.ACTION_INTENT,
        expected_intent_digest: str | None = None,
    ) -> StoredProtectedActionPayload:
        reference = require_protected_payload_reference(reference)
        return _decode_payload(
            self._store.key, self._store.read(reference), reference=reference,
            action_id=require_action_id(action_id),
            purpose=_payload_purpose(purpose),
            expected_intent_digest=expected_intent_digest)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> JsonFileProtectedActionPayloadStore:
        self._store.require_open_owner()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class JsonFileEvidenceStore:
    """Plaintext local evidence files authenticated by installation-local HMAC."""

    def __init__(
        self,
        directory: str | Path,
        key_provider: InstallationKeyProvider,
        *,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._store = _JsonProtectedObjectStore(
            directory, key_provider, namespace="protected-evidence",
            reference_prefix="pev", maximum_bytes=MAX_PROTECTED_EVIDENCE_BYTES,
            id_factory=id_factory)

    def create_attempt_evidence(
        self,
        action_id: str,
        attempt_id: str,
        intent_digest: str,
        evidence: Mapping[str, Any],
    ) -> StoredEvidence:
        return self._create(
            action_id, EvidencePurpose.ATTEMPT_EFFECT, intent_digest, evidence,
            attempt_id=attempt_id)

    def create_reconciliation_evidence(
        self,
        action_id: str,
        claim_epoch: int,
        intent_digest: str,
        evidence: Mapping[str, Any],
    ) -> StoredEvidence:
        return self._create(
            action_id, EvidencePurpose.RECONCILIATION_PROBE, intent_digest,
            evidence, claim_epoch=claim_epoch)

    def _create(
        self,
        action_id: str,
        purpose: EvidencePurpose,
        intent_digest: str,
        evidence: Mapping[str, Any],
        *,
        attempt_id: str | None = None,
        claim_epoch: int | None = None,
    ) -> StoredEvidence:
        reference = require_evidence_reference(self._store.new_reference())
        record = _evidence_record(
            self._store.key, reference, action_id, purpose, intent_digest,
            evidence, attempt_id=attempt_id, claim_epoch=claim_epoch)
        self._store.write_new(reference, record)
        return _decode_evidence(
            self._store.key, self._store.read(reference), reference=reference,
            action_id=action_id, purpose=purpose,
            intent_digest=intent_digest, attempt_id=attempt_id,
            claim_epoch=claim_epoch)

    def load_attempt_evidence(
        self,
        reference: str,
        *,
        action_id: str,
        attempt_id: str,
        intent_digest: str,
    ) -> StoredEvidence:
        return self._load(
            reference, action_id=action_id,
            purpose=EvidencePurpose.ATTEMPT_EFFECT,
            intent_digest=intent_digest, attempt_id=attempt_id)

    def load_reconciliation_evidence(
        self,
        reference: str,
        *,
        action_id: str,
        claim_epoch: int,
        intent_digest: str,
    ) -> StoredEvidence:
        return self._load(
            reference, action_id=action_id,
            purpose=EvidencePurpose.RECONCILIATION_PROBE,
            intent_digest=intent_digest, claim_epoch=claim_epoch)

    def _load(
        self,
        reference: str,
        *,
        action_id: str,
        purpose: EvidencePurpose,
        intent_digest: str,
        attempt_id: str | None = None,
        claim_epoch: int | None = None,
    ) -> StoredEvidence:
        reference = require_evidence_reference(reference)
        return _decode_evidence(
            self._store.key, self._store.read(reference), reference=reference,
            action_id=action_id, purpose=purpose,
            intent_digest=intent_digest, attempt_id=attempt_id,
            claim_epoch=claim_epoch)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> JsonFileEvidenceStore:
        self._store.require_open_owner()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def is_protected_payload_reference(value: object) -> bool:
    return isinstance(value, str) and bool(_PAYLOAD_REF.fullmatch(value))


def is_evidence_reference(value: object) -> bool:
    return isinstance(value, str) and bool(_EVIDENCE_REF.fullmatch(value))


def looks_like_protected_payload_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith("pap_")


def looks_like_evidence_reference(value: object) -> bool:
    return isinstance(value, str) and value.startswith("pev_")


def require_protected_payload_reference(value: object) -> str:
    if not is_protected_payload_reference(value):
        raise ProtectedReferenceError("protected payload reference is invalid")
    assert isinstance(value, str)
    return value


def require_evidence_reference(value: object) -> str:
    if not is_evidence_reference(value):
        raise ProtectedReferenceError("protected evidence reference is invalid")
    assert isinstance(value, str)
    return value


def _payload_record(
    key: bytes,
    reference: str,
    action_id: str,
    purpose: ProtectedPayloadPurpose,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    detached, _ = _canonical_content(
        payload, MAX_PROTECTED_ACTION_PAYLOAD_BYTES)
    digest = _payload_digest(key, purpose, detached)
    unsigned = {
        "schema_version": PROTECTED_STORAGE_SCHEMA_VERSION,
        "namespace": "protected_action_payload",
        "reference": reference,
        "action_id": require_action_id(action_id),
        "purpose": _payload_purpose(purpose).value,
        "intent_digest": digest,
        "payload": detached,
    }
    return {**unsigned, "binding_mac": _binding_mac(
        key, "agentcore.protected-action-payload.binding.v1", unsigned)}


def _decode_payload(
    key: bytes,
    record: Mapping[str, Any],
    *,
    reference: str,
    action_id: str,
    purpose: ProtectedPayloadPurpose,
    expected_intent_digest: str | None,
) -> StoredProtectedActionPayload:
    try:
        _require_exact_keys(record, {
            "schema_version", "namespace", "reference", "action_id",
            "purpose", "intent_digest", "payload", "binding_mac",
        }, "protected payload")
        if record["schema_version"] != PROTECTED_STORAGE_SCHEMA_VERSION or (
                record["namespace"] != "protected_action_payload"):
            raise ProtectedObjectCorruptError("protected payload is corrupt")
        if record["reference"] != reference or record["action_id"] != action_id or (
                record["purpose"] != purpose.value):
            raise ProtectedObjectCorruptError(
                "protected payload binding is invalid")
        digest = _require_digest(record["intent_digest"])
        if expected_intent_digest is not None and digest != (
                _require_digest(expected_intent_digest)):
            raise ProtectedObjectCorruptError(
                "protected payload intent binding is invalid")
        payload, _ = _canonical_content(
            record["payload"], MAX_PROTECTED_ACTION_PAYLOAD_BYTES)
        if digest != _payload_digest(key, purpose, payload):
            raise ProtectedObjectCorruptError(
                "protected payload content integrity is invalid")
        supplied_mac = _require_digest(record["binding_mac"])
        unsigned = {key_name: record[key_name] for key_name in record
                    if key_name != "binding_mac"}
        expected_mac = _binding_mac(
            key, "agentcore.protected-action-payload.binding.v1", unsigned)
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise ProtectedObjectCorruptError(
                "protected payload integrity is invalid")
        return StoredProtectedActionPayload(
            reference=reference, action_id=action_id, purpose=purpose,
            intent_digest=digest, payload=payload).validate()
    except ProtectedStorageError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtectedObjectCorruptError(
            "protected payload is corrupt") from exc


def _evidence_record(
    key: bytes,
    reference: str,
    action_id: str,
    purpose: EvidencePurpose,
    intent_digest: str,
    evidence: Mapping[str, Any],
    *,
    attempt_id: str | None,
    claim_epoch: int | None,
) -> dict[str, Any]:
    action_id = require_action_id(action_id)
    purpose = _evidence_purpose(purpose)
    intent_digest = _require_digest(intent_digest)
    if purpose is EvidencePurpose.ATTEMPT_EFFECT:
        if not isinstance(attempt_id, str) or not (
                _ACTION_ATTEMPT_ID.fullmatch(attempt_id)) or (
                claim_epoch is not None):
            raise ProtectedReferenceError("protected evidence binding is invalid")
    elif attempt_id is not None or not _positive_int(claim_epoch):
        raise ProtectedReferenceError("protected evidence binding is invalid")
    detached, _ = _canonical_content(evidence, MAX_PROTECTED_EVIDENCE_BYTES)
    evidence_digest = _evidence_digest(key, purpose, detached)
    unsigned = {
        "schema_version": PROTECTED_STORAGE_SCHEMA_VERSION,
        "namespace": "protected_evidence",
        "reference": reference,
        "action_id": action_id,
        "purpose": purpose.value,
        "intent_digest": intent_digest,
        "evidence_digest": evidence_digest,
        "attempt_id": attempt_id,
        "claim_epoch": claim_epoch,
        "evidence": detached,
    }
    return {**unsigned, "binding_mac": _binding_mac(
        key, "agentcore.protected-evidence.binding.v1", unsigned)}


def _decode_evidence(
    key: bytes,
    record: Mapping[str, Any],
    *,
    reference: str,
    action_id: str,
    purpose: EvidencePurpose,
    intent_digest: str,
    attempt_id: str | None,
    claim_epoch: int | None,
) -> StoredEvidence:
    try:
        _require_exact_keys(record, {
            "schema_version", "namespace", "reference", "action_id",
            "purpose", "intent_digest", "evidence_digest", "attempt_id",
            "claim_epoch", "evidence", "binding_mac",
        }, "protected evidence")
        action_id = require_action_id(action_id)
        purpose = _evidence_purpose(purpose)
        intent_digest = _require_digest(intent_digest)
        if record["schema_version"] != PROTECTED_STORAGE_SCHEMA_VERSION or (
                record["namespace"] != "protected_evidence"):
            raise ProtectedObjectCorruptError("protected evidence is corrupt")
        if record["reference"] != reference or record["action_id"] != action_id or (
                record["purpose"] != purpose.value) or (
                record["intent_digest"] != intent_digest) or (
                record["attempt_id"] != attempt_id) or (
                record["claim_epoch"] != claim_epoch):
            raise ProtectedObjectCorruptError(
                "protected evidence binding is invalid")
        evidence_digest = _require_digest(record["evidence_digest"])
        evidence, _ = _canonical_content(
            record["evidence"], MAX_PROTECTED_EVIDENCE_BYTES)
        if evidence_digest != _evidence_digest(key, purpose, evidence):
            raise ProtectedObjectCorruptError(
                "protected evidence content integrity is invalid")
        supplied_mac = _require_digest(record["binding_mac"])
        unsigned = {key_name: record[key_name] for key_name in record
                    if key_name != "binding_mac"}
        expected_mac = _binding_mac(
            key, "agentcore.protected-evidence.binding.v1", unsigned)
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise ProtectedObjectCorruptError(
                "protected evidence integrity is invalid")
        return StoredEvidence(
            reference=reference, action_id=action_id, purpose=purpose,
            intent_digest=intent_digest, evidence_digest=evidence_digest,
            evidence=evidence, attempt_id=attempt_id,
            claim_epoch=claim_epoch).validate()
    except ProtectedStorageError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtectedObjectCorruptError(
            "protected evidence is corrupt") from exc


def _payload_digest(
    key: bytes,
    purpose: ProtectedPayloadPurpose,
    payload: Mapping[str, Any],
) -> str:
    purpose = _payload_purpose(purpose)
    _, encoded = _canonical_content(
        payload, MAX_PROTECTED_ACTION_PAYLOAD_BYTES)
    return hmac.new(
        key,
        b"agentcore.protected-action-payload.intent-digest.v1\x00"
        + purpose.value.encode("ascii") + b"\x00" + encoded,
        hashlib.sha256,
    ).hexdigest()


def _evidence_digest(
    key: bytes,
    purpose: EvidencePurpose,
    evidence: Mapping[str, Any],
) -> str:
    purpose = _evidence_purpose(purpose)
    _, encoded = _canonical_content(evidence, MAX_PROTECTED_EVIDENCE_BYTES)
    return hmac.new(
        key,
        b"agentcore.protected-evidence.content-digest.v1\x00"
        + purpose.value.encode("ascii") + b"\x00" + encoded,
        hashlib.sha256,
    ).hexdigest()


def _binding_mac(key: bytes, domain: str, value: Mapping[str, Any]) -> str:
    return hmac.new(
        key,
        domain.encode("ascii") + b"\x00" + _encode_json(value),
        hashlib.sha256,
    ).hexdigest()


def _canonical_content(
    value: object,
    maximum_bytes: int,
) -> tuple[dict[str, Any], bytes]:
    counter = [0]
    text_budget = [0]
    detached = _detach_json(
        value,
        depth=0,
        counter=counter,
        text_budget=text_budget,
        maximum_text_chars=maximum_bytes,
    )
    if not isinstance(detached, dict):
        raise ProtectedObjectCorruptError(
            "protected content must be a JSON object")
    encoded = _encode_json(detached)
    if len(encoded) > maximum_bytes:
        raise ProtectedObjectTooLargeError(
            "protected content exceeds its size bound")
    return detached, encoded


def _detach_json(
    value: object,
    *,
    depth: int,
    counter: list[int],
    text_budget: list[int],
    maximum_text_chars: int,
) -> Any:
    if depth > MAX_PROTECTED_JSON_DEPTH:
        raise ProtectedObjectCorruptError("protected content nesting is invalid")
    counter[0] += 1
    if counter[0] > MAX_PROTECTED_JSON_ITEMS:
        raise ProtectedObjectTooLargeError(
            "protected content item bound is exceeded")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        _consume_text_budget(value, text_budget, maximum_text_chars)
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtectedObjectCorruptError(
                "protected content number is invalid")
        return value
    if isinstance(value, Mapping):
        detached: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtectedObjectCorruptError(
                    "protected content object key is invalid")
            _consume_text_budget(key, text_budget, maximum_text_chars)
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if normalized in _FORBIDDEN_PROTECTED_KEYS or any(
                    marker in normalized
                    for marker in _FORBIDDEN_PROTECTED_KEY_MARKERS):
                raise ProtectedObjectCorruptError(
                    "credential material is forbidden in protected storage")
            detached[key] = _detach_json(
                item,
                depth=depth + 1,
                counter=counter,
                text_budget=text_budget,
                maximum_text_chars=maximum_text_chars,
            )
        return detached
    if isinstance(value, (list, tuple)):
        return [_detach_json(
                    item,
                    depth=depth + 1,
                    counter=counter,
                    text_budget=text_budget,
                    maximum_text_chars=maximum_text_chars,
                )
                for item in value]
    raise ProtectedObjectCorruptError("protected content type is invalid")


def _consume_text_budget(
    value: str,
    text_budget: list[int],
    maximum_text_chars: int,
) -> None:
    text_budget[0] += len(value)
    if text_budget[0] > maximum_text_chars:
        raise ProtectedObjectTooLargeError(
            "protected content exceeds its size bound")


def _bounded_record_bytes(
    record: Mapping[str, Any],
    maximum_bytes: int,
    label: str,
) -> bytes:
    encoded = _encode_json(record)
    if len(encoded) > maximum_bytes:
        raise ProtectedObjectTooLargeError(f"{label} exceeds its size bound")
    return encoded


def _encode_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtectedObjectCorruptError(
            "protected content is not canonical JSON") from exc


def _strict_loads(encoded: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=_strict_json_object,
            parse_constant=lambda _: (_raise_invalid_constant()))
        if not isinstance(payload, Mapping):
            raise ProtectedObjectCorruptError(
                "protected object root is invalid")
        return payload
    except ProtectedStorageError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey,
            TypeError, ValueError) as exc:
        raise ProtectedObjectCorruptError(
            "protected object is corrupt") from exc


def _read_bounded_file(path: Path, maximum_bytes: int) -> bytes | None:
    """Read at most the configured bound plus one byte from one open handle."""
    with path.open("rb") as handle:
        encoded = handle.read(maximum_bytes + 1)
    return encoded if len(encoded) <= maximum_bytes else None


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey("duplicate JSON object key")
        result[key] = value
    return result


def _raise_invalid_constant() -> None:
    raise ValueError("non-finite JSON constant")


def _require_exact_keys(
    data: object,
    expected: set[str],
    label: str,
) -> None:
    if not isinstance(data, Mapping) or set(data) != expected or any(
            not isinstance(key, str) for key in data):
        raise ProtectedObjectCorruptError(f"{label} fields are invalid")


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != INTENT_DIGEST_HEX_CHARS or not (
            _LOWER_HEX_DIGEST.fullmatch(value)):
        raise ProtectedObjectCorruptError("protected digest is invalid")
    return value


def _positive_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and (
        1 <= value <= MAX_COUNTER_VALUE)


def _payload_purpose(value: object) -> ProtectedPayloadPurpose:
    if not isinstance(value, ProtectedPayloadPurpose):
        raise ProtectedReferenceError("protected payload purpose is invalid")
    return value


def _evidence_purpose(value: object) -> EvidencePurpose:
    if not isinstance(value, EvidencePurpose):
        raise ProtectedReferenceError("protected evidence purpose is invalid")
    return value


def _load_key(provider: InstallationKeyProvider) -> bytes:
    if not isinstance(provider, InstallationKeyProvider):
        raise InstallationKeyError("installation integrity key provider is invalid")
    try:
        return provider.load().validate().key
    except InstallationKeyError:
        raise
    except Exception as exc:
        raise InstallationKeyError(
            "installation integrity key is unavailable") from exc


def _require_owner(owner_pid: int) -> None:
    if owner_pid != os.getpid():
        raise ProtectedStoreOwnershipError(
            "protected store instance belongs to another process")


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
        raise ProtectedStoreOwnershipError(
            "protected store is owned by another process") from exc


def _unlock_owner_handle(handle: BinaryIO, *, owner_pid: int) -> None:
    # Recheck AFTER descriptor lookup: a same-thread fork inside cleanup must
    # close only its local copy, never unlock the parent's shared description.
    if owner_pid != os.getpid() or handle.closed:
        return
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        descriptor = handle.fileno()
        if owner_pid == os.getpid():
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        descriptor = handle.fileno()
        if owner_pid == os.getpid():
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "INSTALLATION_KEY_BYTES",
    "INSTALLATION_KEY_SCHEMA_VERSION",
    "MAX_INSTALLATION_KEY_FILE_BYTES",
    "MAX_PROTECTED_ACTION_PAYLOAD_BYTES",
    "MAX_PROTECTED_EVIDENCE_BYTES",
    "PROTECTED_STORAGE_ENCRYPTED_AT_REST",
    "PROTECTED_STORAGE_SCHEMA_VERSION",
    "EvidencePurpose",
    "EvidenceStore",
    "InMemoryEvidenceStore",
    "InMemoryProtectedActionPayloadStore",
    "InstallationKeyMaterial",
    "InstallationKeyProvider",
    "JsonFileEvidenceStore",
    "JsonFileInstallationKeyProvider",
    "JsonFileProtectedActionPayloadStore",
    "ProtectedActionPayloadStore",
    "ProtectedPayloadPurpose",
    "StaticInstallationKeyProvider",
    "StoredEvidence",
    "StoredProtectedActionPayload",
    "is_evidence_reference",
    "is_protected_payload_reference",
    "looks_like_evidence_reference",
    "looks_like_protected_payload_reference",
    "require_evidence_reference",
    "require_protected_payload_reference",
]
