"""Conversation sessions: the dialogue, kept separate from everything else.

The project now has three kinds of state, and this module exists to keep the
third one from contaminating the other two:

* **Run state** (:class:`~agentcore.state.models.AgentState`) — what one
  autonomous task is doing and how to resume it.
* **Durable memory** (:mod:`agentcore.memory`) — facts intentionally kept
  across sessions.
* **Conversation history** (here) — the recent dialogue needed to understand
  what the user is saying *right now*.

A session is deliberately **not** memory. Nothing in a session is durable
knowledge; messages are retained only so the next turn makes sense, and they
are never promoted into the memory store. Equally, a session is not run state:
mixing dialogue into ``AgentState`` would corrupt checkpoint/resume semantics,
so sessions get their own store with their own directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from agentcore.conversation.errors import (
    ConversationValidationError,
    SessionDomainError,
    SessionNotFoundError,
)
from agentcore.models.messages import Message, Role
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.util.clock import utc_now
from agentcore.util.ids import new_id

#: Bumped only when the persisted shape changes incompatibly.
SESSION_SCHEMA_VERSION = 1

#: Subdirectory of the workspace holding persisted sessions.
SESSIONS_SUBDIR = "conversations"
BROWSER_SESSIONS_SUBDIR = ".browser_sessions"
BROWSER_SESSION_MARKER = "browser_read_isolated_v1"


def validate_session_domain(metadata, *, domain="ordinary"):
    """Validate host-selected domains before deserializing/replaying history."""
    if not isinstance(metadata, Mapping):
        raise SessionDomainError("conversation_session_domain_mismatch")
    valid = (domain == "ordinary" and BROWSER_SESSION_MARKER not in metadata
             or domain == "browser_read" and metadata.get(BROWSER_SESSION_MARKER) is True)
    if not valid:
        raise SessionDomainError("conversation_session_domain_mismatch")


def validate_session_directory(directory, *, domain):
    # Reserve the browser namespace before its first transcript is visible.
    if domain == "ordinary" and directory is not None:
        try:
            reserved = (os.path.normcase(Path(directory).resolve().name)
                        == os.path.normcase(BROWSER_SESSIONS_SUBDIR))
        except (OSError, RuntimeError):
            raise SessionDomainError("conversation_session_domain_mismatch") from None
        if reserved:
            raise SessionDomainError("conversation_session_domain_mismatch")


class _SessionDomain:
    """Bind a store to its composition without changing its identity/API."""

    _session_domain = None

    def bind_domain(self, *, domain):
        if domain not in ("ordinary", "browser_read"):
            raise SessionDomainError("conversation_session_domain_mismatch")
        validate_session_directory(getattr(self, "directory", None), domain=domain)
        # A single dictionary operation publishes the immutable binding even
        # when competing composition roots share this store across threads.
        if self.__dict__.setdefault("_session_domain", domain) != domain:
            raise SessionDomainError("conversation_session_domain_mismatch")

    def _validate_domain(self, metadata, *, requested_domain=None):
        if (self._session_domain is not None and requested_domain is not None
                and self._session_domain != requested_domain):
            raise SessionDomainError("conversation_session_domain_mismatch")
        domain = self._session_domain or requested_domain
        if domain is not None:
            validate_session_directory(getattr(self, "directory", None), domain=domain)
            validate_session_domain(metadata, domain=domain)


@dataclass
class ConversationSession:
    """One ongoing dialogue.

    Mutable by design — a conversation grows — but every message is an
    immutable :class:`Message`, reusing the existing abstraction rather than
    inventing a second one.
    """

    id: str = field(default_factory=lambda: new_id("conv"))
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    #: Optional project/scope this conversation belongs to. Passed to memory
    #: retrieval so a project conversation can prefer project memories.
    scope_key: str | None = None
    #: Small, caller-owned annotations. Never used as instructions.
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SESSION_SCHEMA_VERSION

    # -- mutation ----------------------------------------------------------
    def append(self, message: Message, *, max_messages: int | None = None,
               max_message_chars: int | None = None) -> Message:
        """Add a message, enforcing the configured bounds.

        Trimming drops the *oldest* turns from storage. That loses recent
        dialogue context, which is acceptable; what must never happen is old
        turns being quietly written into durable memory to "preserve" them.
        """
        if not isinstance(message, Message):
            raise ConversationValidationError(
                "only Message objects may be appended to a session",
                got=type(message).__name__)
        if max_message_chars is not None and \
                len(message.content) > max_message_chars:
            raise ConversationValidationError(
                "message exceeds the configured maximum length",
                length=len(message.content), maximum=max_message_chars)
        self.messages.append(message)
        if max_messages is not None and len(self.messages) > max_messages:
            del self.messages[:len(self.messages) - max_messages]
        self.updated_at = utc_now()
        return message

    def recent(self, *, max_messages: int, max_chars: int,
               roles: Sequence[Role] | None = None) -> list[Message]:
        """The most recent messages that fit both bounds, oldest-first.

        Two independent limits: a message count and a character budget. Walking
        backwards and reversing keeps the *newest* turns — the ones that make
        the current turn intelligible — rather than truncating the tail.

        System messages are excluded by default: the runtime supplies its own
        system prompt, and letting a stored one ride along would give a past
        turn a way to influence current instructions.
        """
        allowed = set(roles) if roles is not None else {Role.USER,
                                                        Role.ASSISTANT}
        chosen: list[Message] = []
        used = 0
        for message in reversed(self.messages):
            if message.role not in allowed:
                continue
            if len(chosen) >= max_messages:
                break
            cost = len(message.content)
            if chosen and used + cost > max_chars:
                break
            chosen.append(message)
            used += cost
        chosen.reverse()
        return chosen

    @property
    def last_user_message(self) -> Message | None:
        for message in reversed(self.messages):
            if message.role is Role.USER:
                return message
        return None

    def __len__(self) -> int:
        return len(self.messages)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "messages": [m.to_dict() for m in self.messages],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "scope_key": self.scope_key,
            "metadata": dict(self.metadata),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConversationSession:
        """Rebuild a session, validating strictly.

        A malformed message is rejected rather than coerced: a corrupt session
        file must not become a channel for injecting arbitrary roles or
        content into a future prompt.
        """
        if not isinstance(data, Mapping):
            raise ConversationValidationError("session must be a mapping",
                                              got=type(data).__name__)
        if not data.get("id"):
            raise ConversationValidationError("session is missing an id")

        raw_messages = data.get("messages") or []
        if not isinstance(raw_messages, list):
            raise ConversationValidationError("session messages must be a list")
        messages: list[Message] = []
        for entry in raw_messages:
            if not isinstance(entry, Mapping):
                raise ConversationValidationError(
                    "each session message must be a mapping")
            try:
                role = Role(str(entry.get("role")))
            except ValueError as exc:
                raise ConversationValidationError(
                    "unknown message role", value=str(entry.get("role")),
                    allowed=[r.value for r in Role]) from exc
            content = entry.get("content")
            if not isinstance(content, str):
                raise ConversationValidationError(
                    "message content must be a string")
            name = entry.get("name")
            messages.append(Message(role=role, content=content,
                                    name=str(name) if name else None))

        created = _parse_time(data.get("created_at"), "created_at")
        return cls(
            id=str(data["id"]),
            messages=messages,
            created_at=created,
            updated_at=_parse_time(data.get("updated_at"), "updated_at")
            if data.get("updated_at") else created,
            scope_key=data.get("scope_key"),
            metadata=dict(data.get("metadata") or {}),
            schema_version=int(data.get("schema_version",
                                        SESSION_SCHEMA_VERSION) or
                               SESSION_SCHEMA_VERSION),
        )

    def describe(self) -> dict[str, Any]:
        """Log-safe summary: shape only, never message content."""
        return {
            "session_id": self.id,
            "messages": len(self.messages),
            "scope_key": self.scope_key,
            "chars": sum(len(m.content) for m in self.messages),
        }


def _parse_time(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        return utc_now()
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ConversationValidationError(
            f"{field_name} is not a valid timestamp", value=str(value)) from exc


@runtime_checkable
class SessionStore(Protocol):
    """Persistence for conversation sessions. Storage-agnostic."""

    def save(self, session: ConversationSession) -> None: ...

    def load(self, session_id: str) -> ConversationSession: ...

    def exists(self, session_id: str) -> bool: ...

    def list_sessions(self) -> list[str]: ...

    def delete(self, session_id: str) -> None: ...


class InMemorySessionStore(_SessionDomain):
    """Non-durable store, mirroring ``InMemoryStateStore``."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def save(self, session: ConversationSession, *,
             log_session_id: str | None = None) -> None:
        # A serialised copy, so later mutation cannot alter the snapshot.
        self._validate_domain(session.metadata)
        if self._session_domain is not None and self.exists(session.id):
            self.load(session.id)
        self._data[session.id] = session.to_dict()

    def load(self, session_id: str, *, session_domain: str | None = None) -> ConversationSession:
        payload = self._data.get(str(session_id))
        if payload is None:
            raise SessionNotFoundError("no saved conversation session",
                                       session_id=str(session_id))
        self._validate_domain(payload.get("metadata") or {}, requested_domain=session_domain)
        return ConversationSession.from_dict(payload)

    def exists(self, session_id: str) -> bool:
        return str(session_id) in self._data

    def list_sessions(self) -> list[str]:
        return sorted(self._data)

    def delete(self, session_id: str) -> None:
        if self._session_domain is not None:
            self.load(session_id)
        if self._data.pop(str(session_id), None) is None:
            raise SessionNotFoundError("no saved conversation session",
                                       session_id=str(session_id))


class JsonFileSessionStore(_SessionDomain):
    """One JSON file per session, written atomically.

    Deliberately a *separate* store from ``JsonFileStateStore``: conversation
    history and run state have different lifetimes and different meanings, and
    keeping them apart is what stops dialogue from corrupting resume
    semantics.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._log = get_logger("conversation.session")

    def _path(self, session_id: str) -> Path:
        raw = str(session_id)
        if not raw or any(c in raw for c in "/\\" + os.sep):
            raise ConversationValidationError("invalid session id",
                                              session_id=raw)
        return self.directory / f"{raw}.json"

    def save(self, session: ConversationSession, *,
             log_session_id: str | None = None) -> None:
        self._validate_domain(session.metadata)
        path = self._path(session.id)
        if self._session_domain is not None and path.exists():
            self.load(session.id)
        payload = json.dumps(session.to_dict(), indent=2, default=str)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        fields = session.describe()
        if log_session_id is not None:
            fields["session_id"] = log_session_id
        self._log.debug(Events.CONVERSATION_SAVED, **fields)

    def load(self, session_id: str, *, session_domain: str | None = None) -> ConversationSession:
        path = self._path(session_id)
        if not path.exists():
            raise SessionNotFoundError("no saved conversation session",
                                       session_id=str(session_id))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConversationValidationError(
                "saved conversation session is corrupt",
                session_id=str(session_id), reason=str(exc)) from exc
        if isinstance(data, Mapping):
            self._validate_domain(data.get("metadata") or {}, requested_domain=session_domain)
        return ConversationSession.from_dict(data)

    def exists(self, session_id: str) -> bool:
        return self._path(session_id).exists()

    def list_sessions(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def delete(self, session_id: str) -> None:
        if self._session_domain is not None:
            self.load(session_id)
        path = self._path(session_id)
        if not path.exists():
            raise SessionNotFoundError("no saved conversation session",
                                       session_id=str(session_id))
        path.unlink()


def build_session_store(config: Any = None, *,
                        workspace_dir: str | None = None
                        ) -> JsonFileSessionStore:
    """Construct the configured session store.

    Defaults to ``<workspace_dir>/conversations``, mirroring how runs default
    to ``<workspace_dir>/runs`` and memory to ``<workspace_dir>/memory``.
    """
    from agentcore.conversation.config import ConversationConfig

    conversation = getattr(config, "conversation", config)
    if conversation is None or not isinstance(conversation,
                                              ConversationConfig):
        conversation = ConversationConfig()
    root = conversation.directory
    if not root:
        base = workspace_dir or getattr(config, "workspace_dir", None) \
            or "./.agent_workspace"
        root = str(Path(base) / SESSIONS_SUBDIR)
    return JsonFileSessionStore(root)
