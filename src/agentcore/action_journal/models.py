"""Strict immutable records for durable action/effect truth.

This module is the Stage 5 domain substrate only.  It neither grants
permission nor invokes a tool/effect.  In particular, an ``ActionAttempt`` is
a durable *claim*; its existence is not evidence that a mutation seam ran.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any

from agentcore.action_journal.errors import (
    ActionTransitionError,
    ActionValidationError,
)
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


ACTION_JOURNAL_SCHEMA_VERSION = 1

# Public-record bounds.  Hitting a history bound blocks new work; histories
# are never truncated to make space for another possible physical attempt.
MAX_ACTION_KIND_CHARS = 128
MAX_TOOL_NAME_CHARS = 128
MAX_TOOL_VERSION_CHARS = 64
MAX_CAPABILITIES = 32
MAX_CAPABILITY_NAME_CHARS = 64
INTENT_DIGEST_HEX_CHARS = 64
MAX_INTENT_REF_CHARS = 256
MAX_TARGET_CATEGORY_CHARS = 64
TARGET_FINGERPRINT_HEX_CHARS = 64
MAX_DEDUPLICATION_SCOPE_CHARS = 128
MAX_DEDUPLICATION_KEY_CHARS = 256
MAX_IDEMPOTENCY_KEY_CHARS = 256
MAX_IDEMPOTENCY_CONTRACT_CHARS = 128
MAX_IDEMPOTENCY_SCOPE_CHARS = 128
MAX_PROVENANCE_REF_CHARS = 128
AUTHORITY_DIGEST_HEX_CHARS = 64
MAX_AUTHORITY_SCOPE_CHARS = 128
MAX_DUPLICATE_RISK_APPROVAL_REQUEST_ID_CHARS = 128
DUPLICATE_RISK_BINDING_DIGEST_HEX_CHARS = 64
MAX_ACTION_ATTEMPTS = 32
MAX_ACTION_TRANSITIONS = 256
MAX_EVIDENCE_REFS = 64
MAX_EVIDENCE_REF_CHARS = 256
MAX_RESULT_CLASS_CHARS = 64
MAX_RESULT_SUMMARY_CHARS = 512
MAX_PARTIAL_CATEGORIES = 32
MAX_PARTIAL_CATEGORY_CHARS = 64
MAX_RECONCILIATION_OWNER_CHARS = 128
MAX_RECONCILIATION_PROBES = 64
MAX_COUNTER_VALUE = 2**63 - 1

_ACTION_ID = re.compile(r"^act_[0-9a-f]{12}$")
_INVOCATION_ID = re.compile(r"^inv_[0-9a-f]{12}$")
# Deliberately distinct from Stage 4 model transport's ``att_`` namespace.
_ACTION_ATTEMPT_ID = re.compile(r"^aat_[0-9a-f]{12}$")
_ACTION_TRANSITION_ID = re.compile(r"^atx_[0-9a-f]{12}$")
_AUTHORITY_RECEIPT_ID = re.compile(r"^auth_[0-9a-f]{12}$")
_DUPLICATE_RISK_ACKNOWLEDGEMENT_ID = re.compile(r"^dra_[0-9a-f]{12}$")
_TURN_ID = re.compile(r"^turn_[0-9a-f]{12}$")
_OPERATION_ID = re.compile(r"^op_[0-9a-f]{12}$")
_RUN_ID = re.compile(r"^run_[0-9a-f]{12}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{12}$")
_SUBTASK_ID = re.compile(r"^sub_[0-9a-f]{12}$")
_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CATEGORICAL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_VERSION_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+=-]*$")


class ActionState(str, Enum):
    AUTHORIZED = "authorized"
    IN_FLIGHT = "in_flight"
    RECONCILE_REQUIRED = "reconcile_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def __str__(self) -> str:
        return self.value

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_ACTION_STATES


TERMINAL_ACTION_STATES = frozenset({
    ActionState.SUCCEEDED,
    ActionState.FAILED,
    ActionState.CANCELLED,
})

LEGAL_ACTION_TRANSITIONS: Mapping[ActionState, frozenset[ActionState]] = (
    MappingProxyType({
        ActionState.AUTHORIZED: frozenset({
            ActionState.IN_FLIGHT,
            ActionState.CANCELLED,
            ActionState.FAILED,
        }),
        ActionState.IN_FLIGHT: frozenset({
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.RECONCILE_REQUIRED,
            ActionState.AUTHORIZED,
            ActionState.CANCELLED,
        }),
        ActionState.RECONCILE_REQUIRED: frozenset({
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.AUTHORIZED,
            ActionState.RECONCILE_REQUIRED,
        }),
        ActionState.SUCCEEDED: frozenset(),
        ActionState.FAILED: frozenset(),
        ActionState.CANCELLED: frozenset(),
    })
)


class EffectVerdict(str, Enum):
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    CONFIRMED_PARTIAL = "confirmed_partial"
    OUTCOME_UNKNOWN = "outcome_unknown"

    def __str__(self) -> str:
        return self.value


class ActionJournalEligibility(str, Enum):
    PURE_READ = "pure_read"
    EPHEMERAL_CONTROL = "ephemeral_control"
    DURABLE_LOCAL_MUTATION = "durable_local_mutation"
    EXTERNAL_ACCOUNT_MUTATION = "external_account_mutation"
    SECURITY_PRIVILEGE_SYSTEM_MUTATION = (
        "security_privilege_system_mutation"
    )
    LONG_RUNNING_OR_AMBIGUOUS_EFFECT = "long_running_or_ambiguous_effect"

    def __str__(self) -> str:
        return self.value

    @property
    def requires_journal(self) -> bool:
        return self not in {
            ActionJournalEligibility.PURE_READ,
            ActionJournalEligibility.EPHEMERAL_CONTROL,
        }


class ActionEffectClass(str, Enum):
    EXTERNALLY_IDEMPOTENT = "externally_idempotent"
    LOCALLY_VERIFIABLE_OR_REVERSIBLE = "locally_verifiable_or_reversible"
    NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS = (
        "non_idempotent_or_externally_ambiguous"
    )

    def __str__(self) -> str:
        return self.value


class ActionAttemptStatus(str, Enum):
    CLAIMED = "claimed"
    NOT_INVOKED = "not_invoked"
    INVOKED = "invoked"
    POSSIBLY_INVOKED = "possibly_invoked"

    def __str__(self) -> str:
        return self.value


class ActionTransitionProof(str, Enum):
    NONE = "none"
    DEFINITIVE_NO_EFFECT = "definitive_no_effect"
    EXTERNAL_IDEMPOTENCY_GUARANTEE = "external_idempotency_guarantee"
    LIVE_OWNER_NOT_INVOKED = "live_owner_not_invoked"

    def __str__(self) -> str:
        return self.value


class ReconciliationDisposition(str, Enum):
    NONE = "none"
    PROBE_LATER = "probe_later"
    HUMAN_CONFIRMATION_REQUIRED = "human_confirmation_required"
    UNRESOLVABLE = "unresolvable"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AuthorityReceipt:
    """Bounded evidence of an upstream final authority decision.

    Its presence records an allow; it cannot itself grant one.  The binding
    digest is produced by trusted authority code from the canonical action
    kind, intent, capability set, owner, and scope.
    """

    receipt_id: str
    binding_digest: str
    policy_fingerprint: str
    scope_ref: str
    authorized_at: datetime
    expires_at: datetime | None = None
    approval_request_id: str | None = None

    def validate(self) -> AuthorityReceipt:
        _require_pattern(self.receipt_id, _AUTHORITY_RECEIPT_ID,
                         "authority receipt id")
        _require_digest(self.binding_digest, "authority binding digest")
        _require_digest(self.policy_fingerprint, "policy fingerprint")
        _require_reference(self.scope_ref, MAX_AUTHORITY_SCOPE_CHARS,
                           "authority scope")
        authorized = _ensure_utc(self.authorized_at, "authorized_at")
        if self.expires_at is not None:
            expires = _ensure_utc(self.expires_at, "authority expires_at")
            if expires < authorized:
                raise ActionValidationError(
                    "authority expiry precedes authorization")
        if self.approval_request_id is not None:
            _require_reference(self.approval_request_id,
                               MAX_PROVENANCE_REF_CHARS,
                               "approval request id")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "receipt_id": self.receipt_id,
            "binding_digest": self.binding_digest,
            "policy_fingerprint": self.policy_fingerprint,
            "scope_ref": self.scope_ref,
            "authorized_at": _format_utc(self.authorized_at,
                                          "authorized_at"),
            "expires_at": (_format_utc(self.expires_at,
                                       "authority expires_at")
                           if self.expires_at is not None else None),
            "approval_request_id": self.approval_request_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AuthorityReceipt:
        expected = {
            "receipt_id", "binding_digest", "policy_fingerprint",
            "scope_ref", "authorized_at", "expires_at",
            "approval_request_id",
        }
        _require_exact_keys(data, expected, "authority receipt")
        return cls(
            receipt_id=_require_pattern(
                data["receipt_id"], _AUTHORITY_RECEIPT_ID,
                "authority receipt id"),
            binding_digest=_require_digest(
                data["binding_digest"], "authority binding digest"),
            policy_fingerprint=_require_digest(
                data["policy_fingerprint"], "policy fingerprint"),
            scope_ref=_require_reference(
                data["scope_ref"], MAX_AUTHORITY_SCOPE_CHARS,
                "authority scope"),
            authorized_at=_parse_utc(data["authorized_at"], "authorized_at"),
            expires_at=(_parse_utc(data["expires_at"],
                                   "authority expires_at")
                        if data["expires_at"] is not None else None),
            approval_request_id=(
                _require_reference(data["approval_request_id"],
                                   MAX_PROVENANCE_REF_CHARS,
                                   "approval request id")
                if data["approval_request_id"] is not None else None
            ),
        ).validate()


@dataclass(frozen=True)
class DuplicateRiskAcknowledgement:
    """Bounded trusted receipt for deliberately repeating an ambiguous action.

    This receipt records upstream acknowledgement; it neither grants authority
    nor resolves the predecessor.  It deliberately has no field for user or
    model text.
    """

    acknowledgement_id: str
    predecessor_action_id: str
    approval_request_id: str
    binding_digest: str
    acknowledged_at: datetime

    def validate(self) -> DuplicateRiskAcknowledgement:
        _require_pattern(
            self.acknowledgement_id,
            _DUPLICATE_RISK_ACKNOWLEDGEMENT_ID,
            "duplicate-risk acknowledgement id",
        )
        _require_pattern(
            self.predecessor_action_id,
            _ACTION_ID,
            "duplicate-risk predecessor action id",
        )
        _require_reference(
            self.approval_request_id,
            MAX_DUPLICATE_RISK_APPROVAL_REQUEST_ID_CHARS,
            "duplicate-risk approval request id",
        )
        _require_digest(
            self.binding_digest,
            "duplicate-risk acknowledgement binding digest",
        )
        _ensure_utc(self.acknowledged_at, "duplicate-risk acknowledged_at")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "acknowledgement_id": self.acknowledgement_id,
            "predecessor_action_id": self.predecessor_action_id,
            "approval_request_id": self.approval_request_id,
            "binding_digest": self.binding_digest,
            "acknowledged_at": _format_utc(
                self.acknowledged_at, "duplicate-risk acknowledged_at"),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
    ) -> DuplicateRiskAcknowledgement:
        _require_exact_keys(
            data,
            {
                "acknowledgement_id",
                "predecessor_action_id",
                "approval_request_id",
                "binding_digest",
                "acknowledged_at",
            },
            "duplicate-risk acknowledgement",
        )
        return cls(
            acknowledgement_id=_require_pattern(
                data["acknowledgement_id"],
                _DUPLICATE_RISK_ACKNOWLEDGEMENT_ID,
                "duplicate-risk acknowledgement id",
            ),
            predecessor_action_id=_require_pattern(
                data["predecessor_action_id"],
                _ACTION_ID,
                "duplicate-risk predecessor action id",
            ),
            approval_request_id=_require_reference(
                data["approval_request_id"],
                MAX_DUPLICATE_RISK_APPROVAL_REQUEST_ID_CHARS,
                "duplicate-risk approval request id",
            ),
            binding_digest=_require_digest(
                data["binding_digest"],
                "duplicate-risk acknowledgement binding digest",
            ),
            acknowledged_at=_parse_utc(
                data["acknowledged_at"],
                "duplicate-risk acknowledged_at",
            ),
        ).validate()


@dataclass(frozen=True)
class ActionProvenance:
    """Fixed correlation links; none is action identity or authority."""

    invocation_id: str
    turn_id: str | None = None
    operation_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    subtask_id: str | None = None
    schedule_occurrence_id: str | None = None

    def validate(self) -> ActionProvenance:
        _require_pattern(self.invocation_id, _INVOCATION_ID, "invocation id")
        _validate_optional_pattern(self.turn_id, _TURN_ID, "turn id")
        _validate_optional_pattern(self.operation_id, _OPERATION_ID,
                                   "operation id")
        _validate_optional_pattern(self.run_id, _RUN_ID, "run id")
        _validate_optional_pattern(self.task_id, _TASK_ID, "task id")
        _validate_optional_pattern(self.subtask_id, _SUBTASK_ID, "subtask id")
        if self.operation_id is not None and self.turn_id is None:
            raise ActionValidationError("operation id requires a turn id")
        if self.subtask_id is not None and self.task_id is None:
            raise ActionValidationError("subtask id requires a task id")
        if self.schedule_occurrence_id is not None:
            _require_reference(self.schedule_occurrence_id,
                               MAX_PROVENANCE_REF_CHARS,
                               "schedule occurrence id")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "invocation_id": self.invocation_id,
            "turn_id": self.turn_id,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "schedule_occurrence_id": self.schedule_occurrence_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionProvenance:
        expected = {
            "invocation_id", "turn_id", "operation_id", "run_id",
            "task_id", "subtask_id", "schedule_occurrence_id",
        }
        _require_exact_keys(data, expected, "action provenance")
        return cls(
            invocation_id=_require_pattern(
                data["invocation_id"], _INVOCATION_ID, "invocation id"),
            turn_id=_optional_pattern(data["turn_id"], _TURN_ID, "turn id"),
            operation_id=_optional_pattern(
                data["operation_id"], _OPERATION_ID, "operation id"),
            run_id=_optional_pattern(data["run_id"], _RUN_ID, "run id"),
            task_id=_optional_pattern(data["task_id"], _TASK_ID, "task id"),
            subtask_id=_optional_pattern(
                data["subtask_id"], _SUBTASK_ID, "subtask id"),
            schedule_occurrence_id=(
                _require_reference(data["schedule_occurrence_id"],
                                   MAX_PROVENANCE_REF_CHARS,
                                   "schedule occurrence id")
                if data["schedule_occurrence_id"] is not None else None
            ),
        ).validate()


@dataclass(frozen=True)
class IdempotencyContract:
    """One stable external key plus the documented guarantee it relies on."""

    key: str
    contract: str
    scope: str
    valid_until: datetime | None = None

    def validate(self) -> IdempotencyContract:
        _require_reference(self.key, MAX_IDEMPOTENCY_KEY_CHARS,
                           "idempotency key")
        _require_categorical(self.contract, MAX_IDEMPOTENCY_CONTRACT_CHARS,
                             "idempotency contract")
        _require_reference(self.scope, MAX_IDEMPOTENCY_SCOPE_CHARS,
                           "idempotency scope")
        if self.valid_until is not None:
            _ensure_utc(self.valid_until, "idempotency valid_until")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "key": self.key,
            "contract": self.contract,
            "scope": self.scope,
            "valid_until": (_format_utc(self.valid_until,
                                        "idempotency valid_until")
                            if self.valid_until is not None else None),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IdempotencyContract:
        _require_exact_keys(
            data, {"key", "contract", "scope", "valid_until"},
            "idempotency contract")
        return cls(
            key=_require_reference(data["key"], MAX_IDEMPOTENCY_KEY_CHARS,
                                   "idempotency key"),
            contract=_require_categorical(
                data["contract"], MAX_IDEMPOTENCY_CONTRACT_CHARS,
                "idempotency contract"),
            scope=_require_reference(data["scope"],
                                     MAX_IDEMPOTENCY_SCOPE_CHARS,
                                     "idempotency scope"),
            valid_until=(_parse_utc(data["valid_until"],
                                    "idempotency valid_until")
                         if data["valid_until"] is not None else None),
        ).validate()


@dataclass(frozen=True)
class ReconciliationMetadata:
    """Bounded current reconciliation disposition and optional claim."""

    disposition: ReconciliationDisposition = ReconciliationDisposition.NONE
    claim_owner_id: str | None = None
    claim_epoch: int = 0
    claimed_at: datetime | None = None
    probe_count: int = 0
    next_probe_at: datetime | None = None

    def validate(self) -> ReconciliationMetadata:
        _require_enum(self.disposition, ReconciliationDisposition,
                      "reconciliation disposition")
        _require_nonnegative_int(self.claim_epoch, "claim_epoch")
        probe_count = _require_nonnegative_int(
            self.probe_count, "probe_count")
        if probe_count > MAX_RECONCILIATION_PROBES:
            raise ActionValidationError(
                "reconciliation probe bound exceeded",
                maximum=MAX_RECONCILIATION_PROBES)
        if (self.claim_owner_id is None) != (self.claimed_at is None):
            raise ActionValidationError(
                "reconciliation claim owner and time must appear together")
        if self.claim_owner_id is not None:
            _require_reference(self.claim_owner_id,
                               MAX_RECONCILIATION_OWNER_CHARS,
                               "reconciliation claim owner")
            _require_positive_int(self.claim_epoch, "claim_epoch")
            _ensure_utc(self.claimed_at, "reconciliation claimed_at")
        if self.next_probe_at is not None:
            _ensure_utc(self.next_probe_at, "reconciliation next_probe_at")
        if self.disposition is ReconciliationDisposition.NONE:
            if self != ReconciliationMetadata():
                raise ActionValidationError(
                    "empty reconciliation disposition carries metadata")
        elif self.disposition in {
            ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED,
            ReconciliationDisposition.UNRESOLVABLE,
        } and (self.claim_owner_id is not None or
               self.next_probe_at is not None):
            raise ActionValidationError(
                "non-probe reconciliation disposition carries a probe claim")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "disposition": self.disposition.value,
            "claim_owner_id": self.claim_owner_id,
            "claim_epoch": self.claim_epoch,
            "claimed_at": (_format_utc(self.claimed_at,
                                       "reconciliation claimed_at")
                           if self.claimed_at is not None else None),
            "probe_count": self.probe_count,
            "next_probe_at": (_format_utc(
                self.next_probe_at, "reconciliation next_probe_at")
                if self.next_probe_at is not None else None),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReconciliationMetadata:
        expected = {
            "disposition", "claim_owner_id", "claim_epoch", "claimed_at",
            "probe_count", "next_probe_at",
        }
        _require_exact_keys(data, expected, "reconciliation metadata")
        return cls(
            disposition=_parse_enum(
                data["disposition"], ReconciliationDisposition,
                "reconciliation disposition"),
            claim_owner_id=(
                _require_reference(data["claim_owner_id"],
                                   MAX_RECONCILIATION_OWNER_CHARS,
                                   "reconciliation claim owner")
                if data["claim_owner_id"] is not None else None
            ),
            claim_epoch=_require_nonnegative_int(
                data["claim_epoch"], "claim_epoch"),
            claimed_at=(_parse_utc(data["claimed_at"],
                                   "reconciliation claimed_at")
                        if data["claimed_at"] is not None else None),
            probe_count=_require_nonnegative_int(
                data["probe_count"], "probe_count"),
            next_probe_at=(_parse_utc(
                data["next_probe_at"], "reconciliation next_probe_at")
                if data["next_probe_at"] is not None else None),
        ).validate()


@dataclass(frozen=True)
class ActionOutcome:
    """Fixed terminal result/failure summary; never provider raw data."""

    verdict: EffectVerdict
    result_class: str
    summary: str
    partial_categories: tuple[str, ...] = ()

    def validate(self) -> ActionOutcome:
        verdict = _require_enum(self.verdict, EffectVerdict,
                                "terminal effect verdict")
        if verdict is EffectVerdict.OUTCOME_UNKNOWN:
            raise ActionValidationError(
                "an unknown effect cannot be a terminal outcome")
        _require_categorical(self.result_class, MAX_RESULT_CLASS_CHARS,
                             "result class")
        _require_text(self.summary, MAX_RESULT_SUMMARY_CHARS,
                      "result summary")
        _validate_categorical_tuple(
            self.partial_categories,
            maximum_items=MAX_PARTIAL_CATEGORIES,
            maximum_chars=MAX_PARTIAL_CATEGORY_CHARS,
            field_name="partial categories",
        )
        if verdict is EffectVerdict.CONFIRMED_PARTIAL:
            if not self.partial_categories:
                raise ActionValidationError(
                    "confirmed partial outcome requires partial categories")
        elif self.partial_categories:
            raise ActionValidationError(
                "non-partial outcome carries partial categories")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "verdict": self.verdict.value,
            "result_class": self.result_class,
            "summary": self.summary,
            "partial_categories": list(self.partial_categories),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionOutcome:
        _require_exact_keys(
            data, {"verdict", "result_class", "summary",
                   "partial_categories"}, "action outcome")
        raw_partial = data["partial_categories"]
        if not isinstance(raw_partial, list):
            raise ActionValidationError(
                "partial categories must be a list")
        return cls(
            verdict=_parse_enum(data["verdict"], EffectVerdict,
                                "terminal effect verdict"),
            result_class=_require_categorical(
                data["result_class"], MAX_RESULT_CLASS_CHARS,
                "result class"),
            summary=_require_text(data["summary"], MAX_RESULT_SUMMARY_CHARS,
                                  "result summary"),
            partial_categories=tuple(_require_categorical(
                value, MAX_PARTIAL_CATEGORY_CHARS, "partial category")
                for value in raw_partial),
        ).validate()


@dataclass(frozen=True)
class ActionAttempt:
    """One durable claim for at most one possible mutation-seam invocation."""

    attempt_id: str
    action_id: str
    invocation_id: str
    owner_id: str
    owner_epoch: int
    status: ActionAttemptStatus
    claimed_at: datetime
    updated_at: datetime
    effect_verdict: EffectVerdict | None = None
    evidence_refs: tuple[str, ...] = ()

    def validate(self) -> ActionAttempt:
        _require_pattern(self.attempt_id, _ACTION_ATTEMPT_ID,
                         "action attempt id")
        _require_pattern(self.action_id, _ACTION_ID, "action id")
        _require_pattern(self.invocation_id, _INVOCATION_ID, "invocation id")
        _require_reference(self.owner_id, MAX_RECONCILIATION_OWNER_CHARS,
                           "attempt owner")
        _require_positive_int(self.owner_epoch, "attempt owner_epoch")
        status = _require_enum(self.status, ActionAttemptStatus,
                               "attempt status")
        claimed = _ensure_utc(self.claimed_at, "attempt claimed_at")
        updated = _ensure_utc(self.updated_at, "attempt updated_at")
        if updated < claimed:
            raise ActionValidationError(
                "attempt updated_at precedes claimed_at")
        if self.effect_verdict is not None:
            _require_enum(self.effect_verdict, EffectVerdict,
                          "attempt effect verdict")
        _validate_reference_tuple(
            self.evidence_refs,
            maximum_items=MAX_EVIDENCE_REFS,
            maximum_chars=MAX_EVIDENCE_REF_CHARS,
            field_name="attempt evidence refs",
        )
        if status is ActionAttemptStatus.CLAIMED and (
                self.effect_verdict is not None or self.evidence_refs or
                updated != claimed):
            raise ActionValidationError(
                "a claimed attempt carries post-claim evidence")
        if status is ActionAttemptStatus.NOT_INVOKED and \
                self.effect_verdict not in {
                    None, EffectVerdict.CONFIRMED_NOT_APPLIED,
                }:
            raise ActionValidationError(
                "a not-invoked attempt carries an impossible verdict")
        if status is ActionAttemptStatus.INVOKED and \
                self.effect_verdict is None:
            raise ActionValidationError(
                "a resolved invoked attempt requires an effect verdict")
        if status is ActionAttemptStatus.POSSIBLY_INVOKED and \
                self.effect_verdict is None:
            raise ActionValidationError(
                "a possibly-invoked attempt requires an effect verdict")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "attempt_id": self.attempt_id,
            "action_id": self.action_id,
            "invocation_id": self.invocation_id,
            "owner_id": self.owner_id,
            "owner_epoch": self.owner_epoch,
            "status": self.status.value,
            "claimed_at": _format_utc(self.claimed_at,
                                      "attempt claimed_at"),
            "updated_at": _format_utc(self.updated_at,
                                      "attempt updated_at"),
            "effect_verdict": (self.effect_verdict.value
                               if self.effect_verdict is not None else None),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionAttempt:
        expected = {
            "attempt_id", "action_id", "invocation_id", "owner_id",
            "owner_epoch", "status", "claimed_at", "updated_at",
            "effect_verdict", "evidence_refs",
        }
        _require_exact_keys(data, expected, "action attempt")
        refs = _parse_reference_list(data["evidence_refs"],
                                     MAX_EVIDENCE_REF_CHARS,
                                     "attempt evidence refs")
        return cls(
            attempt_id=_require_pattern(
                data["attempt_id"], _ACTION_ATTEMPT_ID,
                "action attempt id"),
            action_id=_require_pattern(data["action_id"], _ACTION_ID,
                                       "action id"),
            invocation_id=_require_pattern(
                data["invocation_id"], _INVOCATION_ID, "invocation id"),
            owner_id=_require_reference(
                data["owner_id"], MAX_RECONCILIATION_OWNER_CHARS,
                "attempt owner"),
            owner_epoch=_require_positive_int(
                data["owner_epoch"], "attempt owner_epoch"),
            status=_parse_enum(data["status"], ActionAttemptStatus,
                               "attempt status"),
            claimed_at=_parse_utc(data["claimed_at"], "attempt claimed_at"),
            updated_at=_parse_utc(data["updated_at"], "attempt updated_at"),
            effect_verdict=(_parse_enum(
                data["effect_verdict"], EffectVerdict,
                "attempt effect verdict")
                if data["effect_verdict"] is not None else None),
            evidence_refs=refs,
        ).validate()


@dataclass(frozen=True)
class ActionTransition:
    """Append-only categorical lifecycle entry with exact replay evidence."""

    transition_id: str
    from_state: ActionState
    to_state: ActionState
    from_version: int
    to_version: int
    at: datetime
    attempt_id: str | None = None
    attempt_status: ActionAttemptStatus | None = None
    effect_verdict: EffectVerdict | None = None
    proof: ActionTransitionProof = ActionTransitionProof.NONE
    evidence_refs: tuple[str, ...] = ()
    reconciliation: ReconciliationMetadata = field(
        default_factory=ReconciliationMetadata)
    terminal_outcome: ActionOutcome | None = None

    def validate(self) -> ActionTransition:
        _require_pattern(self.transition_id, _ACTION_TRANSITION_ID,
                         "action transition id")
        from_state = _require_enum(self.from_state, ActionState, "from state")
        to_state = _require_enum(self.to_state, ActionState, "to state")
        if to_state not in LEGAL_ACTION_TRANSITIONS[from_state]:
            raise ActionTransitionError(
                "illegal action lifecycle transition",
                from_state=from_state.value, to_state=to_state.value)
        from_version = _require_nonnegative_int(
            self.from_version, "from_version")
        to_version = _require_positive_int(self.to_version, "to_version")
        if to_version != from_version + 1:
            raise ActionValidationError(
                "action transition must advance exactly one version")
        _ensure_utc(self.at, "transition at")
        if (self.attempt_id is None) != (self.attempt_status is None):
            raise ActionValidationError(
                "transition attempt identity and status must appear together")
        if self.attempt_id is not None:
            _require_pattern(self.attempt_id, _ACTION_ATTEMPT_ID,
                             "action attempt id")
            _require_enum(self.attempt_status, ActionAttemptStatus,
                          "attempt status")
        if self.effect_verdict is not None:
            _require_enum(self.effect_verdict, EffectVerdict,
                          "transition effect verdict")
        _require_enum(self.proof, ActionTransitionProof, "transition proof")
        _validate_reference_tuple(
            self.evidence_refs,
            maximum_items=MAX_EVIDENCE_REFS,
            maximum_chars=MAX_EVIDENCE_REF_CHARS,
            field_name="transition evidence refs",
        )
        if not isinstance(self.reconciliation, ReconciliationMetadata):
            raise ActionValidationError(
                "transition reconciliation metadata is invalid")
        self.reconciliation.validate()
        if to_state is ActionState.RECONCILE_REQUIRED:
            if self.reconciliation.disposition is (
                    ReconciliationDisposition.NONE):
                raise ActionValidationError(
                    "reconciliation-required transition needs a disposition")
        elif self.reconciliation != ReconciliationMetadata():
            raise ActionValidationError(
                "non-reconciliation transition carries reconciliation state")
        if self.terminal_outcome is not None:
            if not isinstance(self.terminal_outcome, ActionOutcome):
                raise ActionValidationError("terminal outcome is invalid")
            self.terminal_outcome.validate()
            if self.effect_verdict is not self.terminal_outcome.verdict:
                raise ActionValidationError(
                    "terminal outcome does not match transition verdict")
        _validate_state_outcome_shape(self)
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "transition_id": self.transition_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "at": _format_utc(self.at, "transition at"),
            "attempt_id": self.attempt_id,
            "attempt_status": (self.attempt_status.value
                               if self.attempt_status is not None else None),
            "effect_verdict": (self.effect_verdict.value
                               if self.effect_verdict is not None else None),
            "proof": self.proof.value,
            "evidence_refs": list(self.evidence_refs),
            "reconciliation": self.reconciliation.to_dict(),
            "terminal_outcome": (self.terminal_outcome.to_dict()
                                 if self.terminal_outcome is not None else None),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionTransition:
        expected = {
            "transition_id", "from_state", "to_state", "from_version",
            "to_version", "at", "attempt_id", "attempt_status",
            "effect_verdict", "proof", "evidence_refs", "reconciliation",
            "terminal_outcome",
        }
        _require_exact_keys(data, expected, "action transition")
        raw_reconciliation = data["reconciliation"]
        if not isinstance(raw_reconciliation, Mapping):
            raise ActionValidationError(
                "transition reconciliation must be a mapping")
        raw_outcome = data["terminal_outcome"]
        if raw_outcome is not None and not isinstance(raw_outcome, Mapping):
            raise ActionValidationError(
                "transition terminal outcome must be a mapping")
        return cls(
            transition_id=_require_pattern(
                data["transition_id"], _ACTION_TRANSITION_ID,
                "action transition id"),
            from_state=_parse_enum(data["from_state"], ActionState,
                                   "from state"),
            to_state=_parse_enum(data["to_state"], ActionState, "to state"),
            from_version=_require_nonnegative_int(
                data["from_version"], "from_version"),
            to_version=_require_positive_int(data["to_version"],
                                             "to_version"),
            at=_parse_utc(data["at"], "transition at"),
            attempt_id=(_require_pattern(
                data["attempt_id"], _ACTION_ATTEMPT_ID,
                "action attempt id") if data["attempt_id"] is not None
                        else None),
            attempt_status=(_parse_enum(
                data["attempt_status"], ActionAttemptStatus,
                "attempt status") if data["attempt_status"] is not None
                            else None),
            effect_verdict=(_parse_enum(
                data["effect_verdict"], EffectVerdict,
                "transition effect verdict")
                if data["effect_verdict"] is not None else None),
            proof=_parse_enum(data["proof"], ActionTransitionProof,
                              "transition proof"),
            evidence_refs=_parse_reference_list(
                data["evidence_refs"], MAX_EVIDENCE_REF_CHARS,
                "transition evidence refs"),
            reconciliation=ReconciliationMetadata.from_dict(
                raw_reconciliation),
            terminal_outcome=(ActionOutcome.from_dict(raw_outcome)
                              if raw_outcome is not None else None),
        ).validate()


@dataclass(frozen=True)
class ActionRecord:
    """Independent schema-v1 durable truth for one logical action."""

    action_id: str
    state: ActionState
    version: int
    action_kind: str
    tool_name: str
    tool_version: str
    eligibility: ActionJournalEligibility
    mutation_capabilities: tuple[str, ...]
    reconciliation_capabilities: tuple[str, ...]
    effect_class: ActionEffectClass
    intent_digest: str
    intent_ref: str | None
    target_category: str
    target_fingerprint: str | None
    deduplication_scope: str | None
    deduplication_key: str | None
    idempotency: IdempotencyContract | None
    provenance: ActionProvenance
    predecessor_action_id: str | None
    duplicate_risk_acknowledgement: DuplicateRiskAcknowledgement | None
    authority: AuthorityReceipt
    attempts: tuple[ActionAttempt, ...]
    transitions: tuple[ActionTransition, ...]
    reconciliation: ReconciliationMetadata
    terminal_outcome: ActionOutcome | None
    evidence_refs: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    schema_version: int = ACTION_JOURNAL_SCHEMA_VERSION

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def deduplication_identity(self) -> tuple[str, str] | None:
        if self.deduplication_scope is None:
            return None
        assert self.deduplication_key is not None
        return self.deduplication_scope, self.deduplication_key

    def validate(self) -> ActionRecord:
        if isinstance(self.schema_version, bool) or \
                not isinstance(self.schema_version, int) or \
                self.schema_version != ACTION_JOURNAL_SCHEMA_VERSION:
            raise ActionValidationError(
                "unsupported action journal schema version",
                supported=ACTION_JOURNAL_SCHEMA_VERSION)
        _require_pattern(self.action_id, _ACTION_ID, "action id")
        _require_enum(self.state, ActionState, "action state")
        _require_nonnegative_int(self.version, "record version")
        _require_categorical(self.action_kind, MAX_ACTION_KIND_CHARS,
                             "action kind")
        _require_categorical(self.tool_name, MAX_TOOL_NAME_CHARS, "tool name")
        _require_version(self.tool_version, MAX_TOOL_VERSION_CHARS,
                         "tool version")
        eligibility = _require_enum(
            self.eligibility, ActionJournalEligibility,
            "journal eligibility")
        if not eligibility.requires_journal:
            raise ActionValidationError(
                "non-journaled eligibility cannot create an action record",
                eligibility=eligibility.value)
        _validate_categorical_tuple(
            self.mutation_capabilities,
            maximum_items=MAX_CAPABILITIES,
            maximum_chars=MAX_CAPABILITY_NAME_CHARS,
            field_name="mutation capabilities",
            require_nonempty=True,
        )
        _validate_categorical_tuple(
            self.reconciliation_capabilities,
            maximum_items=MAX_CAPABILITIES,
            maximum_chars=MAX_CAPABILITY_NAME_CHARS,
            field_name="reconciliation capabilities",
        )
        effect_class = _require_enum(
            self.effect_class, ActionEffectClass, "effect class")
        _require_digest(self.intent_digest, "intent digest")
        if self.intent_ref is not None:
            _require_reference(self.intent_ref, MAX_INTENT_REF_CHARS,
                               "protected intent ref")
        _require_categorical(self.target_category, MAX_TARGET_CATEGORY_CHARS,
                             "target category")
        if self.target_fingerprint is not None:
            _require_digest(self.target_fingerprint, "target fingerprint")
        if (self.deduplication_scope is None) != (
                self.deduplication_key is None):
            raise ActionValidationError(
                "deduplication scope and key must appear together")
        if self.deduplication_scope is not None:
            _require_reference(self.deduplication_scope,
                               MAX_DEDUPLICATION_SCOPE_CHARS,
                               "deduplication scope")
            _require_reference(self.deduplication_key,
                               MAX_DEDUPLICATION_KEY_CHARS,
                               "deduplication key")
        if self.idempotency is not None:
            if not isinstance(self.idempotency, IdempotencyContract):
                raise ActionValidationError("idempotency metadata is invalid")
            self.idempotency.validate()
        if effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT and \
                self.idempotency is None:
            raise ActionValidationError(
                "externally idempotent action lacks its stable contract")
        if not isinstance(self.provenance, ActionProvenance):
            raise ActionValidationError("action provenance is invalid")
        self.provenance.validate()
        if self.predecessor_action_id is not None:
            _require_pattern(self.predecessor_action_id, _ACTION_ID,
                             "predecessor action id")
            if self.predecessor_action_id == self.action_id:
                raise ActionValidationError(
                    "an action cannot be its own predecessor")
        if self.duplicate_risk_acknowledgement is not None:
            acknowledgement = self.duplicate_risk_acknowledgement
            if not isinstance(
                    acknowledgement, DuplicateRiskAcknowledgement):
                raise ActionValidationError(
                    "duplicate-risk acknowledgement is invalid")
            acknowledgement.validate()
            if self.predecessor_action_id is None:
                raise ActionValidationError(
                    "duplicate-risk acknowledgement requires a predecessor")
            if acknowledgement.predecessor_action_id != (
                    self.predecessor_action_id):
                raise ActionValidationError(
                    "duplicate-risk acknowledgement predecessor mismatch")
        if not isinstance(self.authority, AuthorityReceipt):
            raise ActionValidationError("authority receipt is invalid")
        self.authority.validate()
        created = _ensure_utc(self.created_at, "created_at")
        updated = _ensure_utc(self.updated_at, "updated_at")
        if updated < created:
            raise ActionValidationError("updated_at precedes created_at")
        if self.authority.authorized_at > created:
            raise ActionValidationError(
                "action predates its authority receipt")
        if self.duplicate_risk_acknowledgement is not None and \
                self.duplicate_risk_acknowledgement.acknowledged_at > created:
            raise ActionValidationError(
                "action predates its duplicate-risk acknowledgement")
        if self.authority.expires_at is not None and \
                self.authority.expires_at <= created:
            raise ActionValidationError(
                "action was created after its authority expired")
        if not isinstance(self.attempts, tuple):
            raise ActionValidationError("action attempts must be a tuple")
        if len(self.attempts) > MAX_ACTION_ATTEMPTS:
            raise ActionValidationError("action attempt bound exceeded",
                                        maximum=MAX_ACTION_ATTEMPTS)
        if not isinstance(self.transitions, tuple):
            raise ActionValidationError("action transitions must be a tuple")
        if len(self.transitions) > MAX_ACTION_TRANSITIONS:
            raise ActionValidationError("action transition bound exceeded",
                                        maximum=MAX_ACTION_TRANSITIONS)
        if self.version != len(self.transitions):
            raise ActionValidationError(
                "record version does not match transition ledger",
                version=self.version, transitions=len(self.transitions))
        if not self.is_terminal and self.version >= MAX_ACTION_TRANSITIONS:
            raise ActionValidationError(
                "nonterminal action exhausted terminal transition reserve")
        if not isinstance(self.reconciliation, ReconciliationMetadata):
            raise ActionValidationError(
                "record reconciliation metadata is invalid")
        self.reconciliation.validate()
        if self.terminal_outcome is not None:
            if not isinstance(self.terminal_outcome, ActionOutcome):
                raise ActionValidationError("terminal outcome is invalid")
            self.terminal_outcome.validate()
        _validate_reference_tuple(
            self.evidence_refs,
            maximum_items=MAX_EVIDENCE_REFS,
            maximum_chars=MAX_EVIDENCE_REF_CHARS,
            field_name="action evidence refs",
        )
        self._validate_replay(created, updated)
        return self

    def _validate_replay(self, created: datetime, updated: datetime) -> None:
        state = ActionState.AUTHORIZED
        reconciliation = ReconciliationMetadata()
        terminal_outcome: ActionOutcome | None = None
        previous_time = created
        transition_ids: set[str] = set()
        aggregate_evidence: list[str] = []
        aggregate_seen: set[str] = set()
        attempt_ids: set[str] = set()
        replay: dict[str, tuple[
            ActionAttemptStatus,
            EffectVerdict | None,
            list[str],
            datetime,
        ]] = {}
        claimed_attempts = 0
        unresolved_action_ambiguity = False

        for index, transition in enumerate(self.transitions, start=1):
            if not isinstance(transition, ActionTransition):
                raise ActionValidationError(
                    "action transition ledger entry is invalid")
            transition.validate()
            if transition.transition_id in transition_ids:
                raise ActionValidationError(
                    "duplicate action transition id",
                    transition_id=transition.transition_id)
            transition_ids.add(transition.transition_id)
            if transition.from_version != index - 1 or \
                    transition.to_version != index:
                raise ActionValidationError(
                    "action transition versions are not linear",
                    transition_id=transition.transition_id)
            if transition.from_state is not state:
                raise ActionValidationError(
                    "action transition state chain is broken",
                    transition_id=transition.transition_id)
            if transition.at < previous_time:
                raise ActionValidationError(
                    "action transition timestamps regress",
                    transition_id=transition.transition_id)

            if transition.to_state is ActionState.IN_FLIGHT and index > 1:
                _validate_retry_claim_horizon(
                    self.transitions[index - 2],
                    transition.at,
                    self.idempotency,
                )

            prior_status: ActionAttemptStatus | None = None
            if transition.to_state is ActionState.IN_FLIGHT:
                if claimed_attempts >= len(self.attempts):
                    raise ActionValidationError(
                        "transition claims a missing action attempt")
                attempt = self.attempts[claimed_attempts]
                if not isinstance(attempt, ActionAttempt):
                    raise ActionValidationError(
                        "action attempt ledger entry is invalid")
                attempt.validate()
                if attempt.action_id != self.action_id:
                    raise ActionValidationError(
                        "action attempt belongs to another action")
                if attempt.attempt_id in attempt_ids:
                    raise ActionValidationError(
                        "duplicate action attempt id",
                        attempt_id=attempt.attempt_id)
                if transition.attempt_id != attempt.attempt_id or \
                        transition.attempt_status is not (
                            ActionAttemptStatus.CLAIMED) or \
                        transition.at != attempt.claimed_at:
                    raise ActionValidationError(
                        "attempt claim does not match transition ledger")
                attempt_ids.add(attempt.attempt_id)
                replay[attempt.attempt_id] = (
                    ActionAttemptStatus.CLAIMED, None, [], attempt.claimed_at)
                claimed_attempts += 1
            elif transition.attempt_id is not None:
                if claimed_attempts == 0 or \
                        transition.attempt_id != (
                            self.attempts[claimed_attempts - 1].attempt_id):
                    raise ActionValidationError(
                        "transition does not reference the current attempt")
                prior_status = replay[transition.attempt_id][0]

            _validate_transition_semantics(
                transition,
                effect_class=self.effect_class,
                idempotency=self.idempotency,
                prior_attempt_status=prior_status,
                unresolved_attempt_ambiguity=(
                    unresolved_action_ambiguity),
            )
            _validate_reconciliation_progress(
                reconciliation, transition.reconciliation,
                source=transition.from_state,
                target=transition.to_state,
                has_new_evidence=bool(transition.evidence_refs),
            )

            if transition.attempt_id is not None and \
                    transition.to_state is not ActionState.IN_FLIGHT:
                old_status, old_verdict, refs, _ = replay[
                    transition.attempt_id]
                assert transition.attempt_status is not None
                _validate_attempt_status_change(
                    old_status, transition.attempt_status)
                verdict = _evolved_verdict(
                    old_verdict, transition.effect_verdict)
                replay[transition.attempt_id] = (
                    transition.attempt_status,
                    verdict,
                    refs + list(transition.evidence_refs),
                    transition.at,
                )
                if verdict is EffectVerdict.OUTCOME_UNKNOWN:
                    unresolved_action_ambiguity = True

            if transition.from_state is ActionState.RECONCILE_REQUIRED and \
                    transition.effect_verdict is (
                        EffectVerdict.CONFIRMED_NOT_APPLIED):
                unresolved_action_ambiguity = False

            for ref in transition.evidence_refs:
                if ref in aggregate_seen:
                    raise ActionValidationError(
                        "action evidence reference is duplicated")
                aggregate_seen.add(ref)
                aggregate_evidence.append(ref)

            state = transition.to_state
            reconciliation = transition.reconciliation
            terminal_outcome = transition.terminal_outcome
            previous_time = transition.at

        if claimed_attempts != len(self.attempts):
            raise ActionValidationError(
                "action attempt ledger contains an unclaimed attempt")
        for attempt in self.attempts:
            expected_status, expected_verdict, expected_refs, expected_at = (
                replay[attempt.attempt_id]
            )
            if attempt.status is not expected_status or \
                    attempt.effect_verdict is not expected_verdict or \
                    attempt.evidence_refs != tuple(expected_refs) or \
                    attempt.updated_at != expected_at:
                raise ActionValidationError(
                    "action attempt does not match transition replay",
                    attempt_id=attempt.attempt_id)
        if state is not self.state:
            raise ActionValidationError(
                "record state does not match transition ledger")
        if reconciliation != self.reconciliation:
            raise ActionValidationError(
                "record reconciliation does not match transition ledger")
        if terminal_outcome != self.terminal_outcome:
            raise ActionValidationError(
                "record terminal outcome does not match transition ledger")
        if tuple(aggregate_evidence) != self.evidence_refs:
            raise ActionValidationError(
                "record evidence does not match transition ledger")
        if self.transitions:
            if updated != self.transitions[-1].at:
                raise ActionValidationError(
                    "updated_at does not match transition ledger")
        elif self.state is not ActionState.AUTHORIZED or updated != created:
            raise ActionValidationError("fresh action record is inconsistent")
        if self.state is ActionState.RECONCILE_REQUIRED:
            if self.reconciliation.disposition is (
                    ReconciliationDisposition.NONE):
                raise ActionValidationError(
                    "reconciliation-required action lacks a disposition")
        elif self.reconciliation != ReconciliationMetadata():
            raise ActionValidationError(
                "non-reconciliation action carries reconciliation metadata")
        if self.state in {ActionState.SUCCEEDED, ActionState.FAILED}:
            if self.terminal_outcome is None:
                raise ActionValidationError(
                    "completed action lacks a terminal outcome")
        elif self.terminal_outcome is not None:
            raise ActionValidationError(
                "non-result action carries a terminal outcome")
        if self.state is ActionState.CANCELLED and any(
                attempt.status is not ActionAttemptStatus.NOT_INVOKED
                for attempt in self.attempts):
            raise ActionValidationError(
                "cancelled action has an attempt not proved uninvoked")
        if self.state is ActionState.IN_FLIGHT and (
                not self.attempts or
                self.attempts[-1].status is not ActionAttemptStatus.CLAIMED):
            raise ActionValidationError(
                "in-flight action lacks its active attempt claim")

    def claim_attempt(
        self,
        *,
        attempt_id: str,
        invocation_id: str,
        owner_id: str,
        owner_epoch: int,
        transition_id: str,
        at: datetime,
    ) -> ActionRecord:
        """Return the one-record atomic claim + ``IN_FLIGHT`` transition."""
        self.validate()
        if self.state is not ActionState.AUTHORIZED:
            raise ActionTransitionError(
                "only an authorized action can claim an attempt",
                state=self.state.value)
        if len(self.attempts) >= MAX_ACTION_ATTEMPTS:
            raise ActionValidationError("action attempt bound reached",
                                        maximum=MAX_ACTION_ATTEMPTS)
        # Leave one transition slot for truthful terminalization.
        if self.version + 1 >= MAX_ACTION_TRANSITIONS:
            raise ActionValidationError(
                "action transition reserve cannot admit another attempt",
                maximum=MAX_ACTION_TRANSITIONS)
        observed_at = max(_ensure_utc(at, "claim at"), self.updated_at)
        if self.transitions:
            _validate_retry_claim_horizon(
                self.transitions[-1], observed_at, self.idempotency)
        attempt = ActionAttempt(
            attempt_id=_require_pattern(
                attempt_id, _ACTION_ATTEMPT_ID, "action attempt id"),
            action_id=self.action_id,
            invocation_id=_require_pattern(
                invocation_id, _INVOCATION_ID, "invocation id"),
            owner_id=_require_reference(
                owner_id, MAX_RECONCILIATION_OWNER_CHARS, "attempt owner"),
            owner_epoch=_require_positive_int(owner_epoch,
                                              "attempt owner_epoch"),
            status=ActionAttemptStatus.CLAIMED,
            claimed_at=observed_at,
            updated_at=observed_at,
        ).validate()
        if any(existing.attempt_id == attempt.attempt_id
               for existing in self.attempts):
            raise ActionValidationError("action attempt id was reused",
                                        attempt_id=attempt.attempt_id)
        transition = ActionTransition(
            transition_id=_require_pattern(
                transition_id, _ACTION_TRANSITION_ID,
                "action transition id"),
            from_state=self.state,
            to_state=ActionState.IN_FLIGHT,
            from_version=self.version,
            to_version=self.version + 1,
            at=observed_at,
            attempt_id=attempt.attempt_id,
            attempt_status=ActionAttemptStatus.CLAIMED,
        ).validate()
        return replace(
            self,
            state=ActionState.IN_FLIGHT,
            version=self.version + 1,
            attempts=self.attempts + (attempt,),
            transitions=self.transitions + (transition,),
            updated_at=observed_at,
        ).validate()

    def transition(
        self,
        target: ActionState,
        *,
        transition_id: str,
        at: datetime,
        attempt_status: ActionAttemptStatus | None = None,
        effect_verdict: EffectVerdict | None = None,
        proof: ActionTransitionProof = ActionTransitionProof.NONE,
        evidence_refs: tuple[str, ...] = (),
        reconciliation: ReconciliationMetadata | None = None,
        terminal_outcome: ActionOutcome | None = None,
    ) -> ActionRecord:
        """Apply one typed transition without performing any external work."""
        self.validate()
        target = _require_enum(target, ActionState, "target state")
        if target is ActionState.IN_FLIGHT:
            raise ActionTransitionError(
                "IN_FLIGHT requires an atomic attempt claim")
        if target not in LEGAL_ACTION_TRANSITIONS[self.state]:
            raise ActionTransitionError(
                "illegal action lifecycle transition",
                from_state=self.state.value, to_state=target.value)
        next_version = self.version + 1
        if next_version > MAX_ACTION_TRANSITIONS or (
                not target.is_terminal and
                next_version >= MAX_ACTION_TRANSITIONS):
            raise ActionValidationError(
                "action transition bound reached",
                maximum=MAX_ACTION_TRANSITIONS)
        observed_at = max(_ensure_utc(at, "transition at"), self.updated_at)
        new_refs = _validated_new_evidence(
            evidence_refs, existing=self.evidence_refs)
        resolved_reconciliation = (
            reconciliation if reconciliation is not None
            else ReconciliationMetadata()
        )
        if not isinstance(resolved_reconciliation, ReconciliationMetadata):
            raise ActionValidationError(
                "reconciliation metadata is invalid")
        resolved_reconciliation.validate()
        _validate_reconciliation_progress(
            self.reconciliation,
            resolved_reconciliation,
            source=self.state,
            target=target,
            has_new_evidence=bool(new_refs),
        )
        if effect_verdict is not None:
            effect_verdict = _require_enum(
                effect_verdict, EffectVerdict, "effect verdict")
        proof = _require_enum(proof, ActionTransitionProof,
                              "transition proof")

        attempt_id: str | None = None
        next_attempts = self.attempts
        prior_status: ActionAttemptStatus | None = None
        if self.state is ActionState.IN_FLIGHT:
            if not self.attempts:
                raise ActionValidationError(
                    "effect transition lacks an action attempt")
            current_attempt = self.attempts[-1]
            prior_status = current_attempt.status
            if attempt_status is None:
                raise ActionValidationError(
                    "in-flight transition requires an attempt status")
            next_status = _require_enum(
                attempt_status, ActionAttemptStatus, "attempt status")
            attempt_id = current_attempt.attempt_id
            evolved = _evolve_attempt(
                current_attempt,
                status=next_status,
                effect_verdict=effect_verdict,
                evidence_refs=new_refs,
                at=observed_at,
            )
            next_attempts = self.attempts[:-1] + (evolved,)
            attempt_status = next_status
        elif self.state is ActionState.RECONCILE_REQUIRED:
            if attempt_status is not None:
                raise ActionValidationError(
                    "reconciliation cannot attribute action truth to an "
                    "attempt")
        elif attempt_status is not None:
            raise ActionValidationError(
                "transition without an active attempt carries attempt status")

        transition = ActionTransition(
            transition_id=_require_pattern(
                transition_id, _ACTION_TRANSITION_ID,
                "action transition id"),
            from_state=self.state,
            to_state=target,
            from_version=self.version,
            to_version=next_version,
            at=observed_at,
            attempt_id=attempt_id,
            attempt_status=attempt_status,
            effect_verdict=effect_verdict,
            proof=proof,
            evidence_refs=new_refs,
            reconciliation=resolved_reconciliation,
            terminal_outcome=terminal_outcome,
        ).validate()
        _validate_transition_semantics(
            transition,
            effect_class=self.effect_class,
            idempotency=self.idempotency,
            prior_attempt_status=prior_status,
            unresolved_attempt_ambiguity=(
                _has_unresolved_action_ambiguity(self.transitions)),
        )
        return replace(
            self,
            state=target,
            version=next_version,
            attempts=next_attempts,
            transitions=self.transitions + (transition,),
            reconciliation=resolved_reconciliation,
            terminal_outcome=terminal_outcome,
            evidence_refs=self.evidence_refs + new_refs,
            updated_at=observed_at,
        ).validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "action_id": self.action_id,
            "state": self.state.value,
            "version": self.version,
            "action_kind": self.action_kind,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "eligibility": self.eligibility.value,
            "mutation_capabilities": list(self.mutation_capabilities),
            "reconciliation_capabilities": list(
                self.reconciliation_capabilities),
            "effect_class": self.effect_class.value,
            "intent_digest": self.intent_digest,
            "intent_ref": self.intent_ref,
            "target_category": self.target_category,
            "target_fingerprint": self.target_fingerprint,
            "deduplication_scope": self.deduplication_scope,
            "deduplication_key": self.deduplication_key,
            "idempotency": (self.idempotency.to_dict()
                            if self.idempotency is not None else None),
            "provenance": self.provenance.to_dict(),
            "predecessor_action_id": self.predecessor_action_id,
            "duplicate_risk_acknowledgement": (
                self.duplicate_risk_acknowledgement.to_dict()
                if self.duplicate_risk_acknowledgement is not None else None
            ),
            "authority": self.authority.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "transitions": [item.to_dict() for item in self.transitions],
            "reconciliation": self.reconciliation.to_dict(),
            "terminal_outcome": (self.terminal_outcome.to_dict()
                                 if self.terminal_outcome is not None else None),
            "evidence_refs": list(self.evidence_refs),
            "created_at": _format_utc(self.created_at, "created_at"),
            "updated_at": _format_utc(self.updated_at, "updated_at"),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActionRecord:
        expected = {
            "action_id", "state", "version", "action_kind", "tool_name",
            "tool_version", "eligibility", "mutation_capabilities",
            "reconciliation_capabilities", "effect_class", "intent_digest",
            "intent_ref", "target_category", "target_fingerprint",
            "deduplication_scope", "deduplication_key", "idempotency",
            "provenance", "predecessor_action_id",
            "duplicate_risk_acknowledgement", "authority", "attempts",
            "transitions", "reconciliation", "terminal_outcome",
            "evidence_refs", "created_at", "updated_at", "schema_version",
        }
        _require_exact_keys(data, expected, "action record")
        schema_version = _require_positive_int(
            data["schema_version"], "schema_version")
        if schema_version != ACTION_JOURNAL_SCHEMA_VERSION:
            raise ActionValidationError(
                "unsupported action journal schema version",
                schema_version=schema_version,
                supported=ACTION_JOURNAL_SCHEMA_VERSION)
        mutation_caps = _parse_categorical_list(
            data["mutation_capabilities"], MAX_CAPABILITY_NAME_CHARS,
            "mutation capabilities")
        reconciliation_caps = _parse_categorical_list(
            data["reconciliation_capabilities"],
            MAX_CAPABILITY_NAME_CHARS, "reconciliation capabilities")
        raw_attempts = data["attempts"]
        raw_transitions = data["transitions"]
        if not isinstance(raw_attempts, list):
            raise ActionValidationError("action attempts must be a list")
        if not isinstance(raw_transitions, list):
            raise ActionValidationError("action transitions must be a list")
        raw_idempotency = data["idempotency"]
        raw_provenance = data["provenance"]
        raw_duplicate_risk = data["duplicate_risk_acknowledgement"]
        raw_authority = data["authority"]
        raw_reconciliation = data["reconciliation"]
        raw_terminal = data["terminal_outcome"]
        for value, label in (
            (raw_provenance, "action provenance"),
            (raw_authority, "authority receipt"),
            (raw_reconciliation, "reconciliation metadata"),
        ):
            if not isinstance(value, Mapping):
                raise ActionValidationError(f"{label} must be a mapping")
        if raw_idempotency is not None and not isinstance(
                raw_idempotency, Mapping):
            raise ActionValidationError(
                "idempotency metadata must be a mapping")
        if raw_duplicate_risk is not None and not isinstance(
                raw_duplicate_risk, Mapping):
            raise ActionValidationError(
                "duplicate-risk acknowledgement must be a mapping")
        if raw_terminal is not None and not isinstance(raw_terminal, Mapping):
            raise ActionValidationError(
                "terminal outcome must be a mapping")
        return cls(
            action_id=_require_pattern(data["action_id"], _ACTION_ID,
                                       "action id"),
            state=_parse_enum(data["state"], ActionState, "action state"),
            version=_require_nonnegative_int(data["version"],
                                             "record version"),
            action_kind=_require_categorical(
                data["action_kind"], MAX_ACTION_KIND_CHARS, "action kind"),
            tool_name=_require_categorical(
                data["tool_name"], MAX_TOOL_NAME_CHARS, "tool name"),
            tool_version=_require_version(
                data["tool_version"], MAX_TOOL_VERSION_CHARS, "tool version"),
            eligibility=_parse_enum(
                data["eligibility"], ActionJournalEligibility,
                "journal eligibility"),
            mutation_capabilities=mutation_caps,
            reconciliation_capabilities=reconciliation_caps,
            effect_class=_parse_enum(
                data["effect_class"], ActionEffectClass, "effect class"),
            intent_digest=_require_digest(data["intent_digest"],
                                          "intent digest"),
            intent_ref=(_require_reference(
                data["intent_ref"], MAX_INTENT_REF_CHARS,
                "protected intent ref") if data["intent_ref"] is not None
                        else None),
            target_category=_require_categorical(
                data["target_category"], MAX_TARGET_CATEGORY_CHARS,
                "target category"),
            target_fingerprint=(_require_digest(
                data["target_fingerprint"], "target fingerprint")
                if data["target_fingerprint"] is not None else None),
            deduplication_scope=(_require_reference(
                data["deduplication_scope"],
                MAX_DEDUPLICATION_SCOPE_CHARS, "deduplication scope")
                if data["deduplication_scope"] is not None else None),
            deduplication_key=(_require_reference(
                data["deduplication_key"], MAX_DEDUPLICATION_KEY_CHARS,
                "deduplication key")
                if data["deduplication_key"] is not None else None),
            idempotency=(IdempotencyContract.from_dict(raw_idempotency)
                         if raw_idempotency is not None else None),
            provenance=ActionProvenance.from_dict(raw_provenance),
            predecessor_action_id=(_require_pattern(
                data["predecessor_action_id"], _ACTION_ID,
                "predecessor action id")
                if data["predecessor_action_id"] is not None else None),
            duplicate_risk_acknowledgement=(
                DuplicateRiskAcknowledgement.from_dict(raw_duplicate_risk)
                if raw_duplicate_risk is not None else None
            ),
            authority=AuthorityReceipt.from_dict(raw_authority),
            attempts=tuple(ActionAttempt.from_dict(item)
                           if isinstance(item, Mapping) else
                           _raise_mapping("action attempt")
                           for item in raw_attempts),
            transitions=tuple(ActionTransition.from_dict(item)
                              if isinstance(item, Mapping) else
                              _raise_mapping("action transition")
                              for item in raw_transitions),
            reconciliation=ReconciliationMetadata.from_dict(
                raw_reconciliation),
            terminal_outcome=(ActionOutcome.from_dict(raw_terminal)
                              if raw_terminal is not None else None),
            evidence_refs=_parse_reference_list(
                data["evidence_refs"], MAX_EVIDENCE_REF_CHARS,
                "action evidence refs"),
            created_at=_parse_utc(data["created_at"], "created_at"),
            updated_at=_parse_utc(data["updated_at"], "updated_at"),
            schema_version=schema_version,
        ).validate()


def new_action(
    *,
    action_kind: str,
    tool_name: str,
    tool_version: str,
    eligibility: ActionJournalEligibility,
    mutation_capabilities: tuple[str, ...],
    reconciliation_capabilities: tuple[str, ...],
    effect_class: ActionEffectClass,
    intent_digest: str,
    intent_ref: str | None,
    target_category: str,
    target_fingerprint: str | None,
    provenance: ActionProvenance,
    authority: AuthorityReceipt,
    deduplication_scope: str | None = None,
    deduplication_key: str | None = None,
    idempotency: IdempotencyContract | None = None,
    predecessor_action_id: str | None = None,
    duplicate_risk_acknowledgement: (
        DuplicateRiskAcknowledgement | None
    ) = None,
    clock: Clock | None = None,
    id_factory: Callable[[str], str] = new_id,
) -> ActionRecord:
    """Create one validated, fresh ``AUTHORIZED`` logical action record."""
    resolved_clock = clock or SystemClock()
    now = _ensure_utc(resolved_clock.now(), "clock.now")
    record = ActionRecord(
        action_id=_require_pattern(id_factory("act"), _ACTION_ID, "action id"),
        state=ActionState.AUTHORIZED,
        version=0,
        action_kind=action_kind,
        tool_name=tool_name,
        tool_version=tool_version,
        eligibility=eligibility,
        mutation_capabilities=mutation_capabilities,
        reconciliation_capabilities=reconciliation_capabilities,
        effect_class=effect_class,
        intent_digest=intent_digest,
        intent_ref=intent_ref,
        target_category=target_category,
        target_fingerprint=target_fingerprint,
        deduplication_scope=deduplication_scope,
        deduplication_key=deduplication_key,
        idempotency=idempotency,
        provenance=provenance,
        predecessor_action_id=predecessor_action_id,
        duplicate_risk_acknowledgement=duplicate_risk_acknowledgement,
        authority=authority,
        attempts=(),
        transitions=(),
        reconciliation=ReconciliationMetadata(),
        terminal_outcome=None,
        evidence_refs=(),
        created_at=now,
        updated_at=now,
    )
    return record.validate()


def new_action_attempt_id(
    *, id_factory: Callable[[str], str] = new_id,
) -> str:
    """Mint an action-attempt ID in the stable ``aat_`` namespace."""
    return _require_pattern(id_factory("aat"), _ACTION_ATTEMPT_ID,
                            "action attempt id")


def new_action_transition_id(
    *, id_factory: Callable[[str], str] = new_id,
) -> str:
    return _require_pattern(id_factory("atx"), _ACTION_TRANSITION_ID,
                            "action transition id")


def new_duplicate_risk_acknowledgement_id(
    *, id_factory: Callable[[str], str] = new_id,
) -> str:
    """Mint a receipt ID in the stable ``dra_`` namespace."""
    return _require_pattern(
        id_factory("dra"),
        _DUPLICATE_RISK_ACKNOWLEDGEMENT_ID,
        "duplicate-risk acknowledgement id",
    )


def require_action_id(value: Any) -> str:
    return _require_pattern(value, _ACTION_ID, "action id")


def validate_action_successor(current: ActionRecord,
                              candidate: ActionRecord) -> None:
    """Prove that ``candidate`` is exactly one legal append-only CAS step."""
    if not isinstance(current, ActionRecord) or \
            not isinstance(candidate, ActionRecord):
        raise ActionValidationError("action CAS record is invalid")
    current.validate()
    candidate.validate()
    immutable_current = (
        current.action_id,
        current.action_kind,
        current.tool_name,
        current.tool_version,
        current.eligibility,
        current.mutation_capabilities,
        current.reconciliation_capabilities,
        current.effect_class,
        current.intent_digest,
        current.intent_ref,
        current.target_category,
        current.target_fingerprint,
        current.deduplication_scope,
        current.deduplication_key,
        current.idempotency,
        current.provenance,
        current.predecessor_action_id,
        current.duplicate_risk_acknowledgement,
        current.authority,
        current.created_at,
        current.schema_version,
    )
    immutable_candidate = (
        candidate.action_id,
        candidate.action_kind,
        candidate.tool_name,
        candidate.tool_version,
        candidate.eligibility,
        candidate.mutation_capabilities,
        candidate.reconciliation_capabilities,
        candidate.effect_class,
        candidate.intent_digest,
        candidate.intent_ref,
        candidate.target_category,
        candidate.target_fingerprint,
        candidate.deduplication_scope,
        candidate.deduplication_key,
        candidate.idempotency,
        candidate.provenance,
        candidate.predecessor_action_id,
        candidate.duplicate_risk_acknowledgement,
        candidate.authority,
        candidate.created_at,
        candidate.schema_version,
    )
    if immutable_candidate != immutable_current:
        raise ActionValidationError(
            "action CAS cannot change immutable intent fields",
            action_id=current.action_id)
    if candidate.version != current.version + 1:
        raise ActionValidationError(
            "action CAS must advance exactly one version",
            action_id=current.action_id)
    if candidate.transitions[:-1] != current.transitions or \
            len(candidate.transitions) != len(current.transitions) + 1:
        raise ActionValidationError(
            "action CAS must append exactly one transition",
            action_id=current.action_id)
    if candidate.evidence_refs[:len(current.evidence_refs)] != \
            current.evidence_refs:
        raise ActionValidationError(
            "action CAS cannot remove or rewrite evidence",
            action_id=current.action_id)
    last = candidate.transitions[-1]
    if candidate.evidence_refs[len(current.evidence_refs):] != \
            last.evidence_refs:
        raise ActionValidationError(
            "action CAS evidence suffix does not match transition",
            action_id=current.action_id)
    if last.to_state is ActionState.IN_FLIGHT:
        if candidate.attempts[:-1] != current.attempts or \
                len(candidate.attempts) != len(current.attempts) + 1:
            raise ActionValidationError(
                "attempt claim must append exactly one fresh attempt",
                action_id=current.action_id)
    else:
        if len(candidate.attempts) != len(current.attempts):
            raise ActionValidationError(
                "non-claim transition cannot change attempt count",
                action_id=current.action_id)
        if current.attempts:
            if candidate.attempts[:-1] != current.attempts[:-1]:
                raise ActionValidationError(
                    "action CAS can update only the current attempt",
                    action_id=current.action_id)
            before = current.attempts[-1]
            after = candidate.attempts[-1]
            if (
                before.attempt_id,
                before.action_id,
                before.invocation_id,
                before.owner_id,
                before.owner_epoch,
                before.claimed_at,
            ) != (
                after.attempt_id,
                after.action_id,
                after.invocation_id,
                after.owner_id,
                after.owner_epoch,
                after.claimed_at,
            ):
                raise ActionValidationError(
                    "action CAS cannot rewrite attempt identity",
                    action_id=current.action_id)
        elif candidate.attempts:
            raise ActionValidationError(
                "non-claim transition fabricated an attempt",
                action_id=current.action_id)


def deduplication_compatible(first: ActionRecord,
                             second: ActionRecord) -> bool:
    """Whether two trusted create requests describe the same logical intent."""
    first.validate()
    second.validate()
    return (
        first.action_kind,
        first.tool_name,
        first.tool_version,
        first.eligibility,
        first.mutation_capabilities,
        first.reconciliation_capabilities,
        first.effect_class,
        first.intent_digest,
        first.intent_ref,
        first.target_category,
        first.target_fingerprint,
        first.deduplication_identity,
        first.idempotency,
        first.predecessor_action_id,
        first.duplicate_risk_acknowledgement,
    ) == (
        second.action_kind,
        second.tool_name,
        second.tool_version,
        second.eligibility,
        second.mutation_capabilities,
        second.reconciliation_capabilities,
        second.effect_class,
        second.intent_digest,
        second.intent_ref,
        second.target_category,
        second.target_fingerprint,
        second.deduplication_identity,
        second.idempotency,
        second.predecessor_action_id,
        second.duplicate_risk_acknowledgement,
    )


def _has_unresolved_action_ambiguity(
    transitions: tuple[ActionTransition, ...],
) -> bool:
    """Derive logical ambiguity without rewriting physical attempt verdicts."""
    unresolved = False
    for transition in transitions:
        if transition.attempt_id is not None and \
                transition.effect_verdict is EffectVerdict.OUTCOME_UNKNOWN:
            unresolved = True
        if transition.from_state is ActionState.RECONCILE_REQUIRED and \
                transition.effect_verdict is (
                    EffectVerdict.CONFIRMED_NOT_APPLIED):
            unresolved = False
    return unresolved


def _validate_state_outcome_shape(transition: ActionTransition) -> None:
    target = transition.to_state
    verdict = transition.effect_verdict
    outcome = transition.terminal_outcome
    if target is ActionState.SUCCEEDED:
        if verdict is not EffectVerdict.CONFIRMED_APPLIED or outcome is None:
            raise ActionValidationError(
                "successful action requires confirmed-applied outcome")
    elif target is ActionState.FAILED:
        if verdict not in {
            EffectVerdict.CONFIRMED_NOT_APPLIED,
            EffectVerdict.CONFIRMED_PARTIAL,
        } or outcome is None:
            raise ActionValidationError(
                "failed action requires a confirmed failure/partial outcome")
    elif target is ActionState.CANCELLED:
        if verdict is not None or outcome is not None:
            raise ActionValidationError(
                "cancelled action cannot carry an effect outcome")
    elif target is ActionState.RECONCILE_REQUIRED:
        if verdict is not EffectVerdict.OUTCOME_UNKNOWN or outcome is not None:
            raise ActionValidationError(
                "reconciliation requires an unknown effect verdict")
    elif target is ActionState.IN_FLIGHT:
        if verdict is not None or outcome is not None or \
                transition.attempt_status is not ActionAttemptStatus.CLAIMED:
            raise ActionValidationError(
                "attempt claim carries impossible effect evidence")
    elif outcome is not None:
        raise ActionValidationError(
            "nonterminal action carries a terminal outcome")


def _validate_transition_semantics(
    transition: ActionTransition,
    *,
    effect_class: ActionEffectClass,
    idempotency: IdempotencyContract | None,
    prior_attempt_status: ActionAttemptStatus | None,
    unresolved_attempt_ambiguity: bool,
) -> None:
    transition.validate()
    source = transition.from_state
    target = transition.to_state
    proof = transition.proof
    verdict = transition.effect_verdict
    status = transition.attempt_status

    if source is ActionState.AUTHORIZED:
        if target is ActionState.IN_FLIGHT:
            if transition.attempt_id is None or \
                    status is not ActionAttemptStatus.CLAIMED:
                raise ActionValidationError(
                    "attempt claim transition is incomplete")
        elif transition.attempt_id is not None:
            raise ActionValidationError(
                "authorized direct outcome cannot update an old attempt")
        if target is ActionState.FAILED and verdict is not (
                EffectVerdict.CONFIRMED_NOT_APPLIED):
            raise ActionValidationError(
                "pre-invocation failure must confirm no effect")
        if target is ActionState.FAILED and unresolved_attempt_ambiguity:
            raise ActionValidationError(
                "local failure cannot erase an earlier unknown attempt")
    elif source is ActionState.IN_FLIGHT:
        if transition.attempt_id is None or status is None:
            raise ActionValidationError(
                "in-flight outcome must correlate the current attempt")
        if target is ActionState.FAILED and unresolved_attempt_ambiguity:
            raise ActionValidationError(
                "attempt-local failure cannot erase an earlier unknown "
                "attempt")
    elif source is ActionState.RECONCILE_REQUIRED and (
            transition.attempt_id is not None or status is not None):
        raise ActionValidationError(
            "reconciliation cannot rewrite physical attempt truth")

    if target is ActionState.AUTHORIZED:
        if proof not in {
            ActionTransitionProof.DEFINITIVE_NO_EFFECT,
            ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
        }:
            raise ActionValidationError(
                "retry authorization requires explicit trusted proof")
        if proof is ActionTransitionProof.DEFINITIVE_NO_EFFECT:
            if verdict is not EffectVerdict.CONFIRMED_NOT_APPLIED:
                raise ActionValidationError(
                    "definitive retry proof requires confirmed no effect")
            if source is ActionState.IN_FLIGHT and \
                    unresolved_attempt_ambiguity:
                raise ActionValidationError(
                    "definitive no-effect proof cannot erase an earlier "
                    "unknown attempt")
        else:
            if effect_class is not ActionEffectClass.EXTERNALLY_IDEMPOTENT or \
                    idempotency is None:
                raise ActionValidationError(
                    "external retry proof lacks an idempotency contract")
            idempotency.validate()
            if idempotency.valid_until is not None and \
                    transition.at >= idempotency.valid_until:
                raise ActionValidationError(
                    "external idempotency guarantee has expired")
            if verdict not in {
                EffectVerdict.OUTCOME_UNKNOWN,
                EffectVerdict.CONFIRMED_NOT_APPLIED,
            }:
                raise ActionValidationError(
                    "idempotent retry carries an incompatible verdict")
    elif target is ActionState.CANCELLED:
        if source is ActionState.IN_FLIGHT:
            if proof is not ActionTransitionProof.LIVE_OWNER_NOT_INVOKED or \
                    status is not ActionAttemptStatus.NOT_INVOKED:
                raise ActionValidationError(
                    "claimed cancellation requires live NOT_INVOKED proof")
        elif proof is not ActionTransitionProof.NONE:
            raise ActionValidationError(
                "pre-attempt cancellation carries an irrelevant proof")
    elif proof is not ActionTransitionProof.NONE:
        raise ActionValidationError(
            "transition proof is not valid for this lifecycle edge")

    if source is ActionState.IN_FLIGHT and target is not ActionState.IN_FLIGHT:
        if status is ActionAttemptStatus.CLAIMED:
            raise ActionValidationError(
                "an attempt outcome cannot remain merely claimed")
    if source is ActionState.IN_FLIGHT and \
            target is ActionState.RECONCILE_REQUIRED and status not in {
        ActionAttemptStatus.INVOKED,
        ActionAttemptStatus.POSSIBLY_INVOKED,
    }:
        raise ActionValidationError(
            "unknown outcome requires invoked-or-possible attempt truth")
    if source is ActionState.IN_FLIGHT and verdict in {
        EffectVerdict.CONFIRMED_APPLIED,
        EffectVerdict.CONFIRMED_PARTIAL,
    } and status is not ActionAttemptStatus.INVOKED:
        raise ActionValidationError(
            "applied effect evidence requires confirmed seam invocation")
    if prior_attempt_status is not None and status is not None:
        _validate_attempt_status_change(prior_attempt_status, status)


def _validate_reconciliation_progress(
    before: ReconciliationMetadata,
    after: ReconciliationMetadata,
    *,
    source: ActionState,
    target: ActionState,
    has_new_evidence: bool,
) -> None:
    if target is not ActionState.RECONCILE_REQUIRED or \
            source is not ActionState.RECONCILE_REQUIRED:
        return
    if after.claim_epoch < before.claim_epoch:
        raise ActionValidationError(
            "reconciliation claim epoch would regress")
    if after.probe_count < before.probe_count:
        raise ActionValidationError(
            "reconciliation probe count would regress")
    if after.claim_owner_id is not None and \
            after.claim_owner_id != before.claim_owner_id and \
            after.claim_epoch <= before.claim_epoch:
        raise ActionValidationError(
            "a new reconciliation owner requires a fresh claim epoch")
    if after == before and not has_new_evidence:
        raise ActionValidationError(
            "reconciliation self-transition carries no bounded progress")


def _validate_retry_claim_horizon(
    prior_transition: ActionTransition,
    claimed_at: datetime,
    idempotency: IdempotencyContract | None,
) -> None:
    if prior_transition.to_state is not ActionState.AUTHORIZED or \
            prior_transition.proof is not (
                ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE):
        return
    if idempotency is None:
        raise ActionValidationError(
            "retry attempt lacks its idempotency contract")
    if idempotency.valid_until is not None and \
            claimed_at >= idempotency.valid_until:
        raise ActionValidationError(
            "idempotency guarantee expired before retry attempt claim")


def _evolve_attempt(
    attempt: ActionAttempt,
    *,
    status: ActionAttemptStatus,
    effect_verdict: EffectVerdict | None,
    evidence_refs: tuple[str, ...],
    at: datetime,
) -> ActionAttempt:
    _validate_attempt_status_change(attempt.status, status)
    verdict = _evolved_verdict(attempt.effect_verdict, effect_verdict)
    return replace(
        attempt,
        status=status,
        effect_verdict=verdict,
        evidence_refs=attempt.evidence_refs + evidence_refs,
        updated_at=at,
    ).validate()


def _validate_attempt_status_change(
    before: ActionAttemptStatus,
    after: ActionAttemptStatus,
) -> None:
    allowed: Mapping[ActionAttemptStatus, frozenset[ActionAttemptStatus]] = {
        ActionAttemptStatus.CLAIMED: frozenset({
            ActionAttemptStatus.NOT_INVOKED,
            ActionAttemptStatus.INVOKED,
            ActionAttemptStatus.POSSIBLY_INVOKED,
        }),
        ActionAttemptStatus.NOT_INVOKED: frozenset({
            ActionAttemptStatus.NOT_INVOKED,
        }),
        ActionAttemptStatus.INVOKED: frozenset({ActionAttemptStatus.INVOKED}),
        ActionAttemptStatus.POSSIBLY_INVOKED: frozenset({
            ActionAttemptStatus.POSSIBLY_INVOKED,
            ActionAttemptStatus.INVOKED,
        }),
    }
    if after not in allowed[before]:
        raise ActionValidationError(
            "action attempt status would regress",
            before=before.value, after=after.value)


def _evolved_verdict(
    before: EffectVerdict | None,
    supplied: EffectVerdict | None,
) -> EffectVerdict | None:
    if supplied is None:
        return before
    if before is None or before is supplied:
        return supplied
    if before is EffectVerdict.OUTCOME_UNKNOWN and supplied in {
        EffectVerdict.CONFIRMED_APPLIED,
        EffectVerdict.CONFIRMED_NOT_APPLIED,
        EffectVerdict.CONFIRMED_PARTIAL,
    }:
        return supplied
    raise ActionValidationError(
        "effect verdict would rewrite established truth")


def _validated_new_evidence(
    values: tuple[str, ...],
    *,
    existing: tuple[str, ...],
) -> tuple[str, ...]:
    _validate_reference_tuple(
        values,
        maximum_items=MAX_EVIDENCE_REFS,
        maximum_chars=MAX_EVIDENCE_REF_CHARS,
        field_name="new evidence refs",
    )
    if len(existing) + len(values) > MAX_EVIDENCE_REFS:
        raise ActionValidationError("action evidence bound reached",
                                    maximum=MAX_EVIDENCE_REFS)
    if set(existing).intersection(values):
        raise ActionValidationError(
            "action evidence reference is duplicated")
    return values


def _validate_reference_tuple(
    values: tuple[str, ...],
    *,
    maximum_items: int,
    maximum_chars: int,
    field_name: str,
) -> None:
    if not isinstance(values, tuple):
        raise ActionValidationError(f"{field_name} must be a tuple")
    if len(values) > maximum_items:
        raise ActionValidationError(f"{field_name} bound exceeded",
                                    maximum=maximum_items)
    seen: set[str] = set()
    for value in values:
        _require_reference(value, maximum_chars, field_name)
        if value in seen:
            raise ActionValidationError(
                f"{field_name} contains a duplicate")
        seen.add(value)


def _validate_categorical_tuple(
    values: tuple[str, ...],
    *,
    maximum_items: int,
    maximum_chars: int,
    field_name: str,
    require_nonempty: bool = False,
) -> None:
    if not isinstance(values, tuple):
        raise ActionValidationError(f"{field_name} must be a tuple")
    if require_nonempty and not values:
        raise ActionValidationError(f"{field_name} must not be empty")
    if len(values) > maximum_items:
        raise ActionValidationError(f"{field_name} bound exceeded",
                                    maximum=maximum_items)
    normalized = tuple(sorted(set(values)))
    if normalized != values:
        raise ActionValidationError(
            f"{field_name} must be sorted and unique")
    for value in values:
        _require_categorical(value, maximum_chars, field_name)


def _parse_reference_list(value: Any, maximum_chars: int,
                          field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ActionValidationError(f"{field_name} must be a list")
    return tuple(_require_reference(item, maximum_chars, field_name)
                 for item in value)


def _parse_categorical_list(value: Any, maximum_chars: int,
                            field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ActionValidationError(f"{field_name} must be a list")
    return tuple(_require_categorical(item, maximum_chars, field_name)
                 for item in value)


def _require_exact_keys(data: Mapping[str, Any], expected: set[str],
                        label: str) -> None:
    if not isinstance(data, Mapping):
        raise ActionValidationError(f"{label} must be a mapping")
    keys = tuple(data)
    known = {key for key in keys if isinstance(key, str)}
    unknown = tuple(key for key in keys
                    if not isinstance(key, str) or key not in expected)
    if known != expected or unknown:
        raise ActionValidationError(
            f"{label} fields are invalid",
            missing=sorted(expected - known),
            unknown_count=len(unknown),
            unknown_types=sorted({type(key).__name__ for key in unknown}),
        )


def _require_pattern(value: Any, pattern: re.Pattern[str],
                     field_name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ActionValidationError(f"{field_name} is invalid")
    return value


def _validate_optional_pattern(value: Any, pattern: re.Pattern[str],
                               field_name: str) -> None:
    if value is not None:
        _require_pattern(value, pattern, field_name)


def _optional_pattern(value: Any, pattern: re.Pattern[str],
                      field_name: str) -> str | None:
    return None if value is None else _require_pattern(value, pattern,
                                                        field_name)


def _require_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _LOWER_HEX_DIGEST.fullmatch(value):
        raise ActionValidationError(f"{field_name} is invalid")
    return value


def _require_categorical(value: Any, maximum: int,
                         field_name: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or \
            not _CATEGORICAL.fullmatch(value):
        raise ActionValidationError(f"{field_name} is invalid",
                                    maximum=maximum)
    return value


def _require_reference(value: Any, maximum: int, field_name: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or \
            not _OPAQUE_REFERENCE.fullmatch(value):
        raise ActionValidationError(f"{field_name} is invalid",
                                    maximum=maximum)
    return value


def _require_version(value: Any, maximum: int, field_name: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or \
            not _VERSION_TOKEN.fullmatch(value):
        raise ActionValidationError(f"{field_name} is invalid",
                                    maximum=maximum)
    return value


def _require_text(value: Any, maximum: int, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(
            ord(character) < 32 or ord(character) == 127
            for character in value):
        raise ActionValidationError(f"{field_name} is invalid",
                                    maximum=maximum)
    return value


def _require_enum(value: Any, enum_type: type[Enum],
                  field_name: str):
    if not isinstance(value, enum_type):
        raise ActionValidationError(f"{field_name} is invalid")
    return value


def _parse_enum(value: Any, enum_type: type[Enum], field_name: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ActionValidationError(f"{field_name} is invalid") from exc


def _require_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or \
            not 0 <= value <= MAX_COUNTER_VALUE:
        raise ActionValidationError(
            f"{field_name} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or \
            not 1 <= value <= MAX_COUNTER_VALUE:
        raise ActionValidationError(
            f"{field_name} must be a positive integer")
    return value


def _ensure_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or \
            value.utcoffset() != timedelta(0):
        raise ActionValidationError(f"{field_name} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ActionValidationError(
            f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ActionValidationError(f"{field_name} is invalid") from exc
    return _ensure_utc(parsed, field_name)


def _format_utc(value: datetime, field_name: str) -> str:
    return _ensure_utc(value, field_name).isoformat()


def _raise_mapping(label: str):
    raise ActionValidationError(f"{label} must be a mapping")
