"""Memory persistence.

Two backends, one interface — mirroring ``state/store.py`` deliberately, so
there is one storage idiom in the project rather than two:

* :class:`InMemoryMemoryStore` - non-durable, for tests and ephemeral runs.
* :class:`JsonFileMemoryStore` - one JSON file per record, written through a
  temp file and ``os.replace`` so an interrupted write can never leave a
  half-written record behind.

Both satisfy the pre-existing :class:`~agentcore.core.interfaces.MemoryStore`
Protocol (``remember``/``recall``/``search``) *and* the richer
:class:`MemoryBackend` Protocol below. The Protocol in ``core.interfaces`` was
defined before this layer existed and is intentionally left untouched: the
orchestrator still needs nothing more than write/read/search to stay decoupled.

A JSON-file-per-record layout is chosen for the same reasons the state store
uses it: no dependency, atomic writes, trivially inspectable during
development, and replaceable by SQLite or a vector index later without any
caller noticing, because callers depend on this interface rather than on files.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol, Sequence, runtime_checkable

from agentcore.memory.config import MemoryConfig
from agentcore.memory.errors import (
    MemoryConflictError,
    MemoryLimitError,
    MemoryNotFoundError,
    MemoryValidationError,
)
from agentcore.memory.models import (
    MemoryRecord,
    MemoryScope,
    MemoryTrust,
    MemoryType,
    new_memory,
    normalise_tags,
)
from agentcore.memory.query import MemoryQuery, MemorySearchResult, search_records
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.util.clock import utc_now


@runtime_checkable
class MemoryBackend(Protocol):
    """Storage-agnostic durable memory.

    Nothing here mentions files, SQL, vectors or embeddings — a future backend
    can satisfy this contract without any caller changing.
    """

    def add(self, record: MemoryRecord, *, replace: bool = False) -> MemoryRecord: ...

    def get(self, memory_id: str) -> MemoryRecord: ...

    def update(self, memory_id: str, **changes: Any) -> MemoryRecord: ...

    def delete(self, memory_id: str) -> None: ...

    def search(self, query: MemoryQuery | str | None = None,
               **kwargs: Any) -> MemorySearchResult: ...

    def all_records(self, *, include_expired: bool = False
                    ) -> list[MemoryRecord]: ...


class _BaseMemoryStore:
    """Behaviour shared by both backends: validation, conflict rules, search.

    Subclasses provide only raw persistence (``_read_all``/``_write``/
    ``_remove``/``_exists``), so the *rules* cannot diverge between them.
    """

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config or MemoryConfig()
        self.config.validate()
        self._log = get_logger("memory.store")

    # -- to be provided by subclasses --------------------------------------
    def _read_all(self) -> Iterator[MemoryRecord]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _read_one(self, memory_id: str) -> MemoryRecord | None:  # pragma: no cover
        raise NotImplementedError

    def _write(self, record: MemoryRecord) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _remove(self, memory_id: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _count(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- create ------------------------------------------------------------
    def add(self, record: MemoryRecord, *, replace: bool = False) -> MemoryRecord:
        """Store a new record.

        Duplicate rules, all deterministic and none of them silent:

        * **same id, ``replace=False``** -> :class:`MemoryConflictError`. An id
          collision means two different facts are claiming one identity.
        * **same id, ``replace=True``** -> allowed, but only as an explicit
          overwrite; ``created_at`` and provenance of the *new* record are kept
          as supplied, so the caller owns the decision.
        * **identical content, different id** -> allowed and *not* merged. The
          two may differ in scope, trust or provenance, and collapsing them
          would destroy exactly the information needed to tell them apart.
          :meth:`find_duplicates` exposes them so a caller can consolidate
          deliberately.
        * **conflicting content in the same scope** -> stored, never
          overwritten. Resolution needs judgement this layer does not have.
        """
        record.validate(max_content_chars=self.config.max_content_chars)

        existing = self._read_one(record.id)
        if existing is not None and not replace:
            raise MemoryConflictError(
                "a memory with this id already exists",
                memory_id=record.id,
                hint="use update() to change it, or replace=True to overwrite")
        if existing is None and self._count() >= self.config.max_records:
            raise MemoryLimitError(
                "memory store is full; delete records or raise "
                "memory.max_records",
                maximum=self.config.max_records)

        self._write(record)
        self._log.info(Events.MEMORY_CREATED if existing is None
                       else Events.MEMORY_UPDATED, **record.describe())
        return record

    def remember_record(self, record: MemoryRecord) -> MemoryRecord:
        """Alias for :meth:`add` that reads naturally at call sites."""
        return self.add(record)

    # -- read --------------------------------------------------------------
    def get(self, memory_id: str) -> MemoryRecord:
        record = self._read_one(memory_id)
        if record is None:
            self._log.info(Events.MEMORY_MISSED, memory_id=str(memory_id))
            raise MemoryNotFoundError("no memory with that id",
                                      memory_id=str(memory_id))
        return record

    def exists(self, memory_id: str) -> bool:
        return self._read_one(memory_id) is not None

    def all_records(self, *, include_expired: bool = False
                    ) -> list[MemoryRecord]:
        """Every stored record, newest first. Expired ones hidden by default."""
        now = utc_now()
        records = [r for r in self._read_all()
                   if include_expired or not r.is_expired(now=now)]
        records.sort(key=lambda r: (r.updated_at.timestamp(), r.id),
                     reverse=True)
        return records

    def count(self, *, include_expired: bool = True) -> int:
        return len(self.all_records(include_expired=include_expired))

    # -- update ------------------------------------------------------------
    def update(self, memory_id: str, **changes: Any) -> MemoryRecord:
        """Change a record while preserving its identity and origin.

        Delegates to :meth:`MemoryRecord.evolve`, so ``id``, ``created_at`` and
        the original provenance survive; ``revision`` and ``updated_at`` move
        forward. Provenance can be *replaced* explicitly by passing a new
        ``provenance``, which is an intentional act, not a side effect.
        """
        current = self.get(memory_id)
        updated = current.evolve(**changes)
        updated.validate(max_content_chars=self.config.max_content_chars)
        self._write(updated)
        self._log.info(Events.MEMORY_UPDATED, **updated.describe(),
                       previous_revision=current.revision)
        return updated

    # -- delete ------------------------------------------------------------
    def delete(self, memory_id: str, *, missing_ok: bool = False) -> None:
        if not self._remove(memory_id):
            if missing_ok:
                return
            raise MemoryNotFoundError("no memory with that id",
                                      memory_id=str(memory_id))
        self._log.info(Events.MEMORY_DELETED, memory_id=str(memory_id))

    def purge_expired(self, *, now: datetime | None = None) -> list[str]:
        """Delete every expired record. Deterministic and reported."""
        moment = now or utc_now()
        removed = [r.id for r in self._read_all() if r.is_expired(now=moment)]
        for memory_id in removed:
            self._remove(memory_id)
        if removed:
            self._log.info(Events.MEMORY_EXPIRED, count=len(removed))
        return sorted(removed)

    # -- search ------------------------------------------------------------
    def search(self, query: MemoryQuery | str | None = None,
               **kwargs: Any) -> MemorySearchResult:
        """Deterministic retrieval. Accepts a query object or plain text."""
        if isinstance(query, MemoryQuery):
            if kwargs:
                raise MemoryValidationError(
                    "pass either a MemoryQuery or keyword filters, not both")
            resolved = query
        else:
            resolved = MemoryQuery(text=query, **kwargs)

        result = search_records(self._read_all(), resolved,
                                max_results=self.config.max_search_results)
        # Log the shape of the search, never the query text or the content:
        # a query can itself contain something private.
        self._log.info(Events.MEMORY_SEARCHED,
                       results=len(result.hits),
                       total_matched=result.total_matched,
                       truncated=result.truncated,
                       has_text=bool((resolved.text or "").strip()),
                       types=[t.value for t in resolved.types],
                       scopes=[s.value for s in resolved.scopes])
        return result

    def find_duplicates(self) -> dict[str, list[str]]:
        """Group live records whose content is identical after normalisation.

        Reports rather than resolves: consolidation needs judgement about
        scope, trust and provenance that this layer deliberately does not make.
        """
        groups: dict[str, list[str]] = {}
        for record in self.all_records():
            key = " ".join(record.content.split()).lower()
            groups.setdefault(key, []).append(record.id)
        return {k: sorted(v) for k, v in groups.items() if len(v) > 1}

    # -- the core.interfaces.MemoryStore Protocol --------------------------
    # Implemented exactly as declared there so the existing seam works
    # unchanged; these are thin conveniences over the richer API above.
    def remember(self, key: str, value: Any, *,
                 tags: Sequence[str] = ()) -> None:
        """Protocol form: store ``value`` under the tag ``key``.

        ``key`` is recorded as a tag rather than the record id, so repeated
        calls with the same key do not collide — the id stays generated and
        unique, and the key remains a retrieval handle.
        """
        record = new_memory(str(value), tags=(str(key), *tags))
        self.add(record)

    def recall(self, key: str) -> Any:
        """Protocol form: the best current match for ``key``, or None."""
        result = self.search(MemoryQuery(text=str(key), limit=1))
        return result.hits[0].record if result.hits else None

    def search_text(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        """Protocol-compatible text search returning plain records."""
        return list(self.search(MemoryQuery(text=query, limit=limit)).records)


class InMemoryMemoryStore(_BaseMemoryStore):
    """Non-durable backend. Snapshots on write, like ``InMemoryStateStore``."""

    def __init__(self, config: MemoryConfig | None = None,
                 records: Iterable[MemoryRecord] = ()) -> None:
        super().__init__(config)
        self._data: dict[str, dict[str, Any]] = {}
        for record in records:
            self.add(record)

    def _read_all(self) -> Iterator[MemoryRecord]:
        for payload in list(self._data.values()):
            yield MemoryRecord.from_dict(payload)

    def _read_one(self, memory_id: str) -> MemoryRecord | None:
        payload = self._data.get(str(memory_id))
        return MemoryRecord.from_dict(payload) if payload else None

    def _write(self, record: MemoryRecord) -> None:
        # Store a serialised copy so later mutation cannot alter the snapshot.
        self._data[record.id] = record.to_dict()

    def _remove(self, memory_id: str) -> bool:
        return self._data.pop(str(memory_id), None) is not None

    def _count(self) -> int:
        return len(self._data)


class JsonFileMemoryStore(_BaseMemoryStore):
    """Durable backend: one JSON file per record in a single directory.

    Unreadable files are skipped during a scan rather than aborting it — one
    corrupt record must not make the whole memory unreadable — but they are
    logged, and :meth:`get` on that id still raises honestly.
    """

    def __init__(self, directory: str | Path,
                 config: MemoryConfig | None = None) -> None:
        super().__init__(config)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, memory_id: str) -> Path:
        raw = str(memory_id)
        if not raw or any(c in raw for c in "/\\" + os.sep):
            raise MemoryValidationError("invalid memory id", memory_id=raw)
        return self.directory / f"{raw}.json"

    def _read_all(self) -> Iterator[MemoryRecord]:
        for path in sorted(self.directory.glob("*.json")):
            try:
                yield MemoryRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001 - one bad file must not
                # hide the rest; the id is safe to log, the content is not.
                self._log.warning(Events.MEMORY_UNREADABLE, memory_id=path.stem,
                                  error=type(exc).__name__)

    def _read_one(self, memory_id: str) -> MemoryRecord | None:
        path = self._path(memory_id)
        if not path.exists():
            return None
        try:
            return MemoryRecord.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - report, never crash a caller
            self._log.warning(Events.MEMORY_UNREADABLE,
                              memory_id=str(memory_id),
                              error=type(exc).__name__)
            return None

    def _write(self, record: MemoryRecord) -> None:
        path = self._path(record.id)
        payload = json.dumps(record.to_dict(), indent=2, default=str)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _remove(self, memory_id: str) -> bool:
        path = self._path(memory_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _count(self) -> int:
        return sum(1 for _ in self.directory.glob("*.json"))


def build_memory_store(config: Any = None, *,
                       workspace_dir: str | None = None) -> _BaseMemoryStore:
    """Construct the configured durable store.

    Accepts an :class:`~agentcore.config.schema.AgentConfig` (the usual case)
    or a bare :class:`MemoryConfig`. The directory defaults to
    ``<workspace_dir>/memory``, mirroring how persisted runs default to
    ``<workspace_dir>/runs``.
    """
    memory_config = getattr(config, "memory", config)
    if memory_config is None or not isinstance(memory_config, MemoryConfig):
        memory_config = MemoryConfig()
    root = memory_config.directory
    if not root:
        base = workspace_dir or getattr(config, "workspace_dir", None) \
            or "./.agent_workspace"
        root = str(Path(base) / "memory")
    return JsonFileMemoryStore(root, memory_config)
