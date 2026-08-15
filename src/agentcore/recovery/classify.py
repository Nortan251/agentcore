"""Deterministic failure classification and retry-safety analysis.

Classification is rule-based on purpose: it runs after every failure, so it
must be cheap and predictable. No model is consulted here.

Retry safety is the more important half. A retry is *not* automatically safe
just because something failed — repeating a call that already had a side effect
can duplicate it. This module answers "can we prove a repeat is harmless?" and
returns ``UNKNOWN`` whenever it cannot, which blocks automatic retries.
"""

from __future__ import annotations

from agentcore.permissions.levels import READ_ONLY_CAPABILITIES, Capability
from agentcore.recovery.decision import FailureCategory, RetrySafety
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    TaskStatus,
    VerificationStatus,
)
from agentcore.tools.registry import ToolRegistry

#: Error record types that are never worth repeating unchanged: the same call
#: will deterministically fail the same way.
PERSISTENT_ERROR_TYPES: frozenset[str] = frozenset({
    "validation",
    "ToolValidationError",
    "ToolNotFoundError",
    "unhandled_exception",
})

#: Error types that indicate the permission policy refused the action.
PERMISSION_ERROR_TYPES: frozenset[str] = frozenset({
    "permission_denied",
    "PermissionDeniedError",
    "ApprovalRequiredError",
})


def classify(subtask: Subtask, *, repeated: bool = False) -> FailureCategory:
    """Categorise why ``subtask`` needs recovery.

    ``repeated`` is supplied by the policy when it has detected that the same
    failure signature has already been seen.
    """
    last = subtask.errors[-1] if subtask.errors else None

    if last is not None and _is_permission_error(last):
        # Checked before "repeated" so a blocked action is always reported as
        # a permission problem rather than generic exhaustion.
        return FailureCategory.PERMISSION_DENIED

    if repeated:
        return FailureCategory.REPEATED

    if last is not None:
        if _is_persistent_error(last):
            return FailureCategory.PERSISTENT
        if last.retryable:
            return FailureCategory.TRANSIENT
        return FailureCategory.PERSISTENT

    # No error recorded: the concern is the verification verdict.
    status = subtask.verification.status
    if status is VerificationStatus.FAILED:
        return FailureCategory.VERIFICATION_FAILED
    if status is VerificationStatus.UNVERIFIED:
        return FailureCategory.UNVERIFIED
    return FailureCategory.NONE


def _is_permission_error(error: ErrorRecord) -> bool:
    if error.type in PERMISSION_ERROR_TYPES:
        return True
    return str(error.details.get("kind", "")) in PERMISSION_ERROR_TYPES


def _is_persistent_error(error: ErrorRecord) -> bool:
    if error.type in PERSISTENT_ERROR_TYPES:
        return True
    return str(error.details.get("kind", "")) in PERSISTENT_ERROR_TYPES


def failure_signature(subtask: Subtask) -> str:
    """A stable fingerprint of the current failure.

    Used to detect "the same thing failed the same way again", which is the
    signal that retrying is pointless regardless of the attempt budget.
    """
    if subtask.errors:
        last = subtask.errors[-1]
        return f"error:{last.type}:{last.message}"
    verification = subtask.verification
    if verification.status in (VerificationStatus.FAILED,
                               VerificationStatus.UNVERIFIED):
        return f"verification:{verification.status}:{verification.detail}"
    return "none"


def assess_retry_safety(subtask: Subtask,
                        registry: ToolRegistry | None = None) -> RetrySafety:
    """Decide whether repeating this subtask's action is provably safe.

    The only case treated as ``SAFE`` is a read-only tool: by declaration it
    changes nothing, so repeating it cannot duplicate an effect.

    A mutating tool is ``UNKNOWN`` — not ``UNSAFE`` — because the system cannot
    tell whether the side effect happened before the failure. Callers must
    treat ``UNKNOWN`` as "do not retry automatically".
    """
    if subtask.tool_name is None:
        # Nothing was executed, so nothing can be duplicated.
        return RetrySafety.SAFE

    if registry is None or not registry.has(subtask.tool_name):
        # Cannot inspect the tool contract; refuse to assume anything.
        return RetrySafety.UNKNOWN

    spec = registry.get(subtask.tool_name).spec

    if spec.read_only:
        return RetrySafety.SAFE

    capabilities = set(spec.capabilities)
    if capabilities and capabilities <= set(READ_ONLY_CAPABILITIES):
        return RetrySafety.SAFE

    # Irreversible or externally-visible effects must never be auto-repeated.
    if capabilities & {Capability.FS_DELETE, Capability.EMAIL_SEND,
                       Capability.PAYMENT, Capability.SYSTEM_ADMIN}:
        return RetrySafety.UNSAFE

    return RetrySafety.UNKNOWN


def failed_before_side_effect(subtask: Subtask) -> bool:
    """True when the failure provably occurred *before* the tool ran.

    Validation and permission failures are rejected by the executor ahead of
    ``Tool.run``, so no side effect can have occurred — such a subtask is safe
    to re-attempt from a duplication standpoint (though it may still be
    pointless, which classification handles separately).
    """
    if not subtask.errors:
        return False
    last = subtask.errors[-1]
    kind = str(last.details.get("kind", "")) or last.type
    return kind in {"validation", "ToolValidationError", "permission_denied",
                    "PermissionDeniedError", "ToolNotFoundError"}


def is_recoverable_state(subtask: Subtask) -> bool:
    """Whether the subtask is in a state where recovery makes sense."""
    if subtask.status is TaskStatus.CANCELLED:
        return False
    if subtask.status is TaskStatus.COMPLETED:
        # Completed work is only a recovery candidate if verification objected.
        return subtask.verification.status in (VerificationStatus.FAILED,
                                               VerificationStatus.UNVERIFIED)
    return True
