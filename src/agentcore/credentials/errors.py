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
}


class CredentialError(ToolError):
    """Never accepts free text, provider exceptions, references, or secrets."""

    def __init__(self, reason: CredentialFailure) -> None:
        if type(reason) is not CredentialFailure:
            reason = CredentialFailure.INVALID
        self.reason = reason
        super().__init__(_MESSAGES[reason], reason=reason.value)
