"""Trusted per-request context for native neutral model streams.

The frozen native streaming entry point remains::

    stream(request, *, context: OperationContext, clock: Clock)

``OperationContext`` owns the realtime turn and current GENERATING operation,
but a native provider must also know the runtime-owned logical ``request_id``
in order to construct neutral event envelopes.  ``StreamRequestContext`` is a
narrow ephemeral subtype passed through that existing ``context`` parameter.
It changes neither the provider signature nor the durable turn schema.

The conversation runtime separately gives the same freshly generated ID to
``StreamAccumulator``.  Provider output therefore cannot establish request
identity merely by arriving first.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentcore.models.streaming import (
    StreamValidationError,
    _require_safe_id,
)
from agentcore.realtime.turn import OperationContext


@dataclass(frozen=True)
class StreamRequestContext(OperationContext):
    """One owned model-round context with its runtime-created request ID."""

    request_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        # Reuse the merged 42.5a validator rather than creating a second ID
        # rule that could drift from StreamAccumulator/envelope validation.
        _require_safe_id(self.request_id, "request_id")

    @classmethod
    def from_operation(cls, context: OperationContext, *,
                       request_id: str) -> StreamRequestContext:
        """Copy trusted operation fields without changing owned objects.

        The cancellation token and deadline retain object identity.  No value
        is placed in ``ModelRequest.metadata`` or generic provider options.
        """
        if not isinstance(context, OperationContext):
            raise StreamValidationError(
                "context must be a trusted OperationContext")
        return cls(
            turn_id=context.turn_id,
            session_id=context.session_id,
            generation=context.generation,
            operation_id=context.operation_id,
            cancellation=context.cancellation,
            deadline=context.deadline,
            request_id=request_id,
        )
