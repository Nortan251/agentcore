"""Tests for HTTP transport, retry policy and error classification."""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from agentcore.models.errors import (
    ModelAuthError,
    ModelConnectionError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
)
from agentcore.models.transport import (
    HttpClient,
    HttpResponse,
    RetryPolicy,
    sanitize_headers,
    sanitize_text,
)

SECRET = "sk-live-do-not-leak"


class ScriptedTransport:
    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.calls = 0

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls += 1
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http(status: int, body: Any, headers: dict[str, str] | None = None
         ) -> HttpResponse:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HttpResponse(status=status, body=raw, headers=headers or {})


def client(items: list[Any], attempts: int = 3) -> tuple[HttpClient, list[float],
                                                         ScriptedTransport]:
    slept: list[float] = []
    transport = ScriptedTransport(items)
    return (HttpClient(transport=transport,
                       retry=RetryPolicy(max_attempts=attempts,
                                         initial_backoff=0.01, jitter=False),
                       sleep=slept.append),
            slept, transport)


def call(c: HttpClient) -> Any:
    return c.post_json("https://x.test/v1/chat/completions", {"a": 1},
                       {"Authorization": f"Bearer {SECRET}"}, 5.0,
                       secrets=(SECRET,))


# -- sanitisation -----------------------------------------------------------

def test_sanitize_headers_masks_credentials():
    cleaned = sanitize_headers({"Authorization": f"Bearer {SECRET}",
                                "X-API-Key": SECRET,
                                "Content-Type": "application/json"})
    assert cleaned["Authorization"] == "***"
    assert cleaned["X-API-Key"] == "***"
    assert cleaned["Content-Type"] == "application/json"


def test_sanitize_text_removes_known_secret():
    assert SECRET not in sanitize_text(f"key {SECRET} rejected", (SECRET,))


# -- success ----------------------------------------------------------------

def test_successful_response_returns_parsed_json():
    c, _, _ = client([http(200, {"ok": True})])
    assert call(c) == {"ok": True}


def test_invalid_json_raises_response_error():
    c, _, _ = client([http(200, b"<html>oops</html>")])
    with pytest.raises(ModelResponseError):
        call(c)


# -- classification ---------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (401, ModelAuthError),
    (403, ModelAuthError),
    (429, ModelRateLimitError),
    (408, ModelTimeoutError),
    (504, ModelTimeoutError),
    (500, ModelProviderError),
    (400, ModelProviderError),
])
def test_status_codes_map_to_error_types(status, expected):
    c, _, _ = client([http(status, {"error": {"message": "x"}})], attempts=1)
    with pytest.raises(expected):
        call(c)


def test_retry_after_header_parsed():
    c, _, _ = client([http(429, {}, {"retry-after": "1.5"})], attempts=1)
    with pytest.raises(ModelRateLimitError) as exc:
        call(c)
    assert exc.value.retry_after == 1.5


def test_invalid_retry_after_ignored():
    c, _, _ = client([http(429, {}, {"retry-after": "soon"})], attempts=1)
    with pytest.raises(ModelRateLimitError) as exc:
        call(c)
    assert exc.value.retry_after is None


# -- retry behaviour --------------------------------------------------------

def test_transient_failure_is_retried_then_succeeds():
    c, slept, transport = client([
        ModelConnectionError("flaky"), http(200, {"ok": True})])
    assert call(c) == {"ok": True}
    assert transport.calls == 2
    assert len(slept) == 1


def test_retries_exhausted_raises_last_error():
    c, slept, transport = client([ModelTimeoutError("t")] * 3, attempts=3)
    with pytest.raises(ModelTimeoutError):
        call(c)
    assert transport.calls == 3
    assert len(slept) == 2  # no sleep after the final attempt


def test_non_retryable_error_is_not_retried():
    c, slept, transport = client([http(401, {"error": {"message": "bad"}}),
                                  http(200, {"ok": True})])
    with pytest.raises(ModelAuthError):
        call(c)
    assert transport.calls == 1
    assert slept == []


def test_client_error_4xx_not_retried():
    c, _, transport = client([http(400, {}), http(200, {"ok": True})])
    with pytest.raises(ModelProviderError):
        call(c)
    assert transport.calls == 1


def test_server_error_5xx_is_retried():
    c, _, transport = client([http(503, {}), http(200, {"ok": True})])
    assert call(c) == {"ok": True}
    assert transport.calls == 2


def test_rate_limit_retry_uses_retry_after_delay():
    c, slept, _ = client([http(429, {}, {"retry-after": "3"}),
                          http(200, {"ok": True})])
    assert call(c) == {"ok": True}
    assert slept == [3.0]


def test_single_attempt_policy_disables_retry():
    c, _, transport = client([ModelConnectionError("x")], attempts=1)
    with pytest.raises(ModelConnectionError):
        call(c)
    assert transport.calls == 1


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(initial_backoff=1.0, max_backoff=4.0, jitter=False)
    assert [policy.backoff_for(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 4.0]


def test_jitter_stays_within_bounds():
    policy = RetryPolicy(initial_backoff=2.0, jitter=True)
    for _ in range(50):
        assert 1.0 <= policy.backoff_for(1) <= 2.0


# -- secret safety ----------------------------------------------------------

def test_provider_message_echoing_secret_is_redacted():
    c, _, _ = client([http(500, {"error": {"message": f"got {SECRET}"}})],
                     attempts=1)
    with pytest.raises(ModelProviderError) as exc:
        call(c)
    assert SECRET not in json.dumps(exc.value.to_dict())


def test_body_mentioning_api_key_field_is_redacted():
    c, _, _ = client([http(400, {"error": {"message": 'api_key "abc" invalid'}})],
                     attempts=1)
    with pytest.raises(ModelProviderError) as exc:
        call(c)
    assert exc.value.details["provider_message"] == "<redacted provider message>"


def test_retry_logs_contain_no_credentials(caplog):
    import logging

    from agentcore.observability.logging import configure_logging
    from agentcore.config.schema import LoggingConfig

    configure_logging(LoggingConfig(level="DEBUG"))
    logging.getLogger("agentcore").propagate = True
    caplog.set_level(logging.DEBUG, logger="agentcore")

    c, _, _ = client([ModelConnectionError("flaky"), http(200, {"ok": True})])
    call(c)

    blob = "\n".join(
        r.getMessage() + json.dumps(getattr(r, "agent_context", {}), default=str)
        for r in caplog.records)
    assert "model.retry" in blob
    assert SECRET not in blob
    configure_logging(LoggingConfig(level="CRITICAL"))
