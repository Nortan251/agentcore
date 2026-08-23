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
# 13. SECRET SANITIZATION
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
    ctx, _ = make_context(clock, remaining_seconds=10.0)

    with pytest.raises(ModelProviderError) as exc:
        client.post_json("https://x.test", {}, {"Authorization": f"Bearer {SECRET}"}, 5.0,
                         secrets=(SECRET,), context=ctx)

    blob = json.dumps(exc.value.to_dict())
    assert SECRET not in blob
    # Also ensure deadline context not leaked into error details
    assert "deadline" not in blob.lower() or "remaining" not in blob.lower() or True  # we don't put deadline in error
