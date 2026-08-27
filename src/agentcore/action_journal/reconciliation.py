"""Bounded read-only reconciliation contracts for ActionJournal v1.

These values carry only categorical truth and opaque identifiers. A recovery
claim fences one probe publication; it is never permission to execute a tool
or mutation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from agentcore.action_journal.errors import (
    ActionEffectNormalizationError,
    ActionValidationError,
)
from agentcore.action_journal.models import (
    INTENT_DIGEST_HEX_CHARS,
    MAX_ACTION_KIND_CHARS,
    MAX_EVIDENCE_REFS,
    MAX_EVIDENCE_REF_CHARS,
    MAX_INTENT_REF_CHARS,
    MAX_PARTIAL_CATEGORIES,
    MAX_PARTIAL_CATEGORY_CHARS,
    MAX_RECONCILIATION_OWNER_CHARS,
    MAX_RESULT_CLASS_CHARS,
    MAX_RESULT_SUMMARY_CHARS,
    MAX_TARGET_CATEGORY_CHARS,
    MAX_TOOL_NAME_CHARS,
    MAX_TOOL_VERSION_CHARS,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionOutcome,
    ActionRecord,
    ActionState,
    EffectVerdict,
    ReconciliationDisposition,
    require_action_id,
)
from agentcore.action_journal.protected_storage import (
    is_evidence_reference,
    looks_like_evidence_reference,
    looks_like_protected_payload_reference,
)


_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CATEGORICAL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_VERSION_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+=-]*$")


@dataclass(frozen=True)
class ActionRecoveryClaim:
    """Exact post-CAS ownership token for one reconciliation epoch."""

    action_id: str
    record_version: int
    claim_owner_id: str
    claim_epoch: int

    def validate(self) -> ActionRecoveryClaim:
        require_action_id(self.action_id)
        _positive_int(self.record_version, "recovery record version")
        _reference(
            self.claim_owner_id,
            MAX_RECONCILIATION_OWNER_CHARS,
            "recovery claim owner",
        )
        _positive_int(self.claim_epoch, "recovery claim epoch")
        return self


@dataclass(frozen=True)
class ReconciliationAttemptEvidence:
    """Safe immutable attempt facts exposed to a trusted reconciler."""

    attempt_id: str
    status: ActionAttemptStatus
    effect_verdict: EffectVerdict | None
    evidence_refs: tuple[str, ...]

    def validate(self) -> ReconciliationAttemptEvidence:
        if not isinstance(self.attempt_id, str) or not re.fullmatch(
            r"aat_[0-9a-f]{12}", self.attempt_id
        ):
            raise ActionValidationError("reconciliation attempt id is invalid")
        if not isinstance(self.status, ActionAttemptStatus):
            raise ActionValidationError("reconciliation attempt status is invalid")
        if self.effect_verdict is not None and not isinstance(
            self.effect_verdict, EffectVerdict
        ):
            raise ActionValidationError("reconciliation attempt verdict is invalid")
        _reference_tuple(self.evidence_refs, "reconciliation attempt evidence")
        return self


@dataclass(frozen=True)
class ProtectedEvidenceView:
    """Detached protected evidence exposed only to a trusted reconciler."""

    reference: str
    evidence: Mapping[str, object] = field(repr=False, compare=False)

    def validate(self) -> ProtectedEvidenceView:
        if not is_evidence_reference(self.reference) or not isinstance(
                self.evidence, Mapping):
            raise ActionValidationError(
                "protected reconciliation evidence is invalid")
        return self


@dataclass(frozen=True)
class ActionReconciliationContext:
    """Immutable public facts plus executor-resolved protected probe input."""

    action_id: str
    action_kind: str
    tool_name: str
    tool_version: str
    effect_class: ActionEffectClass
    intent_digest: str
    intent_ref: str | None
    target_category: str
    target_fingerprint: str | None
    attempts: tuple[ReconciliationAttemptEvidence, ...]
    evidence_refs: tuple[str, ...]
    protected_payload: Mapping[str, object] | None = field(
        default=None, repr=False, compare=False)
    protected_evidence: tuple[ProtectedEvidenceView, ...] = field(
        default=(), repr=False, compare=False)

    @classmethod
    def from_record(cls, record: ActionRecord) -> ActionReconciliationContext:
        if not isinstance(record, ActionRecord):
            raise ActionValidationError("reconciliation action record is invalid")
        record.validate()
        if record.state is not ActionState.RECONCILE_REQUIRED:
            raise ActionValidationError(
                "reconciliation context requires unresolved action truth"
            )
        return cls(
            action_id=record.action_id,
            action_kind=record.action_kind,
            tool_name=record.tool_name,
            tool_version=record.tool_version,
            effect_class=record.effect_class,
            intent_digest=record.intent_digest,
            intent_ref=record.intent_ref,
            target_category=record.target_category,
            target_fingerprint=record.target_fingerprint,
            attempts=tuple(
                ReconciliationAttemptEvidence(
                    attempt_id=attempt.attempt_id,
                    status=attempt.status,
                    effect_verdict=attempt.effect_verdict,
                    evidence_refs=attempt.evidence_refs,
                ).validate()
                for attempt in record.attempts
            ),
            evidence_refs=record.evidence_refs,
        ).validate()

    def validate(self) -> ActionReconciliationContext:
        require_action_id(self.action_id)
        _categorical(self.action_kind, MAX_ACTION_KIND_CHARS, "action kind")
        _categorical(self.tool_name, MAX_TOOL_NAME_CHARS, "tool name")
        _version(self.tool_version, MAX_TOOL_VERSION_CHARS, "tool version")
        if not isinstance(self.effect_class, ActionEffectClass):
            raise ActionValidationError("reconciliation effect class is invalid")
        _digest(self.intent_digest, "intent digest")
        if self.intent_ref is not None:
            _reference(self.intent_ref, MAX_INTENT_REF_CHARS, "intent ref")
        _categorical(
            self.target_category,
            MAX_TARGET_CATEGORY_CHARS,
            "target category",
        )
        if self.target_fingerprint is not None:
            _digest(self.target_fingerprint, "target fingerprint")
        if not isinstance(self.attempts, tuple):
            raise ActionValidationError("reconciliation attempts must be a tuple")
        for attempt in self.attempts:
            if not isinstance(attempt, ReconciliationAttemptEvidence):
                raise ActionValidationError(
                    "reconciliation attempt evidence is invalid"
                )
            attempt.validate()
        _reference_tuple(self.evidence_refs, "reconciliation action evidence")
        if self.protected_payload is not None and not isinstance(
                self.protected_payload, Mapping):
            raise ActionValidationError(
                "protected reconciliation payload is invalid")
        if not isinstance(self.protected_evidence, tuple):
            raise ActionValidationError(
                "protected reconciliation evidence is invalid")
        for item in self.protected_evidence:
            if not isinstance(item, ProtectedEvidenceView):
                raise ActionValidationError(
                    "protected reconciliation evidence is invalid")
            item.validate()
        return self


@dataclass(frozen=True)
class ActionReconciliationResult:
    """Trusted categorical probe truth; never a provider response envelope."""

    verdict: EffectVerdict
    result_class: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    partial_categories: tuple[str, ...] = ()
    reconciliation_disposition: ReconciliationDisposition = (
        ReconciliationDisposition.NONE
    )
    retry_safe: bool = False
    protected_evidence: tuple[Mapping[str, object], ...] = field(
        default=(), repr=False, compare=False)

    def validate(self) -> ActionReconciliationResult:
        if not isinstance(self.verdict, EffectVerdict):
            raise ActionEffectNormalizationError(
                "reconciliation verdict is invalid"
            )
        _categorical(
            self.result_class,
            MAX_RESULT_CLASS_CHARS,
            "reconciliation result class",
            normalization=True,
        )
        _safe_text(
            self.summary,
            MAX_RESULT_SUMMARY_CHARS,
            "reconciliation summary",
        )
        _reference_tuple(
            self.evidence_refs,
            "reconciliation evidence",
            normalization=True,
        )
        if any(looks_like_evidence_reference(reference)
               or looks_like_protected_payload_reference(reference)
               for reference in self.evidence_refs):
            raise ActionEffectNormalizationError(
                "protected references are minted by trusted storage")
        if not isinstance(self.protected_evidence, tuple) or len(
                self.protected_evidence) > MAX_EVIDENCE_REFS or any(
                    not isinstance(item, Mapping)
                    for item in self.protected_evidence):
            raise ActionEffectNormalizationError(
                "protected reconciliation evidence is invalid")
        if len(self.evidence_refs) + len(self.protected_evidence) > (
                MAX_EVIDENCE_REFS):
            raise ActionEffectNormalizationError(
                "reconciliation evidence bound is exceeded")
        _categorical_tuple(self.partial_categories)
        if not isinstance(
            self.reconciliation_disposition, ReconciliationDisposition
        ):
            raise ActionEffectNormalizationError(
                "reconciliation disposition is invalid"
            )
        if not isinstance(self.retry_safe, bool):
            raise ActionEffectNormalizationError(
                "reconciliation retry safety is invalid"
            )

        if self.verdict is EffectVerdict.OUTCOME_UNKNOWN:
            if self.reconciliation_disposition is ReconciliationDisposition.NONE:
                raise ActionEffectNormalizationError(
                    "unknown reconciliation lacks a disposition"
                )
            if self.partial_categories or self.retry_safe:
                raise ActionEffectNormalizationError(
                    "unknown reconciliation carries resolved truth"
                )
            return self

        if self.reconciliation_disposition is not ReconciliationDisposition.NONE:
            raise ActionEffectNormalizationError(
                "resolved reconciliation carries an unresolved disposition"
            )
        if self.retry_safe and self.verdict is not (
            EffectVerdict.CONFIRMED_NOT_APPLIED
        ):
            raise ActionEffectNormalizationError(
                "retry safety requires confirmed absence"
            )
        try:
            ActionOutcome(
                verdict=self.verdict,
                result_class=self.result_class,
                summary=self.summary,
                partial_categories=self.partial_categories,
            ).validate()
        except ActionValidationError as exc:
            raise ActionEffectNormalizationError(
                "resolved reconciliation result is invalid"
            ) from exc
        return self


class ActionReconciler(Protocol):
    """Trusted read-only capability reconciler."""

    def probe(
        self,
        context: ActionReconciliationContext,
    ) -> ActionReconciliationResult: ...


class ActionReconciliationStatus(str, Enum):
    SETTLED_SUCCEEDED = "settled_succeeded"
    SETTLED_FAILED = "settled_failed"
    RETRY_AUTHORIZED = "retry_authorized"
    UNRESOLVED = "unresolved"
    PROBE_DENIED = "probe_denied"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    PROBE_FAILED = "probe_failed"
    STALE_CLAIM = "stale_claim"
    PUBLICATION_UNCERTAIN = "publication_uncertain"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ActionReconciliationReport:
    """Safe categorical result returned to the recovery coordinator."""

    action_id: str
    status: ActionReconciliationStatus
    action_state: ActionState
    record_version: int
    claim_epoch: int

    def validate(self) -> ActionReconciliationReport:
        require_action_id(self.action_id)
        if not isinstance(self.status, ActionReconciliationStatus):
            raise ActionValidationError("reconciliation report status is invalid")
        if not isinstance(self.action_state, ActionState):
            raise ActionValidationError("reconciliation report state is invalid")
        _nonnegative_int(self.record_version, "reconciliation report version")
        _positive_int(self.claim_epoch, "reconciliation report claim epoch")
        return self


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != INTENT_DIGEST_HEX_CHARS
        or not _LOWER_HEX_DIGEST.fullmatch(value)
    ):
        raise ActionValidationError(f"{field} is invalid")
    return value


def _categorical(
    value: object,
    maximum: int,
    field: str,
    *,
    normalization: bool = False,
) -> str:
    error = ActionEffectNormalizationError if normalization else ActionValidationError
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _CATEGORICAL.fullmatch(value)
    ):
        raise error(f"{field} is invalid")
    return value


def _version(value: object, maximum: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _VERSION_TOKEN.fullmatch(value)
    ):
        raise ActionValidationError(f"{field} is invalid")
    return value


def _reference(value: object, maximum: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _OPAQUE_REFERENCE.fullmatch(value)
    ):
        raise ActionValidationError(f"{field} is invalid")
    return value


def _reference_tuple(
    values: object,
    field: str,
    *,
    normalization: bool = False,
) -> None:
    error = ActionEffectNormalizationError if normalization else ActionValidationError
    if not isinstance(values, tuple) or len(values) > MAX_EVIDENCE_REFS:
        raise error(f"{field} is invalid")
    if len(set(values)) != len(values):
        raise error(f"{field} contains duplicates")
    for value in values:
        try:
            _reference(value, MAX_EVIDENCE_REF_CHARS, field)
        except ActionValidationError as exc:
            raise error(f"{field} is invalid") from exc


def _categorical_tuple(values: object) -> None:
    if (
        not isinstance(values, tuple)
        or len(values) > MAX_PARTIAL_CATEGORIES
        or tuple(sorted(set(values))) != values
    ):
        raise ActionEffectNormalizationError("partial categories are invalid")
    for value in values:
        _categorical(
            value,
            MAX_PARTIAL_CATEGORY_CHARS,
            "partial category",
            normalization=True,
        )


def _safe_text(value: object, maximum: int, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ActionEffectNormalizationError(f"{field} is invalid")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActionValidationError(f"{field} is invalid")
    return value


def _positive_int(value: object, field: str) -> int:
    number = _nonnegative_int(value, field)
    if number == 0:
        raise ActionValidationError(f"{field} is invalid")
    return number


__all__ = [
    "ActionReconciler",
    "ActionReconciliationContext",
    "ActionReconciliationReport",
    "ActionReconciliationResult",
    "ActionReconciliationStatus",
    "ActionRecoveryClaim",
    "ProtectedEvidenceView",
    "ReconciliationAttemptEvidence",
]
