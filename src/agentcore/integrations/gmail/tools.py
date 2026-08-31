"""Host composition of exactly two authenticated Gmail pure-read tools."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.runtime import CredentialRuntime
from agentcore.credentials.vault import CredentialLease
from agentcore.integrations.gmail.http import GmailReadHttp, GmailReadTransport
from agentcore.integrations.gmail.limits import (
    DEFAULT_RESULTS, ID_PATTERN, MAX_ID_CHARS, MAX_PAGE_TOKEN_CHARS,
    MAX_QUERY_CHARS, MAX_RESULTS, PAGE_TOKEN_PATTERN, QUERY_PATTERN,
    reference, search_arguments,
)
from agentcore.integrations.gmail.message import normalize_message, normalize_search
from agentcore.permissions.levels import Capability
from agentcore.tools.authenticated import AuthenticatedReadTool
from agentcore.tools.base import ParamType, ToolParameter, ToolResult, tool_spec


class _Provider(RuntimeOnly):
    def __init__(self, transport: GmailReadTransport) -> None:
        self._owner_pid = os.getpid()
        self._transport = transport

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
        if credential.info.provider != "google" or credential.info.scopes != GOOGLE_GMAIL_READONLY_SCOPES:
            # Defense against an incorrect trusted host binding. An identity
            # token or broader Gmail token is never an accepted read profile.
            raise CredentialError(CredentialFailure.SCOPE_MISMATCH)


class _Search(_Provider):
    def read(self, args: Mapping[str, Any], credential: CredentialLease) -> ToolResult:
        self._credential(credential)
        cleaned = search_arguments(args)
        raw = self._transport.list_messages(credential, cleaned)
        self._owner()
        return ToolResult.success(normalize_search(raw, cleaned["max_results"]))


class _Read(_Provider):
    def __init__(self, transport: GmailReadTransport, *, allow_message_content: bool) -> None:
        super().__init__(transport)
        self._allow_message_content = allow_message_content

    def read(self, args: Mapping[str, Any], credential: CredentialLease) -> ToolResult:
        self._credential(credential)
        if type(args) is not dict or set(args) != {"message_id"}:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        message_id = reference(args["message_id"])
        # OAuth grants and the generic authenticated-read opt-in do not by
        # themselves choose to disclose private mail to a model/session. Only
        # trusted host composition can enable this output, before any get.
        if not self._allow_message_content:
            raise CredentialError(CredentialFailure.READ_DISCLOSURE)
        raw = self._transport.read_message(credential, message_id)
        self._owner()
        return ToolResult.success(normalize_message(raw, message_id))


class GmailReadonlyTools(RuntimeOnly):
    """Two tool objects and their trusted providers, with no live credentials.

    The host obtains a current generation first, calls bind explicitly, and
    registers tools on its normal registry (or supplies AgentDependencies).
    Reuse this object to rebind after refresh. Reconnect creates a different
    connection and requires a new instance plus explicit registry replacement.
    Message content is withheld unless the host explicitly opts into disclosure
    to the normal model/session path; this never grants executor permission.
    """

    def __init__(self, *, transport: GmailReadTransport | None = None,
                 allow_message_content: bool = False) -> None:
        self._owner_pid = os.getpid()
        if type(allow_message_content) is not bool:
            raise CredentialError(CredentialFailure.INVALID)
        transport = transport if transport is not None else GmailReadHttp()
        self._providers = (_Search(transport), _Read(transport, allow_message_content=allow_message_content))
        capabilities = (Capability.NET_READ, Capability.CREDENTIAL_ACCESS)
        self._tools = (
            AuthenticatedReadTool(tool_spec(
                "gmail_search_messages",
                "Search the bound Gmail mailbox for message references only. One page; no message bodies.",
                parameters=(
                    ToolParameter("q", ParamType.STRING,
                                  "Optional Gmail query, at most 1024 characters; no control characters.",
                                  required=False, max_length=MAX_QUERY_CHARS, pattern=QUERY_PATTERN),
                    ToolParameter("max_results", ParamType.INTEGER,
                                  "Messages in this page: 1 to 50, default 10.",
                                  required=False, default=DEFAULT_RESULTS, minimum=1, maximum=MAX_RESULTS),
                    ToolParameter("page_token", ParamType.STRING,
                                  "Optional opaque next_page_token from a prior result; at most 2048 characters.",
                                  required=False, max_length=MAX_PAGE_TOKEN_CHARS, pattern=PAGE_TOKEN_PATTERN),
                ), capabilities=capabilities, tags=("gmail", "authenticated-read"))),
            AuthenticatedReadTool(tool_spec(
                "gmail_read_message",
                "Read one selected Gmail message as bounded untrusted text. No attachment downloads.",
                parameters=(ToolParameter(
                    "message_id", ParamType.STRING,
                    "One message id from Gmail search: 1 to 128 ASCII letters, digits, underscore or hyphen.",
                    max_length=MAX_ID_CHARS, pattern=ID_PATTERN),),
                capabilities=capabilities, tags=("gmail", "authenticated-read"))),
        )

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @property
    def tools(self) -> tuple[AuthenticatedReadTool, AuthenticatedReadTool]:
        self._owner()
        return self._tools

    def bind(self, runtime: CredentialRuntime, reference: str, *, replace_existing: bool = False) -> None:
        """Explicit host binding; does not refresh, authorize, or register tools.

        Each binding uses the existing per-tool atomic operation. If a rebind
        is busy/fails, no call can silently adopt a new generation; old bindings
        remain stale and the host must explicitly finish/retry composition.
        """
        self._owner()
        for tool, provider in zip(self._tools, self._providers):
            runtime.bind(tool, reference=reference, provider=provider,
                         required_scopes=GOOGLE_GMAIL_READONLY_SCOPES, replace_existing=replace_existing)
