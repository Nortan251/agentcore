"""Conversation runtime configuration.

Lives in the conversation package (like ``memory/config.py`` and
``permissions/config.py``) so ``config.schema`` can re-export it without a
cycle: this module imports only :mod:`agentcore.errors`.

Only settings the runtime actually consumes are defined. There is deliberately
no ``enabled`` flag: the runtime is constructed explicitly by a caller, and
memory participation is already governed by ``features.memory``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from agentcore.errors import ConfigError


@dataclass(frozen=True)
class ConversationConfig:
    """Bounds for one conversational turn."""

    #: Directory holding persisted sessions. ``None`` means
    #: ``<workspace_dir>/conversations``, mirroring runs and memory.
    directory: str | None = None
    #: Recent messages included in the model request. The *stored* session
    #: keeps everything; this only bounds what is sent.
    max_context_messages: int = 20
    #: Character ceiling on the rendered conversation history. A second,
    #: independent bound, because a few very long turns can blow past a
    #: message count while still being enormous.
    max_context_chars: int = 8000
    #: Longest single message accepted into a session.
    max_message_chars: int = 8000
    #: Messages retained in a persisted session. Older turns are dropped from
    #: storage only, never promoted to durable memory.
    max_session_messages: int = 200
    #: Durable memories retrieved per turn and rendered into the prompt.
    max_memories: int = 5
    #: Characters of each memory's content rendered into the prompt.
    max_memory_chars: int = 400
    #: Whether the model may request tools during a conversation. Off by
    #: default: talking is safe, acting is a deliberate choice.
    tools_enabled: bool = False
    #: Hard ceiling on tool -> model -> tool rounds in a single turn. Bounds
    #: the loop so a model cannot call tools forever.
    max_tool_rounds: int = 3
    #: Characters of a single tool result placed into model context. Truncation
    #: is deterministic and explicitly marked.
    max_tool_result_chars: int = 2000
    #: Realtime turn deadline, in seconds, from admission through final
    #: response (Task 42.4a). Consumed only when a realtime turn coordinator
    #: is injected: with no coordinator the conversation path is unchanged
    #: and this bound is not applied. Queue/stage-lease waiting consumes the
    #: budget; every admitted successor receives its own independent budget.
    turn_deadline_seconds: float = 120.0

    def validate(self) -> None:
        for name in ("max_context_messages", "max_context_chars",
                     "max_message_chars", "max_session_messages",
                     "max_memories", "max_memory_chars",
                     "max_tool_rounds", "max_tool_result_chars"):
            if getattr(self, name) < 1:
                raise ConfigError(f"conversation.{name} must be at least 1",
                                  value=getattr(self, name))
        deadline = self.turn_deadline_seconds
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) \
                or not math.isfinite(deadline):
            raise ConfigError(
                "conversation.turn_deadline_seconds must be a finite number",
                value=deadline)
        if not 1.0 <= deadline <= 3600.0:
            raise ConfigError(
                "conversation.turn_deadline_seconds must be between "
                "1.0 and 3600.0 seconds",
                value=deadline)
        if self.max_session_messages < self.max_context_messages:
            raise ConfigError(
                "conversation.max_session_messages must be at least "
                "max_context_messages, otherwise stored history would be "
                "smaller than the window sent to the model",
                stored=self.max_session_messages,
                context=self.max_context_messages)
