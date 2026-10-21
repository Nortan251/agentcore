"""Model-layer error hierarchy.

Extends the project's existing :class:`AgentError` tree rather than inventing a
parallel one, so callers can keep catching ``AgentError`` and the ``retryable``
hint continues to drive recovery logic.

Every error here is safe to log: messages and details are constructed from
status codes, provider error codes and sanitised text only. API keys are never
passed into these constructors — see :mod:`agentcore.models.transport`, which
strips credential headers before any error is raised.
"""

from __future__ import annotations

from typing import Any

from agentcore.errors import AgentError


class ModelError(AgentError):
    """Base class for every model/provider failure."""


class ModelConfigError(ModelError):
    """Provider or model configuration is unusable (bad provider name, no URL)."""


class ModelAuthError(ModelError):
    """Credential missing, rejected or lacking permission (HTTP 401/403).

    Never retryable: retrying a bad key just burns quota.
    """


class ModelRateLimitError(ModelError):
    """Provider applied rate limiting (HTTP 429)."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None,
                 **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after
        if retry_after is not None:
            self.details["retry_after"] = retry_after


class ModelTimeoutError(ModelError):
    """The request exceeded ``model.timeout_seconds``."""

    retryable = True


class ModelConnectionError(ModelError):
    """DNS/socket/TLS failure — the request never reached the provider."""

    retryable = True


class ModelResponseError(ModelError):
    """The provider replied, but the body was not valid or not usable.

    Covers malformed JSON, a schema that doesn't match expectations, and
    responses containing no usable content.
    """


class ModelProviderError(ModelError):
    """The provider returned an error status (5xx, or an unclassified 4xx).

    5xx is treated as retryable; 4xx is a client mistake and is not.
    """

    def __init__(self, message: str, *, status_code: int | None = None,
                 **details: Any) -> None:
        super().__init__(message, **details)
        self.status_code = status_code
        if status_code is not None:
            self.details["status_code"] = status_code
            # Server-side faults are transient; client-side ones are not.
            self.retryable = status_code >= 500


def model_error_category(error: BaseException | None) -> str:
    """Closed error vocabulary for operator output; never exception/provider text."""
    if isinstance(error, ModelResponseError) and error.details.get("finish_reason") == "length":
        return "model_response_budget_exhausted"
    for kind, category in (
        (ModelAuthError, "model_authentication"), (ModelRateLimitError, "model_rate_limit"),
        (ModelTimeoutError, "model_timeout"), (ModelConnectionError, "model_connection"),
        (ModelConfigError, "model_configuration"), (ModelResponseError, "model_response_invalid"),
        (ModelProviderError, "model_http_error"),
    ):
        if isinstance(error, kind):
            return category
    return "model_failure"
