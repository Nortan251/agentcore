"""HTTP transport for model providers.

Uses only the standard library (``urllib``) so the project gains no new runtime
dependency for what is, in practice, one JSON POST.

Responsibilities kept here so providers stay declarative:

* issuing the request and decoding JSON
* mapping transport/status failures onto :mod:`agentcore.models.errors`
* bounded retries with exponential backoff for transient failures
* **guaranteeing credentials never escape** — headers are sanitised before any
  error, log or exception detail is produced

The transport is injectable: tests substitute a fake, so no test touches the
network.
"""

from __future__ import annotations

import json
import math
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from agentcore.models.errors import (
    ModelAuthError,
    ModelConnectionError,
    ModelProviderError,
    ModelRateLimitError,
    ModelResponseError,
    ModelTimeoutError,
)
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.util.clock import Clock, SystemClock

#: Header names whose values must never be logged or included in errors.
_SENSITIVE_HEADERS = frozenset({
    "authorization", "x-api-key", "api-key", "proxy-authorization", "cookie",
})

#: Body keys that some providers echo back and which may carry secrets.
_SENSITIVE_BODY_KEYS = frozenset({"api_key", "apikey", "token", "secret",
                                  "password", "authorization"})


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Replace credential header values with a placeholder.

    Used for every log line and error detail the transport emits.
    """
    return {k: ("***" if k.lower() in _SENSITIVE_HEADERS else v)
            for k, v in headers.items()}


def sanitize_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Defensive scrub: remove any known secret literal from provider text.

    Providers occasionally echo a submitted key inside an error message; this
    ensures such a value cannot reach a log or an exception.
    """
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelResponseError(
                "provider returned a body that is not valid JSON",
                status_code=self.status, reason=str(exc)) from exc


class Transport(Protocol):
    """Minimal HTTP seam so providers can be tested without a network."""

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library HTTP transport."""

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    body=response.read(),
                    headers={k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            # An HTTP error still carries a usable body; let the caller classify.
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - body may be unreadable
                pass
            return HttpResponse(
                status=exc.code, body=body,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            )
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "timed out" in reason.lower():
                raise ModelTimeoutError("request to provider timed out",
                                        timeout_seconds=timeout) from exc
            raise ModelConnectionError("could not reach the model provider",
                                       reason=reason) from exc
        except TimeoutError as exc:
            raise ModelTimeoutError("request to provider timed out",
                                    timeout_seconds=timeout) from exc


@dataclass
class RetryPolicy:
    """Bounded exponential backoff for transient failures."""

    max_attempts: int = 3
    initial_backoff: float = 0.5
    max_backoff: float = 8.0
    jitter: bool = True

    def backoff_for(self, attempt: int) -> float:
        """Delay before ``attempt`` (1-based) is retried."""
        delay = min(self.initial_backoff * (2 ** (attempt - 1)), self.max_backoff)
        if self.jitter:
            delay *= 0.5 + random.random() / 2
        return delay


class HttpClient:
    """Transport + retry + error classification.

    Providers hand it a URL, payload and headers; it returns parsed JSON or
    raises a typed, sanitised :class:`ModelError`.

    Task 42.4b adds trusted deadline enforcement: when an
    :class:`OperationContext` is supplied, every physical attempt uses
    ``min(configured timeout, remaining deadline)`` and no attempt starts
    after expiry. Retries share the same parent deadline and never launch
    after it.
    """

    def __init__(self, transport: Transport | None = None,
                 retry: RetryPolicy | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Clock | None = None) -> None:
        self.transport = transport or UrllibTransport()
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock or SystemClock()
        self._log = get_logger("models.transport")

    def _deadline_state(self, context: Any, clock: Clock
                        ) -> tuple[Any, float, bool]:
        """Return (deadline, remaining, expired) or (None, inf, False).

        Fail-closed hardening (final repair):
        - context is None -> intentional legacy/no-context, unbounded.
        - context is non-None but deadline missing/None/invalid -> expired
          (do not silently disable controls).
        - deadline method failures -> expired.
        """
        if context is None:
            return None, math.inf, False
        # Trusted context is non-None: require explicit deadline.
        # Use explicit presence check to distinguish missing vs None.
        if not hasattr(context, "deadline"):
            return None, 0.0, True
        deadline = getattr(context, "deadline", None)
        if deadline is None:
            return None, 0.0, True
        try:
            expired = bool(deadline.expired(clock))
        except Exception:
            return deadline, 0.0, True
        try:
            remaining = float(deadline.remaining(clock))
        except Exception:
            return deadline, 0.0, True
        if not math.isfinite(remaining):
            # Unbounded deadline -> inf remaining, only explicit expired flag matters.
            remaining = math.inf
            return deadline, remaining, expired
        if remaining <= 0:
            expired = True
        return deadline, remaining, expired

    def _is_cancelled(self, context: Any) -> bool:
        """Fail-closed: malformed trusted context is treated as cancelled."""
        if context is None:
            return False
        if not hasattr(context, "cancellation"):
            return True
        cancellation = getattr(context, "cancellation", None)
        if cancellation is None:
            return True
        try:
            return bool(getattr(cancellation, "requested", False))
        except Exception:
            return True

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float,
                  *, secrets: tuple[str, ...] = (),
                  context: Any | None = None,
                  clock: Clock | None = None) -> Any:
        safe_headers = sanitize_headers(headers)
        effective_clock = clock or self._clock
        last_error: Exception | None = None

        for attempt in range(1, self.retry.max_attempts + 1):
            # -- trusted cancellation / deadline check before each attempt --
            if self._is_cancelled(context):
                raise ModelTimeoutError(
                    "operation cancelled before attempt",
                    timeout_seconds=timeout)

            _, remaining, expired = self._deadline_state(context,
                                                         effective_clock)
            if expired or remaining <= 0:
                raise ModelTimeoutError(
                    "deadline exceeded before attempt",
                    timeout_seconds=timeout)

            if math.isfinite(remaining):
                effective_timeout = min(timeout, remaining)
            else:
                effective_timeout = timeout

            # Never pass zero or negative timeout to urllib.
            if effective_timeout <= 0:
                raise ModelTimeoutError(
                    "deadline exceeded before attempt",
                    timeout_seconds=timeout)

            try:
                response = self.transport.post_json(
                    url, payload, headers, effective_timeout)
                return self._handle(response, url, secrets)
            except (ModelTimeoutError, ModelConnectionError,
                    ModelRateLimitError, ModelProviderError) as exc:
                if not exc.retryable or attempt == self.retry.max_attempts:
                    raise
                last_error = exc

                # -- deadline / cancellation check before retry --
                if self._is_cancelled(context):
                    raise ModelTimeoutError(
                        "operation cancelled before retry",
                        timeout_seconds=timeout) from exc

                _, remaining_after, expired_after = self._deadline_state(
                    context, effective_clock)
                if expired_after or remaining_after <= 0:
                    raise ModelTimeoutError(
                        "deadline exceeded before retry",
                        timeout_seconds=timeout) from exc

                delay = getattr(exc, "retry_after", None) or self.retry.backoff_for(
                    attempt)

                # Backoff must not knowingly carry execution beyond deadline.
                if math.isfinite(remaining_after) and delay >= remaining_after:
                    raise ModelTimeoutError(
                        "deadline would be exceeded by retry backoff",
                        timeout_seconds=remaining_after) from exc

                self._log.warning(
                    Events.MODEL_RETRY, url=url, attempt=attempt,
                    max_attempts=self.retry.max_attempts,
                    error=type(exc).__name__, delay_seconds=round(delay, 3),
                    headers=safe_headers,
                )
                self._sleep(delay)

        assert last_error is not None  # pragma: no cover - loop always raises
        raise last_error

    # -- classification ----------------------------------------------------
    def _handle(self, response: HttpResponse, url: str,
                secrets: tuple[str, ...]) -> Any:
        if 200 <= response.status < 300:
            return response.json()

        detail = self._error_detail(response, secrets)
        status = response.status

        if status in (401, 403):
            raise ModelAuthError(
                "provider rejected the credential; check the API key named by "
                "model.api_key_env",
                status_code=status, provider_message=detail)
        if status == 429:
            raise ModelRateLimitError(
                "provider rate limit exceeded", status_code=status,
                retry_after=self._retry_after(response), provider_message=detail)
        if status == 408 or status == 504:
            raise ModelTimeoutError("provider reported a timeout",
                                    status_code=status, provider_message=detail)
        raise ModelProviderError(
            f"provider returned HTTP {status}", status_code=status,
            provider_message=detail)

    def _error_detail(self, response: HttpResponse,
                      secrets: tuple[str, ...]) -> str:
        """Extract a short, secret-free description of a provider error."""
        try:
            data = json.loads(response.body.decode("utf-8"))
        except Exception:
            text = response.body.decode("utf-8", errors="replace")[:200]
            return sanitize_text(text, secrets)

        message: Any = data
        if isinstance(data, dict):
            error = data.get("error", data)
            if isinstance(error, dict):
                message = error.get("message") or error.get("type") or error
            else:
                message = error
        text = message if isinstance(message, str) else json.dumps(message)[:200]
        return sanitize_text(_strip_sensitive(text), secrets)

    @staticmethod
    def _retry_after(response: HttpResponse) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None


def _strip_sensitive(text: str) -> str:
    """Redact ``key=value`` pairs that name a credential inside free text."""
    lowered = text.lower()
    for key in _SENSITIVE_BODY_KEYS:
        if key in lowered:
            return "<redacted provider message>"
    return text
