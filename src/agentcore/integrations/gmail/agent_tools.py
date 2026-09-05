"""The semantic Gmail read tools approved for ordinary model use."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.runtime import CredentialRuntime
from agentcore.credentials.vault import CredentialLease
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
        if (credential.info.provider != "google"
                or credential.info.scopes != GOOGLE_GMAIL_READONLY_SCOPES):
            raise CredentialError(CredentialFailure.SCOPE_MISMATCH)


class _Search(_Provider):
    def read(self, args: Mapping[str, Any],
             credential: CredentialLease) -> ToolResult:
        self._credential(credential)
        cleaned = semantic_search_arguments(dict(args))
        raw = self._transport.list_messages(credential, cleaned)
        self._owner()
        return ToolResult.success(project_search(raw, cleaned["max_results"]))


class _Read(_Provider):
    def read(self, args: Mapping[str, Any],
             credential: CredentialLease) -> ToolResult:
        self._credential(credential)
        if type(args) is not dict or set(args) != {"message_id"}:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        message_id = reference(args["message_id"])
        raw = self._transport.read_message(credential, message_id)
        self._owner()
        return ToolResult.success(project_message(raw, message_id))


class GmailAgentReadTools(RuntimeOnly):
    """Exactly two semantic read tools for trusted Agent composition."""

    def __init__(self, *, transport: GmailReadTransport | None = None) -> None:
        self._owner_pid = os.getpid()
        transport = transport if transport is not None else GmailReadHttp()
        self._providers = (_Search(transport), _Read(transport))
        capabilities = (Capability.NET_READ, Capability.CREDENTIAL_ACCESS)
        self._tools = (
            AuthenticatedReadTool(tool_spec(
                "gmail_search_messages",
                "Search recent mail in the bound Gmail mailbox using bounded semantic filters. Returns message ids only.",
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
                "Read one selected Gmail message as bounded untrusted text. Attachment content and metadata are excluded.",
                parameters=(ToolParameter(
                    "message_id", ParamType.STRING,
                    "One message id returned by Gmail search: 1 to 128 ASCII letters, digits, underscore or hyphen.",
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

    def bind(self, runtime: CredentialRuntime, reference: str) -> None:
        """Bind exact host-selected authority; never refresh or register."""
        self._owner()
        for tool, provider in zip(self._tools, self._providers):
            runtime.bind(
                tool, reference=reference, provider=provider,
                required_scopes=GOOGLE_GMAIL_READONLY_SCOPES)
