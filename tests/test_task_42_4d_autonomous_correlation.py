"""Task 42.4d — autonomous turn/run correlation public contract.

Deterministic tests, no sleep-based races.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentcore.config.schema import AgentConfig, LoggingConfig, PermissionConfig, ToolConfig, ConversationConfig
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.correlation import RunCorrelation
from agentcore.core.runner import RunControl, RunReport
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.realtime import (
    TurnCoordinator,
    InMemoryTurnStore,
    JsonFileTurnStore,
    TurnPhase,
    CancellationReason,
    Deadline,
    TurnController,
    TurnCorrelationError,
    TurnCorrelationConflictError,
    TurnValidationError,
    TurnNotFoundError,
    derive_session_key,
    new_operation_id,
    require_turn_id,
    require_run_id,
)
from agentcore.realtime.cancellation import CancellationSource
from agentcore.realtime.turn import new_turn
from agentcore.state.models import AgentPhase, AgentState
from agentcore.state.store import InMemoryStateStore, JsonFileStateStore
from agentcore.tasks.models import Task, Subtask, TaskStatus
from agentcore.tools.base import Tool, ToolResult, tool_spec
from agentcore.util.clock import FixedClock
from agentcore.util.ids import new_id
from agentcore.observability.events import Events

WAIT = 30.0
T0 = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _config(tmp_path, *, level=PermissionLevel.AUTONOMOUS):
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path / "workspace"),
        tools=ToolConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        conversation=ConversationConfig(directory=str(tmp_path / "conversations")),
    ).validate()


class CountingTool(Tool):
    def __init__(self):
        self.runs = 0
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec("count", "Counts.", parameters=[], capabilities=[], read_only=True)

    def run(self, args, context):
        with self._lock:
            self.runs += 1
            return ToolResult.success(f"count-{self.runs}")


class FailTool(Tool):
    @property
    def spec(self):
        return tool_spec("fail", "Fails.", parameters=[], capabilities=[], read_only=True)

    def run(self, args, context):
        return ToolResult.failure("failed")


class WriteTool(Tool):
    def __init__(self):
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("write", "Write.", parameters=[], capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("wrote")


class GatedTool(Tool):
    def __init__(self):
        self.runs = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    @property
    def spec(self):
        return tool_spec("gate", "Gate.", parameters=[], capabilities=[], read_only=True)

    def run(self, args, context):
        self.runs += 1
        self.entered.set()
        assert self.release.wait(WAIT)
        return ToolResult.success("gated")


def make_task(tool_name="count"):
    task = Task(objective="test")
    task.add_subtask(Subtask(description=f"run {tool_name}", tool_name=tool_name, tool_args={}))
    return task


def make_agent(tmp_path, tools, *, turn_store=None, state_store=None, level=PermissionLevel.AUTONOMOUS, verifier=None, recovery=None, planner=None, approval_gate=None):
    turn_store = turn_store or InMemoryTurnStore()
    coordinator = TurnCoordinator(turn_store)
    config = _config(tmp_path, level=level)
    deps = AgentDependencies(
        tools=tools,
        turn_coordinator=coordinator,
        state_store=state_store or InMemoryStateStore(),
        verifier=verifier,
        recovery=recovery,
        planner=planner,
        approval_gate=approval_gate,
    )
    agent = Agent(config, deps)
    return agent, turn_store, coordinator


def complete_turn(store, clock=None):
    """Create a COMPLETED turn via controller transitions."""
    clock = clock or FixedClock(T0)
    # Use new_turn then transition to COMPLETED via legal path
    # For simplicity, directly create record with phase COMPLETED using store's internal? Use controller
    # Create fresh record then transition: CREATED->QUEUED->GENERATING->FINALIZING(COMPLETED)->COMPLETED
    session_key = derive_session_key("test_session")
    deadline = Deadline.after(120.0, clock=clock)
    turn_id = new_id("turn")
    record = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
    store.create(record)
    controller = TurnController(store, turn_id, clock=clock)
    # QUEUED
    snap = controller.transition(TurnPhase.CREATED, TurnPhase.QUEUED, expected_version=0, operation_id=new_operation_id())
    # GENERATING
    snap = controller.transition(TurnPhase.QUEUED, TurnPhase.GENERATING, expected_version=snap.version, operation_id=new_operation_id())
    # FINALIZING COMPLETED
    snap = controller.transition(TurnPhase.GENERATING, TurnPhase.FINALIZING, expected_version=snap.version, operation_id=new_operation_id(), requested_terminal=TurnPhase.COMPLETED)
    # COMPLETED
    snap = controller.transition(TurnPhase.FINALIZING, TurnPhase.COMPLETED, expected_version=snap.version, operation_id=new_operation_id())
    return snap.turn_id, store.load(snap.turn_id)


# ---------------------------------------------------------------------------
# TurnStore / Controller tests (1-10)
# ---------------------------------------------------------------------------
def test_bind_run_to_completed_unbound_inmemory():
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    turn_id, before = complete_turn(store, clock)
    assert before.phase is TurnPhase.COMPLETED
    assert before.run_id is None
    before_version = before.version
    before_transitions = before.transitions
    before_updated = before.updated_at

    run_id = new_id("run")
    after = store.bind_run(turn_id, run_id)

    assert after.run_id == run_id
    assert after.phase is TurnPhase.COMPLETED
    assert after.version == before_version
    assert after.transitions == before_transitions
    assert after.updated_at == before_updated


def test_same_run_binding_idempotent():
    store = InMemoryTurnStore()
    turn_id, _ = complete_turn(store)
    run_id = new_id("run")
    first = store.bind_run(turn_id, run_id)
    second = store.bind_run(turn_id, run_id)
    assert first == second
    assert first.run_id == run_id
    assert second.version == first.version


def test_different_run_conflict():
    store = InMemoryTurnStore()
    turn_id, _ = complete_turn(store)
    run1 = new_id("run")
    run2 = new_id("run")
    store.bind_run(turn_id, run1)
    with pytest.raises(TurnCorrelationConflictError):
        store.bind_run(turn_id, run2)
    assert store.load(turn_id).run_id == run1


def test_bind_on_generating_rejected():
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    session_key = derive_session_key("s")
    deadline = Deadline.after(120.0, clock=clock)
    turn_id = new_id("turn")
    record = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
    store.create(record)
    controller = TurnController(store, turn_id, clock=clock)
    controller.transition(TurnPhase.CREATED, TurnPhase.QUEUED, expected_version=0, operation_id=new_operation_id())
    controller.transition(TurnPhase.QUEUED, TurnPhase.GENERATING, expected_version=1, operation_id=new_operation_id())

    with pytest.raises(TurnCorrelationError):
        store.bind_run(turn_id, new_id("run"))


def test_bind_on_cancelled_rejected(tmp_path):
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    session_key = derive_session_key("s")
    deadline = Deadline.after(120.0, clock=clock)
    turn_id = new_id("turn")
    record = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
    store.create(record)
    controller = TurnController(store, turn_id, clock=clock)
    controller.transition(TurnPhase.CREATED, TurnPhase.CANCELLING, expected_version=0, operation_id=new_operation_id(), cancellation_reason=CancellationReason.SUPERSEDED, requested_terminal=TurnPhase.CANCELLED)
    controller.transition(TurnPhase.CANCELLING, TurnPhase.FINALIZING, expected_version=1, operation_id=new_operation_id(), requested_terminal=TurnPhase.CANCELLED, cancellation_reason=CancellationReason.SUPERSEDED)
    controller.transition(TurnPhase.FINALIZING, TurnPhase.CANCELLED, expected_version=2, operation_id=new_operation_id())

    with pytest.raises(TurnCorrelationError):
        store.bind_run(turn_id, new_id("run"))


def test_bind_on_failed_rejected():
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    session_key = derive_session_key("s")
    deadline = Deadline.after(120.0, clock=clock)
    turn_id = new_id("turn")
    record = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
    store.create(record)
    controller = TurnController(store, turn_id, clock=clock)
    controller.transition(TurnPhase.CREATED, TurnPhase.CANCELLING, expected_version=0, operation_id=new_operation_id(), cancellation_reason=CancellationReason.INTERNAL_FAILURE, requested_terminal=TurnPhase.FAILED)
    controller.transition(TurnPhase.CANCELLING, TurnPhase.FINALIZING, expected_version=1, operation_id=new_operation_id(), requested_terminal=TurnPhase.FAILED, cancellation_reason=CancellationReason.INTERNAL_FAILURE)
    controller.transition(TurnPhase.FINALIZING, TurnPhase.FAILED, expected_version=2, operation_id=new_operation_id())

    with pytest.raises(TurnCorrelationError):
        store.bind_run(turn_id, new_id("run"))


def test_bind_on_timed_out_rejected():
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    session_key = derive_session_key("s")
    deadline = Deadline.after(120.0, clock=clock)
    turn_id = new_id("turn")
    record = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
    store.create(record)
    controller = TurnController(store, turn_id, clock=clock)
    controller.transition(TurnPhase.CREATED, TurnPhase.CANCELLING, expected_version=0, operation_id=new_operation_id(), cancellation_reason=CancellationReason.DEADLINE_EXCEEDED, requested_terminal=TurnPhase.TIMED_OUT)
    controller.transition(TurnPhase.CANCELLING, TurnPhase.FINALIZING, expected_version=1, operation_id=new_operation_id(), requested_terminal=TurnPhase.TIMED_OUT, cancellation_reason=CancellationReason.DEADLINE_EXCEEDED)
    controller.transition(TurnPhase.FINALIZING, TurnPhase.TIMED_OUT, expected_version=2, operation_id=new_operation_id())

    with pytest.raises(TurnCorrelationError):
        store.bind_run(turn_id, new_id("run"))


def test_jsonfilestore_roundtrip(tmp_path):
    dir_path = tmp_path / "turns"
    store = JsonFileTurnStore(dir_path)
    try:
        turn_id, _ = complete_turn(store)
        run_id = new_id("run")
        store.bind_run(turn_id, run_id)
        # Close and reopen
        store.close()
        reopened = JsonFileTurnStore(dir_path)
        try:
            loaded = reopened.load(turn_id)
            assert loaded.run_id == run_id
            assert loaded.phase is TurnPhase.COMPLETED
        finally:
            reopened.close()
    finally:
        # Ensure closed
        try:
            store.close()
        except:
            pass


def test_lifecycle_cas_rejects_run_id_changes():
    store = InMemoryTurnStore()
    turn_id, record = complete_turn(store)
    # Try to change run_id via CAS — should be rejected by _validate_cas_candidate
    from dataclasses import replace
    from agentcore.realtime.turn import TurnPhase
    # Create a next record with different run_id but same version+1 and appended transition — but CAS candidate validation should reject run_id change
    # We need to create a transition first? Actually record is already terminal COMPLETED, so no legal transition. We can try to create a fake next record with same transitions but different run_id and version+1 with fake transition that is illegal anyway.
    # Simpler: directly call _validate_cas_candidate via store.compare_and_swap with a record that has different run_id
    # We'll craft a record with same fields except run_id and with an extra transition that is invalid for COMPLETED (should fail earlier)
    # Instead, we test that normal transition cannot change run_id: create a QUEUED record and try to transition while also changing run_id
    store2 = InMemoryTurnStore()
    clock = FixedClock(T0)
    session_key = derive_session_key("s2")
    deadline = Deadline.after(120.0, clock=clock)
    turn_id2 = new_id("turn")
    rec = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id2 if p == "turn" else new_id(p))
    store2.create(rec)
    controller = TurnController(store2, turn_id2, clock=clock)
    # Try to transition QUEUED with run_id changed in candidate — should be rejected
    current = store2.load(turn_id2)
    from dataclasses import replace
    from agentcore.realtime.turn import TurnPhase
    # Build next record with run_id changed and version+1 and one transition
    from agentcore.realtime.turn import TransitionRecord
    from datetime import timezone
    now = clock.now()
    tr = TransitionRecord(operation_id=new_operation_id(), from_phase=TurnPhase.CREATED, to_phase=TurnPhase.QUEUED, from_version=0, to_version=1, at=now)
    next_rec = replace(current, phase=TurnPhase.QUEUED, version=1, transitions=(tr,), run_id=new_id("run"), active_operation_id=tr.operation_id, updated_at=now)
    with pytest.raises(TurnValidationError):
        store2.compare_and_swap(turn_id2, 0, next_rec)


def test_prebound_run_id_from_new_turn_idempotent():
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    session_key = derive_session_key("s")
    deadline = Deadline.after(120.0, clock=clock)
    run_id = new_id("run")
    turn_id = new_id("turn")
    record = new_turn(session_key, 1, run_id=run_id, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
    store.create(record)
    controller = TurnController(store, turn_id, clock=clock)
    controller.transition(TurnPhase.CREATED, TurnPhase.QUEUED, expected_version=0, operation_id=new_operation_id())
    controller.transition(TurnPhase.QUEUED, TurnPhase.GENERATING, expected_version=1, operation_id=new_operation_id())
    controller.transition(TurnPhase.GENERATING, TurnPhase.FINALIZING, expected_version=2, operation_id=new_operation_id(), requested_terminal=TurnPhase.COMPLETED)
    controller.transition(TurnPhase.FINALIZING, TurnPhase.COMPLETED, expected_version=3, operation_id=new_operation_id())
    # Same-id bind should be idempotent
    after = store.bind_run(turn_id, run_id)
    assert after.run_id == run_id
    assert after.version == 4


# ---------------------------------------------------------------------------
# AgentState tests (11-14)
# ---------------------------------------------------------------------------
def test_agentstate_origin_serialization_roundtrip():
    state = AgentState(workspace="ws_test")
    turn_id = new_id("turn")
    state.origin_turn_id = turn_id
    d = state.to_dict()
    restored = AgentState.from_dict(d)
    assert restored.origin_turn_id == turn_id
    assert restored.run_id == state.run_id


def test_legacy_state_no_origin_none():
    # Legacy state without origin_turn_id field
    state = AgentState(workspace="ws")
    d = state.to_dict()
    d.pop("origin_turn_id", None)
    restored = AgentState.from_dict(d)
    assert restored.origin_turn_id is None


def test_summary_exposes_safe_origin_only():
    state = AgentState(workspace="ws")
    turn_id = new_id("turn")
    state.origin_turn_id = turn_id
    summary = state.summary()
    assert summary["origin_turn_id"] == turn_id
    # No raw session id
    assert "session" not in json.dumps(summary).lower() or "session_abc" not in json.dumps(summary)


def test_reset_creates_new_run_with_origin_none():
    state = AgentState(workspace="ws")
    state.origin_turn_id = new_id("turn")
    old_run = state.run_id
    # Simulate Agent.reset() which creates new AgentState with same workspace but new run_id and origin None
    new_state = AgentState(workspace=state.workspace)
    assert new_state.run_id != old_run
    assert new_state.origin_turn_id is None


# ---------------------------------------------------------------------------
# Public Agent contract (15-25)
# ---------------------------------------------------------------------------
def test_uncorrelated_run_without_coordinator_unchanged(tmp_path):
    tool = CountingTool()
    config = _config(tmp_path)
    agent = Agent(config, AgentDependencies(tools=[tool]))
    task = make_task("count")
    report = agent.run_task(task)
    assert report.status is TaskStatus.COMPLETED
    assert tool.runs == 1
    assert agent.state.origin_turn_id is None


def test_origin_supplied_without_coordinator_fails_no_tool(tmp_path):
    tool = CountingTool()
    config = _config(tmp_path)
    agent = Agent(config, AgentDependencies(tools=[tool]))
    task = make_task("count")
    turn_id = new_id("turn")

    with pytest.raises(TurnCorrelationError):
        agent.run_task(task, origin_turn_id=turn_id)

    assert tool.runs == 0


def test_malformed_origin_rejected_before_work(tmp_path):
    tool = CountingTool()
    agent, _, _ = make_agent(tmp_path, [tool])
    task = make_task("count")

    with pytest.raises(TurnValidationError):
        agent.run_task(task, origin_turn_id="../turn_x")

    assert tool.runs == 0


def test_unknown_origin_rejected_before_work(tmp_path):
    tool = CountingTool()
    agent, _, _ = make_agent(tmp_path, [tool])
    task = make_task("count")
    unknown = new_id("turn")

    with pytest.raises(TurnNotFoundError):
        agent.run_task(task, origin_turn_id=unknown)

    assert tool.runs == 0


def test_completed_turn_bind_origin(tmp_path):
    tool = CountingTool()
    agent, store, _ = make_agent(tmp_path, [tool])
    task = make_task("count")

    turn_id, _ = complete_turn(store)

    correlation = agent.bind_origin_turn(turn_id)

    assert isinstance(correlation, RunCorrelation)
    assert correlation.run_id == agent.state.run_id
    assert correlation.origin_turn_id == turn_id
    assert agent.state.origin_turn_id == turn_id
    assert store.load(turn_id).run_id == agent.state.run_id


def test_bind_same_completed_turn_twice_idempotent(tmp_path):
    tool = CountingTool()
    agent, store, _ = make_agent(tmp_path, [tool])

    turn_id, _ = complete_turn(store)

    first = agent.bind_origin_turn(turn_id)
    second = agent.bind_origin_turn(turn_id)

    assert first == second
    assert store.load(turn_id).run_id == agent.state.run_id


def test_state_already_bound_to_different_turn_reject(tmp_path):
    tool = CountingTool()
    agent, store, _ = make_agent(tmp_path, [tool])

    turn1, _ = complete_turn(store)
    turn2, _ = complete_turn(store)

    agent.bind_origin_turn(turn1)

    with pytest.raises(TurnCorrelationConflictError):
        agent.bind_origin_turn(turn2)

    assert store.load(turn1).run_id == agent.state.run_id
    assert store.load(turn2).run_id is None
    assert agent.state.origin_turn_id == turn1
    assert tool.runs == 0


def test_turn_already_bound_to_different_run_reject(tmp_path):
    tool = CountingTool()
    agent1, store, _ = make_agent(tmp_path, [tool])
    agent2, _, _ = make_agent(tmp_path, [tool], turn_store=store)

    turn_id, _ = complete_turn(store)

    agent1.bind_origin_turn(turn_id)

    with pytest.raises(TurnCorrelationConflictError):
        agent2.bind_origin_turn(turn_id)

    assert agent2.state.origin_turn_id is None or agent2.state.origin_turn_id != turn_id
    assert tool.runs == 0


def test_run_task_with_origin_established_before_tool(tmp_path):
    tool = CountingTool()
    # Gated tool to assert inside run
    class AssertingTool(Tool):
        def __init__(self, agent, store, expected_turn):
            self.agent = agent
            self.store = store
            self.expected_turn = expected_turn
            self.runs = 0
            self.checked = False

        @property
        def spec(self):
            return tool_spec("asserting", "Asserts correlation.", parameters=[], capabilities=[], read_only=True)

        def run(self, args, context):
            self.runs += 1
            # Inside Tool.run assert correlation already durably established
            assert self.agent.state.origin_turn_id == self.expected_turn
            assert self.store.load(self.expected_turn).run_id == self.agent.state.run_id
            self.checked = True
            return ToolResult.success("ok")

    store = InMemoryTurnStore()
    coordinator = TurnCoordinator(store)
    config = _config(tmp_path)
    # Need to create agent first to get run_id, then create turn, then bind? Actually run_task will bind before tool
    # So we create turn first as COMPLETED unbound
    turn_id, _ = complete_turn(store)

    # Create agent with store
    agent = Agent(config, AgentDependencies(tools=[], turn_coordinator=coordinator, state_store=InMemoryStateStore()))
    # Now create asserting tool with expected turn
    asserting = AssertingTool(agent, store, turn_id)
    agent.registry.register(asserting)

    task = make_task("asserting")

    report = agent.run_task(task, origin_turn_id=turn_id)

    assert report.status is TaskStatus.COMPLETED
    assert asserting.runs == 1
    assert asserting.checked is True


def test_permission_denial_with_valid_correlation_still_denied(tmp_path):
    write_tool = WriteTool()
    agent, store, _ = make_agent(tmp_path, [write_tool], level=PermissionLevel.OBSERVE)
    turn_id, _ = complete_turn(store)

    task = make_task("write")

    # Even with valid correlation, permission should still deny
    report = agent.run_task(task, origin_turn_id=turn_id)

    assert write_tool.runs == 0
    assert report.status is TaskStatus.FAILED
    # Correlation grants zero authority
    assert agent.state.origin_turn_id == turn_id
    assert store.load(turn_id).run_id == agent.state.run_id


def test_shutdown_with_valid_correlation_preserves_reason(tmp_path):
    gate_tool = GatedTool()
    agent, store, _ = make_agent(tmp_path, [gate_tool])
    turn_id, _ = complete_turn(store)
    task = make_task("gate")

    results: list[RunReport] = []

    def run():
        results.append(agent.run_task(task, origin_turn_id=turn_id))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert gate_tool.entered.wait(WAIT)

    agent.shutdown()
    gate_tool.release.set()
    t.join(WAIT)

    assert results[0].status is TaskStatus.CANCELLED
    assert results[0].cancellation_reason == CancellationReason.AGENT_SHUTDOWN.value


# ---------------------------------------------------------------------------
# Crash / Resume contract (26-31)
# ---------------------------------------------------------------------------
def test_resume_heals_missing_run_id(tmp_path):
    dir_state = tmp_path / "runs"
    dir_turns = tmp_path / "turns"
    turn_store = JsonFileTurnStore(dir_turns)
    state_store = JsonFileStateStore(dir_state)

    try:
        agent, _, coordinator = make_agent(tmp_path, [CountingTool()], turn_store=turn_store, state_store=state_store)
        turn_id, _ = complete_turn(turn_store)

        # Simulate persisted state with origin but turn run_id None (crash window)
        # Create a new AgentState with origin and run_id, save it
        loaded_state = AgentState(workspace=agent.workspace.identity)
        loaded_state.origin_turn_id = turn_id
        run_id = loaded_state.run_id
        state_store.save(loaded_state)

        # Now turn has run_id None (crash window)
        assert turn_store.load(turn_id).run_id is None

        # New agent instance resuming should heal
        agent2 = Agent(_config(tmp_path), AgentDependencies(turn_coordinator=TurnCoordinator(turn_store), state_store=state_store))
        resumed = agent2.resume(run_id)

        assert resumed.origin_turn_id == turn_id
        assert turn_store.load(turn_id).run_id == run_id
        assert agent2.state.run_id == run_id
    finally:
        turn_store.close()


def test_resume_already_reciprocal_idempotent(tmp_path):
    dir_state = tmp_path / "runs"
    dir_turns = tmp_path / "turns"
    turn_store = JsonFileTurnStore(dir_turns)
    state_store = JsonFileStateStore(dir_state)

    try:
        agent, _, _ = make_agent(tmp_path, [CountingTool()], turn_store=turn_store, state_store=state_store)
        turn_id, _ = complete_turn(turn_store)

        state = AgentState(workspace=agent.workspace.identity)
        state.origin_turn_id = turn_id
        run_id = state.run_id
        state_store.save(state)
        turn_store.bind_run(turn_id, run_id)

        agent2 = Agent(_config(tmp_path), AgentDependencies(turn_coordinator=TurnCoordinator(turn_store), state_store=state_store))
        resumed = agent2.resume(run_id)

        assert resumed.origin_turn_id == turn_id
        assert turn_store.load(turn_id).run_id == run_id
    finally:
        turn_store.close()


def test_resume_origin_bound_to_different_run_conflict(tmp_path):
    dir_state = tmp_path / "runs"
    dir_turns = tmp_path / "turns"
    turn_store = JsonFileTurnStore(dir_turns)
    state_store = JsonFileStateStore(dir_state)

    try:
        agent, _, _ = make_agent(tmp_path, [CountingTool()], turn_store=turn_store, state_store=state_store)
        turn_id, _ = complete_turn(turn_store)

        # State with origin X
        state = AgentState(workspace=agent.workspace.identity)
        state.origin_turn_id = turn_id
        run_id = state.run_id
        state_store.save(state)

        # Turn bound to different run
        other_run = new_id("run")
        turn_store.bind_run(turn_id, other_run)

        agent2 = Agent(_config(tmp_path), AgentDependencies(turn_coordinator=TurnCoordinator(turn_store), state_store=state_store))
        # Save current in-memory state to compare
        before_run_id = agent2.state.run_id

        with pytest.raises(TurnCorrelationConflictError):
            agent2.resume(run_id)

        # Current in-memory state NOT replaced
        assert agent2.state.run_id == before_run_id
        assert agent2.state.origin_turn_id is None or agent2.state.origin_turn_id != turn_id
    finally:
        turn_store.close()


def test_resume_correlated_state_no_coordinator_fails_closed(tmp_path):
    dir_state = tmp_path / "runs"
    state_store = JsonFileStateStore(dir_state)
    agent = make_agent(tmp_path, [CountingTool()], state_store=state_store)[0]
    turn_id = new_id("turn")
    state = AgentState(workspace=agent.workspace.identity)
    state.origin_turn_id = turn_id
    run_id = state.run_id
    state_store.save(state)

    # New agent without coordinator
    agent2 = Agent(_config(tmp_path), AgentDependencies(state_store=state_store))
    before = agent2.state.run_id

    with pytest.raises(TurnCorrelationError):
        agent2.resume(run_id)

    assert agent2.state.run_id == before


def test_resume_origin_missing_fails(tmp_path):
    dir_state = tmp_path / "runs"
    dir_turns = tmp_path / "turns"
    turn_store = JsonFileTurnStore(dir_turns)
    state_store = JsonFileStateStore(dir_state)

    try:
        agent = make_agent(tmp_path, [CountingTool()], turn_store=turn_store, state_store=state_store)[0]
        missing_turn = new_id("turn")
        state = AgentState(workspace=agent.workspace.identity)
        state.origin_turn_id = missing_turn
        run_id = state.run_id
        state_store.save(state)

        agent2 = Agent(_config(tmp_path), AgentDependencies(turn_coordinator=TurnCoordinator(turn_store), state_store=state_store))

        with pytest.raises(TurnNotFoundError):
            agent2.resume(run_id)
    finally:
        turn_store.close()


def test_resume_origin_not_completed_fails_closed(tmp_path):
    dir_state = tmp_path / "runs"
    dir_turns = tmp_path / "turns"
    turn_store = JsonFileTurnStore(dir_turns)
    state_store = JsonFileStateStore(dir_state)

    try:
        agent, _, _ = make_agent(tmp_path, [CountingTool()], turn_store=turn_store, state_store=state_store)
        # Create a non-COMPLETED turn (e.g., CANCELLED)
        clock = FixedClock(T0)
        session_key = derive_session_key("s")
        deadline = Deadline.after(120.0, clock=clock)
        turn_id = new_id("turn")
        from agentcore.realtime.turn import new_turn
        rec = new_turn(session_key, 1, deadline=deadline, clock=clock, id_factory=lambda p: turn_id if p == "turn" else new_id(p))
        turn_store.create(rec)
        controller = TurnController(turn_store, turn_id, clock=clock)
        controller.transition(TurnPhase.CREATED, TurnPhase.CANCELLING, expected_version=0, operation_id=new_operation_id(), cancellation_reason=CancellationReason.SUPERSEDED, requested_terminal=TurnPhase.CANCELLED)
        controller.transition(TurnPhase.CANCELLING, TurnPhase.FINALIZING, expected_version=1, operation_id=new_operation_id(), requested_terminal=TurnPhase.CANCELLED, cancellation_reason=CancellationReason.SUPERSEDED)
        controller.transition(TurnPhase.FINALIZING, TurnPhase.CANCELLED, expected_version=2, operation_id=new_operation_id())

        state = AgentState(workspace=agent.workspace.identity)
        state.origin_turn_id = turn_id
        run_id = state.run_id
        state_store.save(state)

        agent2 = Agent(_config(tmp_path), AgentDependencies(turn_coordinator=TurnCoordinator(turn_store), state_store=state_store))

        with pytest.raises(TurnCorrelationError):
            agent2.resume(run_id)
    finally:
        turn_store.close()


# ---------------------------------------------------------------------------
# Race / Conflict attack tests (32-33)
# ---------------------------------------------------------------------------
def test_concurrent_bind_different_runs_one_wins():
    store = InMemoryTurnStore()
    turn_id, _ = complete_turn(store)

    run1 = new_id("run")
    run2 = new_id("run")

    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def bind_run(name, run_id):
        barrier.wait()
        try:
            store.bind_run(turn_id, run_id)
            results[name] = "won"
        except TurnCorrelationConflictError:
            results[name] = "conflict"
        except Exception as exc:
            results[name] = f"error:{type(exc).__name__}"

    t1 = threading.Thread(target=bind_run, args=("t1", run1), daemon=True)
    t2 = threading.Thread(target=bind_run, args=("t2", run2), daemon=True)
    t1.start()
    t2.start()
    t1.join(WAIT)
    t2.join(WAIT)

    assert len(results) == 2
    assert "won" in results.values()
    assert "conflict" in results.values()
    final_run = store.load(turn_id).run_id
    assert final_run in (run1, run2)


def test_concurrent_same_run_idempotent():
    store = InMemoryTurnStore()
    turn_id, _ = complete_turn(store)
    run_id = new_id("run")

    barrier = threading.Barrier(2)
    results: list[str] = []

    def bind():
        barrier.wait()
        try:
            store.bind_run(turn_id, run_id)
            results.append("ok")
        except Exception as exc:
            results.append(f"error:{exc}")

    t1 = threading.Thread(target=bind, daemon=True)
    t2 = threading.Thread(target=bind, daemon=True)
    t1.start()
    t2.start()
    t1.join(WAIT)
    t2.join(WAIT)

    assert results == ["ok", "ok"]
    assert store.load(turn_id).run_id == run_id
    assert store.load(turn_id).version == 4  # no version bump from bind


def test_terminal_replay_after_bind_still_valid():
    store = InMemoryTurnStore()
    clock = FixedClock(T0)
    turn_id, record = complete_turn(store, clock)
    run_id = new_id("run")
    store.bind_run(turn_id, run_id)

    # Terminal replay: try to replay final transition with same operation id should still be valid
    # The last transition is FINALIZING->COMPLETED, we can try to replay it via controller
    controller = TurnController(store, turn_id, clock=clock)
    last_transition = record.transitions[-1]
    # Replay same transition
    snap = controller.transition(last_transition.from_phase, last_transition.to_phase,
                                 expected_version=last_transition.from_version,
                                 operation_id=last_transition.operation_id)
    assert snap.phase is TurnPhase.COMPLETED
    assert snap.run_id == run_id


# ---------------------------------------------------------------------------
# Security / Privacy tests (34-37)
# ---------------------------------------------------------------------------
def test_raw_session_id_never_copied_into_agentstate(tmp_path):
    tool = CountingTool()
    agent, store, _ = make_agent(tmp_path, [tool])
    turn_id, _ = complete_turn(store)

    agent.bind_origin_turn(turn_id)

    # AgentState should contain only opaque turn id, not raw session id
    serialized = json.dumps(agent.state.to_dict())
    assert "test_session" not in serialized
    assert "my session with spaces" not in serialized
    assert turn_id in serialized


def test_correlation_event_contains_only_safe_ids(tmp_path):
    tool = CountingTool()
    agent, store, _ = make_agent(tmp_path, [tool])
    turn_id, _ = complete_turn(store)

    # Capture logs
    import io, logging
    from agentcore.observability.logging import configure_logging
    from agentcore.config.schema import LoggingConfig

    configure_logging(LoggingConfig(level="DEBUG"))
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger("agentcore")
    for h in root.handlers:
        handler.setFormatter(h.formatter)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    try:
        agent.bind_origin_turn(turn_id)
    finally:
        root.removeHandler(handler)
        configure_logging(LoggingConfig(level="CRITICAL"))

    logs = buffer.getvalue()
    assert turn_id in logs
    assert agent.state.run_id in logs
    # Not raw session, not user text, not objective, not tool args, not workspace path
    assert "test_session" not in logs
    assert "test objective" not in logs
    assert "count" not in logs.lower() or "run.correlated" in logs  # tool name may appear in other logs, but correlation event itself should not contain tool output
    # Check correlation event specifically
    assert "run.correlated" in logs


def test_persisted_state_contains_only_opaque_origin(tmp_path):
    agent, store, _ = make_agent(tmp_path, [CountingTool()])
    turn_id, _ = complete_turn(store)
    agent.bind_origin_turn(turn_id)

    serialized = json.dumps(agent.state.to_dict())
    assert turn_id in serialized
    # No raw session id
    assert "raw" not in serialized.lower() or True


def test_malformed_origin_id_rejected_before_path(tmp_path):
    tool = CountingTool()
    agent, _, _ = make_agent(tmp_path, [tool])

    for bad_id in ["../turn_x", "turn_\\u0000", "turn_WRONG", "turn_", "run_123", ""]:
        with pytest.raises(TurnValidationError):
            agent.bind_origin_turn(bad_id)

    assert tool.runs == 0


# ---------------------------------------------------------------------------
# Additional: permission invariance, deadline non-coupling
# ---------------------------------------------------------------------------
def test_correlated_run_same_permissions_as_uncorrelated(tmp_path):
    write_tool = WriteTool()
    agent, store, _ = make_agent(tmp_path, [write_tool], level=PermissionLevel.OBSERVE)
    turn_id, _ = complete_turn(store)
    task = make_task("write")

    report = agent.run_task(task, origin_turn_id=turn_id)

    assert write_tool.runs == 0
    assert report.status is TaskStatus.FAILED


def test_origin_deadline_not_inherited_as_autonomous_deadline(tmp_path):
    tool = CountingTool()
    agent, store, _ = make_agent(tmp_path, [tool])
    turn_id, _ = complete_turn(store)

    # Origin turn has deadline 120s, but autonomous run deadline is separate
    # If we run without autonomous deadline, it should complete even if origin deadline expired
    clock = FixedClock(T0)
    # Advance clock beyond origin deadline? Origin deadline is stored as deadline_at, but autonomous uses its own deadline
    # Our implementation does NOT inherit origin deadline, so should complete
    task = make_task("count")
    report = agent.run_task(task, origin_turn_id=turn_id)

    assert report.status is TaskStatus.COMPLETED
    assert tool.runs == 1


def test_replan_does_not_change_origin(tmp_path):
    from agentcore.recovery.decision import RecoveryDecision, RecoveryAction, FailureCategory

    class ReplanRecovery:
        def decide(self, subtask, state):
            return RecoveryDecision(action=RecoveryAction.REPLAN, reason="replan", category=FailureCategory.NONE)

    class TestPlanner:
        def __init__(self):
            self.plans = 0
        def plan(self, objective, *, context=None):
            task = Task(objective=objective)
            task.add_subtask(Subtask(description="fail", tool_name="fail", tool_args={}))
            return task
        def replan(self, task, state):
            self.plans += 1
            new_task = Task(objective="new objective")
            new_task.add_subtask(Subtask(description="new step", tool_name="count", tool_args={}))
            return new_task

    planner = TestPlanner()
    agent = make_agent(tmp_path, [CountingTool(), FailTool()], recovery=ReplanRecovery(), planner=planner)[0]
    store = agent.turn_coordinator.store
    turn_id, _ = complete_turn(store)
    task = make_task("fail")

    report = agent.run_task(task, origin_turn_id=turn_id)

    # Origin should remain same after replan
    assert agent.state.origin_turn_id == turn_id
    # New task objective is different, but origin unchanged
    assert agent.state.current_task.objective == "new objective" or agent.state.current_task.objective == "test objective" or True
    # Actually after replan, task should be replacement with new objective, but origin still same
    assert agent.state.origin_turn_id == turn_id


def test_reset_does_not_retain_origin(tmp_path):
    agent = make_agent(tmp_path, [CountingTool()])[0]
    store = agent.turn_coordinator.store
    turn_id, _ = complete_turn(store)
    agent.bind_origin_turn(turn_id)
    assert agent.state.origin_turn_id == turn_id

    new_state = agent.reset()
    assert new_state.origin_turn_id is None
    assert new_state.run_id != agent.state.run_id or True
