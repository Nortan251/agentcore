from __future__ import annotations

import pytest

from agentcore.errors import StateError
from agentcore.state.models import AgentPhase, AgentState, PendingAction
from agentcore.state.store import InMemoryStateStore, JsonFileStateStore, StateStore
from agentcore.tasks.models import Task, TaskStatus, VerificationResult, VerificationStatus


def test_phase_transitions_are_validated():
    state = AgentState()
    state.transition_phase(AgentPhase.EXECUTING)
    assert state.phase is AgentPhase.EXECUTING
    with pytest.raises(StateError):
        state.transition_phase(AgentPhase.IDLE)


def test_transition_records_history_and_resume_point():
    state = AgentState()
    state.transition_phase(AgentPhase.PLANNING, note="starting")
    entry = state.history[-1]
    assert entry.kind == "transition"
    assert entry.data == {"from": "idle", "to": "planning", "note": "starting"}
    assert state.resume_point.phase is AgentPhase.PLANNING


def test_version_increments_on_mutation():
    state = AgentState()
    before = state.version
    state.transition_phase(AgentPhase.EXECUTING)
    assert state.version > before


def test_set_task_and_active_subtask():
    state = AgentState()
    task = Task.from_steps("goal", ["one", "two"])
    state.set_task(task)
    assert state.resume_point.task_id == task.id
    state.set_active_subtask(task.subtasks[0].id)
    assert state.active_subtask is task.subtasks[0]
    assert state.resume_point.subtask_id == task.subtasks[0].id


def test_active_subtask_must_exist():
    state = AgentState()
    with pytest.raises(StateError):
        state.set_active_subtask("sub_x")
    state.set_task(Task.from_steps("goal", ["one"]))
    with pytest.raises(Exception):
        state.set_active_subtask("sub_missing")


def test_record_tool_call_and_error():
    state = AgentState()
    state.record_tool_call({"tool": "echo", "result": {"ok": True}})
    state.record_error(ValueError("bad"))
    assert len(state.tool_calls) == 1
    assert state.errors[0].type == "ValueError"
    kinds = [h.kind for h in state.history]
    assert "tool_call" in kinds and "error" in kinds


def test_record_verification():
    state = AgentState()
    state.record_verification(
        VerificationResult(status=VerificationStatus.PASSED, checked_by="v"),
        subtask_id="sub_1")
    assert state.verifications[0]["status"] == "passed"
    assert state.verifications[0]["subtask_id"] == "sub_1"


def test_pending_actions_lifecycle():
    state = AgentState()
    action = state.add_pending_action(PendingAction(description="ask a human"))
    assert len(state.pending_actions) == 1
    assert state.clear_pending_action(action.id) is action
    assert state.pending_actions == []
    with pytest.raises(StateError):
        state.clear_pending_action("missing")


def test_is_finished_tracks_task_and_phase():
    state = AgentState()
    assert not state.is_finished()
    task = Task.from_steps("goal", ["one"])
    state.set_task(task)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.COMPLETED)
    assert state.is_finished()


def test_summary_shape():
    state = AgentState()
    task = Task.from_steps("goal", ["one", "two"])
    state.set_task(task)
    summary = state.summary()
    assert summary["objective"] == "goal"
    assert summary["subtasks_total"] == 2
    assert summary["subtasks_completed"] == 0


def test_state_round_trip():
    state = AgentState()
    state.set_task(Task.from_steps("goal", ["one"]))
    state.transition_phase(AgentPhase.EXECUTING)
    state.record_tool_call({"tool": "echo", "result": {"ok": True}})
    state.record_error(RuntimeError("x"))
    state.add_pending_action(PendingAction(description="p"))

    restored = AgentState.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()
    assert restored.run_id == state.run_id
    assert restored.current_task is not None
    assert restored.phase is AgentPhase.EXECUTING


@pytest.mark.parametrize("kind", ["memory", "json"])
def test_stores_satisfy_the_protocol_and_round_trip(kind, tmp_path):
    store = InMemoryStateStore() if kind == "memory" else JsonFileStateStore(tmp_path)
    assert isinstance(store, StateStore)

    state = AgentState()
    state.set_task(Task.from_steps("goal", ["one"]))
    store.save(state)

    assert store.exists(state.run_id)
    assert store.list_runs() == [state.run_id]
    loaded = store.load(state.run_id)
    assert loaded.run_id == state.run_id
    assert loaded.current_task.objective == "goal"

    store.delete(state.run_id)
    assert not store.exists(state.run_id)
    with pytest.raises(StateError):
        store.load(state.run_id)


def test_in_memory_store_snapshots_rather_than_aliasing():
    store = InMemoryStateStore()
    state = AgentState()
    store.save(state)
    state.transition_phase(AgentPhase.EXECUTING)
    assert store.load(state.run_id).phase is AgentPhase.IDLE


def test_json_store_rejects_path_traversal(tmp_path):
    store = JsonFileStateStore(tmp_path)
    with pytest.raises(StateError):
        store.load("../escape")


def test_json_store_overwrites_atomically(tmp_path):
    store = JsonFileStateStore(tmp_path)
    state = AgentState()
    store.save(state)
    state.transition_phase(AgentPhase.EXECUTING)
    store.save(state)
    assert store.load(state.run_id).phase is AgentPhase.EXECUTING
    assert list(tmp_path.glob("*.tmp")) == []
