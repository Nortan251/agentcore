"""Task 42.5b: owned conversation streaming integration.

All concurrency is gated by ``Event``/``Condition``.  The stream is always
pulled by the calling turn thread; there is no producer thread, queue,
prefetch, timing guess, live transport, or network provider.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from agentcore.config.schema import LoggingConfig, PermissionConfig
from agentcore.conversation.config import ConversationConfig
from agentcore.conversation.errors import ConversationValidationError
from agentcore.conversation.runtime import ConversationRuntime
from agentcore.conversation.session import (
    ConversationSession,
    InMemorySessionStore,
)
from agentcore.conversation.tool_use import ConversationToolRunner
from agentcore.conversation.turns import (
    SUPERSEDED_TURN_ERROR,
    TIMED_OUT_TURN_ERROR,
    TurnBridge,
)
from agentcore.models.messages import (
    FinishReason,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)
from agentcore.models.stream_context import StreamRequestContext
from agentcore.models.streaming import (
    MAX_STREAM_ARGUMENT_CHARS_PER_CALL,
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
from agentcore.observability.events import Events
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime import (
    CancellationReason,
    InMemoryTurnStore,
    TurnCoordinator,
    TurnPhase,
    derive_session_key,
)
from agentcore.state.models import AgentState
from agentcore.tools.base import Tool, ToolResult, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import FixedClock
from agentcore.util.ids import new_id


WAIT = 30.0
T0 = datetime(2026, 8, 24, tzinfo=timezone.utc)

RAW_SESSION_SECRET = "RAW_SESSION_SECRET"
RAW_PROVIDER_SECRET = "RAW_PROVIDER_SECRET_42_5"
AUTH_SECRET = "Authorization: Bearer sk-task-42-5-secret"
TOOL_ARGUMENT_SECRET = "tool-argument-secret-42-5"
WORKSPACE_SENTINEL = "C:/workspace/private/task-42-5.txt"
FULL_DELTA_SENTINEL = "full-model-delta-sentinel-42-5"


def _envelope(context: StreamRequestContext, attempt_id: str, sequence: int,
              **overrides: Any) -> StreamEventEnvelope:
    values = {
        "turn_id": context.turn_id,
        "session_id": context.session_id,
        "generation": context.generation,
        "operation_id": context.operation_id,
        "request_id": context.request_id,
        "attempt_id": attempt_id,
        "sequence": sequence,
    }
    values.update(overrides)
    return StreamEventEnvelope(**values)


def _response(text: str, *, tool_calls: tuple[ToolCall, ...] = (),
              raw: dict[str, Any] | None = None) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="fake-model",
        provider="fake-native",
        finish_reason=(FinishReason.TOOL_CALLS if tool_calls
                       else FinishReason.STOP),
        usage=Usage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        tool_calls=list(tool_calls),
        raw={} if raw is None else raw,
        latency_ms=4.5,
    )


def _success_script(text: str, *deltas: str):
    parts = deltas or (text,)

    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any) -> Iterator[Any]:
        attempt = new_id("att")
        events: list[Any] = [
            StreamStarted(_envelope(context, attempt, 0),
                          model="fake-model", provider="fake-native")]
        for sequence, delta in enumerate(parts, 1):
            events.append(TextDelta(
                _envelope(context, attempt, sequence), delta))
        events.append(ResponseCompleted(
            _envelope(context, attempt, len(events)), _response(text)))
        return iter(events)

    return script


class NativeProvider:
    """Trusted-signature fake with one synchronous script per model round."""

    name = "fake-native"

    def __init__(self, *scripts: Any,
                 generate_response: ModelResponse | None = None) -> None:
        self.scripts = list(scripts)
        self.generate_response = generate_response or _response("one-shot")
        self.stream_calls = 0
        self.generate_calls = 0
        self.requests: list[Any] = []
        self.contexts: list[Any] = []
        self.clocks: list[Any] = []

    def stream(self, request: Any, *, context: Any,
               clock: Any) -> Iterator[Any]:
        self.stream_calls += 1
        self.requests.append(request)
        self.contexts.append(context)
        self.clocks.append(clock)
        if not self.scripts:
            raise AssertionError("native stream script exhausted")
        return self.scripts.pop(0)(context, clock, request)

    def generate(self, request: Any, *, context: Any = None,
                 clock: Any = None) -> ModelResponse:
        self.generate_calls += 1
        self.requests.append(request)
        self.contexts.append(context)
        self.clocks.append(clock)
        return self.generate_response


class OneShotProvider:
    name = "fake-one-shot"

    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.calls = 0
        self.requests: list[Any] = []
        self.contexts: list[Any] = []
        self.clocks: list[Any] = []

    def generate(self, request: Any, *, context: Any = None,
                 clock: Any = None) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        self.contexts.append(context)
        self.clocks.append(clock)
        return self.response


class CompleteOnlyProvider:
    name = "complete-only"

    def __init__(self) -> None:
        self.calls = 0
        self.options: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 context: Any = None, clock: Any = None) -> str:
        self.calls += 1
        self.options.append({
            "prompt": prompt, "system": system,
            "context": context, "clock": clock,
        })
        return "complete fallback"


class RecordingCoordinator:
    def __init__(self, inner: TurnCoordinator) -> None:
        self.inner = inner
        self.admissions: list[Any] = []
        self._condition = threading.Condition()

    @property
    def store(self):
        return self.inner.store

    def admit(self, session_id: str, **kwargs: Any):
        admission = self.inner.admit(session_id, **kwargs)
        with self._condition:
            self.admissions.append(admission)
            self._condition.notify_all()
        return admission

    def wait_for(self, count: int):
        with self._condition:
            ready = self._condition.wait_for(
                lambda: len(self.admissions) >= count, WAIT)
        assert ready, f"only {len(self.admissions)} admissions, wanted {count}"
        return self.admissions[count - 1]


class CaptureOutcome:
    stored: list[Any] = []
    conflicts_recorded: list[Any] = []
    decisions: list[Any] = []

    def describe(self) -> dict[str, int]:
        return {"stored": 0, "conflicts": 0, "decisions": 0}


class CaptureRecorder:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def capture(self, message: Any, **_kwargs: Any) -> CaptureOutcome:
        self.messages.append(message)
        return CaptureOutcome()


def _owned_runtime(model: Any, *, tools: Any = None,
                   capture: Any = None, state: AgentState | None = None,
                   clock: FixedClock | None = None,
                   deadline_seconds: float = 120.0):
    store = InMemoryTurnStore()
    coordinator = RecordingCoordinator(TurnCoordinator(store))
    trusted_clock = clock or FixedClock(T0)
    bridge = TurnBridge(
        coordinator,
        clock=trusted_clock,
        turn_deadline_seconds=deadline_seconds,
    )
    sessions = InMemorySessionStore()
    runtime = ConversationRuntime(
        model=model,
        tools=tools,
        capture=capture,
        state=state,
        sessions=sessions,
        turns=bridge,
        config=ConversationConfig(turn_deadline_seconds=deadline_seconds),
    )
    return runtime, store, coordinator, trusted_clock, sessions


def _records(store: InMemoryTurnStore) -> list[Any]:
    return [store.load(turn_id) for turn_id in store.list_turns()]


def _event_names(events: list[Any]) -> list[str]:
    return [type(event).__name__ for event in events]


@pytest.fixture
def log_buffer():
    configure_logging(LoggingConfig(level="DEBUG"))
    root = logging.getLogger("agentcore")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    for existing in root.handlers:
        handler.setFormatter(existing.formatter)
    root.addHandler(handler)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        configure_logging(LoggingConfig(level="CRITICAL"))


class CountingTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "counter", "Count executions.", parameters=(),
            capabilities=[Capability.FS_READ], read_only=True)

    def run(self, args: Any, context: Any) -> ToolResult:
        self.runs += 1
        return ToolResult.success(f"counter-result-{self.runs}")


class CountingPolicy(PermissionPolicy):
    def __init__(self) -> None:
        super().__init__(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
        self.evaluate_calls = 0
        self.check_calls = 0

    def evaluate(self, capability: Capability):
        self.evaluate_calls += 1
        return super().evaluate(capability)

    def check(self, capabilities: Any, *, description: str = ""):
        self.check_calls += 1
        return super().check(capabilities, description=description)


class CountingExecutor(ToolExecutor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def invoke(self, *args: Any, **kwargs: Any):
        self.calls += 1
        return super().invoke(*args, **kwargs)


class CountingRunner(ConversationToolRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def run_calls(self, *args: Any, **kwargs: Any):
        self.calls += 1
        return super().run_calls(*args, **kwargs)


def _tool_boundary():
    tool = CountingTool()
    registry = ToolRegistry()
    assert registry.register(tool)
    policy = CountingPolicy()
    executor = CountingExecutor(
        registry, policy, workspace_dir="C:/workspace")
    runner = CountingRunner(executor, registry, policy=policy)
    return runner, executor, policy, tool


# ---------------------------------------------------------------------------
# Explicit opt-in and basic final-only behavior
# ---------------------------------------------------------------------------


def test_no_sink_preserves_one_shot_path_even_with_valid_native_stream():
    provider = NativeProvider(
        _success_script("streamed"),
        generate_response=_response("ordinary one-shot"))
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    session = runtime.start()

    result = runtime.send("hello", session=session)

    assert result.ok
    assert result.reply.content == "ordinary one-shot"
    assert provider.generate_calls == 1
    assert provider.stream_calls == 0


def test_sink_without_turn_bridge_fails_before_model_or_session_mutation():
    provider = NativeProvider(_success_script("never"))
    sessions = InMemorySessionStore()
    runtime = ConversationRuntime(model=provider, sessions=sessions)
    session = ConversationSession()

    with pytest.raises(ConversationValidationError,
                       match="owned realtime turn"):
        runtime.send("must not append", session=session,
                     on_stream_event=lambda _event: None)

    assert provider.stream_calls == 0
    assert provider.generate_calls == 0
    assert session.messages == []
    assert sessions.list_sessions() == []


def test_async_sink_is_rejected_before_admission_or_model_execution():
    provider = NativeProvider(_success_script("never"))
    runtime, store, coordinator, _clock, _sessions = _owned_runtime(provider)
    session = runtime.start()

    async def async_sink(_event: Any) -> None:
        return None

    with pytest.raises(ConversationValidationError, match="synchronous"):
        runtime.send("must not append", session=session,
                     on_stream_event=async_sink)

    assert provider.stream_calls == 0
    assert provider.generate_calls == 0
    assert coordinator.admissions == []
    assert store.list_turns() == []
    assert session.messages == []


def test_native_success_is_ordered_detached_and_final_only_persistent():
    capture = CaptureRecorder()
    provider = NativeProvider(_success_script("hello", "hel", "lo"))
    runtime, store, _coordinator, _clock, sessions = _owned_runtime(
        provider, capture=capture)
    session = runtime.start(session_id=RAW_SESSION_SECRET)
    seen: list[Any] = []
    transcript_during_delivery: list[list[str]] = []

    def sink(event: Any) -> None:
        seen.append(event)
        transcript_during_delivery.append(
            [message.content for message in session.messages])
        if isinstance(event, ResponseCompleted):
            assert event.response.raw == {}
            # Presentation mutation cannot rewrite canonical state.
            event.response.text = "sink-tampered"
            event.response.raw["secret"] = RAW_PROVIDER_SECRET

    result = runtime.send(
        "user turn", session=session, on_stream_event=sink)

    assert _event_names(seen) == [
        "StreamStarted", "TextDelta", "TextDelta", "ResponseCompleted"]
    assert all(snapshot == ["user turn"]
               for snapshot in transcript_during_delivery)
    assert result.ok
    assert result.reply.content == "hello"
    assert [message.content for message in session.messages] == [
        "user turn", "hello"]
    persisted = sessions.load(session.id)
    assert [message.content for message in persisted.messages] == [
        "user turn", "hello"]
    assert all(message.content not in {"hel", "lo", "sink-tampered"}
               for message in persisted.messages)
    assert capture.messages == [session.messages[0]]
    assert capture.messages[0].role is Role.USER
    assert capture.messages[0].content == "user turn"
    record_payload = json.dumps(store.load(result.turn_id).to_dict())
    assert "hel" not in record_payload
    assert "sink-tampered" not in record_payload
    assert RAW_PROVIDER_SECRET not in record_payload


# ---------------------------------------------------------------------------
# Synchronous pull backpressure and stale fencing
# ---------------------------------------------------------------------------


class PullRecordingIterator:
    def __init__(self, events: list[Any], *, gate_before: int | None = None,
                 entered: threading.Event | None = None,
                 release: threading.Event | None = None) -> None:
        self.events = events
        self.index = 0
        self.pulls: list[int] = []
        self.gate_before = gate_before
        self.entered = entered
        self.release = release

    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= len(self.events):
            raise StopIteration
        if self.gate_before == self.index:
            assert self.entered is not None and self.release is not None
            self.entered.set()
            assert self.release.wait(WAIT), "iterator gate was not released"
        index = self.index
        self.pulls.append(index)
        self.index += 1
        return self.events[index]


def test_sink_backpressure_prevents_next_iterator_pull():
    holder: dict[str, PullRecordingIterator] = {}

    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        iterator = PullRecordingIterator([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), "ok"),
            ResponseCompleted(
                _envelope(context, attempt, 2), _response("ok")),
        ])
        holder["iterator"] = iterator
        return iterator

    provider = NativeProvider(script)
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    session = runtime.start()
    sink_entered = threading.Event()
    sink_release = threading.Event()
    results: list[Any] = []

    def sink(event: Any) -> None:
        if isinstance(event, StreamStarted):
            sink_entered.set()
            assert sink_release.wait(WAIT), "sink gate was not released"

    thread = threading.Thread(
        target=lambda: results.append(runtime.send(
            "hello", session=session, on_stream_event=sink)),
        daemon=True)
    thread.start()
    assert sink_entered.wait(WAIT), "sink did not receive StreamStarted"

    # The consumer thread is synchronously inside event 0's sink.  Therefore
    # inspecting this exact state needs no timeout guess or polling.
    assert holder["iterator"].pulls == [0]

    sink_release.set()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert results[0].ok
    assert holder["iterator"].pulls == [0, 1, 2]


def test_superseded_stream_delivers_no_later_delta_and_executes_no_tool():
    runner, executor, policy, tool = _tool_boundary()
    gate_entered = threading.Event()
    gate_release = threading.Event()
    old_call = ToolCall("call_old", "counter", {})

    def old_script(context: StreamRequestContext, _clock: FixedClock,
                   _request: Any):
        attempt = new_id("att")
        return PullRecordingIterator([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), "old"),
            ToolCallDelta(_envelope(context, attempt, 2), 0,
                          old_call.id, old_call.name, "{}"),
            ToolCallCompleted(_envelope(context, attempt, 3), 0, old_call),
            TextDelta(_envelope(context, attempt, 4), "-late"),
            ResponseCompleted(
                _envelope(context, attempt, 5),
                _response("old-late", tool_calls=(old_call,))),
        ], gate_before=4, entered=gate_entered, release=gate_release)

    provider = NativeProvider(old_script, _success_script("fresh"))
    runtime, store, coordinator, _clock, sessions = _owned_runtime(
        provider, tools=runner)
    session = runtime.start()
    results: dict[str, Any] = {}
    old_seen: list[Any] = []
    fresh_seen: list[Any] = []

    first = threading.Thread(
        target=lambda: results.setdefault("old", runtime.send(
            "one", session=session, on_stream_event=old_seen.append)),
        daemon=True)
    first.start()
    assert gate_entered.wait(WAIT), "old stream did not reach its gate"

    second = threading.Thread(
        target=lambda: results.setdefault("fresh", runtime.send(
            "two", session=session, on_stream_event=fresh_seen.append)),
        daemon=True)
    second.start()
    successor = coordinator.wait_for(2)
    predecessor_id = coordinator.admissions[0].admitted.turn_id
    predecessor = store.load(predecessor_id)
    assert predecessor.phase is TurnPhase.CANCELLING
    assert predecessor.cancellation_reason is CancellationReason.SUPERSEDED

    gate_release.set()
    first.join(WAIT)
    second.join(WAIT)
    assert not first.is_alive() and not second.is_alive()

    assert _event_names(old_seen) == [
        "StreamStarted", "TextDelta", "ToolCallDelta", "ToolCallCompleted"]
    assert all(not (isinstance(event, TextDelta) and event.text == "-late")
               for event in old_seen)
    assert results["old"].error == SUPERSEDED_TURN_ERROR
    assert results["old"].reply is None
    assert store.load(predecessor_id).phase is TurnPhase.CANCELLED
    assert store.load(successor.admitted.turn_id).phase is TurnPhase.COMPLETED
    assert results["fresh"].reply.content == "fresh"
    assert runner.calls == 0
    assert executor.calls == 0
    assert policy.evaluate_calls == 0
    assert policy.check_calls == 0
    assert tool.runs == 0
    assert [message.content for message in session.messages] == [
        "one", "two", "fresh"]
    persisted = sessions.load(session.id)
    assert [message.content for message in persisted.messages] == [
        "one", "two", "fresh"]
    assert "old" not in [message.content for message in persisted.messages]


# ---------------------------------------------------------------------------
# Failure, tool authority and multi-round streaming
# ---------------------------------------------------------------------------


def test_stream_failed_after_visible_text_persists_no_partial_or_memory():
    capture = CaptureRecorder()

    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), "hel"),
            TextDelta(_envelope(context, attempt, 2), "lo"),
            StreamFailed(_envelope(context, attempt, 3),
                         "ModelConnectionError", True, False),
        ])

    provider = NativeProvider(script)
    runtime, store, _coordinator, _clock, sessions = _owned_runtime(
        provider, capture=capture)
    session = runtime.start()
    seen: list[Any] = []

    result = runtime.send("user", session=session,
                          on_stream_event=seen.append)

    assert [event.text for event in seen if isinstance(event, TextDelta)] == [
        "hel", "lo"]
    assert not result.ok
    assert result.reply is None
    assert result.error == "the model stream failed"
    assert provider.stream_calls == 1
    assert [message.content for message in session.messages] == ["user"]
    assert [message.content for message in
            sessions.load(session.id).messages] == ["user"]
    assert capture.messages == []
    assert store.load(result.turn_id).phase is TurnPhase.FAILED


def test_iterator_exception_is_categorical_and_never_discloses_message(
        log_buffer):
    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")

        def events():
            yield StreamStarted(
                _envelope(context, attempt, 0), "m", "p")
            raise RuntimeError(
                f"{RAW_PROVIDER_SECRET} {AUTH_SECRET} {WORKSPACE_SENTINEL}")

        return events()

    provider = NativeProvider(script)
    runtime, store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    seen: list[Any] = []

    result = runtime.send("user", session=runtime.start(),
                          on_stream_event=seen.append)

    assert not result.ok
    assert result.reply is None
    assert result.error == "the model stream failed"
    assert _event_names(seen) == ["StreamStarted"]
    assert store.load(result.turn_id).phase is TurnPhase.FAILED
    logs = log_buffer.getvalue()
    assert "RuntimeError" in logs
    assert RAW_PROVIDER_SECRET not in logs
    assert AUTH_SECRET not in logs
    assert WORKSPACE_SENTINEL not in logs


def test_completed_looking_tool_before_stream_failure_has_zero_authority(
        log_buffer):
    runner, executor, policy, tool = _tool_boundary()
    call = ToolCall("call_secret", "counter", {})
    fragment = f'{{"secret":"{TOOL_ARGUMENT_SECRET}","path":"{WORKSPACE_SENTINEL}"}}'

    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            ToolCallDelta(_envelope(context, attempt, 1), 0,
                          call.id, call.name, fragment),
            ToolCallCompleted(_envelope(context, attempt, 2), 0, call),
            StreamFailed(_envelope(context, attempt, 3),
                         "ModelConnectionError", True, False),
        ])

    provider = NativeProvider(script)
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(
        provider, tools=runner)
    session = runtime.start()
    seen: list[Any] = []

    result = runtime.send("user", session=session,
                          on_stream_event=seen.append)

    assert not result.ok
    assert "ToolCallCompleted" in _event_names(seen)
    assert runner.calls == 0
    assert executor.calls == 0
    assert policy.evaluate_calls == 0
    assert policy.check_calls == 0
    assert tool.runs == 0
    logs = log_buffer.getvalue()
    assert TOOL_ARGUMENT_SECRET not in logs
    assert WORKSPACE_SENTINEL not in logs


def test_successful_streamed_tool_round_uses_existing_authority_and_staging(
        log_buffer):
    runner, executor, policy, tool = _tool_boundary()
    call = ToolCall("call_ok", "counter", {})

    def tool_round(context: StreamRequestContext, _clock: FixedClock,
                   _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), "working"),
            ToolCallDelta(_envelope(context, attempt, 2), 0,
                          call.id, call.name, "{}"),
            ToolCallCompleted(_envelope(context, attempt, 3), 0, call),
            ResponseCompleted(
                _envelope(context, attempt, 4),
                _response("working", tool_calls=(call,))),
        ])

    provider = NativeProvider(tool_round, _success_script("done"))
    runtime, store, _coordinator, _clock, sessions = _owned_runtime(
        provider, tools=runner)
    session = runtime.start()
    seen: list[Any] = []
    transcript_snapshots: list[list[Role]] = []

    def sink(event: Any) -> None:
        seen.append(event)
        transcript_snapshots.append(
            [message.role for message in session.messages])
        if isinstance(event, ResponseCompleted) and \
                event.response.tool_calls:
            # Even the accepted canonical terminal is presentation-only at
            # this instant. Execution starts only after delivery returns and
            # the runtime rechecks the current GENERATING fence.
            assert runner.calls == 0
            assert executor.calls == 0
            assert policy.evaluate_calls == 0
            assert policy.check_calls == 0
            assert tool.runs == 0

    result = runtime.send("use the tool", session=session,
                          on_stream_event=sink)

    assert result.ok
    assert result.reply.content == "done"
    assert provider.stream_calls == 2
    assert runner.calls == 1
    assert executor.calls == 1
    assert policy.evaluate_calls >= 1
    assert policy.check_calls == 1
    assert tool.runs == 1
    assert result.tools_executed == 1
    assert all(snapshot == [Role.USER]
               for snapshot in transcript_snapshots)
    assert [message.role for message in session.messages] == [
        Role.USER, Role.TOOL, Role.ASSISTANT]
    assert [message.role for message in
            sessions.load(session.id).messages] == [
        Role.USER, Role.TOOL, Role.ASSISTANT]

    first_context, second_context = provider.contexts
    assert isinstance(first_context, StreamRequestContext)
    assert isinstance(second_context, StreamRequestContext)
    assert first_context.turn_id == second_context.turn_id == result.turn_id
    assert first_context.generation == second_context.generation == 1
    assert first_context.operation_id != second_context.operation_id
    assert first_context.request_id != second_context.request_id
    assert all(request.metadata == {} for request in provider.requests)
    assert any(message.role is Role.TOOL
               for message in provider.requests[1].messages)
    assert all(context.cancellation is provider.contexts[0].cancellation
               for context in provider.contexts)

    request_ids = [event.envelope.request_id for event in seen]
    operation_ids = [event.envelope.operation_id for event in seen]
    assert set(request_ids) == {
        first_context.request_id, second_context.request_id}
    assert set(operation_ids) == {
        first_context.operation_id, second_context.operation_id}
    first_token_logs = [
        json.loads(line) for line in log_buffer.getvalue().splitlines()
        if line.strip() and json.loads(line).get("event") ==
        Events.TURN_FIRST_TOKEN]
    assert len(first_token_logs) == 1
    assert first_token_logs[0]["request_id"] == first_context.request_id
    assert "working" not in json.dumps(first_token_logs)
    assert "done" not in json.dumps(first_token_logs)
    assert store.load(result.turn_id).phase is TurnPhase.COMPLETED


# ---------------------------------------------------------------------------
# One-shot fallback and trusted signature detection
# ---------------------------------------------------------------------------


def test_one_shot_generate_fallback_invokes_once_and_scrubs_raw():
    original = _response(
        "fallback ok", raw={"secret": RAW_PROVIDER_SECRET})
    provider = OneShotProvider(original)
    runtime, _store, _coordinator, clock, _sessions = _owned_runtime(provider)
    session = runtime.start()
    seen: list[Any] = []

    result = runtime.send("hello", session=session,
                          on_stream_event=seen.append)

    assert result.ok
    assert result.reply.content == "fallback ok"
    assert provider.calls == 1
    assert _event_names(seen) == ["StreamStarted", "ResponseCompleted"]
    terminal = seen[-1]
    assert terminal.response.raw == {}
    assert RAW_PROVIDER_SECRET not in json.dumps(
        terminal.response.to_dict(include_raw=True))
    assert original.raw == {"secret": RAW_PROVIDER_SECRET}
    assert provider.contexts[0] is not None
    assert provider.clocks[0] is clock
    assert provider.requests[0].metadata == {}
    assert len({event.envelope.request_id for event in seen}) == 1
    assert len({event.envelope.attempt_id for event in seen}) == 1


def test_complete_only_provider_uses_two_event_fallback_once():
    provider = CompleteOnlyProvider()
    runtime, _store, _coordinator, clock, _sessions = _owned_runtime(provider)
    seen: list[Any] = []

    result = runtime.send("hello", session=runtime.start(),
                          on_stream_event=seen.append)

    assert result.ok
    assert result.reply.content == "complete fallback"
    assert provider.calls == 1
    assert _event_names(seen) == ["StreamStarted", "ResponseCompleted"]
    assert provider.options[0]["context"] is not None
    assert provider.options[0]["clock"] is clock


class _InvalidStreamBase:
    name = "invalid-stream"

    def __init__(self) -> None:
        self.stream_calls = 0
        self.generate_calls = 0

    def generate(self, request: Any, **kwargs: Any) -> ModelResponse:
        self.generate_calls += 1
        self.generate_kwargs = dict(kwargs)
        self.request = request
        return _response("fallback")


class KwargsOnlyStream(_InvalidStreamBase):
    def stream(self, request: Any, **kwargs: Any):
        self.stream_calls += 1
        raise AssertionError("invalid stream must not be called")


class MissingContextStream(_InvalidStreamBase):
    def stream(self, request: Any, *, clock: Any):
        self.stream_calls += 1
        raise AssertionError("invalid stream must not be called")


class MissingClockStream(_InvalidStreamBase):
    def stream(self, request: Any, *, context: Any):
        self.stream_calls += 1
        raise AssertionError("invalid stream must not be called")


class PositionalOnlyStream(_InvalidStreamBase):
    def stream(self, request: Any, context: Any, clock: Any, /):
        self.stream_calls += 1
        raise AssertionError("invalid stream must not be called")


@pytest.mark.parametrize("provider_type", [
    KwargsOnlyStream,
    MissingContextStream,
    MissingClockStream,
    PositionalOnlyStream,
])
def test_invalid_native_stream_signature_never_called(provider_type):
    provider = provider_type()
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    seen: list[Any] = []

    result = runtime.send("hello", session=runtime.start(),
                          on_stream_event=seen.append)

    assert result.ok
    assert result.reply.content == "fallback"
    assert provider.stream_calls == 0
    assert provider.generate_calls == 1
    assert provider.generate_kwargs == {}
    assert provider.request.metadata == {}
    assert _event_names(seen) == ["StreamStarted", "ResponseCompleted"]


# ---------------------------------------------------------------------------
# Sink isolation, first-token observability and retry identity
# ---------------------------------------------------------------------------


def test_sink_failure_is_turn_wide_categorical_and_does_not_fail_generation(
        log_buffer):
    runner, executor, _policy, tool = _tool_boundary()
    call = ToolCall("call_after_sink", "counter", {})

    def first_round(context: StreamRequestContext, _clock: FixedClock,
                    _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), FULL_DELTA_SENTINEL),
            ResponseCompleted(
                _envelope(context, attempt, 2),
                _response(FULL_DELTA_SENTINEL, tool_calls=(call,))),
        ])

    provider = NativeProvider(first_round, _success_script("final answer"))
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(
        provider, tools=runner)
    session = runtime.start(session_id=RAW_SESSION_SECRET)
    sink_calls: list[str] = []
    exception_secret = f"{AUTH_SECRET} {TOOL_ARGUMENT_SECRET}"

    def sink(event: Any) -> None:
        sink_calls.append(type(event).__name__)
        if isinstance(event, TextDelta):
            raise RuntimeError(exception_secret)

    result = runtime.send("use tool", session=session,
                          on_stream_event=sink)

    assert result.ok
    assert result.reply.content == "final answer"
    assert provider.stream_calls == 2
    assert runner.calls == 1
    assert executor.calls == 1
    assert tool.runs == 1
    assert sink_calls == ["StreamStarted", "TextDelta"]
    assert FULL_DELTA_SENTINEL not in [
        message.content for message in session.messages]
    logs = log_buffer.getvalue()
    assert "RuntimeError" in logs
    assert exception_secret not in logs
    assert AUTH_SECRET not in logs
    assert TOOL_ARGUMENT_SECRET not in logs
    assert FULL_DELTA_SENTINEL not in logs
    first_tokens = [
        json.loads(line) for line in logs.splitlines()
        if line.strip() and json.loads(line).get("event") ==
        Events.TURN_FIRST_TOKEN]
    assert len(first_tokens) == 1


def test_first_token_ignores_empty_text_and_tool_delta_and_logs_no_text(
        log_buffer):
    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), ""),
            ToolCallDelta(_envelope(context, attempt, 2), 0,
                          None, "", ""),
            TextDelta(_envelope(context, attempt, 3), FULL_DELTA_SENTINEL),
            ResponseCompleted(
                _envelope(context, attempt, 4),
                _response(FULL_DELTA_SENTINEL)),
        ])

    provider = NativeProvider(script)
    runtime, _store, _coordinator, clock, _sessions = _owned_runtime(provider)
    clock.advance(2.5)
    result = runtime.send("hello", session=runtime.start(),
                          on_stream_event=lambda _event: None)
    assert result.ok

    entries = [json.loads(line) for line in log_buffer.getvalue().splitlines()
               if line.strip()]
    first_tokens = [entry for entry in entries
                    if entry.get("event") == Events.TURN_FIRST_TOKEN]
    assert len(first_tokens) == 1
    event = first_tokens[0]
    assert event["sequence"] == 3
    assert event["text_chars"] == len(FULL_DELTA_SENTINEL)
    assert FULL_DELTA_SENTINEL not in json.dumps(event)
    assert "text" not in event


def test_native_retry_keeps_request_id_changes_attempt_and_resets_sequence():
    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        first = new_id("att")
        second = new_id("att")
        return iter([
            StreamStarted(_envelope(context, first, 0), "m", "p"),
            UsageUpdated(_envelope(context, first, 1), Usage(total_tokens=1)),
            StreamFailed(_envelope(context, first, 2),
                         "ModelConnectionError", True, True),
            StreamStarted(_envelope(context, second, 0), "m", "p"),
            TextDelta(_envelope(context, second, 1), "retry ok"),
            ResponseCompleted(
                _envelope(context, second, 2), _response("retry ok")),
        ])

    provider = NativeProvider(script)
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    seen: list[Any] = []
    result = runtime.send("hello", session=runtime.start(),
                          on_stream_event=seen.append)

    assert result.ok
    assert provider.stream_calls == 1
    assert len({event.envelope.request_id for event in seen}) == 1
    attempts = [event.envelope.attempt_id for event in seen]
    assert len(set(attempts)) == 2
    starts = [event for event in seen if isinstance(event, StreamStarted)]
    assert [event.envelope.sequence for event in starts] == [0, 0]


# ---------------------------------------------------------------------------
# Fail-closed validation, cancellation/deadline truth and privacy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", [
    "malformed",
    "wrong_sequence",
    "wrong_request",
    "wrong_operation",
    "text_bound",
])
def test_invalid_event_is_rejected_before_sink_delivery(case: str):
    invalid_holder: dict[str, Any] = {}

    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        started = StreamStarted(
            _envelope(context, attempt, 0), "m", "p")
        if case == "malformed":
            invalid = object()
        elif case == "wrong_sequence":
            invalid = TextDelta(_envelope(context, attempt, 2), "bad")
        elif case == "wrong_request":
            invalid = TextDelta(_envelope(
                context, attempt, 1,
                request_id="req_deadbeef0000"), "bad")
        elif case == "wrong_operation":
            invalid = TextDelta(_envelope(
                context, attempt, 1,
                operation_id="op_deadbeef0000"), "bad")
        else:
            invalid = TextDelta(
                _envelope(context, attempt, 1), "x" * 8001)
        invalid_holder["event"] = invalid
        return iter([started, invalid])

    provider = NativeProvider(script)
    runtime, _store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    seen: list[Any] = []

    result = runtime.send("hello", session=runtime.start(),
                          on_stream_event=seen.append)

    assert not result.ok
    assert result.reply is None
    assert _event_names(seen) == ["StreamStarted"]
    assert all(event is not invalid_holder["event"] for event in seen)


def test_deadline_wins_and_stale_terminal_is_not_delivered():
    clock = FixedClock(T0)
    gate_entered = threading.Event()
    gate_release = threading.Event()

    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        return PullRecordingIterator([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            ResponseCompleted(
                _envelope(context, attempt, 1), _response("too late")),
        ], gate_before=1, entered=gate_entered, release=gate_release)

    provider = NativeProvider(script)
    runtime, store, _coordinator, _trusted_clock, _sessions = _owned_runtime(
        provider, clock=clock, deadline_seconds=5.0)
    session = runtime.start()
    seen: list[Any] = []
    results: list[Any] = []
    thread = threading.Thread(
        target=lambda: results.append(runtime.send(
            "hello", session=session, on_stream_event=seen.append)),
        daemon=True)
    thread.start()
    assert gate_entered.wait(WAIT), "stream did not gate before terminal"
    clock.advance(6.0)
    gate_release.set()
    thread.join(WAIT)
    assert not thread.is_alive()

    assert _event_names(seen) == ["StreamStarted"]
    assert results[0].error == TIMED_OUT_TURN_ERROR
    assert results[0].turn_phase == TurnPhase.TIMED_OUT.value
    record = store.load(results[0].turn_id)
    assert record.phase is TurnPhase.TIMED_OUT
    assert record.cancellation_reason is CancellationReason.DEADLINE_EXCEEDED
    assert [message.content for message in session.messages] == ["hello"]


def test_supersession_reason_wins_over_later_deadline_expiry():
    clock = FixedClock(T0)
    gate_entered = threading.Event()
    gate_release = threading.Event()

    def old_script(context: StreamRequestContext, _clock: FixedClock,
                   _request: Any):
        attempt = new_id("att")
        return PullRecordingIterator([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            TextDelta(_envelope(context, attempt, 1), "late"),
        ], gate_before=1, entered=gate_entered, release=gate_release)

    provider = NativeProvider(old_script)
    runtime, store, coordinator, _trusted_clock, _sessions = _owned_runtime(
        provider, clock=clock, deadline_seconds=5.0)
    session = runtime.start()
    results: dict[str, Any] = {}
    first = threading.Thread(
        target=lambda: results.setdefault("old", runtime.send(
            "one", session=session, on_stream_event=lambda _event: None)),
        daemon=True)
    first.start()
    assert gate_entered.wait(WAIT)
    second = threading.Thread(
        target=lambda: results.setdefault("new", runtime.send(
            "two", session=session, on_stream_event=lambda _event: None)),
        daemon=True)
    second.start()
    coordinator.wait_for(2)
    old_id = coordinator.admissions[0].admitted.turn_id
    assert store.load(old_id).cancellation_reason is \
        CancellationReason.SUPERSEDED
    clock.advance(6.0)
    gate_release.set()
    first.join(WAIT)
    second.join(WAIT)
    assert not first.is_alive() and not second.is_alive()

    old = store.load(old_id)
    assert old.phase is TurnPhase.CANCELLED
    assert old.cancellation_reason is CancellationReason.SUPERSEDED
    assert results["old"].error == SUPERSEDED_TURN_ERROR


def test_privacy_sentinels_stay_out_of_stream_observability_and_state(
        log_buffer):
    original = _response(
        "safe final", raw={"secret": RAW_PROVIDER_SECRET})
    provider = OneShotProvider(original)
    state = AgentState()
    runtime, store, _coordinator, _clock, _sessions = _owned_runtime(
        provider, state=state)
    session = runtime.start(session_id=RAW_SESSION_SECRET)
    seen: list[Any] = []

    result = runtime.send("ordinary user text", session=session,
                          on_stream_event=seen.append)

    assert result.ok
    assert all(event.envelope.session_id ==
               derive_session_key(RAW_SESSION_SECRET) for event in seen)
    assert all(event.envelope.session_id != RAW_SESSION_SECRET
               for event in seen)
    terminal = next(event for event in seen
                    if isinstance(event, ResponseCompleted))
    assert terminal.response.raw == {}
    assert RAW_PROVIDER_SECRET not in json.dumps(
        terminal.response.to_dict(include_raw=True))
    assert RAW_PROVIDER_SECRET not in result.reply.content
    record_json = json.dumps(store.load(result.turn_id).to_dict())
    assert RAW_PROVIDER_SECRET not in record_json
    assert RAW_SESSION_SECRET not in record_json
    state_json = json.dumps(state.to_dict())
    assert RAW_PROVIDER_SECRET not in state_json
    assert RAW_SESSION_SECRET not in state_json
    assert state.version == 0

    entries = [json.loads(line) for line in log_buffer.getvalue().splitlines()
               if line.strip()]
    turn_entries = [entry for entry in entries
                    if str(entry.get("event", "")).startswith("turn.")]
    assert turn_entries
    turn_json = json.dumps(turn_entries)
    assert RAW_SESSION_SECRET not in turn_json
    assert RAW_PROVIDER_SECRET not in turn_json
    assert derive_session_key(RAW_SESSION_SECRET) in turn_json
    assert RAW_PROVIDER_SECRET not in log_buffer.getvalue()
    assert provider.requests[0].metadata == {}
    assert all(value not in provider.requests[0].metadata.values()
               for value in (provider.contexts[0], provider.clocks[0]))


def test_stream_cancelled_deadline_maps_through_existing_turn_lifecycle():
    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            StreamCancelled(
                _envelope(context, attempt, 1),
                CancellationReason.DEADLINE_EXCEEDED),
        ])

    provider = NativeProvider(script)
    runtime, store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    seen: list[Any] = []

    result = runtime.send("hello", session=runtime.start(),
                          on_stream_event=seen.append)

    assert _event_names(seen) == ["StreamStarted", "StreamCancelled"]
    assert result.error == TIMED_OUT_TURN_ERROR
    assert store.load(result.turn_id).phase is TurnPhase.TIMED_OUT


def test_stream_cancelled_non_deadline_maps_to_cancelled():
    def script(context: StreamRequestContext, _clock: FixedClock,
               _request: Any):
        attempt = new_id("att")
        return iter([
            StreamStarted(_envelope(context, attempt, 0), "m", "p"),
            StreamCancelled(
                _envelope(context, attempt, 1),
                CancellationReason.USER_INTERRUPT),
        ])

    provider = NativeProvider(script)
    runtime, store, _coordinator, _clock, _sessions = _owned_runtime(provider)
    result = runtime.send(
        "hello", session=runtime.start(),
        on_stream_event=lambda _event: None)

    record = store.load(result.turn_id)
    assert result.error == SUPERSEDED_TURN_ERROR
    assert result.turn_phase == TurnPhase.CANCELLED.value
    assert record.phase is TurnPhase.CANCELLED
    assert record.cancellation_reason is CancellationReason.USER_INTERRUPT


def test_sink_base_exception_is_not_misclassified_as_ordinary_failure():
    provider = NativeProvider(_success_script("never commits"))
    runtime, store, _coordinator, _clock, _sessions = _owned_runtime(provider)

    def sink(_event: Any) -> None:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        runtime.send("hello", session=runtime.start(), on_stream_event=sink)

    records = _records(store)
    assert len(records) == 1
    assert records[0].phase is TurnPhase.FAILED
