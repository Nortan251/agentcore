from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agentcore.action_journal.store as store_module
from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION,
    LEGAL_ACTION_TRANSITIONS,
    MAX_ACTION_ATTEMPTS,
    MAX_ACTION_TRANSITIONS,
    ActionAttemptStatus,
    ActionDeduplicationConflictError,
    ActionEffectClass,
    ActionJournalEligibility,
    ActionNotFoundError,
    ActionOutcome,
    ActionProvenance,
    ActionRecord,
    ActionState,
    ActionStore,
    ActionStoreError,
    ActionStoreOwnershipError,
    ActionTransition,
    ActionTransitionProof,
    ActionValidationError,
    ActionVersionConflictError,
    AuthorityReceipt,
    DuplicateRiskAcknowledgement,
    EffectVerdict,
    IdempotencyContract,
    InMemoryActionStore,
    JsonFileActionStore,
    ReconciliationDisposition,
    ReconciliationMetadata,
    new_action,
    new_action_attempt_id,
    new_duplicate_risk_acknowledgement_id,
)
from agentcore.realtime import TURN_SCHEMA_VERSION
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _id(prefix: str, number: int) -> str:
    return f"{prefix}_{number:012x}"


def _ids(number: int):
    return lambda prefix: _id(prefix, number)


def _authority(number: int = 1) -> AuthorityReceipt:
    return AuthorityReceipt(
        receipt_id=_id("auth", number),
        binding_digest=f"{number:064x}",
        policy_fingerprint=f"{number + 1:064x}",
        scope_ref=f"workspace:{number}",
        authorized_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        approval_request_id=f"approval:{number}",
    )


def _duplicate_risk_acknowledgement(
    predecessor_number: int = 1,
    number: int = 2,
) -> DuplicateRiskAcknowledgement:
    return DuplicateRiskAcknowledgement(
        acknowledgement_id=_id("dra", number),
        predecessor_action_id=_id("act", predecessor_number),
        approval_request_id=f"approval:duplicate-risk:{number}",
        binding_digest=f"{number + 30:064x}",
        acknowledged_at=NOW,
    )


def _provenance(number: int = 1) -> ActionProvenance:
    return ActionProvenance(
        invocation_id=_id("inv", number),
        turn_id=_id("turn", number),
        operation_id=_id("op", number),
        run_id=_id("run", number),
        task_id=_id("task", number),
        subtask_id=_id("sub", number),
        schedule_occurrence_id=f"schedule:{number}",
    )


def _new_record(
    number: int = 1,
    *,
    deduplication_scope: str | None = None,
    deduplication_key: str | None = None,
    intent_digest: str | None = None,
    effect_class: ActionEffectClass = (
        ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE
    ),
    idempotency: IdempotencyContract | None = None,
    predecessor_action_id: str | None = None,
    duplicate_risk_acknowledgement: (
        DuplicateRiskAcknowledgement | None
    ) = None,
) -> ActionRecord:
    return new_action(
        action_kind="filesystem.move.v1",
        tool_name="move_file",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=("fs.read",),
        effect_class=effect_class,
        intent_digest=intent_digest or f"{number + 10:064x}",
        intent_ref=f"protected:intent:{number}",
        target_category="filesystem.path",
        target_fingerprint=f"{number + 20:064x}",
        deduplication_scope=deduplication_scope,
        deduplication_key=deduplication_key,
        idempotency=idempotency,
        provenance=_provenance(number),
        predecessor_action_id=predecessor_action_id,
        duplicate_risk_acknowledgement=duplicate_risk_acknowledgement,
        authority=_authority(number),
        clock=FixedClock(NOW),
        id_factory=_ids(number),
    )


def _claim(record: ActionRecord, number: int = 1) -> ActionRecord:
    return record.claim_attempt(
        attempt_id=_id("aat", number),
        invocation_id=_id("inv", number),
        owner_id=f"process:{number}",
        owner_epoch=number,
        transition_id=_id("atx", number),
        at=NOW,
    )


def _outcome(verdict: EffectVerdict) -> ActionOutcome:
    partial = (("destination_written",)
               if verdict is EffectVerdict.CONFIRMED_PARTIAL else ())
    return ActionOutcome(
        verdict=verdict,
        result_class=("complete" if verdict is EffectVerdict.CONFIRMED_APPLIED
                      else "categorical_failure"),
        summary=("effect confirmed" if verdict is EffectVerdict.CONFIRMED_APPLIED
                 else "effect did not complete"),
        partial_categories=partial,
    )


def _reconciliation(
    disposition: ReconciliationDisposition = (
        ReconciliationDisposition.PROBE_LATER
    ),
) -> ReconciliationMetadata:
    return ReconciliationMetadata(
        disposition=disposition,
        claim_epoch=0,
        probe_count=0,
        next_probe_at=(NOW + timedelta(minutes=5)
                       if disposition is ReconciliationDisposition.PROBE_LATER
                       else None),
    )


def _to_reconcile(record: ActionRecord, number: int = 2) -> ActionRecord:
    return record.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id=_id("atx", number),
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=_reconciliation(),
        evidence_refs=(f"evidence:timeout:{number}",),
    )


def _unknown_then_claimed_idempotent_retry(
    *,
    valid_until: datetime | None = None,
) -> ActionRecord:
    contract = IdempotencyContract(
        key="provider-key-1",
        contract="provider-contract.v1",
        scope="mailbox:primary",
        valid_until=(valid_until if valid_until is not None
                     else NOW + timedelta(minutes=10)),
    )
    first = _claim(_new_record(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        idempotency=contract,
    ))
    reauthorized = first.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 2),
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )
    return reauthorized.claim_attempt(
        attempt_id=_id("aat", 2),
        invocation_id=_id("inv", 2),
        owner_id="process:two",
        owner_epoch=2,
        transition_id=_id("atx", 3),
        at=NOW,
    )


def _multi_attempt_reconciliation_required() -> ActionRecord:
    second = _unknown_then_claimed_idempotent_retry()
    return second.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id=_id("atx", 4),
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=_reconciliation(),
        evidence_refs=("evidence:timeout:second-attempt",),
    )


@pytest.fixture(params=["memory", "json"])
def action_store(request, tmp_path):
    store = (InMemoryActionStore(clock=FixedClock(NOW))
             if request.param == "memory"
             else JsonFileActionStore(tmp_path, clock=FixedClock(NOW)))
    try:
        yield store
    finally:
        store.close()


def test_action_record_round_trip_is_strict_and_deeply_immutable():
    record = _to_reconcile(_claim(_new_record()))
    restored = ActionRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.created_at.utcoffset() == timedelta(0)
    assert isinstance(restored.attempts, tuple)
    assert isinstance(restored.transitions, tuple)
    assert isinstance(restored.evidence_refs, tuple)
    with pytest.raises(FrozenInstanceError):
        restored.state = ActionState.SUCCEEDED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        restored.attempts[0].status = (  # type: ignore[misc]
            ActionAttemptStatus.INVOKED
        )


def test_duplicate_risk_acknowledgement_round_trip_is_strict_and_bounded():
    acknowledgement = _duplicate_risk_acknowledgement()
    payload = acknowledgement.to_dict()
    record = _new_record(
        2,
        predecessor_action_id=acknowledgement.predecessor_action_id,
        duplicate_risk_acknowledgement=acknowledgement,
    )

    assert DuplicateRiskAcknowledgement.from_dict(payload) == acknowledgement
    assert ActionRecord.from_dict(record.to_dict()) == record
    assert set(payload) == {
        "acknowledgement_id",
        "predecessor_action_id",
        "approval_request_id",
        "binding_digest",
        "acknowledged_at",
    }
    assert new_duplicate_risk_acknowledgement_id(
        id_factory=_ids(8)) == _id("dra", 8)

    for invalid in (
        {key: value for key, value in payload.items()
         if key != "approval_request_id"},
        {**payload, "unexpected": "field"},
        {**payload, "approval_request_id": "x" * 129},
    ):
        with pytest.raises(ActionValidationError):
            DuplicateRiskAcknowledgement.from_dict(invalid)


def test_duplicate_risk_acknowledgement_rejects_malformed_identity_and_digest():
    acknowledgement = _duplicate_risk_acknowledgement()
    with pytest.raises(ActionValidationError):
        replace(
            acknowledgement,
            acknowledgement_id=_id("ack", 2),
        ).validate()
    with pytest.raises(ActionValidationError):
        replace(acknowledgement, binding_digest="not-a-digest").validate()


def test_duplicate_risk_schema_has_no_raw_human_text_field():
    secret = "RAW_HUMAN_ACKNOWLEDGEMENT_SENTINEL"
    payload = _duplicate_risk_acknowledgement().to_dict()
    payload["human_acknowledgement_text"] = secret

    with pytest.raises(ActionValidationError) as captured:
        DuplicateRiskAcknowledgement.from_dict(payload)
    assert secret not in str(captured.value.details)
    assert secret not in json.dumps(
        _duplicate_risk_acknowledgement().to_dict())


def test_duplicate_risk_acknowledgement_requires_exact_predecessor_binding():
    acknowledgement = _duplicate_risk_acknowledgement()
    with pytest.raises(ActionValidationError):
        _new_record(
            2,
            duplicate_risk_acknowledgement=acknowledgement,
        )
    with pytest.raises(ActionValidationError):
        _new_record(
            2,
            predecessor_action_id=_id("act", 3),
            duplicate_risk_acknowledgement=acknowledgement,
        )


def test_closed_enums_have_exact_architecture_members():
    assert {item.name for item in ActionState} == {
        "AUTHORIZED", "IN_FLIGHT", "RECONCILE_REQUIRED",
        "SUCCEEDED", "FAILED", "CANCELLED",
    }
    assert {item.name for item in EffectVerdict} == {
        "CONFIRMED_APPLIED", "CONFIRMED_NOT_APPLIED",
        "CONFIRMED_PARTIAL", "OUTCOME_UNKNOWN",
    }
    assert {item.name for item in ActionJournalEligibility} == {
        "PURE_READ", "EPHEMERAL_CONTROL", "DURABLE_LOCAL_MUTATION",
        "EXTERNAL_ACCOUNT_MUTATION", "SECURITY_PRIVILEGE_SYSTEM_MUTATION",
        "LONG_RUNNING_OR_AMBIGUOUS_EFFECT",
    }
    assert {item.name for item in ActionEffectClass} == {
        "EXTERNALLY_IDEMPOTENT", "LOCALLY_VERIFIABLE_OR_REVERSIBLE",
        "NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS",
    }


def test_transition_matrix_is_exact():
    assert LEGAL_ACTION_TRANSITIONS == {
        ActionState.AUTHORIZED: frozenset({
            ActionState.IN_FLIGHT, ActionState.CANCELLED, ActionState.FAILED,
        }),
        ActionState.IN_FLIGHT: frozenset({
            ActionState.SUCCEEDED, ActionState.FAILED,
            ActionState.RECONCILE_REQUIRED, ActionState.AUTHORIZED,
            ActionState.CANCELLED,
        }),
        ActionState.RECONCILE_REQUIRED: frozenset({
            ActionState.SUCCEEDED, ActionState.FAILED,
            ActionState.AUTHORIZED, ActionState.RECONCILE_REQUIRED,
        }),
        ActionState.SUCCEEDED: frozenset(),
        ActionState.FAILED: frozenset(),
        ActionState.CANCELLED: frozenset(),
    }


def test_every_legal_transition_has_a_valid_typed_form():
    authorized = _new_record()
    inflight = _claim(authorized)
    reconcile = _to_reconcile(inflight)

    assert _claim(authorized).state is ActionState.IN_FLIGHT
    assert authorized.transition(
        ActionState.CANCELLED, transition_id=_id("atx", 9), at=NOW,
    ).state is ActionState.CANCELLED
    assert authorized.transition(
        ActionState.FAILED, transition_id=_id("atx", 9), at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
    ).state is ActionState.FAILED

    assert inflight.transition(
        ActionState.SUCCEEDED, transition_id=_id("atx", 9), at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_APPLIED),
    ).state is ActionState.SUCCEEDED
    assert inflight.transition(
        ActionState.FAILED, transition_id=_id("atx", 9), at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
    ).state is ActionState.FAILED
    assert _to_reconcile(inflight, 9).state is ActionState.RECONCILE_REQUIRED
    assert inflight.transition(
        ActionState.AUTHORIZED, transition_id=_id("atx", 9), at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
    ).state is ActionState.AUTHORIZED
    assert inflight.transition(
        ActionState.CANCELLED, transition_id=_id("atx", 9), at=NOW,
        attempt_status=ActionAttemptStatus.NOT_INVOKED,
        proof=ActionTransitionProof.LIVE_OWNER_NOT_INVOKED,
    ).state is ActionState.CANCELLED

    assert reconcile.transition(
        ActionState.SUCCEEDED, transition_id=_id("atx", 9), at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_APPLIED),
    ).state is ActionState.SUCCEEDED
    assert reconcile.transition(
        ActionState.FAILED, transition_id=_id("atx", 9), at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
    ).state is ActionState.FAILED
    assert reconcile.transition(
        ActionState.AUTHORIZED, transition_id=_id("atx", 9), at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
    ).state is ActionState.AUTHORIZED
    assert reconcile.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id=_id("atx", 9), at=NOW,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=_reconciliation(
            ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED),
    ).state is ActionState.RECONCILE_REQUIRED


@pytest.mark.parametrize("terminal", [
    ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELLED,
])
def test_terminal_records_are_immutable(terminal):
    base = _new_record()
    if terminal is ActionState.CANCELLED:
        record = base.transition(
            terminal, transition_id=_id("atx", 2), at=NOW)
    elif terminal is ActionState.FAILED:
        record = base.transition(
            terminal, transition_id=_id("atx", 2), at=NOW,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            terminal_outcome=_outcome(
                EffectVerdict.CONFIRMED_NOT_APPLIED))
    else:
        record = _claim(base).transition(
            terminal, transition_id=_id("atx", 2), at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
            terminal_outcome=_outcome(EffectVerdict.CONFIRMED_APPLIED))
    for target in ActionState:
        with pytest.raises(ActionValidationError):
            record.transition(
                target, transition_id=_id("atx", 99), at=NOW)


def test_outcome_unknown_cannot_be_terminalized_as_failed():
    inflight = _claim(_new_record())
    with pytest.raises(ActionValidationError):
        inflight.transition(
            ActionState.FAILED,
            transition_id=_id("atx", 2),
            at=NOW,
            attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            terminal_outcome=ActionOutcome(
                verdict=EffectVerdict.OUTCOME_UNKNOWN,
                result_class="timeout",
                summary="unknown",
            ),
        )


def test_retry_requires_explicit_trusted_proof():
    inflight = _claim(_new_record())
    with pytest.raises(ActionValidationError):
        inflight.transition(
            ActionState.AUTHORIZED,
            transition_id=_id("atx", 2),
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        )

    retriable = inflight.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 2),
        at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
    )
    assert retriable.state is ActionState.AUTHORIZED


def test_external_idempotency_retry_requires_matching_live_contract():
    contract = IdempotencyContract(
        key="provider-key-1",
        contract="provider-contract.v1",
        scope="mailbox:primary",
        valid_until=NOW + timedelta(minutes=10),
    )
    inflight = _claim(_new_record(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        idempotency=contract,
    ))
    retriable = inflight.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 2),
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )
    assert retriable.state is ActionState.AUTHORIZED

    with pytest.raises(ActionValidationError):
        inflight.transition(
            ActionState.AUTHORIZED,
            transition_id=_id("atx", 3),
            at=NOW + timedelta(minutes=11),
            attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
        )

    with pytest.raises(ActionValidationError):
        retriable.claim_attempt(
            attempt_id=_id("aat", 2),
            invocation_id=_id("inv", 2),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", 3),
            at=NOW + timedelta(minutes=11),
        )


def test_externally_idempotent_action_requires_stable_contract_at_creation():
    with pytest.raises(ActionValidationError):
        _new_record(effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT)


def test_authorized_local_failure_cannot_claim_a_partial_effect():
    with pytest.raises(ActionValidationError):
        _new_record().transition(
            ActionState.FAILED,
            transition_id=_id("atx", 2),
            at=NOW,
            effect_verdict=EffectVerdict.CONFIRMED_PARTIAL,
            terminal_outcome=_outcome(EffectVerdict.CONFIRMED_PARTIAL),
        )


def test_local_failure_after_unknown_idempotent_attempt_cannot_erase_truth():
    contract = IdempotencyContract(
        key="provider-key-1",
        contract="provider-contract.v1",
        scope="mailbox:primary",
        valid_until=NOW + timedelta(minutes=10),
    )
    retriable = _claim(_new_record(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        idempotency=contract,
    )).transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 2),
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )
    with pytest.raises(ActionValidationError):
        retriable.transition(
            ActionState.FAILED,
            transition_id=_id("atx", 3),
            at=NOW,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            terminal_outcome=_outcome(
                EffectVerdict.CONFIRMED_NOT_APPLIED),
        )


@pytest.mark.parametrize(
    ("second_status", "second_verdict"),
    [
        (ActionAttemptStatus.NOT_INVOKED,
         EffectVerdict.CONFIRMED_NOT_APPLIED),
        (ActionAttemptStatus.INVOKED,
         EffectVerdict.CONFIRMED_NOT_APPLIED),
        (ActionAttemptStatus.INVOKED,
         EffectVerdict.CONFIRMED_PARTIAL),
    ],
    ids=[
        "second-not-invoked",
        "second-invoked-not-applied",
        "second-confirmed-partial",
    ],
)
def test_failed_cannot_erase_an_older_unknown_attempt(
    second_status,
    second_verdict,
):
    second = _unknown_then_claimed_idempotent_retry()

    with pytest.raises(ActionValidationError):
        second.transition(
            ActionState.FAILED,
            transition_id=_id("atx", 4),
            at=NOW,
            attempt_status=second_status,
            effect_verdict=second_verdict,
            terminal_outcome=_outcome(second_verdict),
        )


def test_definitive_no_effect_cannot_launder_an_older_unknown_attempt():
    second = _unknown_then_claimed_idempotent_retry()

    with pytest.raises(ActionValidationError):
        second.transition(
            ActionState.AUTHORIZED,
            transition_id=_id("atx", 4),
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        )


def test_older_unknown_remains_retryable_only_under_live_external_guarantee():
    second = _unknown_then_claimed_idempotent_retry()
    reauthorized = second.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 4),
        at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )

    third = reauthorized.claim_attempt(
        attempt_id=_id("aat", 3),
        invocation_id=_id("inv", 3),
        owner_id="process:three",
        owner_epoch=3,
        transition_id=_id("atx", 5),
        at=NOW + timedelta(minutes=1),
    )
    assert third.state is ActionState.IN_FLIGHT
    assert third.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN


def test_expired_external_guarantee_blocks_claim_without_proof_laundering():
    second = _unknown_then_claimed_idempotent_retry(
        valid_until=NOW + timedelta(minutes=2))

    with pytest.raises(ActionValidationError):
        second.transition(
            ActionState.AUTHORIZED,
            transition_id=_id("atx", 4),
            at=NOW + timedelta(minutes=1),
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        )

    reauthorized = second.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 4),
        at=NOW + timedelta(minutes=1),
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )
    with pytest.raises(ActionValidationError):
        reauthorized.claim_attempt(
            attempt_id=_id("aat", 3),
            invocation_id=_id("inv", 3),
            owner_id="process:three",
            owner_epoch=3,
            transition_id=_id("atx", 5),
            at=NOW + timedelta(minutes=2),
        )


def test_one_current_unknown_attempt_can_be_definitively_reauthorized():
    ambiguous = _to_reconcile(_claim(_new_record()))
    attempt_history = ambiguous.attempts

    reauthorized = ambiguous.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 3),
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        evidence_refs=("reconciliation:logical-effect-absent",),
    )

    assert reauthorized.state is ActionState.AUTHORIZED
    assert reauthorized.attempts == attempt_history
    assert reauthorized.attempts[0].effect_verdict is (
        EffectVerdict.OUTCOME_UNKNOWN)
    assert reauthorized.transitions[-1].attempt_id is None
    assert reauthorized.transitions[-1].attempt_status is None
    assert "reconciliation:logical-effect-absent" in (
        reauthorized.evidence_refs)
    assert "reconciliation:logical-effect-absent" not in (
        reauthorized.attempts[0].evidence_refs)


@pytest.mark.parametrize(
    ("target", "verdict"),
    [
        (ActionState.FAILED, EffectVerdict.CONFIRMED_NOT_APPLIED),
        (ActionState.SUCCEEDED, EffectVerdict.CONFIRMED_APPLIED),
        (ActionState.FAILED, EffectVerdict.CONFIRMED_PARTIAL),
    ],
    ids=["no-effect", "complete-effect", "exact-partial-effect"],
)
def test_multi_attempt_reconciliation_settles_only_logical_action_truth(
    target,
    verdict,
):
    ambiguous = _multi_attempt_reconciliation_required()
    attempt_history = ambiguous.attempts
    evidence_ref = f"reconciliation:logical:{verdict.value}"

    settled = ambiguous.transition(
        target,
        transition_id=_id("atx", 5),
        at=NOW,
        effect_verdict=verdict,
        terminal_outcome=_outcome(verdict),
        evidence_refs=(evidence_ref,),
    )

    assert settled.attempts == attempt_history
    assert all(attempt.effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
               for attempt in settled.attempts)
    assert settled.terminal_outcome is not None
    assert settled.terminal_outcome.verdict is verdict
    if verdict is EffectVerdict.CONFIRMED_PARTIAL:
        assert settled.terminal_outcome.partial_categories == (
            "destination_written",)
    transition = settled.transitions[-1]
    assert transition.attempt_id is None
    assert transition.attempt_status is None
    assert transition.evidence_refs == (evidence_ref,)
    assert evidence_ref in settled.evidence_refs
    assert all(evidence_ref not in attempt.evidence_refs
               for attempt in settled.attempts)
    assert ActionRecord.from_dict(settled.to_dict()) == settled


def test_action_wide_reconciliation_allows_safe_retry_without_rewriting():
    ambiguous = _multi_attempt_reconciliation_required()
    attempt_history = ambiguous.attempts

    reauthorized = ambiguous.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 5),
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        evidence_refs=("reconciliation:logical-effect-absent",),
    )

    assert reauthorized.attempts == attempt_history
    assert all(attempt.effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
               for attempt in reauthorized.attempts)
    third = reauthorized.claim_attempt(
        attempt_id=_id("aat", 3),
        invocation_id=_id("inv", 3),
        owner_id="process:three",
        owner_epoch=3,
        transition_id=_id("atx", 6),
        at=NOW,
    )
    assert third.state is ActionState.IN_FLIGHT
    assert third.attempts[:-1] == attempt_history
    assert third.attempts[-1].status is ActionAttemptStatus.CLAIMED

    failed = third.transition(
        ActionState.FAILED,
        transition_id=_id("atx", 7),
        at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
    )
    assert failed.state is ActionState.FAILED
    assert failed.attempts[:2] == attempt_history
    assert all(attempt.effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
               for attempt in failed.attempts[:2])
    assert failed.attempts[-1].effect_verdict is (
        EffectVerdict.CONFIRMED_NOT_APPLIED)


def test_reconciliation_external_retry_remains_live_contract_dependent():
    ambiguous = _multi_attempt_reconciliation_required()
    attempt_history = ambiguous.attempts
    reauthorized = ambiguous.transition(
        ActionState.AUTHORIZED,
        transition_id=_id("atx", 5),
        at=NOW,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )

    assert reauthorized.attempts == attempt_history
    with pytest.raises(ActionValidationError):
        reauthorized.claim_attempt(
            attempt_id=_id("aat", 3),
            invocation_id=_id("inv", 3),
            owner_id="process:three",
            owner_epoch=3,
            transition_id=_id("atx", 6),
            at=NOW + timedelta(minutes=10),
        )

    third = reauthorized.claim_attempt(
        attempt_id=_id("aat", 3),
        invocation_id=_id("inv", 3),
        owner_id="process:three",
        owner_epoch=3,
        transition_id=_id("atx", 6),
        at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(ActionValidationError):
        third.transition(
            ActionState.FAILED,
            transition_id=_id("atx", 7),
            at=NOW + timedelta(minutes=1),
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            terminal_outcome=_outcome(
                EffectVerdict.CONFIRMED_NOT_APPLIED),
        )


def test_reconciliation_transition_rejects_attempt_attribution():
    ambiguous = _to_reconcile(_claim(_new_record()))

    with pytest.raises(ActionValidationError):
        ambiguous.transition(
            ActionState.SUCCEEDED,
            transition_id=_id("atx", 3),
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
            terminal_outcome=_outcome(EffectVerdict.CONFIRMED_APPLIED),
            evidence_refs=("reconciliation:logical-effect-present",),
        )


def test_confirmed_complete_effect_can_succeed_despite_older_unknown_attempt():
    second = _unknown_then_claimed_idempotent_retry()

    succeeded = second.transition(
        ActionState.SUCCEEDED,
        transition_id=_id("atx", 4),
        at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_APPLIED),
    )

    assert succeeded.state is ActionState.SUCCEEDED
    assert succeeded.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN


def test_cancellation_after_claim_requires_live_not_invoked_proof():
    inflight = _claim(_new_record())
    with pytest.raises(ActionValidationError):
        inflight.transition(
            ActionState.CANCELLED,
            transition_id=_id("atx", 2),
            at=NOW,
            attempt_status=ActionAttemptStatus.NOT_INVOKED,
        )
    with pytest.raises(ActionValidationError):
        inflight.transition(
            ActionState.CANCELLED,
            transition_id=_id("atx", 3),
            at=NOW,
            attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
            proof=ActionTransitionProof.LIVE_OWNER_NOT_INVOKED,
        )
    cancelled = inflight.transition(
        ActionState.CANCELLED,
        transition_id=_id("atx", 4),
        at=NOW,
        attempt_status=ActionAttemptStatus.NOT_INVOKED,
        proof=ActionTransitionProof.LIVE_OWNER_NOT_INVOKED,
    )
    assert cancelled.attempts[0].status is ActionAttemptStatus.NOT_INVOKED
    assert cancelled.attempts[0].effect_verdict is None


def test_read_and_ephemeral_classes_cannot_create_action_records():
    for eligibility in (
        ActionJournalEligibility.PURE_READ,
        ActionJournalEligibility.EPHEMERAL_CONTROL,
    ):
        with pytest.raises(ActionValidationError):
            replace(_new_record(), eligibility=eligibility).validate()


def test_expired_authority_cannot_create_an_authorized_record():
    expired = replace(
        _authority(),
        expires_at=NOW,
    ).validate()
    with pytest.raises(ActionValidationError):
        replace(_new_record(), authority=expired).validate()


def test_store_protocol_create_load_and_nonterminal_listing(action_store):
    assert isinstance(action_store, ActionStore)
    first = action_store.create(_new_record(2))
    second = action_store.create(_new_record(1))
    terminal_base = action_store.create(_new_record(3))
    terminal = terminal_base.transition(
        ActionState.CANCELLED, transition_id=_id("atx", 3), at=NOW)
    action_store.compare_and_swap(terminal.action_id, 0, terminal)

    assert action_store.load(first.action_id) == first
    assert action_store.list_actions() == [second.action_id, first.action_id,
                                           terminal.action_id]
    assert [record.action_id for record in action_store.list_nonterminal()] == [
        second.action_id, first.action_id,
    ]


def test_compare_and_swap_increments_once_and_stale_cas_preserves_winner(
        action_store):
    record = action_store.create(_new_record())
    cancelled = record.transition(
        ActionState.CANCELLED, transition_id=_id("atx", 2), at=NOW)
    failed = record.transition(
        ActionState.FAILED, transition_id=_id("atx", 3), at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
    )

    accepted = action_store.compare_and_swap(record.action_id, 0, cancelled)
    assert accepted.version == 1
    assert action_store.compare_and_swap(record.action_id, 0, cancelled) == accepted
    with pytest.raises(ActionVersionConflictError):
        action_store.compare_and_swap(record.action_id, 0, failed)
    assert action_store.load(record.action_id) == cancelled


def test_claim_attempt_and_in_flight_state_publish_atomically(action_store):
    record = action_store.create(_new_record())
    claimed = action_store.claim_attempt(
        record.action_id,
        0,
        attempt_id=_id("aat", 1),
        invocation_id=_id("inv", 2),
        owner_id="process:one",
        owner_epoch=1,
        transition_id=_id("atx", 1),
    )
    loaded = action_store.load(record.action_id)

    assert claimed == loaded
    assert loaded.state is ActionState.IN_FLIGHT
    assert loaded.version == 1
    assert len(loaded.attempts) == 1
    assert loaded.attempts[0].status is ActionAttemptStatus.CLAIMED
    assert loaded.transitions[-1].attempt_id == loaded.attempts[0].attempt_id


def test_generic_cas_cannot_bypass_atomic_attempt_claim_primitive(action_store):
    record = action_store.create(_new_record())
    forged_route = _claim(record)
    with pytest.raises(ActionValidationError):
        action_store.compare_and_swap(record.action_id, 0, forged_route)
    assert action_store.load(record.action_id) == record


def test_json_reader_cannot_cross_atomic_claim_publication(tmp_path,
                                                           monkeypatch):
    writer = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    reader = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    original_mutex = writer._domain.mutex
    write_entered = threading.Event()
    allow_write = threading.Event()
    reader_waiting = threading.Event()
    reader_done = threading.Event()
    observed: list[ActionRecord] = []
    record = writer.create(_new_record())

    class TrackingMutex:
        def __enter__(self):
            if threading.current_thread().name == "action-reader":
                reader_waiting.set()
            original_mutex.acquire()
            return self

        def __exit__(self, *_):
            original_mutex.release()

    tracking = TrackingMutex()
    writer._domain.mutex = tracking
    original_write = writer._write_unlocked

    def gated_write(path, record):
        write_entered.set()
        assert allow_write.wait(timeout=5)
        original_write(path, record)

    monkeypatch.setattr(writer, "_write_unlocked", gated_write)
    try:
        write_thread = threading.Thread(
            target=lambda: writer.claim_attempt(
                record.action_id, 0,
                attempt_id=_id("aat", 1),
                invocation_id=_id("inv", 2),
                owner_id="process:one",
                owner_epoch=1,
                transition_id=_id("atx", 1),
            ),
            name="action-writer",
        )

        def read():
            observed.append(reader.load(record.action_id))
            reader_done.set()

        read_thread = threading.Thread(target=read, name="action-reader")
        write_thread.start()
        assert write_entered.wait(timeout=5)
        read_thread.start()
        assert reader_waiting.wait(timeout=5)
        assert not reader_done.is_set()
        allow_write.set()
        write_thread.join(timeout=5)
        read_thread.join(timeout=5)
        assert not write_thread.is_alive()
        assert not read_thread.is_alive()
        assert reader_done.is_set()
        assert len(observed) == 1
        assert observed[0].state is ActionState.IN_FLIGHT
        assert len(observed[0].attempts) == 1
        assert observed[0].transitions[-1].attempt_id == \
            observed[0].attempts[0].attempt_id
    finally:
        allow_write.set()
        reader.close()
        writer.close()


def test_two_duplicate_callers_attach_to_one_logical_action(action_store):
    first = _new_record(
        1, deduplication_scope="workflow:step",
        deduplication_key="step:42", intent_digest="a" * 64)
    second = replace(
        first,
        action_id=_id("act", 2),
        provenance=_provenance(2),
        authority=_authority(2),
    ).validate()
    records = [first, second]
    barrier = threading.Barrier(2)
    results: list[ActionRecord] = []
    errors: list[BaseException] = []
    output_lock = threading.Lock()

    def create(record):
        barrier.wait(timeout=5)
        try:
            result = action_store.create(record)
            with output_lock:
                results.append(result)
        except BaseException as exc:
            with output_lock:
                errors.append(exc)

    threads = [threading.Thread(target=create, args=(record,))
               for record in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert len(results) == 2
    assert results[0].action_id == results[1].action_id
    assert len(action_store.list_actions()) == 1


def test_conflicting_dedup_ownership_fails_closed(action_store):
    first = _new_record(
        1, deduplication_scope="workflow:step",
        deduplication_key="step:42", intent_digest="a" * 64)
    conflict = _new_record(
        2, deduplication_scope="workflow:step",
        deduplication_key="step:42", intent_digest="b" * 64)
    action_store.create(first)
    with pytest.raises(ActionDeduplicationConflictError):
        action_store.create(conflict)
    assert action_store.list_actions() == [first.action_id]


def test_predecessor_links_must_resolve_inside_the_action_domain(action_store):
    missing = replace(
        _new_record(2),
        predecessor_action_id=_id("act", 1),
    ).validate()
    with pytest.raises(ActionStoreError):
        action_store.create(missing)

    predecessor = action_store.create(_new_record(1))
    linked = action_store.create(missing)
    assert linked.predecessor_action_id == predecessor.action_id


def test_duplicate_risk_acknowledgement_is_immutable_through_cas(action_store):
    predecessor = action_store.create(_new_record(1))
    acknowledgement = _duplicate_risk_acknowledgement()

    linked_without = action_store.create(_new_record(
        2,
        predecessor_action_id=predecessor.action_id,
    ))
    cancelled_without = linked_without.transition(
        ActionState.CANCELLED,
        transition_id=_id("atx", 20),
        at=NOW,
    )
    with pytest.raises(ActionValidationError):
        action_store.compare_and_swap(
            linked_without.action_id,
            linked_without.version,
            replace(
                cancelled_without,
                duplicate_risk_acknowledgement=acknowledgement,
            ),
        )

    linked_with = action_store.create(_new_record(
        3,
        predecessor_action_id=predecessor.action_id,
        duplicate_risk_acknowledgement=replace(
            acknowledgement,
            acknowledgement_id=_id("dra", 3),
        ),
    ))
    cancelled_with = linked_with.transition(
        ActionState.CANCELLED,
        transition_id=_id("atx", 30),
        at=NOW,
    )
    for rewritten in (
        None,
        replace(
            linked_with.duplicate_risk_acknowledgement,
            binding_digest="f" * 64,
        ),
    ):
        with pytest.raises(ActionValidationError):
            action_store.compare_and_swap(
                linked_with.action_id,
                linked_with.version,
                replace(
                    cancelled_with,
                    duplicate_risk_acknowledgement=rewritten,
                ),
            )


def test_acknowledged_repeat_does_not_resolve_or_execute_predecessor(
    action_store,
):
    predecessor = action_store.create(_new_record(1))
    predecessor = action_store.claim_attempt(
        predecessor.action_id,
        predecessor.version,
        attempt_id=_id("aat", 1),
        invocation_id=_id("inv", 1),
        owner_id="process:one",
        owner_epoch=1,
        transition_id=_id("atx", 1),
    )
    predecessor = action_store.compare_and_swap(
        predecessor.action_id,
        predecessor.version,
        _to_reconcile(predecessor),
    )

    successor = action_store.create(_new_record(
        2,
        predecessor_action_id=predecessor.action_id,
        duplicate_risk_acknowledgement=(
            _duplicate_risk_acknowledgement()),
    ))

    assert action_store.load(predecessor.action_id).state is (
        ActionState.RECONCILE_REQUIRED)
    assert action_store.load(predecessor.action_id).attempts == (
        predecessor.attempts)
    assert successor.state is ActionState.AUTHORIZED
    assert successor.attempts == ()


def test_external_idempotency_identity_has_one_logical_owner(action_store):
    contract = IdempotencyContract(
        key="provider-key-1",
        contract="provider-contract.v1",
        scope="mailbox:primary",
        valid_until=NOW + timedelta(minutes=10),
    )
    first = _new_record(
        1,
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        idempotency=contract,
    )
    second = _new_record(
        2,
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        idempotency=contract,
    )
    action_store.create(first)
    with pytest.raises(ActionStoreError):
        action_store.create(second)
    assert action_store.list_actions() == [first.action_id]


def test_attempt_namespace_is_unique_and_not_stage4_transport_namespace():
    first = new_action_attempt_id(id_factory=_ids(1))
    second = new_action_attempt_id(id_factory=_ids(2))
    assert first.startswith("aat_")
    assert second.startswith("aat_")
    assert first != second
    assert not first.startswith("att_")
    with pytest.raises(ActionValidationError):
        _new_record().claim_attempt(
            attempt_id="att_000000000001",
            invocation_id=_id("inv", 1),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", 1),
            at=NOW,
        )


def test_attempt_identity_cannot_be_owned_by_two_actions(action_store):
    first = action_store.create(_new_record(1))
    second = action_store.create(_new_record(2))
    action_store.claim_attempt(
        first.action_id, 0,
        attempt_id=_id("aat", 1),
        invocation_id=_id("inv", 1),
        owner_id="process:one",
        owner_epoch=1,
        transition_id=_id("atx", 1),
    )
    with pytest.raises(ActionStoreError):
        action_store.claim_attempt(
            second.action_id, 0,
            attempt_id=_id("aat", 1),
            invocation_id=_id("inv", 2),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", 2),
        )
    assert action_store.load(second.action_id).state is ActionState.AUTHORIZED


def test_attempt_history_bound_fails_closed_without_truncation():
    record = _new_record()
    for number in range(1, MAX_ACTION_ATTEMPTS + 1):
        record = record.claim_attempt(
            attempt_id=_id("aat", number),
            invocation_id=_id("inv", number),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", number * 2 - 1),
            at=NOW,
        )
        record = record.transition(
            ActionState.AUTHORIZED,
            transition_id=_id("atx", number * 2),
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        )
    with pytest.raises(ActionValidationError):
        record.claim_attempt(
            attempt_id=_id("aat", MAX_ACTION_ATTEMPTS + 1),
            invocation_id=_id("inv", 1),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", 100),
            at=NOW,
        )
    assert len(record.attempts) == MAX_ACTION_ATTEMPTS


def test_transition_history_reserves_terminalization_and_never_truncates():
    record = _to_reconcile(_claim(_new_record()))
    while record.version < MAX_ACTION_TRANSITIONS - 1:
        current = record.reconciliation
        if current.claim_owner_id is None:
            next_reconciliation = replace(
                current,
                claim_owner_id="worker:bound",
                claim_epoch=current.claim_epoch + 1,
                claimed_at=NOW,
            )
        else:
            next_reconciliation = replace(
                current,
                claim_owner_id=None,
                claimed_at=None,
            )
        record = record.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=_id("atx", record.version + 1),
            at=NOW,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=next_reconciliation,
        )
    with pytest.raises(ActionValidationError):
        record.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=_id("atx", MAX_ACTION_TRANSITIONS),
            at=NOW,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=_reconciliation(),
        )
    terminal = record.transition(
        ActionState.FAILED,
        transition_id=_id("atx", MAX_ACTION_TRANSITIONS),
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
    )
    assert terminal.version == MAX_ACTION_TRANSITIONS
    assert len(terminal.transitions) == MAX_ACTION_TRANSITIONS


def test_reconciliation_claim_epoch_and_probe_count_cannot_regress():
    initial = ReconciliationMetadata(
        disposition=ReconciliationDisposition.PROBE_LATER,
        claim_owner_id="worker:one",
        claim_epoch=4,
        claimed_at=NOW,
        probe_count=3,
    )
    record = _claim(_new_record()).transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id=_id("atx", 2),
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=initial,
    )
    with pytest.raises(ActionValidationError):
        record.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=_id("atx", 3),
            at=NOW,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=replace(initial, claim_epoch=3),
        )
    with pytest.raises(ActionValidationError):
        record.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=_id("atx", 4),
            at=NOW,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=replace(initial, probe_count=2),
        )
    with pytest.raises(ActionValidationError):
        record.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=_id("atx", 5),
            at=NOW,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=replace(initial, claim_owner_id="worker:two"),
        )
    with pytest.raises(ActionValidationError):
        record.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=_id("atx", 7),
            at=NOW,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=initial,
        )

    next_claim = record.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id=_id("atx", 6),
        at=NOW,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=replace(
            initial,
            claim_owner_id="worker:two",
            claim_epoch=5,
            probe_count=4,
        ),
    )
    assert next_claim.reconciliation.claim_epoch == 5


def test_strict_schema_rejects_unknown_missing_and_raw_metadata():
    payload = _new_record().to_dict()
    secret = "SECRET_SENTINEL_DO_NOT_STORE"
    payload["raw"] = {"authorization": secret}
    with pytest.raises(ActionValidationError) as captured:
        ActionRecord.from_dict(payload)
    assert secret not in str(captured.value.details)

    payload = _new_record().to_dict()
    payload.pop("action_kind")
    with pytest.raises(ActionValidationError):
        ActionRecord.from_dict(payload)

    payload = _new_record().to_dict()
    payload.pop("duplicate_risk_acknowledgement")
    with pytest.raises(ActionValidationError):
        ActionRecord.from_dict(payload)


def test_manual_json_cannot_reload_failed_record_with_older_unknown_attempt(
    tmp_path,
):
    second = _unknown_then_claimed_idempotent_retry()
    outcome = _outcome(EffectVerdict.CONFIRMED_NOT_APPLIED)
    terminal = ActionTransition(
        transition_id=_id("atx", 4),
        from_state=ActionState.IN_FLIGHT,
        to_state=ActionState.FAILED,
        from_version=second.version,
        to_version=second.version + 1,
        at=NOW,
        attempt_id=second.attempts[-1].attempt_id,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        terminal_outcome=outcome,
    )
    payload = second.to_dict()
    payload["state"] = ActionState.FAILED.value
    payload["version"] = second.version + 1
    payload["attempts"][-1] = replace(
        second.attempts[-1],
        status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        updated_at=NOW,
    ).to_dict()
    payload["transitions"].append(terminal.to_dict())
    payload["terminal_outcome"] = outcome.to_dict()
    payload["updated_at"] = terminal.to_dict()["at"]
    path = tmp_path / f"{second.action_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        with pytest.raises(ActionValidationError):
            store.load(second.action_id)
    finally:
        store.close()


def test_manual_json_cannot_rewrite_attempt_through_reconciliation():
    ambiguous = _multi_attempt_reconciliation_required()
    outcome = _outcome(EffectVerdict.CONFIRMED_APPLIED)
    evidence_ref = "reconciliation:logical-effect-present"
    terminal = ActionTransition(
        transition_id=_id("atx", 5),
        from_state=ActionState.RECONCILE_REQUIRED,
        to_state=ActionState.SUCCEEDED,
        from_version=ambiguous.version,
        to_version=ambiguous.version + 1,
        at=NOW,
        attempt_id=ambiguous.attempts[-1].attempt_id,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
        evidence_refs=(evidence_ref,),
        terminal_outcome=outcome,
    )
    payload = ambiguous.to_dict()
    payload["state"] = ActionState.SUCCEEDED.value
    payload["version"] = ambiguous.version + 1
    payload["attempts"][-1] = replace(
        ambiguous.attempts[-1],
        status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
        evidence_refs=(
            ambiguous.attempts[-1].evidence_refs + (evidence_ref,)
        ),
        updated_at=NOW,
    ).to_dict()
    payload["transitions"].append(terminal.to_dict())
    payload["reconciliation"] = ReconciliationMetadata().to_dict()
    payload["terminal_outcome"] = outcome.to_dict()
    payload["evidence_refs"].append(evidence_ref)
    payload["updated_at"] = terminal.to_dict()["at"]

    with pytest.raises(ActionValidationError):
        ActionRecord.from_dict(payload)


def test_unknown_schema_and_illegal_history_fail_closed_on_reload(tmp_path):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        base = store.create(_new_record())
        record = store.claim_attempt(
            base.action_id, 0,
            attempt_id=_id("aat", 1),
            invocation_id=_id("inv", 1),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", 1),
        )
        path = tmp_path / f"{record.action_id}.json"
        payload = record.to_dict()
        payload["schema_version"] = ACTION_JOURNAL_SCHEMA_VERSION + 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ActionValidationError):
            store.load(record.action_id)

        payload = record.to_dict()
        payload["transitions"][0]["from_state"] = "failed"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ActionValidationError):
            store.load(record.action_id)
    finally:
        store.close()


def test_malformed_json_and_record_identity_mismatch_fail_closed(tmp_path):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        record = store.create(_new_record())
        path = tmp_path / f"{record.action_id}.json"
        path.write_text("{not-json", encoding="utf-8")
        with pytest.raises(ActionStoreError):
            store.load(record.action_id)

        other = _new_record(2).to_dict()
        path.write_text(json.dumps(other), encoding="utf-8")
        with pytest.raises(ActionStoreError):
            store.load(record.action_id)
    finally:
        store.close()


def test_duplicate_json_object_keys_are_rejected(tmp_path):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        record = store.create(_new_record())
        path = tmp_path / f"{record.action_id}.json"
        serialized = path.read_text(encoding="utf-8")
        path.write_text(
            serialized[:-1] + f',"action_id":"{record.action_id}"}}',
            encoding="utf-8",
        )
        with pytest.raises(ActionStoreError) as captured:
            store.load(record.action_id)
        assert captured.value.details["error"] == "_DuplicateJsonKey"
    finally:
        store.close()


def test_attempt_action_identity_mismatch_is_rejected():
    record = _claim(_new_record())
    forged = replace(
        record,
        attempts=(replace(record.attempts[0],
                          action_id=_id("act", 99)),),
    )
    with pytest.raises(ActionValidationError):
        forged.validate()


def test_json_restart_preserves_exact_state_and_history(tmp_path):
    first = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    base = first.create(_new_record())
    claimed = first.claim_attempt(
        base.action_id, 0,
        attempt_id=_id("aat", 1),
        invocation_id=_id("inv", 1),
        owner_id="process:one",
        owner_epoch=1,
        transition_id=_id("atx", 1),
    )
    candidate = _to_reconcile(claimed)
    record = first.compare_and_swap(base.action_id, 1, candidate)
    first.close()
    second = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        assert second.load(record.action_id) == record
        assert second.list_nonterminal() == [record]
    finally:
        second.close()


def test_duplicate_dedup_records_on_disk_quarantine_domain(tmp_path):
    first = _new_record(
        1, deduplication_scope="workflow:step",
        deduplication_key="step:42", intent_digest="a" * 64)
    second = _new_record(
        2, deduplication_scope="workflow:step",
        deduplication_key="step:42", intent_digest="a" * 64)
    (tmp_path / f"{first.action_id}.json").write_text(
        json.dumps(first.to_dict()), encoding="utf-8")
    (tmp_path / f"{second.action_id}.json").write_text(
        json.dumps(second.to_dict()), encoding="utf-8")
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        with pytest.raises(ActionDeduplicationConflictError):
            store.list_nonterminal()
        with pytest.raises(ActionDeduplicationConflictError):
            store.create(_new_record(3))
    finally:
        store.close()


def _ownership_probe(directory: Path) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\n"
        "from agentcore.action_journal import JsonFileActionStore, "
        "ActionStoreOwnershipError\n"
        "try:\n"
        "    store = JsonFileActionStore(sys.argv[1])\n"
        "except ActionStoreOwnershipError:\n"
        "    print('blocked')\n"
        "    raise SystemExit(23)\n"
        "else:\n"
        "    print('acquired')\n"
        "    store.close()\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    return subprocess.run(
        [sys.executable, "-c", code, str(directory)],
        capture_output=True, text=True, timeout=15, env=environment,
        check=False,
    )


def test_json_store_process_ownership_conflict_and_release(tmp_path):
    owner = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        blocked = _ownership_probe(tmp_path)
        assert blocked.returncode == 23, (blocked.stdout, blocked.stderr)
        assert blocked.stdout.strip() == "blocked"
    finally:
        owner.close()
    acquired = _ownership_probe(tmp_path)
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)
    assert acquired.stdout.strip() == "acquired"


def test_same_process_json_handles_share_deterministic_store_domain(tmp_path):
    first = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    second = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        record = first.create(_new_record())
        claimed = second.claim_attempt(
            record.action_id, 0,
            attempt_id=_id("aat", 1),
            invocation_id=_id("inv", 2),
            owner_id="process:one",
            owner_epoch=1,
            transition_id=_id("atx", 1),
        )
        assert first.load(record.action_id) == claimed
    finally:
        second.close()
        first.close()


def test_last_same_process_handle_controls_workspace_ownership(tmp_path):
    first = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    second = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        first.close()
        blocked = _ownership_probe(tmp_path)
        assert blocked.returncode == 23, (blocked.stdout, blocked.stderr)
        assert second.list_actions() == []
    finally:
        second.close()
        first.close()
    acquired = _ownership_probe(tmp_path)
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_rejects_inherited_store_then_acquires_after_release(
        tmp_path):
    owner = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child_pid = None
    child_reaped = False
    try:
        owner.create(_new_record())
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - assertions run in child
            status = 1
            try:
                os.close(ready_read)
                os.close(release_write)
                try:
                    owner.list_actions()
                except ActionStoreOwnershipError:
                    pass
                else:
                    os._exit(2)
                os.write(ready_write, b"1")
                if os.read(release_read, 1) != b"1":
                    os._exit(3)
                child_store = JsonFileActionStore(tmp_path)
                try:
                    if child_store.list_actions() != [_id("act", 1)]:
                        os._exit(4)
                    status = 0
                finally:
                    child_store.close()
            finally:
                os._exit(status)
        os.close(ready_write)
        os.close(release_read)
        readable, _, _ = select.select([ready_read], [], [], 5)
        assert ready_read in readable
        assert os.read(ready_read, 1) == b"1"
        owner.close()
        os.write(release_write, b"1")
        exited, _, _ = select.select([ready_read], [], [], 5)
        assert ready_read in exited
        assert os.read(ready_read, 1) == b""
        waited_pid, status = os.waitpid(child_pid, os.WNOHANG)
        assert waited_pid == child_pid
        child_reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        owner.close()
        if child_pid not in (None, 0) and not child_reaped:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child_pid, 0)
            except ChildProcessError:
                pass
        for descriptor in (ready_read, ready_write,
                           release_read, release_write):
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_atomic_replace_failure_preserves_prior_record_and_cleans_temp(
        tmp_path, monkeypatch):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        record = store.create(_new_record())
        candidate = record.transition(
            ActionState.CANCELLED, transition_id=_id("atx", 2), at=NOW)

        def fail_replace(*_):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(store_module.os, "replace", fail_replace)
        with pytest.raises(OSError):
            store.compare_and_swap(record.action_id, 0, candidate)
        assert store.load(record.action_id) == record
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        store.close()


def test_retry_after_replace_before_directory_fsync_is_idempotent(
        tmp_path, monkeypatch):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        record = store.create(_new_record())
        candidate = record.transition(
            ActionState.CANCELLED, transition_id=_id("atx", 2), at=NOW)
        calls = 0

        def fail_directory_fsync(_):
            nonlocal calls
            calls += 1
            raise OSError("simulated directory fsync failure")

        monkeypatch.setattr(store_module, "_fsync_directory",
                            fail_directory_fsync)
        with pytest.raises(OSError):
            store.compare_and_swap(record.action_id, 0, candidate)
        assert store.load(record.action_id) == candidate
        assert store.compare_and_swap(record.action_id, 0, candidate) == candidate
        assert calls == 1
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        store.close()


def test_claim_retry_after_replace_before_directory_fsync_is_idempotent(
        tmp_path, monkeypatch):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        record = store.create(_new_record())
        calls = 0

        def fail_directory_fsync(_):
            nonlocal calls
            calls += 1
            raise OSError("simulated directory fsync failure")

        monkeypatch.setattr(store_module, "_fsync_directory",
                            fail_directory_fsync)
        arguments = {
            "attempt_id": _id("aat", 1),
            "invocation_id": _id("inv", 2),
            "owner_id": "process:one",
            "owner_epoch": 1,
            "transition_id": _id("atx", 1),
        }
        with pytest.raises(OSError):
            store.claim_attempt(record.action_id, 0, **arguments)
        persisted = store.load(record.action_id)
        assert persisted.state is ActionState.IN_FLIGHT
        assert store.claim_attempt(record.action_id, 0, **arguments) == persisted
        assert calls == 1
        assert list(tmp_path.glob("*.tmp")) == []
    finally:
        store.close()


def test_public_schema_has_no_generic_secret_or_raw_escape_hatch():
    payload = _new_record().to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert not any(key in payload for key in (
        "metadata", "raw", "provider_raw", "tool_output", "credentials",
        "authorization", "message_body", "browser_content",
    ))
    assert "SECRET_SENTINEL" not in serialized


def test_action_schema_is_independent_from_frozen_turn_schema():
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1


def test_store_missing_and_closed_operations_are_categorical(tmp_path):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    with pytest.raises(ActionNotFoundError):
        store.load(_id("act", 99))
    store.close()
    store.close()
    for operation in (
        lambda: store.load(_id("act", 1)),
        lambda: store.create(_new_record()),
        store.list_nonterminal,
        store.list_actions,
    ):
        with pytest.raises(ActionStoreError):
            operation()
