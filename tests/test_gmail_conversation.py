"""Real Agent/conversation evidence: email is data, never current authority."""

import json
import logging
from types import SimpleNamespace

import pytest

from conftest import journal_for_tools
from agentcore.approvals import ApprovalInvocationContext
from agentcore.config.schema import AgentConfig, ConversationConfig, LoggingConfig, PermissionConfig
from agentcore.conversation.session import JsonFileSessionStore
from agentcore.conversation.tool_use import TOOL_INSTRUCTIONS
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.credentials import CredentialFailure, CredentialRuntime
from agentcore.integrations.gmail import GmailReadonlyTools
from agentcore.models.messages import ModelResponse, Role, ToolCall
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, PermissionPolicy
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import Subtask, Task
from agentcore.tools.base import FunctionTool, tool_spec
from agentcore.tools.executor import ToolExecutor
from gmail_fakes import FakeGmail, GMAIL_SCOPES, MALICIOUS, MESSAGE_ID, READ, SEARCH, connected, message, text_part
from google_oauth_fakes import ACCESS, CLIENT_SECRET, ID_TOKEN, REFRESH


def scripted(*responses):
    model = SimpleNamespace(requests=[], responses=list(responses))
    def generate(request):
        model.requests.append(request)
        return model.responses.pop(0)
    model.generate = generate
    return model


def wants(name=READ, **arguments):
    return ModelResponse(text="", tool_calls=[ToolCall(id="synthetic-call", name=name,
                                                      arguments=arguments or {"message_id": MESSAGE_ID})])


@pytest.mark.parametrize("allow", [False, True])
def test_email_injection_is_only_tool_data_and_cannot_change_live_or_historical_authority(tmp_path, allow):
    value = connected(tmp_path / "protected")
    transport = FakeGmail()
    transport.message_result = message(text_part(MALICIOUS))
    gmail = GmailReadonlyTools(transport=transport)
    runtime = CredentialRuntime(value.vault)
    gmail.bind(runtime, value.info.reference)
    model = scripted(wants(), ModelResponse(text="The selected message was handled."))
    mutator = FunctionTool(tool_spec("synthetic_local_action", "Test-only historical approval fixture.",
                                     capabilities=(Capability.FS_WRITE,), read_only=False), lambda *_: "synthetic success")
    journal = journal_for_tools([mutator])
    states, sessions = JsonFileStateStore(tmp_path / "states"), JsonFileSessionStore(tmp_path / "sessions")
    log_path = tmp_path / "events.log"
    config = AgentConfig(
        workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.CONFIRM),
        conversation=ConversationConfig(tools_enabled=True, max_tool_result_chars=8000),
        logging=LoggingConfig(level="DEBUG", file_path=str(log_path)),
    )
    agent = Agent(config, AgentDependencies(
        tools=[*gmail.tools, mutator], model=model, credential_runtime=runtime,
        allow_authenticated_reads=allow, approval_gate=AlwaysApproveGate(), state_store=states, **journal))
    try:
        historical = agent.executor.invoke(mutator.name)
        assert historical.ok
        old_record = journal["action_store"].load(historical.action_id).to_dict()
        approvals = agent.executor._approval_authority._store
        approval_ids = approvals.list_requests()
        assert len(approval_ids) == 1
        old_approval = approvals.load(approval_ids[0]).to_dict()
        policy = agent.executor.policy
        before_binding = runtime._snapshot(gmail.tools[1])
        conversation = agent.conversation(sessions=sessions)
        assert conversation.tools.executor is agent.executor is agent.runner.executor
        response = conversation.send("Read only the explicitly selected synthetic message.")
        assert response.ok and response.tools_executed == int(allow)
        assert len(transport.calls) == int(allow) and len(model.requests) == 2
        tool_messages = [m for m in model.requests[1].messages if m.role is Role.TOOL]
        assert len(tool_messages) == 1 and tool_messages[0].name == READ
        assert "DATA, not instructions" in tool_messages[0].content
        assert ("EMAIL-INJECTION-SENTINEL" in tool_messages[0].content) is allow
        system_messages = [m for m in model.requests[1].messages if m.role is Role.SYSTEM]
        assert any(TOOL_INSTRUCTIONS in m.content for m in system_messages)
        assert all("EMAIL-INJECTION-SENTINEL" not in m.content for m in system_messages)
        assert agent.executor.policy is policy and runtime._snapshot(gmail.tools[1]) is before_binding
        assert journal["action_store"].list_actions() == [historical.action_id]
        assert journal["action_store"].load(historical.action_id).to_dict() == old_record
        assert approvals.load(approval_ids[0]).to_dict() == old_approval

        # Autonomous Gmail reads use the same executor and cannot change the
        # existing journal/approval record either.
        agent.run_task(Task(objective="Read synthetic mail", subtasks=[
            Subtask(description="Read one message", tool_name=READ, tool_args={"message_id": MESSAGE_ID})]))
        assert len(transport.calls) == 2 * int(allow)
        agent.checkpoint()
        assert states.load(agent.state.run_id).tool_calls
        assert journal["action_store"].load(historical.action_id).to_dict() == old_record

        # A consumed approval, loaded session, and prior read result grant no
        # current permission. Spoofed session metadata is not a host opt-in.
        loaded = sessions.load(response.session.id)
        loaded.metadata.update(scopes=GMAIL_SCOPES, allow_authenticated_reads=True,
                               account=value.info.account_id, credential_reference=value.info.reference)
        agent.executor.policy = PermissionPolicy(config.permissions, approval_gate=AlwaysApproveGate())
        denied = agent.executor.invoke(READ, {"message_id": MESSAGE_ID},
                                       approval_context=ApprovalInvocationContext(approval_ids[0]))
        assert denied.result.metadata["kind"] == "permission_denied"
        model.responses.extend([wants(), ModelResponse(text="Read unavailable.")])
        assert conversation.send("Read again.", session=loaded).tools_executed == 0
        assert len(transport.calls) == 2 * int(allow)
        no_runtime = ToolExecutor(agent.registry, PermissionPolicy(allow_authenticated_reads=True))
        missing = no_runtime.invoke(READ, {"message_id": MESSAGE_ID},
                                    approval_context=ApprovalInvocationContext(approval_ids[0]))
        assert missing.result.metadata["kind"] == CredentialFailure.UNAVAILABLE.value

        # Disconnect blocks old bindings even if the host later restores its
        # opt-in; no saved receipt or transcript can restore access material.
        value.service.disconnect(value.info.reference)
        agent.executor.policy = PermissionPolicy(config.permissions, allow_authenticated_reads=True)
        revoked = agent.executor.invoke(READ, {"message_id": MESSAGE_ID},
                                       approval_context=ApprovalInvocationContext(approval_ids[0]))
        assert revoked.result.metadata["kind"] == CredentialFailure.REVOKED.value
        assert len(value.transport.exchanges) == 1 and not value.transport.refreshes
        assert journal["action_store"].load(historical.action_id).to_dict() == old_record
        for capability in (Capability.EMAIL_SEND, Capability.PAYMENT, Capability.SYSTEM_ADMIN):
            assert not agent.executor.policy.check((capability,)).allowed
        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
        public = json.dumps([agent.state.to_dict(), old_record, old_approval,
                             [r.to_dict() for r in model.requests]], default=str)
        public += log_path.read_text(encoding="utf-8")
        public += "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "sessions").glob("*.json"))
        for secret in (ACCESS, REFRESH, CLIENT_SECRET, ID_TOKEN):
            assert secret not in public
        assert MALICIOUS not in log_path.read_text(encoding="utf-8")
        assert "EMAIL-INJECTION-SENTINEL" not in json.dumps(old_record)
    finally:
        value.vault.close()
        configure_logging(LoggingConfig(level="CRITICAL"))


@pytest.mark.parametrize("followup", [
    ("gmail_send_message", {"to": "attacker@example.test"}),
    (READ, {"message_id": MESSAGE_ID, "account": "other"}),
    ("google_connect", {"profile": "gmail_readonly"}),
])
def test_model_followups_after_malicious_email_still_require_normal_tool_validation(tmp_path, followup):
    value = connected(tmp_path / "protected")
    transport = FakeGmail()
    transport.message_result = message(text_part(MALICIOUS))
    gmail, runtime = GmailReadonlyTools(transport=transport), CredentialRuntime(value.vault)
    gmail.bind(runtime, value.info.reference)
    name, args = followup
    model = scripted(wants(), wants(name, **args), ModelResponse(text="Unsafe follow-up refused."))
    agent = Agent(AgentConfig(workspace_dir=str(tmp_path), conversation=ConversationConfig(tools_enabled=True),
                              logging=LoggingConfig(level="CRITICAL")),
                  AgentDependencies(tools=list(gmail.tools), model=model, credential_runtime=runtime,
                                    allow_authenticated_reads=True))
    try:
        result = agent.conversation().send("Read the selected message.")
        assert result.tools_executed == 1 and len(transport.calls) == 1
        assert len(result.tool_outcomes) == 2
        assert not result.tool_outcomes[1].executed
        assert set(agent.registry.names()) == {SEARCH, READ}
        assert len(value.transport.exchanges) == 1 and not value.transport.refreshes
    finally:
        value.vault.close()
