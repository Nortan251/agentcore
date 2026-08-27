"""Categorical failures for durable human approval authority."""

from __future__ import annotations

from agentcore.errors import AgentError


class ApprovalError(AgentError):
    """Base class for the independent durable-approval domain."""


class ApprovalValidationError(ApprovalError):
    """An approval identity, record, receipt, or transition is malformed."""


class ApprovalStoreError(ApprovalError):
    """Durable approval storage is unavailable or corrupt."""


class ApprovalNotFoundError(ApprovalStoreError):
    """No durable request exists for the requested opaque identity."""


class ApprovalAlreadyExistsError(ApprovalStoreError):
    """An approval request ID already owns another immutable request."""


class ApprovalVersionConflictError(ApprovalStoreError):
    """A compare-and-swap observed another request version."""


class ApprovalStoreOwnershipError(ApprovalStoreError):
    """The approval store belongs to another process or owner."""


class ApprovalUnavailableError(ApprovalError):
    """Trusted approval authority cannot establish a final decision."""
