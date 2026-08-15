"""Memory configuration.

Lives in the memory package (like ``permissions/config.py`` and
``recovery/config.py``) to keep the layering acyclic: ``config.schema``
re-exports :class:`MemoryConfig`, so this module must not import from
``agentcore.config``. It depends on :mod:`agentcore.errors` alone.

Only settings the initial implementation actually consumes are defined. In
particular there is no ``enabled`` flag here — ``features.memory`` already
exists and is the single switch, so duplicating it would create two sources of
truth that could disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentcore.errors import ConfigError


@dataclass(frozen=True)
class MemoryConfig:
    """Where durable memory lives and how far it may grow."""

    #: Directory holding one JSON file per record. ``None`` means
    #: ``<workspace_dir>/memory``, mirroring how runs default to
    #: ``<workspace_dir>/runs``.
    directory: str | None = None
    #: Hard ceiling on stored records. Durable memory that grows without bound
    #: becomes both a performance and a privacy problem, so writes past the
    #: limit are refused loudly rather than silently evicting something the
    #: user asked to keep.
    max_records: int = 1000
    #: Default cap on results returned by a search.
    max_search_results: int = 20
    #: Longest content accepted. Memory is for durable facts, not transcripts.
    max_content_chars: int = 4000
    #: Records the planner may pull into a single prompt. Separate from
    #: ``max_search_results`` on purpose: a prompt is a far tighter budget
    #: than an operator listing, and remembered text is untrusted input to a
    #: model, so it gets its own deliberately small ceiling.
    max_planning_memories: int = 5
    #: Characters of a single record's content rendered into a prompt.
    max_planning_memory_chars: int = 400
    #: Whether conversational capture may run at all. Subordinate to
    #: ``features.memory``: with the feature off there is no store, so capture
    #: cannot happen regardless of this value. It exists so an operator can
    #: keep durable memory for retrieval while refusing automatic capture.
    capture_enabled: bool = True
    #: Candidates considered from a single conversation turn.
    max_capture_candidates: int = 5
    #: Longest candidate content accepted for capture.
    max_capture_chars: int = 500
    #: Whether the agent's own inferences may be stored without the user
    #: confirming them. Off by default: the agent must not teach itself facts.
    capture_inferred: bool = False

    def validate(self) -> None:
        if self.max_records < 1:
            raise ConfigError("memory.max_records must be at least 1",
                              value=self.max_records)
        if self.max_search_results < 1:
            raise ConfigError("memory.max_search_results must be at least 1",
                              value=self.max_search_results)
        if self.max_content_chars < 1:
            raise ConfigError("memory.max_content_chars must be at least 1",
                              value=self.max_content_chars)
        if self.max_planning_memories < 1:
            raise ConfigError("memory.max_planning_memories must be at "
                              "least 1", value=self.max_planning_memories)
        if self.max_planning_memory_chars < 1:
            raise ConfigError("memory.max_planning_memory_chars must be at "
                              "least 1", value=self.max_planning_memory_chars)
        if self.max_capture_candidates < 1:
            raise ConfigError("memory.max_capture_candidates must be at "
                              "least 1", value=self.max_capture_candidates)
        if self.max_capture_chars < 1:
            raise ConfigError("memory.max_capture_chars must be at least 1",
                              value=self.max_capture_chars)
