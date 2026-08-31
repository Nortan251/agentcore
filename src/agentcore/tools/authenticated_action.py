"""Executor-permitted authenticated email actions; no provider in the tool."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.runtime import reserved_credential_key
from agentcore.errors import ToolValidationError
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolContext, ToolResult, ToolSpec


class AuthenticatedActionTool(Tool):
    """Final composition type: only the reviewed email mutation capabilities.

    A host normalizer is pure/local. The provider and credential binding live
    only in CredentialRuntime, and no default registry installs this tool.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("authenticated action tools use composition, not inheritance")

    def __init__(self, spec: ToolSpec, normalizer: Callable[..., dict[str, Any]], *,
                 preparation_scope: str) -> None:
        self._spec, self._normalizer = spec, normalizer
        self._preparation_scope = preparation_scope
        self.validate_contract()

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_contract(self) -> None:
        if type(self.spec) is not ToolSpec or self.spec.read_only is not False or (
            self.spec.capabilities != frozenset({Capability.EMAIL_SEND, Capability.NET_WRITE,
                                                Capability.CREDENTIAL_ACCESS})
            or not callable(self._normalizer)
            or type(self._preparation_scope) is not str or not self._preparation_scope
            or len(self._preparation_scope) > 128
            or any(reserved_credential_key(parameter.name) for parameter in self.spec.parameters)
        ):
            raise CredentialError(CredentialFailure.UNSUPPORTED)

    def validate_action_context(self, context, *, repeat: bool) -> None:
        self.validate_contract()
        if context.intent_preparation_id is None:
            raise CredentialError(CredentialFailure.BINDING)
        if not repeat and (context.deduplication_scope != self._preparation_scope
                           or context.deduplication_key != context.intent_preparation_id):
            raise CredentialError(CredentialFailure.BINDING)

    def validate_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_contract()
        cleaned = super().validate_args(args)
        try:
            return self._normalizer(cleaned)
        except Exception:
            pass
        raise ToolValidationError("Authenticated action arguments were invalid.")

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        from agentcore.credentials.action import CredentialActionExecution

        permit = context.credential_execution
        if type(permit) is not CredentialActionExecution:
            raise CredentialError(CredentialFailure.AUTHORITY)
        return permit.invoke(self, args)
