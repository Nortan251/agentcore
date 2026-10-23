"""Task 50 PR A: opt-in strict verification gates.

The existing Agent runner remains permissive unless explicitly opted in. These
are real ToolExecutor calls using the Agent test configuration; the strict flag
does not create authorization or a new executor.
"""

from __future__ import annotations

import pytest

from agentcore.core.agent import Agent
from agentcore.core.runner import RunControl, TaskRunner
from agentcore.realtime.cancellation import CancellationReason, CancellationSource
from agentcore.tasks.models import (
    Subtask, Task, TaskStatus, VerificationResult, VerificationStatus,
)


class FixedVerifier:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    def verify(self, subtask, state):
        self.calls += 1
        return VerificationResult(status=self.status, checked_by="fixed-test")


class BrokenVerifier:
    def verify(self, subtask, state):
        raise RuntimeError("verification backend failed")


class NeverRetry:
    def should_retry(self, subtask, state):
        raise AssertionError("strict-verification failure must not request retry")


def make_chain():
    task = Task(objective="two approved steps")
    first = task.add_subtask(Subtask(
        description="first", tool_name="echo", tool_args={"message": "one"}))
    second = task.add_subtask(Subtask(
        description="second", tool_name="echo", tool_args={"message": "two"},
        depends_on=[first.id]))
    return task, first, second


@pytest.mark.parametrize("status", [
    VerificationStatus.SKIPPED,
    VerificationStatus.UNVERIFIED,
    VerificationStatus.FAILED,
])
def test_strict_verification_blocks_dependent_and_keeps_execution_truth(
        autonomous_config, status):
    agent = Agent(autonomous_config)
    verifier = FixedVerifier(status)
    agent.runner = TaskRunner(
        agent.executor, verifier=verifier, recovery=NeverRetry(),
        strict_verification=True)
    task, first, second = make_chain()
    unrelated = task.add_subtask(Subtask(
        description="independent", tool_name="echo",
        tool_args={"message": "unrelated"}))

    report = agent.run_task(task)

    assert report.status is TaskStatus.BLOCKED
    assert report.blocked and report.executed == 1 and report.failed == 0
    assert first.status is TaskStatus.BLOCKED
    assert first.result == "one"
    assert first.verification.status is status
    assert second.status is TaskStatus.PENDING
    assert unrelated.status is TaskStatus.PENDING
    assert verifier.calls == 1
    assert len(agent.state.tool_calls) == 1
    assert agent.state.tool_calls[0]["result"]["output"] == "one"
    # Re-entering the blocked task must not silently reexecute the first
    # action or advance the dependent step.
    repeated = agent.runner.run(task, agent.state)
    assert repeated.status is TaskStatus.BLOCKED
    assert len(agent.state.tool_calls) == 1
    assert second.status is TaskStatus.PENDING
    assert unrelated.status is TaskStatus.PENDING


def test_strict_pass_allows_dependency_only_after_verified(
        autonomous_config):
    agent = Agent(autonomous_config)
    verifier = FixedVerifier(VerificationStatus.PASSED)
    agent.runner = TaskRunner(
        agent.executor, verifier=verifier, strict_verification=True)
    task, first, second = make_chain()

    report = agent.run_task(task)

    assert report.status is TaskStatus.COMPLETED
    assert report.executed == 2 and report.failed == 0
    assert first.status is TaskStatus.COMPLETED
    assert second.status is TaskStatus.COMPLETED
    assert verifier.calls == 2
    assert [x["result"]["output"] for x in agent.state.tool_calls] == ["one", "two"]


@pytest.mark.parametrize("status", [
    VerificationStatus.SKIPPED,
    VerificationStatus.FAILED,
])
def test_legacy_runner_keeps_existing_completion_contract(
        autonomous_config, status):
    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(agent.executor, verifier=FixedVerifier(status))
    task, first, second = make_chain()

    report = agent.run_task(task)

    assert report.status is TaskStatus.COMPLETED
    assert report.executed == 2
    assert first.status is TaskStatus.COMPLETED
    assert second.status is TaskStatus.COMPLETED
    assert len(agent.state.tool_calls) == 2


def test_strict_verification_exception_blocks_dependency_and_propagates(
        autonomous_config):
    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(
        agent.executor, verifier=BrokenVerifier(), strict_verification=True)
    task, first, second = make_chain()

    with pytest.raises(RuntimeError, match="verification backend failed"):
        agent.run_task(task)

    assert first.status is TaskStatus.BLOCKED
    assert first.result == "one"
    assert second.status is TaskStatus.PENDING
    assert agent.state.active_subtask_id is None
    assert len(agent.state.tool_calls) == 1


def test_strict_mode_rejects_nonboolean_flag(autonomous_config):
    agent = Agent(autonomous_config)
    for invalid in (None, 0, 1, "", "true", [], {}):
        with pytest.raises(ValueError, match="strict_verification"):
            TaskRunner(agent.executor, strict_verification=invalid)


def test_strict_mode_default_null_verifier_blocks(
        autonomous_config):
    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(agent.executor, strict_verification=True)
    task, first, second = make_chain()

    report = agent.run_task(task)

    assert report.status is TaskStatus.BLOCKED
    assert first.status is TaskStatus.BLOCKED
    assert first.verification.status is VerificationStatus.SKIPPED
    assert second.status is TaskStatus.PENDING
    assert len(agent.state.tool_calls) == 1

def test_cancelled_blocked_strict_run_reports_cancellation(
        autonomous_config):
    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(agent.executor, strict_verification=True)
    task, first, second = make_chain()
    agent.run_task(task)
    assert task.status is TaskStatus.BLOCKED

    source = CancellationSource()
    source.cancel(CancellationReason.OPERATOR_CANCEL)
    report = agent.runner.run(
        task, agent.state, control=RunControl(cancellation=source.token))

    assert report.status is TaskStatus.CANCELLED
    assert report.cancellation_reason == CancellationReason.OPERATOR_CANCEL.value
    assert task.status is TaskStatus.CANCELLED
    assert first.status is TaskStatus.BLOCKED
    assert second.status is TaskStatus.PENDING
    assert len(agent.state.tool_calls) == 1

# -- Independent PR #50 review: strict re-entry must validate previous proofs --

@pytest.mark.parametrize("verdict", [
    VerificationStatus.SKIPPED,
    VerificationStatus.FAILED,
    VerificationStatus.UNVERIFIED,
])
@pytest.mark.parametrize("serialized", [False, True], ids=["in-memory", "restored"])
@pytest.mark.parametrize("parent_status", [
    TaskStatus.PENDING,
    TaskStatus.BLOCKED,
], ids=["pending-parent", "blocked-parent"])
def test_strict_readmission_refuses_unverified_completed_steps(
        autonomous_config, verdict, serialized, parent_status):
    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(
        agent.executor, verifier=FixedVerifier(VerificationStatus.PASSED),
        strict_verification=True)
    task, first, dependent = make_chain()
    independent = task.add_subtask(Subtask(
        description="unrelated step", tool_name="echo",
        tool_args={"message": "independent"}))
    first.transition_to(TaskStatus.RUNNING)
    first.result = "previous physical effect"
    first.attempts = 1
    first.transition_to(TaskStatus.COMPLETED)
    first.verification = VerificationResult(
        status=verdict, checked_by="historical-verifier")
    if parent_status is TaskStatus.BLOCKED:
        task.transition_to(TaskStatus.BLOCKED)
    if serialized:
        task = Task.from_dict(task.to_dict())
        first, dependent, independent = (
            task.get_subtask(s.id) for s in (first, dependent, independent))
    original = first.to_dict()

    report = agent.runner.run(task, agent.state)

    assert report.status is task.status is TaskStatus.BLOCKED
    assert report.blocked
    assert report.executed == report.failed == report.retried == 0
    assert first.to_dict() == original
    assert dependent.status is independent.status is TaskStatus.PENDING
    assert dependent.attempts == independent.attempts == 0
    assert agent.state.tool_calls == []

    # A second call cannot turn the stale COMPLETED result into admission.
    again = agent.runner.run(task, agent.state)
    assert again.status is TaskStatus.BLOCKED
    assert again.executed == 0
    assert agent.state.tool_calls == []


@pytest.mark.parametrize("serialized", [False, True])
def test_strict_readmission_allows_previous_verified_completed_step(
        autonomous_config, serialized):
    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(
        agent.executor, verifier=FixedVerifier(VerificationStatus.PASSED),
        strict_verification=True)
    task, first, second = make_chain()
    first.transition_to(TaskStatus.RUNNING)
    first.result = "one"
    first.attempts = 1
    first.transition_to(TaskStatus.COMPLETED)
    first.verification = VerificationResult(
        status=VerificationStatus.PASSED, checked_by="historical-verifier")
    if serialized:
        task = Task.from_dict(task.to_dict())
        first, second = (task.get_subtask(s.id) for s in (first, second))

    report = agent.runner.run(task, agent.state)

    assert report.status is task.status is TaskStatus.COMPLETED
    assert report.executed == 1
    assert first.attempts == 1
    assert second.attempts == 1
    assert second.verification.status is VerificationStatus.PASSED
    assert len(agent.state.tool_calls) == 1
    assert agent.state.tool_calls[0]["result"]["output"] == "two"


@pytest.mark.parametrize("old_status", [
    TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED,
])
@pytest.mark.parametrize("serialized", [False, True])
def test_strict_terminal_reentry_is_rejected_without_status_rewrite(
        autonomous_config, old_status, serialized):
    from agentcore.errors import StateError

    agent = Agent(autonomous_config)
    agent.runner = TaskRunner(agent.executor, strict_verification=True)
    task, first, second = make_chain()
    first.transition_to(TaskStatus.RUNNING)
    first.result = "one"
    first.transition_to(TaskStatus.COMPLETED)
    first.verification = VerificationResult(
        status=VerificationStatus.UNVERIFIED, checked_by="historical-verifier")
    if old_status is TaskStatus.COMPLETED:
        second.transition_to(TaskStatus.RUNNING)
        second.transition_to(TaskStatus.COMPLETED)
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(old_status)
    if serialized:
        task = Task.from_dict(task.to_dict())
        first, second = (task.get_subtask(s.id) for s in (first, second))

    with pytest.raises(StateError, match="terminal task"):
        agent.runner.run(task, agent.state)

    assert task.status is old_status
    assert first.status is TaskStatus.COMPLETED
    assert second.status is (TaskStatus.COMPLETED if old_status is TaskStatus.COMPLETED
                             else TaskStatus.PENDING)
    assert len(agent.state.tool_calls) == 0
