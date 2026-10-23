"""Task 50 PR A: opt-in strict verification gates.

The existing Agent runner remains permissive unless explicitly opted in. These
are real ToolExecutor calls using the Agent test configuration; the strict flag
does not create authorization or a new executor.
"""

from __future__ import annotations

import pytest

from agentcore.core.agent import Agent
from agentcore.core.runner import TaskRunner
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

    report = agent.run_task(task)

    assert report.status is TaskStatus.BLOCKED
    assert report.blocked and report.executed == 1 and report.failed == 0
    assert first.status is TaskStatus.BLOCKED
    assert first.result == "one"
    assert first.verification.status is status
    assert second.status is TaskStatus.PENDING
    assert verifier.calls == 1
    assert len(agent.state.tool_calls) == 1
    assert agent.state.tool_calls[0]["result"]["output"] == "one"
    # Re-entering the blocked task must not silently reexecute the first
    # action or advance the dependent step.
    repeated = agent.runner.run(task, agent.state)
    assert repeated.status is TaskStatus.BLOCKED
    assert len(agent.state.tool_calls) == 1
    assert second.status is TaskStatus.PENDING


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
