"""Desktop OAuth callback, protocol, signed identity and disclosure regressions."""

import base64
import hashlib
import json
import os
import socket
import ssl
import threading
import traceback
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest

from agentcore.credentials import CredentialError, CredentialFailure, SecretMaterial
from agentcore.credentials.google_protocol import (
    GOOGLE_CERTIFICATES_ENDPOINT, GOOGLE_IDENTITY_SCOPES, GOOGLE_ISSUER,
    GoogleIdentityVerifier, GoogleOAuthConfig, GoogleOAuthHttp, OAuthTokenResponse,
    validate_google_claims,
)
from agentcore.credentials.oauth_loopback import LoopbackCallback
from agentcore.credentials.protected import ConnectionState
from agentcore.util.clock import FixedClock
from google_oauth_fakes import ACCESS, CLIENT_ID, CLIENT_SECRET, ID_TOKEN, NOW, REFRESH, SUBJECT, bundle


STATE = "s" * 43
CODE = "test-only-oauth-code+/with-symbols"


@pytest.fixture
def callback():
    value = LoopbackCallback(SecretMaterial(STATE), clock=FixedClock(NOW))
    yield value
    value.close()


def callback_target(**changes):
    return "/?" + urlencode({"state": STATE, "code": CODE, **changes})


def test_callback_binds_only_loopback_random_port_and_accepts_exact_state_once(callback):
    address, port = callback._socket.getsockname()
    assert address == "127.0.0.1" and 0 < port < 65536
    assert callback.redirect_uri == f"http://127.0.0.1:{port}/"
    assert callback._accept(callback_target(), f"127.0.0.1:{port}")._read() == CODE
    with pytest.raises(CredentialError) as error:
        callback._accept(callback_target(), f"127.0.0.1:{port}")
    assert error.value.reason is CredentialFailure.OAUTH_REPLAY


@pytest.mark.parametrize("target, host, reason", [
    ("/?code=synthetic", None, CredentialFailure.OAUTH_STATE),
    (callback_target(state="wrong"), None, CredentialFailure.OAUTH_STATE),
    (callback_target() + "&state=" + STATE, None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target() + "&code=duplicate", None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target() + "#fragment", None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target().replace("/?", "/wrong?"), None, CredentialFailure.OAUTH_CALLBACK),
    ("http://127.0.0.1/" + callback_target()[1:], None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target(), "localhost:1234", CredentialFailure.OAUTH_CALLBACK),
    (callback_target(), "127.0.0.1:1", CredentialFailure.OAUTH_CALLBACK),
    (callback_target(), "0.0.0.0:1", CredentialFailure.OAUTH_CALLBACK),
    (callback_target() + "&error=access_denied", None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target() + "&iss=https%3A%2F%2Fother.test", None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target() + "&unknown=value", None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target(code=""), None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target(code="\r\nunsafe"), None, CredentialFailure.OAUTH_CALLBACK),
    (callback_target() + "&scope=%ZZ", None, CredentialFailure.OAUTH_CALLBACK),
])
def test_invalid_callback_is_rejected_without_consuming_valid_state(callback, target, host, reason):
    with pytest.raises(CredentialError) as error:
        callback._accept(target, host or urlsplit(callback.redirect_uri).netloc)
    assert error.value.reason is reason
    assert not callback._used


def test_valid_denial_consumes_callback_without_provider_error_text(callback):
    with pytest.raises(CredentialError) as error:
        callback._accept("/?" + urlencode({"state": STATE, "error": "access_denied", "error_description": REFRESH}),
                         urlsplit(callback.redirect_uri).netloc)
    assert error.value.reason is CredentialFailure.OAUTH_DENIED
    assert REFRESH not in str(error.value) and callback._used


def test_callback_expiry_and_fork_fences_precede_sockets_and_locks(callback, monkeypatch):
    callback._clock.advance(181)
    with pytest.raises(CredentialError) as error:
        callback.receive()
    assert error.value.reason is CredentialFailure.OAUTH_TIMEOUT
    class Poison:
        def __enter__(self):
            pytest.fail("inherited lock acquired")
    original = callback._lock
    callback._lock = Poison()
    pid = callback._owner_pid
    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: pid + 1)
        for action in (callback.receive, callback.close):
            with pytest.raises(CredentialError) as error:
                action()
            assert error.value.reason is CredentialFailure.OWNERSHIP
    callback._lock = original


def test_real_loopback_http_callback_has_no_code_or_query_in_reply_or_logs(callback, caplog):
    result = []
    host = urlsplit(callback.redirect_uri).netloc
    request = f"GET {callback_target()} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
    def send():
        with socket.create_connection(callback._socket.getsockname(), timeout=3) as client:
            client.sendall(request)
            result.append(client.recv(4096))
    client = threading.Thread(target=send, daemon=True)
    client.start()
    assert callback.receive()._read() == CODE
    client.join(3)
    assert not client.is_alive() and result
    reply = result[0].decode()
    assert "200 OK" in reply and "Return to AgentCore" in reply
    assert CODE not in reply + caplog.text and STATE not in reply + caplog.text
    assert "connected" not in reply.lower()
    with pytest.raises(CredentialError):
        callback.receive()


@pytest.mark.parametrize("wire", [
    b"POST / HTTP/1.1\r\nHost: example\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: one\r\nHost: two\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: one\r\nContent-Length: 0\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: one\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: one\r\n folded: invalid\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: one\r\n\r\nbody",
    b"GET / HTTP/1.1\r\nHost: one\r\nX-Long: " + b"x" * 16_400 + b"\r\n\r\n",
])
def test_strict_http_callback_parsing(callback, wire):
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(wire)
        with pytest.raises(CredentialError):
            callback._request(receiver)
    finally:
        sender.close()
        receiver.close()


def test_initial_connection_uses_fixed_identity_scopes_offline_pkce_and_verified_subject(tmp_path):
    value = bundle(tmp_path / "credentials")
    try:
        info = value.service.connect()
        parameters = value.browser.parameters
        assert set(parameters["scope"].split()) == GOOGLE_IDENTITY_SCOPES
        assert parameters["response_type"] == "code" and parameters["access_type"] == "offline"
        assert parameters["prompt"] == "consent" and parameters["code_challenge_method"] == "S256"
        assert "client_secret" not in parameters
        assert len(parameters["state"]) >= 43 and len(parameters["nonce"]) >= 43
        _, _, verifier, redirect = value.transport.exchanges[0]
        assert 43 <= len(verifier) <= 128
        assert parameters["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert redirect == parameters["redirect_uri"]
        assert value.verifier.calls == 1 and info.provider == "google"
        assert value.vault._data.identity.subject == SUBJECT
    finally:
        value.vault.close()


@pytest.mark.parametrize("changes", [
    {"iss": "https://attacker.test"}, {"iss": None}, {"aud": "other-client"},
    {"aud": [CLIENT_ID, "other-client"]}, {"azp": "other-client"},
    {"sub": ""}, {"sub": "bad subject"}, {"sub": "x" * 256}, {"sub": "nonascii-é"},
    {"exp": int(NOW.timestamp())}, {"exp": True}, {"exp": 1.0},
    {"iat": int(NOW.timestamp()) + 60}, {"iat": None},
    {"nbf": int(NOW.timestamp()) + 1}, {"auth_time": int(NOW.timestamp()) + 1},
    {"nonce": None}, {"nonce": "wrong"}, {"at_hash": "wrong"},
])
def test_unverified_or_invalid_identity_never_becomes_connection(tmp_path, changes):
    value = bundle(tmp_path / "credentials")
    value.verifier.changes = changes
    try:
        with pytest.raises(CredentialError) as error:
            value.service.connect()
        assert error.value.reason is CredentialFailure.IDENTITY_TOKEN
        assert value.service.restore().state is ConnectionState.DISCONNECTED
        assert value.vault._data.identity is None and value.vault._data.refresh is None
    finally:
        value.vault.close()


def test_google_documented_issuer_alias_is_verified_then_canonicalized(tmp_path):
    value = bundle(tmp_path / "credentials")
    value.verifier.changes["iss"] = "accounts.google.com"
    try:
        value.service.connect()
        assert value.vault._data.identity.issuer == GOOGLE_ISSUER
    finally:
        value.vault.close()


@pytest.mark.parametrize("field, value", [
    ("scope", "openid https://www.googleapis.com/auth/gmail.readonly"),
    ("scope", "email profile"), ("scope", "openid openid"),
    ("scope", "openid  email"), ("expires_in", True), ("expires_in", -1),
    ("expires_in", 86401), ("token_type", "MAC"), ("access_token", "contains space"),
])
def test_invalid_or_excessive_token_response_fails_closed(tmp_path, field, value):
    item = bundle(tmp_path / "credentials")
    item.transport.initial[field] = value
    try:
        with pytest.raises(CredentialError):
            item.service.connect()
        assert item.service.restore().state is ConnectionState.DISCONNECTED
    finally:
        item.vault.close()


def test_google_identity_scope_aliases_are_normalized(tmp_path):
    value = bundle(tmp_path / "credentials")
    value.transport.initial["scope"] = "openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile"
    try:
        assert value.service.connect().scopes == GOOGLE_IDENTITY_SCOPES
    finally:
        value.vault.close()


@pytest.mark.parametrize("field", ["id_token", "refresh_token", "scope"])
def test_initial_connection_requires_proof_durable_refresh_and_explicit_scopes(tmp_path, field):
    value = bundle(tmp_path / "credentials")
    del value.transport.initial[field]
    try:
        with pytest.raises(CredentialError):
            value.service.connect()
        assert value.service.restore().state is ConnectionState.DISCONNECTED
    finally:
        value.vault.close()


def test_authorization_exchange_timeout_is_not_connected_and_is_never_retried(tmp_path, caplog):
    value = bundle(tmp_path / "credentials")
    def fail():
        error = TimeoutError(REFRESH)
        error.add_note(ACCESS)
        raise error
    value.transport.on_exchange = fail
    try:
        with pytest.raises(CredentialError) as error:
            value.service.connect()
        assert error.value.reason is CredentialFailure.OAUTH_UNCERTAIN
        assert error.value.__context__ is None
        assert REFRESH not in "".join(traceback.format_exception(error.value)) + caplog.text
        assert len(value.transport.exchanges) == 1 and value.verifier.calls == 0
        assert value.service.restore().state is ConnectionState.DISCONNECTED
    finally:
        value.vault.close()


@pytest.fixture(scope="module")
def rsa_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()),
            private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


def signed_token(rsa_key, claims):
    from google.auth import crypt, jwt
    return jwt.encode(crypt.RSASigner.from_string(rsa_key[0], key_id="test-key"), claims).decode()


@pytest.mark.parametrize("invalid", [None, "issuer", "audience", "expired", "signature", "algorithm", "duplicate_json"])
def test_real_google_auth_signature_and_claim_verification(rsa_key, monkeypatch, invalid):
    from google.auth import _helpers
    monkeypatch.setattr(_helpers, "utcnow", lambda: NOW.replace(tzinfo=None))
    claims = {"iss": GOOGLE_ISSUER, "sub": SUBJECT, "aud": CLIENT_ID,
              "iat": int(NOW.timestamp()) - 10, "exp": int(NOW.timestamp()) + 3600}
    if invalid == "issuer":
        claims["iss"] = "https://wrong.test"
    elif invalid == "audience":
        claims["aud"] = "wrong-client"
    elif invalid == "expired":
        claims["exp"] = int(NOW.timestamp()) - 1
    token = signed_token(rsa_key, claims)
    if invalid == "signature":
        parts = token.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        token = ".".join(parts)
    elif invalid in ("algorithm", "duplicate_json"):
        header = b'{"alg":"none","kid":"test-key"}' if invalid == "algorithm" else b'{"alg":"RS256","alg":"RS256","kid":"test-key"}'
        token = base64.urlsafe_b64encode(header).rstrip(b"=").decode() + "." + token.split(".", 1)[1]
    requests = []
    def certificates(url, method="GET"):
        requests.append(url)
        assert url == GOOGLE_CERTIFICATES_ENDPOINT and method == "GET"
        return SimpleNamespace(status=200, data=json.dumps({"test-key": rsa_key[1].decode()}).encode())
    verifier = GoogleIdentityVerifier(SimpleNamespace(certificates=certificates))
    if invalid:
        with pytest.raises(CredentialError) as error:
            verifier.verify(SecretMaterial(token), CLIENT_ID)
        assert error.value.reason is CredentialFailure.IDENTITY_TOKEN
        assert token not in "".join(traceback.format_exception(error.value))
    else:
        assert verifier.verify(SecretMaterial(token), CLIENT_ID) == claims
        assert requests == [GOOGLE_CERTIFICATES_ENDPOINT]


def test_oidc_access_hash_and_refresh_nonce_binding():
    claims = {"iss": GOOGLE_ISSUER, "sub": SUBJECT, "aud": CLIENT_ID,
              "iat": int(NOW.timestamp()) - 10, "exp": int(NOW.timestamp()) + 3600,
              "nonce": STATE, "auth_time": int(NOW.timestamp()) - 60,
              "at_hash": base64.urlsafe_b64encode(hashlib.sha256(ACCESS.encode()).digest()[:16]).rstrip(b"=").decode()}
    arguments = dict(client_id=CLIENT_ID, nonce=STATE, access=SecretMaterial(ACCESS), now=NOW,
                     initial=False, expected=None, auth_time=claims["auth_time"])
    result = validate_google_claims(claims, **arguments)
    assert result.identity.subject == SUBJECT
    for change in ({"nonce": "another"}, {"nonce": None}, {"auth_time": None},
                   {"auth_time": claims["auth_time"] + 1}, {"at_hash": "wrong"}):
        with pytest.raises(CredentialError):
            validate_google_claims({**claims, **change}, **arguments)


@pytest.mark.parametrize("status, body, expected", [
    (400, {"error": "invalid_grant", "error_description": REFRESH}, CredentialFailure.RECONNECT),
    (302, {"redirect": "https://attacker.test"}, CredentialFailure.OAUTH_UNCERTAIN),
    (500, {"error": REFRESH}, CredentialFailure.OAUTH_UNCERTAIN),
])
def test_pinned_https_transport_rejects_errors_without_bodies_or_redirects(monkeypatch, status, body, expected):
    requests = []
    class Connection:
        def __init__(self, host, *, timeout, context):
            assert host == "oauth2.googleapis.com" and timeout == 10
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        def request(self, method, path, body, headers):
            requests.append((method, path))
            assert method == "POST" and path == "/token"
            assert headers["Content-Type"] == "application/x-www-form-urlencoded"
        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda _: json.dumps(body).encode(),
                                   getheader=lambda *_: "application/json")
        def close(self):
            pass
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    with pytest.raises(CredentialError) as error:
        GoogleOAuthHttp().refresh(GoogleOAuthConfig(CLIENT_ID, client_secret=SecretMaterial(CLIENT_SECRET)), SecretMaterial(REFRESH))
    assert error.value.reason is expected
    assert error.value.__context__ is None
    assert len(requests) == 1 and REFRESH not in "".join(traceback.format_exception(error.value))


def test_http_success_uses_secret_post_fields_not_url_or_query(monkeypatch):
    requests = []
    class Connection:
        def __init__(self, host, **kwargs):
            assert host == "oauth2.googleapis.com"
        def request(self, method, path, body, headers):
            requests.append((path, body.decode()))
        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _: json.dumps({"access_token": ACCESS, "id_token": ID_TOKEN,
                                   "refresh_token": REFRESH, "expires_in": 3600, "token_type": "Bearer"}).encode(),
                                   getheader=lambda *_: "application/json")
        def close(self):
            pass
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    response = GoogleOAuthHttp().exchange(GoogleOAuthConfig(CLIENT_ID, client_secret=SecretMaterial(CLIENT_SECRET)),
                                        SecretMaterial(CODE), SecretMaterial(STATE), "http://127.0.0.1:1234/")
    assert requests[0][0] == "/token"
    assert "grant_type=authorization_code" in requests[0][1] and "client_secret=" in requests[0][1]
    assert response.access._read() == ACCESS and REFRESH not in repr(response)


def test_token_json_duplicate_keys_rejected():
    with pytest.raises(CredentialError):
        OAuthTokenResponse.parse(b'{"access_token":"first","access_token":"second","expires_in":3600,"token_type":"Bearer"}')
