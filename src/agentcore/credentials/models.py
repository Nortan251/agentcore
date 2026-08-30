"""Secret-free identities and non-serializable credential material.

Account subjects are supplied by trusted host authentication, not email/display
names or model arguments. No class here authenticates an OAuth response.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure


MAX_SECRET_CHARS = 16_384
MAX_SCOPES = 64
_PROVIDER = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_REFERENCE = re.compile(r"crd_[0-9a-f]{32}\Z")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")


class RuntimeOnly:
    """Do not let generic persistence/debug copying walk runtime objects."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<credential runtime object>"

    def __str__(self) -> str:
        return self.__repr__()

    def __reduce_ex__(self, protocol: int) -> Any:
        raise TypeError("credential runtime objects cannot be serialized")

    def __copy__(self) -> Any:
        raise TypeError("credential runtime objects cannot be copied")

    def __deepcopy__(self, memo: Any) -> Any:
        raise TypeError("credential runtime objects cannot be copied")


class SecretMaterial(RuntimeOnly):
    """Explicit host input, with no public plaintext accessor or serializer.

    This is an accidental-disclosure boundary, not locked/encrypted memory.
    Python strings and caller-owned copies cannot be reliably zeroized.
    Production storage and protection against a compromised host are separate
    backend requirements.
    """

    __slots__ = ("_value", "_owner_pid")

    def __init__(self, value: str) -> None:
        if type(value) is not str or not 1 <= len(value) <= MAX_SECRET_CHARS:
            raise CredentialError(CredentialFailure.INVALID)
        self._value: str | None = value
        self._owner_pid = os.getpid()

    def __repr__(self) -> str:
        return "<SecretMaterial redacted>"

    def _read(self) -> str:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._value is None:
            raise CredentialError(CredentialFailure.UNAVAILABLE)
        return self._value

    def _discard(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        self._value = None


def require_provider(value: str) -> str:
    if type(value) is not str or _PROVIDER.fullmatch(value) is None:
        raise CredentialError(CredentialFailure.INVALID)
    return value


def require_reference(value: str) -> str:
    if type(value) is not str or _REFERENCE.fullmatch(value) is None:
        raise CredentialError(CredentialFailure.INVALID)
    return value


def require_scopes(values: frozenset[str]) -> frozenset[str]:
    if type(values) is not frozenset or not 1 <= len(values) <= MAX_SCOPES:
        raise CredentialError(CredentialFailure.INVALID)
    if any(type(value) is not str or _SCOPE.fullmatch(value) is None
           for value in values):
        raise CredentialError(CredentialFailure.INVALID)
    return values


def require_time(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise CredentialError(CredentialFailure.INVALID)
    return value


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """Exact provider/issuer/subject tuple; no normalization or email fallback.

    The issuer includes any provider-specific tenant namespace. These fields
    are trusted host metadata; only the opaque account ID is offered to tools.
    """

    provider: str
    issuer: str = field(repr=False)
    subject: str = field(repr=False)

    def __post_init__(self) -> None:
        require_provider(self.provider)
        for value in (self.issuer, self.subject):
            if type(value) is not str or not 1 <= len(value) <= 512 or any(
                    character.isspace() or ord(character) < 32
                    for character in value):
                raise CredentialError(CredentialFailure.INVALID)


class CredentialState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    REPLACED = "replaced"


@dataclass(frozen=True, slots=True)
class CredentialInfo:
    """Inert metadata. Possession of this snapshot grants no access."""

    reference: str
    connection_id: str
    account_id: str
    provider: str
    scopes: frozenset[str]
    generation: int
    state: CredentialState = CredentialState.ACTIVE
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "connection_id": self.connection_id,
            "account_id": self.account_id,
            "provider": self.provider,
            "scopes": sorted(self.scopes),
            "generation": self.generation,
            "state": self.state.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    def require_current(self, now: datetime) -> None:
        if self.state is CredentialState.REVOKED:
            raise CredentialError(CredentialFailure.REVOKED)
        if self.state is CredentialState.REPLACED:
            raise CredentialError(CredentialFailure.STALE)
        if self.state is not CredentialState.ACTIVE:
            raise CredentialError(CredentialFailure.UNAVAILABLE)
        if self.expires_at is not None and now >= self.expires_at:
            raise CredentialError(CredentialFailure.EXPIRED)
