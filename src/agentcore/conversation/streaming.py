"""Owned synchronous conversation consumption of neutral model streams.

This module is imported lazily only for explicit streaming turns.  Keeping it
out of ``conversation.runtime``'s import graph preserves the legacy guarantee
that a non-realtime conversation does not import or construct realtime state.

The authority order for every event is deliberately visible in one loop:

    accumulator.accept(event) -> fence.check() -> presentation

Presentation is synchronous and provisional.  It never selects the canonical
response, executes a tool, mutates a turn, or persists transcript state.
"""

from __future__ import annotations

import inspect
import math
import re
from dataclasses import dataclass
from typing import Any, Callable

from agentcore.conversation.errors import (
    ConversationValidationError,
    ResponseGenerationError,
)
from agentcore.models.base import _accepts_kwarg
from agentcore.models.messages import ModelRequest, ModelResponse
from agentcore.models.stream_context import StreamRequestContext
from agentcore.models.streaming import (
    ModelStreamEvent,
    ResponseCompleted,
    StreamAccumulator,
    StreamCancelled,
    StreamFailed,
    TextDelta,
    iter_one_shot_events,
)
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger
from agentcore.realtime.turn import OperationContext
from agentcore.util.clock import Clock
from agentcore.util.ids import new_id


StreamEventSink = Callable[[ModelStreamEvent], None]
_SAFE_CATEGORY = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def _exception_kind(exc: BaseException) -> str:
    """Return only a bounded categorical type, never exception text."""
    return _type_kind(exc)


def _type_kind(value: Any) -> str:
    """Sanitize one type name before it can enter observability."""
    kind = type(value).__name__
    return kind if _SAFE_CATEGORY.fullmatch(kind) else "StreamError"


def _stream_failure(exc: BaseException) -> ResponseGenerationError:
    """Normalize an untrusted stream exception without retaining its text."""
    return ResponseGenerationError(
        "the model stream failed",
        error_type=_exception_kind(exc),
    )


def _trusted_native_stream(provider: Any) -> Callable[..., Any] | None:
    """Return a native stream only when both trusted keywords are explicit.

    ``**kwargs`` and positional-only parameters do not count.  Detection does
    not invoke the provider, so an internal ``TypeError`` can never trigger a
    speculative compatibility call.
    """
    stream = getattr(provider, "stream", None)
    if not callable(stream):
        return None
    if not _accepts_kwarg(stream, "context") or \
            not _accepts_kwarg(stream, "clock"):
        return None
    return stream


@dataclass
class TurnStreamState:
    """Turn-wide presentation and first-token state.

    One instance spans every model/tool/model round in the owned turn.  A sink
    exception therefore disables presentation for all later events and model
    rounds, while validation, fencing, provider consumption and finalization
    continue normally.
    """

    sink: StreamEventSink
    logger: StructuredLogger
    started_at: float
    session_key: str
    enabled: bool = True
    first_token_emitted: bool = False
    sink_calls: int = 0
    delivered_events: int = 0

    @classmethod
    def for_owned_turn(cls, sink: StreamEventSink, *, fence: Any,
                       logger: StructuredLogger) -> TurnStreamState:
        """Validate ownership before any streaming-specific turn mutation."""
        if not callable(sink):
            raise ConversationValidationError(
                "on_stream_event must be callable")
        sink_call = getattr(sink, "__call__", None)
        if inspect.iscoroutinefunction(sink) or \
                inspect.iscoroutinefunction(sink_call):
            raise ConversationValidationError(
                "on_stream_event must be synchronous")
        context = fence.operation_context()
        if not isinstance(context, OperationContext):
            raise ConversationValidationError(
                "streaming requires an owned realtime turn")
        fence.check()
        clock = fence.clock
        started_at = float(clock.monotonic())
        if not math.isfinite(started_at):
            raise ConversationValidationError(
                "streaming requires a valid monotonic clock")
        return cls(
            sink=sink,
            logger=logger,
            started_at=started_at,
            session_key=context.session_id,
        )

    def present(self, event: ModelStreamEvent, *, clock: Clock) -> None:
        """Observe first-token shape, then synchronously call the sink.

        The caller has already accepted the event and passed ``fence.check``.
        No payload text or tool fragment is copied into observability.
        """
        envelope = event.envelope
        if isinstance(event, TextDelta) and event.text and \
                not self.first_token_emitted:
            self.first_token_emitted = True
            elapsed = float(clock.monotonic()) - self.started_at
            latency_ms = elapsed * 1000.0 \
                if math.isfinite(elapsed) and elapsed >= 0 else 0.0
            self.logger.info(
                Events.TURN_FIRST_TOKEN,
                turn_id=envelope.turn_id,
                session_key=envelope.session_id,
                generation=envelope.generation,
                operation_id=envelope.operation_id,
                request_id=envelope.request_id,
                sequence=envelope.sequence,
                latency_ms=round(latency_ms, 3),
                text_chars=len(event.text),
            )

        if not self.enabled:
            return

        self.sink_calls += 1
        try:
            self.sink(event)
        except Exception as exc:  # ordinary presentation failure is isolated
            self.enabled = False
            self.logger.warning(
                Events.CONVERSATION_FAILED,
                stage="stream_sink",
                error=_exception_kind(exc),
                turn_id=envelope.turn_id,
                session_key=envelope.session_id,
                generation=envelope.generation,
                operation_id=envelope.operation_id,
                request_id=envelope.request_id,
                event_type=_type_kind(event),
                sink_calls=self.sink_calls,
                delivered_events=self.delivered_events,
            )
        else:
            self.delivered_events += 1


def consume_stream_response(
    provider: Any,
    request: ModelRequest,
    *,
    context: OperationContext,
    clock: Clock,
    fence: Any,
    state: TurnStreamState,
    max_text_chars: int,
) -> ModelResponse:
    """Consume one logical model round and return only its canonical result.

    Exactly one fresh request ID is created here for the logical round.  A
    trusted native stream receives it through ``StreamRequestContext``; an
    invalid or absent native method uses the merged one-shot adapter with one
    fresh compatibility attempt ID.  The conversation layer performs no
    retry of either path.
    """
    if not isinstance(context, OperationContext):
        raise ResponseGenerationError(
            "the model stream lacked trusted turn ownership",
            error_type="MissingOperationContext")

    request_id = new_id("req")
    accumulator = StreamAccumulator(
        context=context,
        request_id=request_id,
        max_text_chars=max_text_chars,
    )

    try:
        native_stream = _trusted_native_stream(provider)
        if native_stream is not None:
            stream_context = StreamRequestContext.from_operation(
                context, request_id=request_id)
            events = native_stream(
                request, context=stream_context, clock=clock)
        else:
            attempt_id = new_id("att")
            events = iter_one_shot_events(
                provider,
                request,
                context=context,
                clock=clock,
                request_id=request_id,
                attempt_id=attempt_id,
            )
        iterator = iter(events)
    except Exception as exc:
        # A provider cannot gain turn-cancellation authority by raising the
        # conversation layer's internal TurnSuperseded signal itself.
        raise _stream_failure(exc) from None

    # Do not prefetch or pull event N+1 until validation, currentness and
    # synchronous presentation of event N have all completed.  The consumer
    # explicitly owns iterator abandonment: relying on refcount-driven
    # generator destruction can leave a provider HTTP handle registered after
    # accumulator or fence failure on other Python runtimes.
    try:
        while True:
            try:
                event = next(iterator)
            except StopIteration:
                break
            except Exception as exc:
                raise _stream_failure(exc) from None

            try:
                canonical_event = accumulator.accept(event)
            except Exception as exc:
                raise _stream_failure(exc) from None

            # This call is deliberately outside every untrusted/provider
            # exception normalization scope. Only this trusted fence may
            # surface TurnSuperseded as durable cancellation/deadline truth.
            fence.check()
            if isinstance(canonical_event, StreamCancelled):
                # The fence just proved that trusted turn state established no
                # cancellation at this boundary. Provider output cannot
                # manufacture cancellation truth or reach presentation.
                raise ResponseGenerationError(
                    "the model stream failed",
                    error_type="StreamValidationError",
                )
            # ``accept`` returns an exact base neutral event detached from both
            # the provider and accumulator-owned state.
            try:
                state.present(canonical_event, clock=clock)
            except Exception as exc:
                raise _stream_failure(exc) from None
    finally:
        close_iterator = getattr(iterator, "close", None)
        if callable(close_iterator):
            try:
                close_iterator()
            except Exception:
                # Resource cleanup must not replace the already-truthful stream
                # or fence outcome, and arbitrary provider text is discarded.
                pass

    try:
        accumulator.finish()
    except Exception as exc:
        raise _stream_failure(exc) from None
    # The iterator may have exhausted after a terminal while a successor was
    # admitted.  Revalidate before interpreting terminal authority.
    fence.check()

    terminal = accumulator.terminal_event
    if isinstance(terminal, ResponseCompleted):
        response = accumulator.response
        if response is None:  # pragma: no cover - accumulator invariant
            raise ResponseGenerationError(
                "the model stream failed",
                error_type="MissingCanonicalResponse")
        return response
    if isinstance(terminal, StreamCancelled):
        raise ResponseGenerationError(
            "the model stream failed",
            error_type="StreamValidationError",
        )
    if isinstance(terminal, StreamFailed):
        raise ResponseGenerationError(
            "the model stream failed",
            error_type=terminal.error_kind,
        )
    raise ResponseGenerationError(  # pragma: no cover - exhaustive union
        "the model stream failed", error_type="InvalidStreamTerminal")
