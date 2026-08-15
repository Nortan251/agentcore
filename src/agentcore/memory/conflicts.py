"""Durable pending memory conflicts.

Capture can detect that a new statement may contradict something already
remembered. The resolver refuses to act on that without an explicit choice —
which is correct, but until now the pending conflict existed only as a return
value, so ending the process discarded it. An agent could notice "the user's
stored preference may have changed" and then silently forget having noticed.

This module makes that observation durable::

    capture -> POSSIBLE_CONFLICT -> PendingConflictStore.record()
                                        |
                                    process exits
                                        |
    fresh process -> list_pending() -> resolve -> RESOLVED (kept for audit)

Design notes
------------
* **Its own boundary.** :class:`PendingConflictStore` is a Protocol, so capture
  and the resolver depend on an interface, never on files. The JSON backend
  writes to a ``conflicts/`` subdirectory of the memory directory; the memory
  store globs ``*.json`` non-recursively, so the two never see each other's
  records while still living under one configured location.
* **No transcripts.** A conflict stores the candidate *fact* and identifiers —
  never conversation, prompts, model replies or tool arguments.
* **Detect automatically, resolve explicitly.** Recording a conflict changes no
  memory. Only a deliberate resolution does.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from agentcore.memory.errors import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryValidationError,
)
from agentcore.memory.models import (
    MemoryProvenance,
    MemoryScope,
    MemoryTrust,
    MemoryType,
    _aware,
    _coerce_enum,
    normalise_tags,
)
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.util.clock import utc_now
from agentcore.util.ids import new_id

#: Bumped only when the persisted shape changes incompatibly.
CONFLICT_SCHEMA_VERSION = 1

#: Subdirectory of the memory directory holding conflict records. A separate
#: directory rather than a filename prefix, so the memory store's
#: non-recursive ``*.json`` glob cannot see them at all.
CONFLICTS_SUBDIR = "conflicts"

#: Longest candidate content kept on a conflict record.
MAX_CONFLICT_CONTENT = 500

#: Longest free-text reason kept on a conflict record.
MAX_CONFLICT_REASON = 240


class ConflictStatus(str, Enum):
    """Minimal lifecycle: a conflict is open, or it has been settled.

    Deliberately two states. A resolved conflict is *kept* rather than deleted
    so the decision stays auditable.
    """

    PENDING = "pending"
    RESOLVED = "resolved"

    def __str__(self) -> str:
        return self.value


def _clip(value: Any, limit: int) -> str:
    """Collapse whitespace and truncate to *at most* ``limit`` characters.

    The ellipsis is counted inside the budget, not added on top of it —
    otherwise clipping to a maximum would produce a value one character over
    that same maximum, which ``validate()`` then rejects.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"


def _parse_time(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(f"{field_name} is not a valid timestamp",
                                    value=str(value)) from exc


@dataclass(frozen=True)
class PendingConflict:
    """One unresolved (or settled) memory contradiction.

    Holds exactly enough to rebuild the decision later: the proposed fact, its
    classification and provenance, and the ids of the memories it may
    contradict. Nothing about the conversation that produced it.
    """

    #: The candidate fact, already bounded and sanitised by the caller.
    candidate_content: str
    id: str = field(default_factory=lambda: new_id("conf"))
    candidate_type: MemoryType = MemoryType.FACT
    candidate_scope: MemoryScope = MemoryScope.GLOBAL
    candidate_scope_key: str | None = None
    candidate_tags: tuple[str, ...] = ()
    candidate_trust: MemoryTrust = MemoryTrust.INFERRED
    candidate_provenance: MemoryProvenance = field(
        default_factory=MemoryProvenance)
    #: Existing memories this candidate may contradict.
    related_ids: tuple[str, ...] = ()
    #: Why capture flagged it. Operator-facing text, never an instruction.
    reason: str = ""
    status: ConflictStatus = ConflictStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    resolved_at: datetime | None = None
    #: The ``ConflictResolution`` value applied, once settled.
    resolution: str | None = None
    #: Who settled it. Free text supplied by the caller, bounded.
    resolved_by: str | None = None
    #: Memory ids the resolution actually touched, for audit.
    resolution_affected_ids: tuple[str, ...] = ()
    schema_version: int = CONFLICT_SCHEMA_VERSION

    @property
    def is_pending(self) -> bool:
        return self.status is ConflictStatus.PENDING

    def validate(self) -> PendingConflict:
        """Check every invariant. Returns self so calls can chain."""
        if not isinstance(self.id, str) or not self.id.strip():
            raise MemoryValidationError("conflict id must be a non-empty "
                                        "string")
        if "/" in self.id or "\\" in self.id:
            raise MemoryValidationError("conflict id must not contain path "
                                        "separators", conflict_id=self.id)
        if not isinstance(self.candidate_content, str) or \
                not self.candidate_content.strip():
            raise MemoryValidationError("conflict candidate content must be a "
                                        "non-empty string",
                                        conflict_id=self.id)
        if len(self.candidate_content) > MAX_CONFLICT_CONTENT:
            raise MemoryValidationError("conflict candidate content is too "
                                        "long", conflict_id=self.id,
                                        length=len(self.candidate_content))
        for name, value, enum_cls in (
                ("candidate_type", self.candidate_type, MemoryType),
                ("candidate_scope", self.candidate_scope, MemoryScope),
                ("candidate_trust", self.candidate_trust, MemoryTrust)):
            if not isinstance(value, enum_cls):
                raise MemoryValidationError(f"{name} is invalid",
                                            conflict_id=self.id)
        if not isinstance(self.status, ConflictStatus):
            raise MemoryValidationError("conflict status is invalid",
                                        conflict_id=self.id)
        if not isinstance(self.created_at, datetime):
            raise MemoryValidationError("created_at must be a datetime",
                                        conflict_id=self.id)
        if not isinstance(self.candidate_provenance, MemoryProvenance):
            raise MemoryValidationError("candidate provenance is invalid",
                                        conflict_id=self.id)
        if not isinstance(self.related_ids, tuple) or any(
                not isinstance(i, str) for i in self.related_ids):
            raise MemoryValidationError("related_ids must be a tuple of "
                                        "strings", conflict_id=self.id)
        if self.status is ConflictStatus.RESOLVED:
            if not self.resolution:
                raise MemoryValidationError(
                    "a resolved conflict must record its resolution",
                    conflict_id=self.id)
            if self.resolved_at is None:
                raise MemoryValidationError(
                    "a resolved conflict must record when it was resolved",
                    conflict_id=self.id)
        elif self.resolution or self.resolved_at:
            raise MemoryValidationError(
                "a pending conflict must not carry resolution details",
                conflict_id=self.id)
        return self

    def resolved(self, *, resolution: str, operator: str | None = None,
                 affected_ids: Sequence[str] = (),
                 now: datetime | None = None) -> PendingConflict:
        """Return a settled copy. Never mutates the original."""
        from dataclasses import replace

        return replace(
            self, status=ConflictStatus.RESOLVED,
            resolution=str(resolution),
            resolved_by=_clip(operator, 64) or None,
            resolution_affected_ids=tuple(affected_ids),
            resolved_at=now or utc_now()).validate()

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_content": self.candidate_content,
            "candidate_type": self.candidate_type.value,
            "candidate_scope": self.candidate_scope.value,
            "candidate_scope_key": self.candidate_scope_key,
            "candidate_tags": list(self.candidate_tags),
            "candidate_trust": self.candidate_trust.value,
            "candidate_provenance": self.candidate_provenance.to_dict(),
            "related_ids": list(self.related_ids),
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat()
            if self.resolved_at else None,
            "resolution": self.resolution,
            "resolved_by": self.resolved_by,
            "resolution_affected_ids": list(self.resolution_affected_ids),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PendingConflict:
        """Rebuild from persisted form, validating strictly.

        A corrupt or hand-edited file must not become a usable record: every
        field is checked, and anything malformed raises rather than being
        coerced into something that merely looks valid.
        """
        if not isinstance(data, Mapping):
            raise MemoryValidationError("conflict record must be a mapping",
                                        got=type(data).__name__)
        missing = [k for k in ("id", "candidate_content", "created_at")
                   if not data.get(k)]
        if missing:
            raise MemoryValidationError("conflict record is missing required "
                                        "fields", fields=missing)
        try:
            status = ConflictStatus(str(data.get("status", "pending")).lower())
        except ValueError as exc:
            raise MemoryValidationError(
                "unknown conflict status", value=str(data.get("status")),
                allowed=[s.value for s in ConflictStatus]) from exc

        related = data.get("related_ids") or []
        if not isinstance(related, list):
            raise MemoryValidationError("related_ids must be a list",
                                        conflict_id=str(data.get("id")))
        affected = data.get("resolution_affected_ids") or []
        if not isinstance(affected, list):
            affected = []

        record = cls(
            candidate_content=str(data["candidate_content"]),
            id=str(data["id"]),
            candidate_type=_coerce_enum(
                MemoryType, data.get("candidate_type", MemoryType.FACT),
                "memory type"),
            candidate_scope=_coerce_enum(
                MemoryScope, data.get("candidate_scope", MemoryScope.GLOBAL),
                "memory scope"),
            candidate_scope_key=data.get("candidate_scope_key"),
            candidate_tags=normalise_tags(data.get("candidate_tags") or ()),
            candidate_trust=_coerce_enum(
                MemoryTrust, data.get("candidate_trust", MemoryTrust.INFERRED),
                "memory trust"),
            candidate_provenance=MemoryProvenance.from_dict(
                data.get("candidate_provenance")),
            related_ids=tuple(str(i) for i in related),
            reason=str(data.get("reason", "") or ""),
            status=status,
            created_at=_parse_time(data["created_at"], "created_at"),
            resolved_at=_parse_time(data["resolved_at"], "resolved_at")
            if data.get("resolved_at") else None,
            resolution=data.get("resolution"),
            resolved_by=data.get("resolved_by"),
            resolution_affected_ids=tuple(str(i) for i in affected),
            schema_version=int(data.get("schema_version",
                                        CONFLICT_SCHEMA_VERSION) or
                               CONFLICT_SCHEMA_VERSION),
        )
        return record.validate()

    def describe(self) -> dict[str, Any]:
        """Log-safe summary: identity and shape, never the candidate text."""
        return {
            "conflict_id": self.id,
            "status": self.status.value,
            "type": self.candidate_type.value,
            "scope": self.candidate_scope.value,
            "trust": self.candidate_trust.value,
            "related": list(self.related_ids),
            "content_chars": len(self.candidate_content),
            "resolution": self.resolution,
        }


@runtime_checkable
class PendingConflictStore(Protocol):
    """Durable storage for unresolved memory contradictions.

    Storage-agnostic: nothing here mentions files, directories or JSON, so a
    future backend can satisfy it without any caller changing.
    """

    def record(self, conflict: PendingConflict) -> PendingConflict: ...

    def get(self, conflict_id: str) -> PendingConflict: ...

    def list_pending(self, *, limit: int | None = None,
                     scope_key: str | None = None) -> list[PendingConflict]: ...

    def list_all(self, *, limit: int | None = None) -> list[PendingConflict]: ...

    def mark_resolved(self, conflict_id: str, *, resolution: str,
                      operator: str | None = None,
                      affected_ids: Sequence[str] = ()) -> PendingConflict: ...

    def find_pending_for(self, content: str, related_ids: Sequence[str]
                         ) -> PendingConflict | None: ...


class _BaseConflictStore:
    """Rules shared by every backend, so behaviour cannot diverge."""

    def __init__(self) -> None:
        self._log = get_logger("memory.conflicts")

    # -- provided by subclasses -------------------------------------------
    def _read_all(self) -> Iterator[PendingConflict]:  # pragma: no cover
        raise NotImplementedError

    def _read_one(self, conflict_id: str) -> PendingConflict | None:  # pragma: no cover
        raise NotImplementedError

    def _write(self, conflict: PendingConflict) -> None:  # pragma: no cover
        raise NotImplementedError

    # -- API ---------------------------------------------------------------
    def record(self, conflict: PendingConflict) -> PendingConflict:
        """Persist a newly detected conflict.

        Recording changes no memory whatsoever — that is the safety property
        this whole mechanism exists to preserve.
        """
        conflict.validate()
        if self._read_one(conflict.id) is not None:
            raise MemoryConflictError("a conflict with this id already exists",
                                      conflict_id=conflict.id)
        self._write(conflict)
        self._log.info(Events.MEMORY_CONFLICT_RECORDED, **conflict.describe())
        return conflict

    def get(self, conflict_id: str) -> PendingConflict:
        found = self._read_one(conflict_id)
        if found is None:
            raise MemoryNotFoundError("no conflict with that id",
                                      conflict_id=str(conflict_id))
        return found

    def exists(self, conflict_id: str) -> bool:
        return self._read_one(conflict_id) is not None

    def list_all(self, *, limit: int | None = None) -> list[PendingConflict]:
        """Every conflict, newest first; id breaks ties so order is stable."""
        found = sorted(
            self._read_all(),
            key=lambda c: (_aware(c.created_at).timestamp(), c.id),
            reverse=True)
        return found[:limit] if limit is not None else found

    def list_pending(self, *, limit: int | None = None,
                     scope_key: str | None = None) -> list[PendingConflict]:
        """Unresolved conflicts only, in the same deterministic order."""
        found = [c for c in self.list_all() if c.is_pending]
        if scope_key is not None:
            found = [c for c in found if c.candidate_scope_key == scope_key]
        return found[:limit] if limit is not None else found

    def count_pending(self) -> int:
        return len(self.list_pending())

    def mark_resolved(self, conflict_id: str, *, resolution: str,
                      operator: str | None = None,
                      affected_ids: Sequence[str] = ()) -> PendingConflict:
        """Settle a conflict exactly once.

        Re-resolving raises: a second resolution would either duplicate a
        memory write or overwrite the record of what was actually decided.
        """
        current = self.get(conflict_id)
        if not current.is_pending:
            raise MemoryConflictError(
                "this conflict has already been resolved",
                conflict_id=current.id, resolution=current.resolution)
        settled = current.resolved(resolution=resolution, operator=operator,
                                   affected_ids=affected_ids)
        self._write(settled)
        self._log.info(Events.MEMORY_CONFLICT_RESOLVED, **settled.describe(),
                       affected=list(affected_ids))
        return settled

    def find_pending_for(self, content: str, related_ids: Sequence[str]
                         ) -> PendingConflict | None:
        """An existing pending conflict for the same candidate and targets.

        Used to keep repeated detections of one contradiction from piling up:
        saying the same thing twice is one unresolved question, not two.
        Matching is exact after normalisation — never fuzzy.
        """
        needle = " ".join(str(content or "").split()).lower()
        wanted = set(related_ids)
        for existing in self.list_pending():
            if " ".join(existing.candidate_content.split()).lower() != needle:
                continue
            if set(existing.related_ids) == wanted:
                return existing
        return None

    def delete(self, conflict_id: str, *, missing_ok: bool = False) -> None:
        """Remove a conflict record entirely. Rarely needed; audit prefers
        keeping resolved records."""
        if not self._remove(conflict_id):
            if missing_ok:
                return
            raise MemoryNotFoundError("no conflict with that id",
                                      conflict_id=str(conflict_id))
        self._log.info(Events.MEMORY_CONFLICT_DELETED,
                       conflict_id=str(conflict_id))

    def _remove(self, conflict_id: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError


class InMemoryConflictStore(_BaseConflictStore):
    """Non-durable backend for tests, mirroring ``InMemoryMemoryStore``."""

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, dict[str, Any]] = {}

    def _read_all(self) -> Iterator[PendingConflict]:
        for payload in list(self._data.values()):
            yield PendingConflict.from_dict(payload)

    def _read_one(self, conflict_id: str) -> PendingConflict | None:
        payload = self._data.get(str(conflict_id))
        return PendingConflict.from_dict(payload) if payload else None

    def _write(self, conflict: PendingConflict) -> None:
        self._data[conflict.id] = conflict.to_dict()

    def _remove(self, conflict_id: str) -> bool:
        return self._data.pop(str(conflict_id), None) is not None


class JsonFileConflictStore(_BaseConflictStore):
    """One JSON file per conflict, written atomically.

    Lives in a ``conflicts/`` subdirectory of the memory directory. The memory
    store globs ``*.json`` **non-recursively**, so conflict files are invisible
    to it — verified — while both remain under one configured location.
    """

    def __init__(self, directory: str | Path) -> None:
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, conflict_id: str) -> Path:
        raw = str(conflict_id)
        if not raw or any(c in raw for c in "/\\" + os.sep):
            raise MemoryValidationError("invalid conflict id", conflict_id=raw)
        return self.directory / f"{raw}.json"

    def _read_all(self) -> Iterator[PendingConflict]:
        for path in sorted(self.directory.glob("*.json")):
            try:
                yield PendingConflict.from_dict(
                    json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001 - one bad file must not
                # hide the rest. The id is safe to log; the content is not.
                self._log.warning(Events.MEMORY_CONFLICT_UNREADABLE,
                                  conflict_id=path.stem,
                                  error=type(exc).__name__)

    def _read_one(self, conflict_id: str) -> PendingConflict | None:
        path = self._path(conflict_id)
        if not path.exists():
            return None
        try:
            return PendingConflict.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - never crash a caller
            self._log.warning(Events.MEMORY_CONFLICT_UNREADABLE,
                              conflict_id=str(conflict_id),
                              error=type(exc).__name__)
            return None

    def _write(self, conflict: PendingConflict) -> None:
        path = self._path(conflict.id)
        payload = json.dumps(conflict.to_dict(), indent=2, default=str)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _remove(self, conflict_id: str) -> bool:
        path = self._path(conflict_id)
        if not path.exists():
            return False
        path.unlink()
        return True


def conflict_from_decision(decision: Any, *, reason: str | None = None
                           ) -> PendingConflict:
    """Build a conflict record from a ``CaptureDecision``.

    Projects the candidate onto the small persistent shape: the fact, its
    classification, provenance and the related ids. The conversation, the
    prompt and the model's reply are structurally absent — there is nowhere to
    put them.
    """
    candidate = decision.candidate
    return PendingConflict(
        candidate_content=_clip(candidate.content, MAX_CONFLICT_CONTENT),
        candidate_type=candidate.type,
        candidate_scope=candidate.scope,
        candidate_scope_key=candidate.scope_key,
        candidate_tags=tuple(candidate.tags),
        candidate_trust=candidate.trust(),
        candidate_provenance=candidate.provenance(),
        related_ids=tuple(decision.related_ids),
        reason=_clip(reason if reason is not None else decision.reason,
                     MAX_CONFLICT_REASON),
    ).validate()


def build_conflict_store(config: Any = None, *,
                         workspace_dir: str | None = None
                         ) -> JsonFileConflictStore:
    """Construct the configured durable conflict store.

    Resolves to ``<memory directory>/conflicts``, reusing exactly the same
    directory resolution as :func:`~agentcore.memory.store.build_memory_store`
    so memory and its conflicts always travel together.
    """
    from agentcore.memory.config import MemoryConfig

    memory_config = getattr(config, "memory", config)
    if memory_config is None or not isinstance(memory_config, MemoryConfig):
        memory_config = MemoryConfig()
    root = memory_config.directory
    if not root:
        base = workspace_dir or getattr(config, "workspace_dir", None) \
            or "./.agent_workspace"
        root = str(Path(base) / "memory")
    return JsonFileConflictStore(Path(root) / CONFLICTS_SUBDIR)
