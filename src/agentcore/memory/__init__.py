"""Persistent memory: facts that outlive the run that produced them.

The boundary
------------
``AgentState`` is working state — the current task, subtasks, tool calls,
verification verdicts, recovery and replan history: everything needed to
*continue one run*. This package is the other side of that line: information
worth keeping *across* runs and sessions.

Nothing crosses the boundary automatically. No transcript, model response or
tool argument is ever persisted here as a side effect; a memory exists only
because a caller deliberately wrote one. That is what keeps durable storage
from becoming an unstructured dumping ground — and what keeps it private.

Layering note: exports are resolved lazily (PEP 562) for the same reason as
:mod:`agentcore.recovery` — ``config.schema`` re-exports :class:`MemoryConfig`
from here, so eagerly importing ``store`` (which reaches ``observability`` and
therefore back into ``config.schema``) would create an import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentcore.memory.capture import (
        CandidateExtractor,
        CaptureAction,
        CaptureDecision,
        CapturePolicy,
        CaptureResult,
        CaptureSource,
        MemoryCandidate,
        MemoryCapture,
    )
    from agentcore.memory.conflicts import (
        ConflictStatus,
        InMemoryConflictStore,
        JsonFileConflictStore,
        PendingConflict,
        PendingConflictStore,
        build_conflict_store,
        conflict_from_decision,
    )
    from agentcore.memory.config import MemoryConfig
    from agentcore.memory.resolve import (
        ConflictResolution,
        CorrectionKind,
        CorrectionRequest,
        MemoryResolver,
        ResolutionOutcome,
        ResolutionStatus,
        parse_correction,
    )
    from agentcore.memory.errors import (
        MemoryConflictError,
        MemoryError,
        MemoryLimitError,
        MemoryNotFoundError,
        MemoryValidationError,
    )
    from agentcore.memory.models import (
        MemoryProvenance,
        MemoryRecord,
        MemoryScope,
        MemoryTrust,
        MemoryType,
        new_memory,
    )
    from agentcore.memory.query import (
        MatchKind,
        MemoryHit,
        MemoryQuery,
        MemorySearchResult,
    )
    from agentcore.memory.store import (
        InMemoryMemoryStore,
        JsonFileMemoryStore,
        MemoryBackend,
        build_memory_store,
    )

#: Public name -> defining submodule.
_EXPORTS: dict[str, str] = {
    "MemoryConfig": "agentcore.memory.config",
    "PendingConflict": "agentcore.memory.conflicts",
    "PendingConflictStore": "agentcore.memory.conflicts",
    "ConflictStatus": "agentcore.memory.conflicts",
    "InMemoryConflictStore": "agentcore.memory.conflicts",
    "JsonFileConflictStore": "agentcore.memory.conflicts",
    "build_conflict_store": "agentcore.memory.conflicts",
    "conflict_from_decision": "agentcore.memory.conflicts",
    "MemoryResolver": "agentcore.memory.resolve",
    "ResolutionOutcome": "agentcore.memory.resolve",
    "ResolutionStatus": "agentcore.memory.resolve",
    "ConflictResolution": "agentcore.memory.resolve",
    "CorrectionRequest": "agentcore.memory.resolve",
    "CorrectionKind": "agentcore.memory.resolve",
    "parse_correction": "agentcore.memory.resolve",
    "MemoryCapture": "agentcore.memory.capture",
    "MemoryCandidate": "agentcore.memory.capture",
    "CandidateExtractor": "agentcore.memory.capture",
    "CapturePolicy": "agentcore.memory.capture",
    "CaptureDecision": "agentcore.memory.capture",
    "CaptureResult": "agentcore.memory.capture",
    "CaptureAction": "agentcore.memory.capture",
    "CaptureSource": "agentcore.memory.capture",
    "MemoryRecord": "agentcore.memory.models",
    "MemoryProvenance": "agentcore.memory.models",
    "MemoryType": "agentcore.memory.models",
    "MemoryScope": "agentcore.memory.models",
    "MemoryTrust": "agentcore.memory.models",
    "new_memory": "agentcore.memory.models",
    "MemoryQuery": "agentcore.memory.query",
    "MemoryHit": "agentcore.memory.query",
    "MemorySearchResult": "agentcore.memory.query",
    "MatchKind": "agentcore.memory.query",
    "MemoryBackend": "agentcore.memory.store",
    "InMemoryMemoryStore": "agentcore.memory.store",
    "JsonFileMemoryStore": "agentcore.memory.store",
    "build_memory_store": "agentcore.memory.store",
    "MemoryError": "agentcore.memory.errors",
    "MemoryValidationError": "agentcore.memory.errors",
    "MemoryNotFoundError": "agentcore.memory.errors",
    "MemoryConflictError": "agentcore.memory.errors",
    "MemoryLimitError": "agentcore.memory.errors",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
