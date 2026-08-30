"""Explicit Gmail consent, exact identity, durable state, and stale responses."""

import subprocess
import sys
import threading

import pytest

from agentcore.credentials import CredentialError, CredentialFailure, CredentialRuntime, GoogleAccessProfile
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES, GOOGLE_IDENTITY_SCOPES
from agentcore.credentials.protected import ConnectionState
from agentcore.integrations.gmail import GmailReadonlyTools
from gmail_fakes import GMAIL_SCOPES, grant_gmail
from google_oauth_fakes import ACCESS, REFRESH, SUBJECT, bundle


@pytest.fixture
def identity(tmp_path):
    value = bundle(tmp_path / "protected")
    value.info = value.service.connect()
    yield value
    value.vault.close()


def test_identity_only_connection_cannot_bind_gmail_or_expand_through_refresh(identity):
    runtime = CredentialRuntime(identity.vault)
    with pytest.raises(CredentialError) as error:
        GmailReadonlyTools().bind(runtime, identity.info.reference)
    assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
    identity.transport.refreshed["scope"] = GMAIL_SCOPES
    with pytest.raises(CredentialError) as error:
        identity.service.refresh(identity.info.reference)
    assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
    assert identity.service.restore().credential.scopes == GOOGLE_IDENTITY_SCOPES
    assert len(identity.transport.exchanges) == 1


def test_gmail_upgrade_is_fresh_complete_consent_and_new_connection_for_same_account(identity):
    old = identity.info
    before = dict(identity.browser.parameters)
    new = grant_gmail(identity)
    request = identity.browser.parameters
    assert set(request["scope"].split()) == GOOGLE_GMAIL_READONLY_SCOPES
    assert request["prompt"] == "consent" and request["access_type"] == "offline"
    assert request["code_challenge_method"] == "S256"
    assert "include_granted_scopes" not in request
    assert request["state"] != before["state"] and request["nonce"] != before["nonce"]
    assert request["code_challenge"] != before["code_challenge"]
    assert len(identity.transport.exchanges) == 2 and not identity.transport.refreshes
    assert identity.vault._data.identity.subject == SUBJECT
    assert new.account_id == old.account_id and new.connection_id != old.connection_id
    assert new.reference != old.reference and new.generation == 1
    assert new.scopes == GOOGLE_GMAIL_READONLY_SCOPES
    assert identity.service.restore().state is ConnectionState.CURRENT


@pytest.mark.parametrize("profile", ["gmail_readonly", ["openid", "email", "profile"], None, True, {"scope": GMAIL_SCOPES}])
def test_host_profile_is_closed_enum_and_not_arbitrary_scope_input(identity, profile):
    before = identity.vault._file.read_current()
    with pytest.raises(CredentialError) as error:
        identity.service.connect(profile=profile)
    assert error.value.reason is CredentialFailure.INVALID
    assert len(identity.transport.exchanges) == 1
    assert identity.vault._file.read_current() == before


@pytest.mark.parametrize("scopes", [
    "openid email profile", "openid email " + "https://www.googleapis.com/auth/gmail.readonly",
    GMAIL_SCOPES + " https://www.googleapis.com/auth/gmail.modify",
    GMAIL_SCOPES + " https://www.googleapis.com/auth/gmail.send",
    GMAIL_SCOPES + " https://www.googleapis.com/auth/gmail.compose",
    GMAIL_SCOPES + " https://www.googleapis.com/auth/gmail.labels",
    GMAIL_SCOPES + " https://mail.google.com/",
    GMAIL_SCOPES + " https://www.googleapis.com/auth/drive.readonly",
    "", None,
])
def test_incomplete_or_broader_gmail_grants_preserve_prior_identity_and_refresh(identity, scopes):
    before = identity.vault._file.read_current()
    identity.transport.initial.update(scope=scopes, access_token="new-" + ACCESS, refresh_token="new-" + REFRESH)
    with pytest.raises(CredentialError) as error:
        identity.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
    assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
    assert identity.vault._file.read_current() == before
    assert identity.service.ensure_current(identity.info.reference) == identity.info
    assert identity.service.restore().can_refresh
    assert identity.vault._data.refresh._read() == REFRESH


def test_gmail_granted_aliases_normalize_to_exact_profile(identity):
    identity.transport.initial["scope"] = (
        "openid https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/userinfo.profile https://www.googleapis.com/auth/gmail.readonly")
    info = identity.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
    assert info.scopes == GOOGLE_GMAIL_READONLY_SCOPES


def test_wrong_signed_google_account_does_not_overwrite_or_publish(identity):
    before = identity.vault._file.read_current()
    identity.verifier.changes["sub"] = "another-synthetic-account"
    with pytest.raises(CredentialError) as error:
        grant_gmail(identity)
    assert error.value.reason is CredentialFailure.IDENTITY_MISMATCH
    assert identity.vault._data.identity.subject == SUBJECT
    assert identity.vault._file.read_current() == before
    assert identity.service.restore().credential == identity.info
    assert identity.vault._access._read() == ACCESS
    assert identity.vault._data.refresh._read() == REFRESH


@pytest.mark.parametrize("failure", ["denied", "network", "proof", "commit", "refresh_missing", "scope_missing"])
def test_failed_gmail_upgrade_preserves_valid_previous_state(identity, monkeypatch, failure):
    before = identity.vault._file.read_current()
    if failure == "denied":
        monkeypatch.setattr(identity.service, "_browser", lambda _: False)
    elif failure == "network":
        def fail():
            raise TimeoutError(ACCESS + REFRESH)
        identity.transport.on_exchange = fail
    elif failure == "proof":
        identity.verifier.error = RuntimeError(ACCESS)
    elif failure == "commit":
        def fail(_):
            raise OSError(REFRESH)
        monkeypatch.setattr(identity.vault._file, "replace", fail)
    elif failure == "refresh_missing":
        del identity.transport.initial["refresh_token"]
    else:
        del identity.transport.initial["scope"]
    with pytest.raises(CredentialError) as error:
        if failure == "scope_missing":
            identity.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
        else:
            grant_gmail(identity)
    assert error.value.__context__ is None
    assert ACCESS not in str(error.value) and REFRESH not in str(error.value)
    assert identity.vault._file.read_current() == before
    assert identity.service.restore().credential == identity.info
    assert identity.service.restore().state is ConnectionState.CURRENT
    assert identity.service.restore().can_refresh


@pytest.mark.parametrize("cut", ["before_replace", "after_replace"])
def test_process_crash_during_gmail_reconsent_reopens_one_complete_connection(identity, tmp_path, cut):
    identity.vault.close()
    code = "\n".join([
        "import os, sys",
        "from pathlib import Path",
        "sys.path.insert(0, 'tests')",
        "from google_oauth_fakes import bundle, TestProtector",
        "from gmail_fakes import grant_gmail",
        "b = bundle(Path(sys.argv[1]), protector=TestProtector(sys.argv[2].encode()))",
        "original = b.vault._file.replace",
        "def crash(data):",
        "    if sys.argv[3] == 'after_replace':",
        "        original(data)",
        "    os._exit(71)",
        "b.vault._file.replace = crash",
        "grant_gmail(b)",
    ])
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "protected"),
                             identity.protector.key.decode(), cut], capture_output=True, text=True, timeout=15)
    assert result.returncode == 71, result.stderr
    reopened = bundle(tmp_path / "protected", protector=identity.protector)
    try:
        status = reopened.service.restore()
        assert status.state is ConnectionState.REFRESH_REQUIRED
        assert status.credential.account_id == identity.info.account_id
        assert reopened.vault._access is None
        expected = GOOGLE_GMAIL_READONLY_SCOPES if cut == "after_replace" else GOOGLE_IDENTITY_SCOPES
        assert status.credential.scopes == expected
        assert (status.credential.connection_id != identity.info.connection_id) == (cut == "after_replace")
        reopened.transport.refreshed["scope"] = " ".join(sorted(expected))
        new = reopened.service.ensure_current(status.credential.reference)
        assert new.scopes == expected and new.generation == status.credential.generation + 1
    finally:
        reopened.vault.close()


@pytest.mark.parametrize("intervention", ["disconnect", "new_consent", "failed_consent"])
def test_late_identity_refresh_cannot_publish_after_gmail_consent_event(identity, intervention):
    entered, release = threading.Event(), threading.Event()
    results = []
    def pause():
        entered.set()
        assert release.wait(5)
    identity.transport.on_refresh = pause
    def refresh():
        try:
            results.append(identity.service.refresh(identity.info.reference))
        except CredentialError as error:
            results.append(error.reason)
    thread = threading.Thread(target=refresh, daemon=True)
    thread.start()
    assert entered.wait(2)
    try:
        if intervention == "disconnect":
            identity.service.disconnect(identity.info.reference)
        elif intervention == "new_consent":
            grant_gmail(identity)
        else:
            identity.verifier.changes["sub"] = "another-synthetic-account"
            with pytest.raises(CredentialError):
                grant_gmail(identity)
            identity.verifier.changes.clear()
        snapshot = identity.vault._file.read_current()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and results == [CredentialFailure.STALE]
    assert identity.vault._file.read_current() == snapshot


def test_default_identity_consent_never_accepts_a_gmail_grant(identity):
    identity.transport.initial["scope"] = GMAIL_SCOPES
    with pytest.raises(CredentialError):
        identity.service.connect()
    assert identity.service.restore().credential == identity.info
    assert set(identity.browser.parameters["scope"].split()) == GOOGLE_IDENTITY_SCOPES


def test_uncertain_post_replace_commit_requires_reopen_and_never_pairs_old_access_with_gmail(identity, tmp_path, monkeypatch):
    original = identity.vault._file.replace
    def replace_then_fail(data):
        original(data)
        raise OSError(ACCESS + REFRESH)
    with monkeypatch.context() as patch:
        patch.setattr(identity.vault._file, "replace", replace_then_fail)
        with pytest.raises(CredentialError) as error:
            grant_gmail(identity)
        assert error.value.reason is CredentialFailure.COMMIT_FAILED
        with pytest.raises(CredentialError):
            identity.service.restore()
    identity.vault.close()
    reopened = bundle(tmp_path / "protected", protector=identity.protector)
    try:
        status = reopened.service.restore()
        assert status.credential.scopes == GOOGLE_GMAIL_READONLY_SCOPES
        assert status.credential.account_id == identity.info.account_id
        assert status.state is ConnectionState.REFRESH_REQUIRED and reopened.vault._access is None
    finally:
        reopened.vault.close()


def test_gmail_authorization_response_cannot_publish_after_disconnect(identity):
    entered, release = threading.Event(), threading.Event()
    results = []
    def pause():
        entered.set()
        assert release.wait(5)
    identity.transport.on_exchange = pause
    def upgrade():
        try:
            results.append(grant_gmail(identity))
        except CredentialError as error:
            results.append(error.reason)
    thread = threading.Thread(target=upgrade, daemon=True)
    thread.start()
    assert entered.wait(2)
    try:
        assert identity.vault._lock.acquire(blocking=False)
        identity.vault._lock.release()
        assert identity.service.restore().state is ConnectionState.PENDING
        identity.service.disconnect(identity.info.reference)
        snapshot = identity.vault._file.read_current()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and results == [CredentialFailure.STALE]
    assert identity.vault._file.read_current() == snapshot
    assert identity.service.restore().state is ConnectionState.DISCONNECTED
