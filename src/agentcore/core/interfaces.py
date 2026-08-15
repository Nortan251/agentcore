"""Extension seams for capabilities that land in later tasks.

These Protocols exist so the orchestrator can depend on abstractions rather
than concrete implementations (dependency inversion). Each has a trivial,
honest default in :mod:`agentcore.core.defaults` — nothing here pretends to
plan, verify or remember.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from agentcore.state.models import AgentState
from agentcore.tasks.models import Subtask, Task, VerificationResult


@runtime_checkable
class Planner(Protocol):
    """Turns an objective into a task with subtasks.

    Future LLM-driven planning implements this; the foundation ships a
    pass-through that performs no decomposition.
    """

    def plan(self, objective: str, *, context: dict[str, Any] | None = None) -> Task: ...

    def replan(self, task: Task, state: AgentState) -> Task: ...


@runtime_checkable
class Verifier(Protocol):
    """Decides whether a completed subtask actually achieved its goal."""

    def verify(self, subtask: Subtask, state: AgentState) -> VerificationResult: ...


@runtime_checkable
class RecoveryStrategy(Protocol):
    """Decides what to do after a subtask fails."""

    def should_retry(self, subtask: Subtask, state: AgentState) -> bool: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Long-term memory seam. No implementation ships in this foundation.

    Deliberately minimal: later tasks may extend it, but the orchestrator
    should never need more than write/search to stay decoupled.
    """

    def remember(self, key: str, value: Any, *,
                 tags: Sequence[str] = ()) -> None: ...

    def recall(self, key: str) -> Any: ...

    def search(self, query: str, *, limit: int = 5) -> list[Any]: ...


@runtime_checkable
class ModelProvider(Protocol):
    """LLM seam. No implementation ships in this foundation."""

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str: ...
