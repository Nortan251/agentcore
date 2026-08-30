"""Opt-in credential/identity foundation. No OAuth or storage auto-discovery."""

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import (
    AccountIdentity,
    CredentialInfo,
    CredentialState,
    SecretMaterial,
)
from agentcore.credentials.runtime import AuthenticatedReadProvider, CredentialRuntime
from agentcore.credentials.vault import CredentialLease, CredentialVault, InMemoryCredentialVault

__all__ = [
    "AccountIdentity", "AuthenticatedReadProvider", "CredentialError",
    "CredentialFailure", "CredentialInfo", "CredentialLease", "CredentialRuntime",
    "CredentialState", "CredentialVault", "InMemoryCredentialVault", "SecretMaterial",
]
