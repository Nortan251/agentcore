"""Planning error hierarchy.

Extends the project's existing :class:`AgentError` tree (same approach as
:mod:`agentcore.models.errors`) so callers keep catching ``AgentError`` and the
``retryable`` hint keeps driving recovery logic.

The distinction between these types matters for debugging: a parse failure is a
model-output problem, a validation failure is a *plan content* problem, and the
details attached to each say exactly which step and field was at fault.
"""

from __future__ import annotations

from typing import Any

from agentcore.errors import AgentError


class PlanningError(AgentError):
    """Base class for every planning failure."""


class PlanGenerationError(PlanningError):
    """The model could not be reached or returned no usable text.

    Retryable because the underlying cause is usually transient (the model
    layer already classifies and retries network-level faults).
    """

    retryable = True


class PlanParseError(PlanningError):
    """Model output was not parseable JSON, or not a JSON object."""


class PlanSchemaError(PlanningError):
    """JSON parsed, but required fields are missing or wrongly typed."""

    def __init__(self, message: str, *, step: Any = None, field: str | None = None,
                 **details: Any) -> None:
        super().__init__(message, **details)
        if step is not None:
            self.details["step"] = step
        if field is not None:
            self.details["field"] = field


class PlanValidationError(PlanningError):
    """The plan is well-formed but semantically unusable.

    Covers unknown tools, invalid tool arguments, bad dependency graphs, empty
    plans and oversized plans.
    """

    def __init__(self, message: str, *, step_id: str | None = None,
                 **details: Any) -> None:
        super().__init__(message, **details)
        if step_id is not None:
            self.details["step_id"] = step_id
