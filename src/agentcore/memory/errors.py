"""Memory layer errors.

All derive from :class:`~agentcore.errors.AgentError` so callers can keep
distinguishing framework failures from arbitrary exceptions, exactly as the
planning, verification and model layers do.
"""

from __future__ import annotations

from agentcore.errors import AgentError


class MemoryError(AgentError):
    """Base class for memory-layer failures."""


class MemoryValidationError(MemoryError):
    """A record is malformed or violates an invariant."""


class MemoryNotFoundError(MemoryError):
    """No memory exists with the requested id."""


class MemoryConflictError(MemoryError):
    """A write would overwrite a different durable fact.

    Raised rather than resolved: silently replacing a conflicting memory would
    destroy information the agent may not be able to recover. The caller must
    decide explicitly (update the existing record, or store a distinct one).
    """


class MemoryLimitError(MemoryError):
    """The configured record ceiling would be exceeded."""
