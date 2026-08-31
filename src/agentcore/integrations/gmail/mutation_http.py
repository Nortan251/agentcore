"""Fixed Gmail primary-profile, direct-send and private reconciliation HTTPS.

No registration, consent, refresh, redirect, proxy discovery or retry lives here.
Only the host can install this transport beneath the journaled send foundation.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import threading
from contextlib import contextmanager
from enum import Enum
from typing import Any
from urllib.parse import quote, urlencode

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES, _oauth_tls_context
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.runtime import _interruption_type
from agentcore.credentials.vault import CredentialLease
from agentcore.integrations.gmail.http import decode_json
from agentcore.integrations.gmail.send import (
    MAX_PROBE_CANDIDATES, GmailProbeRequest, GmailProbeResponse, GmailSendSubmission, _provider_id,
)
from agentcore.integrations.gmail.send_intent import MAX_MIME_BYTES, normalize_address
from agentcore.integrations.gmail.send_match import candidate_from_message


_HOST = "gmail.googleapis.com"
_MESSAGES = "/gmail/v1/users/me/messages"
MAX_PROFILE_BYTES = 4096
MAX_SEND_RESPONSE_BYTES = 16_384
MAX_SEARCH_BYTES = 8192
MAX_CANDIDATE_BYTES = 262_144
MAX_HTTP_HEADER_BYTES = 16_384
MAX_HTTP_HEADER_LINE = 4096
MAX_HTTP_HEADER_LINES = 66  # includes status and terminator
_JSON_TYPE = re.compile(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?', re.IGNORECASE)
_CANDIDATE_FIELDS = (
    "id,threadId,labelIds,payload(mimeType,filename,headers,"
    "body(size,data,attachmentId),parts(partId))"
)


class _Operation(Enum):
    PROFILE = "profile"
    SEND = "send"
    SEARCH = "search"
    CANDIDATE = "candidate"


class _HeaderReader:
    """Bound status/interim headers and trailers before http.client parses them."""

    def __init__(self, stream) -> None:
        self.stream, self.remaining, self.lines = stream, MAX_HTTP_HEADER_BYTES, 0

    def readline(self, limit=-1):
        maximum = min(MAX_HTTP_HEADER_LINE + 1, self.remaining + 1)
        if limit >= 0:
            maximum = min(maximum, limit)
        line = self.stream.readline(maximum)
        self.remaining -= len(line)
        self.lines += 1
        if self.remaining < 0 or len(line) > MAX_HTTP_HEADER_LINE or self.lines > MAX_HTTP_HEADER_LINES or (
                line and not line.endswith(b"\r\n")):
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        return line

    def close(self):
        self.stream.close()


class _GmailResponse(http.client.HTTPResponse):
    @contextmanager
    def _bounded_headers(self):
        stream = self.fp
        wrapper = getattr(self, "_framing_reader", None)
        if wrapper is None:
            self._framing_reader = wrapper = _HeaderReader(stream)
        elif wrapper.stream is not stream:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        self.fp = wrapper
        try:
            yield
        finally:
            if self.fp is wrapper:
                self.fp = stream

    def begin(self):
        self.debuglevel = 0
        with self._bounded_headers():
            super().begin()

    def _read_and_discard_trailer(self):
        with self._bounded_headers():
            super()._read_and_discard_trailer()

    def _read_next_chunk_size(self):
        # stdlib int(line, 16) accepts negative lengths, which can lead to
        # fp.read(-1). Strict lengths and a shared framing budget keep both
        # decoded body and wire overhead bounded. Extensions are outside v1.
        with self._bounded_headers():
            line = self.fp.readline()
        if re.fullmatch(rb"[0-9A-Fa-f]{1,8}\r\n", line) is None:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        return int(line[:-2], 16)


def _response_length(response: http.client.HTTPResponse, maximum: int) -> int | None:
    if response.headers.defects:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    headers: dict[str, list[str]] = {}
    for name, value in response.getheaders():
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}", name):
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        headers.setdefault(name.lower(), []).append(value)

    def one(name, default=None):
        values = headers.get(name, [])
        if len(values) > 1:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        return values[0] if values else default

    content_type = one("content-type", "")
    if len(content_type) > 100 or _JSON_TYPE.fullmatch(content_type.strip()) is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if one("content-encoding", "identity").lower() != "identity":
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    length, transfer = one("content-length"), one("transfer-encoding")
    if transfer is not None and (transfer.lower() != "chunked" or length is not None):
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if length is None:
        return None
    if re.fullmatch(r"[0-9]{1,20}", length) is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if int(length) > maximum:
        raise CredentialError(CredentialFailure.READ_TOO_LARGE)
    return int(length)


def _withhold_secret_echo(value: Any, credential: CredentialLease) -> None:
    # Inputs are already bounded JSON or locally validated protected semantics.
    # Inspect encodings as well as literals without exporting any token variant.
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    info = credential.info
    if any(secret in encoded for secret in credential._redaction_variants((
            info.reference, info.account_id, info.connection_id))):
        raise CredentialError(CredentialFailure.UNSAFE_RESULT)


class _VerifiedSend(RuntimeOnly):
    """One-use, process/thread/lease-bound profile preflight; never durable authority."""

    def __init__(self, transport, submission, credential, body) -> None:
        self._owner_pid, self._thread_id = os.getpid(), threading.get_ident()
        self._transport, self._credential = transport, credential
        self._identity, self._mime = submission.identity, submission.mime_bytes
        self._fingerprint, self._body, self._used = submission.fingerprint, body, False

    def consume(self, transport, submission, credential) -> bytes:
        if self._owner_pid != os.getpid() or self._thread_id != threading.get_ident():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._used or transport is not self._transport or credential is not self._credential or (
                submission.identity != self._identity or submission.mime_bytes != self._mime
                or submission.fingerprint != self._fingerprint):
            raise CredentialError(CredentialFailure.AUTHORITY)
        credential._check_current()
        self._used = True
        body, self._body = self._body, b""
        return body


class GmailMutationHttp(RuntimeOnly):
    """Three reviewed operations. No caller selects HTTP methods, URLs or JSON.

    Each send needs fresh same-lease primary-sender verification. The runtime
    performs that read before marking send provider entry. Socket I/O has a
    ten-second timeout, not a preemptive total-operation deadline.
    """

    test_only = False

    def __init__(self) -> None:
        self._owner_pid = os.getpid()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def _check(self, submission, credential, *, probe=False) -> None:
        self._owner()
        expected_type = GmailProbeRequest if probe else GmailSendSubmission
        if type(submission) is not expected_type or type(credential) is not CredentialLease:
            raise CredentialError(CredentialFailure.AUTHORITY)
        credential._check_current()
        submission._owner()
        info, account = credential.info, submission._payload["account"]
        if info.provider != "google" or info.account_id != account["account_id"] or (
                info.connection_id != account["connection_id"]):
            raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
        if info.scopes != GOOGLE_GMAIL_SEND_SCOPES:
            raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
        if (submission.identity.claim_epoch is not None) != probe:
            raise CredentialError(CredentialFailure.AUTHORITY)
        if not probe and submission.semantics["message"]["bcc"]:
            raise CredentialError(CredentialFailure.UNSUPPORTED)

    def verify_sender(self, submission: GmailSendSubmission, credential: CredentialLease) -> _VerifiedSend:
        self._check(submission, credential)
        _withhold_secret_echo(submission.semantics, credential)
        mime = submission.mime_bytes
        if not mime or len(mime) > MAX_MIME_BYTES:
            raise CredentialError(CredentialFailure.INVALID)
        body = json.dumps({"raw": base64.urlsafe_b64encode(mime).decode("ascii").rstrip("=")},
                          separators=(",", ":"), ensure_ascii=True).encode("ascii")
        _withhold_secret_echo(body.decode("ascii"), credential)
        profile = self._exchange(credential, _Operation.PROFILE)
        # The projection excludes mailbox counters/history. Reject unexpected
        # keys rather than letting provider metadata become action authority.
        if set(profile) != {"emailAddress"}:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        sender = normalize_address(profile["emailAddress"])
        if sender != submission.semantics["sender"]:
            raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
        credential._check_current()
        return _VerifiedSend(self, submission, credential, body)

    def send(self, submission: GmailSendSubmission, credential: CredentialLease, *, verification=None) -> dict[str, Any]:
        self._check(submission, credential)
        if type(verification) is not _VerifiedSend:
            raise CredentialError(CredentialFailure.AUTHORITY)
        result = self._exchange(credential, _Operation.SEND, submission=submission, verification=verification)
        if not {"id", "threadId"} <= set(result) or not set(result) <= {"id", "threadId", "labelIds"}:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        _provider_id(result["id"])
        _provider_id(result["threadId"])
        if "labelIds" in result:
            labels = result["labelIds"]
            if type(labels) is not list or not 1 <= len(labels) <= 20 or "SENT" not in labels:
                raise CredentialError(CredentialFailure.READ_RESPONSE)
            for label in labels:
                _provider_id(label)
        return result

    def probe(self, request: GmailProbeRequest, credential: CredentialLease) -> GmailProbeResponse:
        self._check(request, credential, probe=True)
        result = self._exchange(credential, _Operation.SEARCH, probe=request)
        if not set(result) <= {"messages", "nextPageToken", "resultSizeEstimate"}:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        rows = result.get("messages", [])
        if type(rows) is not list or len(rows) > MAX_PROBE_CANDIDATES:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        estimate = result.get("resultSizeEstimate")
        if estimate is not None and (type(estimate) is not int or not 0 <= estimate < 2**32):
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        if "nextPageToken" in result and (type(result["nextPageToken"]) is not str or len(result["nextPageToken"]) > 2048):
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        complete = "nextPageToken" not in result and (estimate is None or estimate <= len(rows))
        references, seen = [], set()
        for row in rows:
            if type(row) is not dict or set(row) != {"id", "threadId"}:
                raise CredentialError(CredentialFailure.READ_RESPONSE)
            message_id, thread_id = _provider_id(row["id"]), _provider_id(row["threadId"])
            if message_id in seen:
                raise CredentialError(CredentialFailure.READ_RESPONSE)
            seen.add(message_id)
            references.append((message_id, thread_id))
        candidates = []
        for message_id, thread_id in references:
            self._check(request, credential, probe=True)
            message = self._exchange(credential, _Operation.CANDIDATE, message_id=message_id)
            candidates.append(candidate_from_message(message, request, message_id, thread_id))
        return GmailProbeResponse(tuple(candidates), complete)

    def _exchange(self, credential, operation: _Operation, *, submission=None, verification=None, probe=None, message_id=None):
        """Private closed operation selector, never an arbitrary request API."""
        self._owner()
        body = None
        if operation is _Operation.PROFILE and submission is verification is probe is message_id is None:
            method, path, maximum = "GET", "/gmail/v1/users/me/profile?fields=emailAddress", MAX_PROFILE_BYTES
        elif operation is _Operation.SEND and type(submission) is GmailSendSubmission and (
                type(verification) is _VerifiedSend and probe is message_id is None):
            self._check(submission, credential)
            body = verification.consume(self, submission, credential)
            method, path, maximum = "POST", _MESSAGES + "/send", MAX_SEND_RESPONSE_BYTES
        elif operation is _Operation.SEARCH and type(probe) is GmailProbeRequest and submission is verification is message_id is None:
            self._check(probe, credential, probe=True)
            parameters = {"labelIds": "SENT", "q": probe.query, "maxResults": str(MAX_PROBE_CANDIDATES),
                          "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate"}
            method, path, maximum = "GET", _MESSAGES + "?" + urlencode(parameters, quote_via=quote, safe=""), MAX_SEARCH_BYTES
        elif operation is _Operation.CANDIDATE and submission is verification is probe is None:
            parameters = {"format": "full", "fields": _CANDIDATE_FIELDS}
            method, path, maximum = "GET", _MESSAGES + "/" + _provider_id(message_id) + "?" + urlencode(
                parameters, quote_via=quote, safe=""), MAX_CANDIDATE_BYTES
        else:
            raise CredentialError(CredentialFailure.AUTHORITY)
        result, failure, interruption = None, CredentialFailure.PROVIDER, None
        connection = response = None
        try:
            token = credential.reveal()
            if token in path or quote(token, safe="") in path:
                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
            connection = http.client.HTTPSConnection(_HOST, timeout=10, context=_oauth_tls_context())
            connection.debuglevel = 0
            connection.response_class = _GmailResponse
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "Accept-Encoding": "identity"}
            if body is not None:
                headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
            self._owner()
            credential._check_current()
            # Exactly one request. http.client has no automatic request retry,
            # redirect or environment proxy behavior. Never refresh/resend here.
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            self._owner()
            if response.status != 200:
                # Status alone proves no effect truth. Do not read errors,
                # Location, Retry-After, or provider diagnostics; never resend.
                raise CredentialError(CredentialFailure.PROVIDER)
            length = _response_length(response, maximum)
            raw = response.read(maximum + 1)
            self._owner()
            if len(raw) > maximum:
                raise CredentialError(CredentialFailure.READ_TOO_LARGE)
            if length is not None and len(raw) != length:
                raise CredentialError(CredentialFailure.READ_RESPONSE)
            candidate = decode_json(raw)
            _withhold_secret_echo(candidate, credential)
            result = candidate
        except CredentialError as exc:
            failure = exc.reason
        except BaseException as exc:
            interruption = _interruption_type(exc)
        finally:
            self._owner()  # no inherited socket cleanup in a forked child
            try:
                try:
                    if response is not None:
                        response.close()
                finally:
                    if connection is not None:
                        connection.close()
            except BaseException as exc:
                result = None
                interruption = interruption or _interruption_type(exc)
        self._owner()
        # Raise outside handlers: raw exception text, chains and notes cannot
        # escape this boundary, including KeyboardInterrupt/SystemExit text.
        if interruption is not None:
            raise interruption()
        if result is None:
            raise CredentialError(failure)
        return result
