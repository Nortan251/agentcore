"""The semantic Gmail read tools approved for ordinary model use."""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.runtime import CredentialRuntime
from agentcore.credentials.vault import CredentialLease
from agentcore.errors import ToolNotFoundError
from agentcore.integrations.gmail.agent_contract import (
    DEFAULT_AGENT_RESULTS, DEFAULT_RECENT_DAYS, MAX_AGENT_RESULTS,
    MAX_RECENT_DAYS, MAX_SEARCH_TEXT_CHARS, MAX_SENDER_CHARS,
    MAX_SUBJECT_TERMS_CHARS, SEMANTIC_TEXT_PATTERN, SENDER_PATTERN,
    project_message, project_search, semantic_search_arguments,
)
from agentcore.integrations.gmail.http import GmailReadHttp, GmailReadTransport
from agentcore.integrations.gmail.limits import ID_PATTERN, MAX_ID_CHARS, reference
from agentcore.permissions.levels import Capability
from agentcore.tools.authenticated import AuthenticatedReadTool
from agentcore.tools.base import (
    ParamType, Tool, ToolParameter, ToolResult, tool_spec,
)
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError


MAX_PROVENANCE_SESSIONS = 128


class _SearchProvenance(RuntimeOnly):
    """Runtime-only, session-bound authority for IDs returned by search."""

    __slots__ = ("_owner_pid", "_active_session", "_lock", "_sessions")

    def __init__(self) -> None:
        self._owner_pid = os.getpid()
        self._active_session: ContextVar[str | None] = ContextVar(
            "gmail_agent_session", default=None)
        self._lock = threading.RLock()
        self._sessions: OrderedDict[
            str, tuple[object, frozenset[str]]
        ] = OrderedDict()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def _session(self) -> str:
        self._owner()
        session_id = self._active_session.get()
        if type(session_id) is not str or not session_id:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        return session_id

    @contextmanager
    def session(self, session_id: str | None) -> Iterator[None]:
        self._owner()
        if type(session_id) is not str or not session_id:
            raise ValueError("Gmail agent reads require a conversation session")
        token = self._active_session.set(session_id)
        try:
            yield
        finally:
            self._active_session.reset(token)

    def begin_search(self) -> tuple[str, object]:
        """Invalidate older results before the provider search begins."""
        session_id = self._session()
        generation = object()
        with self._lock:
            self._owner()
            self._sessions[session_id] = (generation, frozenset())
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > MAX_PROVENANCE_SESSIONS:
                self._sessions.popitem(last=False)
        return session_id, generation

    def publish(self, grant: tuple[str, object], identifiers: list[str]) -> None:
        """Publish only the IDs from the current completed search."""
        session_id = self._session()
        if grant[0] != session_id:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        with self._lock:
            self._owner()
            current = self._sessions.get(session_id)
            if current is None or current[0] is not grant[1]:
                raise CredentialError(CredentialFailure.READ_ARGUMENTS)
            allowed = frozenset(identifiers)
            if allowed:
                self._sessions[session_id] = (grant[1], allowed)
                self._sessions.move_to_end(session_id)
            else:
                self._sessions.pop(session_id, None)

    def consume(self, message_id: str) -> None:
        """Consume one same-session search result before Gmail I/O."""
        session_id = self._session()
        with self._lock:
            self._owner()
            current = self._sessions.get(session_id)
            if current is None or message_id not in current[1]:
                raise CredentialError(CredentialFailure.READ_ARGUMENTS)
            remaining = current[1] - {message_id}
            if remaining:
                self._sessions[session_id] = (current[0], remaining)
                self._sessions.move_to_end(session_id)
            else:
                self._sessions.pop(session_id, None)


class _Provider(RuntimeOnly):
    def __init__(self, transport: GmailReadTransport,
                 provenance: _SearchProvenance) -> None:
        self._owner_pid = os.getpid()
        self._transport = transport
        self._provenance = provenance

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @property
    def provider_id(self) -> str:
        self._owner()
        return "google"

    def _credential(self, credential: CredentialLease) -> None:
        self._owner()
        if type(credential) is not CredentialLease:
            raise CredentialError(CredentialFailure.AUTHORITY)
        credential._check_current()
        if (credential.info.provider != "google"
                or credential.info.scopes != GOOGLE_GMAIL_READONLY_SCOPES):
            raise CredentialError(CredentialFailure.SCOPE_MISMATCH)


class _Search(_Provider):
    def read(self, args: Mapping[str, Any],
             credential: CredentialLease) -> ToolResult:
        self._credential(credential)
        cleaned = semantic_search_arguments(dict(args))
        grant = self._provenance.begin_search()
        raw = self._transport.list_messages(credential, cleaned)
        self._owner()
        projected = project_search(raw, cleaned["max_results"])
        self._provenance.publish(grant, projected["message_ids"])
        return ToolResult.success(projected)


class _Read(_Provider):
    def read(self, args: Mapping[str, Any],
             credential: CredentialLease) -> ToolResult:
        self._credential(credential)
        if type(args) is not dict or set(args) != {"message_id"}:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        message_id = reference(args["message_id"])
        self._provenance.consume(message_id)
        raw = self._transport.read_message(credential, message_id)
        self._owner()
        return ToolResult.success(project_message(raw, message_id))


class _ScopedRegistry(RuntimeOnly):
    """Expose exact Gmail bindings only while their leases are held."""

    __slots__ = ("_active", "_expected", "_owner_pid", "_registry")

    def __init__(self, registry: ToolRegistry,
                 tools: tuple[AuthenticatedReadTool,
                              AuthenticatedReadTool]) -> None:
        self._owner_pid = os.getpid()
        self._registry = registry
        self._expected = {tool.name: tool for tool in tools}
        if len(self._expected) != len(tools):
            raise CredentialError(CredentialFailure.BINDING)
        self._active: ContextVar[bool] = ContextVar(
            "gmail_agent_registry_scope", default=False)

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @contextmanager
    def activate(self) -> Iterator[None]:
        self._owner()
        token = self._active.set(True)
        try:
            yield
        finally:
            self._active.reset(token)

    def _tool(self, name: str) -> Tool:
        self._owner()
        expected = self._expected.get(name) if self._active.get() else None
        if expected is None or self._registry.get(name) is not expected:
            raise ToolNotFoundError(
                "tool is not available in this conversation",
                tool="unregistered",
            )
        return expected

    def get(self, name: str) -> Tool:
        return self._tool(name)

    def names(self) -> list[str]:
        self._owner()
        if not self._active.get():
            return []
        names = sorted(self._expected)
        try:
            for name in names:
                self._tool(name)
        except ToolNotFoundError:
            return []
        return names

    def describe(self) -> list[dict[str, object]]:
        names = self.names()
        if len(names) != len(self._expected):
            return []
        return [self._expected[name].spec.to_dict() for name in names]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            self._tool(name)
        except ToolNotFoundError:
            return False
        return True


class _ConversationRunner(RuntimeOnly):
    """Lease and expose only exact Gmail tools around canonical dispatch."""

    __slots__ = (
        "_owner_pid", "_provenance", "_registry", "_runner", "_scoped",
        "_tools",
    )

    def __init__(self, runner: Any, provenance: _SearchProvenance, *,
                 registry: ToolRegistry, scoped: _ScopedRegistry,
                 tools: tuple[AuthenticatedReadTool,
                              AuthenticatedReadTool]) -> None:
        self._owner_pid = os.getpid()
        self._runner = runner
        self._provenance = provenance
        self._registry = registry
        self._scoped = scoped
        self._tools = tools

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def _acquire_leases(self) -> ExitStack | None:
        """Acquire both bindings or return no authority before any call."""
        self._owner()
        stack = ExitStack()
        try:
            for tool in sorted(self._tools, key=lambda item: item.name):
                stack.enter_context(
                    self._registry.execution_lease(tool.name, tool))
        except ToolRegistryLeaseError:
            stack.close()
            return None
        return stack

    def catalogue(self) -> list[dict[str, Any]]:
        self._owner()
        leases = self._acquire_leases()
        if leases is None:
            return []
        with leases, self._scoped.activate():
            return self._runner.catalogue()

    def available(self) -> bool:
        self._owner()
        return bool(self.catalogue())

    def run_calls(self, calls: Any, *, session_id: str | None = None) -> Any:
        self._owner()
        leases = self._acquire_leases()
        if leases is None:
            # Run the same bounded normalization with the scoped registry
            # inactive. Every proposal is then an ordinary unknown-tool
            # refusal and nothing can reach the full executor registry.
            return self._runner.run_calls(calls, session_id=session_id)
        with leases, self._scoped.activate(), self._provenance.session(
                session_id):
            return self._runner.run_calls(calls, session_id=session_id)


class GmailAgentReadTools(RuntimeOnly):
    """Exactly two semantic read tools for trusted Agent composition."""

    __slots__ = ("_owner_pid", "_provenance", "_providers", "_tools")

    def __init__(self, *, transport: GmailReadTransport | None = None) -> None:
        self._owner_pid = os.getpid()
        transport = transport if transport is not None else GmailReadHttp()
        self._provenance = _SearchProvenance()
        self._providers = (
            _Search(transport, self._provenance),
            _Read(transport, self._provenance),
        )
        capabilities = (Capability.NET_READ, Capability.CREDENTIAL_ACCESS)
        self._tools = (
            AuthenticatedReadTool(tool_spec(
                "gmail_search_messages",
                "Search recent mail in the bound Gmail mailbox using bounded semantic filters. Returns message ids only; they replace this session's pending one-use read ids.",
                parameters=(
                    ToolParameter(
                        "recent_days", ParamType.INTEGER,
                        "Search only the latest 1 to 30 days; default 7.",
                        required=False, default=DEFAULT_RECENT_DAYS,
                        minimum=1, maximum=MAX_RECENT_DAYS),
                    ToolParameter(
                        "sender", ParamType.STRING,
                        "Optional literal sender name or address text; provider query syntax is not accepted.",
                        required=False, max_length=MAX_SENDER_CHARS,
                        pattern=SENDER_PATTERN),
                    ToolParameter(
                        "subject_terms", ParamType.STRING,
                        "Optional literal subject words; provider query syntax is not accepted.",
                        required=False, max_length=MAX_SUBJECT_TERMS_CHARS,
                        pattern=SEMANTIC_TEXT_PATTERN),
                    ToolParameter(
                        "search_text", ParamType.STRING,
                        "Optional literal message words; provider query syntax is not accepted.",
                        required=False, max_length=MAX_SEARCH_TEXT_CHARS,
                        pattern=SEMANTIC_TEXT_PATTERN),
                    ToolParameter(
                        "max_results", ParamType.INTEGER,
                        "Maximum message ids: 1 to 10, default 5.",
                        required=False, default=DEFAULT_AGENT_RESULTS,
                        minimum=1, maximum=MAX_AGENT_RESULTS),
                ), capabilities=capabilities,
                tags=("gmail", "gmail-agent", "authenticated-read"))),
            AuthenticatedReadTool(tool_spec(
                "gmail_read_message",
                "Read one selected Gmail message as bounded untrusted text. The id must be unused from this session's latest successful search. Attachment content and metadata are excluded.",
                parameters=(ToolParameter(
                    "message_id", ParamType.STRING,
                    "One unused message id from the latest successful Gmail search in this conversation session: 1 to 128 ASCII letters, digits, underscore or hyphen.",
                    max_length=MAX_ID_CHARS, pattern=ID_PATTERN),),
                capabilities=capabilities,
                tags=("gmail", "gmail-agent", "authenticated-read"))),
        )

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @property
    def tools(self) -> tuple[AuthenticatedReadTool, AuthenticatedReadTool]:
        self._owner()
        return self._tools

    def conversation_runner(self, runner: Any) -> Any:
        """Scope exact Gmail bindings around one canonical conversation."""
        self._owner()
        from agentcore.conversation.tool_use import ConversationToolRunner

        if (type(runner) is not ConversationToolRunner
                or type(runner.registry) is not ToolRegistry
                or getattr(runner.executor, "registry", None)
                is not runner.registry):
            raise CredentialError(CredentialFailure.BINDING)
        scoped = _ScopedRegistry(runner.registry, self._tools)
        bounded = ConversationToolRunner(
            runner.executor,
            scoped,
            max_result_chars=runner.max_result_chars,
            max_calls_per_round=runner.max_calls_per_round,
            logger=runner._log,
        )
        return _ConversationRunner(
            bounded,
            self._provenance,
            registry=runner.registry,
            scoped=scoped,
            tools=self._tools,
        )

    def bind(self, runtime: CredentialRuntime, reference: str) -> None:
        """Bind exact host-selected authority; never refresh or register."""
        self._owner()
        for tool, provider in zip(self._tools, self._providers):
            runtime.bind(
                tool, reference=reference, provider=provider,
                required_scopes=GOOGLE_GMAIL_READONLY_SCOPES)
