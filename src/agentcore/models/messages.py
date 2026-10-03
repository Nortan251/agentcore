"""Vendor-neutral request/response types for the model layer.

These types are the boundary between the agent and any LLM. Core components
(Agent, TaskRunner, a future Planner) speak only this vocabulary, so swapping
providers never reaches beyond :mod:`agentcore.models`.

Extensibility note: :class:`ModelRequest` already carries an optional ``tools``
catalogue and ``response_format``, and :class:`ModelResponse` already carries
``tool_calls``. No tool-calling *logic* is implemented in this task — but the
shapes exist so a future Planner can request structured decisions without the
model layer being redesigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Who authored a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    def __str__(self) -> str:
        return self.value


class FinishReason(str, Enum):
    """Why generation stopped, normalised across providers."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Message:
    """A single conversation turn."""

    role: Role
    content: str
    name: str | None = None
    # Protocol-only, current-turn metadata. ConversationSession persists only
    # role/content/name via to_dict(); it never stores model arguments or IDs.
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Persisted dialogue shape, deliberately excluding protocol metadata."""
        payload: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name:
            payload["name"] = self.name
        return payload

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(Role.SYSTEM, content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(Role.USER, content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(Role.ASSISTANT, content)


@dataclass(frozen=True)
class ToolCall:
    """A structured tool invocation requested by the model.

    Populated by providers when present. Nothing in this task acts on it; the
    future Planner will.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True)
class Usage:
    """Token accounting, when the provider reports it."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_payload(cls, data: Any) -> Usage:
        """Read usage defensively — it is optional and often partial."""
        if not isinstance(data, dict):
            return cls()

        def _int(key: str) -> int:
            value = data.get(key)
            return value if isinstance(value, int) and value >= 0 else 0

        prompt = _int("prompt_tokens")
        completion = _int("completion_tokens")
        total = _int("total_tokens") or (prompt + completion)
        return cls(prompt, completion, total)


@dataclass
class ModelRequest:
    """A provider-agnostic generation request.

    ``model`` and generation parameters are left as ``None`` where the provider
    should fall back to its configured defaults, so callers only override what
    they actually care about.
    """

    messages: list[Message]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    stop: tuple[str, ...] = ()
    # Reserved for the future Planner; no logic in this task consumes these.
    tools: list[dict[str, Any]] = field(default_factory=list)
    response_format: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("ModelRequest requires at least one message")

    @classmethod
    def from_prompt(cls, prompt: str, *, system: str | None = None,
                    **options: Any) -> ModelRequest:
        """Build a request from the simple prompt form used by ``complete()``."""
        messages = ([Message.system(system)] if system else []) + [
            Message.user(prompt)]
        known = {"model", "temperature", "max_output_tokens", "stop",
                 "tools", "response_format", "metadata"}
        kwargs = {k: v for k, v in options.items() if k in known}
        extra = {k: v for k, v in options.items() if k not in known}
        request = cls(messages=messages, **kwargs)
        if extra:
            # Preserve unrecognised options instead of silently dropping them.
            request.metadata = {**request.metadata, **extra}
        return request

    def to_dict(self) -> dict[str, Any]:
        """Diagnostic shape; current-turn tool protocol metadata is excluded."""
        return {
            "messages": [m.to_dict() for m in self.messages],
            "model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "stop": list(self.stop),
            "tools": self.tools,
            "response_format": self.response_format,
            "metadata": self.metadata,
        }


@dataclass
class ModelResponse:
    """A normalised successful generation result."""

    text: str
    model: str = ""
    provider: str = ""
    finish_reason: FinishReason = FinishReason.UNKNOWN
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Serialise for logs/state.

        ``raw`` is excluded by default: it is large and is provider-shaped, so
        it should not leak into task reports or checkpoints unasked.
        """
        payload: dict[str, Any] = {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "finish_reason": self.finish_reason.value,
            "usage": self.usage.to_dict(),
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "latency_ms": round(self.latency_ms, 3),
        }
        if include_raw:
            payload["raw"] = self.raw
        return payload
