"""Verification context: the evidence available about a completed subtask.

Assembled once and passed to every strategy, so deterministic and
model-assisted verification judge exactly the same facts.

Two rules govern what goes in here:

* **Only real evidence.** Every field is read from the task, the subtask or
  recorded state. Nothing is inferred or invented.
* **Redacted by default.** Tool arguments can contain sensitive values, so they
  pass through the project's existing log-redaction rules before they can reach
  a model prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentcore.observability.logging import redact
from agentcore.state.models import AgentState
from agentcore.tasks.models import Subtask, Task, TaskStatus

#: Cap on how much tool output is rendered into a prompt or a detail string.
MAX_OUTPUT_CHARS = 2000


@dataclass
class VerificationContext:
    """Everything known about one completed subtask, ready to be judged."""

    subtask_id: str
    description: str
    status: TaskStatus
    objective: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    has_result: bool = False
    tool_ok: bool | None = None
    tool_error: str | None = None
    expected_result: str = ""
    requires_verification: bool = False
    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    # -- derived facts -----------------------------------------------------
    @property
    def executed_a_tool(self) -> bool:
        return self.tool_name is not None

    @property
    def completed(self) -> bool:
        return self.status is TaskStatus.COMPLETED

    @property
    def has_expectation(self) -> bool:
        """True when the plan stated what this step should produce."""
        return bool(self.expected_result.strip())

    def result_text(self) -> str:
        """Tool output rendered as bounded text for matching and prompts."""
        if not self.has_result:
            return ""
        if isinstance(self.result, str):
            text = self.result
        else:
            try:
                import json

                text = json.dumps(redact(self.result), default=str,
                                  sort_keys=True)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                text = str(self.result)
        return text[:MAX_OUTPUT_CHARS]

    def to_dict(self) -> dict[str, Any]:
        """Redacted, JSON-safe view — safe for logs and model prompts."""
        return {
            "subtask_id": self.subtask_id,
            "description": self.description,
            "status": str(self.status),
            "objective": self.objective,
            "tool_name": self.tool_name,
            "tool_args": redact(self.tool_args),
            "result": self.result_text(),
            "has_result": self.has_result,
            "tool_ok": self.tool_ok,
            "tool_error": self.tool_error,
            "expected_result": self.expected_result,
            "requires_verification": self.requires_verification,
            "attempts": self.attempts,
            "errors": list(self.errors),
        }


def _plan_annotation(task: Task | None, subtask_id: str) -> dict[str, Any]:
    """Read planner annotations for this subtask, if the plan recorded any.

    Written by ``LLMPlanner.to_task`` at
    ``task.metadata["plan"]["annotations"][subtask_id]``. Absent for tasks that
    were not produced by the planner, which is a normal case.
    """
    if task is None:
        return {}
    plan = task.metadata.get("plan")
    if not isinstance(plan, dict):
        return {}
    annotations = plan.get("annotations")
    if not isinstance(annotations, dict):
        return {}
    entry = annotations.get(subtask_id)
    return entry if isinstance(entry, dict) else {}


def _tool_invocation(state: AgentState, subtask_id: str) -> dict[str, Any]:
    """Find the most recent recorded tool call for this subtask.

    ``state.tool_calls`` entries carry no subtask id, so the reliable link is
    the history entry the state model writes alongside them.
    """
    for entry in reversed(state.history):
        if entry.kind == "tool_call" and entry.subtask_id == subtask_id:
            data = entry.data
            return data if isinstance(data, dict) else {}
    return {}


def build_context(subtask: Subtask, state: AgentState) -> VerificationContext:
    """Collect the evidence for ``subtask`` from the task and recorded state."""
    task = state.current_task
    annotation = _plan_annotation(task, subtask.id)
    invocation = _tool_invocation(state, subtask.id)
    tool_result = invocation.get("result") if isinstance(
        invocation.get("result"), dict) else {}

    expected = annotation.get("expected_result", "")
    requires = annotation.get("requires_verification", False)

    return VerificationContext(
        subtask_id=subtask.id,
        description=subtask.description,
        status=subtask.status,
        objective=task.objective if task else "",
        tool_name=subtask.tool_name,
        tool_args=dict(subtask.tool_args),
        result=subtask.result,
        has_result=subtask.result is not None,
        tool_ok=tool_result.get("ok") if tool_result else None,
        tool_error=tool_result.get("error") if tool_result else None,
        expected_result=expected if isinstance(expected, str) else "",
        requires_verification=bool(requires),
        attempts=subtask.attempts,
        errors=[e.message for e in subtask.errors],
    )
