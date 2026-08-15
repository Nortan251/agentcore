"""Tests for provider selection and configuration integration."""

from __future__ import annotations

import pytest

from agentcore.config.loader import load_config
from agentcore.config.schema import ModelConfig
from agentcore.errors import ConfigError
from agentcore.models.base import BaseModelProvider
from agentcore.models.errors import ModelConfigError
from agentcore.models.factory import (
    available_providers,
    create_provider,
    default_base_url,
    provider_from_config,
    register_provider,
)
from agentcore.models.messages import ModelRequest, ModelResponse
from agentcore.models.providers.echo import EchoProvider
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider


# -- selection --------------------------------------------------------------

def test_echo_provider_selected():
    provider = create_provider(ModelConfig(provider="echo", name="fake"))
    assert isinstance(provider, EchoProvider)


def test_openai_compatible_selected():
    provider = create_provider(ModelConfig(
        provider="openai_compatible", name="m",
        base_url="https://x.test/v1", api_key_env="K"))
    assert isinstance(provider, OpenAICompatibleProvider)


@pytest.mark.parametrize("alias", ["openai", "groq", "together", "openrouter",
                                   "ollama", "vllm"])
def test_aliases_resolve_with_default_base_url(alias):
    provider = create_provider(ModelConfig(provider=alias, name="m",
                                           api_key_env="K"))
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.config.base_url == default_base_url(alias)


def test_explicit_base_url_wins_over_default():
    provider = create_provider(ModelConfig(provider="openai", name="m",
                                           api_key_env="K",
                                           base_url="https://proxy.internal/v1"))
    assert provider.config.base_url == "https://proxy.internal/v1"


def test_provider_none_is_rejected():
    with pytest.raises(ModelConfigError):
        create_provider(ModelConfig())


def test_unknown_provider_lists_alternatives():
    with pytest.raises(ModelConfigError) as exc:
        create_provider(ModelConfig(provider="hal9000", name="m"))
    assert "echo" in exc.value.details["available"]


def test_provider_name_is_case_and_space_insensitive():
    assert isinstance(create_provider(ModelConfig(provider="  ECHO  ", name="m")),
                      EchoProvider)


def test_available_providers_sorted_and_includes_core():
    names = available_providers()
    assert names == sorted(names)
    assert {"echo", "openai_compatible"} <= set(names)


# -- registration -----------------------------------------------------------

def test_custom_provider_can_be_registered():
    class Custom(BaseModelProvider):
        name = "custom"

        def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(text="custom", provider=self.name)

    register_provider("custom_test", Custom)
    try:
        provider = create_provider(ModelConfig(provider="custom_test", name="m"))
        assert provider.complete("hi") == "custom"
    finally:
        from agentcore.models import factory
        factory._REGISTRY.pop("custom_test", None)


def test_duplicate_registration_rejected_unless_replacing():
    with pytest.raises(ModelConfigError):
        register_provider("echo", EchoProvider)
    register_provider("echo", EchoProvider, replace=True)  # explicit is fine


def test_empty_provider_name_rejected():
    with pytest.raises(ModelConfigError):
        register_provider("  ", EchoProvider)


# -- configuration integration ---------------------------------------------

def test_model_settings_load_from_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        """
[model]
provider = "groq"
name = "llama-3.1-8b-instant"
api_key_env = "MY_GROQ_KEY"
base_url = "https://api.groq.com/openai/v1"
temperature = 0.7
max_output_tokens = 512
timeout_seconds = 12.5
max_retries = 5
""", encoding="utf-8")
    cfg = load_config(path, env={})
    assert cfg.model.provider == "groq"
    assert cfg.model.base_url == "https://api.groq.com/openai/v1"
    assert cfg.model.max_retries == 5
    assert cfg.model.timeout_seconds == 12.5


def test_model_env_overrides_beat_file(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('[model]\nprovider = "echo"\nmax_retries = 2\n',
                    encoding="utf-8")
    cfg = load_config(path, env={
        "AGENTCORE_MODEL__PROVIDER": "openai",
        "AGENTCORE_MODEL__MAX_RETRIES": "7",
        "AGENTCORE_MODEL__BASE_URL": "https://env.test/v1",
    })
    assert cfg.model.provider == "openai"
    assert cfg.model.max_retries == 7
    assert cfg.model.base_url == "https://env.test/v1"


def test_invalid_model_settings_rejected():
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_MODEL__MAX_RETRIES": "0"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_MODEL__TIMEOUT_SECONDS": "0"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_MODEL__BASE_URL": "ftp://bad"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_MODEL__TEMPERATURE": "5"})


def test_defaults_unchanged_for_existing_projects():
    """Regression: adding fields must not alter existing default behaviour."""
    cfg = ModelConfig()
    assert cfg.provider == "none"
    assert cfg.base_url is None
    assert cfg.max_retries == 3
    cfg.validate()


def test_provider_from_config_uses_root_config():
    cfg = load_config(None, env={"AGENTCORE_MODEL__PROVIDER": "echo",
                                 "AGENTCORE_MODEL__NAME": "m"})
    assert isinstance(provider_from_config(cfg), EchoProvider)


# -- echo provider ----------------------------------------------------------

def test_echo_provider_needs_no_credential():
    provider = EchoProvider(ModelConfig(provider="echo", name="m"))
    assert provider.complete("hello") == "echo: hello"
    assert provider.describe()["credential_configured"] is False


def test_echo_provider_records_requests_and_usage():
    provider = EchoProvider(ModelConfig(provider="echo", name="m"))
    response = provider.generate(ModelRequest.from_prompt("hi there",
                                                          system="sys"))
    assert response.text == "echo: hi there"
    assert response.usage.total_tokens > 0
    assert len(provider.calls) == 1
