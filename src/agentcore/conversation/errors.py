"""Conversation runtime errors.

All derive from :class:`~agentcore.errors.AgentError`, matching every other
layer, so callers keep one way to distinguish framework failures.
"""

from __future__ import annotations

from agentcore.errors import AgentError


class ConversationError(AgentError):
    """Base class for conversational runtime failures."""


class ConversationValidationError(ConversationError):
    """A session or message is malformed."""


class SessionDomainError(ConversationError):
    """A transcript is unavailable to this conversation composition."""


class SessionNotFoundError(ConversationError):
    """No session exists with the requested id."""


class ResponseGenerationError(ConversationError):
    """The model could not produce an assistant reply.

    Raised rather than returning an empty reply: a fabricated assistant
    message is worse than an honest failure.
    """

    retryable = True
