"""Offline provider used for development, tests and CLI diagnostics.

Performs no network I/O and needs no credential, so the model layer can be
exercised end to end in a sandbox. It is not a mock inside the test suite — it
is a legitimate configuration (``provider = "echo"``) for running the agent
without a paid API.
"""

from __future__ import annotations

from typing import Any

from agentcore.config.schema import ModelConfig
from agentcore.models.base import BaseModelProvider
from agentcore.models.messages import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    Usage,
)


class EchoProvider(BaseModelProvider):
    """Returns a deterministic transformation of the final user message."""

    name = "echo"

    def __init__(self, config: ModelConfig | None = None, *,
                 prefix: str = "echo: ") -> None:
        super().__init__(config)
        self.prefix = prefix
        self.calls: list[ModelRequest] = []

    def generate(self, request: ModelRequest, *,
                 context: Any | None = None) -> ModelResponse:
        # ``context`` is accepted for API compatibility (Task 42.4b) but
        # intentionally ignored: the echo provider performs no I/O and
        # therefore has no transport timeout to bound. Deadline truth
        # remains owned by the realtime fence, which will reject a late
        # echo result if the turn has already expired.
        _ = context
        self.calls.append(request)
        last_user = next(
            (m for m in reversed(request.messages) if m.role is Role.USER), None)
        source: Message | None = last_user or (
            request.messages[-1] if request.messages else None)
        text = f"{self.prefix}{source.content if source else ''}"

        words = sum(len(m.content.split()) for m in request.messages)
        return ModelResponse(
            text=text,
            model=request.model or self.config.name,
            provider=self.name,
            finish_reason=FinishReason.STOP,
            usage=Usage(prompt_tokens=words, completion_tokens=len(text.split()),
                        total_tokens=words + len(text.split())),
        )
