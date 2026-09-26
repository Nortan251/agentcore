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
from agentcore.models.messages import FinishReason, Message, ModelRequest, Role, ToolCall
from agentcore.models.providers.echo import EchoProvider
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
from agentcore.models.transport import HttpResponse, RetryPolicy

API_KEY_ENV = "TEST_MODEL_KEY"
SECRET = "sk-test-abcdef-supersecret"


def test_pr31_browser_catalogue_has_exact_function_wire_shape():
    from agentcore.browser import BrowserConfig, BrowserSession, browser_tools
    from test_browser_session import FakeBackend
    session = BrowserSession(FakeBackend(), config=BrowserConfig(enabled=True))
    neutral = [tool.spec.to_dict() for tool in browser_tools(session)]
    request = ModelRequest.from_prompt("Read", tools=neutral)
    payload = build(FakeTransport()).build_payload(request)
    expected = []
    for tool in neutral:
        properties = ({"url": {"type": "string", "description": "",
                               "maxLength": session.config.max_url_chars}}
                      if tool["name"] == "browser_open_url" else {})
        expected.append({"type": "function", "function": {
            "name": tool["name"], "description": tool["description"],
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}})
    assert payload["tools"] == expected
    assert request.tools is neutral and "capabilities" in neutral[0]
    session.close()


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


class StrictToolTransport(FakeTransport):
    """Local OpenAI server surrogate that rejects orphan/misordered tool wire."""

    def post_json(self, url, payload, headers, timeout):
        pending = []
        for message in payload["messages"]:
            if message["role"] == "assistant" and message.get("tool_calls"):
                assert not pending
                calls = message["tool_calls"]
                pending = [call["id"] for call in calls]
                assert len(set(pending)) == len(pending)
                assert all(call["type"] == "function" and
                           type(call["function"]["name"]) is str and
                           type(json.loads(call["function"]["arguments"])) is dict
                           for call in calls)
            elif message["role"] == "tool":
                assert pending and message["tool_call_id"] == pending.pop(0)
            else:
                assert not pending
        assert not pending
        return super().post_json(url, payload, headers, timeout)


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


def test_neutral_tools_translated_and_response_format_forwarded_when_present():
    from agentcore.tools.base import tool_spec
    transport = FakeTransport([http(200, ok_body())])
    provider = build(transport)
    provider.generate(ModelRequest(messages=[Message.user("hi")],
                                   tools=[tool_spec("echo", "Echo").to_dict()],
                                   response_format={"type": "json_object"}))
    payload = transport.requests[0]["payload"]
    assert payload["tools"] == [{"type": "function", "function": {
        "name": "echo", "description": "Echo", "parameters": {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False}}}]
    assert payload["response_format"] == {"type": "json_object"}


def test_pr31_generic_parameter_schema_and_neutral_provider_compatibility():
    from agentcore.tools.base import ParamType, ToolParameter, tool_spec
    neutral = tool_spec("all_types", "Contract", parameters=(
        ToolParameter("text", description="Text", required=False, default="ab", choices=("ab", "ac"),
                      max_length=3, pattern="[a-z]+"),
        ToolParameter("count", ParamType.INTEGER, minimum=1, maximum=5),
        ToolParameter("ratio", ParamType.NUMBER, required=False, default=0.5, minimum=0, maximum=1),
        ToolParameter("flag", ParamType.BOOLEAN), ToolParameter("items", ParamType.ARRAY),
        ToolParameter("data", ParamType.OBJECT))).to_dict()
    request = ModelRequest.from_prompt("Test", tools=[neutral])
    schema = build(FakeTransport()).build_payload(request)["tools"][0]["function"]["parameters"]
    assert schema == {"type": "object", "additionalProperties": False,
        "required": ["count", "flag", "items", "data"], "properties": {
        "text": {"type": "string", "description": "Text", "default": "ab", "enum": ["ab", "ac"],
                 "maxLength": 3, "pattern": r"^(?:[a-z]+)(?![\s\S])"},
        "count": {"type": "integer", "description": "", "minimum": 1, "maximum": 5},
        "ratio": {"type": "number", "description": "", "default": 0.5, "minimum": 0, "maximum": 1},
        "flag": {"type": "boolean", "description": ""},
        "items": {"type": "array", "description": "", "items": {}},
        "data": {"type": "object", "description": ""}}}
    import re
    assert re.fullmatch("[a-z]+", "ab\n") is None
    assert re.search(schema["properties"]["text"]["pattern"], "ab\n") is None
    echo = EchoProvider()
    echo.generate(request)
    assert echo.calls == [request] and request.tools == [neutral]


@pytest.mark.parametrize("patch", [
    {"type": "unsupported"}, {"type": []}, {"required": "false"}, {"description": 5},
    {"choices": "abc"}, {"choices": [1]}, {"max_length": -1}, {"max_length": True},
    {"minimum": 1}, {"maximum": float("inf")}, {"pattern": "["},
    {"pattern": r"\w+"}, {"pattern": "(?i)text"}, {"pattern": "a++"},
    {"pattern": r"\Atext\Z"}, {"pattern": "é+"}, {"items": {"type": "string"}},
    {"default": 123}, {"max_length": 1, "default": "long"},
    {"type": "number", "minimum": 4, "maximum": 3},
    {"type": "number", "minimum": 10**1000},
])
def test_pr31_malformed_parameter_contract_refused_before_provider_io(patch):
    param = {"name": "p", "type": "string", "required": True, **patch}
    transport = FakeTransport()
    with pytest.raises(ModelConfigError) as error:
        build(transport).generate(ModelRequest.from_prompt("Test", tools=[{
            "name": "test", "description": "Test", "parameters": [param]}]))
    assert str(error.value) == "Unsupported or malformed neutral tool contract."
    assert not transport.requests


@pytest.mark.parametrize("contract", [
    {"type": "function"}, {"name": "x", "parameters": {}}, {"name": "x", "description": []},
    {"name": "x", "parameters": [{"name": "p", "type": "string"}]*2},
    {"name": "x", "parameters": [None]}, {"name": "x", "unexpected_schema": {}},
])
def test_pr31_malformed_tool_contract_refused(contract):
    with pytest.raises(ModelConfigError):
        build(FakeTransport()).build_payload(ModelRequest.from_prompt("Test", tools=[contract]))


def test_pr31_browser_provider_fake_http_e2e_through_exact_executor(monkeypatch):
    from agentcore.browser import BrowserAgentComposition, BrowserConfig
    from agentcore.browser.composition import BROWSER_TOOL_NAMES
    from agentcore.conversation.tool_use import ConversationToolRunner
    from test_browser_session import FakeBackend
    call_body = ok_body("")
    call_body["choices"][0].update(message={"role": "assistant", "content": "", "tool_calls": [{
        "id": "read_one", "type": "function", "function": {"name": "browser_extract_text", "arguments": "{}"}}]},
        finish_reason="tool_calls")
    class ValidatingTransport(StrictToolTransport):
        def post_json(self, url, payload, headers, timeout):
            assert url.endswith("/chat/completions")
            assert {tool["function"]["name"] for tool in payload["tools"]} == set(BROWSER_TOOL_NAMES)
            for tool in payload["tools"]:
                assert set(tool) == {"type", "function"} and tool["type"] == "function"
                assert set(tool["function"]) == {"name", "description", "parameters"}
                schema = tool["function"]["parameters"]
                assert schema["type"] == "object" and schema["additionalProperties"] is False
                assert type(schema["properties"]) is dict and type(schema["required"]) is list
            return super().post_json(url, payload, headers, timeout)
    transport = ValidatingTransport([http(200, call_body), http(200, ok_body("Read data"))])
    with BrowserAgentComposition(model=build(transport), config=BrowserConfig(enabled=True),
                                _backend=FakeBackend()) as composition:
        assert type(composition._runner._runner) is ConversationToolRunner
        assert composition._runner._runner.executor is composition.executor
        invoked = []
        original = composition.executor.invoke
        def spy(name, args=None, **kwargs):
            invoked.append((name, args))
            return original(name, args, **kwargs)
        monkeypatch.setattr(composition.executor, "invoke", spy)
        result = composition.conversation().send("Read page")
        assert result.ok and result.tools_executed == 1
        assert invoked == [("browser_extract_text", {})]
    assert len(transport.requests) == 2
    messages = transport.requests[1]["payload"]["messages"]
    assert any(m["role"] == "tool" and "UNTRUSTED_DATA" in m["content"] for m in messages)
    assistant = next(m for m in messages if m["role"] == "assistant" and m.get("tool_calls"))
    tool = next(m for m in messages if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "read_one"
    assert tool["tool_call_id"] == "read_one"


def test_pr31_strict_browser_transport_preserves_two_calls_and_two_rounds():
    from agentcore.browser import BrowserAgentComposition, BrowserConfig
    from test_browser_session import FakeBackend

    def call_body(*calls):
        body = ok_body("")
        body["choices"][0].update(message={"role": "assistant", "content": "",
            "tool_calls": [{"id": identifier, "type": "function", "function": {
                "name": name, "arguments": "{}"}} for identifier, name in calls]},
            finish_reason="tool_calls")
        return http(200, body)

    transport = StrictToolTransport([
        call_body(("one", "browser_current_page"), ("two", "browser_extract_text")),
        call_body(("three", "browser_list_links")),
        http(200, ok_body("done")),
    ])
    with BrowserAgentComposition(model=build(transport), config=BrowserConfig(enabled=True),
                                 _backend=FakeBackend()) as composition:
        result = composition.conversation().send("Read")
        assert result.ok and result.reply.content == "done" and result.tools_executed == 3
    assert len(transport.requests) == 3
    last = transport.requests[-1]["payload"]["messages"]
    assert [m["tool_call_id"] for m in last if m["role"] == "tool"] == ["one", "two", "three"]
    assert [[c["id"] for c in m["tool_calls"]] for m in last if m.get("tool_calls")] == [
        ["one", "two"], ["three"]]


def test_pr31_strict_browser_resume_reframes_historical_tool_data(tmp_path):
    from agentcore.browser import BrowserAgentComposition, BrowserConfig
    from test_browser_session import FakeBackend

    call_body = ok_body("")
    call_body["choices"][0].update(message={"role": "assistant", "content": "",
        "tool_calls": [{"id": "current-only-id", "type": "function", "function": {
            "name": "browser_open_url",
            "arguments": '{"url":"https://example.com/page?private=argument"}'}}]},
        finish_reason="tool_calls")
    first_wire = StrictToolTransport([http(200, call_body), http(200, ok_body("read"))])
    directory = tmp_path / "browser-sessions"
    with BrowserAgentComposition(model=build(first_wire), config=BrowserConfig(enabled=True),
                                 sessions_directory=directory, _backend=FakeBackend()) as first:
        initial = first.conversation().send("Open")
        assert initial.ok
        session_id = initial.session.id
    saved = (directory / (session_id + ".json")).read_text(encoding="utf-8")
    assert "private=argument" not in saved and "current-only-id" not in saved
    assert '"tool_calls"' not in saved
    second_wire = StrictToolTransport([http(200, ok_body("resumed"))])
    with BrowserAgentComposition(model=build(second_wire), config=BrowserConfig(enabled=True),
                                 sessions_directory=directory, _backend=FakeBackend()) as second:
        resumed = second.conversation().send("Resume", session_id=session_id)
        assert resumed.ok and resumed.reply.content == "resumed"
    messages = second_wire.requests[0]["payload"]["messages"]
    assert not any(m["role"] == "tool" for m in messages)
    assert any("HISTORICAL TOOL RESULT" in m["content"] for m in messages
               if m["role"] == "user")


def test_pr31_current_tool_result_is_not_replayed_as_history_when_session_trims():
    from agentcore.browser import BrowserAgentComposition, BrowserConfig
    from agentcore.conversation.config import ConversationConfig
    from test_browser_session import FakeBackend

    call_body = ok_body("")
    call_body["choices"][0].update(message={"role": "assistant", "content": "",
        "tool_calls": [{"id": "trimmed-id", "type": "function", "function": {
            "name": "browser_extract_text", "arguments": "{}"}}]}, finish_reason="tool_calls")
    transport = StrictToolTransport([http(200, call_body), http(200, ok_body("done"))])
    with BrowserAgentComposition(model=build(transport), config=BrowserConfig(enabled=True),
            conversation_config=ConversationConfig(max_context_messages=1,
                                                   max_session_messages=1),
            _backend=FakeBackend()) as composition:
        result = composition.conversation().send("Read")
        assert result.ok and result.reply.content == "done"
    messages = transport.requests[1]["payload"]["messages"]
    assert len([m for m in messages if m["role"] == "tool"]) == 1
    assert not any("HISTORICAL TOOL RESULT" in m["content"] for m in messages)


@pytest.mark.parametrize("messages", [
    [Message.user("x"), Message(Role.TOOL, "result", tool_call_id="orphan")],
    [Message.user("x"), Message(Role.ASSISTANT, "", tool_calls=(ToolCall("same", "a"),
        ToolCall("same", "b"))), Message(Role.TOOL, "one", tool_call_id="same"),
        Message(Role.TOOL, "two", tool_call_id="same")],
    [Message.user("x"), Message(Role.ASSISTANT, "", tool_calls=(ToolCall("one", "a"),
        ToolCall("two", "b"))), Message(Role.TOOL, "two", tool_call_id="two"),
        Message(Role.TOOL, "one", tool_call_id="one")],
    [Message.user("x"), Message(Role.ASSISTANT, "", tool_calls=(ToolCall("x" * 257, "a"),)),
        Message(Role.TOOL, "result", tool_call_id="x" * 257)],
])
def test_pr31_malformed_tool_protocol_fails_before_provider_io(messages):
    transport = StrictToolTransport()
    with pytest.raises(ModelConfigError) as error:
        build(transport).generate(ModelRequest(messages=messages))
    assert not transport.requests
    assert "same" not in str(error.value) and "orphan" not in str(error.value)


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

@pytest.mark.parametrize('field', ['choices', 'default'])
@pytest.mark.parametrize('kind,value', [
    ('array', (1, 2)), ('array', [(1, 2)]), ('object', {1: 'value'}),
    ('object', {'nested': (1, 2)}), ('array', [float('nan')]),
    ('array', [float('inf')]), ('object', {'x': float('-inf')}),
    ('array', [{'nested': {False: 1}}]), ('object', {'x': {1, 2}}),
])
def test_pr31_p2_non_native_choice_default_refused_before_io(field, kind, value):
    transport = FakeTransport()
    param = {'name': 'p', 'type': kind, field: [value] if field == 'choices' else value}
    with pytest.raises(ModelConfigError) as error:
        build(transport).generate(ModelRequest.from_prompt('Test', tools=[{
            'name': 'test', 'parameters': [param]}]))
    assert str(error.value) == 'Unsupported or malformed neutral tool contract.'
    assert transport.requests == []

@pytest.mark.parametrize('kind,value', [
    ('array', [1, True, None, 1.0, {'x': ['s', False]}]),
    ('object', {'x': [1, True, None, {'nested': 0.5}]}),
])
def test_pr31_p2_native_enum_default_matches_decoded_local_validation(kind, value):
    from agentcore.tools.base import FunctionTool, ToolParameter, ParamType, tool_spec, ToolResult
    parameter = ToolParameter('p', ParamType(kind), choices=(value,), default=value, required=False)
    tool = FunctionTool(tool_spec('native', 'Fixture', parameters=(parameter,)), lambda *_: ToolResult.success())
    schema = build(FakeTransport()).build_payload(ModelRequest.from_prompt('Test', tools=[tool.spec.to_dict()]))['tools'][0]['function']['parameters']['properties']['p']
    decoded = json.loads(json.dumps(schema))
    assert decoded['enum'] == [value] and decoded['default'] == value
    assert tool.validate_args({'p': decoded['enum'][0]})['p'] == value
    assert tool.validate_args({'p': decoded['default']})['p'] == value
    assert type(decoded['enum'][0]) is type(value)

@pytest.mark.parametrize('field', ['choices', 'default'])
def test_pr31_p2_recursive_values_fail_categorically_before_io(field):
    value = []
    value.append(value)
    transport = FakeTransport()
    param = {'name': 'p', 'type': 'array', field: [value] if field == 'choices' else value}
    with pytest.raises(ModelConfigError):
        build(transport).generate(ModelRequest.from_prompt('Test', tools=[{'name': 'test', 'parameters': [param]}]))
    assert transport.requests == []
