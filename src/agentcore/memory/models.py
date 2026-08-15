"""Durable memory records.

The boundary this module draws
------------------------------
:class:`~agentcore.state.models.AgentState` is *working state*: what one run is
doing and how to resume it. A :class:`MemoryRecord` is the opposite — a fact
intended to outlive the run that produced it. Nothing migrates automatically
between the two; a memory exists only because a caller deliberately wrote it.

Every field below earns its place:

* ``id``/``content``          - identity and the fact itself
* ``type``/``scope``          - what kind of fact, and where it applies
* ``tags``                    - deterministic retrieval keys
* ``trust``                   - how much weight the fact deserves
* ``provenance``              - where it came from, so it can be re-checked
* ``created_at``/``updated_at``/``revision`` - lifecycle and change tracking
* ``expires_at``              - deterministic retention, opt-in

Serialisation follows the project's existing convention exactly:
``to_dict()``/``from_dict()`` with ISO-8601 timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from agentcore.memory.errors import MemoryValidationError
from agentcore.util.clock import utc_now
from agentcore.util.ids import new_id

#: Bumped only when the persisted shape changes incompatibly. Stored on every
#: record so a future migration can recognise what it is reading.
SCHEMA_VERSION = 1


class MemoryType(str, Enum):
    """What kind of fact this is.

    Deliberately small. Each entry answers a question the agent will actually
    ask; anything finer belongs in ``tags``, which needs no code change.
    """

    #: A durable statement about the user or the world: "the API runs on 8080".
    FACT = "fact"
    #: A stable choice the user wants respected: "prefers Python".
    PREFERENCE = "preference"
    #: A repeatable procedure that worked: "deploys are made via make ship".
    WORKFLOW = "workflow"
    #: A decision taken and worth not relitigating: "we chose JSON over YAML".
    DECISION = "decision"
    #: Something that happened, kept for context rather than as a rule.
    EPISODIC = "episodic"

    def __str__(self) -> str:
        return self.value


class MemoryScope(str, Enum):
    """Where the fact applies — "everywhere" vs "only here".

    Ordered from broadest to narrowest by :attr:`specificity`, which is what
    makes "prefer the more specific match" a deterministic rule rather than a
    judgement call.
    """

    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"

    @property
    def specificity(self) -> int:
        return _SCOPE_SPECIFICITY[self]

    def __str__(self) -> str:
        return self.value


_SCOPE_SPECIFICITY: dict[MemoryScope, int] = {
    MemoryScope.GLOBAL: 0,
    MemoryScope.PROJECT: 1,
    MemoryScope.SESSION: 2,
}


class MemoryTrust(str, Enum):
    """How much weight a remembered statement deserves.

    Categorical on purpose. A 0-1 score would imply a calibration this system
    does not have; these four levels describe *how the fact was obtained*,
    which is checkable, and order them by :attr:`rank`.
    """

    #: Stated by a human operator. The strongest claim available.
    USER_PROVIDED = "user_provided"
    #: Checked against reality (e.g. a tool observed it).
    VERIFIED = "verified"
    #: Derived by the agent from something else. Plausible, not confirmed.
    INFERRED = "inferred"
    #: Recorded but explicitly doubted; kept so it is not silently re-learned.
    UNCERTAIN = "uncertain"

    @property
    def rank(self) -> int:
        return _TRUST_RANK[self]

    def __str__(self) -> str:
        return self.value


_TRUST_RANK: dict[MemoryTrust, int] = {
    MemoryTrust.USER_PROVIDED: 3,
    MemoryTrust.VERIFIED: 2,
    MemoryTrust.INFERRED: 1,
    MemoryTrust.UNCERTAIN: 0,
}


def _coerce_enum(enum_cls, value, field_name: str):
    """Accept an enum member or its string value; reject anything else.

    Note ``MemoryType`` and friends subclass ``str``, so ``isinstance(value,
    str)`` is true for members too — the member check must come first.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).lower())
    except ValueError as exc:
        raise MemoryValidationError(
            f"unknown {field_name}", value=str(value),
            allowed=[m.value for m in enum_cls]) from exc


def _parse_time(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(f"{field_name} is not a valid timestamp",
                                    value=str(value)) from exc


def normalise_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Lower-case, de-duplicate and sort tags.

    Tags are retrieval keys, so "Python" and "python" must not be two
    different keys. Sorting makes the persisted form deterministic, which in
    turn makes file comparison in tests meaningful.
    """
    cleaned = {" ".join(str(t).split()).lower() for t in tags}
    return tuple(sorted(t for t in cleaned if t))


@dataclass(frozen=True)
class MemoryProvenance:
    """Where a memory came from.

    The agent must be able to answer "why do I believe this?", so origin is a
    first-class field rather than free text buried in the content. Everything
    here is an identifier or a short label — never a transcript, never tool
    arguments, never environment values.
    """

    #: True when a human supplied the fact; False when the agent inferred it.
    #: Kept separate from ``trust`` because an operator can still record a
    #: statement they are unsure about.
    operator_supplied: bool = False
    #: The run that produced the memory, so it can be traced with
    #: ``agentcore history <run_id>``.
    run_id: str | None = None
    task_id: str | None = None
    subtask_id: str | None = None
    #: Coarse origin label, e.g. a tool name or "operator". Not a payload.
    source: str | None = None
    #: Free-text note about how the fact was obtained. Bounded and sanitised
    #: by the caller; the store never fills this in automatically.
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_supplied": self.operator_supplied,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "source": self.source,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> MemoryProvenance:
        data = data or {}
        if not isinstance(data, Mapping):
            raise MemoryValidationError("provenance must be a mapping",
                                        value=type(data).__name__)
        return cls(
            operator_supplied=bool(data.get("operator_supplied", False)),
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            subtask_id=data.get("subtask_id"),
            source=data.get("source"),
            note=str(data.get("note", "") or ""),
        )


@dataclass(frozen=True)
class MemoryRecord:
    """One durable fact.

    Frozen: an update produces a *new* record via :meth:`evolve`, which bumps
    ``revision`` and ``updated_at`` while preserving ``id``, ``created_at`` and
    provenance. That is what stops an update from quietly erasing origin.
    """

    content: str
    id: str = field(default_factory=lambda: new_id("mem"))
    type: MemoryType = MemoryType.FACT
    scope: MemoryScope = MemoryScope.GLOBAL
    #: Namespaces a non-global memory, e.g. a project name. Required for
    #: PROJECT/SESSION scope, forbidden for GLOBAL — otherwise "applies
    #: everywhere" and "applies to X" would be indistinguishable.
    scope_key: str | None = None
    tags: tuple[str, ...] = ()
    trust: MemoryTrust = MemoryTrust.INFERRED
    provenance: MemoryProvenance = field(default_factory=MemoryProvenance)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    #: Increments on every content-bearing update; 1 for a fresh record.
    revision: int = 1
    #: Deterministic retention. ``None`` means "keep until deleted".
    expires_at: datetime | None = None
    schema_version: int = SCHEMA_VERSION

    # -- validation --------------------------------------------------------
    def validate(self, *, max_content_chars: int | None = None) -> MemoryRecord:
        """Check every invariant. Returns self so calls can be chained."""
        if not isinstance(self.id, str) or not self.id.strip():
            raise MemoryValidationError("memory id must be a non-empty string")
        if "/" in self.id or "\\" in self.id:
            # Ids become filenames in the JSON backend; refuse traversal here
            # rather than relying on the backend to catch it.
            raise MemoryValidationError("memory id must not contain path "
                                        "separators", memory_id=self.id)
        if not isinstance(self.content, str) or not self.content.strip():
            raise MemoryValidationError("memory content must be a non-empty "
                                        "string", memory_id=self.id)
        if max_content_chars is not None and len(self.content) > max_content_chars:
            raise MemoryValidationError(
                "memory content exceeds the configured maximum",
                memory_id=self.id, length=len(self.content),
                maximum=max_content_chars)
        if not isinstance(self.type, MemoryType):
            raise MemoryValidationError("memory type is invalid",
                                        memory_id=self.id, value=str(self.type))
        if not isinstance(self.scope, MemoryScope):
            raise MemoryValidationError("memory scope is invalid",
                                        memory_id=self.id, value=str(self.scope))
        if not isinstance(self.trust, MemoryTrust):
            raise MemoryValidationError("memory trust is invalid",
                                        memory_id=self.id, value=str(self.trust))
        if self.scope is MemoryScope.GLOBAL:
            if self.scope_key:
                raise MemoryValidationError(
                    "global memory must not carry a scope_key",
                    memory_id=self.id, scope_key=self.scope_key)
        elif not (self.scope_key or "").strip():
            raise MemoryValidationError(
                f"{self.scope} memory requires a scope_key",
                memory_id=self.id, scope=str(self.scope))
        if self.revision < 1:
            raise MemoryValidationError("revision must be at least 1",
                                        memory_id=self.id, value=self.revision)
        for name, moment in (("created_at", self.created_at),
                             ("updated_at", self.updated_at)):
            if not isinstance(moment, datetime):
                raise MemoryValidationError(f"{name} must be a datetime",
                                            memory_id=self.id)
        if self.expires_at is not None and not isinstance(self.expires_at,
                                                          datetime):
            raise MemoryValidationError("expires_at must be a datetime or None",
                                        memory_id=self.id)
        if _aware(self.updated_at) < _aware(self.created_at):
            raise MemoryValidationError("updated_at precedes created_at",
                                        memory_id=self.id)
        if not isinstance(self.tags, tuple) or any(
                not isinstance(t, str) for t in self.tags):
            raise MemoryValidationError("tags must be a tuple of strings",
                                        memory_id=self.id)
        if not isinstance(self.provenance, MemoryProvenance):
            raise MemoryValidationError("provenance is invalid",
                                        memory_id=self.id)
        return self

    # -- lifecycle ---------------------------------------------------------
    def is_expired(self, *, now: datetime | None = None) -> bool:
        """Deterministic expiry: strictly past ``expires_at``."""
        if self.expires_at is None:
            return False
        return _aware(now or utc_now()) > _aware(self.expires_at)

    def evolve(self, *, now: datetime | None = None, **changes: Any) -> MemoryRecord:
        """Return an updated copy, preserving identity and origin.

        ``id`` and ``created_at`` cannot be changed — an update that could
        rewrite them would be indistinguishable from fabricating a new record.
        ``revision`` increments and ``updated_at`` moves forward automatically.
        """
        for locked in ("id", "created_at", "revision"):
            if locked in changes:
                raise MemoryValidationError(
                    f"{locked} cannot be changed by an update",
                    memory_id=self.id)
        if "tags" in changes and changes["tags"] is not None:
            changes["tags"] = normalise_tags(changes["tags"])
        moment = now or utc_now()
        return replace(self, **changes, revision=self.revision + 1,
                       updated_at=moment)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "type": self.type.value,
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "tags": list(self.tags),
            "trust": self.trust.value,
            "provenance": self.provenance.to_dict(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "revision": self.revision,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemoryRecord:
        """Rebuild from persisted form, validating as we go.

        Unlike ``AgentState.from_dict``, missing timestamps are *not* invented:
        a record without ``created_at`` is malformed and says so, because a
        fabricated origin time would undermine provenance.
        """
        if not isinstance(data, Mapping):
            raise MemoryValidationError("memory record must be a mapping",
                                        value=type(data).__name__)
        missing = [k for k in ("id", "content", "created_at") if not data.get(k)]
        if missing:
            raise MemoryValidationError("memory record is missing required "
                                        "fields", fields=missing)
        created = _parse_time(data["created_at"], "created_at")
        record = cls(
            content=str(data["content"]),
            id=str(data["id"]),
            type=_coerce_enum(MemoryType, data.get("type", MemoryType.FACT),
                              "memory type"),
            scope=_coerce_enum(MemoryScope, data.get("scope",
                                                     MemoryScope.GLOBAL),
                               "memory scope"),
            scope_key=data.get("scope_key"),
            tags=normalise_tags(data.get("tags", ()) or ()),
            trust=_coerce_enum(MemoryTrust, data.get("trust",
                                                     MemoryTrust.INFERRED),
                               "memory trust"),
            provenance=MemoryProvenance.from_dict(data.get("provenance")),
            created_at=created,
            updated_at=_parse_time(data["updated_at"], "updated_at")
            if data.get("updated_at") else created,
            revision=int(data.get("revision", 1) or 1),
            expires_at=_parse_time(data["expires_at"], "expires_at")
            if data.get("expires_at") else None,
            schema_version=int(data.get("schema_version", SCHEMA_VERSION) or
                               SCHEMA_VERSION),
        )
        return record.validate()

    def describe(self) -> dict[str, Any]:
        """Non-sensitive summary safe to log: identity and shape, no content."""
        return {
            "memory_id": self.id,
            "type": self.type.value,
            "scope": self.scope.value,
            "scope_key": self.scope_key,
            "trust": self.trust.value,
            "revision": self.revision,
            "tags": list(self.tags),
            "content_chars": len(self.content),
        }


def _aware(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC so comparisons never raise.

    Persisted records are always UTC-aware, but a caller constructing a record
    by hand may pass a naive datetime; refusing to compare would be a worse
    failure than assuming the documented default.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def new_memory(content: str, *, type: MemoryType | str = MemoryType.FACT,
               scope: MemoryScope | str = MemoryScope.GLOBAL,
               scope_key: str | None = None,
               tags: Iterable[str] = (),
               trust: MemoryTrust | str = MemoryTrust.INFERRED,
               provenance: MemoryProvenance | None = None,
               expires_at: datetime | None = None,
               now: datetime | None = None) -> MemoryRecord:
    """Build a validated record, accepting strings for the enum fields."""
    moment = now or utc_now()
    record = MemoryRecord(
        content=content,
        type=_coerce_enum(MemoryType, type, "memory type"),
        scope=_coerce_enum(MemoryScope, scope, "memory scope"),
        scope_key=scope_key,
        tags=normalise_tags(tags),
        trust=_coerce_enum(MemoryTrust, trust, "memory trust"),
        provenance=provenance or MemoryProvenance(),
        created_at=moment,
        updated_at=moment,
        expires_at=expires_at,
    )
    return record.validate()
