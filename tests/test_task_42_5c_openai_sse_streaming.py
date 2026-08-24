"""Task 42.5c — real OpenAI-compatible pull streaming.

All transport, retry, cancellation, and backpressure tests are deterministic.
There is no real network, wall-clock sleep, producer thread, or async queue.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agentcore.config.schema import LoggingConfig, ModelConfig, PermissionConfig
from agentcore.conversation.config import ConversationConfig
from agentcore.conversation.runtime import ConversationRuntime
from agentcore.conversation.session import InMemorySessionStore
from agentcore.conversation.tool_use import ConversationToolRunner
from agentcore.conversation.turns import TurnBridge
from agentcore.models.errors import (
    ModelAuthError,
    ModelConnectionError,
    ModelProviderError,
    ModelResponseError,
    ModelTimeoutError,
)
from agentcore.models.messages import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
from agentcore.models.sse import iter_sse_data
from agentcore.models.stream_context import StreamRequestContext
from agentcore.models.streaming import (
    MAX_RAW_SSE_FRAME_BYTES,
    MAX_STREAM_ARGUMENT_CHARS_PER_CALL,
    MAX_STREAM_ARGUMENT_CHARS_TOTAL,
    MAX_STREAM_FUNCTION_NAME_CHARS,
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
from agentcore.models.transport import (
    HttpClient,
    HttpResponse,
    HttpStreamResponse,
    RetryPolicy,
    UrllibTransport,
)
from agentcore.observability.logging import StructuredFormatter
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime import InMemoryTurnStore, TurnCoordinator
from agentcore.realtime.cancellation import CancellationReason, CancellationSource
from agentcore.realtime.turn import Deadline, OperationContext
from agentcore.tools.base import Tool, ToolResult, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import FixedClock


T0 = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
API_KEY_ENV = "TASK_42_5C_KEY"
AUTHORIZATION_SECRET = "AUTHORIZATION_SECRET_42_5C"
RAW_SSE_SECRET = "RAW_SSE_SECRET_42_5C"
TOOL_ARGUMENT_SECRET = "TOOL_ARGUMENT_SECRET_42_5C"
RAW_PROVIDER_SECRET = "RAW_PROVIDER_SECRET_42_5"
WORKSPACE_SECRET = "WORKSPACE_SECRET_42_5C"


@dataclass
class RawBody:
    chunks: list[Any]
    read_calls: int = 0
    requested_sizes: list[int] = field(default_factory=list)
    close_calls: int = 0
    closed: bool = False

    def read(self, size: int) -> bytes:
        self.read_calls += 1
        self.requested_sizes.append(size)
        if not self.chunks:
            return b""
        item = self.chunks[0]
        if isinstance(item, BaseException):
            self.chunks.pop(0)
            raise item
        data = bytes(item)
        result = data[:size]
        if len(result) == len(data):
            self.chunks.pop(0)
        else:
            self.chunks[0] = data[size:]
        return result

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class ScriptedTransport:
    def __init__(self, stream_items: list[Any] | None = None,
                 post_items: list[Any] | None = None) -> None:
        self.stream_items = list(stream_items or [])
        self.post_items = list(post_items or [])
        self.stream_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def open_stream(self, url: str, payload: Mapping[str, Any],
                    headers: Mapping[str, str],
                    timeout: float) -> HttpStreamResponse:
        self.stream_calls.append({
            "url": url,
            "payload": dict(payload),
            "headers": dict(headers),
            "timeout": timeout,
        })
        if not self.stream_items:
            raise AssertionError("stream script exhausted")
        item = self.stream_items.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, HttpStreamResponse):
            return item
        status, response_headers, raw = item
        return HttpStreamResponse(
            status=status, headers=response_headers, stream=raw)

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.post_calls.append({
            "url": url,
            "payload": dict(payload),
            "headers": dict(headers),
            "timeout": timeout,
        })
        if not self.post_items:
            raise AssertionError("post script exhausted")
        item = self.post_items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _sse(value: Any, *, ending: bytes = b"\n\n") -> bytes:
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"))
    return b"data: " + value.encode("utf-8") + ending


def _chunk(*, content: Any = None, finish: Any = None,
           tools: Any = None, usage: Any = None,
           model: Any = "wire-model") -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tools is not None:
        delta["tool_calls"] = tools
    result: dict[str, Any] = {
        "id": "provider-id-is-not-trusted",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish,
        }],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def _success_body(*parts: str) -> RawBody:
    frames = []
    for index, part in enumerate(parts):
        frames.append(_sse(_chunk(
            content=part,
            finish="stop" if index == len(parts) - 1 else None,
        )))
    frames.append(_sse("[DONE]"))
    return RawBody(frames)


def _stream_context(clock: FixedClock, *, remaining: float = 30.0,
                    source: CancellationSource | None = None,
                    request_id: str = "req_task_42_5c") -> tuple[
                        StreamRequestContext, CancellationSource]:
    owner = source or CancellationSource()
    context = StreamRequestContext(
        turn_id="turn_425c00000001",
        session_id="session_task_42_5c",
        generation=1,
        operation_id="op_425c00000001",
        cancellation=owner.token,
        deadline=Deadline.after(remaining, clock=clock),
        request_id=request_id,
    )
    return context, owner


def _request() -> ModelRequest:
    return ModelRequest(messages=[Message.user("hello")])


def _provider(
    transport: ScriptedTransport,
    *,
    max_retries: int = 1,
    backoff: float = 0.5,
    sleep: Any = None,
) -> OpenAICompatibleProvider:
    client = HttpClient(
        transport=transport,
        # Deliberately different from ModelConfig.max_retries: native stream
        # attempt truth comes from config; this policy supplies delay only.
        retry=RetryPolicy(
            max_attempts=99,
            initial_backoff=backoff,
            max_backoff=backoff,
            jitter=False,
        ),
        sleep=(sleep if sleep is not None else lambda _delay: None),
    )
    return OpenAICompatibleProvider(
        ModelConfig(
            provider="openai_compatible",
            name="configured-model",
            api_key_env=API_KEY_ENV,
            base_url="https://provider.invalid/v1",
            timeout_seconds=20.0,
            max_retries=max_retries,
        ),
        client=client,
    )


def _active_cancellation_callbacks(source: CancellationSource) -> int:
    """White-box proof that one attempt leaves no registered close hook."""
    with source._state.lock:
        return len(source._state.callbacks)


class _CountingTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "counter", "Count native stream executions.", parameters=(),
            capabilities=[Capability.FS_READ], read_only=True,
        )

    def run(self, args: Any, context: Any) -> ToolResult:
        self.runs += 1
        return ToolResult.success(f"counter-result-{self.runs}")


class _CountingPolicy(PermissionPolicy):
    def __init__(self) -> None:
        super().__init__(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
        self.check_calls = 0

    def check(self, capabilities: Any, *, description: str = ""):
        self.check_calls += 1
        return super().check(capabilities, description=description)


class _CountingExecutor(ToolExecutor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def invoke(self, *args: Any, **kwargs: Any):
        self.calls += 1
        return super().invoke(*args, **kwargs)


class _CountingRunner(ConversationToolRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls = 0

    def run_calls(self, *args: Any, **kwargs: Any):
        self.calls += 1
        return super().run_calls(*args, **kwargs)


def _tool_boundary() -> tuple[
        _CountingRunner, _CountingExecutor, _CountingPolicy, _CountingTool]:
    tool = _CountingTool()
    registry = ToolRegistry()
    assert registry.register(tool)
    policy = _CountingPolicy()
    executor = _CountingExecutor(
        registry, policy, workspace_dir="C:/workspace")
    runner = _CountingRunner(executor, registry, policy=policy)
    return runner, executor, policy, tool


def _owned_runtime(model: Any, *, tools: Any = None) -> tuple[
        ConversationRuntime, InMemorySessionStore, InMemoryTurnStore,
        FixedClock]:
    clock = FixedClock(T0)
    turn_store = InMemoryTurnStore()
    turns = TurnBridge(
        TurnCoordinator(turn_store),
        clock=clock,
        turn_deadline_seconds=120.0,
    )
    sessions = InMemorySessionStore()
    runtime = ConversationRuntime(
        model=model,
        tools=tools,
        sessions=sessions,
        turns=turns,
        config=ConversationConfig(turn_deadline_seconds=120.0),
    )
    return runtime, sessions, turn_store, clock


@pytest.fixture(autouse=True)
def _credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, AUTHORIZATION_SECRET)


def _collect(provider: OpenAICompatibleProvider,
             context: StreamRequestContext,
             clock: FixedClock) -> list[Any]:
    return list(provider.stream(_request(), context=context, clock=clock))


# ---------------------------------------------------------------------------
# Bounded SSE parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_sse_parser_supports_standard_line_endings_and_comments(ending: bytes):
    raw = RawBody([
        b": ignored" + ending +
        b"data: {\"a\":" + ending +
        b"data: 1}" + ending + ending,
    ])
    assert list(iter_sse_data(raw)) == ['{"a":\n1}']
    assert all(0 < size <= 4096 for size in raw.requested_sizes)


def test_sse_parser_suspends_after_first_frame_without_another_read():
    raw = RawBody([_sse("one"), _sse("two")])
    iterator = iter(iter_sse_data(raw))
    assert next(iterator) == "one"
    assert raw.read_calls == 1
    assert next(iterator) == "two"
    assert raw.read_calls == 2


def test_sse_parser_pauses_mid_buffer_after_first_of_two_frames():
    raw = RawBody([_sse("one") + _sse("two")])
    iterator = iter(iter_sse_data(raw))
    assert next(iterator) == "one"
    assert raw.read_calls == 1
    assert next(iterator) == "two"
    assert raw.read_calls == 1


def test_sse_parser_handles_split_utf8_and_split_crlf_boundaries():
    encoded = "é".encode("utf-8")
    raw = RawBody([
        b"data: " + encoded[:1],
        encoded[1:] + b"\r",
        b"\n\r",
        b"\n",
    ])
    assert list(iter_sse_data(raw)) == ["é"]


def test_sse_parser_enforces_exact_incremental_frame_bound():
    exact = RawBody([b"data:x\n\n"])
    assert list(iter_sse_data(exact, max_frame_bytes=8)) == ["x"]
    oversized = RawBody([b"data:x\n\n"])
    with pytest.raises(ModelResponseError, match="byte limit"):
        list(iter_sse_data(oversized, max_frame_bytes=7))


def test_sse_parser_rejects_no_newline_oversize_while_accumulating():
    raw = RawBody([b"x" * (MAX_RAW_SSE_FRAME_BYTES + 1)])
    with pytest.raises(ModelResponseError, match="byte limit"):
        list(iter_sse_data(raw))


@pytest.mark.parametrize("body", [
    b"data: \xff\n\n",
    b": \xff\n\n",
    b"event: message\n\n",
    b"data: {\"a\":1}",
])
def test_sse_parser_fails_closed_without_raw_content(body: bytes):
    with pytest.raises(ModelResponseError) as caught:
        list(iter_sse_data(RawBody([body])))
    text = str(caught.value)
    assert RAW_SSE_SECRET not in text
    assert body.decode("utf-8", errors="ignore") not in text


def test_http_stream_response_prefers_read1_and_closes_once():
    class ReadOne:
        def __init__(self) -> None:
            self.read_calls = 0
            self.read1_calls = 0
            self.close_calls = 0

        def read(self, _size: int) -> bytes:
            self.read_calls += 1
            return b"wrong"

        def read1(self, _size: int) -> bytes:
            self.read1_calls += 1
            return b"right"

        def close(self) -> None:
            self.close_calls += 1

    raw = ReadOne()
    response = HttpStreamResponse(status=200, headers={}, stream=raw)
    assert response.read(10) == b"right"
    response.close()
    response.close()
    assert (raw.read1_calls, raw.read_calls, raw.close_calls) == (1, 0, 1)


def test_http_stream_response_concurrent_close_is_idempotent():
    gate = threading.Barrier(9)

    class OwnedRaw:
        def __init__(self) -> None:
            self.close_calls = 0

        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            self.close_calls += 1

    raw = OwnedRaw()
    response = HttpStreamResponse(status=200, headers={}, stream=raw)

    def close_at_gate() -> None:
        gate.wait()
        response.close()

    threads = [threading.Thread(target=close_at_gate) for _ in range(8)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(10.0)
        assert not thread.is_alive()
    assert raw.close_calls == 1


def test_http_stream_response_second_close_waits_for_owned_close_completion():
    close_entered = threading.Event()
    release_close = threading.Event()

    class BlockingCloseRaw:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            close_entered.set()
            assert release_close.wait(10.0)

    class WaitSpy:
        def __init__(self) -> None:
            self.inner = threading.Event()
            self.wait_entered = threading.Event()

        def wait(self) -> bool:
            self.wait_entered.set()
            return self.inner.wait()

        def set(self) -> None:
            self.inner.set()

    response = HttpStreamResponse(
        status=200, headers={}, stream=BlockingCloseRaw())
    wait_spy = WaitSpy()
    response._close_done = wait_spy
    first = threading.Thread(target=response.close)
    first.start()
    assert close_entered.wait(10.0)

    second_done = threading.Event()

    def second_close() -> None:
        response.close()
        second_done.set()

    second = threading.Thread(target=second_close)
    second.start()
    assert wait_spy.wait_entered.wait(10.0)
    assert not second_done.is_set()

    release_close.set()
    first.join(10.0)
    second.join(10.0)
    assert not first.is_alive() and not second.is_alive()
    assert second_done.is_set()


def test_urllib_stream_seam_issues_one_post_and_does_not_read_success(
        monkeypatch: pytest.MonkeyPatch):
    class FakeWireResponse:
        status = 200
        headers = {"X-Test": "ok"}

        def __init__(self) -> None:
            self.read_calls = 0
            self.close_calls = 0

        def read(self, _size: int) -> bytes:
            self.read_calls += 1
            return b""

        def close(self) -> None:
            self.close_calls += 1

    class FakeOpener:
        def __init__(self, response: FakeWireResponse) -> None:
            self.response = response
            self.calls: list[tuple[Any, float]] = []

        def open(self, request: Any, *, timeout: float):
            self.calls.append((request, timeout))
            return self.response

    raw = FakeWireResponse()
    opener = FakeOpener(raw)
    handlers: list[Any] = []

    def build_opener(*items: Any) -> FakeOpener:
        handlers.extend(items)
        return opener

    monkeypatch.setattr("urllib.request.build_opener", build_opener)
    response = UrllibTransport().open_stream(
        "https://provider.invalid/v1/chat/completions",
        {"model": "m", "stream": True},
        {"Authorization": f"Bearer {AUTHORIZATION_SECRET}"},
        7.5,
    )
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.get_method() == "POST"
    assert timeout == 7.5
    assert json.loads(request.data) == {"model": "m", "stream": True}
    assert len(handlers) == 1
    assert handlers[0].redirect_request(
        None, None, 307, "redirect", {}, "https://elsewhere.invalid",
    ) is None
    assert raw.read_calls == 0
    response.close()
    assert raw.close_calls == 1


# ---------------------------------------------------------------------------
# Payload, identity, text, usage, and one-shot compatibility
# ---------------------------------------------------------------------------


def test_valid_text_stream_is_incremental_canonical_and_raw_free():
    clock = FixedClock(T0)
    body = _success_body("hel", "lo")
    transport = ScriptedTransport([(200, {}, body)])
    provider = _provider(transport)
    context, source = _stream_context(clock)
    iterator = provider.stream(_request(), context=context, clock=clock)

    started = next(iterator)
    assert isinstance(started, StreamStarted)
    assert transport.stream_calls == []
    assert body.read_calls == 0
    first = next(iterator)
    assert isinstance(first, TextDelta) and first.text == "hel"
    assert body.read_calls == 1
    events = [started, first, *list(iterator)]

    assert [type(event) for event in events] == [
        StreamStarted, TextDelta, TextDelta, ResponseCompleted]
    assert [event.envelope.sequence for event in events] == [0, 1, 2, 3]
    terminal = events[-1]
    assert terminal.response == ModelResponse(
        text="hello",
        model="wire-model",
        provider="openai_compatible",
        finish_reason=FinishReason.STOP,
        usage=Usage(),
        tool_calls=[],
        raw={},
        latency_ms=0.0,
    )
    assert body.close_calls == 1
    sent = transport.stream_calls[0]
    assert sent["payload"]["stream"] is True
    assert "stream_options" not in sent["payload"]
    wire = json.dumps(sent["payload"])
    for trusted in (
        context.turn_id, context.session_id, context.operation_id,
        context.request_id,
    ):
        assert trusted not in wire
    assert sent["headers"]["Authorization"] == \
        f"Bearer {AUTHORIZATION_SECRET}"
    assert _active_cancellation_callbacks(source) == 0


def test_generate_stays_on_post_json_without_stream_flag_or_raw_scrub():
    one_shot = {
        "id": "ordinary",
        "model": "configured-model",
        "choices": [{
            "message": {"content": "ordinary"},
            "finish_reason": "stop",
        }],
        "secret": RAW_PROVIDER_SECRET,
    }
    transport = ScriptedTransport(
        stream_items=[(200, {}, _success_body("unused"))],
        post_items=[HttpResponse(200, json.dumps(one_shot).encode())],
    )
    response = _provider(transport).generate(_request())
    assert response.text == "ordinary"
    assert response.raw is one_shot or response.raw == one_shot
    assert RAW_PROVIDER_SECRET in json.dumps(response.raw)
    assert len(transport.post_calls) == 1
    assert transport.stream_calls == []
    assert "stream" not in transport.post_calls[0]["payload"]
    assert "stream_options" not in transport.post_calls[0]["payload"]


def test_conversation_without_sink_stays_on_ordinary_generate_path():
    one_shot = {
        "model": "configured-model",
        "choices": [{
            "message": {"content": "ordinary runtime reply"},
            "finish_reason": "stop",
        }],
    }
    transport = ScriptedTransport(
        stream_items=[(200, {}, _success_body("must-not-stream"))],
        post_items=[HttpResponse(200, json.dumps(one_shot).encode())],
    )
    provider = _provider(transport)
    sessions = InMemorySessionStore()
    runtime = ConversationRuntime(model=provider, sessions=sessions)
    session = runtime.start()

    result = runtime.send("hello", session=session)

    assert result.ok and result.reply.content == "ordinary runtime reply"
    assert len(transport.post_calls) == 1
    assert transport.stream_calls == []
    assert "stream" not in transport.post_calls[0]["payload"]


def test_native_openai_text_stream_integrates_through_conversation_runtime():
    transport = ScriptedTransport([(200, {}, _success_body("hel", "lo"))])
    provider = _provider(transport)
    runtime, sessions, _turn_store, _clock = _owned_runtime(provider)
    session = runtime.start()
    seen: list[Any] = []

    result = runtime.send(
        "say hello", session=session, on_stream_event=seen.append)

    assert [type(event) for event in seen] == [
        StreamStarted, TextDelta, TextDelta, ResponseCompleted]
    assert [event.text for event in seen if isinstance(event, TextDelta)] == [
        "hel", "lo"]
    assert seen[-1].response.raw == {}
    assert result.ok and result.reply.content == "hello"
    assert [message.content for message in session.messages] == [
        "say hello", "hello"]
    persisted = sessions.load(session.id)
    assert [message.content for message in persisted.messages] == [
        "say hello", "hello"]
    assert all(message.content not in {"hel", "lo"}
               for message in persisted.messages)
    assert len(transport.stream_calls) == 1


def test_stream_requires_trusted_request_context_before_http():
    clock = FixedClock(T0)
    source = CancellationSource()
    plain = OperationContext(
        turn_id="turn_425c00000002",
        session_id="session_plain",
        generation=1,
        operation_id="op_425c00000002",
        cancellation=source.token,
        deadline=Deadline.after(10, clock=clock),
    )
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    provider = _provider(transport)
    with pytest.raises(ModelResponseError, match="trusted request"):
        list(provider.stream(_request(), context=plain, clock=clock))
    assert transport.stream_calls == []


def test_usage_only_chunk_is_nonsemantic_and_latest_usage_is_canonical():
    clock = FixedClock(T0)
    first_usage = {"prompt_tokens": 99, "vendor": WORKSPACE_SECRET}
    latest_usage = {"prompt_tokens": 2, "completion_tokens": 3,
                    "total_tokens": 0}
    body = RawBody([
        _sse({"model": "wire-model", "choices": [],
              "usage": first_usage}),
        _sse(_chunk(
            content="ok", finish="stop", usage=latest_usage)),
        _sse("[DONE]"),
    ])
    transport = ScriptedTransport([(200, {}, body)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    usage_events = [event.usage for event in events
                    if isinstance(event, UsageUpdated)]
    assert usage_events == [Usage(99, 0, 99), Usage(2, 3, 5)]
    terminal = events[-1]
    assert terminal.response.usage == Usage(2, 3, 5)
    assert WORKSPACE_SECRET not in repr(events)


@pytest.mark.parametrize("usage", [
    "bad",
    {"prompt_tokens": True},
    {"completion_tokens": -1},
    {"total_tokens": 1.5},
])
def test_malformed_usage_fails_nonretryably(usage: Any):
    clock = FixedClock(T0)
    body = RawBody([_sse(_chunk(usage=usage))])
    transport = ScriptedTransport([(200, {}, body)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"
    assert events[-1].retryable is False
    assert len(transport.stream_calls) == 1


def test_provider_chunk_identity_cannot_replace_trusted_envelope_identity():
    clock = FixedClock(T0)
    body = RawBody([_sse({
        "id": "req_provider_forgery",
        "attempt_id": "att_provider_forgery",
        **_chunk(content="ok", finish="stop"),
    }), _sse("[DONE]")])
    transport = ScriptedTransport([(200, {}, body)])
    context, _ = _stream_context(clock, request_id="req_trusted")
    events = _collect(_provider(transport), context, clock)
    assert {event.envelope.request_id for event in events} == {"req_trusted"}
    assert all(event.envelope.attempt_id != "att_provider_forgery"
               for event in events)


def test_conflicting_provider_model_fails_closed_without_values():
    clock = FixedClock(T0)
    body = RawBody([
        _sse(_chunk(model="model-a")),
        _sse(_chunk(model="model-b")),
    ])
    transport = ScriptedTransport([(200, {}, body)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"
    assert "model-a" not in repr(events) and "model-b" not in repr(events)


@pytest.mark.parametrize("wire,expected", [
    ("stop", FinishReason.STOP),
    ("length", FinishReason.LENGTH),
    ("max_tokens", FinishReason.LENGTH),
    ("tool_calls", FinishReason.TOOL_CALLS),
    ("function_call", FinishReason.TOOL_CALLS),
    ("content_filter", FinishReason.CONTENT_FILTER),
    ("vendor_specific", FinishReason.UNKNOWN),
])
def test_stream_finish_reason_matches_one_shot_vocabulary(
        wire: str, expected: FinishReason):
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(content="ok", finish=wire)),
        _sse("[DONE]"),
    ])
    context, _ = _stream_context(clock)
    events = _collect(
        _provider(ScriptedTransport([(200, {}, raw)])), context, clock)
    assert isinstance(events[-1], ResponseCompleted)
    assert events[-1].response.finish_reason is expected


def test_done_without_finish_reason_completes_as_unknown():
    clock = FixedClock(T0)
    raw = RawBody([_sse(_chunk(content="ok")), _sse("[DONE]")])
    context, _ = _stream_context(clock)
    events = _collect(
        _provider(ScriptedTransport([(200, {}, raw)])), context, clock)
    assert events[-1].response.finish_reason is FinishReason.UNKNOWN


@pytest.mark.parametrize("first_reason,second_reason", [
    ("stop", "length"),
    ("vendor_a", "vendor_b"),
])
def test_conflicting_finish_reason_transition_fails_closed(
        first_reason: str, second_reason: str):
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(content="ok", finish=first_reason)),
        _sse(_chunk(finish=second_reason)),
        _sse("[DONE]"),
    ])
    context, _ = _stream_context(clock)
    events = _collect(
        _provider(ScriptedTransport([(200, {}, raw)])), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"


# ---------------------------------------------------------------------------
# Provider-owned semantic retry and shared deadline
# ---------------------------------------------------------------------------


def test_connection_failure_before_semantic_retries_with_fresh_attempt():
    clock = FixedClock(T0)
    slept: list[float] = []
    transport = ScriptedTransport([
        ModelConnectionError("RAW_SSE_SECRET_42_5C"),
        (200, {}, _success_body("ok")),
    ])
    context, _ = _stream_context(clock)
    events = _collect(_provider(
        transport, max_retries=2, sleep=slept.append), context, clock)
    assert [type(event) for event in events] == [
        StreamStarted, StreamFailed, StreamStarted,
        TextDelta, ResponseCompleted]
    first_failed = events[1]
    assert (first_failed.retryable, first_failed.will_retry) == (True, True)
    assert events[0].envelope.request_id == events[2].envelope.request_id
    assert events[0].envelope.attempt_id != events[2].envelope.attempt_id
    assert events[2].envelope.sequence == 0
    assert len(transport.stream_calls) == 2
    assert slept == [0.5]
    assert RAW_SSE_SECRET not in repr(events)


@pytest.mark.parametrize("status,headers,error_kind", [
    (429, {"retry-after": "0"}, "ModelRateLimitError"),
    (408, {}, "ModelTimeoutError"),
    (500, {}, "ModelProviderError"),
    (503, {}, "ModelProviderError"),
    (504, {}, "ModelTimeoutError"),
])
def test_retryable_http_status_before_semantic_retries(
        status: int, headers: dict[str, str], error_kind: str):
    clock = FixedClock(T0)
    failed_raw = RawBody([b"must-not-be-read"])
    transport = ScriptedTransport([
        (status, headers, failed_raw),
        (200, {}, _success_body("ok")),
    ])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    failure = next(event for event in events if isinstance(event, StreamFailed))
    assert failure.error_kind == error_kind
    assert failure.will_retry is True
    assert failed_raw.read_calls == 0
    assert failed_raw.close_calls == 1
    assert len(transport.stream_calls) == 2


def test_zero_retry_after_is_respected_instead_of_backoff():
    clock = FixedClock(T0)
    slept: list[float] = []
    transport = ScriptedTransport([
        (429, {"retry-after": "0"}, RawBody([b"unread"])),
        (200, {}, _success_body("ok")),
    ])
    context, _ = _stream_context(clock)
    events = _collect(_provider(
        transport, max_retries=2, backoff=7.0, sleep=slept.append),
        context, clock)
    assert any(isinstance(event, ResponseCompleted) for event in events)
    assert slept == [0.0]


def test_physical_stream_read_timeout_retries_before_semantic_output():
    clock = FixedClock(T0)
    first = RawBody([TimeoutError("raw timeout text is discarded")])
    transport = ScriptedTransport([
        (200, {}, first),
        (200, {}, _success_body("ok")),
    ])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    failure = next(event for event in events if isinstance(event, StreamFailed))
    assert failure.error_kind == "ModelTimeoutError"
    assert (failure.retryable, failure.will_retry) == (True, True)
    assert first.close_calls == 1
    assert len(transport.stream_calls) == 2


@pytest.mark.parametrize("status,error_kind", [
    (401, "ModelAuthError"),
    (403, "ModelAuthError"),
    (400, "ModelProviderError"),
])
def test_nonretryable_http_status_never_retries(status: int, error_kind: str):
    clock = FixedClock(T0)
    raw = RawBody([b"secret body"])
    transport = ScriptedTransport([
        (status, {}, raw),
        (200, {}, _success_body("must-not-run")),
    ])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    terminal = events[-1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == error_kind
    assert (terminal.retryable, terminal.will_retry) == (False, False)
    assert len(transport.stream_calls) == 1
    assert raw.read_calls == 0 and raw.close_calls == 1


def test_stream_http_error_body_and_authorization_never_enter_error_or_logs(
        caplog: pytest.LogCaptureFixture):
    caplog.set_level("WARNING")
    raw = RawBody([(
        RAW_PROVIDER_SECRET + AUTHORIZATION_SECRET + WORKSPACE_SECRET
    ).encode()])
    transport = ScriptedTransport([(500, {}, raw)])
    client = HttpClient(
        transport=transport,
        retry=RetryPolicy(max_attempts=99, jitter=False),
        sleep=lambda _delay: None,
    )

    with pytest.raises(ModelProviderError) as caught:
        client.open_stream(
            "https://provider.invalid/v1/chat/completions",
            {"stream": True},
            {"Authorization": f"Bearer {AUTHORIZATION_SECRET}"},
            5.0,
            secrets=(AUTHORIZATION_SECRET,),
        )

    public = " ".join([
        str(caught.value), repr(caught.value.details),
        json.dumps(caught.value.to_dict()), caplog.text,
    ])
    for secret in (
        RAW_PROVIDER_SECRET, AUTHORIZATION_SECRET, WORKSPACE_SECRET,
    ):
        assert secret not in public
    assert raw.read_calls == 0 and raw.close_calls == 1


def test_stream_retry_log_is_categorical_and_content_free():
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(StructuredFormatter(LoggingConfig(level="WARNING")))
    root = logging.getLogger("agentcore")
    original_level = root.level
    root.setLevel(logging.WARNING)
    root.addHandler(handler)
    secret_message = " ".join([
        RAW_PROVIDER_SECRET, RAW_SSE_SECRET,
        AUTHORIZATION_SECRET, WORKSPACE_SECRET,
    ])
    transport = ScriptedTransport([
        ModelConnectionError(secret_message),
        (200, {}, _success_body("ok")),
    ])
    clock = FixedClock(T0)
    context, _ = _stream_context(clock)
    try:
        _collect(_provider(transport, max_retries=2), context, clock)
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)
        handler.close()

    emitted = buffer.getvalue()
    assert '"event": "model.retry"' in emitted
    assert '"error": "ModelConnectionError"' in emitted
    for secret in (
        RAW_PROVIDER_SECRET, RAW_SSE_SECRET,
        AUTHORIZATION_SECRET, WORKSPACE_SECRET,
    ):
        assert secret not in emitted


def test_usage_then_connection_failure_remains_retryable_and_resets_usage():
    clock = FixedClock(T0)
    first = RawBody([
        _sse({
            "model": "wire-model",
            "choices": [],
            "usage": {"prompt_tokens": 99},
        }),
        ModelConnectionError("transient"),
    ])
    second = _success_body("ok")
    transport = ScriptedTransport([(200, {}, first), (200, {}, second)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert [type(event) for event in events[:4]] == [
        StreamStarted, UsageUpdated, StreamFailed, StreamStarted]
    assert events[2].will_retry is True
    assert events[-1].response.usage == Usage()


@pytest.mark.parametrize("semantic_frame,event_type", [
    (_sse(_chunk(content="h")), TextDelta),
    (_sse(_chunk(tools=[{
        "index": 0,
        "id": "call_visible",
        "type": "function",
        "function": {"name": "echo", "arguments": ""},
    }])), ToolCallDelta),
])
def test_transient_failure_after_semantic_delta_never_retries(
        semantic_frame: bytes, event_type: type[Any]):
    clock = FixedClock(T0)
    body = RawBody([semantic_frame, ModelConnectionError("transient")])
    transport = ScriptedTransport([
        (200, {}, body), (200, {}, _success_body("duplicate"))])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert any(isinstance(event, event_type) for event in events)
    terminal = events[-1]
    assert isinstance(terminal, StreamFailed)
    assert (terminal.retryable, terminal.will_retry) == (True, False)
    assert len(transport.stream_calls) == 1


@pytest.mark.parametrize("remaining,backoff", [(1.0, 1.0), (1.0, 2.0)])
def test_retry_delay_equal_or_greater_than_remaining_forbids_retry(
        remaining: float, backoff: float):
    clock = FixedClock(T0)
    slept: list[float] = []
    transport = ScriptedTransport([
        ModelConnectionError("transient"),
        (200, {}, _success_body("late")),
    ])
    context, _ = _stream_context(clock, remaining=remaining)
    events = _collect(_provider(
        transport, max_retries=2, backoff=backoff,
        sleep=slept.append), context, clock)
    terminal = events[-1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.retryable is True and terminal.will_retry is False
    assert len(transport.stream_calls) == 1
    assert slept == []


def test_all_attempt_timeouts_share_original_deadline_and_are_clamped():
    clock = FixedClock(T0)
    slept: list[float] = []

    def sleep_and_advance(delay: float) -> None:
        slept.append(delay)
        clock.advance(delay)

    transport = ScriptedTransport([
        ModelConnectionError("transient"),
        (200, {}, _success_body("ok")),
    ])
    context, _ = _stream_context(clock, remaining=10.0)
    _collect(_provider(
        transport, max_retries=2, backoff=1.0,
        sleep=sleep_and_advance), context, clock)
    assert [call["timeout"] for call in transport.stream_calls] == [10.0, 9.0]
    assert slept == [1.0]


def test_deadline_winning_during_failed_open_cancels_without_retry():
    clock = FixedClock(T0)

    def expire_and_fail() -> Any:
        clock.advance(2.0)
        raise ModelConnectionError("late network return")

    transport = ScriptedTransport([
        expire_and_fail,
        (200, {}, _success_body("late")),
    ])
    context, _ = _stream_context(clock, remaining=1.0)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert [type(event) for event in events] == [
        StreamStarted, StreamCancelled]
    assert events[-1].reason is CancellationReason.DEADLINE_EXCEEDED
    assert len(transport.stream_calls) == 1


def test_retry_promise_cancellation_gets_new_started_then_cancelled_no_wire():
    clock = FixedClock(T0)
    transport = ScriptedTransport([
        ModelConnectionError("transient"),
        (200, {}, _success_body("must-not-open")),
    ])
    context, source = _stream_context(clock)
    iterator = _provider(transport, max_retries=2).stream(
        _request(), context=context, clock=clock)
    started = next(iterator)
    failed = next(iterator)
    assert isinstance(failed, StreamFailed) and failed.will_retry
    source.cancel(CancellationReason.OPERATOR_CANCEL)
    retried_started = next(iterator)
    cancelled = next(iterator)
    with pytest.raises(StopIteration):
        next(iterator)
    assert isinstance(retried_started, StreamStarted)
    assert retried_started.envelope.sequence == 0
    assert retried_started.envelope.attempt_id != started.envelope.attempt_id
    assert isinstance(cancelled, StreamCancelled)
    assert cancelled.reason is CancellationReason.OPERATOR_CANCEL
    assert len(transport.stream_calls) == 1


def test_retry_promise_shrunk_backoff_budget_never_opens_without_backoff():
    clock = FixedClock(T0)
    transport = ScriptedTransport([
        ModelConnectionError("transient"),
        (200, {}, _success_body("must-not-open")),
    ])
    context, _ = _stream_context(clock, remaining=2.0)
    iterator = _provider(
        transport, max_retries=2, backoff=1.0).stream(
            _request(), context=context, clock=clock)
    first = next(iterator)
    failed = next(iterator)
    assert isinstance(failed, StreamFailed) and failed.will_retry
    clock.advance(1.5)
    second = next(iterator)
    terminal = next(iterator)
    assert isinstance(second, StreamStarted)
    assert second.envelope.attempt_id != first.envelope.attempt_id
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "ModelTimeoutError"
    assert terminal.will_retry is False
    assert len(transport.stream_calls) == 1


def test_cancellation_during_retry_log_prevents_promised_backoff_and_wire():
    clock = FixedClock(T0)
    slept: list[float] = []
    transport = ScriptedTransport([
        ModelConnectionError("transient"),
        (200, {}, _success_body("must-not-open")),
    ])
    context, source = _stream_context(clock)
    provider = _provider(
        transport, max_retries=2, backoff=1.0, sleep=slept.append)
    log_entered = threading.Event()
    release_log = threading.Event()

    class GateLogger:
        def warning(self, _event: str, **_fields: Any) -> None:
            log_entered.set()
            assert release_log.wait(10.0)

    provider._log = GateLogger()
    iterator = provider.stream(_request(), context=context, clock=clock)
    first = next(iterator)
    failed = next(iterator)
    assert isinstance(first, StreamStarted)
    assert isinstance(failed, StreamFailed) and failed.will_retry
    observed: list[Any] = []

    thread = threading.Thread(target=lambda: observed.append(next(iterator)))
    thread.start()
    assert log_entered.wait(10.0)
    source.cancel(CancellationReason.SUPERSEDED)
    release_log.set()
    thread.join(10.0)

    assert not thread.is_alive()
    assert len(observed) == 1 and isinstance(observed[0], StreamStarted)
    cancelled = next(iterator)
    assert isinstance(cancelled, StreamCancelled)
    assert cancelled.reason is CancellationReason.SUPERSEDED
    assert slept == []
    assert len(transport.stream_calls) == 1


# ---------------------------------------------------------------------------
# Cancellation winner, handle closure, registration cleanup, generator close
# ---------------------------------------------------------------------------


def test_cancellation_before_open_emits_trusted_terminal_and_zero_http():
    clock = FixedClock(T0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    context, source = _stream_context(clock)
    source.cancel(CancellationReason.USER_INTERRUPT)
    events = _collect(_provider(transport), context, clock)
    assert [type(event) for event in events] == [
        StreamStarted, StreamCancelled]
    assert events[-1].reason is CancellationReason.USER_INTERRUPT
    assert transport.stream_calls == []


def test_pre_cancelled_stream_wins_before_missing_credential_setup(
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(API_KEY_ENV)
    clock = FixedClock(T0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    context, source = _stream_context(clock)
    source.cancel(CancellationReason.USER_INTERRUPT)

    events = _collect(_provider(transport), context, clock)

    assert [type(event) for event in events] == [
        StreamStarted, StreamCancelled]
    assert events[-1].reason is CancellationReason.USER_INTERRUPT
    assert transport.stream_calls == []


def test_missing_stream_credential_is_categorical_attempt_failure(
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(API_KEY_ENV)
    clock = FixedClock(T0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    context, _ = _stream_context(clock)

    events = _collect(_provider(transport), context, clock)

    assert [type(event) for event in events] == [StreamStarted, StreamFailed]
    assert events[-1].error_kind == "ModelAuthError"
    assert (events[-1].retryable, events[-1].will_retry) == (False, False)
    assert transport.stream_calls == []


def test_cancellation_immediately_after_open_closes_owned_handle_once():
    clock = FixedClock(T0)
    context, source = _stream_context(clock)
    raw = _success_body("no")

    def cancel_before_return() -> tuple[int, dict[str, str], RawBody]:
        source.cancel(CancellationReason.AGENT_SHUTDOWN)
        return 200, {}, raw

    transport = ScriptedTransport([cancel_before_return])
    events = _collect(_provider(transport), context, clock)
    assert [type(event) for event in events] == [
        StreamStarted, StreamCancelled]
    assert events[-1].reason is CancellationReason.AGENT_SHUTDOWN
    assert raw.read_calls == 0
    assert raw.close_calls == 1
    assert _active_cancellation_callbacks(source) == 0


def test_cancellation_while_blocked_read_closes_handle_and_reason_wins():
    clock = FixedClock(T0)
    context, source = _stream_context(clock)
    entered = threading.Event()
    closed = threading.Event()

    class BlockingRaw:
        def __init__(self) -> None:
            self.close_calls = 0

        def read(self, _size: int) -> bytes:
            entered.set()
            assert closed.wait(10.0)
            raise ModelConnectionError("close woke read")

        def close(self) -> None:
            self.close_calls += 1
            closed.set()

    raw = BlockingRaw()
    transport = ScriptedTransport([(200, {}, raw)])
    iterator = _provider(transport).stream(
        _request(), context=context, clock=clock)
    assert isinstance(next(iterator), StreamStarted)
    observed: list[Any] = []

    def pull() -> None:
        observed.append(next(iterator))

    thread = threading.Thread(target=pull)
    thread.start()
    assert entered.wait(10.0)
    source.cancel(CancellationReason.SUPERSEDED)
    thread.join(10.0)
    assert not thread.is_alive()
    assert len(observed) == 1
    assert isinstance(observed[0], StreamCancelled)
    assert observed[0].reason is CancellationReason.SUPERSEDED
    assert raw.close_calls == 1


def test_earlier_cancellation_beats_later_deadline():
    clock = FixedClock(T0)
    context, source = _stream_context(clock, remaining=1.0)
    source.cancel(CancellationReason.SUPERSEDED)
    clock.advance(2.0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    events = _collect(_provider(transport), context, clock)
    assert events[-1].reason is CancellationReason.SUPERSEDED
    assert transport.stream_calls == []


def test_cancellation_linearizing_during_deadline_read_wins_trusted_race():
    base_clock = FixedClock(T0)
    entered_deadline_read = threading.Event()
    release_deadline_read = threading.Event()

    class GatedClock:
        def __init__(self) -> None:
            self.monotonic_calls = 0

        def now(self):
            return base_clock.now()

        def monotonic(self) -> float:
            self.monotonic_calls += 1
            if self.monotonic_calls == 2:
                entered_deadline_read.set()
                assert release_deadline_read.wait(10.0)
            return base_clock.monotonic()

    gated_clock = GatedClock()
    context, source = _stream_context(base_clock, remaining=1.0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    iterator = _provider(transport).stream(
        _request(), context=context, clock=gated_clock)
    assert isinstance(next(iterator), StreamStarted)
    observed: list[Any] = []

    thread = threading.Thread(target=lambda: observed.append(next(iterator)))
    thread.start()
    assert entered_deadline_read.wait(10.0)
    source.cancel(CancellationReason.SUPERSEDED)
    base_clock.advance(2.0)
    release_deadline_read.set()
    thread.join(10.0)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert isinstance(observed[0], StreamCancelled)
    assert observed[0].reason is CancellationReason.SUPERSEDED
    assert transport.stream_calls == []


def test_cancellation_during_unexpired_pre_yield_clock_read_blocks_delta():
    base_clock = FixedClock(T0)
    clock_entered = threading.Event()
    release_clock = threading.Event()

    class ArmedClock:
        def __init__(self) -> None:
            self.armed = False

        def now(self):
            return base_clock.now()

        def monotonic(self) -> float:
            if self.armed:
                self.armed = False
                clock_entered.set()
                assert release_clock.wait(10.0)
            return base_clock.monotonic()

    gated_clock = ArmedClock()

    class ArmAfterFrameRead(RawBody):
        def read(self, size: int) -> bytes:
            chunk = super().read(size)
            if chunk:
                gated_clock.armed = True
            return chunk

    raw = ArmAfterFrameRead([
        _sse(_chunk(content="must-not-yield")),
        _sse(_chunk(content="later", finish="stop")),
        _sse("[DONE]"),
    ])
    context, source = _stream_context(base_clock, remaining=30.0)
    transport = ScriptedTransport([(200, {}, raw)])
    iterator = _provider(transport).stream(
        _request(), context=context, clock=gated_clock)
    assert isinstance(next(iterator), StreamStarted)
    observed: list[Any] = []

    thread = threading.Thread(target=lambda: observed.append(next(iterator)))
    thread.start()
    assert clock_entered.wait(10.0)
    source.cancel(CancellationReason.SUPERSEDED)
    release_clock.set()
    thread.join(10.0)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert isinstance(observed[0], StreamCancelled)
    assert observed[0].reason is CancellationReason.SUPERSEDED
    assert raw.close_calls == 1


def test_cancellation_linearizing_during_remaining_read_opens_no_wire():
    base_clock = FixedClock(T0)
    entered_remaining_read = threading.Event()
    release_remaining_read = threading.Event()

    class GatedClock:
        def __init__(self) -> None:
            self.monotonic_calls = 0

        def now(self):
            return base_clock.now()

        def monotonic(self) -> float:
            self.monotonic_calls += 1
            # logical start, preflight expiry, effective-timeout expiry, and
            # remaining-helper expiry precede Deadline.remaining's own read.
            if self.monotonic_calls == 5:
                entered_remaining_read.set()
                assert release_remaining_read.wait(10.0)
            return base_clock.monotonic()

    gated_clock = GatedClock()
    context, source = _stream_context(base_clock, remaining=30.0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    iterator = _provider(transport).stream(
        _request(), context=context, clock=gated_clock)
    assert isinstance(next(iterator), StreamStarted)
    observed: list[Any] = []

    thread = threading.Thread(target=lambda: observed.append(next(iterator)))
    thread.start()
    assert entered_remaining_read.wait(10.0)
    source.cancel(CancellationReason.SUPERSEDED)
    release_remaining_read.set()
    thread.join(10.0)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert isinstance(observed[0], StreamCancelled)
    assert observed[0].reason is CancellationReason.SUPERSEDED
    assert transport.stream_calls == []


def test_cancellation_during_failure_retry_budget_read_becomes_cancelled():
    base_clock = FixedClock(T0)
    entered_retry_remaining = threading.Event()
    release_retry_remaining = threading.Event()

    class GatedClock:
        def __init__(self) -> None:
            self.monotonic_calls = 0

        def now(self):
            return base_clock.now()

        def monotonic(self) -> float:
            self.monotonic_calls += 1
            # The ninth read is Deadline.remaining in the failed-attempt retry
            # decision, after attempt-timeout preflight and winner checks.
            if self.monotonic_calls == 9:
                entered_retry_remaining.set()
                assert release_retry_remaining.wait(10.0)
            return base_clock.monotonic()

    gated_clock = GatedClock()
    context, source = _stream_context(base_clock, remaining=30.0)
    transport = ScriptedTransport([
        ModelConnectionError("failed open"),
        (200, {}, _success_body("must-not-open")),
    ])
    iterator = _provider(transport, max_retries=2).stream(
        _request(), context=context, clock=gated_clock)
    assert isinstance(next(iterator), StreamStarted)
    observed: list[Any] = []

    thread = threading.Thread(target=lambda: observed.append(next(iterator)))
    thread.start()
    assert entered_retry_remaining.wait(10.0)
    source.cancel(CancellationReason.SUPERSEDED)
    release_retry_remaining.set()
    thread.join(10.0)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert isinstance(observed[0], StreamCancelled)
    assert observed[0].reason is CancellationReason.SUPERSEDED
    assert len(transport.stream_calls) == 1


def test_transport_cannot_fabricate_private_cancellation_signal():
    import agentcore.models.providers.openai_compatible as module

    clock = FixedClock(T0)
    context, source = _stream_context(clock)
    transport = ScriptedTransport([
        module._TrustedStreamCancellation(CancellationReason.SUPERSEDED),
    ])

    events = _collect(_provider(transport), context, clock)

    assert source.token.requested is False
    assert [type(event) for event in events] == [StreamStarted, StreamFailed]
    assert events[-1].error_kind == "ModelResponseError"
    assert events[-1].retryable is False


def test_deadline_without_earlier_cancellation_is_deadline_exceeded():
    clock = FixedClock(T0)
    context, _ = _stream_context(clock, remaining=1.0)
    clock.advance(1.0)
    transport = ScriptedTransport([(200, {}, _success_body("no"))])
    events = _collect(_provider(transport), context, clock)
    assert events[-1].reason is CancellationReason.DEADLINE_EXCEEDED
    assert transport.stream_calls == []


def test_generator_close_midstream_closes_handle_and_unregisters_callback():
    clock = FixedClock(T0)
    context, source = _stream_context(clock)
    raw = RawBody([
        _sse(_chunk(content="partial")),
        _sse(_chunk(content="later", finish="stop")),
        _sse("[DONE]"),
    ])
    transport = ScriptedTransport([(200, {}, raw)])
    iterator = _provider(transport).stream(
        _request(), context=context, clock=clock)
    assert isinstance(next(iterator), StreamStarted)
    assert isinstance(next(iterator), TextDelta)
    iterator.close()
    assert raw.close_calls == 1
    assert _active_cancellation_callbacks(source) == 0
    source.cancel(CancellationReason.OPERATOR_CANCEL)
    assert raw.close_calls == 1


@pytest.mark.parametrize("control_flow", [
    KeyboardInterrupt("keyboard"),
    SystemExit("system"),
    GeneratorExit("generator"),
], ids=["KeyboardInterrupt", "SystemExit", "GeneratorExit"])
def test_control_flow_baseexception_propagates_but_still_closes_attempt(
        control_flow: BaseException):
    clock = FixedClock(T0)
    context, source = _stream_context(clock)
    raw = RawBody([control_flow])
    iterator = _provider(ScriptedTransport([(200, {}, raw)])).stream(
        _request(), context=context, clock=clock)
    assert isinstance(next(iterator), StreamStarted)

    with pytest.raises(type(control_flow)):
        next(iterator)

    assert raw.close_calls == 1
    assert _active_cancellation_callbacks(source) == 0


def test_throwing_registration_cleanup_cannot_skip_owned_handle_close():
    class ThrowingRegistration:
        def close(self) -> None:
            raise KeyboardInterrupt("registration cleanup")

    class ThrowingToken:
        requested = False
        reason = None

        def raise_if_requested(self) -> None:
            return None

        def register(self, _callback: Any) -> ThrowingRegistration:
            return ThrowingRegistration()

    clock = FixedClock(T0)
    context = StreamRequestContext(
        turn_id="turn_425c00000003",
        session_id="session_cleanup",
        generation=1,
        operation_id="op_425c00000003",
        cancellation=ThrowingToken(),
        deadline=Deadline.after(30.0, clock=clock),
        request_id="req_cleanup",
    )
    raw = _success_body("ok")
    iterator = _provider(ScriptedTransport([(200, {}, raw)])).stream(
        _request(), context=context, clock=clock)
    assert isinstance(next(iterator), StreamStarted)

    with pytest.raises(KeyboardInterrupt, match="registration cleanup"):
        list(iterator)

    assert raw.close_calls == 1


def test_conversation_explicitly_closes_retained_invalid_native_iterator():
    class RetainedIterator:
        def __init__(self, events: list[Any]) -> None:
            self.events = iter(events)
            self.close_calls = 0

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.events)

        def close(self) -> None:
            self.close_calls += 1

    class RetainingProvider:
        name = "retaining-provider"

        def __init__(self) -> None:
            self.iterator: RetainedIterator | None = None

        def stream(self, request: Any, *, context: StreamRequestContext,
                   clock: FixedClock):
            del request, clock
            base = {
                "turn_id": context.turn_id,
                "session_id": context.session_id,
                "generation": context.generation,
                "operation_id": context.operation_id,
                "request_id": context.request_id,
                "attempt_id": "att_425c00000004",
            }
            self.iterator = RetainedIterator([
                StreamStarted(
                    StreamEventEnvelope(sequence=0, **base), "m", self.name),
                # The sequence gap makes accumulator validation abandon the
                # retained iterator while it still has more provider output.
                TextDelta(StreamEventEnvelope(sequence=2, **base), "bad"),
            ])
            return self.iterator

    provider = RetainingProvider()
    runtime, _sessions, _store, _clock = _owned_runtime(provider)
    result = runtime.send(
        "hello", session=runtime.start(),
        on_stream_event=lambda _event: None)

    assert not result.ok
    assert provider.iterator is not None
    assert provider.iterator.close_calls == 1


# ---------------------------------------------------------------------------
# Strict chunks, explicit [DONE], privacy, and tool-call assembly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    RawBody([b"data: {not json " + RAW_SSE_SECRET.encode() + b"\n\n"]),
    RawBody([b"data: \xff" + RAW_SSE_SECRET.encode() + b"\n\n"]),
    RawBody([_sse([])]),
    RawBody([_sse({"choices": "bad"})]),
    RawBody([_sse({"choices": [{}]})]),
    RawBody([_sse({"choices": [{"delta": "bad"}]})]),
    RawBody([_sse(_chunk(content="ok", finish="stop"))]),
])
def test_malformed_or_missing_done_streams_fail_categorically_and_privately(
        body: RawBody):
    clock = FixedClock(T0)
    transport = ScriptedTransport([(200, {}, body)])
    context, source = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    terminal = events[-1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.error_kind == "ModelResponseError"
    assert terminal.retryable is False and terminal.will_retry is False
    blob = repr(events)
    assert RAW_SSE_SECRET not in blob
    assert AUTHORIZATION_SECRET not in blob
    assert body.close_calls == 1
    assert len(transport.stream_calls) == 1
    assert _active_cancellation_callbacks(source) == 0


@pytest.mark.parametrize("near_miss", [
    "[done]",
    " [DONE]",
    "[DONE] ",
    '"[DONE]"',
])
def test_done_terminator_is_exact_and_near_misses_fail(near_miss: str):
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(content="ok", finish="stop")),
        _sse(near_miss),
    ])
    context, _ = _stream_context(clock)
    events = _collect(
        _provider(ScriptedTransport([(200, {}, raw)])), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"


def test_oversized_sse_frame_fails_and_closes_without_disclosure():
    clock = FixedClock(T0)
    raw = RawBody([b"data: " + b"x" * MAX_RAW_SSE_FRAME_BYTES])
    transport = ScriptedTransport([(200, {}, raw)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"
    assert raw.close_calls == 1


def _tool_delta(*, index: Any = 0, call_id: Any = None,
                name: Any = None, arguments: Any = None,
                include_id: bool = True) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    result: dict[str, Any] = {
        "index": index,
        "type": "function",
        "function": function,
    }
    if include_id:
        result["id"] = call_id
    return result


def test_fragmented_tool_stream_completes_at_finish_and_finalizes_at_done():
    clock = FixedClock(T0)
    first = _tool_delta(
        call_id="call_provider", name="echo_",
        arguments='{"secret":"')
    second = _tool_delta(
        call_id="call_provider", name="test",
        arguments=TOOL_ARGUMENT_SECRET + '"}')
    raw = RawBody([
        _sse(_chunk(tools=[first])),
        _sse(_chunk(tools=[second], finish="tool_calls")),
        _sse("[DONE]"),
    ])
    transport = ScriptedTransport([(200, {}, raw)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    completed = [event for event in events
                 if isinstance(event, ToolCallCompleted)]
    assert len(deltas) == 2
    assert deltas[0].call_id == "call_provider"
    assert deltas[1].call_id is None
    assert completed == [ToolCallCompleted(
        envelope=completed[0].envelope,
        index=0,
        call=ToolCall(
            id="call_provider",
            name="echo_test",
            arguments={"secret": TOOL_ARGUMENT_SECRET},
        ),
    )]
    terminal = events[-1]
    assert isinstance(terminal, ResponseCompleted)
    assert terminal.response.tool_calls == [completed[0].call]
    assert terminal.response.finish_reason is FinishReason.TOOL_CALLS
    assert terminal.response.raw == {}


def test_provisional_tool_completion_cannot_mutate_canonical_final_call():
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_detached",
            name="echo",
            arguments='{"nested":{"value":1}}',
        )], finish="tool_calls")),
        _sse("[DONE]"),
    ])
    context, _ = _stream_context(clock)
    iterator = _provider(ScriptedTransport([(200, {}, raw)])).stream(
        _request(), context=context, clock=clock)
    assert isinstance(next(iterator), StreamStarted)
    assert isinstance(next(iterator), ToolCallDelta)
    completed = next(iterator)
    assert isinstance(completed, ToolCallCompleted)
    completed.call.arguments["nested"]["value"] = 999
    completed.call.arguments["injected"] = True

    terminal = list(iterator)[-1]

    assert isinstance(terminal, ResponseCompleted)
    assert terminal.response.tool_calls == [ToolCall(
        "call_detached", "echo", {"nested": {"value": 1}})]


def test_deep_valid_tool_json_never_crashes_pending_completion_detachment():
    clock = FixedClock(T0)
    arguments = '{"x":' * 500 + "0" + "}" * 500
    raw = RawBody([
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_deep",
            name="echo",
            arguments=arguments,
        )])),
        _sse("[DONE]"),
    ])
    context, _ = _stream_context(clock)

    events = _collect(
        _provider(ScriptedTransport([(200, {}, raw)])), context, clock)

    assert isinstance(events[-1], ResponseCompleted)
    assert sum(isinstance(event, ToolCallCompleted)
               for event in events) == 1
    assert raw.close_calls == 1


def _counter_tool_body(*, include_done: bool) -> RawBody:
    frames = [
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_native_counter",
            name="coun",
            arguments="{",
        )])),
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_native_counter",
            name="ter",
            arguments="}",
        )], finish="tool_calls")),
    ]
    if include_done:
        frames.append(_sse("[DONE]"))
    return RawBody(frames)


def test_native_fragmented_tool_round_uses_existing_executor_then_streams_again():
    runner, executor, policy, tool = _tool_boundary()
    transport = ScriptedTransport([
        (200, {}, _counter_tool_body(include_done=True)),
        (200, {}, _success_body("done")),
    ])
    provider = _provider(transport)
    runtime, sessions, _turn_store, _clock = _owned_runtime(
        provider, tools=runner)
    session = runtime.start()
    seen: list[Any] = []

    def sink(event: Any) -> None:
        seen.append(event)
        if isinstance(event, (ToolCallCompleted, ResponseCompleted)) and (
                isinstance(event, ToolCallCompleted) or
                event.response.tool_calls):
            assert runner.calls == 0
            assert executor.calls == 0
            assert policy.check_calls == 0
            assert tool.runs == 0

    result = runtime.send(
        "use counter", session=session, on_stream_event=sink)

    deltas = [event for event in seen if isinstance(event, ToolCallDelta)]
    completed = [event for event in seen
                 if isinstance(event, ToolCallCompleted)]
    assert [(event.name_fragment, event.arguments_fragment)
            for event in deltas] == [("coun", "{"), ("ter", "}")]
    assert len(completed) == 1
    assert completed[0].call == ToolCall(
        "call_native_counter", "counter", {})
    first_terminal = next(
        event for event in seen
        if isinstance(event, ResponseCompleted) and event.response.tool_calls)
    assert first_terminal.response.tool_calls == [completed[0].call]
    assert first_terminal.response.raw == {}
    assert result.ok and result.reply.content == "done"
    assert result.tools_executed == 1
    assert (runner.calls, executor.calls, policy.check_calls, tool.runs) == (
        1, 1, 1, 1)
    assert len(transport.stream_calls) == 2
    assert any(message["role"] == "tool"
               for message in transport.stream_calls[1]["payload"]["messages"])
    assert [message.role for message in session.messages] == [
        Role.USER, Role.TOOL, Role.ASSISTANT]
    assert [message.role for message in
            sessions.load(session.id).messages] == [
        Role.USER, Role.TOOL, Role.ASSISTANT]


def test_native_tool_completion_followed_by_eof_executes_zero_tools():
    runner, executor, policy, tool = _tool_boundary()
    transport = ScriptedTransport([
        (200, {}, _counter_tool_body(include_done=False)),
        (200, {}, _success_body("must-not-retry")),
    ])
    provider = _provider(transport, max_retries=2)
    runtime, sessions, _turn_store, _clock = _owned_runtime(
        provider, tools=runner)
    session = runtime.start()
    seen: list[Any] = []

    result = runtime.send(
        "use counter", session=session, on_stream_event=seen.append)

    assert any(isinstance(event, ToolCallCompleted) for event in seen)
    assert isinstance(seen[-1], StreamFailed)
    assert seen[-1].will_retry is False
    assert not result.ok and result.reply is None
    assert (runner.calls, executor.calls, policy.check_calls, tool.runs) == (
        0, 0, 0, 0)
    assert len(transport.stream_calls) == 1
    assert [message.role for message in session.messages] == [Role.USER]
    assert [message.role for message in
            sessions.load(session.id).messages] == [Role.USER]


def test_tool_completed_then_eof_is_failure_no_retry_and_no_execution():
    clock = FixedClock(T0)
    raw = RawBody([_sse(_chunk(
        tools=[_tool_delta(
            call_id="call_visible", name="echo", arguments="{}")],
        finish="tool_calls",
    ))])
    transport = ScriptedTransport([
        (200, {}, raw), (200, {}, _success_body("must-not-retry"))])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert any(isinstance(event, ToolCallCompleted) for event in events)
    terminal = events[-1]
    assert isinstance(terminal, StreamFailed)
    assert terminal.will_retry is False
    assert len(transport.stream_calls) == 1
    # Provider normalization imports no registry, policy, executor, or Tool.run
    # authority; the accepted 42.5b integration suite proves the later gate.


@pytest.mark.parametrize("tool", [
    _tool_delta(index=-1, call_id="c", name="x", arguments="{}"),
    _tool_delta(index=True, call_id="c", name="x", arguments="{}"),
    _tool_delta(index=32, call_id="c", name="x", arguments="{}"),
    _tool_delta(call_id="c", name="x" *
                (MAX_STREAM_FUNCTION_NAME_CHARS + 1), arguments="{}"),
    _tool_delta(call_id="c", name="x", arguments="x" *
                (MAX_STREAM_ARGUMENT_CHARS_PER_CALL + 1)),
])
def test_tool_fragment_indices_and_bounds_fail_closed(tool: dict[str, Any]):
    clock = FixedClock(T0)
    raw = RawBody([_sse(_chunk(tools=[tool]))])
    transport = ScriptedTransport([(200, {}, raw)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"


def test_total_tool_argument_fragment_bound_fails_while_accumulating():
    assert MAX_STREAM_ARGUMENT_CHARS_TOTAL == (
        4 * MAX_STREAM_ARGUMENT_CHARS_PER_CALL)
    frames = [
        _sse(_chunk(tools=[_tool_delta(
            index=index,
            call_id=f"call_{index}",
            name="echo",
            arguments="x" * MAX_STREAM_ARGUMENT_CHARS_PER_CALL,
        )]))
        for index in range(4)
    ]
    frames.append(_sse(_chunk(tools=[_tool_delta(
        index=4,
        call_id="call_overflow",
        name="echo",
        arguments="x",
    )])))
    clock = FixedClock(T0)
    context, _ = _stream_context(clock)
    events = _collect(_provider(ScriptedTransport([
        (200, {}, RawBody(frames)),
    ])), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"
    assert events[-1].will_retry is False


def test_retryable_failure_after_tool_completion_cannot_retry():
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_semantic",
            name="echo",
            arguments="{}",
        )], finish="tool_calls")),
        ModelConnectionError("transient after completion"),
    ])
    transport = ScriptedTransport([
        (200, {}, raw),
        (200, {}, _success_body("must-not-retry")),
    ])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert any(isinstance(event, ToolCallCompleted) for event in events)
    assert isinstance(events[-1], StreamFailed)
    assert (events[-1].retryable, events[-1].will_retry) == (True, False)
    assert len(transport.stream_calls) == 1


def test_conflicting_repeated_tool_id_fails_after_first_meaningful_delta():
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_a", name="echo", arguments="{")])),
        _sse(_chunk(tools=[_tool_delta(
            call_id="call_b", arguments="}")])),
    ])
    transport = ScriptedTransport([
        (200, {}, raw), (200, {}, _success_body("must-not-retry"))])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport, max_retries=2), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].will_retry is False
    assert len(transport.stream_calls) == 1


@pytest.mark.parametrize("arguments", [
    "{bad json " + TOOL_ARGUMENT_SECRET,
    "[]",
    "null",
    "1",
])
def test_final_tool_arguments_require_strict_json_object_without_disclosure(
        arguments: str):
    clock = FixedClock(T0)
    raw = RawBody([_sse(_chunk(
        tools=[_tool_delta(
            call_id="call_bad", name="echo", arguments=arguments)],
        finish="tool_calls",
    ))])
    transport = ScriptedTransport([(200, {}, raw)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    assert isinstance(events[-1], StreamFailed)
    assert events[-1].error_kind == "ModelResponseError"
    assert TOOL_ARGUMENT_SECRET not in repr(events)


def test_missing_tool_id_is_minted_once_only_at_completion(
        monkeypatch: pytest.MonkeyPatch):
    import agentcore.models.providers.openai_compatible as module

    calls: list[str] = []

    def fixed_id(prefix: str = "") -> str:
        calls.append(prefix)
        count = sum(1 for value in calls if value == prefix)
        return f"{prefix}_{count}"

    monkeypatch.setattr(module, "new_id", fixed_id)
    clock = FixedClock(T0)
    raw = RawBody([
        _sse(_chunk(tools=[_tool_delta(
            include_id=False, name="echo", arguments="{}")],
            finish="tool_calls")),
        _sse("[DONE]"),
    ])
    transport = ScriptedTransport([(200, {}, raw)])
    context, _ = _stream_context(clock)
    events = _collect(_provider(transport), context, clock)
    deltas = [event for event in events if isinstance(event, ToolCallDelta)]
    completed = [event for event in events
                 if isinstance(event, ToolCallCompleted)]
    assert deltas[0].call_id is None
    assert completed[0].call.id == "call_1"
    assert calls.count("call") == 1
    assert events[-1].response.tool_calls[0].id == "call_1"
