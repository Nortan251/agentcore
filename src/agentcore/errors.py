"""Central error hierarchy.

Every failure raised by the framework derives from :class:`AgentError` so that
callers can distinguish framework failures from arbitrary Python exceptions.
Errors carry a ``retryable`` hint used later by recovery logic.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class for all framework errors."""

    retryable: bool = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class ConfigError(AgentError):
    """Invalid or missing configuration."""


class ToolError(AgentError):
    """Base class for tool-related failures."""


class ToolNotFoundError(ToolError):
    """Requested tool is not registered."""


class ToolValidationError(ToolError):
    """Arguments did not satisfy the tool's declared parameter schema."""


class ToolExecutionError(ToolError):
    """The tool ran but failed."""

    retryable = True


class PermissionDeniedError(AgentError):
    """The active permission policy refused an action."""


class PolicyLeaseBusyError(ToolError):
    """Policy replacement was refused during an externally effectful read.

    The current policy is unchanged. Hosts may retry replacement after the
    bounded invocation finishes; this is not an execution authorization.
    """


class ApprovalRequiredError(AgentError):
    """Action is allowed but requires an explicit human confirmation first."""


class StateError(AgentError):
    """Invalid state transition or missing state record."""


class TaskError(AgentError):
    """Invalid task structure (e.g. unknown or cyclic dependency)."""
