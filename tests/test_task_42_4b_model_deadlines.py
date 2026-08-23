"""Task 42.4b — model / HTTP / retry deadline enforcement.

Deterministic tests using FixedClock, fake Transport, injected sleep,
and gated fakes. No real time.sleep().
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

import pytest

from agentcore.config.schema import AgentConfig, ConversationConfig, ModelConfig, LoggingConfig, PermissionConfig, ToolConfig
from agentcore.conversation.turns import TurnBridge, TurnSuperseded, StageLease, SUPERSEDED_TURN_ERROR, TIMED_OUT_TURN_ERROR
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.models.errors import ModelConnectionError, ModelTimeoutError, ModelProviderError
from agentcore.models.messages import ModelRequest, ModelResponse, Message
from agentcore.models.transport import HttpClient, HttpResponse, RetryPolicy
from agentcore.realtime import TurnCoordinator, InMemoryTurnStore, TurnPhase, CancellationReason, Deadline, derive_session_key
from agentcore.realtime.cancellation import CancellationSource
from agentcore.realtime.turn import OperationContext, new_operation_id
from agentcore.util.clock import FixedClock
from agentcore.util.ids import new_id

WAIT = 30.0
T0 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
DEADLINE_SECONDS = 120.0
EXPIRE_BY = 121.0

SECRET = "sk-live-do-not-leak-42b"


class RecordingTransport:
    """Records timeout args and replays queued responses."""

    def __init__(self, items: list[Any]) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append({"url": url, "timeout": timeout})
        self.timeouts.append(timeout)
        if not self.items:
            raise AssertionError("RecordingTransport exhausted")
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http_ok(body: Any = None) -> HttpResponse:
    if body is None:
        body = {"ok": True}
    raw = json.dumps(body).encode()
    return HttpResponse(status=200, body=raw)


def make_context(clock: FixedClock, remaining_seconds: float | None = None,
                 expired: bool = False,
                 cancelled_reason: CancellationReason | None = None,
                 unbounded: bool = False):
    """Create an OperationContext with a deadline and token."""
    source = CancellationSource()
    if cancelled_reason is not None:
        source.cancel(cancelled_reason)
    if unbounded:
        deadline = Deadline.unbounded()
    else:
        if expired:
            # Deadline already expired: create after then advance past
            deadline = Deadline.after(1.0, clock=clock)
            clock.advance(2.0)
        else:
            if remaining_seconds is None:
                remaining_seconds = DEADLINE_SECONDS
            # We want deadline with remaining_seconds from now.
            # Use Deadline.after
            deadline = Deadline.after(remaining_seconds, clock=clock)
    ctx = OperationContext(
        turn_id=new_id("turn"),
        session_id="session_abc",
        generation=1,
        operation_id=new_id("op"),
        cancellation=source.token,
        deadline=deadline,
    )
    return ctx, source


# ---------------------------------------------------------------------------
# 1. EXPIRED BEFORE PROVIDER CALL
# ---------------------------------------------------------------------------
def test_expired_before_provider_call_no_transport():
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=3, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)
    ctx, _ = make_context(clock, expired=True)

    with pytest.raises(ModelTimeoutError) as exc:
        client.post_json("https://x.test", {}, {}, 30.0, context=ctx)

    assert transport.calls == []
    assert "deadline" in str(exc.value).lower() or "expired" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# 2. REMAINING < MODEL TIMEOUT
# ---------------------------------------------------------------------------
def test_remaining_smaller_than_configured_timeout():
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)
    ctx, _ = make_context(clock, remaining_seconds=4.0)

    result = client.post_json("https://x.test", {}, {}, 30.0, context=ctx)

    assert result == {"ok": True}
    assert len(transport.timeouts) == 1
    # Effective timeout must be <= remaining, never 30
    assert transport.timeouts[0] <= 4.0 + 0.001
    assert transport.timeouts[0] >= 3.9  # deterministic, no jitter
    assert transport.timeouts[0] != 30.0


# ---------------------------------------------------------------------------
# 3. CONFIGURED TIMEOUT SMALLER
# ---------------------------------------------------------------------------
def test_configured_timeout_smaller_than_remaining():
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)
    ctx, _ = make_context(clock, remaining_seconds=50.0)

    result = client.post_json("https://x.test", {}, {}, 5.0, context=ctx)

    assert result == {"ok": True}
    assert transport.timeouts[0] == 5.0


# ---------------------------------------------------------------------------
# 4. FAILED FIRST ATTEMPT CONSUMES BUDGET
# ---------------------------------------------------------------------------
def test_failed_first_attempt_consumes_budget():
    clock = FixedClock(T0)
    # First fails, second succeeds
    transport = RecordingTransport([
        ModelConnectionError("flaky"),
        http_ok(),
    ])
    slept: list[float] = []

    def sleep_and_advance(delay: float):
        slept.append(delay)
        clock.advance(delay)

    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=2, initial_backoff=1.0, jitter=False),
                        sleep=sleep_and_advance,
                        clock=clock)
    ctx, _ = make_context(clock, remaining_seconds=10.0)

    result = client.post_json("https://x.test", {}, {}, 30.0, context=ctx)

    assert result == {"ok": True}
    assert len(transport.timeouts) == 2
    # First timeout should be min(30,10)=10
    assert transport.timeouts[0] == 10.0
    # After 1s sleep, remaining should be ~9, so second timeout = min(30,9)=9
    assert transport.timeouts[1] == pytest.approx(9.0, abs=0.01)
    assert slept == [1.0]


# ---------------------------------------------------------------------------
# 5. DEADLINE EXPIRES BEFORE RETRY
# ---------------------------------------------------------------------------
def test_deadline_expires_before_retry_no_second_call():
    clock = FixedClock(T0)
    transport = RecordingTransport([
        ModelConnectionError("flaky"),
        http_ok(),
    ])
    # Sleep advances clock beyond deadline
    def sleep_expire(delay: float):
        clock.advance(EXPIRE_BY)

    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=2, initial_backoff=0.5, jitter=False),
                        sleep=sleep_expire,
                        clock=clock)
    ctx, _ = make_context(clock, remaining_seconds=2.0)

    with pytest.raises(ModelTimeoutError):
        client.post_json("https://x.test", {}, {}, 30.0, context=ctx)

    # Only first attempt should have happened
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# 6. RETRY BACKOFF EXCEEDS REMAINING BUDGET
# ---------------------------------------------------------------------------
def test_retry_backoff_exceeds_remaining_aborts():
    clock = FixedClock(T0)
    transport = RecordingTransport([
        ModelConnectionError("flaky"),
        http_ok(),
    ])
    slept: list[float] = []

    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=2, initial_backoff=5.0, jitter=False),
                        sleep=slept.append,
                        clock=clock)
    ctx, _ = make_context(clock, remaining_seconds=2.0)

    with pytest.raises(ModelTimeoutError) as exc:
        client.post_json("https://x.test", {}, {}, 30.0, context=ctx)

    assert len(transport.calls) == 1
    assert slept == []  # should not sleep full backoff
    assert "backoff" in str(exc.value).lower() or "deadline" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# 7. DEADLINE EXPIRES DURING RETRY WAIT
# ---------------------------------------------------------------------------
def test_deadline_expires_during_retry_wait():
    clock = FixedClock(T0)
    transport = RecordingTransport([
        ModelConnectionError("flaky"),
        ModelConnectionError("flaky again"),
        http_ok(),
    ])

    def sleep_advance_half(delay: float):
        # Advance clock by delay, but also ensure deadline expires after first retry wait
        clock.advance(delay)

    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=3, initial_backoff=0.5, jitter=False),
                        sleep=sleep_advance_half,
                        clock=clock)
    # Remaining 1.0, backoff 0.5 -> first retry allowed, after sleep remaining 0.5
    # second backoff 1.0 would exceed remaining 0.5 -> should abort
    ctx, _ = make_context(clock, remaining_seconds=1.0)

    with pytest.raises(ModelTimeoutError):
        client.post_json("https://x.test", {}, {}, 30.0, context=ctx)

    # First attempt + one retry, second retry aborted because backoff >= remaining
    assert len(transport.calls) == 2


# ---------------------------------------------------------------------------
# 8. PROVIDER PHYSICALLY RETURNS AFTER DEADLINE (conversation integration)
# ---------------------------------------------------------------------------
class ScriptedModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list = []
        self.calls = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self.gates: dict[int, threading.Event] = {}
        self.entered: dict[int, threading.Event] = {}

    def gate(self, index: int) -> threading.Event:
        self.gates[index] = threading.Event()
        self.entered[index] = threading.Event()
        return self.gates[index]

    def generate(self, request, *, context=None):
        with self._condition:
            self.calls += 1
            idx = self.calls
            self.requests.append(request)
        if idx in self.entered:
            self.entered[idx].set()
        if idx in self.gates:
            assert self.gates[idx].wait(WAIT), "gate never released"
        if not self.steps:
            raise AssertionError("ScriptedModel exhausted")
        return self.steps.pop(0)

    def wait_entered(self, index: int):
        assert self.entered[index].wait(WAIT), f"call {index} never started"


def _reply(text: str):
    return ModelResponse(text=text)


def _config(tmp_path, *, tools_enabled=False):
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path / "workspace"),
        model=ModelConfig(),
        tools=ToolConfig(),
        permissions=PermissionConfig(),
        logging=LoggingConfig(level="CRITICAL"),
        conversation=ConversationConfig(directory=str(tmp_path / "conversations"),
                                        tools_enabled=tools_enabled),
    ).validate()


def _agent(tmp_path, model, *, clock=None, store=None):
    store = store or InMemoryTurnStore()
    coordinator = TurnCoordinator(store, clock=clock or FixedClock(T0))
    # Wrap coordinator to observe admissions if needed
    agent = Agent(_config(tmp_path), AgentDependencies(model=model, turn_coordinator=coordinator))
    # Inject bridge with controllable clock
    if clock is not None:
        agent._turns = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    return agent, agent.conversation(), store, coordinator


def test_provider_returns_after_deadline_fenced(tmp_path):
    clock = FixedClock(T0)
    model = ScriptedModel(_reply("late reply"))
    release = model.gate(1)
    agent, runtime, store, _ = _agent(tmp_path, model, clock=clock)
    session = runtime.start()
    results: list = []

    t = threading.Thread(target=lambda: results.append(runtime.send("hello", session=session)), daemon=True)
    t.start()
    model.wait_entered(1)
    clock.advance(EXPIRE_BY)
    release.set()
    t.join(WAIT)

    assert not results[0].ok
    assert results[0].turn_phase == "timed_out"
    assert store.load(results[0].turn_id).phase is TurnPhase.TIMED_OUT
    # No assistant output persisted
    assert [m.content for m in session.messages] == ["hello"]


# ---------------------------------------------------------------------------
# 9. SUPERSESSION BEFORE LATER DEADLINE
# ---------------------------------------------------------------------------
class RecordingCoordinator:
    def __init__(self, inner):
        self.inner = inner
        self._condition = threading.Condition()
        self.admissions: list = []

    @property
    def store(self):
        return self.inner.store

    def admit(self, session_id, **kwargs):
        admission = self.inner.admit(session_id, **kwargs)
        with self._condition:
            self.admissions.append(admission)
            self._condition.notify_all()
        return admission

    def wait_for(self, count: int):
        with self._condition:
            ready = self._condition.wait_for(lambda: len(self.admissions) >= count, WAIT)
        assert ready, f"only {len(self.admissions)} admissions, wanted {count}"
        return self.admissions[count - 1]


def test_supersession_wins_over_later_deadline(tmp_path):
    clock = FixedClock(T0)
    model = ScriptedModel(_reply("first"), _reply("second"))
    gate_first = model.gate(1)
    store = InMemoryTurnStore()
    inner_coord = TurnCoordinator(store, clock=clock)
    coordinator = RecordingCoordinator(inner_coord)
    agent = Agent(_config(tmp_path), AgentDependencies(model=model, turn_coordinator=coordinator))
    agent._turns = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    runtime = agent.conversation()
    session = runtime.start()
    results: dict[str, Any] = {}

    def send(name, text):
        results[name] = runtime.send(text, session=session)

    first = threading.Thread(target=send, args=("first", "one"), daemon=True)
    first.start()
    model.wait_entered(1)
    second = threading.Thread(target=send, args=("second", "two"), daemon=True)
    second.start()
    admission2 = coordinator.wait_for(2)
    # Supersession should have happened synchronously inside admit
    first_id = coordinator.admissions[0].admitted.turn_id
    rec_first_before = store.load(first_id)
    assert rec_first_before.phase is TurnPhase.CANCELLING
    assert rec_first_before.cancellation_reason is CancellationReason.SUPERSEDED

    # Advance beyond deadline after supersession
    clock.advance(EXPIRE_BY)
    gate_first.set()
    first.join(WAIT)
    second.join(WAIT)

    # First turn should be cancelled (superseded), not timed_out
    rec1 = store.load(first_id)
    assert rec1.phase is TurnPhase.CANCELLED
    assert rec1.cancellation_reason is CancellationReason.SUPERSEDED
    assert not results["first"].ok
    assert results["first"].turn_phase == "cancelled"
    assert results["first"].error == SUPERSEDED_TURN_ERROR


# ---------------------------------------------------------------------------
# 10. SHUTDOWN BEFORE LATER DEADLINE
# ---------------------------------------------------------------------------
def test_shutdown_wins_over_later_deadline(tmp_path):
    clock = FixedClock(T0)
    model = ScriptedModel(_reply("late"))
    gate = model.gate(1)
    agent, runtime, store, _ = _agent(tmp_path, model, clock=clock)
    session = runtime.start()
    results: list = []

    t = threading.Thread(target=lambda: results.append(runtime.send("hello", session=session)), daemon=True)
    t.start()
    model.wait_entered(1)
    # Shutdown before deadline expiry
    agent.shutdown()
    clock.advance(EXPIRE_BY)
    gate.set()
    t.join(WAIT)

    rec = store.load(results[0].turn_id)
    assert rec.phase is TurnPhase.CANCELLED
    assert rec.cancellation_reason is CancellationReason.AGENT_SHUTDOWN
    assert results[0].turn_phase == "cancelled"


# ---------------------------------------------------------------------------
# 11. COMPLETION CAS WINS FIRST (existing test retained)
# ---------------------------------------------------------------------------
def test_completion_cas_wins_over_later_expiry(tmp_path):
    from agentcore.conversation.runtime import ConversationRuntime

    clock = FixedClock(T0)

    class AdvancingStore:
        def __init__(self, delegate, clock, seconds):
            self._delegate = delegate
            self._clock = clock
            self._seconds = seconds
            self.committed = False

        @property
        def admission_coordination(self):
            return self._delegate.admission_coordination

        def create(self, record):
            return self._delegate.create(record)

        def load(self, turn_id):
            return self._delegate.load(turn_id)

        def compare_and_swap(self, turn_id, expected_version, next_record):
            result = self._delegate.compare_and_swap(turn_id, expected_version, next_record)
            if not self.committed and next_record.phase is TurnPhase.FINALIZING and next_record.requested_terminal is TurnPhase.COMPLETED:
                self.committed = True
                self._clock.advance(self._seconds)
            return result

        def list_turns(self):
            return self._delegate.list_turns()

        def close(self):
            return self._delegate.close()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    model = ScriptedModel(_reply("committed"))
    store = AdvancingStore(InMemoryTurnStore(), clock, EXPIRE_BY)
    coordinator = TurnCoordinator(store, clock=clock)
    bridge = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    runtime = ConversationRuntime(model=model, turns=bridge)

    result = runtime.send("hello")

    assert store.committed
    assert result.ok
    assert result.turn_phase == "completed"
    assert store.load(result.turn_id).phase is TurnPhase.COMPLETED


# ---------------------------------------------------------------------------
# 12. LEGACY CALLER WITHOUT CONTEXT
# ---------------------------------------------------------------------------
def test_legacy_caller_without_context_retains_old_behavior():
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)

    # No context supplied
    result = client.post_json("https://x.test", {}, {}, 5.0)

    assert result == {"ok": True}
    assert transport.timeouts[0] == 5.0


def test_provider_legacy_generate_without_context():
    from agentcore.models.providers.echo import EchoProvider
    provider = EchoProvider()
    req = ModelRequest(messages=[Message.user("hi")])
    resp = provider.generate(req)  # no context
    assert resp.text.startswith("echo:")

    # With context
    clock = FixedClock(T0)
    ctx, _ = make_context(clock, remaining_seconds=10.0)
    resp2 = provider.generate(req, context=ctx)
    assert resp2.text.startswith("echo:")


# ---------------------------------------------------------------------------
# 13. SECRET SANITIZATION (P3 fixed)
# ---------------------------------------------------------------------------
def test_secret_sanitization_with_context():
    clock = FixedClock(T0)
    transport = RecordingTransport([
        HttpResponse(status=500, body=json.dumps({"error": {"message": f"got {SECRET}"}}).encode(), headers={}),
    ])
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)
    # Use distinctive identifiers in context so we can assert they don't leak
    turn_id = "turn_deadbeef1234"
    session_id = "session_abc123"
    op_id = "op_abcdef123456"
    source = CancellationSource()
    deadline = Deadline.after(10.0, clock=clock)
    ctx = OperationContext(
        turn_id=turn_id,
        session_id=session_id,
        generation=1,
        operation_id=op_id,
        cancellation=source.token,
        deadline=deadline,
    )

    with pytest.raises(ModelProviderError) as exc:
        client.post_json("https://x.test", {}, {"Authorization": f"Bearer {SECRET}"}, 5.0,
                         secrets=(SECRET,), context=ctx)

    blob = json.dumps(exc.value.to_dict())
    # Independent meaningful checks, no OR True escape hatch
    assert SECRET not in blob
    # Raw Authorization header value must not appear (sanitized)
    assert f"Bearer {SECRET}" not in blob
    # OperationContext must not be serialized
    assert "OperationContext" not in blob
    # Turn/session/operation identifiers from context must not leak
    assert turn_id not in blob
    assert session_id not in blob
    assert op_id not in blob
    # Clock representation must not leak
    assert "FixedClock" not in blob
    assert "SystemClock" not in blob


# ---------------------------------------------------------------------------
# P1-1 — UNSAFE TypeError FALLBACK REGRESSION
# ---------------------------------------------------------------------------
def test_context_aware_provider_internal_typeerror_calls_once():
    """A provider that accepts context but raises internal TypeError must be invoked exactly once."""
    from agentcore.conversation.runtime import ConversationRuntime

    calls: list[int] = []

    class ContextAwareProvider:
        def generate(self, request, *, context=None):
            calls.append(1)
            assert context is not None
            raise TypeError("internal provider bug")

    clock = FixedClock(T0)
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, clock=clock)
    bridge = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    model = ContextAwareProvider()
    runtime = ConversationRuntime(model=model, turns=bridge)

    result = runtime.send("hello")

    # Exactly one invocation, no fallback without context
    assert len(calls) == 1
    # Runtime normalizes failure, does not strip deadline context
    assert not result.ok
    assert result.reply is None
    # Should be FAILED (internal failure), not silently retried without context
    assert store.load(result.turn_id).phase is TurnPhase.FAILED


def test_legacy_provider_without_context_executes_once():
    from agentcore.conversation.runtime import ConversationRuntime

    calls: list[int] = []

    class LegacyProvider:
        def generate(self, request):
            calls.append(1)
            return ModelResponse(text="legacy ok")

    clock = FixedClock(T0)
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, clock=clock)
    bridge = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    model = LegacyProvider()
    runtime = ConversationRuntime(model=model, turns=bridge)

    result = runtime.send("hello")

    assert len(calls) == 1
    assert result.ok
    assert result.reply.content == "legacy ok"


# ---------------------------------------------------------------------------
# P1-2 — CLOCK DOMAIN MUST MATCH DEADLINE CREATION
# ---------------------------------------------------------------------------
def test_same_clock_propagation_through_runtime_provider_transport(tmp_path):
    """Deadline created with FixedClock must be evaluated with SAME clock in HttpClient.

    Provider/client NOT preconfigured with FixedClock; runtime must propagate
    fence clock. If it didn't, SystemClock would see the FixedClock deadline
    as instantly expired.
    """
    from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
    from agentcore.config.schema import ModelConfig

    clock = FixedClock(T0)
    transport = RecordingTransport([HttpResponse(status=200, body=json.dumps({
        "id": "cmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode())])

    # HttpClient uses default SystemClock and default sleep — NOT FixedClock.
    # The runtime must propagate fence clock via post_json(clock=).
    provider = OpenAICompatibleProvider(
        ModelConfig(provider="openai_compatible", name="test-model",
                    base_url="https://api.example.com/v1",
                    timeout_seconds=30.0, max_retries=1,
                    api_key_env="TEST_KEY"),
        transport=transport,
        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
    )

    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, clock=clock)
    bridge = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    # Agent composition with our provider and coordinator
    agent = Agent(_config(tmp_path), AgentDependencies(model=provider, turn_coordinator=coordinator))
    agent._turns = bridge
    runtime = agent.conversation()

    # Need API key for OpenAI provider
    import os
    os.environ["TEST_KEY"] = "sk-test"

    result = runtime.send("hello")

    assert result.ok
    assert len(transport.calls) == 1
    # Effective timeout must be min(configured 30, remaining 120) = 30, not expired
    assert transport.timeouts[0] == 30.0

    # Now test retry timeout shrinks when FixedClock advances
    clock2 = FixedClock(T0)
    transport2 = RecordingTransport([
        ModelConnectionError("flaky"),
        HttpResponse(status=200, body=json.dumps({
            "id": "cmpl-2",
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok after retry"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()),
    ])
    slept: list[float] = []

    def sleep_and_advance(delay: float):
        slept.append(delay)
        clock2.advance(delay)

    client2 = HttpClient(transport=transport2,
                         retry=RetryPolicy(max_attempts=2, initial_backoff=1.0, jitter=False),
                         sleep=sleep_and_advance,
                         clock=None)  # default SystemClock, not FixedClock

    provider2 = OpenAICompatibleProvider(
        ModelConfig(provider="openai_compatible", name="test-model",
                    base_url="https://api.example.com/v1",
                    timeout_seconds=30.0, max_retries=2,
                    api_key_env="TEST_KEY"),
        client=client2,
    )

    store2 = InMemoryTurnStore()
    coordinator2 = TurnCoordinator(store2, clock=clock2)
    bridge2 = TurnBridge(coordinator2, clock=clock2, turn_deadline_seconds=10.0)
    agent2 = Agent(_config(tmp_path), AgentDependencies(model=provider2, turn_coordinator=coordinator2))
    agent2._turns = bridge2
    runtime2 = agent2.conversation()

    result2 = runtime2.send("hello retry")

    assert result2.ok
    assert len(transport2.timeouts) == 2
    assert transport2.timeouts[0] == 10.0  # min(30,10)
    # After 1s sleep, remaining 9, so second timeout = 9
    assert transport2.timeouts[1] == pytest.approx(9.0, abs=0.01)


# ---------------------------------------------------------------------------
# P2-2 — DO NOT SWALLOW NON-TurnSuperseded FENCE FAILURES
# ---------------------------------------------------------------------------
def test_fence_check_failure_not_swallowed_after_provider_failure(tmp_path):
    """If fence.check() raises non-TurnSuperseded after provider failure, it must not be ignored."""
    from agentcore.conversation.runtime import ConversationRuntime

    class FailingFence:
        def __init__(self):
            self._lock = threading.RLock()

        def check(self):
            raise RuntimeError("store ownership lost")

        def operation_context(self):
            return None

        @property
        def clock(self):
            return None

        def enter_tool_round(self):
            return None

        def leave_tool_round(self):
            return None

        def commit(self):
            return None

        def stage_tool_message(self, *args, **kwargs):
            return None

        def flush_staged(self, *args, **kwargs):
            return None

        def session_lock(self):
            return threading.RLock()

    class ExplodingModel:
        def generate(self, request, *, context=None, clock=None):
            raise ModelConnectionError("provider down")

    runtime = ConversationRuntime(model=ExplodingModel())
    session = runtime.start()
    fence = FailingFence()

    with pytest.raises(RuntimeError, match="store ownership lost"):
        runtime._run_turn(session, Message.user("hello"), fence=fence)


# ---------------------------------------------------------------------------
# FINAL REPAIR — **kwargs legacy compatibility (P1) + malformed context
# ---------------------------------------------------------------------------
def test_legacy_complete_provider_with_kwargs_does_not_receive_trusted_context(tmp_path):
    """Public ModelProvider Protocol is complete(..., **options); trusted context must NOT leak into **options."""
    from agentcore.conversation.runtime import ConversationRuntime

    class LegacyCompleteProvider:
        def __init__(self):
            self.calls = 0
            self.options: dict[str, Any] | None = None

        def complete(self, prompt, *, system=None, **options):
            self.calls += 1
            self.options = dict(options)
            return "ok"

    clock = FixedClock(T0)
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, clock=clock)
    bridge = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    model = LegacyCompleteProvider()
    runtime = ConversationRuntime(model=model, turns=bridge)

    result = runtime.send("hello")

    assert model.calls == 1
    assert result.ok
    assert result.reply.content == "ok"
    assert model.options is not None
    assert "context" not in model.options
    assert "clock" not in model.options
    # Ensure no OperationContext or Clock leaks into generic options
    for v in model.options.values():
        assert not isinstance(v, OperationContext)
        assert "turn_" not in str(v)
    # Store should be COMPLETED
    assert store.load(result.turn_id).phase is TurnPhase.COMPLETED


def test_explicit_context_aware_provider_receives_both_context_and_clock(tmp_path):
    """Explicit context+clock params must be forwarded (opt-in)."""
    from agentcore.conversation.runtime import ConversationRuntime

    seen: dict[str, Any] = {}

    class ExplicitProvider:
        def generate(self, request, *, context=None, clock=None):
            seen["context"] = context
            seen["clock"] = clock
            return ModelResponse(text="explicit ok")

    clock = FixedClock(T0)
    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store, clock=clock)
    bridge = TurnBridge(coordinator, clock=clock, turn_deadline_seconds=DEADLINE_SECONDS)
    model = ExplicitProvider()
    runtime = ConversationRuntime(model=model, turns=bridge)

    result = runtime.send("hello explicit")

    assert result.ok
    assert seen["context"] is not None
    assert isinstance(seen["context"], OperationContext)
    assert seen["clock"] is clock
    # Deadline in context must be same object created at admission
    assert seen["context"].deadline is not None


def test_malformed_context_missing_deadline_fails_closed():
    """If trusted context is non-None but deadline missing, treat as expired (fail-closed)."""
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])

    class MissingDeadlineContext:
        # Has cancellation but no deadline attribute
        def __init__(self):
            source = CancellationSource()
            self.cancellation = source.token

    ctx = MissingDeadlineContext()
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)

    with pytest.raises(ModelTimeoutError):
        client.post_json("https://x.test", {}, {}, 5.0, context=ctx)

    assert transport.calls == []


def test_malformed_context_missing_cancellation_fails_closed():
    """If trusted context is non-None but cancellation missing, treat as cancelled (fail-closed)."""
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])

    class MissingCancellationContext:
        def __init__(self):
            self.deadline = Deadline.after(10.0, clock=clock)
            # No cancellation attribute

    ctx = MissingCancellationContext()
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)

    with pytest.raises(ModelTimeoutError):
        client.post_json("https://x.test", {}, {}, 5.0, context=ctx)

    assert transport.calls == []


def test_malformed_deadline_methods_fail_closed():
    """Deadline methods raising should be treated as expired, not unbounded."""
    clock = FixedClock(T0)
    transport = RecordingTransport([http_ok()])

    class BrokenDeadline:
        def expired(self, clock):
            raise RuntimeError("broken expired")

        def remaining(self, clock):
            raise RuntimeError("broken remaining")

    class BrokenContext:
        def __init__(self):
            source = CancellationSource()
            self.cancellation = source.token
            self.deadline = BrokenDeadline()

    ctx = BrokenContext()
    client = HttpClient(transport=transport,
                        retry=RetryPolicy(max_attempts=1, initial_backoff=0.01, jitter=False),
                        sleep=lambda _: None,
                        clock=clock)

    with pytest.raises(ModelTimeoutError):
        client.post_json("https://x.test", {}, {}, 5.0, context=ctx)

    assert transport.calls == []


def test_positional_only_param_not_treated_as_keyword():
    """POSITIONAL_ONLY should not be considered keyword-compatible."""
    from agentcore.conversation.runtime import _accepts_kwarg
    from agentcore.models.base import _accepts_kwarg as base_accepts

    def pos_only(a, /, *, b=None):
        pass

    def kw_only(*, context=None):
        pass

    def pos_or_kw(context=None):
        pass

    def with_kwargs(**kwargs):
        pass

    # POSITIONAL_ONLY 'a' should not be accepted as keyword 'context'
    assert not _accepts_kwarg(pos_only, "context")
    assert not base_accepts(pos_only, "context")
    # But kw_only and pos_or_kw should accept
    assert _accepts_kwarg(kw_only, "context")
    assert base_accepts(kw_only, "context")
    assert _accepts_kwarg(pos_or_kw, "context")
    assert base_accepts(pos_or_kw, "context")
    # **kwargs should NOT be accepted for trusted keywords (final repair)
    assert not _accepts_kwarg(with_kwargs, "context")
    assert not base_accepts(with_kwargs, "context")
    assert not _accepts_kwarg(with_kwargs, "clock")
    assert not base_accepts(with_kwargs, "clock")
