"""Provider for OpenAI-compatible ``/chat/completions`` HTTP APIs.

Chosen because the existing ``ModelConfig`` is provider-neutral (``provider``,
``name``, ``api_key_env``, ``temperature``, ``max_output_tokens``,
``timeout_seconds``) and this request/response shape is the de-facto standard
implemented by OpenAI, Azure OpenAI, Groq, Together, OpenRouter, vLLM, Ollama
and llama.cpp. One implementation therefore covers many backends by changing
only ``base_url`` and ``name`` in configuration.

Vendor specifics live *here only*. Core components never import this module.
"""

from __future__ import annotations

import json
import time
from typing import Any

from agentcore.config.schema import ModelConfig
from agentcore.models.base import BaseModelProvider
from agentcore.models.errors import ModelAuthError, ModelConfigError, ModelResponseError
from agentcore.models.messages import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)
from agentcore.models.transport import HttpClient, RetryPolicy, Transport
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.util.ids import new_id

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "max_tokens": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAICompatibleProvider(BaseModelProvider):
    """Talks to any ``POST {base_url}/chat/completions`` endpoint."""

    name = "openai_compatible"

    def __init__(
        self,
        config: ModelConfig | None = None,
        *,
        transport: Transport | None = None,
        client: HttpClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        super().__init__(config)
        # Honour the configured attempt budget unless a policy is injected.
        policy = retry or RetryPolicy(max_attempts=self.config.max_retries)
        self.client = client or HttpClient(transport=transport, retry=policy)
        self._log = get_logger("models.openai_compatible")
        self._validate_config()

    # -- configuration -----------------------------------------------------
    def _validate_config(self) -> None:
        if not self.config.name or self.config.name == "none":
            raise ModelConfigError(
                "model.name must be set to a real model identifier",
                provider=self.name)
        if not self.config.base_url:
            raise ModelConfigError(
                "model.base_url is required for an OpenAI-compatible provider",
                provider=self.name)

    @property
    def endpoint(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _auth_headers(self) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build headers and return the secret separately for scrubbing.

        The key is resolved through the project's existing indirection
        (``ModelConfig.resolve_api_key`` reads the env var named by
        ``api_key_env``) and is never stored on the instance.
        """
        headers = {"Content-Type": "application/json"}
        api_key = self.config.resolve_api_key()
        if not api_key:
            raise ModelAuthError(
                "no API credential available; set the environment variable "
                f"named by model.api_key_env ({self.config.api_key_env!r})",
                provider=self.name, api_key_env=self.config.api_key_env)
        headers["Authorization"] = f"Bearer {api_key}"
        return headers, (api_key,)

    # -- request construction ---------------------------------------------
    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        """Translate a neutral request into the provider's wire format.

        Per-request values win; otherwise the configured defaults apply.
        """
        payload: dict[str, Any] = {
            "model": request.model or self.config.name,
            "messages": [m.to_dict() for m in request.messages],
        }
        temperature = (request.temperature if request.temperature is not None
                       else self.config.temperature)
        payload["temperature"] = temperature
        max_tokens = (request.max_output_tokens if request.max_output_tokens
                      is not None else self.config.max_output_tokens)
        payload["max_tokens"] = max_tokens
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.tools:
            payload["tools"] = request.tools
        if request.response_format:
            payload["response_format"] = request.response_format
        return payload

    # -- execution ---------------------------------------------------------
    def generate(self, request: ModelRequest) -> ModelResponse:
        payload = self.build_payload(request)
        headers, secrets = self._auth_headers()
        started = time.perf_counter()

        self._log.info(Events.MODEL_REQUEST, provider=self.name,
                       model=payload["model"], endpoint=self.endpoint,
                       messages=len(request.messages),
                       temperature=payload.get("temperature"),
                       max_tokens=payload.get("max_tokens"))

        try:
            data = self.client.post_json(
                self.endpoint, payload, headers,
                self.config.timeout_seconds, secrets=secrets)
        except Exception as exc:
            self._log.warning(Events.MODEL_FAILED, provider=self.name,
                              model=payload["model"],
                              error=type(exc).__name__,
                              message=getattr(exc, "message", str(exc)))
            raise

        response = self.parse_response(data, latency_ms=(
            time.perf_counter() - started) * 1000)
        self._log.info(Events.MODEL_RESPONSE, provider=self.name,
                       model=response.model,
                       finish_reason=str(response.finish_reason),
                       latency_ms=round(response.latency_ms, 3),
                       **response.usage.to_dict())
        return response

    # -- response normalisation -------------------------------------------
    def parse_response(self, data: Any, *, latency_ms: float = 0.0
                       ) -> ModelResponse:
        """Normalise a provider payload, failing loudly if it is unusable."""
        if not isinstance(data, dict):
            raise ModelResponseError("provider response was not a JSON object",
                                     provider=self.name,
                                     got=type(data).__name__)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelResponseError("provider response contained no choices",
                                     provider=self.name)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ModelResponseError("provider choice was not an object",
                                     provider=self.name)

        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelResponseError("provider choice contained no message",
                                     provider=self.name)

        content = message.get("content")
        text = content if isinstance(content, str) else ""
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))

        # Empty content is only acceptable when the model chose to call a tool.
        if not text.strip() and not tool_calls:
            raise ModelResponseError("provider returned empty content",
                                     provider=self.name,
                                     finish_reason=choice.get("finish_reason"))

        return ModelResponse(
            text=text,
            model=data.get("model") if isinstance(data.get("model"), str)
            else self.config.name,
            provider=self.name,
            finish_reason=_FINISH_REASONS.get(
                choice.get("finish_reason"), FinishReason.UNKNOWN),
            usage=Usage.from_payload(data.get("usage")),
            tool_calls=tool_calls,
            raw=data,
            latency_ms=latency_ms,
        )

    def _parse_tool_calls(self, raw: Any) -> list[ToolCall]:
        """Normalise provider tool calls into the neutral :class:`ToolCall`.

        The wire shape is the de-facto standard::

            {"id": "call_123", "type": "function",
             "function": {"name": "echo_test", "arguments": "{\\"text\\": \\"hi\\"}"}}

        Vendor nesting stops here: everything above this layer sees only
        ``ToolCall(id, name, arguments)``.

        Failure policy — a tool call is an instruction to *act*, so a
        half-understood one is worse than none:

        * **Malformed structure** (not an object, no ``function``, no name, or
          arguments that are neither a JSON object nor absent) raises
          :class:`ModelResponseError`. Silently skipping such an entry would
          make the model believe a tool ran, or leave it waiting for a result
          that never comes.
        * **Invalid JSON arguments** raise. They are never "repaired", guessed
          at, or wrapped in a placeholder: the conversation layer must never
          receive an apparently-valid call built from unparseable data.
        * **Absent/empty arguments** become ``{}`` — the argument-free call
          the ``ToolCall`` contract already models. Tool-side validation then
          decides whether that is acceptable.

        Nothing here looks up, validates against, or executes a tool. It is
        pure normalisation; the registry, permission policy and executor act
        later and unchanged.
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ModelResponseError(
                "provider tool_calls was not a list",
                provider=self.name, got=type(raw).__name__)

        calls: list[ToolCall] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ModelResponseError(
                    "provider tool call was not an object",
                    provider=self.name, index=index,
                    got=type(item).__name__)

            function = item.get("function")
            if not isinstance(function, dict):
                raise ModelResponseError(
                    "provider tool call had no function object",
                    provider=self.name, index=index)

            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ModelResponseError(
                    "provider tool call had no usable function name",
                    provider=self.name, index=index)

            # A provider id is authoritative and passed through untouched. When
            # one is missing, a fresh random id is minted with the project's
            # own utility — never derived from the name or arguments, so two
            # identical calls stay distinguishable.
            raw_id = item.get("id")
            call_id = (raw_id if isinstance(raw_id, str) and raw_id.strip()
                       else new_id("call"))

            calls.append(ToolCall(
                id=call_id, name=name,
                arguments=self._parse_arguments(function.get("arguments"),
                                                index=index, tool=name)))
        return calls

    def _parse_arguments(self, arguments: Any, *, index: int,
                         tool: str) -> dict[str, Any]:
        """Decode tool-call arguments. Never guesses, never executes.

        ``json.loads`` only — no ``eval``, no literal evaluation, no partial
        recovery. Anything that is not a JSON *object* is a structural error,
        because tool arguments are a named mapping by contract.
        """
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            return dict(arguments)
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError as exc:
                # The raw text is deliberately not included: it is
                # model-generated and could carry anything.
                raise ModelResponseError(
                    "provider tool call arguments were not valid JSON",
                    provider=self.name, index=index, tool=tool,
                    reason=exc.msg) from None
            if not isinstance(decoded, dict):
                raise ModelResponseError(
                    "provider tool call arguments were not a JSON object",
                    provider=self.name, index=index, tool=tool,
                    got=type(decoded).__name__)
            return decoded
        raise ModelResponseError(
            "provider tool call arguments had an unexpected type",
            provider=self.name, index=index, tool=tool,
            got=type(arguments).__name__)
