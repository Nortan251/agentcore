from __future__ import annotations

import pytest

from agentcore.errors import TaskError
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
    can_transition,
)


def test_transition_rules():
    assert can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
    assert can_transition(TaskStatus.FAILED, TaskStatus.PENDING)  # retry
    assert TaskStatus.COMPLETED.is_terminal
    assert not TaskStatus.RUNNING.is_terminal


def test_illegal_transition_raises():
    task = Task(objective="x")
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.COMPLETED)
    with pytest.raises(TaskError):
        task.transition_to(TaskStatus.RUNNING)


def test_transition_sets_timestamps():
    sub = Subtask(description="s")
    assert sub.started_at is None
    sub.transition_to(TaskStatus.RUNNING)
    assert sub.started_at is not None and sub.finished_at is None
    sub.transition_to(TaskStatus.COMPLETED)
    assert sub.finished_at is not None


def test_dependency_ordering():
    task = Task(objective="build")
    a = task.add_subtask(Subtask(description="a"))
    b = task.add_subtask(Subtask(description="b", depends_on=[a.id]))
    task.add_subtask(Subtask(description="c", depends_on=[b.id]))

    assert [s.description for s in task.ready_subtasks()] == ["a"]
    a.transition_to(TaskStatus.RUNNING)
    a.transition_to(TaskStatus.COMPLETED)
    assert [s.description for s in task.ready_subtasks()] == ["b"]


def test_add_subtask_rejects_unknown_dependency():
    task = Task(objective="x")
    with pytest.raises(TaskError):
        task.add_subtask(Subtask(description="a", depends_on=["missing"]))


def test_add_subtask_rejects_duplicate_id():
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="a"))
    with pytest.raises(TaskError):
        task.add_subtask(Subtask(description="dup", id=sub.id))


def test_validate_detects_cycle():
    task = Task(objective="x")
    a = task.add_subtask(Subtask(description="a"))
    b = task.add_subtask(Subtask(description="b", depends_on=[a.id]))
    a.depends_on.append(b.id)  # introduce a cycle after the fact
    with pytest.raises(TaskError):
        task.validate()


def test_get_unknown_subtask_raises():
    with pytest.raises(TaskError):
        Task(objective="x").get_subtask("nope")


def test_from_steps_builds_a_chain():
    task = Task.from_steps("goal", ["one", "two", "three"])
    task.validate()
    assert len(task.subtasks) == 3
    assert task.subtasks[1].depends_on == [task.subtasks[0].id]
    assert task.next_subtask().description == "one"


def test_completion_and_failure_queries():
    task = Task.from_steps("goal", ["one"])
    assert not task.is_complete()
    sub = task.subtasks[0]
    sub.transition_to(TaskStatus.RUNNING)
    sub.transition_to(TaskStatus.FAILED)
    assert task.has_failure()
    assert not task.is_complete()


def test_error_record_from_exception():
    from agentcore.errors import ToolExecutionError

    record = ErrorRecord.from_exception(ToolExecutionError("bad", tool="t"))
    assert record.type == "ToolExecutionError"
    assert record.retryable is True
    assert record.details == {"tool": "t"}


def test_round_trip_serialisation():
    task = Task.from_steps("goal", ["one", "two"])
    sub = task.subtasks[0]
    sub.tool_name = "echo"
    sub.tool_args = {"message": "hi"}
    sub.transition_to(TaskStatus.RUNNING)
    sub.result = "hi"
    sub.transition_to(TaskStatus.COMPLETED)
    sub.verification = VerificationResult(status=VerificationStatus.PASSED,
                                          checked_by="t")
    task.subtasks[1].record_error(ValueError("nope"))
    task.metadata["source"] = "test"

    restored = Task.from_dict(task.to_dict())
    assert restored.to_dict() == task.to_dict()
    assert restored.objective == "goal"
    assert restored.subtasks[0].verification.passed
    assert restored.subtasks[1].errors[0].type == "ValueError"
    assert restored.metadata == {"source": "test"}
