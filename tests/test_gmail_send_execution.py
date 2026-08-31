import base64
import copy
import json
from dataclasses import asdict, replace

import pytest

from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION, ActionAttemptStatus, ActionInvocationContext,
    ActionJournalEligibility, ActionState, EffectVerdict,
)
from agentcore.approvals import ApprovalInvocationContext, HumanApprovalDecision
from agentcore.credentials import CredentialError, CredentialFailure, GoogleAccessProfile
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES, GOOGLE_GMAIL_SEND_SCOPES
from agentcore.integrations.gmail.send import GmailSendFoundation
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, Decision, Outcome, PermissionPolicy
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import ToolContext
from gmail_send_fakes import ACCESS, ARGS, REFRESH, SENDER, DecisionGate, Harness, send_policy


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path)
    yield value
    value.close()


def test_real_stage5_success_with_prior_authorized_state_fresh_claim_and_protected_evidence(harness):
    h = harness
    seen = []

    def seam(submission):
        record = h.actions.load(submission.identity.action_id)
        assert record.state is ActionState.IN_FLIGHT
        assert record.version >= 1 and len(record.attempts) == 1
        assert record.attempts[-1].attempt_id == submission.identity.attempt_id
        assert record.authority.approval_request_id is not None
        assert record.eligibility is ActionJournalEligibility.EXTERNAL_ACCOUNT_MUTATION
        assert record.intent_ref is not None and record.idempotency is None
        assert record.transitions[0].from_state is ActionState.AUTHORIZED
        assert record.mutation_capabilities == ("credential.access", "email.send", "net.write")
        seen.append(record)

    h.fake.before_effect = seam
    call = h.invoke()
    assert call.ok, call.to_dict()
    record = h.actions.load(call.action_id)
    assert len(seen) == 1 and record.state is ActionState.SUCCEEDED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert len(record.evidence_refs) == 1
    evidence = h.evidence.load_attempt_evidence(record.evidence_refs[0], action_id=record.action_id,
                                               attempt_id=call.attempt_id, intent_digest=record.intent_digest)
    assert evidence.evidence["message_id"] == "syntheticmessage1"
    assert evidence.evidence["attempt_id"] == call.attempt_id
    assert len(h.fake.sends) == 1
    with pytest.raises(CredentialError):
        h.fake.lease.reveal()


@pytest.mark.parametrize("level", list(PermissionLevel))
def test_default_and_legacy_boolean_gate_deny_email_at_every_level(tmp_path, level):
    from agentcore.permissions.config import PermissionConfig
    h = Harness(tmp_path, policy=PermissionPolicy(PermissionConfig(level=level), approval_gate=AlwaysApproveGate()))
    try:
        assert not h.foundation.tool.spec.read_only
        assert Capability.EMAIL_SEND in h.foundation.tool.spec.capabilities
        call = h.invoke()
        assert not call.ok and call.result.metadata["kind"] == "permission_denied"
        assert not h.fake.sends and not h.actions.list_actions() and not h.gate.prompts
        assert not h.executor.policy.evaluate(Capability.PAYMENT).allowed
        assert not h.executor.policy.evaluate(Capability.SYSTEM_ADMIN).allowed
        assert not h.executor.policy.check((Capability.CREDENTIAL_ACCESS,)).allowed
    finally:
        h.close()


@pytest.mark.parametrize("level", [PermissionLevel.CONFIRM, PermissionLevel.AUTONOMOUS])
def test_durable_approval_required_even_in_autonomous(tmp_path, level):
    h = Harness(tmp_path, gate=DecisionGate(HumanApprovalDecision.PENDING), policy=send_policy(level=level))
    try:
        pending = h.invoke()
        assert not pending.ok and pending.approval_request_id
        assert not h.fake.sends and not h.actions.list_actions()
        h.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
        sent = h.invoke(approval=pending.approval_request_id)
        assert sent.ok and len(h.fake.sends) == 1
        assert h.invoke(approval=pending.approval_request_id).ok is False
        assert len(h.fake.sends) == 1
    finally:
        h.close()


@pytest.mark.parametrize("key", ["account", "provider", "scopes", "raw", "mime_base64", "from", "headers", "url",
                                  "message_id", "approval", "allow_email_send", "credential_execution", "intent_preparation_id"])
def test_model_fields_cannot_select_account_content_encoding_or_authority(harness, key):
    h = harness
    h.prepare()
    call = h.invoke({**ARGS, key: "injected-private-value"})
    assert call.result.metadata["kind"] == "validation"
    assert not h.fake.sends and not h.actions.list_actions()
    assert "injected-private-value" not in json.dumps(h.logger.events)


def test_protected_preparation_is_required_and_not_an_ordinary_model_argument(harness):
    h = harness
    call = h.executor.invoke("gmail_send_message", ARGS)
    assert not call.ok and not h.fake.sends
    preparation = h.prepare()
    forged = replace(preparation.context(), deduplication_key="another-logical-action")
    assert not h.executor.invoke("gmail_send_message", ARGS, action_context=forged).ok
    assert not h.fake.sends


def test_direct_run_no_permit_and_no_journal_or_approval_authority_fail_closed(tmp_path):
    for flag in ("no_journal", "no_approval"):
        h = Harness(tmp_path / flag, **{flag: True})
        try:
            with pytest.raises(CredentialError) as error:
                h.foundation.tool.run(ARGS, ToolContext())
            assert error.value.reason is CredentialFailure.AUTHORITY
            assert not h.invoke().ok and not h.fake.sends
        finally:
            h.close()


def test_production_default_stops_before_provider_even_with_current_approval(tmp_path, monkeypatch):
    import http.client
    import socket
    h = Harness(tmp_path, production=True)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("production Gmail foundation attempted network I/O")
        monkeypatch.setattr(http.client.HTTPSConnection, "request", forbidden)
        monkeypatch.setattr(socket, "create_connection", forbidden)
        call = h.invoke()
        record = h.actions.load(call.action_id)
        assert record.state is ActionState.FAILED
        assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
        assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
        assert not h.fake.sends
    finally:
        h.close()


def test_no_plain_message_or_tokens_in_ordinary_journal_approval_logs_or_invocation(harness):
    h = harness
    call = h.invoke()
    assert call.ok, call.to_dict()
    ordinary = h.ordinary_json() + json.dumps(call.to_dict()) + json.dumps(h.logger.events)
    for value in (*ARGS["to"], *ARGS["cc"], *ARGS["bcc"], ARGS["subject"], ARGS["body"].splitlines()[0], SENDER,
                  ACCESS, REFRESH, "syntheticmessage1", "syntheticthread1", h.info.reference):
        assert value not in ordinary
    record = h.actions.load(call.action_id)
    payload = h.payloads.load(record.intent_ref, action_id=record.action_id, expected_intent_digest=record.intent_digest)
    encoded = json.dumps(payload.payload)
    assert ACCESS not in encoded and REFRESH not in encoded and h.info.reference not in encoded
    assert h.foundation.review(h.preparation)["message"]["subject"] == ARGS["subject"]
    assert record.schema_version == ACTION_JOURNAL_SCHEMA_VERSION == 1


@pytest.mark.parametrize("field", ["to", "cc", "bcc", "subject", "body"])
def test_approval_for_a_cannot_authorize_changed_send_semantics(harness, field):
    h = harness
    h.gate.decision = HumanApprovalDecision.PENDING
    pending = h.invoke()
    h.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
    changed = copy.deepcopy(ARGS)
    changed[field] = ["different@sample.invalid"] if field in ("to", "cc", "bcc") else "different"
    before = h.preparation.intent_digest
    h.prepare(changed)
    assert h.preparation.intent_digest != before
    call = h.invoke(changed, approval=pending.approval_request_id)
    assert not call.ok and not h.fake.sends


def test_timeout_after_effect_never_retries_and_keeps_same_message_id(harness):
    h = harness
    h.fake.after_effect = "timeout"
    call = h.invoke()
    record = h.actions.load(call.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    marker = h.fake.sends[0]["marker"]
    for _ in range(2):
        attached = h.invoke(action_id=call.action_id)
        assert attached.action_state == ActionState.RECONCILE_REQUIRED.value
    assert len(h.fake.sends) == 1
    assert h.foundation.review(h.preparation)["message_id"] == marker


@pytest.mark.parametrize("failure", ["stale", "disconnect", "same_account_reconnect", "expired"])
def test_current_credential_failures_prevent_provider_before_effect(harness, failure):
    h = harness
    h.prepare()
    original = h.info
    if failure == "stale":
        h.info = h.google.service.refresh(original.reference)
    elif failure == "disconnect":
        h.google.service.disconnect(original.reference)
    elif failure == "same_account_reconnect":
        h.info = h.google.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
        assert h.info.account_id == original.account_id and h.info.connection_id != original.connection_id
        with pytest.raises(CredentialError):
            h.foundation.bind(h.runtime, h.info.reference, replace_existing=True)
    else:
        h.clock.advance(7200)
    call = h.invoke()
    assert not call.ok and not h.fake.sends
    if call.action_id and h.actions.list_actions():
        record = h.actions.load(call.action_id)
        assert record.state is ActionState.FAILED and record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


def test_explicit_same_connection_refresh_rebind_keeps_intent_and_marker(harness):
    h = harness
    h.prepare()
    review = h.foundation.review(h.preparation)
    h.info = h.google.service.refresh(h.info.reference)
    h.foundation.bind(h.runtime, h.info.reference, replace_existing=True)
    assert h.invoke().ok
    assert h.fake.sends[0]["marker"] == review["message_id"]


def test_cancellation_before_claimed_provider_is_not_invoked(harness):
    h = harness
    def checkpoint():
        actions = h.actions.list_actions()
        if actions and h.actions.load(actions[0]).state is ActionState.IN_FLIGHT:
            raise CancellationRequested("test cancellation")
    with pytest.raises(CancellationRequested):
        h.invoke(checkpoint=checkpoint)
    record = h.actions.load(h.actions.list_actions()[0])
    assert record.state is ActionState.CANCELLED
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert not h.fake.sends


def test_cancellation_after_provider_entry_preserves_uncertainty(harness):
    h = harness
    def cancelled(submission, lease):
        raise CancellationRequested("test cancellation")
    h.fake.send_hook = cancelled
    call = h.invoke()
    assert len(h.fake.sends) == 1
    assert h.actions.load(call.action_id).state is ActionState.RECONCILE_REQUIRED


@pytest.mark.parametrize("response", [None, {}, {"id": "one"}, {"id": "one", "threadId": "two", "raw": "private"},
                                      {"id": ACCESS, "threadId": "two"}, {"id": "one", "threadId": "two", "labelIds": ["DRAFT"]}])
def test_malformed_or_secret_bearing_success_response_is_ambiguous(harness, response):
    h = harness
    h.fake.send_hook = lambda *_: response
    call = h.invoke()
    assert not call.ok and len(h.fake.sends) == 1
    assert h.actions.load(call.action_id).state is ActionState.RECONCILE_REQUIRED
    assert ACCESS not in json.dumps(call.to_dict()) + h.ordinary_json()


def test_evidence_storage_failure_never_claims_success_or_repeats_effect(tmp_path):
    h = Harness(tmp_path, no_evidence=True)
    try:
        call = h.invoke()
        assert not call.ok and len(h.fake.sends) == 1
        assert h.actions.load(call.action_id).state is ActionState.RECONCILE_REQUIRED
    finally:
        h.close()


def test_custom_policy_cannot_remove_the_mandatory_durable_approval_requirement(tmp_path, monkeypatch):
    h = Harness(tmp_path, no_approval=True)
    try:
        monkeypatch.setattr(h.executor.policy, "evaluate_authenticated_send",
                            lambda *_: Decision(Outcome.ALLOW, "custom host policy"))
        result = h.invoke()
        assert not result.ok and not h.fake.sends and not h.actions.list_actions()
    finally:
        h.close()
