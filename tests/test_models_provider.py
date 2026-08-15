"""Tests for the OpenAI-compatible provider.

No test performs network I/O: a fake transport is injected everywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

import pytest

from agentcore.config.schema import ModelConfig
from agentcore.core.interfaces import ModelProvider
from agentcore.errors import AgentError
from agentcore.models.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelConnectionError,
    ModelError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
)
from agentcore.models.messages import FinishReason, Message, ModelRequest
from agentcore.models.providers.echo import EchoProvider
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
from agentcore.models.transport import HttpResponse, RetryPolicy

API_KEY_ENV = "TEST_MODEL_KEY"
SECRET = "sk-test-abcdef-supersecret"


def make_config(**overrides: Any) -> ModelConfig:
    base = dict(provider="openai_compatible", name="test-model",
                api_key_env=API_KEY_ENV, base_url="https://api.example.com/v1",
                temperature=0.3, max_output_tokens=128, timeout_seconds=5.0,
                max_retries=3)
    base.update(overrides)
    return ModelConfig(**base)


def ok_body(text: str = "hello there", **extra: Any) -> dict[str, Any]:
    payload = {
        "id": "cmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15},
    }
    payload.update(extra)
    return payload


@dataclass
class FakeTransport:
    """Records requests and replays queued responses or raises queued errors."""

    responses: list[Any] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.requests.append({"url": url, "payload": dict(payload),
                              "headers": dict(headers), "timeout": timeout})
        if not self.responses:
            raise AssertionError("FakeTransport ran out of queued responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http(status: int, body: Any, headers: dict[str, str] | None = None
         ) -> HttpResponse:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HttpResponse(status=status, body=raw, headers=headers or {})


@pytest.fixture(autouse=True)
def _credential(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, SECRET)


def build(transport: FakeTransport, **cfg: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        make_config(**cfg), transport=transport,
        retry=RetryPolicy(max_attempts=cfg.pop("max_attempts", 1),
                          initial_backoff=0, jitter=False))


# -- contract ---------------------------------------------------------------

def test_provider_satisfies_the_existing_protocol():
    provider = build(FakeTransport([http(200, ok_body())]))
    assert isinstance(provider, ModelProvider)
    assert isinstance(EchoProvider(), ModelProvider)


def test_complete_returns_plain_string():
    provider = build(FakeTransport([http(200, ok_body("the answer"))]))
    result = provider.complete("question", system="be terse")
    assert result == "the answer"
    assert isinstance(result, str)


def test_model_errors_derive_from_agent_error():
    """Reuses the project's hierarchy rather than a parallel one."""
    for cls in (ModelConfigError, ModelAuthError, ModelRateLimitError,
                ModelTimeoutError, ModelConnectionError, ModelResponseError,
                ModelProviderError):
        assert issubclass(cls, ModelError)
        assert issubclass(cls, AgentError)


# -- configuration ----------------------------------------------------------

def test_missing_model_name_is_rejected():
    with pytest.raises(ModelConfigError):
        OpenAICompatibleProvider(make_config(name="none"))


def test_missing_base_url_is_rejected():
    with pytest.raises(ModelConfigError):
        OpenAICompatibleProvider(make_config(base_url=None))


def test_endpoint_built_from_base_url():
    provider = build(FakeTransport(), base_url="https://x.test/v1/")
    assert provider.endpoint == "https://x.test/v1/chat/completions"


def test_describe_is_safe_and_reports_credential_presence():
    info = build(FakeTransport()).describe()
    assert info["provider"] == "openai_compatible"
    assert info["credential_configured"] is True
    assert info["api_key_env"] == API_KEY_ENV
    assert SECRET not in json.dumps(info)


# -- authentication ---------------------------------------------------------

def test_missing_credential_raises_auth_error(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    provider = build(FakeTransport([http(200, ok_body())]))
    with pytest.raises(ModelAuthError) as exc:
        provider.complete("hi")
    # names the variable to set, but never a value
    assert API_KEY_ENV in str(exc.value)
    assert not exc.value.retryable


def test_credential_sent_as_bearer_header():
    transport = FakeTransport([http(200, ok_body())])
    build(transport).complete("hi")
    assert transport.requests[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_no_credential_lookup_uses_config_indirection(monkeypatch):
    """The key must come from the env var named by api_key_env."""
    monkeypatch.setenv("OTHER_VAR", "other-secret")
    transport = FakeTransport([http(200, ok_body())])
    build(transport, api_key_env="OTHER_VAR").complete("hi")
    assert transport.requests[0]["headers"]["Authorization"] == "Bearer other-secret"


def test_auth_failure_status_is_classified():
    provider = build(FakeTransport([
        http(401, {"error": {"message": "Invalid Authentication"}})]))
    with pytest.raises(ModelAuthError):
        provider.complete("hi")


def test_forbidden_status_is_auth_error():
    provider = build(FakeTransport([http(403, {"error": {"message": "no access"}})]))
    with pytest.raises(ModelAuthError):
        provider.complete("hi")


# -- request construction ---------------------------------------------------

def test_request_uses_configured_model_and_parameters():
    transport = FakeTransport([http(200, ok_body())])
    build(transport).complete("hi")
    payload = transport.requests[0]["payload"]
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0.3
    assert payload["max_tokens"] == 128
    assert transport.requests[0]["timeout"] == 5.0
    assert transport.requests[0]["url"] == "https://api.example.com/v1/chat/completions"


def test_request_message_structure():
    transport = FakeTransport([http(200, ok_body())])
    build(transport).complete("do the thing", system="you are terse")
    messages = transport.requests[0]["payload"]["messages"]
    assert messages == [
        {"role": "system", "content": "you are terse"},
        {"role": "user", "content": "do the thing"},
    ]


def test_per_request_values_override_config():
    transport = FakeTransport([http(200, ok_body())])
    provider = build(transport)
    provider.generate(ModelRequest(messages=[Message.user("hi")],
                                   model="other-model", temperature=1.5,
                                   max_output_tokens=7, stop=("STOP",)))
    payload = transport.requests[0]["payload"]
    assert payload["model"] == "other-model"
    assert payload["temperature"] == 1.5
    assert payload["max_tokens"] == 7
    assert payload["stop"] == ["STOP"]


def test_optional_fields_omitted_when_unset():
    transport = FakeTransport([http(200, ok_body())])
    build(transport).complete("hi")
    payload = transport.requests[0]["payload"]
    assert "stop" not in payload
    assert "tools" not in payload
    assert "response_format" not in payload


def test_tools_and_response_format_forwarded_when_present():
    transport = FakeTransport([http(200, ok_body())])
    provider = build(transport)
    provider.generate(ModelRequest(messages=[Message.user("hi")],
                                   tools=[{"type": "function"}],
                                   response_format={"type": "json_object"}))
    payload = transport.requests[0]["payload"]
    assert payload["tools"] == [{"type": "function"}]
    assert payload["response_format"] == {"type": "json_object"}


# -- response handling ------------------------------------------------------

def test_successful_response_is_normalised():
    provider = build(FakeTransport([http(200, ok_body("result text"))]))
    resp = provider.generate(ModelRequest.from_prompt("hi"))
    assert resp.text == "result text"
    assert resp.model == "test-model"
    assert resp.provider == "openai_compatible"
    assert resp.finish_reason is FinishReason.STOP
    assert resp.usage.total_tokens == 15
    assert resp.latency_ms >= 0


def test_length_finish_reason_mapped():
    body = ok_body()
    body["choices"][0]["finish_reason"] = "length"
    provider = build(FakeTransport([http(200, body)]))
    assert provider.generate(
        ModelRequest.from_prompt("hi")).finish_reason is FinishReason.LENGTH


def test_unknown_finish_reason_maps_to_unknown():
    body = ok_body()
    body["choices"][0]["finish_reason"] = "wat"
    provider = build(FakeTransport([http(200, body)]))
    assert provider.generate(
        ModelRequest.from_prompt("hi")).finish_reason is FinishReason.UNKNOWN


def test_malformed_json_raises_response_error():
    provider = build(FakeTransport([http(200, b"not json at all")]))
    with pytest.raises(ModelResponseError):
        provider.complete("hi")


def test_non_object_response_raises():
    provider = build(FakeTransport([http(200, [1, 2, 3])]))
    with pytest.raises(ModelResponseError):
        provider.complete("hi")


def test_missing_choices_raises():
    provider = build(FakeTransport([http(200, {"model": "m"})]))
    with pytest.raises(ModelResponseError):
        provider.complete("hi")


def test_empty_choices_raises():
    provider = build(FakeTransport([http(200, {"choices": []})]))
    with pytest.raises(ModelResponseError):
        provider.complete("hi")


def test_missing_message_raises():
    provider = build(FakeTransport([http(200, {"choices": [{"index": 0}]})]))
    with pytest.raises(ModelResponseError):
        provider.complete("hi")


def test_empty_content_raises():
    body = ok_body("")
    provider = build(FakeTransport([http(200, body)]))
    with pytest.raises(ModelResponseError) as exc:
        provider.complete("hi")
    assert "empty" in exc.value.message


def test_whitespace_only_content_raises():
    provider = build(FakeTransport([http(200, ok_body("   \n  "))]))
    with pytest.raises(ModelResponseError):
        provider.complete("hi")


def test_tool_calls_are_preserved_without_content():
    """Empty text is valid when the model returned tool calls."""
    body = {
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": "c1", "type": "function",
                                                 "function": {"name": "echo",
                                                              "arguments":
                                                                  '{"message": "x"}'}}]},
                     "finish_reason": "tool_calls"}],
    }
    provider = build(FakeTransport([http(200, body)]))
    resp = provider.generate(ModelRequest.from_prompt("hi"))
    assert resp.finish_reason is FinishReason.TOOL_CALLS
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "echo"
    assert resp.tool_calls[0].arguments == {"message": "x"}


def test_malformed_tool_call_arguments_fail_loudly():
    """A tool call is an instruction to act, so a half-understood one is
    refused. Previously invalid JSON was wrapped as ``{"_raw": ...}``, which
    handed the conversation layer an apparently-valid call built from
    unparseable data."""
    body = ok_body("text")
    body["choices"][0]["message"]["tool_calls"] = [
        {"id": "c1", "function": {"name": "t", "arguments": "{not json"}},
    ]
    provider = build(FakeTransport([http(200, body)]))
    with pytest.raises(ModelResponseError) as exc:
        provider.generate(ModelRequest.from_prompt("hi"))
    assert "not valid JSON" in exc.value.message
    # The unparseable text is model-generated; it must not be echoed back.
    assert "{not json" not in json.dumps(exc.value.details)


@pytest.mark.parametrize("entry", [
    "garbage",                                   # not an object
    {"id": "c", "function": {}},                 # no name
    {"id": "c", "function": {"name": ""}},       # blank name
    {"id": "c"},                                 # no function object
])
def test_malformed_tool_call_shapes_fail_loudly(entry):
    """Skipping a malformed entry would leave the model expecting a result
    for a call that never happened."""
    body = ok_body("text")
    body["choices"][0]["message"]["tool_calls"] = [entry]
    provider = build(FakeTransport([http(200, body)]))
    with pytest.raises(ModelResponseError):
        provider.generate(ModelRequest.from_prompt("hi"))


def test_provider_error_status():
    provider = build(FakeTransport([http(500, {"error": {"message": "boom"}})]))
    with pytest.raises(ModelProviderError) as exc:
        provider.complete("hi")
    assert exc.value.status_code == 500
    assert exc.value.retryable is True


def test_client_error_status_is_not_retryable():
    provider = build(FakeTransport([http(400, {"error": {"message": "bad request"}})]))
    with pytest.raises(ModelProviderError) as exc:
        provider.complete("hi")
    assert exc.value.retryable is False


# -- network failures -------------------------------------------------------

def test_timeout_propagates():
    provider = build(FakeTransport([ModelTimeoutError("timed out")]))
    with pytest.raises(ModelTimeoutError):
        provider.complete("hi")


def test_connection_failure_propagates():
    provider = build(FakeTransport([ModelConnectionError("no route")]))
    with pytest.raises(ModelConnectionError):
        provider.complete("hi")


def test_rate_limit_raises_with_retry_after():
    provider = build(FakeTransport([
        http(429, {"error": {"message": "slow down"}}, {"retry-after": "2.5"})]))
    with pytest.raises(ModelRateLimitError) as exc:
        provider.complete("hi")
    assert exc.value.retry_after == 2.5
    assert exc.value.retryable is True


def test_gateway_timeout_is_timeout_error():
    provider = build(FakeTransport([http(504, {"error": {"message": "gw"}})]))
    with pytest.raises(ModelTimeoutError):
        provider.complete("hi")


# -- secret safety ----------------------------------------------------------

def test_secret_never_appears_in_any_error(monkeypatch):
    """Every failure mode must be safe to log."""
    failures = [
        http(401, {"error": {"message": f"bad key {SECRET}"}}),
        http(429, {"error": {"message": SECRET}}),
        http(500, {"error": {"message": SECRET}}),
        http(200, b"garbage"),
        http(400, SECRET.encode()),
    ]
    for failure in failures:
        provider = build(FakeTransport([failure]))
        with pytest.raises(ModelError) as exc:
            provider.complete("hi")
        blob = json.dumps(exc.value.to_dict()) + str(exc.value)
        assert SECRET not in blob, f"secret leaked for {failure.status}"
