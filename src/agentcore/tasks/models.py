"""Task and subtask model.

A :class:`Task` holds an objective and an ordered list of :class:`Subtask`.
Only one level of nesting is supported deliberately — richer plan trees are a
planning-layer concern and would be speculative here. Everything is
serialisable so a run can be checkpointed and resumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from agentcore.errors import TaskError
from agentcore.util.clock import utc_now
from agentcore.util.ids import new_id


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.COMPLETED, TaskStatus.FAILED,
                        TaskStatus.CANCELLED}

    def __str__(self) -> str:
        return self.value


#: Allowed status transitions. Enforced so a corrupt run fails loudly.
_ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED,
                                   TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED,
                                   TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PENDING, TaskStatus.RUNNING,
                                   TaskStatus.CANCELLED, TaskStatus.FAILED}),
    # COMPLETED -> PENDING is permitted so that work which executed but failed
    # verification can be re-attempted by the recovery layer. It is the only
    # exit from COMPLETED; nothing may move it straight back to RUNNING.
    TaskStatus.COMPLETED: frozenset({TaskStatus.PENDING}),
    TaskStatus.FAILED: frozenset({TaskStatus.PENDING}),  # retry
    TaskStatus.CANCELLED: frozenset(),
}


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in _ALLOWED[current]


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"

    def __str__(self) -> str:
        return self.value


@dataclass
class VerificationResult:
    """Outcome of checking that a subtask actually achieved its goal.

    The foundation records these; it does not yet generate them automatically.
    """

    status: VerificationStatus = VerificationStatus.UNVERIFIED
    checked_by: str = ""
    detail: str = ""
    created_at: datetime = field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checked_by": self.checked_by,
            "detail": self.detail,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationResult:
        return cls(
            status=VerificationStatus(data.get("status", "unverified")),
            checked_by=data.get("checked_by", ""),
            detail=data.get("detail", ""),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at") else utc_now(),
        )


@dataclass
class TaskError_:  # noqa: N801 - record, not an exception
    """A recorded failure attached to a task or subtask."""

    type: str
    message: str
    retryable: bool = False
    occurred_at: datetime = field(default_factory=utc_now)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "retryable": self.retryable,
            "occurred_at": self.occurred_at.isoformat(),
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskError_:
        return cls(
            type=data["type"],
            message=data["message"],
            retryable=data.get("retryable", False),
            occurred_at=datetime.fromisoformat(data["occurred_at"])
            if data.get("occurred_at") else utc_now(),
            details=data.get("details", {}),
        )

    @classmethod
    def from_exception(cls, exc: BaseException) -> TaskError_:
        retryable = bool(getattr(exc, "retryable", False))
        details = dict(getattr(exc, "details", {}) or {})
        return cls(type=type(exc).__name__, message=str(exc),
                   retryable=retryable, details=details)


#: Alias kept readable at call sites; the exception class lives in errors.py.
ErrorRecord = TaskError_


@dataclass
class Subtask:
    """A single unit of work inside a task."""

    description: str
    id: str = field(default_factory=lambda: new_id("sub"))
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    verification: VerificationResult = field(default_factory=VerificationResult)
    errors: list[ErrorRecord] = field(default_factory=list)
    attempts: int = 0
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def transition_to(self, target: TaskStatus) -> None:
        if not can_transition(self.status, target):
            raise TaskError("illegal subtask transition", subtask_id=self.id,
                            current=self.status.value, target=target.value)
        self.status = target
        if target is TaskStatus.RUNNING and self.started_at is None:
            self.started_at = utc_now()
        if target.is_terminal:
            self.finished_at = utc_now()

    def record_error(self, error: ErrorRecord | BaseException) -> ErrorRecord:
        record = (error if isinstance(error, ErrorRecord)
                  else ErrorRecord.from_exception(error))
        self.errors.append(record)
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "depends_on": list(self.depends_on),
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "result": self.result,
            "verification": self.verification.to_dict(),
            "errors": [e.to_dict() for e in self.errors],
            "attempts": self.attempts,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Subtask:
        def _dt(key: str) -> datetime | None:
            raw = data.get(key)
            return datetime.fromisoformat(raw) if raw else None

        return cls(
            description=data["description"],
            id=data.get("id") or new_id("sub"),
            status=TaskStatus(data.get("status", "pending")),
            depends_on=list(data.get("depends_on", [])),
            tool_name=data.get("tool_name"),
            tool_args=dict(data.get("tool_args", {})),
            result=data.get("result"),
            verification=VerificationResult.from_dict(data.get("verification", {})),
            errors=[ErrorRecord.from_dict(e) for e in data.get("errors", [])],
            attempts=data.get("attempts", 0),
            created_at=_dt("created_at") or utc_now(),
            started_at=_dt("started_at"),
            finished_at=_dt("finished_at"),
        )


@dataclass
class Task:
    """A goal plus the subtasks that satisfy it."""

    objective: str
    id: str = field(default_factory=lambda: new_id("task"))
    status: TaskStatus = TaskStatus.PENDING
    subtasks: list[Subtask] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[ErrorRecord] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    # -- structure ---------------------------------------------------------
    def add_subtask(self, subtask: Subtask) -> Subtask:
        if any(s.id == subtask.id for s in self.subtasks):
            raise TaskError("duplicate subtask id", subtask_id=subtask.id)
        known = {s.id for s in self.subtasks}
        for dep in subtask.depends_on:
            if dep not in known:
                raise TaskError("dependency must reference an existing subtask",
                                subtask_id=subtask.id, dependency=dep)
        self.subtasks.append(subtask)
        return subtask

    def get_subtask(self, subtask_id: str) -> Subtask:
        for sub in self.subtasks:
            if sub.id == subtask_id:
                return sub
        raise TaskError("unknown subtask", subtask_id=subtask_id, task_id=self.id)

    # -- scheduling --------------------------------------------------------
    def _completed_ids(self) -> set[str]:
        return {s.id for s in self.subtasks if s.status is TaskStatus.COMPLETED}

    def ready_subtasks(self) -> list[Subtask]:
        """Pending subtasks whose dependencies have all completed."""
        done = self._completed_ids()
        return [s for s in self.subtasks
                if s.status is TaskStatus.PENDING and set(s.depends_on) <= done]

    def next_subtask(self) -> Subtask | None:
        ready = self.ready_subtasks()
        return ready[0] if ready else None

    def is_complete(self) -> bool:
        return bool(self.subtasks) and all(
            s.status is TaskStatus.COMPLETED for s in self.subtasks)

    def has_failure(self) -> bool:
        return any(s.status is TaskStatus.FAILED for s in self.subtasks)

    def validate(self) -> None:
        """Reject unknown dependencies and dependency cycles."""
        ids = {s.id for s in self.subtasks}
        for sub in self.subtasks:
            for dep in sub.depends_on:
                if dep not in ids:
                    raise TaskError("unknown dependency", subtask_id=sub.id,
                                    dependency=dep)
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        graph = {s.id: list(s.depends_on) for s in self.subtasks}
        state: dict[str, int] = {}  # 0 = visiting, 1 = done

        def visit(node: str, trail: list[str]) -> None:
            mark = state.get(node)
            if mark == 1:
                return
            if mark == 0:
                cycle = trail[trail.index(node):] + [node]
                raise TaskError("dependency cycle detected", cycle=cycle)
            state[node] = 0
            for dep in graph.get(node, []):
                visit(dep, trail + [node])
            state[node] = 1

        for node in graph:
            visit(node, [])

    # -- lifecycle ---------------------------------------------------------
    def transition_to(self, target: TaskStatus) -> None:
        if not can_transition(self.status, target):
            raise TaskError("illegal task transition", task_id=self.id,
                            current=self.status.value, target=target.value)
        self.status = target
        if target is TaskStatus.RUNNING and self.started_at is None:
            self.started_at = utc_now()
        if target.is_terminal:
            self.finished_at = utc_now()

    def record_error(self, error: ErrorRecord | BaseException) -> ErrorRecord:
        record = (error if isinstance(error, ErrorRecord)
                  else ErrorRecord.from_exception(error))
        self.errors.append(record)
        return record

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "status": self.status.value,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "metadata": self.metadata,
            "errors": [e.to_dict() for e in self.errors],
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        def _dt(key: str) -> datetime | None:
            raw = data.get(key)
            return datetime.fromisoformat(raw) if raw else None

        return cls(
            objective=data["objective"],
            id=data.get("id") or new_id("task"),
            status=TaskStatus(data.get("status", "pending")),
            subtasks=[Subtask.from_dict(s) for s in data.get("subtasks", [])],
            metadata=dict(data.get("metadata", {})),
            errors=[ErrorRecord.from_dict(e) for e in data.get("errors", [])],
            created_at=_dt("created_at") or utc_now(),
            started_at=_dt("started_at"),
            finished_at=_dt("finished_at"),
        )

    @classmethod
    def from_steps(cls, objective: str, steps: Iterable[str]) -> Task:
        """Convenience builder: a linear chain of subtasks."""
        task = cls(objective=objective)
        previous: str | None = None
        for step in steps:
            sub = Subtask(description=step,
                          depends_on=[previous] if previous else [])
            task.add_subtask(sub)
            previous = sub.id
        return task
