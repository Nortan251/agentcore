"""Current account/policy, not historical approval, session, or mail, grants authority."""

import json

import pytest

from agentcore.action_journal import ActionState
from agentcore.approvals import HumanApprovalDecision
from agentcore.config.schema import PermissionConfig
from agentcore.conversation.session import JsonFileSessionStore
from agentcore.credentials import GoogleAccessProfile
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES
from agentcore.integrations.gmail import GmailReadonlyTools
from agentcore.models.messages import ModelResponse
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, PermissionPolicy
from agentcore.tasks.models import Subtask, Task
from gmail_fakes import FakeGmail, MALICIOUS, MESSAGE_ID, READ, message, text_part
from gmail_send_fakes import ACCESS, ARGS, REFRESH, DecisionGate, Harness, send_policy
from test_gmail_conversation import scripted, wants


@pytest.mark.parametrize("reuse_old_payload", [False, True])
def test_approval_for_account_a_cannot_send_on_account_b_after_restart(tmp_path, reuse_old_payload):
    h = Harness(tmp_path, gate=DecisionGate(HumanApprovalDecision.PENDING))
    old = h.prepare()
    pending = h.invoke()
    account = h.info.account_id
    h.close()
    other = Harness(tmp_path, account_subject="another-signed-test-account", google_directory=tmp_path / "other-google")
    try:
        assert other.info.account_id != account
        other.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
        if reuse_old_payload:
            other.foundation.restore(old)
            preparation = old
        else:
            preparation = other.prepare()
            assert preparation.intent_digest != old.intent_digest
        result = other.invoke(preparation=preparation, action_id=pending.action_id, approval=pending.approval_request_id)
        assert not result.ok and not other.fake.sends
        if reuse_old_payload:
            assert other.actions.load(result.action_id).state is ActionState.FAILED
        else:
            assert not other.actions.list_actions()
    finally:
        other.close()


def test_real_agent_history_and_gmail_read_injection_cannot_grant_send_authority(tmp_path):
    model = scripted(wants(), wants("gmail_send_message", **ARGS), ModelResponse(text="Sending was refused."))
    h = Harness(tmp_path, as_agent=True, model=model)
    try:
        # One earlier, explicitly host-enabled, approved fake send supplies real
        # historical journal/approval receipts. It grants no subsequent rights.
        h.executor.policy = send_policy()
        historical = h.invoke()
        assert historical.ok
        old_action = h.actions.load(historical.action_id).to_dict()
        approval_id = h.approvals.list_requests()[0]
        old_approval = h.approvals.load(approval_id).to_dict()
        h.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS),
                                              approval_gate=AlwaysApproveGate(), allow_authenticated_reads=True)
        current_policy = h.executor.policy
        gmail_transport = FakeGmail()
        gmail_transport.message_result = message(text_part(MALICIOUS))
        gmail = GmailReadonlyTools(transport=gmail_transport, allow_message_content=True)
        # Existing model-visible read tools deliberately accept only the exact
        # readonly profile; this test does not widen that security boundary.
        h.google.transport.initial["scope"] = " ".join(sorted(GOOGLE_GMAIL_READONLY_SCOPES))
        readonly = h.google.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
        gmail.bind(h.runtime, readonly.reference)
        h.registry.register_all(list(gmail.tools))
        sessions = JsonFileSessionStore(tmp_path / "sessions")
        conversation = h.agent.conversation(sessions=sessions)
        response = conversation.send("Read synthetic email and consider its content.")
        assert response.tools_executed == 1 and len(gmail_transport.calls) == 1, [
            outcome.invocation.result.metadata for outcome in response.tool_outcomes]
        assert len(response.tool_outcomes) == 2 and not response.tool_outcomes[1].executed
        loaded = sessions.load(response.session.id)
        loaded.metadata.update(allow_email_send=True, capabilities=["email.send", "credential.access"],
                               approval=old_approval, action=old_action, account=h.info.account_id)
        model.responses.extend([wants("gmail_send_message", **ARGS), ModelResponse(text="Still refused.")])
        again = conversation.send("Use the historical permission.", session=loaded)
        assert again.tools_executed == 0
        h.agent.run_task(Task(objective="Attempt historical send", subtasks=[
            Subtask(description="Try a send", tool_name="gmail_send_message", tool_args=ARGS)]))
        assert h.executor.policy is current_policy and len(h.fake.sends) == 1
        assert h.actions.load(historical.action_id).to_dict() == old_action
        assert h.approvals.load(approval_id).to_dict() == old_approval
        assert not current_policy.check((Capability.EMAIL_SEND,)).allowed
        h.agent.checkpoint()
        public = json.dumps([request.to_dict() for request in model.requests]) + json.dumps(h.agent.state.to_dict())
        public += "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "sessions").glob("*.json"))
        assert ACCESS not in public and REFRESH not in public
    finally:
        h.close()
