"""Explicit agent state.

:class:`AgentState` is the complete, serialisable picture of a run: what the
agent is doing, what it has done, what went wrong, and where to resume. Keeping
it explicit (rather than scattered across object attributes) is what makes
checkpoint/resume and post-hoc debugging possible.

This is *working* state, not memory. Long-term memory arrives later and will
attach through :mod:`agentcore.state.store` and the history it records here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from agentcore.errors import StateError
from agentcore.tasks.models import ErrorRecord, Task, TaskStatus, VerificationResult
from agentcore.util.clock import utc_now
from agentcore.util.ids import new_id


class AgentPhase(str, Enum):
    """Coarse lifecycle phase of the agent itself, distinct from task status."""

    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    WAITING_APPROVAL = "waiting_approval"
    DONE = "done"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


_ALLOWED_PHASES: dict[AgentPhase, frozenset[AgentPhase]] = {
    AgentPhase.IDLE: frozenset({AgentPhase.PLANNING, AgentPhase.EXECUTING,
                                AgentPhase.DONE, AgentPhase.ERROR}),
    AgentPhase.PLANNING: frozenset({AgentPhase.EXECUTING, AgentPhase.ERROR,
                                    AgentPhase.IDLE}),
    AgentPhase.EXECUTING: frozenset({AgentPhase.VERIFYING, AgentPhase.RECOVERING,
                                     AgentPhase.WAITING_APPROVAL, AgentPhase.DONE,
                                     AgentPhase.ERROR, AgentPhase.PLANNING}),
    AgentPhase.VERIFYING: frozenset({AgentPhase.EXECUTING, AgentPhase.RECOVERING,
                                     AgentPhase.DONE, AgentPhase.ERROR}),
    AgentPhase.RECOVERING: frozenset({AgentPhase.EXECUTING, AgentPhase.PLANNING,
                                      AgentPhase.ERROR, AgentPhase.DONE}),
    AgentPhase.WAITING_APPROVAL: frozenset({AgentPhase.EXECUTING, AgentPhase.ERROR,
                                            AgentPhase.DONE}),
    AgentPhase.DONE: frozenset({AgentPhase.IDLE}),
    AgentPhase.ERROR: frozenset({AgentPhase.IDLE, AgentPhase.RECOVERING}),
}


@dataclass
class HistoryEntry:
    """One append-only fact about what happened during the run."""

    kind: str                      # e.g. "tool_call", "transition", "verification"
    summary: str
    at: datetime = field(default_factory=utc_now)
    data: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    subtask_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "at": self.at.isoformat(),
            "data": self.data,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryEntry:
        return cls(
            kind=data["kind"],
            summary=data.get("summary", ""),
            at=datetime.fromisoformat(data["at"]) if data.get("at") else utc_now(),
            data=dict(data.get("data", {})),
            task_id=data.get("task_id"),
            subtask_id=data.get("subtask_id"),
        )


@dataclass
class PendingAction:
    """An action the agent wants to take but may not yet execute.

    Produced at PROPOSE level, or while awaiting approval at CONFIRM level.
    """

    description: str
    id: str = field(default_factory=lambda: new_id("act"))
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    subtask_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "reason": self.reason,
            "subtask_id": self.subtask_id,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingAction:
        return cls(
            description=data["description"],
            id=data.get("id") or new_id("act"),
            tool_name=data.get("tool_name"),
            tool_args=dict(data.get("tool_args", {})),
            reason=data.get("reason", ""),
            subtask_id=data.get("subtask_id"),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at") else utc_now(),
        )


@dataclass
class ResumePoint:
    """Where a interrupted run should pick up."""

    task_id: str | None = None
    subtask_id: str | None = None
    phase: AgentPhase = AgentPhase.IDLE
    note: str = ""
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "phase": self.phase.value,
            "note": self.note,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResumePoint:
        return cls(
            task_id=data.get("task_id"),
            subtask_id=data.get("subtask_id"),
            phase=AgentPhase(data.get("phase", "idle")),
            note=data.get("note", ""),
            updated_at=datetime.fromisoformat(data["updated_at"])
            if data.get("updated_at") else utc_now(),
        )


@dataclass
class AgentState:
    """The complete working state of one agent run."""

    run_id: str = field(default_factory=lambda: new_id("run"))
    phase: AgentPhase = AgentPhase.IDLE
    current_task: Task | None = None
    active_subtask_id: str | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[ErrorRecord] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[PendingAction] = field(default_factory=list)
    resume_point: ResumePoint = field(default_factory=ResumePoint)
    scratch: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 0

    # -- mutation helpers --------------------------------------------------
    def _touch(self) -> None:
        self.updated_at = utc_now()
        self.version += 1

    def transition_phase(self, target: AgentPhase, *, note: str = "") -> None:
        if target is not self.phase and target not in _ALLOWED_PHASES[self.phase]:
            raise StateError("illegal phase transition", run_id=self.run_id,
                             current=self.phase.value, target=target.value)
        previous = self.phase
        self.phase = target
        self.record(HistoryEntry(
            kind="transition",
            summary=f"{previous} -> {target}",
            data={"from": previous.value, "to": target.value, "note": note},
            task_id=self.current_task.id if self.current_task else None,
            subtask_id=self.active_subtask_id,
        ))
        self.update_resume_point(phase=target, note=note or f"phase {target}")

    def record(self, entry: HistoryEntry) -> HistoryEntry:
        self.history.append(entry)
        self._touch()
        return entry

    def record_tool_call(self, invocation: Any) -> dict[str, Any]:
        """Append a tool invocation record (accepts anything with ``to_dict``)."""
        payload = (invocation.to_dict() if hasattr(invocation, "to_dict")
                   else dict(invocation))
        self.tool_calls.append(payload)
        self.record(HistoryEntry(
            kind="tool_call",
            summary=f"{payload.get('tool')} -> "
                    f"{'ok' if payload.get('result', {}).get('ok') else 'failed'}",
            data=payload,
            task_id=self.current_task.id if self.current_task else None,
            subtask_id=self.active_subtask_id,
        ))
        return payload

    def record_error(self, error: ErrorRecord | BaseException) -> ErrorRecord:
        record = (error if isinstance(error, ErrorRecord)
                  else ErrorRecord.from_exception(error))
        self.errors.append(record)
        self.record(HistoryEntry(
            kind="error", summary=f"{record.type}: {record.message}",
            data=record.to_dict(),
            task_id=self.current_task.id if self.current_task else None,
            subtask_id=self.active_subtask_id,
        ))
        return record

    def record_verification(self, result: VerificationResult,
                            *, subtask_id: str | None = None) -> dict[str, Any]:
        payload = result.to_dict()
        payload["subtask_id"] = subtask_id or self.active_subtask_id
        self.verifications.append(payload)
        self.record(HistoryEntry(
            kind="verification", summary=f"verification {result.status}",
            data=payload,
            task_id=self.current_task.id if self.current_task else None,
            subtask_id=payload["subtask_id"],
        ))
        return payload

    def add_pending_action(self, action: PendingAction) -> PendingAction:
        self.pending_actions.append(action)
        self.record(HistoryEntry(
            kind="pending_action", summary=action.description,
            data=action.to_dict(),
            task_id=self.current_task.id if self.current_task else None,
            subtask_id=action.subtask_id,
        ))
        return action

    def clear_pending_action(self, action_id: str) -> PendingAction:
        for i, action in enumerate(self.pending_actions):
            if action.id == action_id:
                self._touch()
                return self.pending_actions.pop(i)
        raise StateError("unknown pending action", action_id=action_id)

    def record_decision(self, action: PendingAction, *, decision: str,
                        operator: str = "operator", note: str = "",
                        ) -> HistoryEntry:
        """Record a human decision about a pending action.

        Written to the same append-only history as every other fact, so the
        audit trail lives in one place and survives checkpoint/resume. This
        records *only* the decision — it neither clears the action nor grants
        any permission; callers do that explicitly.
        """
        return self.record(HistoryEntry(
            kind="operator_decision",
            summary=f"{decision} by {operator}: {action.description}",
            data={
                "decision": decision,
                "operator": operator,
                "action_id": action.id,
                "description": action.description,
                "reason": action.reason,
                "note": note,
                # Deliberately no tool_args: an audit record must never carry
                # potentially sensitive argument values.
                "tool_name": action.tool_name,
                "subtask_id": action.subtask_id,
            },
            task_id=self.current_task.id if self.current_task else None,
            subtask_id=action.subtask_id,
        ))

    def set_task(self, task: Task | None) -> None:
        self.current_task = task
        self.active_subtask_id = None
        if task is not None:
            self.update_resume_point(task_id=task.id, subtask_id=None,
                                     note="task assigned")
        self._touch()

    def set_active_subtask(self, subtask_id: str | None) -> None:
        if subtask_id is not None:
            if self.current_task is None:
                raise StateError("cannot set a subtask without a current task",
                                 subtask_id=subtask_id)
            self.current_task.get_subtask(subtask_id)  # validates existence
        self.active_subtask_id = subtask_id
        self.update_resume_point(subtask_id=subtask_id)

    def update_resume_point(self, *, task_id: str | None = None,
                            subtask_id: str | None = None,
                            phase: AgentPhase | None = None,
                            note: str | None = None) -> ResumePoint:
        rp = self.resume_point
        self.resume_point = ResumePoint(
            task_id=task_id if task_id is not None else (
                self.current_task.id if self.current_task else rp.task_id),
            subtask_id=subtask_id if subtask_id is not None else self.active_subtask_id,
            phase=phase or self.phase,
            note=note if note is not None else rp.note,
        )
        self._touch()
        return self.resume_point

    # -- queries -----------------------------------------------------------
    @property
    def active_subtask(self):  # -> Subtask | None
        if self.current_task is None or self.active_subtask_id is None:
            return None
        return self.current_task.get_subtask(self.active_subtask_id)

    def is_finished(self) -> bool:
        if self.phase in {AgentPhase.DONE, AgentPhase.ERROR}:
            return True
        return (self.current_task is not None
                and self.current_task.status.is_terminal)

    def summary(self) -> dict[str, Any]:
        """Compact snapshot for logs and dashboards."""
        task = self.current_task
        return {
            "run_id": self.run_id,
            "phase": self.phase.value,
            "version": self.version,
            "task_id": task.id if task else None,
            "task_status": task.status.value if task else None,
            "objective": task.objective if task else None,
            "active_subtask_id": self.active_subtask_id,
            "subtasks_total": len(task.subtasks) if task else 0,
            "subtasks_completed": sum(
                1 for s in task.subtasks if s.status is TaskStatus.COMPLETED
            ) if task else 0,
            "tool_calls": len(self.tool_calls),
            "errors": len(self.errors),
            "pending_actions": len(self.pending_actions),
            "history_entries": len(self.history),
        }

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "phase": self.phase.value,
            "current_task": self.current_task.to_dict() if self.current_task else None,
            "active_subtask_id": self.active_subtask_id,
            "history": [h.to_dict() for h in self.history],
            "tool_calls": self.tool_calls,
            "errors": [e.to_dict() for e in self.errors],
            "verifications": self.verifications,
            "pending_actions": [p.to_dict() for p in self.pending_actions],
            "resume_point": self.resume_point.to_dict(),
            "scratch": self.scratch,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentState:
        return cls(
            run_id=data.get("run_id") or new_id("run"),
            phase=AgentPhase(data.get("phase", "idle")),
            current_task=Task.from_dict(data["current_task"])
            if data.get("current_task") else None,
            active_subtask_id=data.get("active_subtask_id"),
            history=[HistoryEntry.from_dict(h) for h in data.get("history", [])],
            tool_calls=list(data.get("tool_calls", [])),
            errors=[ErrorRecord.from_dict(e) for e in data.get("errors", [])],
            verifications=list(data.get("verifications", [])),
            pending_actions=[PendingAction.from_dict(p)
                             for p in data.get("pending_actions", [])],
            resume_point=ResumePoint.from_dict(data.get("resume_point", {})),
            scratch=dict(data.get("scratch", {})),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at") else utc_now(),
            updated_at=datetime.fromisoformat(data["updated_at"])
            if data.get("updated_at") else utc_now(),
            version=data.get("version", 0),
        )
