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
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Generator, Iterator

from agentcore.config.schema import ModelConfig
from agentcore.models.base import BaseModelProvider
from agentcore.models.errors import (
    ModelAuthError,
    ModelConfigError,
    ModelError,
    ModelResponseError,
)
from agentcore.models.messages import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)
from agentcore.models.sse import iter_sse_data
from agentcore.models.stream_context import StreamRequestContext
from agentcore.models.streaming import (
    MAX_STREAM_ARGUMENT_CHARS_PER_CALL,
    MAX_STREAM_ARGUMENT_CHARS_TOTAL,
    MAX_STREAM_FUNCTION_NAME_CHARS,
    MAX_STREAM_TOOL_CALLS,
    ModelStreamEvent,
    ResponseCompleted,
    StreamCancelled,
    StreamEventEnvelope,
    StreamFailed,
    StreamStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    UsageUpdated,
)
from agentcore.models.transport import HttpClient, RetryPolicy, Transport
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.turn import OperationContext
from agentcore.util.clock import Clock
from agentcore.util.ids import new_id

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "max_tokens": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}

_SAFE_ERROR_KIND = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def _wire_messages(messages: list) -> list[dict[str, Any]]:
    """Translate and validate one complete neutral tool protocol transcript.

    The call envelope is ephemeral ModelRequest state. Persisted TOOL history
    has no envelope and must be reframed by the conversation runtime instead
    of being sent as an orphan wire tool response.
    """
    wire: list[dict[str, Any]] = []
    pending: list[str] = []
    for message in messages:
        if message.role is Role.TOOL:
            identifier = message.tool_call_id
            if not pending or identifier != pending[0] or message.tool_calls:
                raise ModelConfigError("Malformed tool message correlation.")
            pending.pop(0)
            wire.append({"role": "tool", "tool_call_id": identifier,
                         "content": message.content})
            continue
        if pending or message.tool_call_id is not None:
            raise ModelConfigError("Malformed tool message correlation.")
        if message.tool_calls:
            if message.role is not Role.ASSISTANT or len(message.tool_calls) > 32:
                raise ModelConfigError("Malformed assistant tool-call envelope.")
            calls = []
            ids: set[str] = set()
            for call in message.tool_calls:
                if (not isinstance(call, ToolCall) or type(call.id) is not str
                        or not 1 <= len(call.id) <= 256 or not call.id.isprintable()
                        or type(call.name) is not str or not 1 <= len(call.name) <= 256
                        or not call.name.isprintable()
                        or type(call.arguments) is not dict or call.id in ids):
                    raise ModelConfigError("Malformed assistant tool-call envelope.")
                try:
                    arguments = json.dumps(call.arguments, separators=(",", ":"),
                                           ensure_ascii=True, allow_nan=False)
                except (TypeError, ValueError):
                    raise ModelConfigError("Malformed assistant tool-call envelope.") from None
                ids.add(call.id)
                pending.append(call.id)
                calls.append({"id": call.id, "type": "function", "function": {
                    "name": call.name, "arguments": arguments}})
            wire.append({"role": "assistant", "content": message.content,
                         "tool_calls": calls})
            continue
        wire.append(message.to_dict())
    if pending:
        raise ModelConfigError("Malformed tool message correlation.")
    return wire


@dataclass
class _ToolAssembly:
    provider_id: str | None = None
    name: str = ""
    arguments: str = ""

    def clone(self) -> "_ToolAssembly":
        return _ToolAssembly(
            provider_id=self.provider_id,
            name=self.name,
            arguments=self.arguments,
        )


@dataclass(frozen=True)
class _TextSpec:
    text: str


@dataclass(frozen=True)
class _ToolDeltaSpec:
    index: int
    call_id: str | None
    name_fragment: str
    arguments_fragment: str


@dataclass(frozen=True)
class _ToolCompletedSpec:
    index: int
    call: ToolCall


@dataclass(frozen=True)
class _UsageSpec:
    usage: Usage


_EventSpec = _TextSpec | _ToolDeltaSpec | _ToolCompletedSpec | _UsageSpec


def _strict_json_loads(value: str, *, category: str) -> Any:
    """Decode strict JSON without ever putting the source in an error."""
    def reject_constant(_: str) -> None:
        raise ValueError("non-standard JSON constant")

    try:
        return json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        raise ModelResponseError(category) from None


def _usage_from_stream(value: Any) -> Usage | None:
    """Strictly detach usable token counters from a provider chunk."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ModelResponseError("provider stream usage was not an object")

    known = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not any(key in value for key in known):
        return None
    parsed: dict[str, int] = {}
    for key in known:
        item = value.get(key, 0)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ModelResponseError(
                "provider stream usage contained an invalid token count")
        parsed[key] = item
    total = parsed["total_tokens"] or (
        parsed["prompt_tokens"] + parsed["completion_tokens"])
    return Usage(
        prompt_tokens=parsed["prompt_tokens"],
        completion_tokens=parsed["completion_tokens"],
        total_tokens=total,
    )


class _OpenAIStreamNormalizer:
    """Attempt-local, provider-owned OpenAI chunk and tool assembler."""

    def __init__(self, *, model_hint: str) -> None:
        self.model_hint = model_hint
        self.provider_model: str | None = None
        self.text_parts: list[str] = []
        self.tools: dict[int, _ToolAssembly] = {}
        self.total_argument_chars = 0
        self.usage: Usage | None = None
        self.finish_reason: FinishReason | None = None
        self.wire_finish_reason: str | None = None
        self.completed_calls: dict[int, ToolCall] = {}

    def clone(self) -> "_OpenAIStreamNormalizer":
        duplicate = _OpenAIStreamNormalizer(model_hint=self.model_hint)
        duplicate.provider_model = self.provider_model
        duplicate.text_parts = list(self.text_parts)
        duplicate.tools = {
            index: state.clone() for index, state in self.tools.items()
        }
        duplicate.total_argument_chars = self.total_argument_chars
        duplicate.usage = self.usage
        duplicate.finish_reason = self.finish_reason
        duplicate.wire_finish_reason = self.wire_finish_reason
        duplicate.completed_calls = dict(self.completed_calls)
        return duplicate

    def _adopt(self, candidate: "_OpenAIStreamNormalizer") -> None:
        self.provider_model = candidate.provider_model
        self.text_parts = candidate.text_parts
        self.tools = candidate.tools
        self.total_argument_chars = candidate.total_argument_chars
        self.usage = candidate.usage
        self.finish_reason = candidate.finish_reason
        self.wire_finish_reason = candidate.wire_finish_reason
        self.completed_calls = candidate.completed_calls

    def accept_data(self, data: str) -> list[_EventSpec]:
        decoded = _strict_json_loads(
            data, category="provider stream data was not valid JSON")
        if not isinstance(decoded, dict):
            raise ModelResponseError(
                "provider stream chunk was not a JSON object")
        candidate = self.clone()
        specs = candidate._accept_chunk(decoded)
        self._adopt(candidate)
        return specs

    def _accept_chunk(self, chunk: dict[str, Any]) -> list[_EventSpec]:
        raw_model = chunk.get("model")
        if "model" in chunk and raw_model is not None:
            if not isinstance(raw_model, str) or not raw_model:
                raise ModelResponseError(
                    "provider stream model identity was invalid")
            if self.provider_model is not None and \
                    raw_model != self.provider_model:
                raise ModelResponseError(
                    "provider stream model identity changed")
            self.provider_model = raw_model

        usage = _usage_from_stream(chunk.get("usage")) \
            if "usage" in chunk else None
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise ModelResponseError(
                "provider stream choices was not a list")
        if not choices:
            if usage is None:
                raise ModelResponseError(
                    "provider stream chunk contained no usable choice")
            self.usage = usage
            return [_UsageSpec(usage)]

        choice = choices[0]
        if not isinstance(choice, dict):
            raise ModelResponseError(
                "provider stream choice was not an object")
        if "index" in choice:
            choice_index = choice["index"]
            if isinstance(choice_index, bool) or \
                    not isinstance(choice_index, int) or choice_index != 0:
                raise ModelResponseError(
                    "provider stream choice index was invalid")

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ModelResponseError(
                "provider stream choice delta was not an object")
        role = delta.get("role")
        if role is not None and role != "assistant":
            raise ModelResponseError(
                "provider stream delta role was invalid")

        allowed_delta = {"role", "content", "tool_calls"}
        for key, value in delta.items():
            if key not in allowed_delta and value not in (None, "", [], {}):
                raise ModelResponseError(
                    "provider stream delta contained unsupported content")

        content = delta.get("content")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            raise ModelResponseError(
                "provider stream content delta was not a string")

        raw_tools = delta.get("tool_calls")
        if raw_tools is None:
            raw_tools = []
        elif not isinstance(raw_tools, list):
            raise ModelResponseError(
                "provider stream tool_calls delta was not a list")

        raw_finish = choice.get("finish_reason")
        if raw_finish is not None and not isinstance(raw_finish, str):
            raise ModelResponseError(
                "provider stream finish reason was invalid")
        normalized_finish = (
            _FINISH_REASONS.get(raw_finish, FinishReason.UNKNOWN)
            if raw_finish is not None else None
        )

        if self.finish_reason is not None:
            if content or raw_tools:
                raise ModelResponseError(
                    "provider stream changed after its finish reason")
            if normalized_finish is not None and \
                    (normalized_finish is not self.finish_reason or
                     raw_finish != self.wire_finish_reason):
                raise ModelResponseError(
                    "provider stream finish reason changed")
            specs: list[_EventSpec] = []
            if usage is not None:
                self.usage = usage
                specs.append(_UsageSpec(usage))
            return specs

        specs = []
        if content:
            self.text_parts.append(content)
            specs.append(_TextSpec(content))

        for item in raw_tools:
            spec = self._accept_tool_fragment(item)
            if spec is not None:
                specs.append(spec)

        if usage is not None:
            self.usage = usage
            specs.append(_UsageSpec(usage))

        if normalized_finish is not None:
            self.finish_reason = normalized_finish
            self.wire_finish_reason = raw_finish
            specs.extend(self._complete_tools())
        return specs

    def _accept_tool_fragment(self, item: Any) -> _ToolDeltaSpec | None:
        if not isinstance(item, dict):
            raise ModelResponseError(
                "provider stream tool delta was not an object")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or \
                index < 0 or index >= MAX_STREAM_TOOL_CALLS:
            raise ModelResponseError(
                "provider stream tool delta index was invalid")

        raw_type = item.get("type")
        if raw_type is not None and raw_type != "function":
            raise ModelResponseError(
                "provider stream tool delta type was invalid")

        raw_id = item.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            raise ModelResponseError(
                "provider stream tool call id was invalid")
        usable_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else None

        function = item.get("function")
        if function is None:
            function = {}
        elif not isinstance(function, dict):
            raise ModelResponseError(
                "provider stream tool delta function was not an object")
        for key, value in function.items():
            if key not in {"name", "arguments"} and \
                    value not in (None, "", [], {}):
                raise ModelResponseError(
                    "provider stream tool function contained unsupported content")

        name_fragment = function.get("name")
        if name_fragment is None:
            name_fragment = ""
        elif not isinstance(name_fragment, str):
            raise ModelResponseError(
                "provider stream function name fragment was invalid")
        arguments_fragment = function.get("arguments")
        if arguments_fragment is None:
            arguments_fragment = ""
        elif not isinstance(arguments_fragment, str):
            raise ModelResponseError(
                "provider stream argument fragment was invalid")

        current = self.tools.get(index)
        emitted_id: str | None = None
        if current is None:
            current = _ToolAssembly()
        if usable_id is not None:
            if current.provider_id is not None and \
                    current.provider_id != usable_id:
                raise ModelResponseError(
                    "provider stream tool call id changed")
            if current.provider_id is None:
                current.provider_id = usable_id
                emitted_id = usable_id

        if len(current.name) + len(name_fragment) > \
                MAX_STREAM_FUNCTION_NAME_CHARS:
            raise ModelResponseError(
                "provider stream function name exceeded the character limit")
        if len(current.arguments) + len(arguments_fragment) > \
                MAX_STREAM_ARGUMENT_CHARS_PER_CALL:
            raise ModelResponseError(
                "provider stream tool arguments exceeded the per-call limit")
        if self.total_argument_chars + len(arguments_fragment) > \
                MAX_STREAM_ARGUMENT_CHARS_TOTAL:
            raise ModelResponseError(
                "provider stream tool arguments exceeded the total limit")

        meaningful = bool(emitted_id or name_fragment or arguments_fragment)
        if not meaningful:
            return None
        current.name += name_fragment
        current.arguments += arguments_fragment
        self.total_argument_chars += len(arguments_fragment)
        self.tools[index] = current
        return _ToolDeltaSpec(
            index=index,
            call_id=emitted_id,
            name_fragment=name_fragment,
            arguments_fragment=arguments_fragment,
        )

    def _complete_tools(self) -> list[_ToolCompletedSpec]:
        specs: list[_ToolCompletedSpec] = []
        for index in sorted(self.tools):
            if index in self.completed_calls:
                continue
            state = self.tools[index]
            if not state.name.strip():
                raise ModelResponseError(
                    "provider stream tool call had no usable function name")
            if state.arguments == "":
                arguments: dict[str, Any] = {}
                event_arguments: dict[str, Any] = {}
            else:
                decoded = _strict_json_loads(
                    state.arguments,
                    category=(
                        "provider stream tool arguments were not valid JSON"
                    ),
                )
                if not isinstance(decoded, dict):
                    raise ModelResponseError(
                        "provider stream tool arguments were not a JSON object")
                arguments = decoded
                # Parse the same bounded strict JSON into an independently
                # owned presentation value. This avoids mutable aliasing with
                # canonical state without recursion-sensitive ``deepcopy``.
                event_decoded = _strict_json_loads(
                    state.arguments,
                    category=(
                        "provider stream tool arguments were not valid JSON"
                    ),
                )
                if not isinstance(event_decoded, dict):
                    raise ModelResponseError(
                        "provider stream tool arguments were not a JSON object")
                event_arguments = event_decoded
            call_id = state.provider_id or new_id("call")
            call = ToolCall(
                id=call_id,
                name=state.name,
                arguments=arguments,
            )
            self.completed_calls[index] = call
            specs.append(_ToolCompletedSpec(
                index=index,
                call=ToolCall(
                    id=call_id,
                    name=state.name,
                    arguments=event_arguments,
                ),
            ))
        return specs

    def finalize(self, *, latency_ms: float) -> tuple[list[_EventSpec],
                                                       ModelResponse]:
        candidate = self.clone()
        pending: list[_EventSpec] = list(candidate._complete_tools())
        text = "".join(candidate.text_parts)
        if not text.strip() and not candidate.completed_calls:
            raise ModelResponseError(
                "provider stream returned empty content")
        calls = [candidate.completed_calls[index]
                 for index in sorted(candidate.completed_calls)]
        response = ModelResponse(
            text=text,
            model=candidate.provider_model or candidate.model_hint,
            provider="openai_compatible",
            finish_reason=candidate.finish_reason or FinishReason.UNKNOWN,
            usage=candidate.usage or Usage(),
            tool_calls=calls,
            raw={},
            latency_ms=latency_ms,
        )
        self._adopt(candidate)
        return pending, response


class _TrustedStreamCancellation(Exception):
    def __init__(self, reason: CancellationReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class _AttemptSuccess:
    pending: tuple[_EventSpec, ...]
    response: ModelResponse


def _function_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate neutral ToolSpec dictionaries, not authority metadata.

    Only representable ToolParameter contracts are accepted. In particular,
    Python-specific regex constructs are refused rather than silently widened.
    Local Tool.validate_args remains the execution authority.
    """
    def invalid() -> None:
        raise ModelConfigError("Unsupported or malformed neutral tool contract.")

    def finite(value):
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            return False

    def matches_type(value, kind):
        if kind == "number":
            return finite(value)
        if kind == "array":
            return type(value) is list
        return type(value) is {"string": str, "integer": int, "boolean": bool,
                               "array": list, "object": dict}[kind]

    def json_native(value, ancestors=None):
        """Strict recursive representation contract, not Python equality.

        JSON must not turn tuples into lists, stringify object keys, coerce
        subclasses, or admit non-finite floats. Cycles fail deterministically.
        """
        kind = type(value)
        if value is None or kind in (str, bool, int):
            return True
        if kind is float:
            return math.isfinite(value)
        if kind not in (list, dict):
            return False
        ancestors = set() if ancestors is None else ancestors
        identity = id(value)
        if identity in ancestors:
            return False
        ancestors.add(identity)
        try:
            if kind is dict:
                return all(type(key) is str and json_native(item, ancestors)
                           for key, item in value.items())
            return all(json_native(item, ancestors) for item in value)
        finally:
            ancestors.remove(identity)

    result = []
    names = set()
    if type(tools) is not list:
        invalid()
    for tool in tools:
        if type(tool) is not dict or set(tool) - {
            "name", "description", "parameters", "capabilities", "read_only", "version", "tags"
        }:
            invalid()
        name = tool.get("name")
        description = tool.get("description", "")
        parameters = tool.get("parameters", [])
        if (type(name) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                or name in names or type(description) is not str or type(parameters) is not list):
            invalid()
        names.add(name)
        properties = {}
        required = []
        for param in parameters:
            if type(param) is not dict or set(param) - {
                "name", "type", "description", "required", "default", "choices",
                "max_length", "pattern", "minimum", "maximum"
            }:
                invalid()
            key, kind = param.get("name"), param.get("type")
            if (type(key) is not str or not key or key in properties
                    or type(kind) is not str or kind not in {"string", "integer", "number", "boolean", "array", "object"}
                    or type(param.get("required", True)) is not bool
                    or type(param.get("description", "")) is not str):
                invalid()
            schema = {"type": kind, "description": param.get("description", "")}
            if kind == "array":
                schema["items"] = {}  # ToolParameter imposes no item contract
            length = param.get("max_length")
            if length is not None:
                if kind != "string" or type(length) is not int or length < 0:
                    invalid()
                schema["maxLength"] = length
            pattern = param.get("pattern")
            if pattern is not None:
                if (kind != "string" or type(pattern) is not str or not pattern.isascii()
                        or "(?" in pattern or re.search(r"\\(?![xunrtfv])\w", pattern)
                        or re.search(r"(?:[+*?]|\})\+", pattern)):
                    invalid()
                try:
                    re.compile(pattern)
                except (re.error, RecursionError):
                    invalid()
                # JSON Schema searches; AgentCore uses re.fullmatch. The end
                # assertion also refuses a trailing newline, unlike JS '$'.
                schema["pattern"] = "^(?:" + pattern + r")(?![\s\S])"
            for bound in ("minimum", "maximum"):
                value = param.get(bound)
                if value is not None:
                    if kind not in {"integer", "number"} or not finite(value):
                        invalid()
                    schema[bound] = value
            if schema.get("minimum", -math.inf) > schema.get("maximum", math.inf):
                invalid()
            choices = param.get("choices")
            if choices is not None:
                try:
                    native = json_native(choices)
                except RecursionError:
                    native = False
                if type(choices) is not list or not native or any(not matches_type(choice, kind) for choice in choices):
                    invalid()
                if choices:  # empty choices have no constraint in ToolParameter
                    schema["enum"] = choices.copy()
            if param.get("default") is not None:
                default = param["default"]
                try:
                    native = json_native(default)
                except RecursionError:
                    native = False
                if (not native or not matches_type(default, kind) or (choices and default not in choices)
                        or (length is not None and len(default) > length)
                        or (pattern is not None and re.fullmatch(pattern, default) is None)
                        or ("minimum" in schema and default < schema["minimum"])
                        or ("maximum" in schema and default > schema["maximum"])):
                    invalid()
                schema["default"] = default
            try:
                schema = json.loads(json.dumps(schema, allow_nan=False))
            except (TypeError, ValueError, RecursionError):
                invalid()
            properties[key] = schema
            if param.get("required", True):
                required.append(key)
        result.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}})
    return result


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
        clock: Any | None = None,
    ) -> None:
        super().__init__(config)
        # Honour the configured attempt budget unless a policy is injected.
        policy = retry or RetryPolicy(max_attempts=self.config.max_retries)
        self.client = client or HttpClient(
            transport=transport, retry=policy, clock=clock)
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
            "messages": _wire_messages(request.messages),
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
            payload["tools"] = _function_tools(request.tools)
        if request.response_format:
            payload["response_format"] = request.response_format
        return payload

    # -- execution ---------------------------------------------------------
    def generate(self, request: ModelRequest, *,
                 context: Any | None = None,
                 clock: Any | None = None) -> ModelResponse:
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
                self.config.timeout_seconds, secrets=secrets,
                context=context, clock=clock)
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

    def stream(
        self,
        request: ModelRequest,
        *,
        context: OperationContext,
        clock: Clock,
    ) -> Iterator[ModelStreamEvent]:
        """Yield one trusted, provider-neutral OpenAI-compatible SSE stream.

        Each call to ``HttpClient.open_stream`` below is exactly one physical
        attempt.  This provider—not the transport—tracks whether semantic
        output reached the consumer and is therefore the sole retry owner.
        """
        if not isinstance(context, StreamRequestContext):
            raise ModelResponseError(
                "native model streaming requires a trusted request context")
        if not hasattr(clock, "now") or not hasattr(clock, "monotonic"):
            raise ModelResponseError(
                "native model streaming requires a trusted clock")
        if not isinstance(request, ModelRequest):
            raise ModelResponseError(
                "native model streaming requires a ModelRequest")

        try:
            logical_started = float(clock.monotonic())
        except Exception:
            raise ModelResponseError(
                "native model streaming clock was invalid") from None
        if not math.isfinite(logical_started):
            raise ModelResponseError(
                "native model streaming clock was invalid")

        # Keep setup lazy until trusted cancellation has had the first say. A
        # no-wire preflight failure is not a native attempt and therefore must
        # not expose an attempt ID or neutral event.
        self._raise_for_trusted_cancellation(context, clock)
        model_hint = request.model or self.config.name
        payload: dict[str, Any] | None = None
        headers: dict[str, str] | None = None
        secrets: tuple[str, ...] = ()
        # The frozen native-stream budget is the existing model setting.
        # An injected client policy supplies backoff/jitter only; its opaque
        # one-shot attempt count does not redefine physical stream truth.
        max_attempts = self.config.max_retries
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) \
                or max_attempts < 1:
            raise ModelConfigError(
                "stream retry policy requires at least one attempt")

        semantic_output_emitted = False
        attempt_number = 0
        while attempt_number < max_attempts:
            # Everything through the final trusted guard is preflight.  Any
            # exception here crosses no native event boundary because no
            # physical transport seam has been invoked.
            self._raise_for_trusted_cancellation(context, clock)
            if payload is None:
                payload = self.build_payload(request)
                payload["stream"] = True
            if headers is None:
                headers, secrets = self._auth_headers()
            timeout = self._effective_stream_timeout(context, clock)
            self._guard_stream_transport_invocation(context, clock)

            # Allocate immediately before the physical seam.  It remains
            # unobservable until that seam has actually been invoked.
            attempt_number += 1
            attempt_id = new_id("att")
            sequence = 0

            def envelope() -> StreamEventEnvelope:
                nonlocal sequence
                value = StreamEventEnvelope(
                    turn_id=context.turn_id,
                    session_id=context.session_id,
                    generation=context.generation,
                    operation_id=context.operation_id,
                    request_id=context.request_id,
                    attempt_id=attempt_id,
                    sequence=sequence,
                )
                sequence += 1
                return value

            handle = None
            registration = None
            attempt_error: Exception | None = None
            cleanup_error: Exception | None = None
            success: _AttemptSuccess | None = None
            try:
                try:
                    handle = self.client.open_stream(
                        self.endpoint,
                        payload,
                        headers,
                        timeout,
                        secrets=secrets,
                    )
                except Exception as exc:
                    # Invocation itself is the physical-attempt boundary, so
                    # even an immediate ordinary failure owns this attempt ID.
                    attempt_error = exc

                if attempt_error is None:
                    try:
                        assert handle is not None
                        registration = context.cancellation.register(
                            lambda _reason, owned=handle: owned.close())
                        # Registration closes an already-cancelled handle
                        # synchronously. Recheck before Started is observable.
                        self._raise_for_trusted_cancellation(context, clock)
                    except Exception as exc:
                        attempt_error = exc
                        # A failed registration or post-registration trusted
                        # check must not leave an unowned live response while
                        # the consumer holds backpressure on Started.
                        if handle is not None:
                            handle.close()

                # The physical invocation is now established.  This is the
                # first observable neutral event even when open/registration
                # immediately failed.
                yield StreamStarted(
                    envelope=envelope(),
                    model=model_hint,
                    provider=self.name,
                )

                if attempt_error is None:
                    assert handle is not None
                    attempt = self._iter_stream_attempt(
                        handle=handle,
                        context=context,
                        clock=clock,
                        logical_started=logical_started,
                        model_hint=model_hint,
                        envelope=envelope,
                    )
                    try:
                        while True:
                            try:
                                event = next(attempt)
                            except StopIteration as stopped:
                                success = stopped.value
                                break
                            if self._is_semantic_event(event):
                                semantic_output_emitted = True
                            yield event
                    except Exception as exc:
                        attempt_error = exc
                    finally:
                        try:
                            attempt.close()
                        except Exception as close_error:
                            if attempt_error is None:
                                attempt_error = close_error
            finally:
                try:
                    if registration is not None:
                        try:
                            registration.close()
                        except Exception as exc:
                            cleanup_error = exc
                finally:
                    # Registration cleanup is not trusted to succeed. The
                    # physical handle must still close for ordinary failures
                    # and control-flow BaseExceptions alike.
                    if handle is not None:
                        handle.close()

            if attempt_error is None and cleanup_error is not None:
                attempt_error = cleanup_error

            if attempt_error is not None:
                # Cleanup has completed before any terminal becomes visible.
                # Trusted winner truth outranks I/O/parser/orchestration shape.
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return
                if isinstance(attempt_error, _TrustedStreamCancellation):
                    # A transport may import and fabricate the private signal;
                    # only the trusted state check above grants authority.
                    attempt_error = ModelResponseError(
                        "native stream cancellation lacked a trusted winner")

                retryable = isinstance(attempt_error, ModelError) and \
                    bool(attempt_error.retryable)
                delay = self._stream_retry_delay(
                    attempt_error, attempt_number) if retryable else None
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return
                remaining = self._remaining_stream_deadline(context, clock)
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return

                retry_eligible = (
                    retryable
                    and not semantic_output_emitted
                    and attempt_number < max_attempts
                    and delay is not None
                    and delay < remaining
                )
                error_kind = self._safe_error_kind(attempt_error)
                if not retry_eligible:
                    yield StreamFailed(
                        envelope=envelope(),
                        error_kind=error_kind,
                        retryable=retryable,
                        will_retry=False,
                    )
                    return

                assert delay is not None
                try:
                    self._log.warning(
                        Events.MODEL_RETRY,
                        provider=self.name,
                        model=model_hint,
                        attempt=attempt_number,
                        max_attempts=max_attempts,
                        error=error_kind,
                        delay_seconds=round(delay, 3),
                    )
                except Exception:
                    # Categorical best-effort observability cannot alter retry
                    # eligibility or expose provider content.
                    pass

                # Logging may synchronously block. Revalidate both authority
                # and the still-unpaid delay immediately before sleeping.
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return
                remaining = self._remaining_stream_deadline(context, clock)
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return
                if delay >= remaining:
                    yield StreamFailed(
                        envelope=envelope(),
                        error_kind=error_kind,
                        retryable=retryable,
                        will_retry=False,
                    )
                    return

                try:
                    self.client.sleep(delay)
                except Exception as sleep_error:
                    reason = self._trusted_cancellation_reason(context, clock)
                    if reason is not None:
                        yield StreamCancelled(
                            envelope=envelope(), reason=reason)
                        return
                    if isinstance(sleep_error, _TrustedStreamCancellation):
                        sleep_error = ModelResponseError(
                            "native stream cancellation lacked a trusted "
                            "winner")
                    yield StreamFailed(
                        envelope=envelope(),
                        error_kind=self._safe_error_kind(sleep_error),
                        retryable=(
                            isinstance(sleep_error, ModelError)
                            and bool(sleep_error.retryable)
                        ),
                        will_retry=False,
                    )
                    return

                # The retry promise crosses the event boundary only after its
                # mandatory delay has been consumed and authority revalidated.
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return
                yield StreamFailed(
                    envelope=envelope(),
                    error_kind=error_kind,
                    retryable=retryable,
                    will_retry=True,
                )
                # Backpressure is preserved here: loop preflight and physical
                # retry invocation occur only on the next consumer pull.
                continue

            if not isinstance(success, _AttemptSuccess):
                # This is still a failure of the real physical attempt whose
                # Started event was exposed above.
                yield StreamFailed(
                    envelope=envelope(),
                    error_kind="ModelResponseError",
                    retryable=False,
                    will_retry=False,
                )
                return

            # The successful HTTP resource and cancellation registration are
            # already closed.  Final tool completions and ResponseCompleted are
            # therefore safe even if the consumer pauses on a terminal event.
            for spec in success.pending:
                reason = self._trusted_cancellation_reason(context, clock)
                if reason is not None:
                    yield StreamCancelled(
                        envelope=envelope(), reason=reason)
                    return
                event = self._event_from_spec(spec, envelope())
                if self._is_semantic_event(event):
                    semantic_output_emitted = True
                yield event

            reason = self._trusted_cancellation_reason(context, clock)
            if reason is not None:
                yield StreamCancelled(envelope=envelope(), reason=reason)
                return
            yield ResponseCompleted(
                envelope=envelope(), response=success.response)
            return

    def _guard_stream_transport_invocation(
        self,
        context: StreamRequestContext,
        clock: Clock,
    ) -> None:
        """Perform the final trusted check immediately before physical open."""
        self._raise_for_trusted_cancellation(context, clock)

    def _iter_stream_attempt(
        self,
        *,
        handle: Any,
        context: StreamRequestContext,
        clock: Clock,
        logical_started: float,
        model_hint: str,
        envelope: Any,
    ) -> Generator[ModelStreamEvent, None, _AttemptSuccess]:
        """Normalize one already-opened physical HTTP/SSE attempt."""
        normalizer = _OpenAIStreamNormalizer(model_hint=model_hint)
        parser = iter(iter_sse_data(handle))
        while True:
            self._raise_for_trusted_cancellation(context, clock)
            try:
                data = next(parser)
            except StopIteration:
                raise ModelResponseError(
                    "provider stream ended before explicit [DONE]") from None
            self._raise_for_trusted_cancellation(context, clock)

            if data == "[DONE]":
                self._raise_for_trusted_cancellation(context, clock)
                latency_ms = self._stream_latency_ms(
                    logical_started, clock)
                pending, response = normalizer.finalize(
                    latency_ms=latency_ms)
                self._raise_for_trusted_cancellation(context, clock)
                return _AttemptSuccess(
                    pending=tuple(pending), response=response)

            specs = normalizer.accept_data(data)
            self._raise_for_trusted_cancellation(context, clock)
            for spec in specs:
                self._raise_for_trusted_cancellation(context, clock)
                yield self._event_from_spec(spec, envelope())

    @staticmethod
    def _event_from_spec(spec: _EventSpec,
                         envelope: StreamEventEnvelope) -> ModelStreamEvent:
        if isinstance(spec, _TextSpec):
            return TextDelta(envelope=envelope, text=spec.text)
        if isinstance(spec, _ToolDeltaSpec):
            return ToolCallDelta(
                envelope=envelope,
                index=spec.index,
                call_id=spec.call_id,
                name_fragment=spec.name_fragment,
                arguments_fragment=spec.arguments_fragment,
            )
        if isinstance(spec, _ToolCompletedSpec):
            return ToolCallCompleted(
                envelope=envelope,
                index=spec.index,
                # The normalizer created this as a detached presentation copy
                # before adopting any candidate state.
                call=spec.call,
            )
        if isinstance(spec, _UsageSpec):
            return UsageUpdated(envelope=envelope, usage=spec.usage)
        raise ModelResponseError(
            "provider stream produced an invalid neutral event")

    @staticmethod
    def _is_semantic_event(event: ModelStreamEvent) -> bool:
        if isinstance(event, TextDelta):
            return bool(event.text)
        if isinstance(event, ToolCallDelta):
            return bool(event.call_id or event.name_fragment or
                        event.arguments_fragment)
        return isinstance(event, ToolCallCompleted)

    @staticmethod
    def _trusted_cancellation_reason(
        context: StreamRequestContext,
        clock: Clock,
    ) -> CancellationReason | None:
        def requested_reason() -> CancellationReason | None:
            try:
                requested = bool(context.cancellation.requested)
            except Exception:
                raise ModelResponseError(
                    "trusted cancellation state was invalid") from None
            if not requested:
                return None
            try:
                reason = context.cancellation.reason
            except Exception:
                raise ModelResponseError(
                    "trusted cancellation state was invalid") from None
            if not isinstance(reason, CancellationReason):
                raise ModelResponseError(
                    "trusted cancellation lacked a valid reason")
            return reason

        reason = requested_reason()
        if reason is not None:
            return reason
        try:
            expired = bool(context.deadline.expired(clock))
        except Exception:
            # Fail closed in the trusted clock/deadline domain.
            expired = True
        # Cancellation may linearize while an expired/unexpired deadline clock
        # read is in flight. Recheck in both cases before authorizing any late
        # frame/event or assigning deadline truth.
        reason = requested_reason()
        if reason is not None:
            return reason
        if expired:
            return CancellationReason.DEADLINE_EXCEEDED
        return None

    @classmethod
    def _raise_for_trusted_cancellation(
        cls,
        context: StreamRequestContext,
        clock: Clock,
    ) -> None:
        reason = cls._trusted_cancellation_reason(context, clock)
        if reason is not None:
            raise _TrustedStreamCancellation(reason)

    @classmethod
    def _remaining_stream_deadline(
        cls,
        context: StreamRequestContext,
        clock: Clock,
    ) -> float:
        reason = cls._trusted_cancellation_reason(context, clock)
        if reason is not None:
            return 0.0
        try:
            remaining = float(context.deadline.remaining(clock))
        except Exception:
            return 0.0
        # Cancellation/deadline may linearize while ``remaining`` reads its
        # clock.  Recheck before the value can authorize a wire attempt,
        # retry promise, or backoff.
        if cls._trusted_cancellation_reason(context, clock) is not None:
            return 0.0
        if math.isinf(remaining) and remaining > 0:
            return math.inf
        if not math.isfinite(remaining) or remaining <= 0:
            return 0.0
        return remaining

    def _effective_stream_timeout(
        self,
        context: StreamRequestContext,
        clock: Clock,
    ) -> float:
        self._raise_for_trusted_cancellation(context, clock)
        remaining = self._remaining_stream_deadline(context, clock)
        configured = self.config.timeout_seconds
        if isinstance(configured, bool) or \
                not isinstance(configured, (int, float)) or \
                not math.isfinite(float(configured)) or configured <= 0:
            raise ModelConfigError(
                "model.timeout_seconds must be a positive finite number")
        timeout = min(float(configured), remaining)
        if not math.isfinite(timeout) and math.isinf(remaining):
            timeout = float(configured)
        if timeout <= 0:
            raise _TrustedStreamCancellation(
                CancellationReason.DEADLINE_EXCEEDED)
        return timeout

    def _stream_retry_delay(self, exc: Exception,
                            attempt_number: int) -> float | None:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None and not isinstance(retry_after, bool) and \
                isinstance(retry_after, (int, float)):
            candidate = float(retry_after)
            if math.isfinite(candidate) and candidate >= 0:
                return candidate
        try:
            candidate = float(self.client.retry.backoff_for(attempt_number))
        except Exception:
            return None
        if not math.isfinite(candidate) or candidate < 0:
            return None
        return candidate

    @staticmethod
    def _safe_error_kind(exc: BaseException) -> str:
        kind = type(exc).__name__
        return kind if _SAFE_ERROR_KIND.fullmatch(kind) else "StreamError"

    @staticmethod
    def _stream_latency_ms(started: float, clock: Clock) -> float:
        try:
            elapsed = float(clock.monotonic()) - started
        except Exception:
            raise ModelResponseError(
                "native model streaming clock was invalid") from None
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ModelResponseError(
                "native model streaming clock was invalid")
        return elapsed * 1000.0

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
