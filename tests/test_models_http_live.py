"""Exercises the real ``UrllibTransport`` against a local stub server.

Mocked tests replace the transport, so the actual socket/urllib code path would
otherwise be untested. This binds a throwaway HTTP server on localhost — no
external network, no real credential.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentcore.config.schema import ModelConfig
from agentcore.models.errors import ModelAuthError, ModelProviderError, ModelTimeoutError
from agentcore.models.factory import create_provider
from agentcore.models.messages import ModelRequest
from agentcore.models.transport import UrllibTransport

FAKE_KEY = "sk-localhost-stub-not-a-real-key"
ENV_VAR = "LOCAL_STUB_MODEL_KEY"


class _State:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_auth: str | None = None
        self.mode = "ok"


STATE = _State()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        STATE.calls += 1
        STATE.seen_auth = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if STATE.mode == "rate_limit_then_ok" and STATE.calls == 1:
            return self._send(429, {"error": {"message": "slow down"}},
                              {"Retry-After": "0"})
        if STATE.mode == "auth_fail":
            return self._send(401, {"error": {
                "message": f"Invalid API key {FAKE_KEY}"}})
        if STATE.mode == "server_error":
            return self._send(500, {"error": {"message": "internal"}})
        if STATE.mode == "malformed":
            return self._send_raw(200, b"<html>not json</html>")
        if STATE.mode == "timeout":
            time.sleep(2.0)

        return self._send(200, {
            "id": "chatcmpl-stub",
            "model": body.get("model", "stub-model"),
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": f"reply: "
                                                f"{body['messages'][-1]['content']}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 6,
                      "total_tokens": 17},
        })

    def _send(self, status, payload, headers=None):
        self._send_raw(status, json.dumps(payload).encode(), headers)

    def _send_raw(self, status, data, headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


@pytest.fixture
def provider_factory(server, monkeypatch):
    monkeypatch.setenv(ENV_VAR, FAKE_KEY)

    def make(**overrides):
        STATE.calls = 0
        STATE.seen_auth = None
        STATE.mode = overrides.pop("mode", "ok")
        config = ModelConfig(provider="openai_compatible", name="stub-model",
                             api_key_env=ENV_VAR, base_url=server,
                             timeout_seconds=overrides.pop("timeout", 5.0),
                             max_retries=overrides.pop("max_retries", 3))
        return create_provider(config, **overrides)

    return make


def test_uses_the_real_urllib_transport(provider_factory):
    assert isinstance(provider_factory().client.transport, UrllibTransport)


def test_real_http_round_trip(provider_factory):
    response = provider_factory().generate(
        ModelRequest.from_prompt("hello", system="be brief"))
    assert response.text == "reply: hello"
    assert response.usage.total_tokens == 17
    assert response.latency_ms > 0


def test_real_auth_header_delivered(provider_factory):
    provider_factory().complete("hi")
    assert STATE.seen_auth == f"Bearer {FAKE_KEY}"


def test_real_retry_recovers_after_rate_limit(provider_factory):
    assert provider_factory(mode="rate_limit_then_ok").complete("hi").startswith(
        "reply:")
    assert STATE.calls == 2


def test_real_auth_failure_hides_secret(provider_factory):
    """Server echoes the key back; it must not reach the exception."""
    with pytest.raises(ModelAuthError) as exc:
        provider_factory(mode="auth_fail").complete("hi")
    assert FAKE_KEY not in json.dumps(exc.value.to_dict()) + str(exc.value)


def test_real_server_error_is_retried_then_raised(provider_factory):
    with pytest.raises(ModelProviderError):
        provider_factory(mode="server_error", max_retries=2).complete("hi")
    assert STATE.calls == 2


def test_real_malformed_body_raises(provider_factory):
    from agentcore.models.errors import ModelResponseError

    with pytest.raises(ModelResponseError):
        provider_factory(mode="malformed").complete("hi")


def test_real_socket_timeout(provider_factory):
    with pytest.raises(ModelTimeoutError):
        provider_factory(mode="timeout", timeout=0.3, max_retries=1).complete("hi")
