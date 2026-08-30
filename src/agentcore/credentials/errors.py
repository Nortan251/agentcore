"""Closed, content-free errors for the credential boundary."""

from enum import Enum

from agentcore.errors import ToolError


class CredentialFailure(str, Enum):
    INVALID = "credential_invalid"
    NOT_FOUND = "credential_not_found"
    REVOKED = "credential_revoked"
    STALE = "credential_stale"
    EXPIRED = "credential_expired"
    UNAVAILABLE = "credential_unavailable"
    BUSY = "credential_busy"
    IDENTITY_MISMATCH = "credential_identity_mismatch"
    SCOPE_MISMATCH = "credential_scope_mismatch"
    ALREADY_CONNECTED = "credential_already_connected"
    OWNERSHIP = "credential_ownership_mismatch"
    BINDING = "credential_binding_invalid"
    AUTHORITY = "credential_authority_required"
    PERMISSION = "permission_denied"
    PROVIDER = "credential_provider_failed"
    UNSAFE_RESULT = "credential_unsafe_result"
    UNSUPPORTED = "credential_operation_unsupported"
    CAPACITY = "credential_capacity_reached"
    CORRUPT = "credential_store_corrupt"
    STORAGE_CHANGED = "credential_store_changed"
    COMMIT_FAILED = "credential_commit_failed"
    OAUTH_CALLBACK = "oauth_callback_invalid"
    OAUTH_STATE = "oauth_state_mismatch"
    OAUTH_REPLAY = "oauth_response_replayed"
    OAUTH_TIMEOUT = "oauth_authorization_expired"
    OAUTH_DENIED = "oauth_authorization_denied"
    OAUTH_UNCERTAIN = "oauth_exchange_uncertain"
    IDENTITY_TOKEN = "oauth_identity_invalid"
    RECONNECT = "oauth_reconnect_required"


_MESSAGES = {
    CredentialFailure.INVALID: "Credential input is invalid.",
    CredentialFailure.NOT_FOUND: "The credential reference is unknown.",
    CredentialFailure.REVOKED: "The credential has been revoked.",
    CredentialFailure.STALE: "The credential reference has been replaced.",
    CredentialFailure.EXPIRED: "The credential has expired.",
    CredentialFailure.UNAVAILABLE: "The credential service is unavailable.",
    CredentialFailure.BUSY: "The credential is in use; the change did not occur.",
    CredentialFailure.IDENTITY_MISMATCH: "The credential account binding does not match.",
    CredentialFailure.SCOPE_MISMATCH: "The credential scopes do not permit this operation.",
    CredentialFailure.ALREADY_CONNECTED: "This account already has a current connection.",
    CredentialFailure.OWNERSHIP: "The credential object belongs to another execution owner.",
    CredentialFailure.BINDING: "The current authenticated tool binding could not be verified.",
    CredentialFailure.AUTHORITY: "Current executor authority is required for credential use.",
    CredentialFailure.PERMISSION: "Current permission does not allow credential use.",
    CredentialFailure.PROVIDER: "The authenticated provider call failed.",
    CredentialFailure.UNSAFE_RESULT: "The authenticated provider result was withheld.",
    CredentialFailure.UNSUPPORTED: "This authenticated operation is not supported.",
    CredentialFailure.CAPACITY: "The credential service capacity has been reached.",
    CredentialFailure.CORRUPT: "The protected credential record could not be verified.",
    CredentialFailure.STORAGE_CHANGED: "The protected credential storage changed unexpectedly.",
    CredentialFailure.COMMIT_FAILED: "The credential update was not confirmed durable.",
    CredentialFailure.OAUTH_CALLBACK: "The authorization callback is invalid.",
    CredentialFailure.OAUTH_STATE: "The authorization state does not match.",
    CredentialFailure.OAUTH_REPLAY: "The authorization response has already been consumed.",
    CredentialFailure.OAUTH_TIMEOUT: "The authorization attempt has expired.",
    CredentialFailure.OAUTH_DENIED: "Authorization was not granted.",
    CredentialFailure.OAUTH_UNCERTAIN: "The token exchange outcome is uncertain.",
    CredentialFailure.IDENTITY_TOKEN: "The Google identity proof could not be verified.",
    CredentialFailure.RECONNECT: "A new Google authorization is required.",
}


class CredentialError(ToolError):
    """Never accepts free text, provider exceptions, references, or secrets."""

    def __init__(self, reason: CredentialFailure) -> None:
        if type(reason) is not CredentialFailure:
            reason = CredentialFailure.INVALID
        self.reason = reason
        super().__init__(_MESSAGES[reason], reason=reason.value)
