"""Adversarial lifecycle tests for the same production host orchestration."""

import io
import json
import os
import pickle
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agentcore.action_journal import ActionState
from agentcore.approvals import ApprovalPrompt, ApprovalState, HumanApprovalDecision
from agentcore.credentials import CredentialError, GoogleAccessProfile
from agentcore.credentials._protected_file import ProtectedFile
from agentcore.gmail_host import cli
from agentcore.gmail_host.composition import GmailHostMode, CONFIRMATION_SECONDS
from agentcore.gmail_host.errors import GmailHostError, HostFailure
from agentcore.gmail_host.storage import GmailHostStorage
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from gmail_host_fakes import BODY, SUBJECT, FakeOperator, HostHarness
from gmail_mutation_http_fakes import Reply, boundary, no_network
from google_oauth_fakes import ACCESS, REFRESH, CLIENT_ID, CLIENT_SECRET
from test_gmail_host import prepare, unknown_send


@pytest.fixture
def harness(tmp_path):
    return HostHarness(tmp_path)


def prompt_for(request):
    return ApprovalPrompt(
        request.request_id, request.action_kind, request.tool_name, request.tool_version,
        request.mutation_capabilities, request.principal_ref, request.scope_ref,
        "Review one protected send.",
    )


def pending_request(host, slot):
    pending = host.authority.authorize(host._approval_binding(slot), action_id=slot.action_id,
                                       description="Review one protected send.")
    return host.services.approvals.load(pending.request_id)


def test_confirmation_for_a_different_action_or_preparation_is_rejected(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    operator = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        host._activate(verify_primary=True)
        request = pending_request(host, slot)
        different = replace(slot, action_id="act_123456789abc")
        with host.gate.arm(request.request_id, different):
            assert host.gate.decide(prompt_for(request)) is HumanApprovalDecision.DENY
        changed = replace(slot, preparation=replace(slot.preparation, intent_digest="0" * 64))
        with host.gate.arm(request.request_id, changed):
            assert host.gate.decide(prompt_for(request)) is HumanApprovalDecision.DENY
        assert operator.prompts == []
    assert not boundary.posts


def test_one_time_gate_cannot_be_replayed(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    operator = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        host._activate(verify_primary=True)
        request = pending_request(host, slot)
        with host.gate.arm(request.request_id, slot):
            assert host.gate.decide(prompt_for(request)) is HumanApprovalDecision.APPROVE
            assert host.gate.decide(prompt_for(request)) is HumanApprovalDecision.PENDING
        assert host.gate.decide(prompt_for(request)) is HumanApprovalDecision.PENDING
        assert host.services.approvals.load(request.request_id).state is ApprovalState.PENDING
    assert not boundary.posts  # the UX decision alone is never execution authority


def test_previous_durable_approval_cannot_skip_new_private_confirmation(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    with harness.host(GmailHostMode.LIVE_SEND) as host:
        request = pending_request(host, slot)
        host.authority.record_decision(request.request_id, HumanApprovalDecision.APPROVE)
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("no")) as host:
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        assert invocation.approval_request_id != request.request_id
        assert host.services.approvals.load(request.request_id).state is ApprovalState.APPROVED
        assert host.services.approvals.load(invocation.approval_request_id).state is ApprovalState.DENIED
    assert not boundary.posts


def test_stale_challenge_cannot_approve_a_fresh_request(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    first = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=first) as host:
        host._activate(verify_primary=True)
        request = pending_request(host, slot)
        with host.gate.arm(request.request_id, slot):
            assert host.gate.decide(prompt_for(request)) is HumanApprovalDecision.APPROVE
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator(first.last_challenge)) as host:
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        assert host.services.approvals.load(invocation.approval_request_id).state is ApprovalState.DENIED
    assert not boundary.posts


def test_confirmation_time_bound_and_clock_change_cannot_send(harness, boundary, monkeypatch):
    slot, _, _ = prepare(harness, boundary)
    import agentcore.gmail_host.composition as composition
    real_monotonic = composition.time.monotonic
    operator = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        operator.on_input = lambda: monkeypatch.setattr(
            composition.time, "monotonic", lambda: real_monotonic() + CONFIRMATION_SECONDS + 1)
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
    assert not boundary.posts


def test_current_permission_revocation_during_confirmation_denies(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    operator = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        operator.on_input = lambda: setattr(host.executor, "policy", PermissionPolicy())
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        assert host.services.actions.list_actions() == []
    assert not boundary.posts


def test_disconnect_during_private_confirmation_denies(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    operator = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        operator.on_input = host.services.disconnect
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        assert host.services.actions.list_actions() == []
    assert not boundary.posts


def test_missing_durable_authority_prevents_execution(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    operator = FakeOperator("CHALLENGE")
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        host.executor._approval_authority = None
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        assert not operator.prompts and host.services.actions.list_actions() == []
    assert not boundary.posts


def test_approval_consumed_and_in_flight_are_durable_before_post(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    observations = []
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        def at_wire(request):
            if request.method == "POST":
                action = host.services.actions.load(slot.action_id)
                approvals = [host.services.approvals.load(ref) for ref in host.services.approvals.list_requests()]
                assert action.state is ActionState.IN_FLIGHT and len(action.attempts) == 1
                assert len(approvals) == 1 and approvals[0].state is ApprovalState.CONSUMED
                assert action.intent_digest == slot.preparation.intent_digest
                observations.append(action.attempts[0].attempt_id)
        boundary.on_write = at_wire
        assert host.send_smoke_test(slot.action_id).result.ok
        record = host.services.actions.load(slot.action_id)
        assert [entry.to_state for entry in record.transitions] == [ActionState.IN_FLIGHT, ActionState.SUCCEEDED]
    assert observations and len(set(observations)) == 1 and len(boundary.posts) == 1


def test_primary_address_changed_after_private_review_fails_before_post(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    operator = FakeOperator("CHALLENGE")
    operator.on_input = lambda: setattr(boundary, "profile", Reply({"emailAddress": "different@sample.invalid"}))
    with harness.host(GmailHostMode.LIVE_SEND, operator=operator) as host:
        invocation = host.send_smoke_test(slot.action_id)
        assert not invocation.result.ok
        status = host.action_status()
        assert status.state == "FAILED" and status.last_attempt == "NOT_INVOKED"
    assert not boundary.posts
    count = len(boundary.requests)
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"))
    assert code == 2 and len(boundary.requests) == count


def test_current_reconciliation_denial_does_not_spend_probe_quota(harness, boundary):
    slot, _, _ = unknown_send(harness, boundary)
    count = len(boundary.requests)
    with harness.host(GmailHostMode.RECONCILE, permissions=PermissionConfig(
        level=PermissionLevel.CONFIRM, blocked_capabilities=(str(Capability.NET_READ),),
    )) as host:
        result = host.reconcile(slot.action_id)
        assert result.status.value == "probe_denied"
        assert host.action_status().probe_count == 0
        assert host.action_status().state == "RECONCILE_REQUIRED"
    assert len(boundary.requests) == count and len(boundary.posts) == 1


def test_foreign_reconnected_account_binding_cannot_probe_old_action(harness, boundary):
    slot, _, _ = unknown_send(harness, boundary)
    count = len(boundary.requests)
    with harness.host(GmailHostMode.RECONCILE) as host:
        # A trusted reconnect outside the narrow CLI still cannot adopt old
        # intent. Same signed account, new exact connection.
        old_connection = host.services.google.restore().credential.connection_id
        new = host.services.google.connect(profile=GoogleAccessProfile.GMAIL_SEND)
        assert old_connection != new.connection_id
        with pytest.raises(GmailHostError, match=HostFailure.CONNECTION.value):
            host.reconcile(slot.action_id)
        assert host.action_status().state == "RECONCILE_REQUIRED"
        assert host.action_status().probe_count == 0
    assert len(boundary.requests) == count


@pytest.mark.parametrize("mode", [GmailHostMode.LIVE_SEND, GmailHostMode.RECONCILE])
def test_reconnected_restart_rejects_old_action_before_any_credential_io(harness, boundary, mode):
    slot, _, _ = (unknown_send(harness, boundary) if mode is GmailHostMode.RECONCILE
                  else prepare(harness, boundary))
    with harness.services() as services:
        old = services.google.restore().credential.connection_id
        replacement = services.google.connect(profile=GoogleAccessProfile.GMAIL_SEND)
        assert replacement.connection_id != old
    count = len(boundary.requests)
    with harness.host(mode, operator=FakeOperator("CHALLENGE")) as host:
        assert host.services.google.restore().state.value == "refresh_required"
        failure = None
        try:
            if mode is GmailHostMode.LIVE_SEND:
                host.send_smoke_test(slot.action_id)
            else:
                host.reconcile(slot.action_id)
        except GmailHostError as error:
            failure = error.reason
        # Even OAuth refresh is too late: this action does not own the new
        # connection. No replacement-account profile or Sent query may occur.
        assert len(harness.last_google.transport.refreshes) == 0
        assert failure is HostFailure.CONNECTION
        assert not host.operator.private
        assert host.action_status().state == (
            "RECONCILE_REQUIRED" if mode is GmailHostMode.RECONCILE else "PREPARED"
        )
    assert len(boundary.requests) == count


def test_restored_preparation_keeps_bytes_and_refreshes_only_same_connection(harness, boundary):
    slot, review, _ = prepare(harness, boundary)
    with harness.host() as host:
        before = host.services.google.restore()
        assert before.state.value == "refresh_required"
        assert host.foundation.review(slot.preparation) == review
        assert not harness.last_google.transport.refreshes
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        assert host.send_smoke_test(slot.action_id).result.ok
        assert host.info.connection_id == before.credential.connection_id
        assert host.info.generation > before.credential.generation
        assert host.foundation.review(slot.preparation) == review
    assert review["message_id"].encode() in boundary.mime and len(boundary.posts) == 1


def test_missing_journal_after_consumed_approval_cannot_recreate_send(harness, boundary):
    slot, _, _ = unknown_send(harness, boundary)
    (harness.directory / "actions" / (slot.action_id + ".json")).unlink()
    count = len(boundary.requests)
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"))
    assert code == 2 and "host_storage_unavailable" in output and "DO NOT SEND AGAIN" in output
    assert len(boundary.requests) == count and len(boundary.posts) == 1


@pytest.mark.parametrize("cut", ["save_failure", "process_crash", "process_temp"])
def test_orphaned_preparation_blocks_reopen_and_any_second_proposal(harness, boundary, cut):
    if cut == "save_failure":
        with harness.host(GmailHostMode.PREPARE, operator=FakeOperator(SUBJECT, BODY)) as host:
            def fail_save(slot):
                raise OSError("synthetic host publication failure")
            host.services.storage.save_once = fail_save
            with pytest.raises(OSError):
                host.prepare_smoke_test()
    else:
        worker = Path(__file__).with_name("gmail_host_crash_worker.py")
        crashed = subprocess.run([sys.executable, str(worker), str(harness.directory),
                                  "act_123456789abc", "preparation_temp" if cut == "process_temp" else "preparation"],
                                 capture_output=True, text=True, timeout=30)
        assert crashed.returncode == 71, crashed.stderr
    state = json.loads((harness.directory / "gmail-host.json").read_text())
    assert state["slot"] is None

    def private_files():
        directory = harness.directory / "payloads"
        return set(directory.glob("*.json")) | set(directory.glob("*.tmp"))

    payloads = private_files()
    assert len(payloads) == 1
    assert not list((harness.directory / "actions").glob("*.json"))
    count = len(boundary.requests)
    code, output = harness.cli(["prepare-smoke-test"], operator=FakeOperator(SUBJECT, BODY))
    assert code == 2 and "host_storage_unavailable" in output
    assert private_files() == payloads
    assert len(boundary.requests) == count and not boundary.posts


@pytest.mark.parametrize("cut", ["preflight", "post", "publication"])
def test_actual_process_crash_reopens_same_action_without_resend(harness, boundary, cut):
    slot, review, _ = prepare(harness, boundary)
    worker = Path(__file__).with_name("gmail_host_crash_worker.py")
    crashed = subprocess.run([sys.executable, str(worker), str(harness.directory), slot.action_id, cut],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 71, crashed.stderr
    stored = json.loads((harness.directory / "actions" / (slot.action_id + ".json")).read_text())
    assert stored["state"] == "in_flight"
    count = len(boundary.requests)
    code, output = harness.cli(["action-status", slot.action_id])
    assert code == 0 and "RECONCILE_REQUIRED" in output and "DO NOT SEND AGAIN" in output
    assert len(boundary.requests) == count
    with harness.host(GmailHostMode.RECONCILE) as host:
        assert host.foundation.review(slot.preparation) == review
        payload = host.services.payloads.load(
            slot.preparation.reference, action_id=slot.preparation.storage_action_id,
            expected_intent_digest=slot.preparation.intent_digest,
        ).payload
        import base64
        boundary.template_mime = base64.b64decode(payload["mime_base64"])
        if cut == "preflight":
            boundary.search = Reply({"messages": [], "resultSizeEstimate": 0})
        result = host.reconcile(slot.action_id)
        assert result.action_state is (ActionState.RECONCILE_REQUIRED if cut == "preflight" else ActionState.SUCCEEDED)
    assert not boundary.posts


def test_authorized_but_never_attempted_restart_needs_fresh_confirmation(harness, boundary):
    slot, review, _ = prepare(harness, boundary)
    worker = Path(__file__).with_name("gmail_host_crash_worker.py")
    crashed = subprocess.run([sys.executable, str(worker), str(harness.directory), slot.action_id, "authorized"],
                             capture_output=True, text=True, timeout=30)
    assert crashed.returncode == 71, crashed.stderr
    with harness.host() as host:
        assert host.action_status().state == "AUTHORIZED" and host.action_status().attempts == 0
        assert host.foundation.review(slot.preparation) == review
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("no"))
    assert code == 2 and not boundary.posts
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        assert host.send_smoke_test(slot.action_id).result.ok
        assert host.services.actions.list_actions() == [slot.action_id]
        assert host.foundation.review(slot.preparation) == review
    assert len(boundary.posts) == 1


@pytest.mark.skipif(os.name != "nt", reason="production host uses Windows current-user DPAPI")
def test_actual_local_factory_cli_uses_dpapi_and_real_stores_with_fake_internet(tmp_path, boundary, monkeypatch):
    from agentcore.credentials import GoogleConnectionService
    from agentcore.gmail_host import local
    from agentcore.util.clock import SystemClock
    from gmail_send_fakes import SCOPES
    from google_oauth_fakes import FakeBrowser, FakeTransport, FakeVerifier, FakeCallback

    # Only OS configuration location and OAuth/provider boundaries differ.
    # Production file/DPAPI storage, services, parser, gate, executor all run.
    monkeypatch.setattr(local, "default_host_directory", lambda: tmp_path)
    monkeypatch.setattr(cli, "default_host_directory", lambda: tmp_path)
    output = io.StringIO()
    assert cli.main(["init"], output=output) == 0
    (tmp_path / "oauth-client.json").write_text(
        json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}), encoding="utf-8")

    def google(vault, config):
        browser, transport = FakeBrowser(), FakeTransport()
        transport.initial["scope"] = transport.refreshed["scope"] = SCOPES
        return GoogleConnectionService(vault, config, transport=transport,
                                       identity_verifier=FakeVerifier(browser, SystemClock()),
                                       browser=browser, callback_factory=FakeCallback)

    monkeypatch.setattr(local, "GoogleConnectionService", google)
    assert cli.main(["connect"], operator=FakeOperator(), services_factory=local.open_local_services, output=output) == 0
    assert cli.main(["prepare-smoke-test"], operator=FakeOperator(SUBJECT, BODY),
                    services_factory=local.open_local_services, output=output) == 0
    with local.open_local_services() as services:
        slot = services.storage.load_slot()
        assert services.google.restore().state is not None
    assert cli.main(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"),
                    services_factory=local.open_local_services, output=output) == 0
    assert len(boundary.posts) == 1 and "SUCCEEDED" in output.getvalue()
    assert all(value not in output.getvalue() for value in (SUBJECT, BODY, ACCESS, REFRESH, CLIENT_SECRET))
    protected = (tmp_path / "google" / "connection.dpapi").read_bytes()
    assert b"TEST-ONLY" not in protected and REFRESH.encode() not in protected


def test_inherited_or_other_thread_host_authority_fails_before_io(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    failures = []
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        def attempt():
            try:
                host.send_smoke_test(slot.action_id)
            except GmailHostError as error:
                failures.append(error.reason)
        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive() and failures == [HostFailure.OWNERSHIP]
    assert not boundary.posts


@pytest.mark.skipif(not hasattr(os, "fork"), reason="real inherited authority requires POSIX fork")
def test_forked_child_cannot_reuse_armed_live_authority(harness, boundary):
    slot, _, _ = prepare(harness, boundary)
    with harness.host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        host._activate(verify_primary=True)
        request = pending_request(host, slot)
        with host.gate.arm(request.request_id, slot):
            pid = os.fork()
            if pid == 0:
                try:
                    host.gate.decide(prompt_for(request))
                except GmailHostError as error:
                    os._exit(0 if error.reason is HostFailure.OWNERSHIP else 2)
                except BaseException:
                    os._exit(3)
                os._exit(4)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
    assert not boundary.posts


@pytest.mark.parametrize("damage", ["mac", "extra_permission", "missing", "key", "payload"])
def test_corrupt_saved_continuity_never_enables_or_recreates_send(harness, boundary, damage):
    slot, _, _ = prepare(harness, boundary)
    path = harness.directory / "gmail-host.json"
    if damage == "missing":
        path.unlink()
    elif damage == "key":
        (harness.directory / "installation-key.json").unlink()
    elif damage == "payload":
        (harness.directory / "payloads" / (slot.preparation.reference + ".json")).unlink()
    else:
        value = json.loads(path.read_text())
        if damage == "mac":
            value["slot"]["action_id"] = "act_123456789abc"
        else:
            value["allow_email_send"] = True
        path.write_text(json.dumps(value), encoding="utf-8")
    count = len(boundary.requests)
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"))
    assert code == 2 and len(boundary.requests) == count and not boundary.posts


@pytest.mark.parametrize("extra", ["token", "access_token", "refresh_token", "scopes", "recipient",
                                  "live_send", "account", "url", "payload_path"])
def test_host_client_configuration_cannot_supply_authority_or_tokens(tmp_path, extra):
    with GmailHostStorage(tmp_path, initialize=True) as storage:
        (tmp_path / "oauth-client.json").write_text(json.dumps({
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, extra: ACCESS,
        }), encoding="utf-8")
        with pytest.raises(GmailHostError):
            storage.oauth_config()


def test_host_configuration_missing_or_corrupt_fails_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    with GmailHostStorage(tmp_path, initialize=True) as storage:
        with pytest.raises(GmailHostError, match=HostFailure.CONFIGURATION.value):
            storage.oauth_config()
        (tmp_path / "oauth-client.json").write_text(
            '{"client_id":"' + CLIENT_ID + '","client_id":"' + CLIENT_ID + '"}', encoding="utf-8")
        with pytest.raises(CredentialError):
            storage.oauth_config()


def test_private_host_config_is_runtime_only_and_not_echoed(tmp_path):
    with GmailHostStorage(tmp_path, initialize=True) as storage:
        (tmp_path / "oauth-client.json").write_text(
            json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}), encoding="utf-8")
        config = storage.oauth_config()
        assert config.client_id == CLIENT_ID and CLIENT_SECRET not in repr(config)
        with pytest.raises(TypeError):
            pickle.dumps(config)
        assert CLIENT_SECRET not in (tmp_path / "gmail-host.json").read_text()


@pytest.mark.parametrize("name", ["../escape", "C:\\escape", ".credential-owner", "con", "file:stream", "arbitrary.json"])
def test_pinned_file_reuse_accepts_only_the_closed_record_names(tmp_path, name):
    with pytest.raises(CredentialError):
        ProtectedFile(tmp_path, record_name=name)


def test_host_storage_excludes_another_process_owner(tmp_path):
    with GmailHostStorage(tmp_path, initialize=True):
        with pytest.raises(Exception):
            GmailHostStorage(tmp_path)


def test_cli_provider_exceptions_never_print_traceback_or_private_material(harness, boundary, capsys):
    slot, _, _ = prepare(harness, boundary)
    boundary.send = Reply(error=SystemExit(ACCESS + REFRESH + SUBJECT + BODY))
    code, output = harness.cli(["send-smoke-test", slot.action_id, "--live-send"], operator=FakeOperator("CHALLENGE"))
    captured = capsys.readouterr()
    assert code == 2 and "DO NOT SEND AGAIN" in output
    assert all(value not in output + captured.out + captured.err for value in (ACCESS, REFRESH, SUBJECT, BODY, "Traceback"))
    assert len(boundary.posts) == 1


def test_module_entrypoint_suppresses_private_exception_chain_when_output_breaks():
    script = '''
import runpy
from contextlib import contextmanager
from agentcore.gmail_host import cli
from gmail_mutation_http_fakes import HttpBoundary

HttpBoundary().install()

class BrokenOutput:
    def write(self, text):
        raise OSError("synthetic-private-output-failure")

@contextmanager
def unavailable_services():
    raise RuntimeError("synthetic-private-provider-material")
    yield

original_main = cli.main
cli.main = lambda: original_main(["status"], services_factory=unavailable_services, output=BrokenOutput())
runpy.run_module("agentcore.gmail_host", run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert result.stdout == result.stderr == ""
