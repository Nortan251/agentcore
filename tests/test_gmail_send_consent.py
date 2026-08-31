"""Fresh, exact send consent and binding, using only synthetic Google proof."""

import pytest

from agentcore.action_journal import InMemoryProtectedActionPayloadStore, StaticInstallationKeyProvider
from agentcore.credentials import CredentialError, CredentialFailure, CredentialRuntime, GoogleAccessProfile
from agentcore.credentials.google_protocol import (
    GOOGLE_GMAIL_READONLY_SCOPES, GOOGLE_GMAIL_SEND_SCOPES, GOOGLE_IDENTITY_SCOPES,
)
from agentcore.integrations.gmail.send import GmailSendFoundation
from google_oauth_fakes import ACCESS, REFRESH, SUBJECT, bundle
from gmail_send_fakes import SENDER, SCOPES


@pytest.fixture
def readonly(tmp_path):
    b = bundle(tmp_path / "google")
    b.transport.initial["scope"] = " ".join(sorted(GOOGLE_GMAIL_READONLY_SCOPES))
    b.info = b.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
    yield b
    b.vault.close()


def test_send_profile_exact_fresh_consent_keeps_signed_account_replaces_connection(readonly):
    b = readonly
    old, previous = b.info, dict(b.browser.parameters)
    b.transport.initial["scope"] = SCOPES
    new = b.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    assert new.scopes == GOOGLE_GMAIL_SEND_SCOPES == GOOGLE_GMAIL_READONLY_SCOPES | {
        "https://www.googleapis.com/auth/gmail.send"}
    request = b.browser.parameters
    assert set(request["scope"].split()) == GOOGLE_GMAIL_SEND_SCOPES
    assert request["prompt"] == "consent" and request["access_type"] == "offline"
    assert request["code_challenge_method"] == "S256" and "include_granted_scopes" not in request
    for field in ("state", "nonce", "code_challenge"):
        assert request[field] != previous[field]
    assert len(b.transport.exchanges) == 2 and not b.transport.refreshes
    assert b.vault._data.identity.subject == SUBJECT
    assert new.account_id == old.account_id and new.connection_id != old.connection_id
    assert new.reference != old.reference and new.generation == 1


def test_readonly_cannot_bind_send_or_expand_send_through_refresh(readonly):
    b = readonly
    runtime = CredentialRuntime(b.vault)
    payloads = InMemoryProtectedActionPayloadStore(StaticInstallationKeyProvider(bytes(range(32))))
    try:
        with pytest.raises(CredentialError) as error:
            GmailSendFoundation(payloads, sender=SENDER).bind(runtime, b.info.reference)
        assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
        b.transport.refreshed["scope"] = SCOPES
        with pytest.raises(CredentialError) as error:
            b.service.refresh(b.info.reference)
        assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
        status = b.service.restore()
        assert status.credential == b.info and not status.can_refresh
        assert b.vault._access._read() == ACCESS and b.vault._data.refresh._read() == REFRESH
    finally:
        runtime.close()
        payloads.close()


@pytest.mark.parametrize("scope", [
    " ".join(sorted(GOOGLE_IDENTITY_SCOPES)), " ".join(sorted(GOOGLE_GMAIL_READONLY_SCOPES)),
    "openid email profile https://www.googleapis.com/auth/gmail.send",
    SCOPES + " https://www.googleapis.com/auth/gmail.compose",
    SCOPES + " https://www.googleapis.com/auth/gmail.modify",
    SCOPES + " https://mail.google.com/", SCOPES + " https://www.googleapis.com/auth/drive.readonly",
    "", None,
], ids=["identity", "readonly", "no_read_scope", "compose", "modify", "full_mail", "drive", "empty", "missing"])
def test_send_consent_rejects_partial_or_broader_scopes_without_replacing_prior_connection(readonly, scope):
    b = readonly
    before = b.vault._file.read_current()
    b.transport.initial.update(scope=scope, access_token="new-" + ACCESS, refresh_token="new-" + REFRESH)
    with pytest.raises(CredentialError) as error:
        b.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
    assert b.vault._file.read_current() == before
    assert b.service.restore().credential == b.info
    assert b.vault._access._read() == ACCESS and b.vault._data.refresh._read() == REFRESH


@pytest.mark.parametrize("scope", [" ".join(sorted(GOOGLE_GMAIL_READONLY_SCOPES)),
                                  SCOPES + " https://www.googleapis.com/auth/gmail.compose", ""])
def test_send_refresh_neither_shrinks_nor_broadens_profile(readonly, scope):
    b = readonly
    b.transport.initial["scope"] = SCOPES
    b.info = b.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    b.transport.refreshed["scope"] = scope
    with pytest.raises(CredentialError) as error:
        b.service.refresh(b.info.reference)
    assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
    status = b.service.restore()
    assert status.credential == b.info and not status.can_refresh
    assert status.credential.scopes == GOOGLE_GMAIL_SEND_SCOPES
    assert b.vault._access._read() == ACCESS and b.vault._data.refresh._read() == REFRESH


def test_omitted_refresh_scope_means_exact_prior_send_profile(readonly):
    b = readonly
    b.transport.initial["scope"] = SCOPES
    info = b.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    del b.transport.refreshed["scope"]
    refreshed = b.service.refresh(info.reference)
    assert refreshed.scopes == info.scopes == GOOGLE_GMAIL_SEND_SCOPES
    assert refreshed.account_id == info.account_id and refreshed.connection_id == info.connection_id


def test_different_signed_account_cannot_upgrade_old_account_to_send(readonly):
    b = readonly
    before = b.vault._file.read_current()
    b.transport.initial["scope"] = SCOPES
    b.verifier.changes["sub"] = "different-synthetic-account"
    with pytest.raises(CredentialError) as error:
        b.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
    assert error.value.reason is CredentialFailure.IDENTITY_MISMATCH
    assert b.vault._file.read_current() == before
    assert b.vault._data.identity.subject == SUBJECT
    assert b.service.restore().credential == b.info


def test_send_profile_accepts_only_documented_identity_scope_aliases(readonly):
    b = readonly
    b.transport.initial["scope"] = (
        "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile "
        "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send")
    assert b.service.connect(profile=GoogleAccessProfile.GMAIL_SEND).scopes == GOOGLE_GMAIL_SEND_SCOPES
