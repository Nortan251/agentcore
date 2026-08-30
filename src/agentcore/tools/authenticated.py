"""A pure-read tool whose credential use exists only within ToolExecutor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.runtime import (
    CredentialExecution,
    detached_public_json,
    reserved_credential_key,
)
from agentcore.errors import ToolValidationError
from agentcore.permissions.levels import READ_ONLY_CAPABILITIES, Capability
from agentcore.tools.base import Tool, ToolContext, ToolResult, ToolSpec


class AuthenticatedReadTool(Tool):
    """Static model-visible schema; provider/account binding stays out of band.

    Use composition, not subclasses: the run boundary must always consume the
    executor permit. Provider/account changes need explicit host binding.
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("authenticated tools use composition, not inheritance")

    def __init__(self, spec: ToolSpec) -> None:
        self._spec = spec
        self.validate_contract()

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_contract(self) -> None:
        spec = self.spec
        if type(spec) is not ToolSpec or spec.read_only is not True or (
            Capability.NET_READ not in spec.capabilities
            or not spec.capabilities <= READ_ONLY_CAPABILITIES
        ):
            raise CredentialError(CredentialFailure.UNSUPPORTED)
        if any(reserved_credential_key(parameter.name) for parameter in spec.parameters):
            raise CredentialError(CredentialFailure.INVALID)

    def validate_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_contract()
        cleaned = super().validate_args(args)
        try:
            return detached_public_json(cleaned)
        except Exception:
            pass
        raise ToolValidationError("Authenticated tool arguments were invalid.")

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        permit = context.credential_execution
        if type(permit) is not CredentialExecution:
            raise CredentialError(CredentialFailure.AUTHORITY)
        return permit.invoke(self, args)
