"""Provider-neutral streaming foundation (Task 42.5a).

This module implements the frozen vocabulary and validation described by the
"Task 42.5 streaming implementation amendment" in
``docs/architecture/task-42.1-realtime-turn-lifecycle.md``. It is the first of
three separately reviewed Stage 4 slices:

* **42.5a (this module)** — immutable neutral event schemas, the trusted
  envelope and its validation, the provider-neutral accumulator/state
  machine, attempt/retry invariants, and a one-shot ``generate``/``complete``
  compatibility adapter.
* 42.5b — conversation stream integration (not implemented here).
* 42.5c — OpenAI-compatible real streaming (not implemented here).

Nothing in this module performs network I/O, executes a tool, mutates a
``TurnRecord``, appends to a ``ConversationSession``, or grants permission. It
is a pure, deterministic set of data/state-machine primitives that sits
between a provider and (in a later slice) presentation.

Every event exposed here is immutable. Every stream event carries a trusted
:class:`StreamEventEnvelope` whose ``turn_id``, ``session_id``, ``generation``
and ``operation_id`` come from a caller-supplied, already-validated
:class:`~agentcore.realtime.turn.OperationContext` — never from provider
output. Provider events never establish trusted turn identity by being first.
"""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from typing import Any, Iterator

from agentcore.models.base import _accepts_kwarg
from agentcore.models.errors import ModelError
from agentcore.models.messages import (
    FinishReason,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)
from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.turn import OperationContext
from agentcore.util.clock import Clock

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StreamValidationError(ModelError):
    """A stream event, envelope, or accumulator invariant was violated.

    Fail-closed: raised instead of repairing, renumbering, guessing, or
    otherwise forgiving a malformed or out-of-order event.
    """


class StreamAdapterError(ModelError):
    """The one-shot compatibility adapter could not proceed safely."""


# ---------------------------------------------------------------------------
# Internal bounds (Task 42.5 amendment, "Initial internal bounds")
# ---------------------------------------------------------------------------

#: Streamed tool calls per model response.
MAX_STREAM_TOOL_CALLS = 32
#: Accumulated function name per call, in characters.
MAX_STREAM_FUNCTION_NAME_CHARS = 256
#: Accumulated argument string per call, in characters.
MAX_STREAM_ARGUMENT_CHARS_PER_CALL = 65_536
#: Total argument-fragment characters per model response.
MAX_STREAM_ARGUMENT_CHARS_TOTAL = 262_144
#: Raw SSE frame ceiling, in bytes. Groundwork only: 42.5a implements no SSE
#: handling. Defined here so 42.5c inherits a single named constant instead
#: of a second hardcoded value.
MAX_RAW_SSE_FRAME_BYTES = 262_144

# Provisional and final assistant text has no new stream-specific ceiling:
# the existing ``conversation.max_message_chars`` is authoritative. Rather
# than duplicate that config key here, :class:`StreamAccumulator` takes an
# explicit ``max_text_chars`` from its caller so a later slice can pass the
# existing configured value without this module knowing the config schema.

# ---------------------------------------------------------------------------
# Shape validation helpers
# ---------------------------------------------------------------------------

#: Opaque trusted identifiers (turn/session/operation/request/attempt ids)
#: share one conservative safe charset. This mirrors — without importing —
#: the private link-id convention already enforced by
#: :class:`agentcore.realtime.turn.OperationContext`, since request_id and
#: attempt_id are new vocabulary introduced by this task and have no
#: existing validator to reuse.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: Categorical error kinds are bounded, safe identifiers — never free text,
#: never an exception message.
_SAFE_ERROR_KIND = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def _require_safe_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise StreamValidationError(f"{field_name} is not a valid opaque id")
    return value


def _require_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StreamValidationError(
            f"{field_name} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StreamValidationError(f"{field_name} must be a positive integer")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise StreamValidationError(f"{field_name} must be a boolean")
    return value


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise StreamValidationError(f"{field_name} must be a string")
    return value


def _detach_json_like(value: Any) -> Any:
    try:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, list):
            return [_detach_json_like(item) for item in value]
        if isinstance(value, dict):
            copied: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise StreamValidationError(
                        "tool call arguments must have string keys")
                copied[key] = _detach_json_like(item)
            return copied
        raise StreamValidationError(
            "tool call arguments must contain only JSON-like values")
    except RecursionError as exc:
        raise StreamValidationError(
            "tool call arguments must be acyclic JSON-like values") from exc


def _detach_usage_field(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StreamValidationError(
            f"response.usage.{field_name} must be a non-negative integer")
    return value


def _detach_usage(usage: Any) -> Usage:
    if not isinstance(usage, Usage):
        raise StreamValidationError("response.usage must be a Usage")
    prompt_tokens = _detach_usage_field(usage.prompt_tokens, "prompt_tokens")
    completion_tokens = _detach_usage_field(
        usage.completion_tokens, "completion_tokens")
    total_tokens = _detach_usage_field(usage.total_tokens, "total_tokens")
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _detach_latency_ms(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StreamValidationError(
            "response.latency_ms must be a non-negative finite number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise StreamValidationError(
            "response.latency_ms must be a non-negative finite number")
    return numeric


def _detach_tool_call(call: Any) -> ToolCall:
    if not isinstance(call, ToolCall):
        raise StreamValidationError("tool_calls items must be ToolCall values")
    if not isinstance(call.id, str):
        raise StreamValidationError("tool call id must be a string")
    if not isinstance(call.name, str):
        raise StreamValidationError("tool call name must be a string")
    if len(call.name) > MAX_STREAM_FUNCTION_NAME_CHARS:
        raise StreamValidationError(
            "tool call name exceeds MAX_STREAM_FUNCTION_NAME_CHARS")
    if type(call.arguments) is not dict:
        raise StreamValidationError("tool call arguments must be a dict")
    return ToolCall(
        id=call.id,
        name=call.name,
        arguments=_detach_json_like(call.arguments),
    )


def _detach_model_response(response: Any, *, max_text_chars: int | None,
                           require_empty_raw: bool) -> ModelResponse:
    if not isinstance(response, ModelResponse):
        raise StreamValidationError("response must be a ModelResponse")
    if not isinstance(response.text, str):
        raise StreamValidationError("response.text must be a string")
    if max_text_chars is not None and len(response.text) > max_text_chars:
        raise StreamValidationError(
            "response.text exceeds the configured max_text_chars")
    if not isinstance(response.model, str):
        raise StreamValidationError("response.model must be a string")
    if not isinstance(response.provider, str):
        raise StreamValidationError("response.provider must be a string")
    if not isinstance(response.finish_reason, FinishReason):
        raise StreamValidationError(
            "response.finish_reason must be a FinishReason")
    if type(response.tool_calls) is not list:
        raise StreamValidationError("response.tool_calls must be a real list")
    if len(response.tool_calls) > MAX_STREAM_TOOL_CALLS:
        raise StreamValidationError(
            "response.tool_calls exceeds MAX_STREAM_TOOL_CALLS")
    if require_empty_raw and response.raw != {}:
        raise StreamValidationError(
            "stream-safe response.raw must be exactly {}")
    return ModelResponse(
        text=response.text,
        model=response.model,
        provider=response.provider,
        finish_reason=response.finish_reason,
        usage=_detach_usage(response.usage),
        tool_calls=[_detach_tool_call(call) for call in response.tool_calls],
        raw={},
        latency_ms=_detach_latency_ms(response.latency_ms),
    )


# ---------------------------------------------------------------------------
# Trusted envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamEventEnvelope:
    """Trusted correlation carried by every neutral stream event.

    ``turn_id``, ``session_id``, ``generation`` and ``operation_id`` must
    match the trusted :class:`OperationContext` supplied by the consumer.
    ``session_id`` is the existing opaque derived realtime session key —
    never a raw ``ConversationSession.id``. ``request_id`` identifies one
    logical model request; ``attempt_id`` identifies one observable attempt
    (one physical stream attempt for native streaming, or one adapter
    attempt around one selected legacy provider invocation for one-shot
    compatibility). ``sequence`` is zero-based and exactly contiguous within
    one attempt. No value here is ever derived from provider/model text,
    arguments, or output.
    """

    turn_id: str
    session_id: str
    generation: int
    operation_id: str
    request_id: str
    attempt_id: str
    sequence: int

    def __post_init__(self) -> None:
        _require_safe_id(self.turn_id, "turn_id")
        _require_safe_id(self.session_id, "session_id")
        _require_positive_int(self.generation, "generation")
        _require_safe_id(self.operation_id, "operation_id")
        _require_safe_id(self.request_id, "request_id")
        _require_safe_id(self.attempt_id, "attempt_id")
        _require_nonnegative_int(self.sequence, "sequence")


def _require_envelope(value: Any) -> None:
    if not isinstance(value, StreamEventEnvelope):
        raise StreamValidationError("envelope must be a StreamEventEnvelope")


# ---------------------------------------------------------------------------
# Immutable neutral events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamStarted:
    """First event of every attempt. Safe provider/model metadata only."""

    envelope: StreamEventEnvelope
    model: str
    provider: str

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        _require_str(self.model, "model")
        _require_str(self.provider, "provider")


@dataclass(frozen=True)
class TextDelta:
    """Provisional text. Never persisted; presentation only."""

    envelope: StreamEventEnvelope
    text: str

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        _require_str(self.text, "text")


@dataclass(frozen=True)
class ToolCallDelta:
    """A bounded fragment of one in-progress streamed tool call.

    Never executable. An index alone (``call_id``, ``name_fragment`` and
    ``arguments_fragment`` all empty/``None``) is not semantic output.
    """

    envelope: StreamEventEnvelope
    index: int
    call_id: str | None
    name_fragment: str
    arguments_fragment: str

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        _require_nonnegative_int(self.index, "index")
        if self.call_id is not None:
            _require_str(self.call_id, "call_id")
        _require_str(self.name_fragment, "name_fragment")
        _require_str(self.arguments_fragment, "arguments_fragment")


@dataclass(frozen=True)
class ToolCallCompleted:
    """A validated neutral tool call. Never executable on its own."""

    envelope: StreamEventEnvelope
    index: int
    call: ToolCall

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        _require_nonnegative_int(self.index, "index")
        if not isinstance(self.call, ToolCall):
            raise StreamValidationError("call must be a ToolCall")


@dataclass(frozen=True)
class UsageUpdated:
    """Neutral token accounting. Nonsemantic on its own."""

    envelope: StreamEventEnvelope
    usage: Usage

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        if not isinstance(self.usage, Usage):
            raise StreamValidationError("usage must be a Usage")


@dataclass(frozen=True)
class ResponseCompleted:
    """The canonical, stream-safe successful terminal for one attempt.

    ``response.raw`` is always exactly ``{}``: no provider-shaped dictionary
    ever crosses this boundary.
    """

    envelope: StreamEventEnvelope
    response: ModelResponse

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        if not isinstance(self.response, ModelResponse):
            raise StreamValidationError("response must be a ModelResponse")
        if self.response.raw != {}:
            raise StreamValidationError(
                "stream-safe response.raw must be exactly {}")


@dataclass(frozen=True)
class StreamFailed:
    """A categorical, sanitised failure terminal for one attempt.

    ``error_kind`` is a bounded safe categorical string (typically the
    exception class name) — never an exception message, traceback, provider
    body, or header. ``will_retry=True`` is only ever valid alongside
    ``retryable=True``; the accumulator separately enforces that it may only
    be true before any semantic output has been accepted.
    """

    envelope: StreamEventEnvelope
    error_kind: str
    retryable: bool
    will_retry: bool

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        if not isinstance(self.error_kind, str) or \
                not _SAFE_ERROR_KIND.fullmatch(self.error_kind):
            raise StreamValidationError(
                "error_kind must be a bounded safe categorical string")
        _require_bool(self.retryable, "retryable")
        _require_bool(self.will_retry, "will_retry")
        if self.will_retry and not self.retryable:
            raise StreamValidationError(
                "will_retry=True requires retryable=True")


@dataclass(frozen=True)
class StreamCancelled:
    """The neutral attempt terminal for trusted logical cancellation.

    Includes deadline expiry, represented as
    ``reason=CancellationReason.DEADLINE_EXCEEDED``. This does not by itself
    mean the durable turn is ``CANCELLED``; mapping to ``TurnPhase`` is a
    later-slice/conversation-layer concern, not performed here.
    """

    envelope: StreamEventEnvelope
    reason: CancellationReason

    def __post_init__(self) -> None:
        _require_envelope(self.envelope)
        if not isinstance(self.reason, CancellationReason):
            raise StreamValidationError("reason must be a CancellationReason")


#: Union of every neutral stream event type.
ModelStreamEvent = (
    StreamStarted | TextDelta | ToolCallDelta | ToolCallCompleted |
    UsageUpdated | ResponseCompleted | StreamFailed | StreamCancelled
)

_EVENT_TYPES = (
    StreamStarted, TextDelta, ToolCallDelta, ToolCallCompleted,
    UsageUpdated, ResponseCompleted, StreamFailed, StreamCancelled,
)

#: Attempt-terminal event types (exactly one occurs, last, per attempt).
_TERMINAL_TYPES = (ResponseCompleted, StreamFailed, StreamCancelled)


# ---------------------------------------------------------------------------
# Stream-safe ModelResponse normalisation
# ---------------------------------------------------------------------------


def stream_safe_response(response: ModelResponse) -> ModelResponse:
    """Return a new stream-safe :class:`ModelResponse` with ``raw={}``.

    Preserves ``text``, ``model``, ``provider``, ``finish_reason``, ``usage``,
    ``tool_calls`` and ``latency_ms``. Never mutates ``response`` in place;
    the ``tool_calls`` list is copied rather than aliased. This boundary rule
    does not alter direct, non-streaming ``generate()`` callers, who continue
    to see the provider's original ``ModelResponse.raw``.
    """
    if not isinstance(response, ModelResponse):
        raise StreamAdapterError("expected a ModelResponse to normalise")
    return _detach_model_response(
        response,
        max_text_chars=None,
        require_empty_raw=False,
    )


# ---------------------------------------------------------------------------
# StreamAccumulator — trusted-init, fail-closed attempt/retry state machine
# ---------------------------------------------------------------------------


class StreamAccumulator:
    """Fail-closed provider-neutral validator between provider and consumer.

    Initialised from trusted runtime state — never from the first provider
    event — so a provider can never establish trusted turn/session/
    generation/operation/request identity merely by arriving first. Every
    event must match the identity this accumulator was constructed with.

    Responsibilities: trusted envelope identity, attempt lifecycle and retry
    transitions, exact contiguous sequencing, semantic-output tracking,
    bounded text/tool accumulation, and canonical final-response consistency.
    It never repairs, renumbers, infers a missing transition, forgives a
    malformed duplicate, or guesses a fragment.
    """

    def __init__(self, *, context: OperationContext, request_id: str,
                 max_text_chars: int) -> None:
        if not isinstance(context, OperationContext):
            raise StreamValidationError(
                "context must be a trusted OperationContext")
        _require_safe_id(request_id, "request_id")
        if isinstance(max_text_chars, bool) or \
                not isinstance(max_text_chars, int) or max_text_chars < 0:
            raise StreamValidationError(
                "max_text_chars must be a non-negative integer")

        self._context = context
        self._request_id = request_id
        self._max_text_chars = max_text_chars

        # Attempt/lifecycle state.
        self._started = False
        self._attempt_id: str | None = None
        self._expected_sequence = 0
        self._attempt_terminal: ModelStreamEvent | None = None
        self._logical_terminal: ModelStreamEvent | None = None
        self._pending_retry = False
        self._semantic_output_seen = False
        self._finished = False
        self._result_response: ModelResponse | None = None

        # Per-attempt accumulation. Reset on every StreamStarted (including
        # retries): a retry is only ever reachable before semantic output,
        # so there is never carried-over fragment state to preserve.
        self._text_parts: list[str] = []
        self._tool_name_fragment_chars: dict[int, int] = {}
        self._tool_argument_fragment_chars: dict[int, int] = {}
        self._total_argument_fragment_chars = 0
        self._tool_seen_index: set[int] = set()
        self._tool_completed: dict[int, ToolCall] = {}

    # -- public API ---------------------------------------------------

    def accept(self, event: ModelStreamEvent) -> None:
        """Validate and accept one event, or raise without mutating state.

        A failed call leaves no partially accepted state: every check runs
        before any commit.
        """
        if self._finished:
            raise StreamValidationError(
                "accumulator already finished; no further events accepted")
        if not isinstance(event, _EVENT_TYPES):
            raise StreamValidationError(
                "event is not a recognised neutral stream event")

        self._check_identity(event.envelope)

        if isinstance(event, StreamStarted):
            self._accept_started(event)
            return

        if not self._started or self._attempt_terminal is not None:
            raise StreamValidationError(
                "no active attempt is open to accept this event")
        if event.envelope.attempt_id != self._attempt_id:
            raise StreamValidationError(
                "attempt_id does not match the currently active attempt")
        if event.envelope.sequence != self._expected_sequence:
            raise StreamValidationError(
                "sequence is not the next contiguous value for this attempt")

        if isinstance(event, TextDelta):
            self._accept_text_delta(event)
        elif isinstance(event, ToolCallDelta):
            self._accept_tool_call_delta(event)
        elif isinstance(event, ToolCallCompleted):
            self._accept_tool_call_completed(event)
        elif isinstance(event, UsageUpdated):
            pass
        elif isinstance(event, ResponseCompleted):
            self._accept_response_completed(event)
        elif isinstance(event, StreamCancelled):
            self._attempt_terminal = event
            self._logical_terminal = event
            self._pending_retry = False
        elif isinstance(event, StreamFailed):
            self._accept_stream_failed(event)
        else:  # pragma: no cover - _EVENT_TYPES is exhaustive
            raise StreamValidationError("unhandled neutral stream event type")

        self._expected_sequence += 1

    def finish(self) -> None:
        """Finalise iteration. Raises unless a valid logical terminal state
        was reached; never relies on iterator exhaustion alone as proof.
        """
        if self._finished:
            raise StreamValidationError("finish() already called")
        self._finished = True
        if not self._started:
            raise StreamValidationError("no StreamStarted was ever accepted")
        if self._logical_terminal is None:
            if self._pending_retry:
                raise StreamValidationError(
                    "a retry was promised but no new attempt was started")
            raise StreamValidationError(
                "iteration ended without a final logical terminal outcome")

    @property
    def terminal_event(self) -> ModelStreamEvent:
        if not self._finished or self._logical_terminal is None:
            raise StreamValidationError(
                "finish() has not completed successfully")
        if isinstance(self._logical_terminal, ResponseCompleted):
            return ResponseCompleted(
                envelope=self._logical_terminal.envelope,
                response=self.response,
            )
        return self._logical_terminal

    @property
    def response(self) -> ModelResponse | None:
        if not self._finished or self._logical_terminal is None:
            raise StreamValidationError(
                "finish() has not completed successfully")
        if self._result_response is None:
            return None
        return _detach_model_response(
            self._result_response,
            max_text_chars=None,
            require_empty_raw=True,
        )

    @property
    def semantic_output_seen(self) -> bool:
        return self._semantic_output_seen

    # -- identity -------------------------------------------------------

    def _check_identity(self, envelope: StreamEventEnvelope) -> None:
        if envelope.turn_id != self._context.turn_id:
            raise StreamValidationError("envelope turn_id does not match")
        if envelope.session_id != self._context.session_id:
            raise StreamValidationError("envelope session_id does not match")
        if envelope.generation != self._context.generation:
            raise StreamValidationError("envelope generation does not match")
        if envelope.operation_id != self._context.operation_id:
            raise StreamValidationError("envelope operation_id does not match")
        if envelope.request_id != self._request_id:
            raise StreamValidationError("envelope request_id does not match")

    # -- StreamStarted / attempt admission -------------------------------

    def _accept_started(self, event: StreamStarted) -> None:
        if self._logical_terminal is not None:
            raise StreamValidationError(
                "logical request already reached a final terminal outcome")
        if self._started and self._attempt_terminal is None:
            raise StreamValidationError(
                "StreamStarted already active; attempt has no terminal yet")
        if self._started and not self._pending_retry:
            raise StreamValidationError(
                "no retry was promised for a new attempt")
        if self._started and event.envelope.attempt_id == self._attempt_id:
            raise StreamValidationError(
                "a retry attempt must use a new attempt_id")
        if event.envelope.sequence != 0:
            raise StreamValidationError("StreamStarted must be sequence 0")

        self._started = True
        self._attempt_id = event.envelope.attempt_id
        self._expected_sequence = 1
        self._attempt_terminal = None
        self._pending_retry = False

        self._text_parts = []
        self._tool_name_fragment_chars = {}
        self._tool_argument_fragment_chars = {}
        self._total_argument_fragment_chars = 0
        self._tool_seen_index = set()
        self._tool_completed = {}

    # -- text -------------------------------------------------------------

    def _accept_text_delta(self, event: TextDelta) -> None:
        if not event.text:
            return  # empty delta is nonsemantic; nothing to accumulate
        current_len = sum(len(part) for part in self._text_parts)
        if current_len + len(event.text) > self._max_text_chars:
            raise StreamValidationError(
                "text delta would exceed the configured max_text_chars")
        self._text_parts.append(event.text)
        self._semantic_output_seen = True

    # -- tools --------------------------------------------------------------

    def _accept_tool_call_delta(self, event: ToolCallDelta) -> None:
        index = event.index
        if index >= MAX_STREAM_TOOL_CALLS:
            raise StreamValidationError(
                "tool index exceeds MAX_STREAM_TOOL_CALLS")
        if index in self._tool_completed:
            raise StreamValidationError(
                "tool call delta received after that index already completed")

        if event.name_fragment:
            new_name_len = self._tool_name_fragment_chars.get(index, 0) + \
                len(event.name_fragment)
            if new_name_len > MAX_STREAM_FUNCTION_NAME_CHARS:
                raise StreamValidationError(
                    "function name fragment exceeds "
                    "MAX_STREAM_FUNCTION_NAME_CHARS")
        if event.arguments_fragment:
            new_arg_len = self._tool_argument_fragment_chars.get(index, 0) + \
                len(event.arguments_fragment)
            if new_arg_len > MAX_STREAM_ARGUMENT_CHARS_PER_CALL:
                raise StreamValidationError(
                    "argument fragment exceeds "
                    "MAX_STREAM_ARGUMENT_CHARS_PER_CALL")
            new_total = self._total_argument_fragment_chars + \
                len(event.arguments_fragment)
            if new_total > MAX_STREAM_ARGUMENT_CHARS_TOTAL:
                raise StreamValidationError(
                    "argument fragment exceeds "
                    "MAX_STREAM_ARGUMENT_CHARS_TOTAL")

        if event.name_fragment:
            self._tool_name_fragment_chars[index] = \
                self._tool_name_fragment_chars.get(index, 0) + \
                len(event.name_fragment)
        if event.arguments_fragment:
            self._tool_argument_fragment_chars[index] = \
                self._tool_argument_fragment_chars.get(index, 0) + \
                len(event.arguments_fragment)
            self._total_argument_fragment_chars += len(
                event.arguments_fragment)

        is_semantic = bool(event.call_id) or bool(event.name_fragment) or \
            bool(event.arguments_fragment)
        if is_semantic:
            self._tool_seen_index.add(index)
            self._semantic_output_seen = True

    def _accept_tool_call_completed(self, event: ToolCallCompleted) -> None:
        index = event.index
        if index >= MAX_STREAM_TOOL_CALLS:
            raise StreamValidationError(
                "tool index exceeds MAX_STREAM_TOOL_CALLS")
        if index in self._tool_completed:
            raise StreamValidationError(
                "duplicate ToolCallCompleted for the same index")
        call = _detach_tool_call(event.call)

        self._tool_completed[index] = call
        self._tool_seen_index.add(index)
        self._semantic_output_seen = True

    # -- terminals ------------------------------------------------------

    def _accept_response_completed(self, event: ResponseCompleted) -> None:
        response = _detach_model_response(
            event.response,
            max_text_chars=self._max_text_chars,
            require_empty_raw=True,
        )

        if self._text_parts:
            concatenated = "".join(self._text_parts)
            if concatenated != response.text:
                raise StreamValidationError(
                    "accumulated TextDelta text does not equal "
                    "response.text")

        incomplete = self._tool_seen_index - set(self._tool_completed)
        if incomplete:
            raise StreamValidationError(
                "streamed tool call fragments never reached "
                "ToolCallCompleted", indices=sorted(incomplete))

        if self._tool_completed:
            ordered_calls = [self._tool_completed[i]
                             for i in sorted(self._tool_completed)]
            if ordered_calls != list(response.tool_calls):
                raise StreamValidationError(
                    "completed tool calls do not match "
                    "response.tool_calls")

        terminal = ResponseCompleted(envelope=event.envelope, response=response)
        self._attempt_terminal = terminal
        self._logical_terminal = terminal
        self._result_response = response
        self._pending_retry = False

    def _accept_stream_failed(self, event: StreamFailed) -> None:
        if event.will_retry and self._semantic_output_seen:
            raise StreamValidationError(
                "cannot promise a retry after semantic output has been "
                "accepted for this logical request")

        self._attempt_terminal = event
        if event.will_retry:
            self._logical_terminal = None
            self._pending_retry = True
        else:
            self._logical_terminal = event
            self._pending_retry = False


# ---------------------------------------------------------------------------
# One-shot generate()/complete() compatibility adapter
# ---------------------------------------------------------------------------


class _Outcome:
    """Internal success/failure holder for one provider invocation."""

    __slots__ = ("response", "error")

    def __init__(self, response: ModelResponse | None,
                 error: Exception | None) -> None:
        self.response = response
        self.error = error

    @classmethod
    def success(cls, response: ModelResponse) -> "_Outcome":
        return cls(response, None)

    @classmethod
    def failure(cls, error: Exception) -> "_Outcome":
        return cls(None, error)


def _safe_provider_name(provider: Any) -> str:
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) else ""


def _safe_model_hint(provider: Any, request: ModelRequest) -> str:
    """Safe model label known before invocation.

    Prefers the request's explicit model; otherwise falls back to the
    provider's own configured model name, when that trusted configuration
    exists. Never invents a value — an empty string is used instead.
    """
    if isinstance(request.model, str) and request.model:
        return request.model
    config = getattr(provider, "config", None)
    name = getattr(config, "name", None)
    return name if isinstance(name, str) else ""


def _invoke_generate(generate: Any, request: ModelRequest, *,
                     context: OperationContext, clock: Clock) -> _Outcome:
    kwargs: dict[str, Any] = {}
    if _accepts_kwarg(generate, "context"):
        kwargs["context"] = context
    if _accepts_kwarg(generate, "clock"):
        kwargs["clock"] = clock
    try:
        result = generate(request, **kwargs)
        if not isinstance(result, ModelResponse):
            raise StreamAdapterError(
                "provider generate() did not return a ModelResponse")
        safe_result = stream_safe_response(result)
    except Exception as exc:  # noqa: BLE001 - classify, never re-invoke
        return _Outcome.failure(exc)
    return _Outcome.success(safe_result)


def _invoke_complete(complete: Any, request: ModelRequest, provider: Any, *,
                     context: OperationContext, clock: Clock) -> _Outcome:
    """Legacy ``complete()`` fallback.

    Mirrors the existing ``ConversationRuntime._call_model`` request
    rendering exactly: SYSTEM messages joined with ``"\\n\\n"``; the
    remaining messages rendered as ``"{role}: {content}"`` and likewise
    joined. A complete-only provider cannot report richer truth, so the
    resulting response uses truthful defaults rather than invented values.
    """
    kwargs: dict[str, Any] = {}
    if _accepts_kwarg(complete, "context"):
        kwargs["context"] = context
    if _accepts_kwarg(complete, "clock"):
        kwargs["clock"] = clock

    system = "\n\n".join(
        m.content for m in request.messages if m.role is Role.SYSTEM)
    body = "\n\n".join(
        f"{m.role.value}: {m.content}" for m in request.messages
        if m.role is not Role.SYSTEM)

    try:
        if _accepts_kwarg(complete, "system"):
            kwargs["system"] = system
            text = complete(body, **kwargs)
        elif kwargs:
            text = complete(body, system=system, **kwargs)
        else:
            text = complete(body, system=system)
    except Exception as exc:  # noqa: BLE001 - classify, never re-invoke
        return _Outcome.failure(exc)

    if not isinstance(text, str):
        return _Outcome.failure(
            StreamAdapterError("provider complete() did not return a str"))

    response = ModelResponse(
        text=text,
        model=_safe_model_hint(provider, request),
        provider=_safe_provider_name(provider),
        finish_reason=FinishReason.UNKNOWN,
        usage=Usage(),
        tool_calls=[],
        raw={},
    )
    return _Outcome.success(response)


def _stream_failed_from_error(envelope: StreamEventEnvelope,
                              error: BaseException) -> StreamFailed:
    """Classify a raised exception into a sanitised categorical terminal.

    Never exposes the exception message, traceback, provider body, headers,
    or credentials — only the exception's type name and (for a
    :class:`ModelError`) its existing ``retryable`` hint.
    """
    if isinstance(error, ModelError):
        return StreamFailed(
            envelope=envelope,
            error_kind=type(error).__name__,
            retryable=bool(error.retryable),
            will_retry=False,
        )
    return StreamFailed(
        envelope=envelope,
        error_kind=type(error).__name__,
        retryable=False,
        will_retry=False,
    )


def _select_terminal(envelope: StreamEventEnvelope, *,
                     context: OperationContext, clock: Clock,
                     outcome: _Outcome) -> ModelStreamEvent:
    """Choose exactly one sequence=1 terminal, without reinvoking the
    provider. Winner order: trusted cancellation, then deadline expiry,
    then success, then failure. A physically successful provider return
    after supersession/deadline never becomes ``ResponseCompleted``.
    """
    if context.cancellation.requested:
        reason = context.cancellation.reason
        if not isinstance(reason, CancellationReason):
            # Fail closed: a requested-but-reasonless token is never trusted
            # enough to construct a neutral event from.
            raise StreamAdapterError(
                "cancellation is requested but carries no valid reason")
        return StreamCancelled(envelope=envelope, reason=reason)
    if context.deadline.expired(clock):
        return StreamCancelled(
            envelope=envelope, reason=CancellationReason.DEADLINE_EXCEEDED)
    if outcome.error is None:
        assert outcome.response is not None
        return ResponseCompleted(envelope=envelope, response=outcome.response)
    return _stream_failed_from_error(envelope, outcome.error)


def iter_one_shot_events(
    provider: Any,
    request: ModelRequest,
    *,
    context: OperationContext,
    clock: Clock,
    request_id: str,
    attempt_id: str,
) -> Iterator[ModelStreamEvent]:
    """Adapt an existing one-shot ``generate``/``complete`` provider into the
    neutral streaming event vocabulary.

    Selects exactly one provider entry point using the existing one-shot
    precedence (``generate`` when callable, otherwise ``complete``) and
    invokes it exactly once. ``request_id`` and ``attempt_id`` are trusted,
    caller-owned opaque identifiers — never generated from provider output.

    Event sequence is exactly two events: ``StreamStarted(sequence=0)``, then
    one ``sequence=1`` terminal (``ResponseCompleted``, ``StreamFailed``, or
    ``StreamCancelled``) chosen only after the provider call returns or
    raises. This adapter never performs an outer retry: any retries hidden
    inside the single legacy invocation remain opaque, and no attempt events
    are fabricated for them. ``StreamFailed.will_retry`` is always ``False``
    here — the compatibility adapter itself never promises a retry.
    """
    if not isinstance(context, OperationContext):
        raise StreamAdapterError(
            "context must be a trusted OperationContext")
    if not hasattr(clock, "now") or not hasattr(clock, "monotonic"):
        raise StreamAdapterError("clock must be a trusted Clock")

    envelope0 = StreamEventEnvelope(
        turn_id=context.turn_id,
        session_id=context.session_id,
        generation=context.generation,
        operation_id=context.operation_id,
        request_id=request_id,
        attempt_id=attempt_id,
        sequence=0,
    )
    model_hint = _safe_model_hint(provider, request)
    provider_hint = _safe_provider_name(provider)
    yield StreamStarted(envelope=envelope0, model=model_hint,
                        provider=provider_hint)

    envelope1 = StreamEventEnvelope(
        turn_id=context.turn_id,
        session_id=context.session_id,
        generation=context.generation,
        operation_id=context.operation_id,
        request_id=request_id,
        attempt_id=attempt_id,
        sequence=1,
    )

    generate = getattr(provider, "generate", None)
    if callable(generate):
        outcome = _invoke_generate(generate, request, context=context,
                                   clock=clock)
    else:
        complete = getattr(provider, "complete", None)
        if not callable(complete):
            outcome = _Outcome.failure(
                StreamAdapterError(
                    "provider exposes neither generate nor complete"))
        else:
            outcome = _invoke_complete(complete, request, provider,
                                       context=context, clock=clock)

    yield _select_terminal(envelope1, context=context, clock=clock,
                           outcome=outcome)
