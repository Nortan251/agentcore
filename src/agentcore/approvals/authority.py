"""Trusted host boundary and durable one-shot approval orchestration."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Any, Callable, Protocol

from agentcore.approvals.errors import (
    ApprovalError,
    ApprovalUnavailableError,
    ApprovalValidationError,
)
from agentcore.approvals.models import (
    ApprovalBinding,
    ApprovalReceipt,
    ApprovalRequest,
    ApprovalSemantics,
    ApprovalState,
    HumanApprovalDecision,
    new_approval_request,
    require_approval_action_id,
    require_approval_receipt_id,
    require_approval_request_id,
)
from agentcore.approvals.store import ApprovalStore
from agentcore.permissions.levels import Capability
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


MAX_APPROVAL_TTL = timedelta(days=1)


@dataclass(frozen=True)
class ApprovalPrompt:
    """Safe metadata plus an ephemeral human-facing description."""

    request_id: str
    action_kind: str
    tool_name: str
    tool_version: str
    mutation_capabilities: tuple[str, ...]
    principal_ref: str
    scope_ref: str
    description: str = field(repr=False, compare=False)

    def validate(self) -> ApprovalPrompt:
        require_approval_request_id(self.request_id)
        if not isinstance(self.description, str) or len(self.description) > 512 or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.description):
            raise ApprovalValidationError("approval prompt description is invalid")
        ApprovalBinding(
            action_kind=self.action_kind,
            intent_digest="0" * 64,
            mutation_capabilities=self.mutation_capabilities,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            policy_fingerprint="0" * 64,
            principal_ref=self.principal_ref,
            scope_ref=self.scope_ref,
        ).validate()
        return self


class HumanApprovalGate(Protocol):
    """Host-owned typed boundary. Models and tools never receive this object."""

    def decide(self, prompt: ApprovalPrompt) -> HumanApprovalDecision: ...


class PendingApprovalGate:
    """Production-safe default when no interactive human boundary is attached."""

    def decide(self, prompt: ApprovalPrompt) -> HumanApprovalDecision:
        prompt.validate()
        return HumanApprovalDecision.PENDING


class LegacyApprovalGateAdapter:
    """Feed the synchronous v0 gate into durable exact-set approval."""

    def __init__(self, gate: Any) -> None:
        if not callable(getattr(gate, "request", None)):
            raise ApprovalValidationError("legacy approval gate is invalid")
        self._gate = gate

    def decide(self, prompt: ApprovalPrompt) -> HumanApprovalDecision:
        prompt.validate()
        for value in prompt.mutation_capabilities:
            try:
                capability = Capability(value)
            except ValueError as exc:
                raise ApprovalValidationError(
                    "approval capability is not registered") from exc
            result = self._gate.request(capability, prompt.description)
            if not isinstance(result, bool):
                raise ApprovalValidationError(
                    "legacy approval gate returned a non-boolean decision")
            if not result:
                return HumanApprovalDecision.DENY
        return HumanApprovalDecision.APPROVE


@dataclass(frozen=True)
class ApprovalInvocationContext:
    """Opaque request continuity supplied only by trusted host code."""

    request_id: str

    def validate(self) -> ApprovalInvocationContext:
        require_approval_request_id(self.request_id)
        return self


class ApprovalAuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    PENDING = "pending"
    DENIED = "denied"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass(frozen=True)
class ApprovalAuthorization:
    request_id: str
    action_id: str
    status: ApprovalAuthorizationStatus
    receipt: ApprovalReceipt | None = None

    @property
    def authorized(self) -> bool:
        return self.status is ApprovalAuthorizationStatus.AUTHORIZED

    def validate(self) -> ApprovalAuthorization:
        require_approval_request_id(self.request_id)
        require_approval_action_id(self.action_id)
        if not isinstance(self.status, ApprovalAuthorizationStatus):
            raise ApprovalValidationError("approval authorization status is invalid")
        if self.authorized:
            if not isinstance(self.receipt, ApprovalReceipt):
                raise ApprovalValidationError(
                    "authorized approval lacks a receipt")
            self.receipt.validate()
            if self.receipt.request_id != self.request_id or (
                    self.receipt.action_id != self.action_id):
                raise ApprovalValidationError(
                    "approval authorization receipt is mismatched")
        elif self.receipt is not None:
            raise ApprovalValidationError(
                "non-authorized approval exposes a receipt")
        return self


class DurableApprovalAuthority:
    """Create, decide, validate, and atomically consume exact approvals."""

    def __init__(
        self,
        store: ApprovalStore,
        key_provider: Any,
        *,
        principal_ref: str,
        scope_ref: str,
        gate: HumanApprovalGate | None = None,
        ttl: timedelta = timedelta(minutes=5),
        clock: Clock | None = None,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        if not isinstance(store, ApprovalStore):
            raise ApprovalValidationError("approval store is invalid")
        try:
            material = key_provider.load().validate()
            key = material.key
        except ApprovalError:
            raise
        except Exception as exc:
            raise ApprovalUnavailableError(
                "installation approval key is unavailable") from exc
        if not isinstance(key, bytes) or len(key) != 32:
            raise ApprovalUnavailableError(
                "installation approval key is invalid")
        if not isinstance(ttl, timedelta) or not (
                timedelta(0) < ttl <= MAX_APPROVAL_TTL):
            raise ApprovalValidationError("approval TTL is invalid")
        self._store = store
        self._key = bytes(key)
        self._gate = gate or PendingApprovalGate()
        if not callable(getattr(self._gate, "decide", None)):
            raise ApprovalValidationError("human approval gate is invalid")
        self._ttl = ttl
        self._clock = clock or SystemClock()
        self._id_factory = id_factory
        self._owner_pid = os.getpid()
        self._principal_ref = principal_ref
        self._scope_ref = scope_ref
        ApprovalBinding(
            action_kind="authority.validation.v1",
            intent_digest="0" * 64,
            mutation_capabilities=("authority.validate",),
            tool_name="authority",
            tool_version="1",
            policy_fingerprint="0" * 64,
            principal_ref=principal_ref,
            scope_ref=scope_ref,
        ).validate()

    @property
    def principal_ref(self) -> str:
        return self._principal_ref

    @property
    def scope_ref(self) -> str:
        return self._scope_ref

    def _require_owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise ApprovalUnavailableError(
                "approval authority belongs to another process")

    def binding(
        self,
        *,
        action_kind: str,
        intent_digest: str,
        mutation_capabilities: tuple[str, ...],
        tool_name: str,
        tool_version: str,
        policy_fingerprint: str,
    ) -> ApprovalBinding:
        self._require_owner()
        return ApprovalBinding(
            action_kind=action_kind,
            intent_digest=intent_digest,
            mutation_capabilities=mutation_capabilities,
            tool_name=tool_name,
            tool_version=tool_version,
            policy_fingerprint=policy_fingerprint,
            principal_ref=self._principal_ref,
            scope_ref=self._scope_ref,
            semantics=ApprovalSemantics.ONE_SHOT,
        ).validate()

    def authorize(
        self,
        binding: ApprovalBinding,
        *,
        description: str,
        action_id: str | None = None,
        request_id: str | None = None,
    ) -> ApprovalAuthorization:
        """Resolve and consume once; human waiting occurs outside store locks."""
        self._require_owner()
        binding.validate()
        if binding.principal_ref != self._principal_ref or (
                binding.scope_ref != self._scope_ref):
            raise ApprovalUnavailableError(
                "approval binding belongs to another authority")
        if request_id is None:
            if action_id is None:
                raise ApprovalValidationError(
                    "new approval requires a trusted action id")
            current = self._create_request(binding, action_id=action_id)
        else:
            current = self._load_exact(request_id, binding)
            if action_id is not None and current.action_id != (
                    require_approval_action_id(action_id)):
                raise ApprovalUnavailableError(
                    "approval request belongs to another action")

        current = self._expire_if_needed(current)
        if current.state is ApprovalState.PENDING:
            prompt = ApprovalPrompt(
                request_id=current.request_id,
                action_kind=current.action_kind,
                tool_name=current.tool_name,
                tool_version=current.tool_version,
                mutation_capabilities=current.mutation_capabilities,
                principal_ref=current.principal_ref,
                scope_ref=current.scope_ref,
                description=description,
            ).validate()
            # Deliberately no store mutex is held across the human boundary.
            decision = self._gate.decide(prompt)
            if not isinstance(decision, HumanApprovalDecision):
                raise ApprovalUnavailableError(
                    "human approval gate returned an invalid decision")
            if decision is not HumanApprovalDecision.PENDING:
                current = self.record_decision(current.request_id, decision)
                current = self._expire_if_needed(current)

        if current.state is ApprovalState.APPROVED:
            return self._consume(current)
        statuses = {
            ApprovalState.PENDING: ApprovalAuthorizationStatus.PENDING,
            ApprovalState.DENIED: ApprovalAuthorizationStatus.DENIED,
            ApprovalState.EXPIRED: ApprovalAuthorizationStatus.EXPIRED,
            ApprovalState.CONSUMED: ApprovalAuthorizationStatus.CONSUMED,
        }
        status = statuses.get(current.state)
        if status is None:
            raise ApprovalUnavailableError("approval state is invalid")
        return ApprovalAuthorization(
            current.request_id, current.action_id, status).validate()

    def record_decision(
        self,
        request_id: str,
        decision: HumanApprovalDecision,
    ) -> ApprovalRequest:
        """Host-only terminal decision entry; history is never rewritten."""
        self._require_owner()
        request_id = require_approval_request_id(request_id)
        if not isinstance(decision, HumanApprovalDecision) or (
                decision is HumanApprovalDecision.PENDING):
            raise ApprovalValidationError(
                "a terminal human approval decision is required")
        current = self._store.load(request_id)
        current.validate()
        if current.principal_ref != self._principal_ref or (
                current.scope_ref != self._scope_ref):
            raise ApprovalUnavailableError(
                "approval request belongs to another authority")
        current = self._expire_if_needed(current)
        if current.state is not ApprovalState.PENDING:
            return current
        now = self._clock.now()
        if decision is HumanApprovalDecision.DENY:
            candidate = current.deny(at=now)
        else:
            receipt = ApprovalReceipt(
                receipt_id=require_approval_receipt_id(
                    self._id_factory("aprc")),
                request_id=current.request_id,
                action_id=current.action_id,
                binding_digest=current.binding_digest,
                policy_fingerprint=current.policy_fingerprint,
                principal_ref=current.principal_ref,
                scope_ref=current.scope_ref,
                semantics=current.semantics,
                approved_at=now,
                expires_at=current.expires_at,
            ).validate()
            candidate = current.approve(receipt=receipt, at=now)
        return self._publish_or_observe(current, candidate)

    def _create_request(self, binding: ApprovalBinding, *,
                        action_id: str) -> ApprovalRequest:
        action_id = require_approval_action_id(action_id)
        now = self._clock.now()
        request_id = require_approval_request_id(self._id_factory("apr"))
        expires_at = now + self._ttl
        digest = approval_binding_digest(
            self._key,
            request_id=request_id,
            action_id=action_id,
            binding=binding,
            expires_at=expires_at.isoformat(),
        )
        request = new_approval_request(
            request_id=request_id,
            action_id=action_id,
            binding_digest=digest,
            binding=binding,
            created_at=now,
            expires_at=expires_at,
        )
        created = self._store.create(request)
        if created != request:
            raise ApprovalUnavailableError(
                "approval request creation was not exact")
        return created

    def _load_exact(self, request_id: str,
                    binding: ApprovalBinding) -> ApprovalRequest:
        current = self._store.load(require_approval_request_id(request_id))
        current.validate()
        expected = approval_binding_digest(
            self._key,
            request_id=current.request_id,
            action_id=current.action_id,
            binding=binding,
            expires_at=current.expires_at.isoformat(),
        )
        if current.as_binding() != binding or not hmac.compare_digest(
                current.binding_digest, expected):
            raise ApprovalUnavailableError(
                "approval request binding does not match the action")
        return current

    def _expire_if_needed(self, current: ApprovalRequest) -> ApprovalRequest:
        if current.state not in {ApprovalState.PENDING, ApprovalState.APPROVED} or (
                self._clock.now() < current.expires_at):
            return current
        return self._publish_or_observe(current, current.expire())

    def _consume(self, current: ApprovalRequest) -> ApprovalAuthorization:
        assert current.receipt is not None
        try:
            consumed = self._store.consume(
                current.request_id,
                current.version,
                action_id=current.action_id,
                binding_digest=current.binding_digest,
                consumed_at=self._clock.now(),
            )
        except Exception:
            observed = self._store.load(current.request_id)
            if observed.state is not ApprovalState.CONSUMED:
                raise
            return ApprovalAuthorization(
                observed.request_id,
                observed.action_id,
                ApprovalAuthorizationStatus.CONSUMED,
            ).validate()
        if consumed.state is not ApprovalState.CONSUMED or (
                consumed.receipt != current.receipt):
            raise ApprovalUnavailableError(
                "approval consumption was not exact")
        return ApprovalAuthorization(
            consumed.request_id,
            consumed.action_id,
            ApprovalAuthorizationStatus.AUTHORIZED,
            receipt=consumed.receipt,
        ).validate()

    def _publish_or_observe(
        self,
        current: ApprovalRequest,
        candidate: ApprovalRequest,
    ) -> ApprovalRequest:
        try:
            self._store.compare_and_swap(
                current.request_id, current.version, candidate)
        except Exception:
            pass
        observed = self._store.load(current.request_id)
        observed.validate()
        if observed == candidate:
            return observed
        if observed.version > current.version:
            return observed
        raise ApprovalUnavailableError(
            "approval decision publication is uncertain")


def approval_binding_digest(
    key: bytes,
    *,
    request_id: str,
    action_id: str,
    binding: ApprovalBinding,
    expires_at: str,
) -> str:
    """Keyed, deterministic, domain-separated exact approval binding."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise ApprovalValidationError("approval binding key is invalid")
    binding.validate()
    require_approval_request_id(request_id)
    require_approval_action_id(action_id)
    payload = {
        "domain": "agentcore.durable-approval.binding.v1",
        "request_id": request_id,
        "action_id": action_id,
        "action_kind": binding.action_kind,
        "intent_digest": binding.intent_digest,
        "mutation_capabilities": list(binding.mutation_capabilities),
        "tool_name": binding.tool_name,
        "tool_version": binding.tool_version,
        "policy_fingerprint": binding.policy_fingerprint,
        "principal_ref": binding.principal_ref,
        "scope_ref": binding.scope_ref,
        "semantics": binding.semantics.value,
        "expires_at": expires_at,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")
    return hmac.new(
        key,
        b"agentcore.durable-approval.binding.v1\x00" + encoded,
        hashlib.sha256,
    ).hexdigest()


__all__ = [
    "MAX_APPROVAL_TTL",
    "ApprovalAuthorization",
    "ApprovalAuthorizationStatus",
    "ApprovalInvocationContext",
    "ApprovalPrompt",
    "DurableApprovalAuthority",
    "HumanApprovalGate",
    "LegacyApprovalGateAdapter",
    "PendingApprovalGate",
    "approval_binding_digest",
]
