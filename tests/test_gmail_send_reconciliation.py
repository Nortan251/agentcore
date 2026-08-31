"""Conservative account-bound send truth: probes cannot authorize mutations."""

import copy
import json
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest

from agentcore.action_journal import (
    MAX_RECONCILIATION_PROBES, ActionReconciliationContext, ActionReconciliationStatus, ActionState, ReconciliationDisposition,
)
from agentcore.credentials import CredentialError, GoogleAccessProfile
from agentcore.integrations.gmail.send import GmailProbeCandidate, GmailProbeResponse
from agentcore.permissions.levels import Capability
from agentcore.realtime.errors import CancellationRequested
from gmail_send_fakes import ACCESS, ARGS, REFRESH, Harness, send_policy


@pytest.fixture
def uncertain(tmp_path):
    h = Harness(tmp_path)
    h.fake.after_effect = "timeout"
    h.first = h.invoke()
    assert h.first.action_state == ActionState.RECONCILE_REQUIRED.value
    yield h
    h.close()


def test_one_complete_sent_candidate_with_exact_semantics_settles_success_and_protects_evidence(uncertain):
    h = uncertain
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.SUCCEEDED and len(record.attempts) == 1
    assert len(h.fake.sends) == len(h.fake.probes) == 1
    assert h.fake.probes[0].claim_epoch == report.claim_epoch
    assert h.fake.probes[0].attempt_id == h.first.attempt_id
    assert len(record.evidence_refs) == 1
    evidence = h.evidence.load_reconciliation_evidence(record.evidence_refs[0], action_id=record.action_id,
                                                      claim_epoch=report.claim_epoch, intent_digest=record.intent_digest)
    assert evidence.evidence["reason"] == "single_match"
    assert evidence.evidence["candidates"] == [{"message_id": "syntheticmessage1", "thread_id": "syntheticthread1", "strong_match": True}]
    public = json.dumps(asdict(report)) + json.dumps(h.first.to_dict()) + h.ordinary_json() + json.dumps(h.logger.events)
    for private in [ACCESS, REFRESH, ARGS["subject"], "Private send body", *ARGS["to"], *ARGS["cc"], *ARGS["bcc"], h.fake.sends[0]["marker"]]:
        assert private not in public
    assert ARGS["body"] not in json.dumps(evidence.evidence)
    assert ACCESS not in json.dumps(evidence.evidence)
    assert h.invoke(action_id=record.action_id).ok and len(h.fake.sends) == 1


def test_no_match_is_never_proof_of_absence_or_permission_to_retry(uncertain):
    h = uncertain
    h.fake.candidates = ()
    for _ in range(2):
        report = h.reconcile(h.first.action_id)
        assert report.status is ActionReconciliationStatus.UNRESOLVED
        record = h.actions.load(h.first.action_id)
        assert record.state is ActionState.RECONCILE_REQUIRED
        assert record.reconciliation.disposition is ReconciliationDisposition.PROBE_LATER
        assert len(record.attempts) == 1
        assert not h.invoke(action_id=record.action_id).ok
    assert len(h.fake.sends) == 1


@pytest.mark.parametrize("case", ["multiple", "recipient", "cc", "bcc", "subject", "body", "sender", "marker", "date", "version", "id_only", "not_sent", "incomplete_candidate", "incomplete_search"])
def test_weak_mismatching_or_duplicate_candidates_preserve_uncertainty(uncertain, case):
    h = uncertain
    semantics = copy.deepcopy(h.fake.sends[0]["semantics"])
    if case in {"recipient", "cc", "bcc"}:
        semantics["message"]["to" if case == "recipient" else case] = ["different@sample.invalid"]
    elif case in {"subject", "body"}:
        semantics["message"][case] = "different content"
    elif case == "sender":
        semantics["sender"] = "different@sample.invalid"
    elif case == "marker":
        semantics["message_id"] = "<" + "f" * 64 + "@agentcore.invalid>"
    elif case == "date":
        semantics["date"] = "Sun, 30 Aug 2026 10:00:00 +0000"
    elif case == "version":
        semantics["mime_version"] = 2
    elif case == "id_only":
        semantics = {"message_id": semantics["message_id"]}
    candidate = GmailProbeCandidate("one", "thread", case != "not_sent", case != "incomplete_candidate", semantics)
    h.fake.candidates = ((candidate, replace(candidate, message_id="two")) if case == "multiple" else (candidate,))
    h.fake.complete = case != "incomplete_search"
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.UNRESOLVED
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and len(record.attempts) == 1
    expected = (ReconciliationDisposition.PROBE_LATER if case == "incomplete_search" else
                ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
    assert record.reconciliation.disposition is expected
    assert len(h.fake.sends) == 1


@pytest.mark.parametrize("case", ["too_many", "duplicate_id", "bad_id", "secret_id", "raw", "extra_field", "missing_recipients", "unbounded_body", "error"])
def test_malformed_or_secret_bearing_probe_never_settles_or_discloses(uncertain, case):
    h = uncertain
    semantics = copy.deepcopy(h.fake.sends[0]["semantics"])
    candidate = GmailProbeCandidate("one", "thread", True, True, semantics)
    h.fake.candidates = (candidate,)
    if case == "too_many":
        h.fake.candidates = tuple(replace(candidate, message_id=str(i)) for i in range(6))
    elif case == "duplicate_id":
        h.fake.candidates = (candidate, candidate)
    elif case in {"bad_id", "secret_id"}:
        h.fake.candidates = (replace(candidate, message_id=ACCESS if case == "secret_id" else "x" * 129),)
    elif case == "raw":
        h.fake.probe_hook = lambda *_: {"effect": "applied"}
    elif case == "extra_field":
        semantics["access_token"] = ACCESS
    elif case == "missing_recipients":
        del semantics["message"]["bcc"]
    elif case == "unbounded_body":
        semantics["message"]["body"] = "x" * 65537
    else:
        def fail(*_):
            raise TimeoutError(ACCESS + REFRESH)
        h.fake.probe_hook = fail
    report = h.reconcile(h.first.action_id)
    assert report.status is ActionReconciliationStatus.PROBE_FAILED
    assert h.actions.load(h.first.action_id).state is ActionState.RECONCILE_REQUIRED
    assert h.actions.load(h.first.action_id).reconciliation.probe_count == 1
    assert len(h.fake.sends) == len(h.fake.probes) == 1
    assert ACCESS not in json.dumps(asdict(report)) + h.ordinary_json()


@pytest.mark.parametrize("case", ["probe_permission", "credential_permission", "read_permission", "disconnect", "stale", "reconnect", "expired"])
def test_probe_requires_current_credential_and_independent_current_read_authority(uncertain, case):
    h = uncertain
    if case == "probe_permission":
        h.executor.policy = send_policy(probe=False)
    elif case in {"credential_permission", "read_permission"}:
        blocked = Capability.CREDENTIAL_ACCESS if case == "credential_permission" else Capability.NET_READ
        h.executor.policy = send_policy(blocked=(str(blocked),))
    elif case == "disconnect":
        h.google.service.disconnect(h.info.reference)
    elif case == "stale":
        h.google.service.refresh(h.info.reference)
    elif case == "reconnect":
        h.google.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    else:
        h.clock.advance(7200)
    report = h.reconcile(h.first.action_id)
    assert report.status in {ActionReconciliationStatus.PROBE_DENIED, ActionReconciliationStatus.PROBE_FAILED}
    assert h.actions.load(h.first.action_id).state is ActionState.RECONCILE_REQUIRED
    assert h.actions.load(h.first.action_id).reconciliation.probe_count == 0
    assert not h.fake.probes and len(h.fake.sends) == 1


@pytest.mark.parametrize("failure", ["missing_runtime", "stale_binding"])
def test_pretransport_failures_preserve_probe_quota_until_credentials_are_restored(uncertain, monkeypatch, failure):
    h = uncertain
    if failure == "missing_runtime":
        monkeypatch.setattr(h.executor, "_credential_runtime", None)
    else:
        refreshed = h.google.service.refresh(h.info.reference)

    # Exercise the actual frozen limit: failed local resolution must not make
    # this uncertain send permanently unclaimable once credentials are usable.
    for _ in range(MAX_RECONCILIATION_PROBES):
        assert h.reconcile(h.first.action_id).status is ActionReconciliationStatus.PROBE_FAILED
    record = h.actions.load(h.first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED and len(record.attempts) == 1
    assert not h.fake.probes and len(h.fake.sends) == 1
    assert record.reconciliation.probe_count == 0
    assert record.reconciliation.claim_owner_id is None
    if failure == "missing_runtime":
        monkeypatch.setattr(h.executor, "_credential_runtime", h.runtime)
    else:
        h.foundation.bind(h.runtime, refreshed.reference, replace_existing=True)
    assert h.reconcile(record.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert h.actions.load(record.action_id).state is ActionState.SUCCEEDED
    assert len(h.fake.probes) == len(h.fake.sends) == 1


@pytest.mark.parametrize("cancel_at", ["credential_lookup", "after_provider"])
def test_cancelled_reconciliation_counts_only_an_entered_provider_probe(uncertain, monkeypatch, cancel_at):
    h = uncertain
    cancel = False
    original_lease = h.google.vault._lease

    @contextmanager
    def lookup(*args, **kwargs):
        nonlocal cancel
        with original_lease(*args, **kwargs) as lease:
            cancel = cancel_at == "credential_lookup"
            yield lease

    def probe(request, credential):
        nonlocal cancel
        cancel = cancel_at == "after_provider"
        return GmailProbeResponse((GmailProbeCandidate("one", "thread", True, True, request.semantics),), True)

    def checkpoint():
        if cancel:
            raise CancellationRequested("synthetic host cancellation")

    monkeypatch.setattr(h.google.vault, "_lease", lookup)
    h.fake.probe_hook = probe
    with pytest.raises(CancellationRequested):
        h.reconcile(h.first.action_id, checkpoint=checkpoint)
    record = h.actions.load(h.first.action_id)
    entered = int(cancel_at == "after_provider")
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.reconciliation.claim_owner_id is None
    assert record.reconciliation.probe_count == len(h.fake.probes) == entered
    assert len(record.attempts) == len(h.fake.sends) == 1
    assert h.reconcile(record.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert len(h.fake.probes) == entered + 1 and len(h.fake.sends) == 1


def test_revoking_send_can_still_allow_readonly_reconciliation_but_cannot_send_again(uncertain):
    h = uncertain
    h.executor.policy = send_policy(send=False, probe=True, blocked=(str(Capability.EMAIL_SEND),))
    assert h.reconcile(h.first.action_id).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    h.prepare()
    assert not h.invoke().ok and len(h.fake.sends) == 1


def test_old_claim_and_fabricated_direct_reconciler_context_never_probe(uncertain):
    h = uncertain
    record = h.actions.load(h.first.action_id)
    context = ActionReconciliationContext.from_record(record)
    with pytest.raises(CredentialError):
        h.foundation.reconciler.probe(context)
    claim = h.recovery.claim(record.action_id, expected_version=record.version)
    assert h.recovery.reconcile(claim).status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert h.recovery.reconcile(claim).status is ActionReconciliationStatus.STALE_CLAIM
    assert len(h.fake.probes) == 1 and len(h.fake.sends) == 1
