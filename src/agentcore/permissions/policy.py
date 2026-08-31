"""Permission policy: the single decision point for "may this action run?".

The policy answers with a :class:`Decision` and never performs the action
itself. Executors are responsible for honouring the decision — that separation
keeps the rule set testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Protocol

from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import (
    DEFAULT_BLOCKED_CAPABILITIES,
    DEFAULT_REQUIREMENTS,
    READ_ONLY_CAPABILITIES,
    Capability,
    PermissionLevel,
)


class Outcome(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    capability: Capability | None = None
    required_level: PermissionLevel | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "capability": str(self.capability) if self.capability else None,
            "required_level": (str(self.required_level)
                               if self.required_level else None),
        }


@dataclass(frozen=True)
class CapabilityEvaluation:
    """Aggregate current policy truth before any approval is consulted."""

    outcome: Outcome
    capabilities: tuple[Capability, ...]
    approval_capabilities: tuple[Capability, ...] = ()
    denied: Decision | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW


class ApprovalGate(Protocol):
    """Where a CONFIRM-level approval request is routed.

    The foundation ships a deny-by-default implementation; a UI or CLI prompt
    can be substituted later without touching the policy.
    """

    def request(self, capability: Capability, description: str) -> bool: ...


class DenyAllApprovalGate:
    """Default gate: no human is attached, so nothing is approved."""

    def request(self, capability: Capability, description: str) -> bool:
        return False


class AlwaysApproveGate:
    """For tests and trusted headless runs only. Never wire this to production."""

    def request(self, capability: Capability, description: str) -> bool:
        return True


class PermissionPolicy:
    """Evaluates requests against a detached, immutable permission snapshot.

    Change authority by replacing the executor's policy, not by mutating a
    published policy/configuration. Custom policies must honor that contract.
    The authenticated-read opt-in is trusted composition only, never loaded
    from model arguments, generic state, approvals, or PermissionConfig.
    """

    def __init__(
        self,
        config: PermissionConfig | None = None,
        *,
        approval_gate: ApprovalGate | None = None,
        allow_authenticated_reads: bool = False,
        allow_email_send: bool = False,
        allow_authenticated_reconciliation: bool = False,
    ) -> None:
        if type(allow_authenticated_reads) is not bool:
            raise TypeError("authenticated-read authorization must be a bool")
        if type(allow_email_send) is not bool or type(allow_authenticated_reconciliation) is not bool:
            raise TypeError("authenticated-action authorization must be a bool")
        source = config or PermissionConfig()
        self._config = replace(
            source,
            capability_overrides=MappingProxyType(dict(source.capability_overrides)),
            blocked_capabilities=tuple(source.blocked_capabilities),
        )
        self._allow_authenticated_reads = allow_authenticated_reads
        self._allow_email_send = allow_email_send
        self._allow_authenticated_reconciliation = allow_authenticated_reconciliation
        self.approval_gate = approval_gate or DenyAllApprovalGate()

    @property
    def config(self) -> PermissionConfig:
        return self._config

    @property
    def allow_authenticated_reads(self) -> bool:
        return self._allow_authenticated_reads

    @property
    def allow_email_send(self) -> bool:
        return self._allow_email_send

    @property
    def allow_authenticated_reconciliation(self) -> bool:
        return self._allow_authenticated_reconciliation

    def evaluate_authenticated_send(self, capabilities: Iterable[Capability]) -> Decision:
        """Executor context for an exact journaled authenticated send tool.

        This never consults a boolean gate and never returns ALLOW. The
        executor must obtain and revalidate durable one-shot approval.
        Ordinary evaluate/check remain default-denied for EMAIL_SEND.
        """
        requested = frozenset(capabilities)
        required = frozenset({Capability.EMAIL_SEND, Capability.NET_WRITE,
                              Capability.CREDENTIAL_ACCESS})
        if requested != required or not self.allow_email_send:
            return Decision(Outcome.DENY, "bound email sends are blocked by policy",
                            capability=Capability.EMAIL_SEND)
        for capability in required:
            if str(capability) in self.config.blocked_capabilities:
                return Decision(Outcome.DENY, "send capability is explicitly blocked",
                                capability=capability)
        for capability in (Capability.EMAIL_SEND, Capability.CREDENTIAL_ACCESS):
            minimum = max(PermissionLevel.CONFIRM, self.config.capability_overrides.get(
                str(capability), PermissionLevel.CONFIRM))
            if self.level < minimum:
                return Decision(Outcome.DENY, "bound email send level is not permitted",
                                capability=capability, required_level=minimum)
        network = self.evaluate(Capability.NET_WRITE)
        if network.outcome is Outcome.DENY:
            return network
        return Decision(Outcome.REQUIRE_APPROVAL, "durable email approval is required",
                        capability=Capability.EMAIL_SEND, required_level=PermissionLevel.CONFIRM)

    def check_authenticated_reconciliation(self, capabilities: Iterable[Capability]) -> Decision:
        """Read-only, exact registered action-probe context; never a resend."""
        required = frozenset({Capability.NET_READ, Capability.CREDENTIAL_ACCESS})
        if frozenset(capabilities) != required or not self.allow_authenticated_reconciliation:
            return Decision(Outcome.DENY, "bound reconciliation is blocked by policy",
                            capability=Capability.CREDENTIAL_ACCESS)
        credential = Capability.CREDENTIAL_ACCESS
        minimum = self.config.capability_overrides.get(str(credential), PermissionLevel.OBSERVE)
        if str(credential) in self.config.blocked_capabilities or self.level < minimum:
            return Decision(Outcome.DENY, "reconciliation credential use is blocked",
                            capability=credential)
        return self.check((Capability.NET_READ,), description="Read-only action reconciliation")

    @property
    def level(self) -> PermissionLevel:
        return self.config.level

    def _blocked(self, capability: Capability) -> bool:
        if capability in DEFAULT_BLOCKED_CAPABILITIES:
            return True
        return str(capability) in self.config.blocked_capabilities

    def required_level(self, capability: Capability) -> PermissionLevel:
        override = self.config.capability_overrides.get(str(capability))
        if override is not None:
            return override
        if capability in READ_ONLY_CAPABILITIES:
            return PermissionLevel.OBSERVE
        return DEFAULT_REQUIREMENTS.get(capability, PermissionLevel.AUTONOMOUS)

    def evaluate(self, capability: Capability) -> Decision:
        """Decide a single capability without consulting the approval gate."""
        if self._blocked(capability):
            return Decision(Outcome.DENY,
                            f"capability '{capability}' is blocked by policy",
                            capability=capability)

        required = self.required_level(capability)
        if self.level >= required:
            return Decision(Outcome.ALLOW, "permitted at current level",
                            capability=capability, required_level=required)

        if capability in READ_ONLY_CAPABILITIES:
            return Decision(Outcome.ALLOW, "read-only capability",
                            capability=capability, required_level=required)

        if self.level is PermissionLevel.CONFIRM:
            return Decision(Outcome.REQUIRE_APPROVAL,
                            f"'{capability}' needs explicit approval",
                            capability=capability, required_level=required)

        return Decision(
            Outcome.DENY,
            f"'{capability}' requires level '{required}' "
            f"but agent runs at '{self.level}'",
            capability=capability,
            required_level=required,
        )

    def check(
        self,
        capabilities: Iterable[Capability],
        *,
        description: str = "",
        authenticated_read: bool = False,
    ) -> Decision:
        """Evaluate every capability; the most restrictive outcome wins.

        REQUIRE_APPROVAL is resolved through the approval gate, turning into
        ALLOW or DENY so callers only ever see a terminal answer.

        ``authenticated_read`` is an executor-supplied classification for an
        exact AuthenticatedReadTool with a live host binding. It is not a grant:
        the separate host opt-in and all current capability gates still apply.
        Generic CREDENTIAL_ACCESS, raw secret export/management, mutations and
        reconciliation cannot use this context.
        """
        if type(authenticated_read) is not bool:
            raise TypeError("authenticated-read classification must be a bool")
        if authenticated_read:
            capabilities = tuple(capabilities)
            credential = self._authenticated_read_decision(capabilities)
            if not credential.allowed:
                return credential
            capabilities = tuple(cap for cap in capabilities
                                 if cap is not Capability.CREDENTIAL_ACCESS)
        evaluation = self.evaluate_all(capabilities)
        if not evaluation.capabilities:
            return Decision(Outcome.ALLOW, "no capabilities requested")
        if evaluation.outcome is Outcome.DENY:
            assert evaluation.denied is not None
            return evaluation.denied
        if evaluation.outcome is Outcome.ALLOW:
            return Decision(Outcome.ALLOW, "all capabilities permitted")

        pending = evaluation.approval_capabilities
        request_many = getattr(self.approval_gate, "request_many", None)
        if callable(request_many):
            approved = request_many(pending, description)
            if not isinstance(approved, bool):
                approved = False
        else:
            approved = all(
                self.approval_gate.request(capability, description)
                for capability in pending
            )
        cap = pending[0]
        required = self.required_level(cap)
        if approved:
            return Decision(Outcome.ALLOW, "approved by gate", capability=cap,
                            required_level=required)
        return Decision(Outcome.DENY, "approval was not granted", capability=cap,
                        required_level=required)

    def _authenticated_read_decision(
        self, capabilities: tuple[Capability, ...],
    ) -> Decision:
        if any(not isinstance(cap, Capability) for cap in capabilities):
            raise TypeError("capabilities must contain Capability values")
        requested = frozenset(capabilities)
        credential = Capability.CREDENTIAL_ACCESS
        if not {Capability.NET_READ, credential} <= requested or not (
            requested - {credential} <= READ_ONLY_CAPABILITIES
        ):
            return Decision(Outcome.DENY, "authenticated read capability contract is invalid",
                            capability=credential)
        # The host opt-in is narrower than unblocking the capability. Explicit
        # blocks win; neither level overrides nor any approval can create it.
        if not self.allow_authenticated_reads or str(credential) in (
            self.config.blocked_capabilities
        ):
            return Decision(Outcome.DENY, "bound credential reads are blocked by policy",
                            capability=credential)
        required = self.config.capability_overrides.get(
            str(credential), PermissionLevel.OBSERVE)
        if self.level < required:
            return Decision(Outcome.DENY, "bound credential read level is not permitted",
                            capability=credential, required_level=required)
        return Decision(Outcome.ALLOW, "host-authorized bound credential read",
                        capability=credential, required_level=required)

    def evaluate_all(
        self,
        capabilities: Iterable[Capability],
    ) -> CapabilityEvaluation:
        """Resolve the exact sorted set without entering a human boundary."""
        values = tuple(capabilities)
        if any(not isinstance(capability, Capability) for capability in values):
            raise TypeError("capabilities must contain Capability values")
        caps = tuple(sorted(set(values), key=str))
        pending: list[Capability] = []
        for capability in caps:
            decision = self.evaluate(capability)
            if decision.outcome is Outcome.DENY:
                return CapabilityEvaluation(
                    Outcome.DENY, caps, denied=decision)
            if decision.outcome is Outcome.REQUIRE_APPROVAL:
                pending.append(capability)
        if pending:
            return CapabilityEvaluation(
                Outcome.REQUIRE_APPROVAL,
                caps,
                approval_capabilities=tuple(pending),
            )
        return CapabilityEvaluation(Outcome.ALLOW, caps)
