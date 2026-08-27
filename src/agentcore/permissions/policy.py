"""Permission policy: the single decision point for "may this action run?".

The policy answers with a :class:`Decision` and never performs the action
itself. Executors are responsible for honouring the decision — that separation
keeps the rule set testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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
    """Evaluates capability requests against the configured posture."""

    def __init__(
        self,
        config: PermissionConfig | None = None,
        *,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        self.config = config or PermissionConfig()
        self.approval_gate = approval_gate or DenyAllApprovalGate()

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
    ) -> Decision:
        """Evaluate every capability; the most restrictive outcome wins.

        REQUIRE_APPROVAL is resolved through the approval gate, turning into
        ALLOW or DENY so callers only ever see a terminal answer.
        """
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
