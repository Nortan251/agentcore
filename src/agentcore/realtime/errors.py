"""Categorical failures for the internal real-time lifecycle primitives."""

from __future__ import annotations

from typing import Any

from agentcore.errors import AgentError


# Cancellation detail is ephemeral operator/runtime context, never durable
# error metadata.  Keep even that ephemeral copy small and predictable.
MAX_CANCELLATION_DETAIL_CHARS = 256


class RealtimeError(AgentError):
    """Base class for real-time lifecycle failures."""


class TurnValidationError(RealtimeError):
    """A turn record, identity, deadline, or operation is malformed."""


class TurnTransitionError(RealtimeError):
    """A requested turn transition violates the lifecycle contract."""


class TurnOperationConflictError(TurnTransitionError):
    """An operation id was reused for a different transition intent."""


class TurnStoreError(RealtimeError):
    """Base class for durable turn-store failures."""


class TurnNotFoundError(TurnStoreError):
    """No durable turn exists for the requested identity."""


class TurnAlreadyExistsError(TurnStoreError):
    """Creation would overwrite a different existing turn."""


class TurnVersionConflictError(TurnStoreError):
    """A compare-and-swap write observed a newer record version."""


class TurnStoreOwnershipError(TurnStoreError):
    """Another process already owns the durable turn store."""


class TurnCoordinatorError(RealtimeError):
    """Durable session admission state violates coordinator invariants."""


class TurnCorrelationError(RealtimeError):
    """Correlation between TurnRecord and AgentState is invalid."""


class TurnCorrelationConflictError(TurnCorrelationError):
    """A turn is already bound to a different run, or vice versa."""


class CancellationError(RealtimeError):
    """Base class for cancellation primitive failures."""


class CancellationClosedError(CancellationError):
    """An operation requires a cancellation source that is still open."""


class CancellationRequested(CancellationError):
    """Cooperative cancellation was requested for this operation."""

    def __init__(self, reason: Any, *, detail: str = "") -> None:
        value = getattr(reason, "value", None)
        if not isinstance(value, str):
            value = type(reason).__name__
        # Normal requests are validated by CancellationSource.  Bound again at
        # this exception boundary so direct construction cannot retain an
        # arbitrarily large string.  The raw text stays on this short-lived
        # exception only; AgentError.details receives shape, never content.
        ephemeral_detail = (detail if isinstance(detail, str) else "")[
            :MAX_CANCELLATION_DETAIL_CHARS]
        super().__init__("operation cancellation requested",
                         reason=value,
                         has_detail=bool(ephemeral_detail),
                         detail_chars=len(ephemeral_detail))
        self.reason = reason
        self.detail = ephemeral_detail
