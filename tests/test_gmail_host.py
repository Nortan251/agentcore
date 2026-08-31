"""Real host composition/parser/approval/journal with all Internet egress denied."""

import ast
import inspect
import io
import json
import socket

import pytest

from agentcore.action_journal import ActionState
from agentcore.approvals import ApprovalState
from agentcore.credentials import CredentialError
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.errors import ToolNotFoundError
from agentcore.gmail_host import cli
from agentcore.gmail_host.composition import (
    GmailHostComposition, GmailHostMode,
)
from agentcore.gmail_host.errors import GmailHostError, HostFailure
from agentcore.gmail_host.operator import PrivateConsole
from agentcore.integrations.gmail.mutation_http import GmailMutationHttp
from agentcore.integrations.gmail.send_intent import SendIntentError
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from gmail_host_fakes import BODY, SUBJECT, FakeOperator, HostHarness
from gmail_mutation_http_fakes import (
    Reply, SENDER, boundary, no_network,
)
from google_oauth_fakes import ACCESS, REFRESH, CLIENT_SECRET, ID_TOKEN


@pytest.fixture
def harness(tmp_path):
    return HostHarness(tmp_path)


def prepare(harness, boundary):
    operator = FakeOperator(SUBJECT, BODY)
    with harness.host(GmailHostMode.PREPARE, operator=operator) as host:
        slot = host.prepare_smoke_test()
        review = host.foundation.review(slot.preparation)
    return slot, review, operator


def unknown_send(harness, boundary):
    slot, review, _ = prepare(harness, boundary)
    boundary.send = Reply(error=TimeoutError(ACCESS + REFRESH + SUBJECT + BODY))
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"],
                               operator=FakeOperator("CHALLENGE"))
    assert code == 2 and "RECONCILE_REQUIRED" in output
    return slot, review, output


def test_default_composition_is_absent_even_with_send_scope_and_transport(harness, boundary, monkeypatch):
    monkeypatch.setenv("AGENTCORE_ALLOW_EMAIL_SEND", "true")
    monkeypatch.setenv("GMAIL_HOST_LIVE_SEND", "1")
    transport = GmailMutationHttp()
    assert transport is not None
    assert "gmail_send_message" not in {tool.spec.name for tool in default_tools()}
    assert not PermissionPolicy().allow_email_send
    with harness.host() as host:
        assert host.registry.names() == []
        assert host.foundation is None and not host.mutation_enabled
        assert host.services.google.restore().credential.scopes == GOOGLE_GMAIL_SEND_SCOPES
        assert not boundary.requests
        assert host.services.actions.list_actions() == []


@pytest.mark.parametrize("mode", ["live_send", True, {"allow_email_send": True}, None])
def test_model_or_saved_values_cannot_select_host_mode(harness, mode, boundary):
    with harness.services() as services:
        with pytest.raises(GmailHostError):
            GmailHostComposition(services, FakeOperator(), transport=GmailMutationHttp(), mode=mode)
    assert not boundary.requests


@pytest.mark.parametrize("flag", ["--token", "--access-token", "--refresh-token", "--client-secret",
                                 "--body", "--subject", "--recipient", "--cc", "--bcc", "--scope",
                                 "--config-url", "--allow-email-send", "--live"])
def test_cli_rejects_private_or_authority_arguments_without_echo(harness, flag, boundary):
    code, output = harness.cli(["prepare-smoke-test", flag, ACCESS + SUBJECT])
    assert code == 2 and harness.opens == 0 and not boundary.requests
    assert ACCESS not in output and SUBJECT not in output


def test_status_is_offline_categorical_and_contains_no_credentials(harness, boundary):
    code, output = harness.cli(["status"])
    assert code == 0 and "Mutation composition: DISABLED" in output
    assert "GMAIL_SEND profile available: True" in output
    assert not boundary.requests
    assert all(secret not in output for secret in (ACCESS, REFRESH, CLIENT_SECRET, ID_TOKEN, SENDER))
    assert not harness.last_google.transport.refreshes


def test_connect_uses_actual_existing_lifecycle_and_exact_profile(tmp_path, boundary):
    harness = HostHarness(tmp_path, connected=False)
    code, output = harness.cli(["connect"])
    assert code == 0 and "durably committed" in output and "DISABLED" in output
    assert harness.last_google.service is not None
    assert len(harness.last_google.transport.exchanges) == 1
    assert "https://www.googleapis.com/auth/gmail.send" in harness.last_google.browser.parameters["scope"].split()
    assert not boundary.requests


def test_prepare_uses_production_graph_and_private_output_only(harness, boundary, caplog):
    operator = FakeOperator(SUBJECT, BODY)
    with harness.host(GmailHostMode.PREPARE, operator=operator) as host:
        slot = host.prepare_smoke_test()
        review = host.foundation.review(slot.preparation)
        assert host.executor.registry is host.registry
        assert host.executor._action_store is host.services.actions
        assert host.executor._approval_authority is host.authority
        assert host.executor._protected_payload_store is host.services.payloads
        assert host.executor._evidence_store is host.services.evidence
        assert host.executor._credential_runtime is host.services.runtime
        assert host.recovery._executor is host.executor
        assert host.registry.get("gmail_send_message") is host.foundation.tool
        assert not host.mutation_enabled
        assert host.services.actions.list_actions() == []
        assert host.services.approvals.list_requests() == []
        assert review["sender"] == SENDER and review["message"]["to"] == [SENDER]
        assert review["message"]["subject"] == SUBJECT and review["message"]["body"] == BODY + "\r\n"
        private = "".join(operator.private)
        assert all(value in private for value in (SENDER, SUBJECT, BODY, review["message_id"], slot.action_id))
        assert "Cc: NONE" in private and "Bcc: NONE" in private
        assert "THIS WILL SEND A REAL EMAIL IF EXECUTED" in private
        assert host.action_status().state == "PREPARED"
    assert [request.method for request in boundary.requests] == ["GET"]
    ordinary = harness.ordinary() + repr(harness.logger.events) + caplog.text
    assert all(value not in ordinary for value in (SENDER, SUBJECT, BODY, ACCESS, REFRESH, review["message_id"]))


def test_actual_cli_prepares_without_any_content_arguments(harness, boundary, capsys):
    operator = FakeOperator(SUBJECT, BODY)
    code, output = harness.cli(["prepare-smoke-test"], operator=operator)
    assert code == 0 and "PREPARED" in output
    assert "Stopped before live approval" in output and not boundary.posts
    assert SUBJECT not in output + capsys.readouterr().out and BODY not in output
    assert SUBJECT in "".join(operator.private)


@pytest.mark.parametrize("change", [
    {"to": ["someone@sample.invalid"]}, {"to": [SENDER, "someone@sample.invalid"]},
    {"cc": ["copy@sample.invalid"]}, {"bcc": ["blind@sample.invalid"]},
    {"html": "<p>test</p>"}, {"body": "<html><body>test</body></html>"},
    {"attachments": []}, {"reply_to": "thread"}, {"thread_id": "thread"},
    {"forward": True}, {"subject": "Re: old message"}, {"subject": "Fwd: old message"},
    {"subject": " Re: old message"}, {"subject": "  fW: old message"},
    {"subject": "\u00a0Fwd: old message"},
    {"subject": "x" * 161}, {"body": "x" * 1025},
    {"subject": ""}, {"body": ""}, {"body": "x\n" * 9},
    {"subject": "header\r\nBcc: outside@sample.invalid"}, {"body": "\x1b[2Jhidden"},
])
def test_smoke_restrictions_preserve_one_self_send(harness, boundary, change):
    args = {"to": [SENDER], "subject": SUBJECT, "body": BODY, **change}
    with harness.host(GmailHostMode.PREPARE) as host:
        with pytest.raises((GmailHostError, SendIntentError)):
            host.prepare_smoke_test(args)
        assert host.services.storage.load_slot() is None
        assert host.services.actions.list_actions() == []
    assert not boundary.posts


def test_second_preparation_is_never_created_even_after_restart(harness, boundary):
    slot, review, _ = prepare(harness, boundary)
    requests = len(boundary.requests)
    with harness.host(GmailHostMode.PREPARE) as host:
        with pytest.raises(GmailHostError, match=HostFailure.EXISTING.value):
            host.prepare_smoke_test()
        assert host.services.storage.load_slot() == slot
        assert host.foundation.review(slot.preparation) == review
    assert len(boundary.requests) == requests


def test_saved_preparation_and_session_do_not_enable_composition(harness, boundary, monkeypatch):
    slot, _, _ = prepare(harness, boundary)
    (harness.directory / "session.json").write_text(json.dumps({
        "allow_email_send": True, "mode": "live_send", "action_id": slot.action_id, "approved": True,
    }), encoding="utf-8")
    monkeypatch.setenv("GMAIL_SEND_APPROVED", slot.action_id)
    count = len(boundary.requests)
    with harness.host() as host:
        assert host.registry.names() == [] and not host.mutation_enabled
        assert host.services.storage.load_slot() == slot
        with pytest.raises(ToolNotFoundError):
            host.executor.invoke("gmail_send_message", {"allow_email_send": True})
    assert len(boundary.requests) == count and not boundary.posts


@pytest.mark.parametrize("reply", [
    Reply({"emailAddress": "bad\naddress"}),
    Reply({"emailAddress": "sender@sample.invalid", "historyId": "private"}),
    Reply({"emailAddress": ACCESS + "@sample.invalid"}),
    Reply(status=500, raw=(ACCESS + REFRESH).encode()),
])
def test_primary_profile_failure_never_prepares_or_discloses(harness, boundary, reply, capsys):
    boundary.profile = reply
    code, output = harness.cli(["prepare-smoke-test"], operator=FakeOperator(SUBJECT, BODY))
    captured = capsys.readouterr()
    assert code == 2 and not boundary.posts
    assert all(secret not in output + captured.out + captured.err for secret in (ACCESS, REFRESH, SUBJECT, BODY))
    with harness.services() as services:
        assert services.storage.load_slot() is None and services.actions.list_actions() == []


def test_no_live_flag_stops_before_services_or_network(harness, boundary):
    code, output = harness.cli(["send-smoke-test", "act_123456789abc"])
    assert code == 2 and harness.opens == 0 and "pre-live boundary" in output
    assert not boundary.requests


def test_preparation_mode_cannot_invoke_even_with_valid_confirmation(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    with harness.host(GmailHostMode.PREPARE, operator=FakeOperator("CHALLENGE")) as host:
        with pytest.raises(GmailHostError, match=HostFailure.DISABLED.value):
            host.send_smoke_test(slot.action_id)
    assert not boundary.posts


@pytest.mark.parametrize("answer", ["", "yes", "approve", "SEND act_123456789abc 0000000000000000"])
def test_first_gate_without_exact_second_gate_cannot_send(harness, boundary, answer):
    slot, _, _ = prepare(harness, boundary)
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator(answer)) as host:
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        assert host.services.actions.list_actions() == []
        request = host.services.approvals.load(invocation.approval_request_id)
        assert request.state is ApprovalState.DENIED
    assert not boundary.posts


def test_redirected_terminal_cannot_satisfy_live_gate(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    count = len(boundary.requests)
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"],
                               operator=FakeOperator("CHALLENGE", interactive=False))
    assert code == 2 and "private_interactive_terminal_required" in output
    assert len(boundary.requests) == count and not boundary.posts


def test_production_console_rejects_redirected_stdio(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("model provided confirmation"))
    with pytest.raises(GmailHostError):
        PrivateConsole().require_interactive()


def test_success_consumes_durable_approval_and_journals_exactly_one_post(harness, boundary):
    slot, review, _ = prepare(harness, boundary)
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        invocation = host.send_smoke_test(slot.action_id)
        assert invocation.result.ok, invocation.result
        assert invocation.action_id == slot.action_id
        record = host.services.actions.load(slot.action_id)
        assert record.state is ActionState.SUCCEEDED and len(record.attempts) == 1
        assert host.services.approvals.load(invocation.approval_request_id).state is ApprovalState.CONSUMED
        assert host.foundation.review(slot.preparation) == review
        assert host.action_status().attempts == 1
        assert host.action_status().attempt_id == record.attempts[0].attempt_id
        assert record.intent_digest == slot.preparation.intent_digest
    assert len(boundary.posts) == 1
    mime = boundary.mime
    assert review["message_id"].encode() in mime and ("To: " + SENDER).encode() in mime
    assert b"Cc:" not in mime and b"Bcc:" not in mime and b"text/plain" in mime
    assert [request.method for request in boundary.requests] == ["GET", "GET", "GET", "POST"]
    assert all(value not in harness.ordinary() for value in (SUBJECT, BODY, SENDER, ACCESS, REFRESH))
    code, output = harness.cli(["action-status", slot.action_id])
    assert code == 0 and record.attempts[0].attempt_id in output
    count = len(boundary.requests)
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"))
    assert code == 2 and "action_inspection_required" in output
    assert len(boundary.requests) == count  # no retry, refresh, or profile read


def test_executor_without_armed_human_gate_stays_pending(harness, boundary):
    slot, review, _ = prepare(harness, boundary)
    with harness.host(GmailHostMode.LIVE_SEND) as host:
        host._activate(verify_primary=True)
        invocation = host.executor.invoke("gmail_send_message", review["message"],
                                          action_context=slot.preparation.context())
        assert not invocation.result.ok
        assert host.services.approvals.load(invocation.approval_request_id).state is ApprovalState.PENDING
        assert host.services.actions.list_actions() == []
        with pytest.raises(CredentialError):
            host.foundation.tool.run(review["message"], ToolContext())
    assert not boundary.posts


def test_cli_orchestrator_never_calls_tool_or_send_transport_directly():
    for module in (cli, __import__("agentcore.gmail_host.composition", fromlist=["x"])):
        tree = ast.parse(inspect.getsource(module))
        assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and node.func.attr in {"run", "send"} for node in ast.walk(tree))


def test_reconcile_uses_coordinator_gets_only_and_preserves_action(harness, boundary):
    slot, review, output = unknown_send(harness, boundary)
    assert "DO NOT SEND AGAIN" in output
    before = len(boundary.requests)
    with harness.host(GmailHostMode.RECONCILE) as host:
        assert not host.mutation_enabled
        assert host.executor.policy.allow_authenticated_reconciliation
        old_approvals = host.services.approvals.list_requests()
        report = host.reconcile(slot.action_id)
        assert report.action_state is ActionState.SUCCEEDED
        assert host.services.actions.list_actions() == [slot.action_id]
        assert host.services.approvals.list_requests() == old_approvals
        assert host.foundation.review(slot.preparation) == review
        # Successful terminal truth clears the existing unresolved-probe quota
        # metadata; it is not a lifetime HTTP request counter.
        assert host.action_status().probe_count == 0
        assert host.action_status().effect == "CONFIRMED_APPLIED"
    assert [request.method for request in boundary.requests[before:]] == ["GET", "GET"]
    assert len(boundary.posts) == 1


def test_zero_matches_never_claim_not_sent(harness, boundary):
    slot, _, _ = unknown_send(harness, boundary)
    boundary.search = Reply({"messages": [], "resultSizeEstimate": 0})
    code, output = harness.cli(["reconcile", slot.action_id])
    assert code == 0 and "RECONCILE_REQUIRED" in output and "DO NOT SEND AGAIN" in output
    assert "No conclusive Sent match found; effect remains uncertain." in output
    assert "email was not sent" not in output and "not sent" not in output
    assert len(boundary.posts) == 1


def test_multiple_matches_requires_human_and_never_selects_send(harness, boundary):
    slot, _, _ = unknown_send(harness, boundary)
    boundary.search = Reply({"messages": [
        {"id": "syntheticmessage1", "threadId": "syntheticthread1"},
        {"id": "syntheticmessage2", "threadId": "syntheticthread1"},
    ], "resultSizeEstimate": 2})
    code, output = harness.cli(["reconcile", slot.action_id])
    assert code == 0 and "Human confirmation required" in output and "DO NOT SEND AGAIN" in output
    count = len(boundary.requests)
    code, output = harness.cli(["reconcile", slot.action_id])
    assert len(boundary.requests) == count and len(boundary.posts) == 1


def test_uncertain_send_command_offers_no_resend_or_new_preparation(harness, boundary):
    slot, _, _ = unknown_send(harness, boundary)
    count = len(boundary.requests)
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"))
    assert code == 2 and "DO NOT SEND AGAIN" in output
    code, output = harness.cli(["prepare-smoke-test"], operator=FakeOperator(SUBJECT, BODY))
    assert code == 2 and "one_preparation_already_exists" in output
    assert len(boundary.requests) == count and len(boundary.posts) == 1


@pytest.mark.parametrize("command", ["reconcile", "action-status", "review", "send-smoke-test"])
def test_unknown_action_cannot_select_or_create_anything(harness, boundary, command):
    slot, _, _ = prepare(harness, boundary)
    count = len(boundary.requests)
    code, output = harness.cli([command, "act_123456789abc"] + (["--live-send"] if command == "send-smoke-test" else []))
    assert code == 2 and "exact_preparation_required" in output
    assert len(boundary.requests) == count and not boundary.posts


def test_prepared_action_reconciliation_cannot_invoke_or_create_send(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    count = len(boundary.requests)
    code, output = harness.cli(["reconcile", slot.action_id])
    assert code == 2 and "action_inspection_required" in output
    assert len(boundary.requests) == count and not boundary.posts


def test_no_background_or_scheduled_command(harness, boundary):
    for command in ("schedule", "send-later", "watch", "repeat", "poll"):
        code, output = harness.cli([command])
        assert code == 2
    assert harness.opens == 0 and not boundary.requests


@pytest.mark.parametrize("call", [
    lambda: socket.getaddrinfo("gmail.googleapis.com", 443),
    lambda: socket.gethostbyname("gmail.googleapis.com"),
    lambda: socket.create_connection(("gmail.googleapis.com", 443)),
    lambda: socket.socket().connect(("127.0.0.1", 443)),
    lambda: socket.socket().send(b"synthetic"),
    lambda: socket.socket().sendall(b"synthetic"),
    lambda: socket.socket().sendto(b"synthetic", ("127.0.0.1", 443)),
])
def test_automated_egress_is_forbidden(call):
    with pytest.raises(AssertionError, match="cannot use DNS or real sockets"):
        call()
