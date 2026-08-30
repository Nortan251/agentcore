"""Narrow OS protection seam. There is deliberately no plaintext fallback."""

from __future__ import annotations

import os
from typing import Protocol

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.runtime import _metadata_call


class CredentialProtector(Protocol):
    """Trusted host adapter; test protectors require explicit test-only opt-in."""

    def protect(self, plaintext: bytes, *, purpose: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes, *, purpose: bytes) -> bytes: ...


class WindowsDpapiProtector(RuntimeOnly):
    """Windows current-user DPAPI, UI forbidden, never machine-wide protection.

    pywin32 calls CryptProtectData/CryptUnprotectData. DPAPI supplies encryption
    and integrity; purpose is application/domain binding, not a homemade key.
    It does not protect against code running as the same user or an admin.
    """

    __slots__ = ("_owner_pid",)

    def __init__(self) -> None:
        if os.name != "nt":
            raise CredentialError(CredentialFailure.UNSUPPORTED)
        self._owner_pid = os.getpid()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def protect(self, plaintext: bytes, *, purpose: bytes) -> bytes:
        self._owner()

        def call() -> bytes:
            import win32crypt

            return win32crypt.CryptProtectData(
                plaintext, "AgentCore credentials v1", purpose, None, None, 0x1)

        result = _metadata_call(call)
        self._owner()
        return result

    def unprotect(self, ciphertext: bytes, *, purpose: bytes) -> bytes:
        self._owner()

        def call() -> bytes:
            import win32crypt

            return win32crypt.CryptUnprotectData(ciphertext, purpose, None, None, 0x1)[1]

        result = _metadata_call(call)
        self._owner()
        return result
