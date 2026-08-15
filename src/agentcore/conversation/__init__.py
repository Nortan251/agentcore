"""Conversational runtime: dialogue wired to durable memory.

Turns a user message into an assistant reply while keeping three kinds of
state strictly apart — conversation history, durable memory, and run state —
and while letting only what the capture policy independently judges durable
reach the memory store.

Exports are resolved lazily (PEP 562) for the same reason as
:mod:`agentcore.memory` and :mod:`agentcore.recovery`: ``config.schema``
re-exports :class:`ConversationConfig` from here, so eagerly importing
``runtime`` (which reaches ``observability`` and therefore back into
``config.schema``) would create an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentcore.conversation.config import ConversationConfig
    from agentcore.conversation.errors import (
        ConversationError,
        ConversationValidationError,
        ResponseGenerationError,
        SessionNotFoundError,
    )
    from agentcore.conversation.runtime import (
        SYSTEM_PROMPT,
        ConversationRuntime,
        TurnResult,
    )
    from agentcore.conversation.session import (
        ConversationSession,
        InMemorySessionStore,
        JsonFileSessionStore,
        SessionStore,
        build_session_store,
    )

_EXPORTS: dict[str, str] = {
    "ConversationConfig": "agentcore.conversation.config",
    "ConversationRuntime": "agentcore.conversation.runtime",
    "TurnResult": "agentcore.conversation.runtime",
    "SYSTEM_PROMPT": "agentcore.conversation.runtime",
    "ConversationSession": "agentcore.conversation.session",
    "SessionStore": "agentcore.conversation.session",
    "InMemorySessionStore": "agentcore.conversation.session",
    "JsonFileSessionStore": "agentcore.conversation.session",
    "build_session_store": "agentcore.conversation.session",
    "ConversationError": "agentcore.conversation.errors",
    "ConversationValidationError": "agentcore.conversation.errors",
    "SessionNotFoundError": "agentcore.conversation.errors",
    "ResponseGenerationError": "agentcore.conversation.errors",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
