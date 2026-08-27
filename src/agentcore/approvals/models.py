"""Strict immutable records for durable one-shot human approvals."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from agentcore.approvals.errors import ApprovalValidationError


APPROVAL_SCHEMA_VERSION = 1
MAX_APPROVAL_ACTION_KIND_CHARS = 128
MAX_APPROVAL_TOOL_NAME_CHARS = 128
MAX_APPROVAL_TOOL_VERSION_CHARS = 64
MAX_APPROVAL_CAPABILITIES = 32
MAX_APPROVAL_CAPABILITY_CHARS = 64
MAX_APPROVAL_PRINCIPAL_CHARS = 128
MAX_APPROVAL_SCOPE_CHARS = 128
APPROVAL_DIGEST_HEX_CHARS = 64
MAX_APPROVAL_COUNTER = 2**63 - 1

_REQUEST_ID = re.compile(r"^apr_[0-9a-f]{12}$")
_RECEIPT_ID = re.compile(r"^aprc_[0-9a-f]{12}$")
_ACTION_ID = re.compile(r"^act_[0-9a-f]{12}$")
_LOWER_HEX = re.compile(r"^[0-9a-f]{64}$")
_CATEGORICAL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+=-]*$")


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CONSUMED = "consumed"
    EXPIRED = "expired"

    def __str__(self) -> str:
        return self.value


class ApprovalSemantics(str, Enum):
    ONE_SHOT = "one_shot"

    def __str__(self) -> str:
        return self.value


class HumanApprovalDecision(str, Enum):
    """Typed output of the host-controlled human boundary."""

    APPROVE = "approve"
    DENY = "deny"
    PENDING = "pending"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ApprovalBinding:
    """Current trusted mutation identity, excluding store-minted fields."""

    action_kind: str
    intent_digest: str
    mutation_capabilities: tuple[str, ...]
    tool_name: str
    tool_version: str
    policy_fingerprint: str
    principal_ref: str
    scope_ref: str
    semantics: ApprovalSemantics = ApprovalSemantics.ONE_SHOT

    def validate(self) -> ApprovalBinding:
        _categorical(self.action_kind, MAX_APPROVAL_ACTION_KIND_CHARS,
                     "approval action kind")
        _digest(self.intent_digest, "approval intent digest")
        _categorical_tuple(
            self.mutation_capabilities,
            MAX_APPROVAL_CAPABILITIES,
            MAX_APPROVAL_CAPABILITY_CHARS,
            "approval mutation capabilities",
            require_nonempty=True,
        )
        _categorical(self.tool_name, MAX_APPROVAL_TOOL_NAME_CHARS,
                     "approval tool name")
        _version(self.tool_version, MAX_APPROVAL_TOOL_VERSION_CHARS,
                 "approval tool version")
        _digest(self.policy_fingerprint, "approval policy fingerprint")
        _reference(self.principal_ref, MAX_APPROVAL_PRINCIPAL_CHARS,
                   "approval principal")
        _reference(self.scope_ref, MAX_APPROVAL_SCOPE_CHARS,
                   "approval scope")
        if self.semantics is not ApprovalSemantics.ONE_SHOT:
            raise ApprovalValidationError("approval semantics are invalid")
        return self


@dataclass(frozen=True)
class ApprovalReceipt:
    """Durable trusted human decision bound to one exact action opportunity."""

    receipt_id: str
    request_id: str
    action_id: str
    binding_digest: str
    policy_fingerprint: str
    principal_ref: str
    scope_ref: str
    semantics: ApprovalSemantics
    approved_at: datetime
    expires_at: datetime

    def validate(self) -> ApprovalReceipt:
        require_approval_receipt_id(self.receipt_id)
        require_approval_request_id(self.request_id)
        require_approval_action_id(self.action_id)
        _digest(self.binding_digest, "approval binding digest")
        _digest(self.policy_fingerprint, "approval policy fingerprint")
        _reference(self.principal_ref, MAX_APPROVAL_PRINCIPAL_CHARS,
                   "approval principal")
        _reference(self.scope_ref, MAX_APPROVAL_SCOPE_CHARS,
                   "approval scope")
        if self.semantics is not ApprovalSemantics.ONE_SHOT:
            raise ApprovalValidationError("approval receipt semantics are invalid")
        approved = _utc(self.approved_at, "approval approved_at")
        expires = _utc(self.expires_at, "approval expires_at")
        if expires <= approved:
            raise ApprovalValidationError(
                "approval receipt must expire after approval")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "action_id": self.action_id,
            "binding_digest": self.binding_digest,
            "policy_fingerprint": self.policy_fingerprint,
            "principal_ref": self.principal_ref,
            "scope_ref": self.scope_ref,
            "semantics": self.semantics.value,
            "approved_at": _format_utc(self.approved_at, "approval approved_at"),
            "expires_at": _format_utc(self.expires_at, "approval expires_at"),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApprovalReceipt:
        _exact_keys(data, {
            "receipt_id", "request_id", "action_id", "binding_digest",
            "policy_fingerprint", "principal_ref", "scope_ref", "semantics",
            "approved_at", "expires_at",
        }, "approval receipt")
        return cls(
            receipt_id=require_approval_receipt_id(data["receipt_id"]),
            request_id=require_approval_request_id(data["request_id"]),
            action_id=require_approval_action_id(data["action_id"]),
            binding_digest=_digest(data["binding_digest"],
                                   "approval binding digest"),
            policy_fingerprint=_digest(data["policy_fingerprint"],
                                       "approval policy fingerprint"),
            principal_ref=_reference(data["principal_ref"],
                                     MAX_APPROVAL_PRINCIPAL_CHARS,
                                     "approval principal"),
            scope_ref=_reference(data["scope_ref"], MAX_APPROVAL_SCOPE_CHARS,
                                 "approval scope"),
            semantics=_parse_enum(data["semantics"], ApprovalSemantics,
                                  "approval semantics"),
            approved_at=_parse_utc(data["approved_at"], "approval approved_at"),
            expires_at=_parse_utc(data["expires_at"], "approval expires_at"),
        ).validate()


@dataclass(frozen=True)
class ApprovalRequest:
    """One append-only decision with a small CAS-controlled lifecycle."""

    request_id: str
    action_id: str
    state: ApprovalState
    version: int
    binding_digest: str
    action_kind: str
    intent_digest: str
    mutation_capabilities: tuple[str, ...]
    tool_name: str
    tool_version: str
    policy_fingerprint: str
    principal_ref: str
    scope_ref: str
    semantics: ApprovalSemantics
    created_at: datetime
    expires_at: datetime
    decision_at: datetime | None = None
    consumed_at: datetime | None = None
    consumed_action_id: str | None = None
    receipt: ApprovalReceipt | None = None
    schema_version: int = APPROVAL_SCHEMA_VERSION
    # The authority-owned keyed authenticator for the complete current record.
    # A newly constructed request carries a zero placeholder until the
    # authority computes the MAC before publishing it.
    state_mac: str = "0" * APPROVAL_DIGEST_HEX_CHARS

    def validate(self) -> ApprovalRequest:
        if self.schema_version != APPROVAL_SCHEMA_VERSION or isinstance(
                self.schema_version, bool):
            raise ApprovalValidationError(
                "unsupported approval schema version",
                supported=APPROVAL_SCHEMA_VERSION)
        require_approval_request_id(self.request_id)
        require_approval_action_id(self.action_id)
        if not isinstance(self.state, ApprovalState):
            raise ApprovalValidationError("approval state is invalid")
        _counter(self.version, "approval version")
        binding = self.as_binding().validate()
        _digest(self.binding_digest, "approval binding digest")
        _digest(self.state_mac, "approval state MAC")
        created = _utc(self.created_at, "approval created_at")
        expires = _utc(self.expires_at, "approval expires_at")
        if expires <= created:
            raise ApprovalValidationError(
                "approval request must expire after creation")
        if self.decision_at is not None:
            decided = _utc(self.decision_at, "approval decision_at")
            if decided < created:
                raise ApprovalValidationError(
                    "approval decision predates request")
        if self.consumed_at is not None:
            consumed = _utc(self.consumed_at, "approval consumed_at")
            if self.decision_at is None or consumed < self.decision_at:
                raise ApprovalValidationError(
                    "approval consumption predates decision")
        if self.consumed_action_id is not None:
            require_approval_action_id(self.consumed_action_id)
        if self.receipt is not None:
            if not isinstance(self.receipt, ApprovalReceipt):
                raise ApprovalValidationError("approval receipt is invalid")
            self.receipt.validate()
            if (
                self.receipt.request_id != self.request_id
                or self.receipt.action_id != self.action_id
                or self.receipt.binding_digest != self.binding_digest
                or self.receipt.policy_fingerprint != binding.policy_fingerprint
                or self.receipt.principal_ref != binding.principal_ref
                or self.receipt.scope_ref != binding.scope_ref
                or self.receipt.semantics is not binding.semantics
                or self.receipt.expires_at != self.expires_at
                or self.receipt.approved_at != self.decision_at
            ):
                raise ApprovalValidationError(
                    "approval receipt does not match request")
        self._validate_state_shape()
        return self

    def _validate_state_shape(self) -> None:
        if self.state is ApprovalState.PENDING:
            if self.version != 0 or any(value is not None for value in (
                self.decision_at, self.consumed_at, self.consumed_action_id,
                self.receipt,
            )):
                raise ApprovalValidationError("pending approval carries history")
        elif self.state is ApprovalState.APPROVED:
            if self.version != 1 or self.decision_at is None or self.receipt is None or (
                self.consumed_at is not None or self.consumed_action_id is not None
            ):
                raise ApprovalValidationError("approved request shape is invalid")
        elif self.state is ApprovalState.DENIED:
            if self.version != 1 or self.decision_at is None or any(
                value is not None for value in (
                    self.consumed_at, self.consumed_action_id, self.receipt)
            ):
                raise ApprovalValidationError("denied request shape is invalid")
        elif self.state is ApprovalState.CONSUMED:
            if self.version != 2 or self.decision_at is None or (
                self.consumed_at is None or self.receipt is None or
                self.consumed_action_id != self.action_id
            ):
                raise ApprovalValidationError("consumed request shape is invalid")
        elif self.state is ApprovalState.EXPIRED:
            if self.version not in {1, 2} or self.consumed_at is not None or (
                    self.consumed_action_id is not None):
                raise ApprovalValidationError("expired request shape is invalid")
            if self.version == 1 and self.decision_at is not None:
                raise ApprovalValidationError(
                    "pending expiration cannot fabricate a decision")
            if self.version == 2 and (
                    self.decision_at is None or self.receipt is None):
                raise ApprovalValidationError(
                    "approved expiration must preserve its receipt")

    def as_binding(self) -> ApprovalBinding:
        return ApprovalBinding(
            action_kind=self.action_kind,
            intent_digest=self.intent_digest,
            mutation_capabilities=self.mutation_capabilities,
            tool_name=self.tool_name,
            tool_version=self.tool_version,
            policy_fingerprint=self.policy_fingerprint,
            principal_ref=self.principal_ref,
            scope_ref=self.scope_ref,
            semantics=self.semantics,
        )

    def approve(self, *, receipt: ApprovalReceipt,
                at: datetime) -> ApprovalRequest:
        self.validate()
        if self.state is not ApprovalState.PENDING:
            raise ApprovalValidationError(
                "only a pending approval may be approved")
        at = _utc(at, "approval decision_at")
        if at >= self.expires_at:
            raise ApprovalValidationError("approval request has expired")
        return replace(
            self, state=ApprovalState.APPROVED, version=1,
            decision_at=at, receipt=receipt).validate()

    def deny(self, *, at: datetime) -> ApprovalRequest:
        self.validate()
        if self.state is not ApprovalState.PENDING:
            raise ApprovalValidationError(
                "only a pending approval may be denied")
        return replace(
            self, state=ApprovalState.DENIED, version=1,
            decision_at=_utc(at, "approval decision_at")).validate()

    def expire(self) -> ApprovalRequest:
        self.validate()
        if self.state not in {ApprovalState.PENDING, ApprovalState.APPROVED}:
            raise ApprovalValidationError(
                "only an unresolved approval may expire")
        return replace(
            self, state=ApprovalState.EXPIRED,
            version=self.version + 1).validate()

    def consume(self, *, action_id: str, binding_digest: str,
                at: datetime) -> ApprovalRequest:
        self.validate()
        at = _utc(at, "approval consumed_at")
        if self.state is not ApprovalState.APPROVED:
            raise ApprovalValidationError("approval is not consumable")
        if at >= self.expires_at:
            raise ApprovalValidationError("approval request has expired")
        if require_approval_action_id(action_id) != self.action_id or not (
                isinstance(binding_digest, str) and
                binding_digest == self.binding_digest):
            raise ApprovalValidationError("approval consumption binding mismatch")
        return replace(
            self, state=ApprovalState.CONSUMED, version=2,
            consumed_at=at, consumed_action_id=self.action_id).validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "request_id": self.request_id,
            "action_id": self.action_id,
            "state": self.state.value,
            "version": self.version,
            "binding_digest": self.binding_digest,
            "action_kind": self.action_kind,
            "intent_digest": self.intent_digest,
            "mutation_capabilities": list(self.mutation_capabilities),
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "policy_fingerprint": self.policy_fingerprint,
            "principal_ref": self.principal_ref,
            "scope_ref": self.scope_ref,
            "semantics": self.semantics.value,
            "created_at": _format_utc(self.created_at, "approval created_at"),
            "expires_at": _format_utc(self.expires_at, "approval expires_at"),
            "decision_at": (_format_utc(self.decision_at,
                                         "approval decision_at")
                            if self.decision_at is not None else None),
            "consumed_at": (_format_utc(self.consumed_at,
                                         "approval consumed_at")
                            if self.consumed_at is not None else None),
            "consumed_action_id": self.consumed_action_id,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "schema_version": self.schema_version,
            "state_mac": self.state_mac,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApprovalRequest:
        expected = {
            "request_id", "action_id", "state", "version", "binding_digest",
            "action_kind", "intent_digest", "mutation_capabilities",
            "tool_name", "tool_version", "policy_fingerprint",
            "principal_ref", "scope_ref", "semantics", "created_at",
            "expires_at", "decision_at", "consumed_at",
            "consumed_action_id", "receipt", "schema_version", "state_mac",
        }
        _exact_keys(data, expected, "approval request")
        raw_caps = data["mutation_capabilities"]
        if not isinstance(raw_caps, list):
            raise ApprovalValidationError(
                "approval mutation capabilities must be a list")
        raw_receipt = data["receipt"]
        if raw_receipt is not None and not isinstance(raw_receipt, Mapping):
            raise ApprovalValidationError("approval receipt must be a mapping")
        return cls(
            request_id=require_approval_request_id(data["request_id"]),
            action_id=require_approval_action_id(data["action_id"]),
            state=_parse_enum(data["state"], ApprovalState, "approval state"),
            version=_counter(data["version"], "approval version"),
            binding_digest=_digest(data["binding_digest"],
                                   "approval binding digest"),
            action_kind=_categorical(data["action_kind"],
                                     MAX_APPROVAL_ACTION_KIND_CHARS,
                                     "approval action kind"),
            intent_digest=_digest(data["intent_digest"],
                                  "approval intent digest"),
            mutation_capabilities=tuple(_categorical(
                item, MAX_APPROVAL_CAPABILITY_CHARS,
                "approval mutation capability") for item in raw_caps),
            tool_name=_categorical(data["tool_name"],
                                   MAX_APPROVAL_TOOL_NAME_CHARS,
                                   "approval tool name"),
            tool_version=_version(data["tool_version"],
                                  MAX_APPROVAL_TOOL_VERSION_CHARS,
                                  "approval tool version"),
            policy_fingerprint=_digest(data["policy_fingerprint"],
                                       "approval policy fingerprint"),
            principal_ref=_reference(data["principal_ref"],
                                     MAX_APPROVAL_PRINCIPAL_CHARS,
                                     "approval principal"),
            scope_ref=_reference(data["scope_ref"], MAX_APPROVAL_SCOPE_CHARS,
                                 "approval scope"),
            semantics=_parse_enum(data["semantics"], ApprovalSemantics,
                                  "approval semantics"),
            created_at=_parse_utc(data["created_at"], "approval created_at"),
            expires_at=_parse_utc(data["expires_at"], "approval expires_at"),
            decision_at=(_parse_utc(data["decision_at"],
                                    "approval decision_at")
                         if data["decision_at"] is not None else None),
            consumed_at=(_parse_utc(data["consumed_at"],
                                    "approval consumed_at")
                         if data["consumed_at"] is not None else None),
            consumed_action_id=(require_approval_action_id(
                data["consumed_action_id"])
                if data["consumed_action_id"] is not None else None),
            receipt=(ApprovalReceipt.from_dict(raw_receipt)
                     if raw_receipt is not None else None),
            schema_version=_counter(data["schema_version"],
                                    "approval schema version"),
            state_mac=_digest(data["state_mac"], "approval state MAC"),
        ).validate()


def new_approval_request(
    *, request_id: str, action_id: str, binding_digest: str,
    binding: ApprovalBinding, created_at: datetime, expires_at: datetime,
) -> ApprovalRequest:
    binding.validate()
    return ApprovalRequest(
        request_id=require_approval_request_id(request_id),
        action_id=require_approval_action_id(action_id),
        state=ApprovalState.PENDING,
        version=0,
        binding_digest=_digest(binding_digest, "approval binding digest"),
        action_kind=binding.action_kind,
        intent_digest=binding.intent_digest,
        mutation_capabilities=binding.mutation_capabilities,
        tool_name=binding.tool_name,
        tool_version=binding.tool_version,
        policy_fingerprint=binding.policy_fingerprint,
        principal_ref=binding.principal_ref,
        scope_ref=binding.scope_ref,
        semantics=binding.semantics,
        created_at=created_at,
        expires_at=expires_at,
    ).validate()


def validate_approval_successor(current: ApprovalRequest,
                                candidate: ApprovalRequest) -> None:
    current.validate()
    candidate.validate()
    immutable_before = (
        current.request_id, current.action_id, current.binding_digest,
        current.action_kind, current.intent_digest,
        current.mutation_capabilities, current.tool_name,
        current.tool_version, current.policy_fingerprint,
        current.principal_ref, current.scope_ref, current.semantics,
        current.created_at, current.expires_at, current.schema_version,
    )
    immutable_after = (
        candidate.request_id, candidate.action_id, candidate.binding_digest,
        candidate.action_kind, candidate.intent_digest,
        candidate.mutation_capabilities, candidate.tool_name,
        candidate.tool_version, candidate.policy_fingerprint,
        candidate.principal_ref, candidate.scope_ref, candidate.semantics,
        candidate.created_at, candidate.expires_at, candidate.schema_version,
    )
    if immutable_after != immutable_before:
        raise ApprovalValidationError(
            "approval transition rewrites immutable binding")
    allowed = {
        ApprovalState.PENDING: {
            ApprovalState.APPROVED, ApprovalState.DENIED, ApprovalState.EXPIRED,
        },
        ApprovalState.APPROVED: {
            ApprovalState.CONSUMED, ApprovalState.EXPIRED,
        },
        ApprovalState.DENIED: set(),
        ApprovalState.CONSUMED: set(),
        ApprovalState.EXPIRED: set(),
    }
    if candidate.state not in allowed[current.state] or (
            candidate.version != current.version + 1):
        raise ApprovalValidationError("illegal approval lifecycle transition")
    if current.decision_at is not None and (
            candidate.decision_at != current.decision_at or
            candidate.receipt != current.receipt):
        raise ApprovalValidationError("approval decision history was rewritten")


def require_approval_request_id(value: Any) -> str:
    return _pattern(value, _REQUEST_ID, "approval request id")


def require_approval_receipt_id(value: Any) -> str:
    return _pattern(value, _RECEIPT_ID, "approval receipt id")


def require_approval_action_id(value: Any) -> str:
    return _pattern(value, _ACTION_ID, "approval action id")


def _exact_keys(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(data, Mapping) or set(data) != expected or any(
            not isinstance(key, str) for key in data):
        raise ApprovalValidationError(f"{label} fields are invalid")


def _pattern(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ApprovalValidationError(f"{field} is invalid")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _LOWER_HEX.fullmatch(value):
        raise ApprovalValidationError(f"{field} is invalid")
    return value


def _categorical(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or not (
            _CATEGORICAL.fullmatch(value)):
        raise ApprovalValidationError(f"{field} is invalid")
    return value


def _version(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or not (
            _VERSION.fullmatch(value)):
        raise ApprovalValidationError(f"{field} is invalid")
    return value


def _reference(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or not (
            _REFERENCE.fullmatch(value)):
        raise ApprovalValidationError(f"{field} is invalid")
    return value


def _categorical_tuple(values: Any, maximum_items: int, maximum_chars: int,
                       field: str, *, require_nonempty: bool = False) -> None:
    if not isinstance(values, tuple) or len(values) > maximum_items or (
            require_nonempty and not values) or tuple(sorted(set(values))) != values:
        raise ApprovalValidationError(f"{field} are invalid")
    for value in values:
        _categorical(value, maximum_chars, field)


def _counter(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (
            0 <= value <= MAX_APPROVAL_COUNTER):
        raise ApprovalValidationError(f"{field} is invalid")
    return value


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or (
            value.utcoffset() != timedelta(0)):
        raise ApprovalValidationError(f"{field} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime, field: str) -> str:
    return _utc(value, field).isoformat()


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ApprovalValidationError(f"{field} is invalid")
    try:
        return _utc(datetime.fromisoformat(value), field)
    except (TypeError, ValueError) as exc:
        raise ApprovalValidationError(f"{field} is invalid") from exc


def _parse_enum(value: Any, enum_type: type[Enum], field: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ApprovalValidationError(f"{field} is invalid") from exc
