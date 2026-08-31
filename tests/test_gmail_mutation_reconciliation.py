"""Production Sent probes with real HTTP framing and no DNS/network access."""

import base64
import json
import subprocess
import sys
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit

import pytest

from agentcore.action_journal import (
    ActionReconciliationStatus, ActionState, EffectVerdict, ReconciliationDisposition,
)
from agentcore.credentials import GoogleAccessProfile
from agentcore.integrations.gmail.send import GmailSendPreparation
from agentcore.permissions.levels import Capability
from gmail_mutation_http_fakes import (
    LIVE_ARGS, LiveHarness, Reply, boundary, full_message, live, no_network,
)
from gmail_send_fakes import ACCESS, REFRESH, SENDER, send_policy


@pytest.fixture
def uncertain(live, boundary):
    boundary.send = Reply(error=TimeoutError(ACCESS))
    live.first = live.invoke()
    assert live.first.action_state == ActionState.RECONCILE_REQUIRED.value
    boundary.template_mime = boundary.mime
    boundary.requests.clear()
    return live


def set_header(message, name, value):
    headers = message["payload"]["headers"]
    headers[:] = [row for row in headers if row["name"].lower() != name.lower()]
    if value is not None:
        headers.append({"name": name, "value": value})


def set_body(message, value):
    message["payload"]["body"] = {
        "size": len(value), "data": base64.urlsafe_b64encode(value).decode("ascii").rstrip("="),
    }


def test_exact_full_candidate_settles_with_fixed_gets_and_only_protected_minimal_evidence(uncertain, boundary):
    h = uncertain
    review = h.foundation.review(h.preparation)
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert len(boundary.requests) == 2 and not boundary.posts
    search, candidate = boundary.requests
    assert urlsplit(search.path).path == "/gmail/v1/users/me/messages"
    assert parse_qs(urlsplit(search.path).query) == {
        "labelIds": ["SENT"], "q": ["rfc822msgid:" + review["message_id"][1:-1]],
        "maxResults": ["5"], "fields": ["messages(id,threadId),nextPageToken,resultSizeEstimate"],
    }
    assert urlsplit(candidate.path).path == "/gmail/v1/users/me/messages/syntheticmessage1"
    assert parse_qs(urlsplit(candidate.path).query) == {
        "format": ["full"],
        "fields": ["id,threadId,labelIds,payload(mimeType,filename,headers,body(size,data,attachmentId),parts(partId))"],
    }
    for request in boundary.requests:
        assert request.method == "GET" and request.body == b"" and request.closed
        assert request.headers == {
            "host": "gmail.googleapis.com", "authorization": "Bearer " + ACCESS,
            "accept": "application/json", "accept-encoding": "identity",
        }
        assert ACCESS not in request.path and REFRESH.encode() not in request.bytes
        assert "/attachments/" not in request.path and "format=raw" not in request.path
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.SUCCEEDED and len(record.attempts) == 1
    assert record.terminal_outcome.verdict is EffectVerdict.CONFIRMED_APPLIED
    assert record.idempotency is None
    evidence = h.evidence.load_reconciliation_evidence(record.evidence_refs[0], action_id=record.action_id,
        claim_epoch=report.claim_epoch, intent_digest=record.intent_digest).evidence
    assert evidence["reason"] == "single_match" and evidence["candidates"] == [
        {"message_id": "syntheticmessage1", "thread_id": "syntheticthread1", "strong_match": True},
    ]
    ordinary = h.ordinary_json() + json.dumps(asdict(report)) + json.dumps(h.logger.events)
    for private in (ACCESS, REFRESH, SENDER, LIVE_ARGS["subject"], "Private send body", review["message_id"],
                    "syntheticmessage1", *LIVE_ARGS["to"], *LIVE_ARGS["cc"]):
        assert private not in ordinary
    for private in (ACCESS, REFRESH, LIVE_ARGS["subject"], "Private send body", *LIVE_ARGS["to"]):
        assert private not in json.dumps(evidence)
    assert h.invoke(action_id=record.action_id).ok and len(boundary.requests) == 2
    assert not h.fake.sends and not h.fake.probes


@pytest.mark.parametrize("case", ["zero", "multiple", "five", "next_page", "empty_page_token", "estimate"])
def test_absence_multiplicity_and_incomplete_search_never_authorize_resend(uncertain, boundary, case):
    h = uncertain
    count = {"zero": 0, "multiple": 2, "five": 5}.get(case, 1)
    rows = [{"id": "candidate" + str(i), "threadId": "syntheticthread1"} for i in range(count)]
    payload = {"messages": rows, "resultSizeEstimate": count}
    if case in {"next_page", "empty_page_token"}:
        payload["nextPageToken"] = "private-next-page" if case == "next_page" else ""
    if case == "estimate":
        payload["resultSizeEstimate"] = count + 1
    boundary.search = Reply(payload)
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.UNRESOLVED
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and len(record.attempts) == 1
    expected = (ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED if count > 1 else
                ReconciliationDisposition.PROBE_LATER)
    assert record.reconciliation.disposition is expected
    assert not h.invoke(action_id=record.action_id).ok
    assert len(boundary.requests) == 1 + count and not boundary.posts
    assert all("pageToken=" not in request.path for request in boundary.requests)


@pytest.mark.parametrize("case", [
    "from", "from_alias", "to", "cc", "bcc", "subject", "subject_encoding", "body", "message_id",
    "date", "date_zone", "mime_version", "content_type", "transfer_encoding", "multipart", "filename",
    "attachment", "parts", "missing_header", "duplicate_header", "header_injection", "reply_to",
    "content_extra", "body_size", "body_encoding", "body_utf8", "not_sent", "unknown_payload",
])
def test_unprovable_candidate_is_unknown_without_exposing_content_or_fetching_parts(uncertain, boundary, case):
    h = uncertain
    candidate = full_message(boundary.mime)
    headers = {
        "from": ("From", "other@sample.invalid"), "from_alias": ("From", "Sender <" + SENDER + ">"),
        "to": ("To", "other@sample.invalid"), "cc": ("Cc", None), "bcc": ("Bcc", "blind@sample.invalid"),
        "subject": ("Subject", "Different private subject"),
        "subject_encoding": ("Subject", "=?unknown?b?dGVzdA==?="),
        "message_id": ("Message-ID", h.foundation.review(h.preparation)["message_id"][1:-1]),
        "date": ("Date", "Sun, 30 Aug 2026 10:00:00 +0000"),
        "date_zone": ("Date", "Sun, 30 Aug 2026 12:00:00 +0200"),
        "mime_version": ("MIME-Version", "2.0"), "content_type": ("Content-Type", "text/html; charset=utf-8"),
        "transfer_encoding": ("Content-Transfer-Encoding", "quoted-printable"),
        "missing_header": ("Subject", None), "header_injection": ("Subject", "Private send subject\nInjected: bad"),
        "reply_to": ("Reply-To", "other@sample.invalid"), "content_extra": ("Content-Language", "en"),
    }
    if case in headers:
        set_header(candidate, *headers[case])
    elif case == "body":
        set_body(candidate, b"different private body\r\n")
    elif case == "multipart":
        candidate["payload"]["mimeType"] = "multipart/mixed"
    elif case == "filename":
        candidate["payload"]["filename"] = "private.txt"
    elif case == "attachment":
        candidate["payload"]["body"]["attachmentId"] = "do-not-fetch"
    elif case == "parts":
        candidate["payload"]["parts"] = [{"partId": "1"}]
    elif case == "duplicate_header":
        candidate["payload"]["headers"].append({"name": "Subject", "value": LIVE_ARGS["subject"]})
    elif case == "body_size":
        candidate["payload"]["body"]["size"] += 1
    elif case == "body_encoding":
        candidate["payload"]["body"]["data"] += "!"
    elif case == "body_utf8":
        set_body(candidate, b"\xff")
    elif case == "not_sent":
        candidate["labelIds"] = ["INBOX"]
    else:
        candidate["payload"]["private_unknown"] = "unreviewed content"
    boundary.candidates["syntheticmessage1"] = Reply(candidate)
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.UNRESOLVED
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.reconciliation.disposition is ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
    assert len(boundary.requests) == 2 and not boundary.posts
    assert all("/attachments/" not in request.path for request in boundary.requests)
    evidence = h.evidence.load_reconciliation_evidence(record.evidence_refs[0], action_id=record.action_id,
        claim_epoch=report.claim_epoch, intent_digest=record.intent_digest).evidence
    assert evidence["candidates"][0]["strong_match"] is False
    assert "different private" not in json.dumps(evidence) + h.ordinary_json() + json.dumps(asdict(report))


@pytest.mark.parametrize("case", ["unicode", "folded_subject", "lf_body", "gmt_date", "domain_case"])
def test_reviewed_lossless_mime_representations_can_prove_exact_semantics(live, boundary, case):
    h = live
    args = {**LIVE_ARGS, "subject": "A café subject" if case == "unicode" else "Private send subject"}
    boundary.send = Reply(error=TimeoutError())
    first = h.invoke(args)
    candidate = full_message(boundary.mime)
    if case == "folded_subject":
        set_header(candidate, "Subject", "Private send\r\n subject")
    elif case == "lf_body":
        set_body(candidate, b"Private send body\nsecond line\n")
    elif case == "gmt_date":
        set_header(candidate, "Date", h.foundation.review(h.preparation)["date"])
    elif case == "domain_case":
        set_header(candidate, "From", "sender@SAMPLE.INVALID")
    boundary.candidates["syntheticmessage1"] = Reply(candidate)
    assert h.reconcile(first.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert len(boundary.posts) == 1


@pytest.mark.parametrize("case", [
    "six", "duplicate", "missing_thread", "bad_id", "secret_id", "secret_thread", "extra_key", "bad_estimate",
    "bad_page", "redirect", "too_large", "bad_json", "compressed", "timeout", "candidate_id",
    "candidate_thread", "candidate_secret", "candidate_large", "candidate_html", "candidate_missing",
])
def test_malformed_or_secret_echoing_provider_results_fail_probe_without_false_truth(uncertain, boundary, case):
    h = uncertain
    rows = [{"id": "syntheticmessage1", "threadId": "syntheticthread1"}]
    payload = {"messages": rows}
    if case == "six":
        payload["messages"] = [{"id": str(i), "threadId": "thread"} for i in range(6)]
    elif case == "duplicate":
        rows.append(dict(rows[0]))
    elif case == "missing_thread":
        del rows[0]["threadId"]
    elif case in {"bad_id", "secret_id", "secret_thread"}:
        rows[0]["threadId" if case == "secret_thread" else "id"] = "../send" if case == "bad_id" else ACCESS
    elif case == "extra_key":
        payload["raw"] = "unreviewed"
    elif case == "bad_estimate":
        payload["resultSizeEstimate"] = True
    elif case == "bad_page":
        payload["nextPageToken"] = {}
    boundary.search = Reply(payload)
    if case == "redirect":
        boundary.search = Reply(status=302, headers=[("Location", "https://other.invalid")])
    elif case == "too_large":
        boundary.search = Reply(raw=b" " * 8193)
    elif case == "bad_json":
        boundary.search = Reply(raw=b'{"messages":[],"messages":[]}')
    elif case == "compressed":
        boundary.search = Reply(payload, headers=[("Content-Type", "application/json"), ("Content-Encoding", "gzip")])
    elif case == "timeout":
        boundary.search = Reply(error=TimeoutError(ACCESS + REFRESH))
    elif case.startswith("candidate_"):
        candidate = full_message(boundary.mime)
        if case == "candidate_id":
            candidate["id"] = "other"
        elif case == "candidate_thread":
            candidate["threadId"] = "other"
        elif case == "candidate_secret":
            candidate["threadId"] = base64.urlsafe_b64encode(ACCESS.encode()).decode().rstrip("=")
        elif case == "candidate_missing":
            del candidate["labelIds"]
        reply = Reply(candidate)
        if case == "candidate_large":
            reply = Reply(raw=b" " * 262145)
        elif case == "candidate_html":
            reply = Reply(raw=b"<html>private</html>", headers=[("Content-Type", "text/html")])
        boundary.candidates["syntheticmessage1"] = reply
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.PROBE_FAILED
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and len(record.attempts) == 1
    assert record.reconciliation.probe_count == 1 and not record.evidence_refs
    assert len(boundary.requests) == (2 if case.startswith("candidate_") else 1) and not boundary.posts
    assert ACCESS not in h.ordinary_json() + json.dumps(asdict(report)) + json.dumps(h.logger.events)


@pytest.mark.parametrize("case", ["permission", "credential_permission", "read_permission", "disconnect", "stale", "reconnect", "expired"])
def test_production_probe_needs_current_read_permission_and_exact_live_connection(uncertain, boundary, case):
    h = uncertain
    if case == "permission":
        h.executor.policy = send_policy(probe=False)
    elif case.endswith("permission"):
        capability = Capability.CREDENTIAL_ACCESS if case == "credential_permission" else Capability.NET_READ
        h.executor.policy = send_policy(blocked=(str(capability),))
    elif case == "disconnect":
        h.google.service.disconnect(h.info.reference)
    elif case == "stale":
        h.google.service.refresh(h.info.reference)
    elif case == "reconnect":
        h.google.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    else:
        h.clock.advance(7200)
    assert h.reconcile(h.first.action_id).status in {
        ActionReconciliationStatus.PROBE_DENIED, ActionReconciliationStatus.PROBE_FAILED,
    }
    assert not boundary.requests
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and record.reconciliation.probe_count == 0


def test_read_probe_survives_send_revocation_and_old_claim_cannot_read_again(uncertain, boundary):
    h = uncertain
    h.executor.policy = send_policy(send=False, probe=True, blocked=(str(Capability.EMAIL_SEND), str(Capability.NET_WRITE)))
    record = h.actions.load(h.first.action_id)
    claim = h.recovery.claim(record.action_id, expected_version=record.version)
    assert h.recovery.reconcile(claim).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert h.recovery.reconcile(claim).status is ActionReconciliationStatus.STALE_CLAIM
    h.prepare()
    assert not h.invoke().ok
    assert len(boundary.requests) == 2 and not boundary.posts


@pytest.mark.parametrize("cut", ["provider_result", "success_publication"])
def test_actual_process_crash_after_synthetic_post_recovers_identical_intent_and_only_gets(tmp_path, boundary, cut):
    code = "\n".join([
        "import base64, json, os, sys", "from pathlib import Path", "from dataclasses import asdict",
        "sys.path.insert(0, 'tests')",
        "from gmail_mutation_http_fakes import HttpBoundary, LiveHarness",
        "from agentcore.action_journal import ActionState",
        "wire = HttpBoundary(); wire.install()", "h = LiveHarness(Path(sys.argv[1]))", "h.prepare()",
        "def effect(request):", "    if request.method != 'POST': return",
        "    record = h.actions.load(h.actions.list_actions()[0])",
        "    assert record.state is ActionState.IN_FLIGHT and len(wire.posts) == 1",
        "    receipt = {'action_id': record.action_id, 'handle': asdict(h.preparation),",
        "               'mime': base64.b64encode(wire.mime).decode(), 'account': h.info.account_id,",
        "               'connection': h.info.connection_id, 'generation': h.info.generation}",
        "    (h.directory / 'synthetic-remote-effect.json').write_text(json.dumps(receipt), encoding='utf-8')",
        "    if sys.argv[2] == 'provider_result': os._exit(71)",
        "wire.on_response = effect", "cas = h.actions.compare_and_swap",
        "def publish(action_id, version, candidate):",
        "    if candidate.state is ActionState.SUCCEEDED: os._exit(71)",
        "    return cas(action_id, version, candidate)",
        "if sys.argv[2] == 'success_publication': h.actions.compare_and_swap = publish", "h.invoke()",
    ])
    crashed = subprocess.run([sys.executable, "-c", code, str(tmp_path), cut], capture_output=True, text=True, timeout=20)
    assert crashed.returncode == 71, crashed.stderr
    receipt = json.loads((tmp_path / "synthetic-remote-effect.json").read_text(encoding="utf-8"))
    stored = json.loads(next((tmp_path / "actions").glob("act_*.json")).read_text(encoding="utf-8"))
    assert stored["state"] == ActionState.IN_FLIGHT.value and "credential_execution" not in json.dumps(stored)
    boundary.template_mime = base64.b64decode(receipt["mime"])
    h = LiveHarness(tmp_path)
    try:
        record = h.actions.load(receipt["action_id"])
        assert record.state is ActionState.RECONCILE_REQUIRED and len(record.attempts) == 1
        assert not boundary.requests
        assert h.info.account_id == receipt["account"] and h.info.connection_id == receipt["connection"]
        assert h.info.generation > receipt["generation"]
        handle = GmailSendPreparation(**receipt["handle"])
        h.foundation.restore(handle)
        assert h.foundation._load_preparation(handle.preparation_id)[0]["mime_base64"] == receipt["mime"]
        assert h.reconcile(record.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
        assert h.invoke(preparation=handle, action_id=record.action_id).ok
        assert len(boundary.requests) == 2 and not boundary.posts
    finally:
        h.close()
