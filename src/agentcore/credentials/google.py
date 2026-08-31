"""Trusted desktop connection lifecycle, entirely below model/tool authority.

OAuth is a host setup operation, not a tool and not a journaled provider
mutation. There are no Gmail calls or remote-revocation calls in this service.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import webbrowser
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlencode

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import (
    GOOGLE_AUTHORIZATION_ENDPOINT, GOOGLE_IDENTITY_SCOPES, GOOGLE_GMAIL_READONLY_SCOPES,
    GMAIL_READONLY_SCOPE, GoogleAccessProfile, GoogleIdentityVerifier,
    GoogleOAuthConfig, GoogleOAuthHttp, GoogleTokenTransport, IdentityTokenVerifier,
    OAuthTokenResponse, validate_google_claims,
)
from agentcore.credentials.models import CredentialInfo, RuntimeOnly, SecretMaterial
from agentcore.credentials.oauth_loopback import LoopbackCallback
from agentcore.credentials.protected import ConnectionState, ConnectionStatus, ProtectedCredentialVault, _Operation
from agentcore.credentials.runtime import _interruption_type, _metadata_call


def _system_browser(url: str) -> bool:
    return webbrowser.open(url, new=1, autoraise=True)


class GoogleConnectionService(RuntimeOnly):
    """Explicit host composition; a restored reference cannot construct this.

    Call ensure_current/refresh in host orchestration before binding a tool to
    the returned generation. An invocation that captured an older generation
    is never silently rebound. Test doubles are trusted constructor inputs.
    """

    def __init__(self, vault: ProtectedCredentialVault, config: GoogleOAuthConfig, *,
                 transport: GoogleTokenTransport | None = None,
                 identity_verifier: IdentityTokenVerifier | None = None,
                 browser: Callable[[str], bool] = _system_browser,
                 callback_factory: Callable[..., LoopbackCallback] = LoopbackCallback) -> None:
        self._owner_pid = os.getpid()
        if type(config) is not GoogleOAuthConfig or type(vault) is not ProtectedCredentialVault:
            raise CredentialError(CredentialFailure.INVALID)
        self._vault, self._config = vault, config
        self._transport = transport or GoogleOAuthHttp()
        self._verifier = identity_verifier or GoogleIdentityVerifier()
        self._browser, self._callback_factory = browser, callback_factory
        self._authority = object()
        vault._bind_manager(self._authority, config.client_id)

    def _process_owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def _owner(self) -> None:
        self._process_owner()
        self._vault._owner()

    def restore(self) -> ConnectionStatus:
        """Read verified local metadata. Reopen never restores access authority."""
        self._owner()
        return _metadata_call(self._vault.status)

    def connect(self, *, profile: GoogleAccessProfile = GoogleAccessProfile.IDENTITY) -> CredentialInfo:
        """Host-only fresh consent for one reviewed profile.

        Installed apps cannot incrementally authorize. Upgrading to Gmail is
        a new full consent/reconnection, preserving the vault's exact account
        identity and durable publication order. A new connection needs new
        concrete tool bindings; this method never registers or invokes tools.
        """
        self._owner()
        if type(profile) is not GoogleAccessProfile:
            raise CredentialError(CredentialFailure.INVALID)
        return _metadata_call(lambda: self._connect(profile))

    def _connect(self, profile: GoogleAccessProfile) -> CredentialInfo:
        operation = self._vault._begin("authorize", None, authority=self._authority)
        return self._run(operation, lambda: self._authorize(operation, profile))

    def _authorize(self, operation: _Operation, profile: GoogleAccessProfile) -> CredentialInfo:
        state, verifier, nonce = [SecretMaterial(secrets.token_urlsafe(length)) for length in (32, 64, 32)]
        code = None
        try:
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier._read().encode("ascii")).digest()).rstrip(b"=").decode()
            self._owner()
            with self._callback_factory(state, timeout=self._config.callback_timeout, clock=self._vault._clock) as callback:
                # The redirect comes only from our bound listener. The factory
                # is a trusted host/test seam, never a model argument.
                redirect = callback.redirect_uri
                parameters = {
                    "client_id": self._config.client_id, "redirect_uri": redirect,
                    "response_type": "code", "scope": " ".join(sorted(profile.scopes)),
                    "code_challenge": challenge, "code_challenge_method": "S256",
                    "state": state._read(), "nonce": nonce._read(),
                    "access_type": "offline", "prompt": "consent",
                }
                if self._browser(GOOGLE_AUTHORIZATION_ENDPOINT + "?" + urlencode(parameters)) is not True:
                    raise CredentialError(CredentialFailure.OAUTH_DENIED)
                code = callback.receive()
                self._owner()
                self._vault._check_operation(operation, authority=self._authority)
                started, mono = self._vault._time()
                response = self._transport.exchange(self._config, code, verifier, redirect)
            return self._accept_tokens(operation, response, started, mono, nonce._read(), profile=profile)
        finally:
            self._process_owner()
            for material in (state, verifier, nonce, code):
                if material is not None:
                    material._discard()

    def refresh(self, reference: str) -> CredentialInfo:
        """Single flight, fail-busy; no retries or metadata mutex across HTTP."""
        self._owner()

        def perform() -> CredentialInfo:
            operation = self._vault._begin("refresh", reference, authority=self._authority)

            def exchange() -> CredentialInfo:
                self._vault._check_operation(operation, authority=self._authority)
                started, mono = self._vault._time()
                assert operation.refresh is not None and operation.nonce is not None
                response = self._transport.refresh(self._config, operation.refresh)
                return self._accept_tokens(operation, response, started, mono, operation.nonce)

            return self._run(operation, exchange)

        return _metadata_call(perform)

    def ensure_current(self, reference: str) -> CredentialInfo:
        """Host expiry-triggered refresh. Tool code must never call this."""
        self._owner()
        status = self.restore()
        if status.credential is None or status.credential.reference != reference:
            raise CredentialError(CredentialFailure.STALE)
        if status.state is ConnectionState.CURRENT:
            return status.credential
        if status.state is ConnectionState.PENDING:
            raise CredentialError(CredentialFailure.BUSY)
        if status.state is ConnectionState.DISCONNECTED:
            raise CredentialError(CredentialFailure.REVOKED)
        if not status.can_refresh:
            raise CredentialError(CredentialFailure.RECONNECT)
        return self.refresh(reference)

    def _accept_tokens(self, operation: _Operation, response: OAuthTokenResponse, started,
                       mono: float, nonce: str, *, profile: GoogleAccessProfile | None = None) -> CredentialInfo:
        self._owner()
        if type(response) is not OAuthTokenResponse:
            raise CredentialError(CredentialFailure.INVALID)
        try:
            self._vault._check_operation(operation, authority=self._authority)
            if response.id_token is None:
                raise CredentialError(CredentialFailure.IDENTITY_TOKEN)
            initial = operation.kind == "authorize"
            # RFC 6749 permits omitted scope only when unchanged; Google uses
            # that on refresh. Initial grants must enumerate their scopes.
            scopes = response.scopes
            if scopes is None and not initial and operation.expected is not None:
                scopes = operation.expected.scopes
            # Profile is captured by this host operation, not mutable service
            # state. On refresh the protected current grant is the ceiling;
            # neither a response nor old identity-only metadata can widen it.
            gmail = (profile is GoogleAccessProfile.GMAIL_READONLY if initial else
                     operation.expected is not None and GMAIL_READONLY_SCOPE in operation.expected.scopes)
            allowed = GOOGLE_GMAIL_READONLY_SCOPES if gmail else GOOGLE_IDENTITY_SCOPES
            if scopes is None or "openid" not in scopes or not scopes <= allowed or (
                not initial and operation.expected is not None and not scopes <= operation.expected.scopes
            ) or (gmail and scopes != GOOGLE_GMAIL_READONLY_SCOPES):
                raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
            if initial and response.refresh is None:
                raise CredentialError(CredentialFailure.RECONNECT)
            claims = self._verifier.verify(response.id_token, self._config.client_id)
            self._owner()
            now, current_mono = self._vault._time()
            identity = validate_google_claims(
                claims, client_id=self._config.client_id, nonce=nonce, access=response.access,
                now=now, initial=initial, expected=operation.identity, auth_time=operation.auth_time)
            expires_at = min(started + timedelta(seconds=response.expires_in), identity.expires_at)
            deadline = min(mono + response.expires_in, current_mono + (identity.expires_at - now).total_seconds())
            refresh_expires = operation.refresh_expires
            if initial:
                refresh_expires = None
            if response.refresh_expires_in is not None:
                refresh_expires = started + timedelta(seconds=response.refresh_expires_in)
            # If a replacement omits expiry, retaining the previous bound is
            # conservative. The service never invents a longer refresh life.
            return self._vault._publish(
                operation, identity=identity.identity, access=response.access, refresh=response.refresh,
                scopes=scopes, expires_at=expires_at, deadline=deadline, refresh_expires=refresh_expires,
                nonce=nonce, auth_time=identity.auth_time, authority=self._authority)
        finally:
            self._process_owner()
            response.discard()

    def _run(self, operation: _Operation, action: Callable[[], CredentialInfo]) -> CredentialInfo:
        result = None
        failure = None
        interruption = None
        try:
            result = action()
        except CredentialError as exc:
            failure = exc.reason
        except BaseException as exc:
            failure = CredentialFailure.OAUTH_UNCERTAIN
            interruption = _interruption_type(exc)
        self._process_owner()
        # Identity/scope errors and transport ambiguity do not revoke the old
        # access token. They disable unattended refresh pending fresh consent.
        reconnect = failure in {
            CredentialFailure.RECONNECT, CredentialFailure.OAUTH_UNCERTAIN,
            CredentialFailure.IDENTITY_TOKEN, CredentialFailure.IDENTITY_MISMATCH,
            CredentialFailure.SCOPE_MISMATCH, CredentialFailure.COMMIT_FAILED,
        }
        cleanup_failure = None
        try:
            self._vault._abort(operation, authority=self._authority, reconnect=reconnect)
        except CredentialError as exc:
            cleanup_failure = exc.reason
        # Close/corruption can prevent vault cleanup, but same-process private
        # operation material must still be discarded without changing storage.
        operation.discard()
        if interruption is not None:
            raise interruption()
        if failure is not None or cleanup_failure is not None:
            raise CredentialError(failure or cleanup_failure)
        if result is None:
            raise CredentialError(CredentialFailure.UNAVAILABLE)
        return result

    def disconnect(self, reference: str | None) -> ConnectionStatus:
        """Local deletion/invalidation only. This never revokes Google consent."""
        self._owner()
        return _metadata_call(lambda: self._vault._disconnect(reference, authority=self._authority))
