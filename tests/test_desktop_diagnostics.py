"""Offline provider diagnostics, safe launcher errors and native authority."""

from dataclasses import replace
import io
import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from agentcore import cli
from agentcore.config.schema import AgentConfig, ModelConfig, ToolConfig
from agentcore.conversation.config import ConversationConfig
from agentcore.desktop import diagnostics as diag
from agentcore.integrations.computer_control import ComputerControlAdapter
from agentcore.models.errors import (
    ModelAuthError, ModelConfigError, ModelConnectionError, ModelProviderError,
    ModelRateLimitError, ModelResponseError, ModelTimeoutError, model_error_category,
)
from agentcore.models.transport import UrllibTransport
from agentcore.permissions.config import PermissionConfig


SECRET = "TEST_CREDENTIAL_MUST_NEVER_APPEAR"


def config():
    return AgentConfig(model=ModelConfig(provider="openai_compatible", name="z-ai/glm-5.3-flash",
        base_url=diag.ENDPOINT, api_key_env="NVIDIA_API_KEY", timeout_seconds=120,
        max_output_tokens=4096, max_retries=1),
        conversation=ConversationConfig(tools_enabled=True),
        tools=ToolConfig(enabled=("get_foreground_app",)))


class Stream:
    def __init__(self, body, status=200):
        self.status = status
        self.body = io.BytesIO(body)
        self.closed = False
    def read(self, size): return self.body.read(size)
    def close(self): self.closed = True


def response(*, text="OK", finish="stop", calls=None, reasoning=None):
    message = {"role": "assistant", "content": text}
    if calls is not None: message["tool_calls"] = calls
    if reasoning is not None: message["reasoning_content"] = reasoning
    return json.dumps({"choices": [{"message": message, "finish_reason": finish}]}).encode()


@pytest.mark.parametrize("tools", [False, True])
def test_provider_is_one_small_attempt_and_never_executes_tools(monkeypatch, tools):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    calls = []
    tool_calls = [{"id": "diagnostic", "type": "function", "function": {"name": "diagnostic_noop", "arguments": "{}"}}]
    stream = Stream(response(text="" if tools else "OK", finish="tool_calls" if tools else "stop",
                             calls=tool_calls if tools else None))
    def open_stream(self, url, payload, headers, timeout):
        calls.append(True)
        assert url == diag.ENDPOINT + "/chat/completions"
        assert payload["model"] == "z-ai/glm-5.3-flash" and payload["max_tokens"] == 128
        assert timeout == 20 and headers["Authorization"] == "Bearer " + SECRET
        assert bool(payload.get("tools")) == tools
        return stream
    monkeypatch.setattr(UrllibTransport, "open_stream", open_stream)
    monkeypatch.setattr(ComputerControlAdapter, "connect", lambda _: pytest.fail("provider probe touched desktop"))
    original = config()
    result = diag.provider_check(original, tools=tools)
    assert result["category"] == ("tool_call_returned" if tools else "provider_ok")
    assert result["attempts"] == 1 and result["http_status"] == 200 and calls == [True]
    assert SECRET not in json.dumps(result) and stream.closed
    assert original.model.timeout_seconds == 120 and original.model.max_output_tokens == 4096


@pytest.mark.parametrize("status,category", [(401, "model_authentication"), (403, "model_authentication"),
    (429, "model_rate_limit"), (404, "model_http_error"), (422, "model_http_error"),
    (503, "model_http_error"), (504, "model_timeout")])
def test_http_categories_without_provider_error_body_or_retries(monkeypatch, status, category):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    stream = Stream(SECRET.encode(), status=status)
    monkeypatch.setattr(stream, "read", lambda _: pytest.fail("read private HTTP error body"))
    monkeypatch.setattr(UrllibTransport, "open_stream", lambda *a, **k: stream)
    result = diag.provider_check(config())
    assert result["category"] == category and result["attempts"] == 1
    assert result["http_status"] == status and stream.closed and SECRET not in json.dumps(result)


@pytest.mark.parametrize("error,category", [(ModelTimeoutError(SECRET), "model_timeout"),
    (ModelConnectionError(SECRET), "model_connection")])
def test_timeout_and_connection_errors_are_content_free(monkeypatch, error, category):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    def fail(*a, **k): raise error
    monkeypatch.setattr(UrllibTransport, "open_stream", fail)
    result = diag.provider_check(config())
    assert result["category"] == category and result["attempts"] == 1
    assert result["http_status"] is None and SECRET not in json.dumps(result)


def test_reasoning_only_length_is_budget_exhaustion_not_connectivity_failure(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    stream = Stream(response(text="", finish="length", reasoning=SECRET))
    monkeypatch.setattr(UrllibTransport, "open_stream", lambda *a, **k: stream)
    result = diag.provider_check(config())
    assert result["category"] == "model_response_budget_exhausted"
    assert result["reasoning_present"] and result["http_status"] == 200
    assert SECRET not in json.dumps(result)


def test_no_tool_call_does_not_claim_unsupported(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    monkeypatch.setattr(UrllibTransport, "open_stream", lambda *a, **k: Stream(response()))
    assert diag.provider_check(config(), tools=True)["category"] == "tool_call_not_returned"


@pytest.mark.parametrize("body", [b"not json", b"x" * (diag.BODY_LIMIT + 1)], ids=["malformed", "oversized"])
def test_malformed_or_oversized_response(monkeypatch, body):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    stream = Stream(body)
    monkeypatch.setattr(UrllibTransport, "open_stream", lambda *a, **k: stream)
    assert diag.provider_check(config())["category"] == "model_response_invalid"
    assert stream.closed


def test_no_key_or_wrong_endpoint_does_not_send_a_request(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setattr(UrllibTransport, "open_stream", lambda *a, **k: pytest.fail("unexpected HTTP"))
    assert diag.provider_check(config()) == {"category": "credential_unavailable", "attempts": 0}
    bad = replace(config(), model=replace(config().model, base_url="https://untrusted.invalid"))
    assert diag.provider_check(bad) == {"category": "model_configuration", "attempts": 0}


def test_supervisor_deadline_and_secret_free_cancellation(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("NVIDIA_API_KEY", SECRET)
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "must-not-be-created"))
    previous = logging.root.manager.disable
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 30 and kwargs["stderr"] == subprocess.DEVNULL
        assert "SSLKEYLOGFILE" not in kwargs["env"]
        assert SECRET not in str(command)
        raise subprocess.TimeoutExpired(SECRET, 30)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert diag.main(["provider", "--config", "unused.toml"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["category"] == "diagnostic_wall_timeout" and result["request_outcome"] == "unknown"
    assert SECRET not in json.dumps(result) and logging.root.manager.disable == previous
    assert not (tmp_path / "must-not-be-created").exists()


def test_native_smoke_uses_executor_and_never_constructs_provider(monkeypatch):
    calls = []
    def request(self, operation):
        calls.append(operation)
        return "ready" if operation == "health" else {"status": "observed", "app": "Notepad"}
    monkeypatch.setattr(ComputerControlAdapter, "_request", request)
    monkeypatch.setattr(diag, "create_provider", lambda *a, **k: pytest.fail("native check used a model"))
    result = diag.native_check(config(), enabled=True)
    assert result["desktop_observations"] == [{"status": "observed", "app": "Notepad"}]
    assert not result["model_used"] and not result["operator_confirmed"]
    assert calls == ["health", "get_foreground_app"]
    calls.clear()
    blocked = replace(config(), permissions=PermissionConfig(blocked_capabilities=("screen.observe",)))
    assert diag.native_check(blocked, enabled=True)["category"] == "permission_denied"
    assert calls == ["health"]


def test_native_opt_in_and_allowlist_precede_service_access(monkeypatch):
    monkeypatch.setattr(ComputerControlAdapter, "connect", lambda _: pytest.fail("unauthorized desktop access"))
    assert diag.native_check(config(), enabled=False)["category"] == "desktop_not_enabled"
    off = replace(config(), conversation=ConversationConfig(tools_enabled=False))
    assert diag.native_check(off, enabled=True)["category"] == "desktop_not_enabled"
    denied = replace(config(), tools=ToolConfig(enabled=("open_notepad",)))
    assert diag.native_check(denied, enabled=True)["category"] == "tool_disabled"


@pytest.mark.parametrize("error,category", [(ModelAuthError(SECRET), "model_authentication"),
    (ModelRateLimitError(SECRET), "model_rate_limit"), (ModelTimeoutError(SECRET), "model_timeout"),
    (ModelConnectionError(SECRET), "model_connection"), (ModelConfigError(SECRET), "model_configuration"),
    (ModelResponseError(SECRET), "model_response_invalid"), (ModelProviderError(SECRET), "model_http_error"),
    (RuntimeError(SECRET), "model_failure")])
def test_conversation_retains_safe_typed_category_in_cli_json(tmp_path, monkeypatch, capsys, error, category):
    class FailedModel:
        def generate(self, request): raise error
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: FailedModel())
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{tmp_path.as_posix()}"\n[logging]\nlevel = "CRITICAL"\n')
    assert cli.main(["--config", str(path), "chat", "--message", "Hello", "--json"]) == 1
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["error_category"] == category
    assert SECRET not in output.out + output.err
    assert model_error_category(error) == category
