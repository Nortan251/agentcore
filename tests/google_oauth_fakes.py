"""Synthetic OAuth fixtures. No Google account, credentials, or network."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet

from agentcore.credentials import (
    GoogleConnectionService, GoogleOAuthConfig, ProtectedCredentialVault, SecretMaterial,
)
from agentcore.credentials.google_protocol import GOOGLE_ISSUER, OAuthTokenResponse
from agentcore.credentials.models import RuntimeOnly
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 30, 9, tzinfo=timezone.utc)
CLIENT_ID = "test-only-desktop.apps.googleusercontent.com"
ACCESS = "test-only-access-material-10+/=never-public"
REFRESH = "test-only-refresh-material-10+/=never-public"
ID_TOKEN = "test-only-id-token-material-10-never-public"
CLIENT_SECRET = "test-only-installed-client-secret-never-public"
SUBJECT = "google-stable-test-subject-111"


class TestProtector(RuntimeOnly):
    __test__ = False

    def __init__(self, key=None):
        self.key = key or Fernet.generate_key()
        self.cipher = Fernet(self.key)

    def protect(self, plaintext, *, purpose):
        return b"TEST-ONLY\0" + self.cipher.encrypt(purpose + plaintext)

    def unprotect(self, ciphertext, *, purpose):
        assert ciphertext.startswith(b"TEST-ONLY\0")
        value = self.cipher.decrypt(ciphertext[len(b"TEST-ONLY\0"):])
        assert value.startswith(purpose)
        return value[len(purpose):]


class FakeCallback(RuntimeOnly):
    redirect_uri = "http://127.0.0.1:52731/"

    def __init__(self, state, *, timeout, clock):
        self.closed = False

    def receive(self):
        return SecretMaterial("test-only-authorization-code")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.parameters = {}

    def __call__(self, url):
        self.parameters = {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}
        return True


class FakeVerifier:
    def __init__(self, browser, clock):
        self.browser, self.clock = browser, clock
        self.changes = {}
        self.error = None
        self.calls = 0

    def verify(self, token, client_id):
        self.calls += 1
        assert token._read() == ID_TOKEN and client_id == CLIENT_ID
        if self.error is not None:
            raise self.error
        result = {"iss": GOOGLE_ISSUER, "sub": SUBJECT, "aud": CLIENT_ID,
                  "iat": int(self.clock.now().timestamp()) - 10,
                  "exp": int(self.clock.now().timestamp()) + 3600}
        if "nonce" in self.browser.parameters:
            result["nonce"] = self.browser.parameters["nonce"]
        result.update(self.changes)
        return result


class FakeTransport:
    def __init__(self):
        self.initial = {"access_token": ACCESS, "refresh_token": REFRESH, "id_token": ID_TOKEN,
                        "token_type": "Bearer", "expires_in": 3600,
                        "scope": "openid email profile"}
        self.refreshed = {**self.initial, "access_token": "replacement-" + ACCESS}
        del self.refreshed["refresh_token"]
        self.exchanges, self.refreshes = [], []
        self.on_exchange = None
        self.on_refresh = None

    def exchange(self, config, code, verifier, redirect_uri):
        self.exchanges.append((config.client_id, code._read(), verifier._read(), redirect_uri))
        if self.on_exchange:
            self.on_exchange()
        return OAuthTokenResponse.parse(json.dumps(self.initial).encode())

    def refresh(self, config, refresh):
        self.refreshes.append(refresh._read())
        if self.on_refresh:
            self.on_refresh()
        return OAuthTokenResponse.parse(json.dumps(self.refreshed).encode())


def bundle(directory, *, protector=None, clock=None, production=False):
    clock = clock or FixedClock(NOW)
    protector = None if production else protector or TestProtector()
    vault = ProtectedCredentialVault(directory, clock=clock, **(
        {} if production else {"protector": protector, "test_only": True}))
    browser, transport = FakeBrowser(), FakeTransport()
    verifier = FakeVerifier(browser, clock)
    config = GoogleOAuthConfig(CLIENT_ID, client_secret=SecretMaterial(CLIENT_SECRET))
    service = GoogleConnectionService(vault, config, transport=transport, identity_verifier=verifier,
                                      browser=browser, callback_factory=FakeCallback)
    return SimpleNamespace(vault=vault, browser=browser, transport=transport, verifier=verifier,
                           service=service, config=config, protector=protector, clock=clock)
