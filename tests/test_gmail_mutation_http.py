"""Production HTTP and executor truth through deterministic, non-network sockets."""

import base64
import copy
import http.client
import json
import os
import pickle
import socket
import ssl
import urllib.request

import pytest

from agentcore.action_journal import ActionAttemptStatus, ActionState, EffectVerdict
from agentcore.approvals import HumanApprovalDecision
from agentcore.credentials import CredentialError, CredentialFailure, GoogleAccessProfile
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES
from agentcore.integrations.gmail.mutation_http import (
    MAX_HTTP_HEADER_BYTES, MAX_HTTP_HEADER_LINE, MAX_SEND_RESPONSE_BYTES, GmailMutationHttp,
)
from agentcore.integrations.gmail.send import GmailSendPreparation
from agentcore.integrations.gmail.send_intent import make_send_payload
from agentcore.permissions.levels import Capability
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from gmail_mutation_http_fakes import (
    LIVE_ARGS, PROFILE_PATH, SEND_PATH, LiveHarness, Reply,
    boundary, live, no_network,
)
from gmail_send_fakes import ACCESS, ARGS, REFRESH, SENDER, DecisionGate, Harness, send_policy
from google_oauth_fakes import SUBJECT


def public(h, result=None):
    return h.ordinary_json() + json.dumps(h.logger.events) + (json.dumps(result.to_dict()) if result else "")


@pytest.mark.parametrize("cc", [[], ["copy@sample.invalid"]], ids=["to-only", "to-cc"])
def test_actual_http_bytes_and_success_observe_the_exact_durable_effect_seam(live, boundary, monkeypatch, cc):
    h = live
    args = {**LIVE_ARGS, "cc": cc, "subject": "Subject café", "body": "Body café\nsecond line"}
    handle = h.prepare(args)
    payload = h.foundation._load_preparation(handle.preparation_id)[0]
    assert not boundary.requests
    permits = []
    original_run = h.foundation.tool.run

    def run(arguments, context):
        permits.append(context.credential_execution)
        return original_run(arguments, context)

    def at_http(request):
        record = h.actions.load(h.actions.list_actions()[0])
        assert record.state is ActionState.IN_FLIGHT
        assert len(record.attempts) == 1 and record.authority.approval_request_id
        assert record.transitions[0].from_state is ActionState.AUTHORIZED
        assert permits[0].provider_entered is (request.method == "POST")
        assert record.attempts[0].attempt_id == permits[0]._identity.attempt_id
        assert h.google.vault._data.identity.subject == SUBJECT

    monkeypatch.setattr(h.foundation.tool, "run", run)
    boundary.on_response = at_http
    result = h.invoke(args)
    assert result.ok, result.to_dict()
    assert [request.method for request in boundary.requests] == ["GET", "POST"]
    assert boundary.requests[0].path == PROFILE_PATH
    post = boundary.posts[0]
    assert post.path == SEND_PATH and post.body.startswith(b'{"raw":"') and post.body.endswith(b'"}')
    assert post.headers == {
        "host": "gmail.googleapis.com", "authorization": "Bearer " + ACCESS,
        "accept": "application/json", "accept-encoding": "identity",
        "content-type": "application/json", "content-length": str(len(post.body)),
    }
    raw = json.loads(post.body)
    assert set(raw) == {"raw"} and "=" not in raw["raw"]
    decoded = base64.urlsafe_b64decode(raw["raw"] + "=" * (-len(raw["raw"]) % 4))
    assert decoded == base64.b64decode(payload["mime_base64"]) == boundary.mime
    for request in boundary.requests:
        assert request.bytes.count(ACCESS.encode()) == 1
        assert ACCESS.encode() not in request.bytes.replace(b"Authorization: Bearer " + ACCESS.encode() + b"\r\n", b"")
        assert REFRESH.encode() not in request.bytes and request.closed
    record = h.actions.load(result.action_id)
    assert record.state is ActionState.SUCCEEDED and record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert record.schema_version == 1 and record.idempotency is None
    evidence = h.evidence.load_attempt_evidence(record.evidence_refs[0], action_id=record.action_id,
        attempt_id=result.attempt_id, intent_digest=record.intent_digest).evidence
    assert evidence["message_id"] == "syntheticmessage1" and "raw" not in evidence
    ordinary = public(h, result)
    for secret in (ACCESS, REFRESH, SENDER, payload["message_id"], args["subject"], "syntheticmessage1", *args["to"], *cc):
        assert secret not in ordinary
    assert not h.fake.sends and not h.fake.probes  # no whole-provider fake executed


def test_live_code_grants_nothing_to_default_composition(tmp_path, boundary):
    assert "gmail_send_message" not in {tool.name for tool in default_tools()}
    h = Harness(tmp_path, production=True)
    try:
        call = h.invoke()
        record = h.actions.load(call.action_id)
        assert record.state is ActionState.FAILED
        assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
        assert not boundary.requests
    finally:
        h.close()


def test_bcc_is_blocked_before_approval_and_cannot_enter_via_restored_foundation_intent(live, boundary):
    h = live
    with pytest.raises(ValueError, match="Bcc live delivery disabled pending controlled Gmail validation"):
        h.prepare(ARGS)
    assert not h.gate.prompts and not h.actions.list_actions() and not h.approvals.list_requests()
    # An inert older foundation Bcc payload can be restored, but not admitted.
    payload = make_send_payload(ARGS, h.info, sender=SENDER, now=h.clock.now())
    stored = h.payloads.create("act_eeeeeeeeeeee", payload)
    handle = GmailSendPreparation(payload["preparation_id"], stored.action_id, stored.reference, stored.intent_digest)
    h.foundation.restore(handle)
    call = h.invoke(ARGS, preparation=handle)
    assert not call.ok and call.result.metadata["kind"] == "validation"
    assert not boundary.requests and not h.gate.prompts and not h.actions.list_actions()
    assert base64.b64decode(payload["mime_base64"]).find(b"Bcc: blind@sample.invalid") >= 0


@pytest.mark.parametrize("key", ["sender", "account", "connection", "scopes", "userId", "url", "path", "method",
                                  "raw", "headers", "transport", "allow_email_send", "live_bcc"])
def test_model_cannot_select_sender_transport_or_authority(live, boundary, key):
    handle = live.prepare()
    result = live.invoke({**LIVE_ARGS, key: "forged-private-value"}, preparation=handle)
    assert not result.ok and result.result.metadata["kind"] == "validation"
    assert not boundary.requests and not live.gate.prompts
    assert "forged-private-value" not in public(live, result)


@pytest.mark.parametrize("blocked", [Capability.EMAIL_SEND, Capability.CREDENTIAL_ACCESS, Capability.NET_WRITE, Capability.NET_READ])
def test_current_denial_prevents_profile_and_post(live, boundary, blocked):
    live.executor.policy = send_policy(blocked=(str(blocked),))
    result = live.invoke()
    assert not result.ok and not boundary.requests
    if result.action_id and live.actions.list_actions():
        record = live.actions.load(result.action_id)
        assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


@pytest.mark.parametrize("missing", ["no_journal", "no_approval"])
def test_missing_current_host_services_and_direct_run_cannot_post(tmp_path, boundary, missing):
    h = LiveHarness(tmp_path, **{missing: True})
    try:
        with pytest.raises(CredentialError) as error:
            h.foundation.tool.run(LIVE_ARGS, ToolContext())
        assert error.value.reason is CredentialFailure.AUTHORITY
        assert not h.invoke().ok and not boundary.requests
    finally:
        h.close()


def test_invalid_and_consumed_approval_do_not_reach_http(live, boundary):
    h = live
    h.gate.decision = HumanApprovalDecision.PENDING
    pending = h.invoke()
    assert pending.approval_request_id and not boundary.requests
    assert not h.invoke(approval="apr_eeeeeeeeeeee").ok and not boundary.requests
    h.authority.record_decision(pending.approval_request_id, HumanApprovalDecision.APPROVE)
    assert h.invoke(approval=pending.approval_request_id).ok
    h.prepare()
    assert not h.invoke(approval=pending.approval_request_id).ok
    assert len(boundary.posts) == 1


@pytest.mark.parametrize("change", ["readonly", "same-sub-reconnect", "disconnect", "stale", "expired"])
def test_current_exact_profile_connection_and_expiry_precede_any_http(live, boundary, change):
    h = live
    h.prepare()
    if change == "readonly":
        h.google.transport.initial["scope"] = " ".join(sorted(GOOGLE_GMAIL_READONLY_SCOPES))
        other = h.google.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
        with pytest.raises(CredentialError):
            h.foundation.bind(h.runtime, other.reference, replace_existing=True)
    elif change == "same-sub-reconnect":
        other = h.google.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
        assert other.account_id == h.info.account_id and other.connection_id != h.info.connection_id
        with pytest.raises(CredentialError):
            h.foundation.bind(h.runtime, other.reference, replace_existing=True)
    elif change == "disconnect":
        h.google.service.disconnect(h.info.reference)
    elif change == "stale":
        h.google.service.refresh(h.info.reference)
    else:
        h.clock.advance(7200)
    result = h.invoke()
    assert not result.ok and not boundary.requests
    assert h.actions.load(result.action_id).attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


@pytest.mark.parametrize("profile", [{}, {"emailAddress": None}, {"emailAddress": "bad"},
                                    {"emailAddress": "other@sample.invalid"}, {"emailAddress": "Sender@sample.invalid"},
                                    {"emailAddress": SENDER, "historyId": "123"}, {"emailAddress": ACCESS}])
def test_profile_failure_proves_no_send_and_preserves_signed_identity(live, boundary, profile):
    h = live
    signed = h.google.vault._data.identity
    h.prepare()
    before = h.foundation.review(h.preparation)
    boundary.profile = Reply(profile)
    result = h.invoke()
    record = h.actions.load(result.action_id)
    assert record.state is ActionState.FAILED and record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED
    assert not boundary.posts and len(boundary.requests) == 1
    assert h.google.vault._data.identity == signed
    assert h.foundation.review(h.preparation) == before
    assert ACCESS not in public(h, result)


def test_primary_address_normalization_preserves_local_spelling_and_only_folds_domain(live, boundary):
    boundary.profile = Reply({"emailAddress": " sender@SAMPLE.INVALID "})
    assert live.invoke().ok
    assert live.foundation.review(live.preparation)["sender"] == SENDER


@pytest.mark.parametrize("stage", ["profile", "send"])
@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504])
def test_status_never_retries_follows_redirects_reads_error_body_or_invents_send_truth(live, boundary, stage, status):
    setattr(boundary, stage, Reply(status=status, raw=(ACCESS + REFRESH).encode(), headers=[
        ("Location", "https://attacker.invalid/" + ACCESS), ("Retry-After", "0"), ("Content-Type", "text/html"),
    ]))
    result = live.invoke()
    record = live.actions.load(result.action_id)
    assert record.state is (ActionState.FAILED if stage == "profile" else ActionState.RECONCILE_REQUIRED)
    assert len(boundary.posts) == (0 if stage == "profile" else 1)
    assert boundary.requests[-1].stream.body_reads == 0
    count = len(boundary.requests)
    assert not live.invoke(action_id=result.action_id).ok
    assert len(boundary.requests) == count
    assert ACCESS not in public(live, result) and REFRESH not in public(live, result)


BAD_RESPONSES = [
    Reply(raw=b'{"id":'), Reply(raw=b'{}\xff'), Reply(raw=b'{"id":1,"id":2}'), Reply(raw=b'{"x":NaN}'),
    Reply(raw=b'{"x":1.0}'), Reply(raw=b'{"x":"\\ud800"}'), Reply(payload=[]),
    Reply(payload={}, headers=[("Content-Type", "text/html")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("Content-Encoding", "gzip")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("Content-Length", "-1")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("Content-Length", "99")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("Content-Length", "2"), ("Content-Length", "2")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("Content-Type", "application/json")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("Content-Length", "2"), ("Transfer-Encoding", "chunked")]),
    Reply(raw=b"x" * (MAX_SEND_RESPONSE_BYTES + 1)),
    Reply(raw=b"x" * (MAX_SEND_RESPONSE_BYTES + 1), headers=[("Content-Type", "application/json")]),
    Reply(payload={}, headers=[("Content-Type", "application/json"), ("X-Synthetic", "x" * MAX_HTTP_HEADER_LINE)]),
    Reply(payload={}, headers=[("Content-Type", "application/json")] + [("X-Synthetic", "x" * 1000)] * 17),
    Reply(payload={}, headers=[("Content-Type", "application/json")] + [("X-Synthetic", "x")] * 70),
    Reply(raw=b'{"x":' + b'[' * 33 + b'0' + b']' * 33 + b'}'),
]


@pytest.mark.parametrize("stage", ["profile", "send"])
@pytest.mark.parametrize("reply", BAD_RESPONSES, ids=["json", "utf8", "duplicate-json", "nan", "float", "surrogate", "shape",
    "content-type", "encoding", "negative-length", "short-body", "duplicate-length", "duplicate-type", "conflicting-framing",
    "large-length", "large-body", "header-line", "header-total", "header-count", "json-depth"])
def test_strict_bounded_production_response_parser_preserves_seam_truth(live, boundary, stage, reply):
    setattr(boundary, stage, reply)
    result = live.invoke()
    record = live.actions.load(result.action_id)
    assert record.state is (ActionState.FAILED if stage == "profile" else ActionState.RECONCILE_REQUIRED)
    assert len(boundary.posts) == (0 if stage == "profile" else 1)
    assert all(request.closed for request in boundary.requests)


@pytest.mark.parametrize("stage", ["profile", "send"])
@pytest.mark.parametrize("error_type", [TimeoutError, ConnectionResetError, CancellationRequested, KeyboardInterrupt, SystemExit])
def test_transport_interruptions_have_no_text_or_hidden_retry(live, boundary, stage, error_type):
    error = error_type(ACCESS + REFRESH)
    error.add_note(ACCESS)
    setattr(boundary, stage, Reply(error=error))
    if error_type in {KeyboardInterrupt, SystemExit}:
        with pytest.raises(error_type) as captured:
            live.invoke()
        assert str(captured.value) == "" and captured.value.args == ()
    else:
        assert not live.invoke().ok
    record = live.actions.load(live.actions.list_actions()[0])
    assert record.state is (ActionState.FAILED if stage == "profile" else ActionState.RECONCILE_REQUIRED)
    assert len(boundary.posts) == (0 if stage == "profile" else 1)
    assert ACCESS not in public(live) and REFRESH not in public(live)


def test_reset_during_post_write_is_ambiguous_and_never_replayed(live, boundary):
    def reset(request):
        if request.method == "POST" and request.body:
            raise ConnectionResetError(ACCESS)
    boundary.on_write = reset
    result = live.invoke()
    assert result.action_state == ActionState.RECONCILE_REQUIRED.value
    assert len(boundary.posts) == 1 and len(boundary.requests) == 2


@pytest.mark.parametrize("value", [ACCESS, base64.b64encode(ACCESS.encode()).decode(), "prefix-" + ACCESS])
def test_provider_token_echo_is_withheld_before_evidence(live, boundary, value):
    boundary.send = Reply({"id": value, "threadId": "thread"})
    result = live.invoke()
    record = live.actions.load(result.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and not record.evidence_refs
    assert ACCESS not in public(live, result) and value not in public(live, result)


def test_tls_proxy_keylog_and_debug_settings_cannot_expose_tokens(live, boundary, monkeypatch, tmp_path, capsys):
    keylog = tmp_path / "must-not-exist.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://attacker.invalid:9")
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 9)
    monkeypatch.setattr(http.client.HTTPResponse, "debuglevel", 9, raising=False)
    def forbidden_discovery(*args, **kwargs):
        raise AssertionError("No environment proxy or keylog-aware context discovery")
    monkeypatch.setattr(urllib.request, "getproxies", forbidden_discovery)
    monkeypatch.setattr(ssl, "create_default_context", forbidden_discovery)
    assert live.invoke().ok
    for request in boundary.requests:
        context = request.connection._context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert context.keylog_filename is None and context.get_ca_certs()
        assert context.verify_flags & ssl.VERIFY_X509_STRICT
        assert request.connection.debuglevel == 0
    assert os.environ["SSLKEYLOGFILE"] == str(keylog) and not keylog.exists()
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    assert ACCESS not in public(live)


def test_verification_is_one_use_and_bound_to_transport_lease_and_exact_intent(live, boundary, monkeypatch):
    h = live
    # The binding captured its method at bind time, so instrument the actual
    # HTTP method to observe the issued object without changing that binding.
    transport = h.foundation._provider._transport
    verify = transport.verify_sender
    issued = []
    def capture(submission, credential):
        proof = verify(submission, credential)
        for serialize in (pickle.dumps, copy.copy, copy.deepcopy):
            with pytest.raises(TypeError):
                serialize(proof)
        with pytest.raises(CredentialError):
            proof.consume(GmailMutationHttp(), submission, credential)
        issued.append((proof, submission, credential))
        return proof
    monkeypatch.setattr(transport, "verify_sender", capture)
    assert h.invoke().ok
    proof, submission, credential = issued[0]
    with pytest.raises(CredentialError):
        proof.consume(transport, submission, credential)
    with pytest.raises(CredentialError):
        transport.send(submission, credential)
    assert len(boundary.posts) == 1


@pytest.mark.parametrize("case", ["valid", "oversize_body", "long_trailer", "interim_headers", "truncated_chunk",
                                 "long_chunk_line", "many_chunks"])
def test_chunked_framing_and_interim_headers_are_bounded_without_hidden_post_retries(live, boundary, case):
    body = b'{"id":"syntheticmessage1","threadId":"syntheticthread1"}'
    if case == "oversize_body":
        body = b" " * (MAX_SEND_RESPONSE_BYTES + 1)
    trailer = b"X-Safe: inert\r\n"
    if case == "long_trailer":
        trailer = b"X-Long: " + b"x" * MAX_HTTP_HEADER_LINE + b"\r\n"
    chunks = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n" + trailer + b"\r\n"
    if case == "truncated_chunk":
        chunks = b"ff\r\nshort"
    elif case == "long_chunk_line":
        chunks = b"0" * MAX_HTTP_HEADER_LINE + f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
    elif case == "many_chunks":
        chunks = b"".join(b"1\r\n" + bytes([byte]) + b"\r\n" for byte in b" " * 100 + body) + b"0\r\n\r\n"
    prefix = b"HTTP/1.1 100 Continue\r\nX-Safe: interim\r\n\r\n" * (24 if case == "interim_headers" else 1)
    boundary.send = Reply(wire=prefix + b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks)
    result = live.invoke()
    assert result.action_state == (ActionState.SUCCEEDED.value if case == "valid" else ActionState.RECONCILE_REQUIRED.value)
    assert len(boundary.posts) == 1 and all(request.closed for request in boundary.requests)


def test_test_guard_blocks_real_dns_and_socket_egress_even_without_a_transport_fake():
    with pytest.raises(AssertionError, match="cannot use DNS or real sockets"):
        socket.getaddrinfo("gmail.googleapis.com", 443)
    with socket.socket() as connection:
        for operation in (
            lambda: connection.connect(("192.0.2.1", 443)),
            lambda: connection.sendall(b"synthetic bytes"),
        ):
            with pytest.raises(AssertionError, match="cannot use DNS or real sockets"):
                operation()


def test_negative_chunk_length_never_reaches_an_unbounded_stream_read(live, boundary):
    boundary.send = Reply(wire=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                               b"-1\r\nprivate provider bytes\r\n0\r\n\r\n")
    result = live.invoke()
    assert result.action_state == ActionState.RECONCILE_REQUIRED.value
    assert len(boundary.posts) == 1
    assert all(0 <= size <= MAX_SEND_RESPONSE_BYTES + 1 for size in boundary.posts[0].stream.read_sizes)
