"""Task 42.5a — provider-neutral streaming foundation.

Deterministic tests using FixedClock and CancellationSource. No real
time.sleep(), no threads, no network, no real provider.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from agentcore.models.errors import (
    ModelAuthError,
    ModelConnectionError,
    ModelError,
    ModelResponseError,
)
from agentcore.models.messages import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)
from agentcore.models.streaming import (
    MAX_STREAM_ARGUMENT_CHARS_PER_CALL,
    MAX_STREAM_ARGUMENT_CHARS_TOTAL,
    MAX_STREAM_FUNCTION_NAME_CHARS,
    MAX_STREAM_TOOL_CALLS,
    ResponseCompleted,
    StreamAccumulator,
    StreamCancelled,
    StreamEventEnvelope,
    StreamFailed,
    StreamStarted,
    StreamValidationError,
    TextDelta,
    ToolCallCompleted,
    ToolCallDelta,
    UsageUpdated,
    iter_one_shot_events,
    stream_safe_response,
)
from agentcore.realtime.cancellation import CancellationReason, CancellationSource
from agentcore.realtime.turn import Deadline, OperationContext
from agentcore.util.clock import FixedClock
from agentcore.util.ids import new_id

T0 = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

RAW_SECRET = "RAW_PROVIDER_SECRET_42_5"
RUNTIME_SECRET = "API_SECRET_42_5"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_context(clock: FixedClock, *, remaining_seconds: float = 30.0,
                 unbounded: bool = False) -> tuple[OperationContext,
                                                    CancellationSource]:
    source = CancellationSource()
    deadline = (Deadline.unbounded() if unbounded
                else Deadline.after(remaining_seconds, clock=clock))
    ctx = OperationContext(
        turn_id=new_id("turn"),
        session_id="session_abc",
        generation=1,
        operation_id=new_id("op"),
        cancellation=source.token,
        deadline=deadline,
    )
    return ctx, source


def envelope(ctx: OperationContext, *, request_id: str, attempt_id: str,
            sequence: int) -> StreamEventEnvelope:
    return StreamEventEnvelope(
        turn_id=ctx.turn_id,
        session_id=ctx.session_id,
        generation=ctx.generation,
        operation_id=ctx.operation_id,
        request_id=request_id,
        attempt_id=attempt_id,
        sequence=sequence,
    )


def new_accumulator(ctx: OperationContext, request_id: str,
                    max_text_chars: int = 10_000) -> StreamAccumulator:
    return StreamAccumulator(context=ctx, request_id=request_id,
                             max_text_chars=max_text_chars)


class RecordingProvider:
    """Fake ``generate``-capable provider recording every call."""

    name = "fake"

    def __init__(self, response: ModelResponse | None = None,
                error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def generate(self, request: ModelRequest, *,
                context: Any | None = None,
                clock: Any | None = None) -> ModelResponse:
        self.calls.append({"request": request, "context": context,
                           "clock": clock})
        if self.error is not None:
            raise self.error
        return self.response


def basic_request() -> ModelRequest:
    return ModelRequest(messages=[Message.user("hello")])


# ---------------------------------------------------------------------------
# 24. IMMUTABILITY / SHAPES
# ---------------------------------------------------------------------------
def test_envelope_is_frozen():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=0)
    with pytest.raises(FrozenInstanceError):
        env.sequence = 1  # type: ignore[misc]


def test_every_event_type_is_frozen():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=0)
    events = [
        StreamStarted(envelope=env, model="m", provider="p"),
        TextDelta(envelope=env, text="hi"),
        ToolCallDelta(envelope=env, index=0, call_id=None,
                      name_fragment="", arguments_fragment=""),
        UsageUpdated(envelope=env, usage=Usage()),
        StreamFailed(envelope=env, error_kind="X", retryable=False,
                    will_retry=False),
        StreamCancelled(envelope=env, reason=CancellationReason.USER_INTERRUPT),
    ]
    for event in events:
        with pytest.raises(FrozenInstanceError):
            event.envelope = env  # type: ignore[misc]


def test_invalid_generation_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    for bad in (0, -1, True):
        with pytest.raises(StreamValidationError):
            StreamEventEnvelope(
                turn_id=ctx.turn_id, session_id=ctx.session_id,
                generation=bad, operation_id=ctx.operation_id,
                request_id=new_id("req"), attempt_id=new_id("att"),
                sequence=0)


def test_invalid_sequence_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    for bad in (-1, True, "0"):
        with pytest.raises(StreamValidationError):
            StreamEventEnvelope(
                turn_id=ctx.turn_id, session_id=ctx.session_id,
                generation=ctx.generation, operation_id=ctx.operation_id,
                request_id=new_id("req"), attempt_id=new_id("att"),
                sequence=bad)


def test_invalid_tool_index_rejected_bool_and_negative():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=1)
    for bad in (-1, True):
        with pytest.raises(StreamValidationError):
            ToolCallDelta(envelope=env, index=bad, call_id=None,
                         name_fragment="", arguments_fragment="")


def test_invalid_cancellation_reason_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=1)
    with pytest.raises(StreamValidationError):
        StreamCancelled(envelope=env, reason="not_a_reason")  # type: ignore[arg-type]


def test_response_completed_rejects_nonempty_raw():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=1)
    dirty = ModelResponse(text="hi", raw={"leaked": "yes"})
    with pytest.raises(StreamValidationError):
        ResponseCompleted(envelope=env, response=dirty)


def test_will_retry_requires_retryable():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=1)
    with pytest.raises(StreamValidationError):
        StreamFailed(envelope=env, error_kind="X", retryable=False,
                    will_retry=True)


# ---------------------------------------------------------------------------
# 25. ATTEMPT STATE MACHINE
# ---------------------------------------------------------------------------
def test_started_to_completed_valid():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    response = ModelResponse(text="hi")
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), response=response))
    acc.finish()
    assert acc.response == response
    assert isinstance(acc.terminal_event, ResponseCompleted)


def test_terminal_without_started_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=0), response=ModelResponse(text="hi")))


def test_started_sequence_nonzero_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    with pytest.raises(StreamValidationError):
        acc.accept(StreamStarted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=1), model="m", provider="p"))


def test_sequence_gap_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    with pytest.raises(StreamValidationError):
        acc.accept(TextDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), text="hi"))


def test_duplicate_sequence_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    with pytest.raises(StreamValidationError):
        acc.accept(StreamStarted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=0), model="m", provider="p"))


def test_second_started_in_same_attempt_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    with pytest.raises(StreamValidationError):
        acc.accept(StreamStarted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=0), model="m", provider="p"))


def test_event_after_terminal_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), response=ModelResponse(text="hi")))
    with pytest.raises(StreamValidationError):
        acc.accept(TextDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), text="more"))


def test_retry_before_semantic_output_then_new_attempt_valid():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_a, att_b = new_id("req"), new_id("att"), new_id("att")
    assert att_a != att_b
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_a,
                          sequence=0), model="m", provider="p"))
    acc.accept(StreamFailed(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_a,
                          sequence=1), error_kind="ModelConnectionError",
        retryable=True, will_retry=True))
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_b,
                          sequence=0), model="m", provider="p"))
    response = ModelResponse(text="ok")
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_b,
                          sequence=1), response=response))
    acc.finish()
    assert acc.response == response


def test_retry_using_same_attempt_id_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(StreamFailed(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), error_kind="ModelConnectionError",
        retryable=True, will_retry=True))
    with pytest.raises(StreamValidationError):
        acc.accept(StreamStarted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=0), model="m", provider="p"))


def test_retry_after_text_delta_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), text="hi"))
    with pytest.raises(StreamValidationError):
        acc.accept(StreamFailed(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), error_kind="ModelConnectionError",
            retryable=True, will_retry=True))


def test_retry_after_meaningful_tool_call_delta_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0, call_id=None,
        name_fragment="get_weather", arguments_fragment=""))
    with pytest.raises(StreamValidationError):
        acc.accept(StreamFailed(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), error_kind="ModelConnectionError",
            retryable=True, will_retry=True))


def test_retry_after_tool_call_completed_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0,
        call=ToolCall(id="call_1", name="get_weather")))
    with pytest.raises(StreamValidationError):
        acc.accept(StreamFailed(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), error_kind="ModelConnectionError",
            retryable=True, will_retry=True))


def test_will_retry_true_and_retryable_false_rejected_at_construction():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    env = envelope(ctx, request_id=new_id("req"), attempt_id=new_id("att"),
                  sequence=1)
    with pytest.raises(StreamValidationError):
        StreamFailed(envelope=env, error_kind="X", retryable=False,
                    will_retry=True)


def test_finish_after_promised_retry_without_next_attempt_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(StreamFailed(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), error_kind="ModelConnectionError",
        retryable=True, will_retry=True))
    with pytest.raises(StreamValidationError):
        acc.finish()


def test_second_logical_final_outcome_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_a = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_a,
                          sequence=0), model="m", provider="p"))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_a,
                          sequence=1), response=ModelResponse(text="hi")))
    att_b = new_id("att")
    with pytest.raises(StreamValidationError):
        acc.accept(StreamStarted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_b,
                              sequence=0), model="m", provider="p"))


# ---------------------------------------------------------------------------
# 26. TEXT
# ---------------------------------------------------------------------------
def test_empty_text_delta_is_nonsemantic():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), text=""))
    assert acc.semantic_output_seen is False


def test_nonempty_text_delta_is_semantic():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), text="hi"))
    assert acc.semantic_output_seen is True


def test_exact_deltas_concatenate_to_final_response():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), text="Hel"))
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=2), text="lo"))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=3), response=ModelResponse(text="Hello")))
    acc.finish()
    assert acc.response.text == "Hello"


def test_final_text_mismatch_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), text="Hello"))
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), response=ModelResponse(text="Goodbye")))


def test_exceeding_max_text_chars_rejected_before_acceptance():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id, max_text_chars=5)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    with pytest.raises(StreamValidationError):
        acc.accept(TextDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=1), text="123456"))
    assert acc.semantic_output_seen is False
    # Rejected before acceptance: the same sequence number is still usable.
    acc.accept(TextDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), text="12345"))
    assert acc.semantic_output_seen is True


def test_one_shot_started_then_completed_without_deltas_is_valid():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    response = ModelResponse(text="direct final text")
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), response=response))
    acc.finish()
    assert acc.response.text == "direct final text"


def test_final_response_text_bound_rejected_without_deltas_and_state_stays_put():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id, max_text_chars=5)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=1),
            response=ModelResponse(text="123456"),
        ))
    assert acc.semantic_output_seen is False
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=ModelResponse(text="12345"),
    ))
    acc.finish()
    assert acc.response.text == "12345"


# ---------------------------------------------------------------------------
# 27. TOOLS
# ---------------------------------------------------------------------------
def test_tool_index_boundaries_0_and_31_accepted():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0,
        call=ToolCall(id="call_a", name="a")))
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=2), index=31,
        call=ToolCall(id="call_b", name="b")))
    response = ModelResponse(text="", tool_calls=[
        ToolCall(id="call_a", name="a"), ToolCall(id="call_b", name="b")])
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=3), response=response))
    acc.finish()


def test_tool_index_32_rejected_by_accumulator():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    assert MAX_STREAM_TOOL_CALLS == 32
    with pytest.raises(StreamValidationError):
        acc.accept(ToolCallDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=1), index=32, call_id=None,
            name_fragment="x", arguments_fragment=""))


def test_function_name_fragment_ceiling():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0, call_id=None,
        name_fragment="x" * MAX_STREAM_FUNCTION_NAME_CHARS,
        arguments_fragment=""))
    with pytest.raises(StreamValidationError):
        acc.accept(ToolCallDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), index=0, call_id=None,
            name_fragment="y", arguments_fragment=""))


def test_arguments_per_call_ceiling():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0, call_id=None,
        name_fragment="",
        arguments_fragment="a" * MAX_STREAM_ARGUMENT_CHARS_PER_CALL))
    with pytest.raises(StreamValidationError):
        acc.accept(ToolCallDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), index=0, call_id=None,
            name_fragment="", arguments_fragment="b"))


def test_total_argument_fragment_ceiling():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    per_call = MAX_STREAM_ARGUMENT_CHARS_PER_CALL
    assert MAX_STREAM_ARGUMENT_CHARS_TOTAL == per_call * 4
    seq = 1
    for index in range(4):
        acc.accept(ToolCallDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=seq), index=index, call_id=None,
            name_fragment="", arguments_fragment="a" * per_call))
        seq += 1
    with pytest.raises(StreamValidationError):
        acc.accept(ToolCallDelta(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=seq), index=4, call_id=None,
            name_fragment="", arguments_fragment="a"))


def test_duplicate_tool_call_completed_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0,
        call=ToolCall(id="call_a", name="a")))
    with pytest.raises(StreamValidationError):
        acc.accept(ToolCallCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), index=0,
            call=ToolCall(id="call_a2", name="a")))


def test_incomplete_tool_fragments_at_response_completed_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallDelta(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0, call_id=None,
        name_fragment="get_weather", arguments_fragment=""))
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), response=ModelResponse(text="")))


def test_completed_calls_mismatch_final_tool_calls_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0,
        call=ToolCall(id="call_a", name="a")))
    mismatched = ModelResponse(text="", tool_calls=[
        ToolCall(id="different", name="b")])
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=2), response=mismatched))


def test_matching_completed_calls_accepted():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    call = ToolCall(id="call_a", name="a")
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), index=0, call=call))
    response = ModelResponse(text="", tool_calls=[call])
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=2), response=response))
    acc.finish()
    assert acc.response.tool_calls == [call]


def test_direct_response_completed_with_tool_calls_and_no_streamed_events():
    """One-shot compatibility: tool_calls may arrive only in the final
    response, with zero ToolCallDelta/ToolCallCompleted events."""
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    response = ModelResponse(text="", tool_calls=[
        ToolCall(id="call_a", name="a")])
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1), response=response))
    acc.finish()
    assert acc.response.tool_calls == [ToolCall(id="call_a", name="a")]


def test_direct_final_response_accepts_32_tool_calls_without_streamed_events():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    calls = [
        ToolCall(id=f"call_{index}", name=f"tool_{index}")
        for index in range(32)
    ]
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=ModelResponse(text="", tool_calls=calls),
    ))
    acc.finish()
    assert len(acc.response.tool_calls) == 32


def test_direct_final_response_rejects_33_tool_calls_without_state_advance():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    too_many = [
        ToolCall(id=f"call_{index}", name=f"tool_{index}")
        for index in range(33)
    ]
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=1),
            response=ModelResponse(text="", tool_calls=too_many),
        ))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=ModelResponse(text="", tool_calls=too_many[:32]),
    ))
    acc.finish()
    assert len(acc.response.tool_calls) == 32


def test_direct_final_response_tool_name_length_boundary():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=ModelResponse(text="", tool_calls=[
            ToolCall(id="call_ok", name="x" * 256),
        ]),
    ))
    acc.finish()
    assert acc.response.tool_calls[0].name == "x" * 256


def test_direct_final_response_tool_name_too_long_rejected():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    with pytest.raises(StreamValidationError):
        acc.accept(ResponseCompleted(
            envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                              sequence=1),
            response=ModelResponse(text="", tool_calls=[
                ToolCall(id="call_bad", name="x" * 257),
            ]),
        ))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=ModelResponse(text="", tool_calls=[
            ToolCall(id="call_ok", name="x" * 256),
        ]),
    ))
    acc.finish()
    assert acc.response.tool_calls[0].name == "x" * 256


def test_response_completed_mutation_isolation():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    arguments = {"nested": {"value": 1}}
    response = ModelResponse(
        text="safe",
        tool_calls=[ToolCall(id="call_a", name="tool", arguments=arguments)],
    )
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=response,
    ))
    response.text = "rewritten"
    response.tool_calls.append(ToolCall(id="call_b", name="other"))
    arguments["nested"]["value"] = 999
    acc.finish()
    canonical = acc.response
    assert canonical.text == "safe"
    assert canonical.tool_calls == [
        ToolCall(id="call_a", name="tool",
                 arguments={"nested": {"value": 1}})
    ]


def test_tool_call_completed_mutation_isolation():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    arguments = {"nested": {"value": 1}}
    call = ToolCall(id="call_a", name="tool", arguments=arguments)
    acc.accept(ToolCallCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        index=0,
        call=call,
    ))
    arguments["nested"]["value"] = 999
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=2),
        response=ModelResponse(text="", tool_calls=[
            ToolCall(id="call_a", name="tool",
                     arguments={"nested": {"value": 1}})
        ]),
    ))
    acc.finish()
    assert acc.response.tool_calls == [
        ToolCall(id="call_a", name="tool",
                 arguments={"nested": {"value": 1}})
    ]


def test_public_accessors_are_mutation_isolated():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    req_id, att_id = new_id("req"), new_id("att")
    acc = new_accumulator(ctx, req_id)
    acc.accept(StreamStarted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=0), model="m", provider="p"))
    acc.accept(ResponseCompleted(
        envelope=envelope(ctx, request_id=req_id, attempt_id=att_id,
                          sequence=1),
        response=ModelResponse(text="safe", tool_calls=[
            ToolCall(id="call_a", name="tool",
                     arguments={"nested": {"value": 1}})
        ]),
    ))
    acc.finish()

    r1 = acc.response
    r1.text = "rewritten"
    r1.tool_calls.append(ToolCall(id="call_b", name="other"))
    r1.tool_calls[0].arguments["nested"]["value"] = 999

    r2 = acc.response
    assert r2.text == "safe"
    assert r2.tool_calls == [
        ToolCall(id="call_a", name="tool",
                 arguments={"nested": {"value": 1}})
    ]

    t1 = acc.terminal_event
    assert isinstance(t1, ResponseCompleted)
    t1.response.text = "rewritten again"
    t1.response.tool_calls.clear()

    t2 = acc.terminal_event
    assert isinstance(t2, ResponseCompleted)
    assert t2.response.text == "safe"
    assert t2.response.tool_calls == [
        ToolCall(id="call_a", name="tool",
                 arguments={"nested": {"value": 1}})
    ]


# ---------------------------------------------------------------------------
# 28. RAW SCRUB
# ---------------------------------------------------------------------------
def test_one_shot_adapter_scrubs_raw_and_does_not_mutate_original():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    dirty = ModelResponse(text="hello", model="m", provider="fake",
                          finish_reason=FinishReason.STOP,
                          usage=Usage(1, 2, 3),
                          raw={"secret": RAW_SECRET}, latency_ms=12.5)
    provider = RecordingProvider(response=dirty)

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    assert isinstance(events[0], StreamStarted)
    assert isinstance(events[1], ResponseCompleted)
    public_response = events[1].response
    assert public_response.raw == {}
    assert public_response.text == "hello"
    assert public_response.model == "m"
    assert public_response.provider == "fake"
    assert public_response.finish_reason is FinishReason.STOP
    assert public_response.usage == Usage(1, 2, 3)
    assert public_response.latency_ms == 12.5

    # Original provider object must be untouched (no in-place mutation).
    assert dirty.raw == {"secret": RAW_SECRET}

    # The sentinel must not leak anywhere in the public event stream.
    rendered = repr(events)
    assert RAW_SECRET not in rendered


# ---------------------------------------------------------------------------
# 29. ONE-SHOT FAILURE
# ---------------------------------------------------------------------------
def test_one_shot_connection_error_terminal():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(error=ModelConnectionError("flaky"))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "ModelConnectionError"
    assert terminal.retryable is True
    assert terminal.will_retry is False


def test_one_shot_auth_error_terminal():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(error=ModelAuthError("bad key"))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "ModelAuthError"
    assert terminal.retryable is False
    assert terminal.will_retry is False


def test_one_shot_response_error_terminal():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(error=ModelResponseError("bad body"))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "ModelResponseError"
    assert terminal.retryable is False
    assert terminal.will_retry is False


def test_one_shot_unexpected_error_never_leaks_message():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(error=RuntimeError(RUNTIME_SECRET))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "RuntimeError"
    assert terminal.retryable is False
    assert terminal.will_retry is False
    assert RUNTIME_SECRET not in repr(terminal)
    assert RUNTIME_SECRET not in terminal.error_kind


@pytest.mark.parametrize(
    ("response", "expected_kind"),
    [
        (ModelResponse(text=123), "StreamValidationError"),
        (ModelResponse(text="ok", usage="bad"), "StreamValidationError"),
        (ModelResponse(text="ok", tool_calls=None), "StreamValidationError"),
        (ModelResponse(text="ok", tool_calls=[object()]),
         "StreamValidationError"),
        (ModelResponse(text="ok", tool_calls=[
            ToolCall(id="call_a", name=123),  # type: ignore[arg-type]
        ]), "StreamValidationError"),
        (ModelResponse(text="ok", tool_calls=[
            ToolCall(id="call_a", name="tool",
                     arguments=[]),  # type: ignore[arg-type]
        ]), "StreamValidationError"),
    ],
)
def test_one_shot_malformed_model_response_becomes_stream_failed(
        response: ModelResponse, expected_kind: str):
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(response=response)

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    assert isinstance(events[0], StreamStarted)
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == expected_kind
    assert terminal.retryable is False
    assert terminal.will_retry is False
    assert RAW_SECRET not in repr(terminal)
    assert RUNTIME_SECRET not in repr(terminal)


def test_one_shot_usage_secret_field_becomes_stream_failed():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(response=ModelResponse(
        text="ok",
        usage=Usage(
            prompt_tokens={"secret": RAW_SECRET},
            completion_tokens=0,
            total_tokens=0,
        ),
    ))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    assert isinstance(events[0], StreamStarted)
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "StreamValidationError"
    assert terminal.retryable is False
    assert terminal.will_retry is False
    assert RAW_SECRET not in repr(events)


@pytest.mark.parametrize(
    "usage",
    [
        Usage(prompt_tokens=-1, completion_tokens=0, total_tokens=0),
        Usage(prompt_tokens=True, completion_tokens=0, total_tokens=0),
    ],
)
def test_one_shot_invalid_usage_fields_become_stream_failed(usage: Usage):
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(response=ModelResponse(text="ok", usage=usage))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    assert isinstance(events[0], StreamStarted)
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "StreamValidationError"
    assert terminal.retryable is False
    assert terminal.will_retry is False


@pytest.mark.parametrize(
    "latency_ms",
    [
        {"secret": RAW_SECRET},
        True,
        -1,
        math.nan,
        math.inf,
    ],
)
def test_one_shot_invalid_latency_becomes_stream_failed(latency_ms: Any):
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    provider = RecordingProvider(response=ModelResponse(
        text="ok",
        latency_ms=latency_ms,
    ))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    assert isinstance(events[0], StreamStarted)
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "StreamValidationError"
    assert terminal.retryable is False
    assert terminal.will_retry is False
    assert RAW_SECRET not in repr(events)


def test_cyclic_tool_arguments_become_stream_failed():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    args: list[Any] = []
    args.append(args)
    provider = RecordingProvider(response=ModelResponse(
        text="ok",
        tool_calls=[ToolCall(id="call_a", name="tool", arguments={"loop": args})],
    ))

    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(provider.calls) == 1
    assert isinstance(events[0], StreamStarted)
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "StreamValidationError"
    assert terminal.retryable is False
    assert terminal.will_retry is False


def test_keyboard_interrupt_propagates_from_one_shot_adapter():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    calls: list[int] = []

    class InterruptingProvider:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            calls.append(1)
            raise KeyboardInterrupt()

    events = iter_one_shot_events(
        InterruptingProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att"))

    assert isinstance(next(events), StreamStarted)
    with pytest.raises(KeyboardInterrupt):
        next(events)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 30. CANCELLATION / DEADLINE
# ---------------------------------------------------------------------------
def test_cancellation_wins_while_provider_returns_success():
    clock = FixedClock(T0)
    ctx, source = make_context(clock, remaining_seconds=30.0)

    class CancelDuringCall:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            source.cancel(CancellationReason.USER_INTERRUPT)
            return ModelResponse(text="too late")

    events = list(iter_one_shot_events(
        CancelDuringCall(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    terminal = events[1]
    assert isinstance(terminal, StreamCancelled)
    assert terminal.reason is CancellationReason.USER_INTERRUPT


def test_deadline_expiry_wins_with_no_earlier_cancellation():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock, remaining_seconds=10.0)

    class SlowProvider:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            clock.advance(20.0)  # pushes remaining past the deadline
            return ModelResponse(text="too slow")

    events = list(iter_one_shot_events(
        SlowProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    terminal = events[1]
    assert isinstance(terminal, StreamCancelled)
    assert terminal.reason is CancellationReason.DEADLINE_EXCEEDED


def test_superseded_wins_over_later_deadline_expiry():
    clock = FixedClock(T0)
    ctx, source = make_context(clock, remaining_seconds=10.0)

    class SupersededThenSlow:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            source.cancel(CancellationReason.SUPERSEDED)
            clock.advance(20.0)
            return ModelResponse(text="stale")

    events = list(iter_one_shot_events(
        SupersededThenSlow(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    terminal = events[1]
    assert isinstance(terminal, StreamCancelled)
    assert terminal.reason is CancellationReason.SUPERSEDED


def test_cancellation_wins_over_provider_failure():
    clock = FixedClock(T0)
    ctx, source = make_context(clock, remaining_seconds=30.0)

    class CancelThenFail:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            source.cancel(CancellationReason.OPERATOR_CANCEL)
            raise ModelConnectionError("down")

    events = list(iter_one_shot_events(
        CancelThenFail(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    terminal = events[1]
    assert isinstance(terminal, StreamCancelled)
    assert terminal.reason is CancellationReason.OPERATOR_CANCEL


def test_deadline_wins_over_provider_success():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock, remaining_seconds=5.0)

    class LateSuccess:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            clock.advance(50.0)
            return ModelResponse(text="ok but late")

    events = list(iter_one_shot_events(
        LateSuccess(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    terminal = events[1]
    assert isinstance(terminal, StreamCancelled)
    assert terminal.reason is CancellationReason.DEADLINE_EXCEEDED


# ---------------------------------------------------------------------------
# 31. TRUSTED ARGUMENT FORWARDING
# ---------------------------------------------------------------------------
def test_generate_with_explicit_params_receives_exact_objects():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    seen: dict[str, Any] = {}

    class ExplicitProvider:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            seen["context"] = context
            seen["clock"] = clock
            return ModelResponse(text="ok")

    list(iter_one_shot_events(
        ExplicitProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert seen["context"] is ctx
    assert seen["clock"] is clock


def test_generate_without_trusted_params_invoked_once_without_them():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    calls: list[ModelRequest] = []

    class NoParamsProvider:
        name = "fake"

        def generate(self, request):
            calls.append(request)
            return ModelResponse(text="ok")

    events = list(iter_one_shot_events(
        NoParamsProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(calls) == 1
    assert isinstance(events[1], ResponseCompleted)


def test_generate_with_kwargs_only_does_not_receive_trusted_context_or_clock():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    seen: dict[str, Any] = {}

    class KwargsOnlyProvider:
        name = "fake"

        def generate(self, request, **options):
            seen["options"] = dict(options)
            return ModelResponse(text="ok")

    list(iter_one_shot_events(
        KwargsOnlyProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert "context" not in seen["options"]
    assert "clock" not in seen["options"]


def test_positional_only_context_and_clock_do_not_count():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    calls: list[Any] = []

    class PositionalOnlyProvider:
        name = "fake"

        def generate(self, request, context=None, clock=None, /):
            calls.append((context, clock))
            return ModelResponse(text="ok")

    events = list(iter_one_shot_events(
        PositionalOnlyProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(calls) == 1
    assert calls[0] == (None, None)  # never forwarded as keyword args
    assert isinstance(events[1], ResponseCompleted)


def test_internal_type_error_from_generate_does_not_trigger_second_call():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    calls: list[int] = []

    class BuggyProvider:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            calls.append(1)
            raise TypeError("internal bug, not a missing-argument error")

    events = list(iter_one_shot_events(
        BuggyProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(calls) == 1
    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "TypeError"
    assert terminal.retryable is False
    assert terminal.will_retry is False


def test_complete_fallback_trusted_forwarding_and_system_rendering():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    seen: dict[str, Any] = {}

    class CompleteOnlyProvider:
        name = "fake"
        config = None

        def complete(self, prompt, *, system=None, context=None, clock=None,
                     **options):
            seen["prompt"] = prompt
            seen["system"] = system
            seen["context"] = context
            seen["clock"] = clock
            seen["options"] = dict(options)
            return "legacy reply"

    request = ModelRequest(messages=[
        Message.system("be terse"), Message.user("hi")])
    events = list(iter_one_shot_events(
        CompleteOnlyProvider(), request, context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert seen["system"] == "be terse"
    assert seen["prompt"] == "user: hi"
    assert seen["context"] is ctx
    assert seen["clock"] is clock
    assert seen["options"] == {}

    terminal = events[1]
    assert isinstance(terminal, ResponseCompleted)
    response = terminal.response
    assert response.text == "legacy reply"
    assert response.raw == {}
    assert response.finish_reason == FinishReason.UNKNOWN
    assert response.usage == Usage()
    assert response.tool_calls == []


def test_generate_takes_precedence_over_complete():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)
    generate_calls: list[int] = []
    complete_calls: list[int] = []

    class BothProvider:
        name = "fake"

        def generate(self, request, *, context=None, clock=None):
            generate_calls.append(1)
            return ModelResponse(text="from generate")

        def complete(self, prompt, *, system=None, **options):
            complete_calls.append(1)
            return "from complete"

    events = list(iter_one_shot_events(
        BothProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert len(generate_calls) == 1
    assert len(complete_calls) == 0
    assert events[1].response.text == "from generate"


def test_no_probing_invocation_when_provider_lacks_both_entry_points():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)

    class NeitherProvider:
        name = "fake"

    events = list(iter_one_shot_events(
        NeitherProvider(), basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    terminal = events[1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.will_retry is False


# ---------------------------------------------------------------------------
# 32. OPAQUE INTERNAL RETRIES
# ---------------------------------------------------------------------------
def test_internal_provider_retries_stay_opaque():
    clock = FixedClock(T0)
    ctx, _ = make_context(clock)

    class HiddenRetryProvider:
        name = "fake"

        def __init__(self) -> None:
            self.internal_attempts = 0
            self.outer_calls = 0

        def generate(self, request, *, context=None, clock=None):
            self.outer_calls += 1
            # Simulate hidden internal HTTP-retry work inside one logical
            # generate() invocation: never observable from the outside.
            for _ in range(3):
                self.internal_attempts += 1
            return ModelResponse(text="eventually ok")

    provider = HiddenRetryProvider()
    events = list(iter_one_shot_events(
        provider, basic_request(), context=ctx, clock=clock,
        request_id=new_id("req"), attempt_id=new_id("att")))

    assert provider.internal_attempts == 3
    assert provider.outer_calls == 1  # the adapter calls generate() once
    assert len(events) == 2
    assert isinstance(events[0], StreamStarted)
    assert isinstance(events[1], ResponseCompleted)
    envelopes = {e.envelope.attempt_id for e in events}
    assert len(envelopes) == 1  # single attempt_id across the whole stream


# ---------------------------------------------------------------------------
# stream_safe_response() helper
# ---------------------------------------------------------------------------
def test_stream_safe_response_copies_tool_calls_list():
    original_calls = [ToolCall(id="c1", name="a")]
    original = ModelResponse(text="hi", tool_calls=original_calls,
                             raw={"x": 1})
    safe = stream_safe_response(original)
    assert safe.raw == {}
    assert safe.tool_calls == original_calls
    assert safe.tool_calls is not original_calls
    assert original.raw == {"x": 1}  # untouched


def test_stream_safe_response_rejects_non_model_response():
    with pytest.raises(Exception):
        stream_safe_response("not a response")  # type: ignore[arg-type]
