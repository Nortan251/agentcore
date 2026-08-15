"""Provider registry and factory.

Maps the existing ``model.provider`` configuration string onto a concrete
implementation. New providers register here; nothing else in the project needs
to change, which is what keeps the provider replaceable.
"""

from __future__ import annotations

from typing import Any, Callable

from agentcore.config.schema import AgentConfig, ModelConfig
from agentcore.models.base import BaseModelProvider
from agentcore.models.errors import ModelConfigError
from agentcore.models.providers.echo import EchoProvider
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider

#: Configuration value -> constructor.
ProviderFactory = Callable[..., BaseModelProvider]

_REGISTRY: dict[str, ProviderFactory] = {
    "echo": EchoProvider,
    "openai_compatible": OpenAICompatibleProvider,
    # Convenience aliases: all speak the OpenAI chat-completions shape and
    # differ only by base_url/model in configuration.
    "openai": OpenAICompatibleProvider,
    "azure_openai": OpenAICompatibleProvider,
    "groq": OpenAICompatibleProvider,
    "together": OpenAICompatibleProvider,
    "openrouter": OpenAICompatibleProvider,
    "ollama": OpenAICompatibleProvider,
    "vllm": OpenAICompatibleProvider,
    "local": OpenAICompatibleProvider,
}

#: Default endpoints for aliases whose URL is well-known, applied only when
#: configuration does not specify ``base_url``.
_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "vllm": "http://localhost:8000/v1",
}


def register_provider(name: str, factory: ProviderFactory, *,
                      replace: bool = False) -> None:
    """Add a provider implementation under ``name``."""
    key = name.strip().lower()
    if not key:
        raise ModelConfigError("provider name must not be empty")
    if key in _REGISTRY and not replace:
        raise ModelConfigError("provider already registered", provider=key)
    _REGISTRY[key] = factory


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def default_base_url(provider: str) -> str | None:
    return _DEFAULT_BASE_URLS.get(provider.strip().lower())


def create_provider(config: ModelConfig, **kwargs: Any) -> BaseModelProvider:
    """Instantiate the provider named by ``config.provider``.

    ``kwargs`` are forwarded to the constructor (e.g. an injected transport),
    which is how tests avoid the network.
    """
    key = (config.provider or "").strip().lower()
    if not key or key == "none":
        raise ModelConfigError(
            "no model provider configured; set model.provider",
            available=available_providers())
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ModelConfigError("unknown model provider", provider=key,
                               available=available_providers())

    # Apply a well-known endpoint when the alias implies one and none was set.
    if not config.base_url:
        fallback = _DEFAULT_BASE_URLS.get(key)
        if fallback:
            config = replace_base_url(config, fallback)

    return factory(config, **kwargs)


def replace_base_url(config: ModelConfig, base_url: str) -> ModelConfig:
    """Return a copy of ``config`` with ``base_url`` set (config is frozen)."""
    import dataclasses

    return dataclasses.replace(config, base_url=base_url)


def provider_from_config(config: AgentConfig, **kwargs: Any) -> BaseModelProvider:
    """Convenience wrapper for building a provider from the root config."""
    return create_provider(config.model, **kwargs)
