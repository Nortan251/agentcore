"""Existing deliberate-risk path, with new marker and separate exact approval."""

from dataclasses import replace

import pytest

from agentcore.action_journal import DuplicateRiskInvocationContext, ActionState, ReconciliationDisposition
from agentcore.approvals import HumanApprovalDecision
from agentcore.integrations.gmail.send import GmailProbeCandidate
from gmail_send_fakes import ARGS, Harness, send_policy


@pytest.fixture
def risky(tmp_path):
    h = Harness(tmp_path)
    h.fake.after_effect = "timeout"
    h.first = h.invoke()
    h.old_preparation = h.preparation
    candidate = GmailProbeCandidate("one", "thread", True, True, h.fake.sends[0]["semantics"])
    h.fake.candidates = (candidate, replace(candidate, message_id="two"))
    h.reconcile(h.first.action_id)
    h.predecessor = h.actions.load(h.first.action_id)
    assert h.predecessor.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
    h.fake.after_effect = None
    h.gate.decision = HumanApprovalDecision.PENDING
    h.risk = DuplicateRiskInvocationContext(h.first.action_id, "act_eeeeeeeeeeee")
    h.prepare()
    yield h
    h.close()


def approve_risk(h):
    pending = h.invoke(risk=h.risk)
    assert pending.result.metadata["kind"] == "duplicate_risk_acknowledgement_required", pending.to_dict()
    assert len(h.fake.sends) == 1 and h.actions.list_actions() == [h.first.action_id]
    h.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
    h.risk = replace(h.risk, approval_request_id=pending.approval_request_id)
    return pending


def test_deliberate_repeat_needs_risk_ack_and_new_ordinary_approval_and_uses_new_marker(risky):
    h = risky
    pending_risk = approve_risk(h)
    pending_send = h.invoke(risk=h.risk)
    assert pending_send.result.metadata["kind"] == "approval_pending"
    assert pending_send.approval_request_id != pending_risk.approval_request_id
    assert len(h.fake.sends) == 1
    h.authority.record_decision(pending_send.approval_request_id, HumanApprovalDecision.APPROVE)
    call = h.invoke(risk=h.risk, approval=pending_send.approval_request_id)
    assert call.ok, call.to_dict()
    successor = h.actions.load(call.action_id)
    assert successor.action_id == h.risk.successor_action_id and len(successor.attempts) == 1
    assert successor.intent_digest != h.predecessor.intent_digest
    assert successor.duplicate_risk_acknowledgement.predecessor_action_id == h.first.action_id
    assert successor.authority.approval_request_id == pending_send.approval_request_id
    assert h.fake.sends[0]["marker"] != h.fake.sends[1]["marker"]
    assert h.fake.sends[0]["identity"].attempt_id != h.fake.sends[1]["identity"].attempt_id
    assert h.actions.load(h.first.action_id) == h.predecessor
    assert not h.invoke(risk=h.risk, approval=pending_risk.approval_request_id).ok
    assert len(h.fake.sends) == 2


def test_risky_repeat_cannot_reuse_old_message_id_even_with_identical_digest(risky):
    h = risky
    result = h.invoke(preparation=h.old_preparation, risk=h.risk)
    assert not result.ok and len(h.fake.sends) == 1
    assert h.actions.list_actions() == [h.first.action_id]
    assert not h.gate.prompts[1:]  # Only the original ordinary-send prompt.


@pytest.mark.parametrize("field", ["to", "cc", "bcc", "subject", "body"])
def test_risk_acknowledgement_cannot_change_predecessor_send_semantics(risky, field):
    h = risky
    args = dict(ARGS)
    args[field] = ["different@sample.invalid"] if field in {"to", "cc", "bcc"} else "different content"
    h.prepare(args)
    assert not h.invoke(args, risk=h.risk).ok
    assert h.actions.list_actions() == [h.first.action_id] and len(h.fake.sends) == 1


def test_current_permission_revocation_defeats_both_approved_risk_and_send_receipts(risky):
    h = risky
    approve_risk(h)
    pending = h.invoke(risk=h.risk)
    h.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
    h.executor.policy = send_policy(send=False)
    assert not h.invoke(risk=h.risk, approval=pending.approval_request_id).ok
    assert len(h.fake.sends) == 1


def test_zero_result_search_does_not_make_deliberate_repeat_eligible(tmp_path):
    h = Harness(tmp_path)
    try:
        h.fake.after_effect = "timeout"
        first = h.invoke()
        h.fake.candidates = ()
        h.reconcile(first.action_id)
        h.prepare()
        result = h.invoke(risk=DuplicateRiskInvocationContext(first.action_id, "act_eeeeeeeeeeee"))
        assert not result.ok and len(h.fake.sends) == 1
        assert h.actions.load(first.action_id).state is ActionState.RECONCILE_REQUIRED
        assert h.actions.list_actions() == [first.action_id]
    finally:
        h.close()
