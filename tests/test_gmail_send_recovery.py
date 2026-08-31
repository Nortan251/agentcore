"""Restart with durable stores and an actual child-process crash after effect."""

import base64
import json
import subprocess
import sys
from dataclasses import asdict

import pytest

from agentcore.action_journal import ActionReconciliationStatus, ActionState
from agentcore.approvals import HumanApprovalDecision
from agentcore.integrations.gmail.send import GmailProbeCandidate, GmailSendPreparation
from gmail_send_fakes import ARGS, DecisionGate, Harness, send_policy


def test_pending_exact_approval_survives_restart_with_identical_mime_and_fresh_credentials(tmp_path):
    h = Harness(tmp_path, gate=DecisionGate(HumanApprovalDecision.PENDING))
    handle = h.prepare()
    review = h.foundation.review(handle)
    mime = base64.b64decode(h.foundation._load_preparation(handle.preparation_id)[0]["mime_base64"])
    pending = h.invoke()
    info = h.info
    assert not pending.ok and pending.approval_request_id and not h.actions.list_actions()
    h.close()
    reopened = Harness(tmp_path, gate=DecisionGate(HumanApprovalDecision.PENDING))
    try:
        reopened.foundation.restore(handle)
        reopened.preparation = handle
        assert reopened.info.account_id == info.account_id and reopened.info.connection_id == info.connection_id
        assert reopened.info.generation > info.generation
        assert reopened.foundation.review(handle) == review
        assert not reopened.fake.sends and not reopened.fake.probes
        reopened.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
        call = reopened.invoke(action_id=pending.action_id, approval=pending.approval_request_id)
        assert call.ok, call.to_dict()
        assert reopened.fake.sends[0]["mime"] == mime
        assert reopened.fake.sends[0]["marker"] == review["message_id"]
        assert reopened.invoke(action_id=call.action_id, approval=pending.approval_request_id).ok
        assert len(reopened.fake.sends) == 1
        reopened.prepare()
        assert not reopened.invoke(approval=pending.approval_request_id).ok
        assert len(reopened.fake.sends) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("cut", ["provider_result", "success_publication"])
def test_real_process_crash_after_fake_effect_recovers_to_probe_without_resend(tmp_path, cut):
    code = "\n".join([
        "import base64, json, os, sys",
        "from pathlib import Path",
        "from dataclasses import asdict",
        "sys.path.insert(0, 'tests')",
        "from gmail_send_fakes import Harness",
        "from agentcore.action_journal import ActionState",
        "h = Harness(Path(sys.argv[1]))",
        "h.prepare()",
        "def effect(submission, lease):",
        "    record = h.actions.load(submission.identity.action_id)",
        "    assert record.state is ActionState.IN_FLIGHT",
        "    receipt = {'action_id': record.action_id, 'handle': asdict(h.preparation),",
        "               'semantics': submission.semantics, 'mime': base64.b64encode(submission.mime_bytes).decode(),",
        "               'account': h.info.account_id, 'connection': h.info.connection_id, 'generation': h.info.generation}",
        "    (h.directory / 'synthetic-remote-effect.json').write_text(json.dumps(receipt), encoding='utf-8')",
        "    if sys.argv[2] == 'provider_result': os._exit(71)",
        "    return dict(h.fake.response)",
        "h.fake.send_hook = effect",
        "cas = h.actions.compare_and_swap",
        "def publish(action_id, version, candidate):",
        "    if candidate.state is ActionState.SUCCEEDED: os._exit(71)",
        "    return cas(action_id, version, candidate)",
        "if sys.argv[2] == 'success_publication': h.actions.compare_and_swap = publish",
        "h.invoke()",
    ])
    crashed = subprocess.run([sys.executable, "-c", code, str(tmp_path), cut], capture_output=True, text=True, timeout=20)
    assert crashed.returncode == 71, crashed.stderr
    receipt = json.loads((tmp_path / "synthetic-remote-effect.json").read_text(encoding="utf-8"))
    stored = json.loads(next((tmp_path / "actions").glob("act_*.json")).read_text(encoding="utf-8"))
    assert stored["state"] == ActionState.IN_FLIGHT.value
    assert "credential_execution" not in json.dumps(stored)
    reopened = Harness(tmp_path)
    try:
        record = reopened.actions.load(receipt["action_id"])
        assert record.state is ActionState.RECONCILE_REQUIRED
        assert len(record.attempts) == 1 and not reopened.fake.sends and not reopened.fake.probes
        assert reopened.info.account_id == receipt["account"] and reopened.info.connection_id == receipt["connection"]
        assert reopened.info.generation > receipt["generation"]
        handle = GmailSendPreparation(**receipt["handle"])
        reopened.foundation.restore(handle)
        assert reopened.foundation._load_preparation(handle.preparation_id)[0]["mime_base64"] == receipt["mime"]
        reopened.fake.candidates = (GmailProbeCandidate("syntheticmessage1", "syntheticthread1", True, True, receipt["semantics"]),)
        assert reopened.reconcile(record.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
        assert reopened.invoke(preparation=handle, action_id=record.action_id).ok
        assert not reopened.fake.sends and len(reopened.fake.probes) == 1
    finally:
        reopened.close()


def test_recovery_does_not_restore_permission_from_old_approval_or_journal(tmp_path):
    h = Harness(tmp_path)
    h.fake.after_effect = "timeout"
    first = h.invoke()
    handle = h.preparation
    h.close()
    reopened = Harness(tmp_path, policy=send_policy(send=False, probe=False))
    try:
        assert reopened.actions.load(first.action_id).state is ActionState.RECONCILE_REQUIRED
        assert reopened.reconcile(first.action_id).status is ActionReconciliationStatus.PROBE_DENIED
        reopened.foundation.restore(handle)
        assert not reopened.invoke(preparation=handle, action_id=first.action_id).ok
        assert not reopened.fake.sends and not reopened.fake.probes
    finally:
        reopened.close()
