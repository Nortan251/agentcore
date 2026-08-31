"""Pinned Google OAuth transport and Google-maintained ID-token verification.

There is no provider discovery, ambient credential lookup, tokeninfo request,
HTTP retry, redirect, debug logging, or model-selected configuration here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import os
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import urlencode

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import AccountIdentity, RuntimeOnly, SecretMaterial, require_time
from agentcore.credentials.protected import strict_json
from agentcore.credentials.runtime import _metadata_call


GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CERTIFICATES_ENDPOINT = "https://www.googleapis.com/oauth2/v1/certs"
GOOGLE_IDENTITY_SCOPES = frozenset({"openid", "email", "profile"})
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_GMAIL_READONLY_SCOPES = GOOGLE_IDENTITY_SCOPES | {GMAIL_READONLY_SCOPE}
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_GMAIL_SEND_SCOPES = GOOGLE_GMAIL_READONLY_SCOPES | {GMAIL_SEND_SCOPE}
_SCOPE_ALIASES = {"openid": "openid", "email": "email", "profile": "profile",
                  "https://www.googleapis.com/auth/userinfo.email": "email",
                  "https://www.googleapis.com/auth/userinfo.profile": "profile",
                  GMAIL_READONLY_SCOPE: GMAIL_READONLY_SCOPE,
                  GMAIL_SEND_SCOPE: GMAIL_SEND_SCOPE}


class GoogleAccessProfile(Enum):
    """Reviewed host consent choices, never model-selected scope strings."""

    IDENTITY = "identity"
    GMAIL_READONLY = "gmail_readonly"
    GMAIL_SEND = "gmail_send"

    @property
    def scopes(self) -> frozenset[str]:
        return {
            GoogleAccessProfile.IDENTITY: GOOGLE_IDENTITY_SCOPES,
            GoogleAccessProfile.GMAIL_READONLY: GOOGLE_GMAIL_READONLY_SCOPES,
            GoogleAccessProfile.GMAIL_SEND: GOOGLE_GMAIL_SEND_SCOPES,
        }[self]


class GoogleOAuthConfig(RuntimeOnly):
    """Host-only installed-app identity; endpoints and scopes are not options."""

    __slots__ = ("_owner_pid", "_client_id", "_secret", "_callback_timeout")

    def __init__(self, client_id: str, *, client_secret: SecretMaterial | None = None,
                 callback_timeout: float = 180) -> None:
        if type(client_id) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,1024}", client_id):
            raise CredentialError(CredentialFailure.INVALID)
        if type(callback_timeout) not in (int, float) or not 1 <= callback_timeout <= 600:
            raise CredentialError(CredentialFailure.INVALID)
        if client_secret is not None and type(client_secret) is not SecretMaterial:
            raise CredentialError(CredentialFailure.INVALID)
        self._owner_pid = os.getpid()
        self._client_id = client_id
        self._secret = SecretMaterial(client_secret._read()) if client_secret is not None else None
        self._callback_timeout = callback_timeout

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @property
    def client_id(self) -> str:
        self._owner()
        return self._client_id

    @property
    def callback_timeout(self) -> float:
        self._owner()
        return self._callback_timeout

    def _client_fields(self) -> dict[str, str]:
        self._owner()
        fields = {"client_id": self._client_id}
        if self._secret is not None:
            fields["client_secret"] = self._secret._read()
        return fields


def _token(value: Any) -> SecretMaterial:
    if type(value) is not str or not value.isascii() or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in value):
        raise CredentialError(CredentialFailure.INVALID)
    return SecretMaterial(value)


class OAuthTokenResponse(RuntimeOnly):
    """Unverified provider response. No repr/serialization or public raw body."""

    @classmethod
    def parse(cls, body: bytes) -> OAuthTokenResponse:
        return _metadata_call(lambda: cls._parse(body))

    @classmethod
    def _parse(cls, body: bytes) -> OAuthTokenResponse:
        if len(body) > 65_536:
            raise CredentialError(CredentialFailure.INVALID)
        values = strict_json(body)
        allowed = {"access_token", "refresh_token", "id_token", "expires_in", "token_type", "scope", "refresh_token_expires_in"}
        if not set(values) <= allowed or not {"access_token", "expires_in", "token_type"} <= set(values):
            raise CredentialError(CredentialFailure.INVALID)
        if values["token_type"] != "Bearer" or type(values["expires_in"]) is not int or not 1 <= values["expires_in"] <= 86400:
            raise CredentialError(CredentialFailure.INVALID)
        result = cls()
        result.access = _token(values["access_token"])
        result.refresh = _token(values["refresh_token"]) if "refresh_token" in values else None
        result.id_token = _token(values["id_token"]) if "id_token" in values else None
        result.expires_in = values["expires_in"]
        result.refresh_expires_in = values.get("refresh_token_expires_in")
        if "refresh_token_expires_in" in values and (type(result.refresh_expires_in) is not int or not 1 <= result.refresh_expires_in <= 315_360_000):
            raise CredentialError(CredentialFailure.INVALID)
        scope = values.get("scope")
        result.scopes: frozenset[str] | None = None
        if "scope" in values:
            if type(scope) is not str or not 1 <= len(scope) <= 4096:
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            parts = scope.split(" ")
            if any(part not in _SCOPE_ALIASES for part in parts):
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            normalized = [_SCOPE_ALIASES[part] for part in parts]
            if len(set(normalized)) != len(normalized):
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            result.scopes = frozenset(normalized)
        return result

    def discard(self) -> None:
        for value in (self.access, self.refresh, self.id_token):
            if value is not None:
                value._discard()


class GoogleTokenTransport(Protocol):
    def exchange(self, config: GoogleOAuthConfig, code: SecretMaterial,
                 verifier: SecretMaterial, redirect_uri: str) -> OAuthTokenResponse: ...

    def refresh(self, config: GoogleOAuthConfig, refresh: SecretMaterial) -> OAuthTokenResponse: ...


def _oauth_tls_context() -> ssl.SSLContext:
    # create_default_context reads SSLKEYLOGFILE and can write session keys.
    # Build its verified-client equivalent without that environment hook or
    # any process-wide environment mutation. Keep modern X.509 verification.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN | ssl.VERIFY_X509_STRICT
    context.load_default_certs(ssl.Purpose.SERVER_AUTH)
    context.keylog_filename = None
    return context


class GoogleOAuthHttp(RuntimeOnly):
    """Pinned HTTPS endpoints, default TLS verification, bounded bodies.

    Socket timeout is per I/O (10 seconds), not a hard preemptive deadline.
    No proxy/environment discovery, redirects, automatic retries, or logs.
    """

    def __init__(self) -> None:
        self._owner_pid = os.getpid()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def _request(self, host: str, path: str, fields: dict[str, str] | None) -> bytes:
        self._owner()
        failure = CredentialFailure.OAUTH_UNCERTAIN
        result = None
        try:
            connection = http.client.HTTPSConnection(host, timeout=10, context=_oauth_tls_context())
            # Override a process-wide HTTPConnection.debuglevel setting too.
            connection.debuglevel = 0
            try:
                headers = {"Accept": "application/json"}
                body = None
                if fields is not None:
                    body = urlencode(fields).encode("ascii")
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                connection.request("POST" if fields is not None else "GET", path, body=body, headers=headers)
                response = connection.getresponse()
                maximum = 262_144 if fields is None else 65_536
                raw = response.read(maximum + 1)
                if len(raw) > maximum or response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    failure = CredentialFailure.INVALID
                elif response.status == 200:
                    result = raw
                elif response.status in (400, 401):
                    parsed = strict_json(raw)
                    failure = (CredentialFailure.RECONNECT if parsed.get("error") == "invalid_grant"
                               else CredentialFailure.OAUTH_DENIED)
            finally:
                self._owner()
                connection.close()
        except Exception:
            pass
        self._owner()
        if result is None:
            raise CredentialError(failure)
        return result

    def exchange(self, config: GoogleOAuthConfig, code: SecretMaterial,
                 verifier: SecretMaterial, redirect_uri: str) -> OAuthTokenResponse:
        self._owner()
        fields = config._client_fields()
        fields.update(grant_type="authorization_code", code=code._read(),
                      code_verifier=verifier._read(), redirect_uri=redirect_uri)
        return _metadata_call(lambda: OAuthTokenResponse.parse(self._request("oauth2.googleapis.com", "/token", fields)))

    def refresh(self, config: GoogleOAuthConfig, refresh: SecretMaterial) -> OAuthTokenResponse:
        self._owner()
        fields = config._client_fields()
        fields.update(grant_type="refresh_token", refresh_token=refresh._read())
        return _metadata_call(lambda: OAuthTokenResponse.parse(self._request("oauth2.googleapis.com", "/token", fields)))

    def certificates(self, url: str, method: str = "GET", **kwargs):
        self._owner()
        if url != GOOGLE_CERTIFICATES_ENDPOINT or method != "GET" or kwargs:
            raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
        data = _metadata_call(lambda: self._request("www.googleapis.com", "/oauth2/v1/certs", None))
        certificates = strict_json(data)
        if not 1 <= len(certificates) <= 32 or "keys" in certificates or any(
            type(key) is not str or not 1 <= len(key) <= 256 or type(value) is not str or not 1 <= len(value) <= 16_384
            for key, value in certificates.items()
        ):
            raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
        return SimpleNamespace(status=200, data=data, headers={})


class IdentityTokenVerifier(Protocol):
    def verify(self, token: SecretMaterial, client_id: str) -> dict[str, Any]: ...


class GoogleIdentityVerifier(RuntimeOnly):
    """Google's google-auth verifies RSA signatures, issuer, audience and time."""

    def __init__(self, transport: GoogleOAuthHttp | None = None) -> None:
        self._owner_pid = os.getpid()
        self._transport = transport or GoogleOAuthHttp()

    def verify(self, token: SecretMaterial, client_id: str) -> dict[str, Any]:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        result = None
        try:
            from google.oauth2 import id_token

            raw = token._read()
            parts = raw.split(".")
            if len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
                raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
            # Strict syntax is separate from crypto. Reject duplicate JSON keys
            # and unsupported JOSE headers before entering the audited verifier.
            header, payload = [strict_json(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))) for part in parts[:2]]
            if not set(header) <= {"alg", "kid", "typ"} or header.get("alg") != "RS256" or (
                type(header.get("kid")) is not str or not 1 <= len(header["kid"]) <= 256
            ) or header.get("typ", "JWT") != "JWT":
                raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
            verified = id_token.verify_oauth2_token(raw, self._transport.certificates, audience=client_id,
                                                   clock_skew_in_seconds=0)
            if type(verified) is dict and verified == payload:
                result = verified
        except Exception:
            pass
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if result is None:
            raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
        return result


@dataclass(frozen=True, slots=True)
class VerifiedGoogleIdentity:
    identity: AccountIdentity
    expires_at: datetime
    auth_time: int | None


def validate_google_claims(claims: dict[str, Any], *, client_id: str, nonce: str,
                           access: SecretMaterial, now: datetime, initial: bool,
                           expected: AccountIdentity | None, auth_time: int | None) -> VerifiedGoogleIdentity:
    """Validate relevant OIDC claims even when a host injects a verifier."""
    require_time(now)
    if type(claims) is not dict or claims.get("iss") not in (GOOGLE_ISSUER, "accounts.google.com"):
        raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    if claims.get("aud") != client_id or claims.get("azp", client_id) != client_id:
        raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    subject = claims.get("sub")
    if type(subject) is not str or not 1 <= len(subject) <= 255 or not subject.isascii() or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in subject):
        raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    identity = AccountIdentity("google", GOOGLE_ISSUER, subject)
    if expected is not None and identity != expected:
        raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
    timestamp = now.timestamp()
    issued, expires = claims.get("iat"), claims.get("exp")
    if type(issued) is not int or type(expires) is not int or not 0 <= issued <= timestamp < expires <= 253402300799:
        raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > timestamp):
        raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    received_nonce = claims.get("nonce")
    if initial or "nonce" in claims:
        if type(received_nonce) is not str or not received_nonce.isascii() or not hmac.compare_digest(received_nonce, nonce):
            raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    authenticated = claims.get("auth_time")
    if "auth_time" in claims and (type(authenticated) is not int or not 0 <= authenticated <= timestamp or (
        not initial and auth_time is not None and authenticated != auth_time
    )):
        raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    if "at_hash" in claims:
        received_hash = claims["at_hash"]
        expected_hash = base64.urlsafe_b64encode(hashlib.sha256(access._read().encode("ascii")).digest()[:16]).rstrip(b"=").decode()
        if type(received_hash) is not str or not received_hash.isascii() or not hmac.compare_digest(received_hash, expected_hash):
            raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
    return VerifiedGoogleIdentity(identity, datetime.fromtimestamp(expires, timezone.utc),
                                  authenticated if initial else auth_time)
