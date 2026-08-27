"""Trusted external-idempotency boundary for durable actions.

Providers are registered by trusted composition code against one exact tool
object.  Model-visible arguments never carry the external key or select its
contract.  The executor mints the immutable permit immediately before the
physical effect seam and the provider must use that permit for the call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agentcore.action_journal.models import (
    ActionEffectClass,
    IdempotencyContract,
    require_action_id,
)


_ACTION_ATTEMPT_ID = re.compile(r"^aat_[0-9a-f]{12}$")


@dataclass(frozen=True)
class ExternalIdempotencyRequest:
    """Public, bounded identity supplied to a trusted provider."""

    action_id: str
    action_kind: str
    tool_name: str
    tool_version: str
    effect_class: ActionEffectClass

    def validate(self) -> ExternalIdempotencyRequest:
        require_action_id(self.action_id)
        if self.effect_class is not ActionEffectClass.EXTERNALLY_IDEMPOTENT:
            raise ValueError("external idempotency request has wrong effect class")
        if not all(isinstance(value, str) and value for value in (
            self.action_kind, self.tool_name, self.tool_version,
        )):
            raise ValueError("external idempotency request identity is invalid")
        return self


@dataclass(frozen=True)
class ActionExecutionPermit:
    """Executor-owned capability for one exact physical attempt."""

    action_id: str
    attempt_id: str
    tool_name: str
    tool_version: str
    idempotency: IdempotencyContract

    def validate(self) -> ActionExecutionPermit:
        require_action_id(self.action_id)
        if not isinstance(self.attempt_id, str) or not (
                _ACTION_ATTEMPT_ID.fullmatch(self.attempt_id)):
            raise ValueError("execution permit attempt identity is invalid")
        if not all(isinstance(value, str) and value for value in (
            self.tool_name, self.tool_version,
        )):
            raise ValueError("execution permit tool identity is invalid")
        if not isinstance(self.idempotency, IdempotencyContract):
            raise TypeError("execution permit idempotency contract is invalid")
        self.idempotency.validate()
        return self


class ExternalIdempotencyProvider(Protocol):
    """Trusted owner of key creation, guarantee checks, and physical use."""

    allows_indefinite_retention: bool

    def establish_contract(
        self,
        request: ExternalIdempotencyRequest,
    ) -> IdempotencyContract: ...

    def validate_contract(
        self,
        request: ExternalIdempotencyRequest,
        stored_contract: IdempotencyContract,
    ) -> IdempotencyContract: ...

    def execute(
        self,
        tool: Any,
        cleaned: Mapping[str, Any],
        context: Any,
        permit: ActionExecutionPermit,
    ) -> Any: ...
