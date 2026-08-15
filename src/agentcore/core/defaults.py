"""Honest no-op defaults for the extension seams.

These let the agent run end to end today without pretending to have
capabilities it does not have. Each is explicit about what it does *not* do so
a later task can replace it without archaeology.
"""

from __future__ import annotations

from typing import Any

from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)


class PassthroughPlanner:
    """Creates a single-subtask task from the objective. No decomposition.

    Replaced by a real planner in a later task.
    """

    def plan(self, objective: str, *,
             context: dict[str, Any] | None = None) -> Task:
        task = Task(objective=objective, metadata=dict(context or {}))
        task.add_subtask(Subtask(description=objective))
        return task

    def replan(self, task: Task, state: AgentState) -> Task:
        # No re-planning capability yet; the task is returned untouched.
        return task


class NullVerifier:
    """Records SKIPPED. Verification logic belongs to a later task."""

    def verify(self, subtask: Subtask, state: AgentState) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.SKIPPED,
            checked_by="NullVerifier",
            detail="no verification strategy configured",
        )


class NoRetryStrategy:
    """Never retries. A real backoff/repair policy comes later."""

    def should_retry(self, subtask: Subtask, state: AgentState) -> bool:
        return False


class BoundedRetryStrategy:
    """Retries a failed subtask up to ``max_attempts`` if the error is retryable.

    Included because retry is the one recovery behaviour the executor needs to
    have a defined shape for; it is not enabled by default.
    """

    def __init__(self, max_attempts: int = 2) -> None:
        self.max_attempts = max_attempts

    def should_retry(self, subtask: Subtask, state: AgentState) -> bool:
        if subtask.attempts >= self.max_attempts:
            return False
        if subtask.status is not TaskStatus.FAILED:
            return False
        return bool(subtask.errors) and subtask.errors[-1].retryable
