"""Base class for concrete model providers.

Separation of concerns inside the model layer:

* :class:`BaseModelProvider` — shared contract, defaults, and the bridge from
  the project's existing ``complete(prompt, ...) -> str`` Protocol to the
  richer :meth:`generate` call.
* subclasses — request construction and response normalisation for one API
  shape.
* :mod:`agentcore.models.transport` — HTTP, retries, auth headers.

Subclasses implement :meth:`generate` only; ``complete()`` comes for free, so
every provider automatically satisfies ``core.interfaces.ModelProvider``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agentcore.config.schema import ModelConfig
from agentcore.models.messages import ModelRequest, ModelResponse


class BaseModelProvider(ABC):
    """Common behaviour for all providers."""

    #: Short provider identifier, e.g. ``"openai_compatible"``.
    name: str = "base"

    def __init__(self, config: ModelConfig | None = None) -> None:
        self.config = config or ModelConfig()

    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Execute a request and return a normalised response.

        Implementations must raise a :class:`agentcore.models.errors.ModelError`
        subclass on failure — never return a partial or empty response.
        """

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        """Satisfy the existing ``ModelProvider`` Protocol.

        Deliberately returns plain ``str`` so the established core contract is
        preserved. Callers needing usage, finish reason or tool calls should
        use :meth:`generate` directly.
        """
        return self.generate(
            ModelRequest.from_prompt(prompt, system=system, **options)).text

    def describe(self) -> dict[str, Any]:
        """Non-sensitive provider summary, safe to log or expose via the CLI."""
        return {
            "provider": self.name,
            "model": self.config.name,
            "timeout_seconds": self.config.timeout_seconds,
            "credential_configured": bool(self.config.resolve_api_key()),
            "api_key_env": self.config.api_key_env,  # variable NAME, not value
        }
