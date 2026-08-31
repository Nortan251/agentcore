"""Host-injected credentials and Google identity connections; no auto-discovery."""

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import (
    AccountIdentity,
    CredentialInfo,
    CredentialState,
    SecretMaterial,
)
from agentcore.credentials.runtime import AuthenticatedReadProvider, CredentialRuntime
from agentcore.credentials.vault import CredentialLease, CredentialVault, InMemoryCredentialVault
from agentcore.credentials.google import GoogleConnectionService
from agentcore.credentials.google_protocol import GoogleAccessProfile, GoogleOAuthConfig
from agentcore.credentials.protected import ConnectionState, ConnectionStatus, ProtectedCredentialVault
from agentcore.credentials.protection import WindowsDpapiProtector

__all__ = [
    "AccountIdentity", "AuthenticatedReadProvider", "CredentialError",
    "CredentialFailure", "CredentialInfo", "CredentialLease", "CredentialRuntime",
    "CredentialState", "CredentialVault", "InMemoryCredentialVault", "SecretMaterial",
    "ConnectionState", "ConnectionStatus", "GoogleAccessProfile", "GoogleConnectionService", "GoogleOAuthConfig",
    "ProtectedCredentialVault", "WindowsDpapiProtector",
]
