"""Categorical failures for the durable action-journal domain."""

from __future__ import annotations

from agentcore.errors import AgentError


class ActionJournalError(AgentError):
    """Base class for ActionJournal failures."""


class ActionValidationError(ActionJournalError):
    """An action record, identity, bound, or transition is malformed."""


class ActionTransitionError(ActionValidationError):
    """A requested action transition violates the frozen lifecycle."""


class ActionAdapterError(ActionJournalError):
    """A trusted action adapter contract is missing or malformed."""


class ActionAdapterRegistrationError(ActionAdapterError):
    """Trusted action-adapter registration is contradictory or duplicated."""


class ActionEffectNormalizationError(ActionAdapterError):
    """A capability adapter did not return bounded categorical effect truth."""


class ActionStoreError(ActionJournalError):
    """Base class for durable action-store failures."""


class ActionNotFoundError(ActionStoreError):
    """No durable action exists for the requested identity."""


class ActionAlreadyExistsError(ActionStoreError):
    """An action ID already owns a different immutable intent."""


class ActionVersionConflictError(ActionStoreError):
    """A compare-and-swap observed a newer action-record version."""


class ActionDeduplicationConflictError(ActionStoreError):
    """A deduplication identity has conflicting or duplicate ownership."""


class ActionStoreOwnershipError(ActionStoreError):
    """Another process owns the durable action store."""


class ProtectedStorageError(ActionJournalError):
    """Base class for segregated protected action-storage failures."""


class InstallationKeyError(ProtectedStorageError):
    """Installation-local integrity key material is unavailable or corrupt."""


class ProtectedReferenceError(ProtectedStorageError):
    """A protected object reference is malformed or has the wrong namespace."""


class ProtectedObjectNotFoundError(ProtectedStorageError):
    """A required protected object does not exist."""


class ProtectedObjectCorruptError(ProtectedStorageError):
    """A protected object failed strict decoding or integrity validation."""


class ProtectedObjectAlreadyExistsError(ProtectedStorageError):
    """An immutable protected object reference is already occupied."""


class ProtectedObjectTooLargeError(ProtectedStorageError):
    """A protected object exceeds its fixed serialized size bound."""


class ProtectedStoreOwnershipError(ProtectedStorageError):
    """A protected store instance belongs to another process."""
