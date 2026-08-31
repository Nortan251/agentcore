"""Pinned, GET-only Gmail HTTPS. No account selection, refresh, or retries."""

from __future__ import annotations

import http.client
import json
import os
import re
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import _oauth_tls_context
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.runtime import _interruption_type
from agentcore.credentials.vault import CredentialLease
from agentcore.integrations.gmail.limits import (
    MAX_JSON_BYTES, MAX_JSON_DEPTH, MAX_JSON_NODES, reference, search_arguments,
)


_HOST = "gmail.googleapis.com"
_MESSAGES = "/gmail/v1/users/me/messages"
# Match the reviewed mutation transport's framing budgets without changing it.
MAX_HTTP_HEADER_BYTES = 16_384
MAX_HTTP_HEADER_LINE = 4096
MAX_HTTP_HEADER_LINES = 66  # status/interim headers, chunk-size lines, trailers
_JSON_TYPE = re.compile(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?', re.IGNORECASE)
_HEADER_NAME = re.compile(rb"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}")
_FRAMING_FIELDS = {b"content-length", b"transfer-encoding", b"content-type", b"content-encoding"}
_STATUS_FAILURES = {
    401: CredentialFailure.READ_AUTHENTICATION,
    # A 403 does not prove which scope, Workspace restriction, or quota
    # caused rejection. Never infer revocation or request more authority.
    403: CredentialFailure.READ_PERMISSION,
    404: CredentialFailure.READ_NOT_FOUND,
    429: CredentialFailure.READ_RATE_LIMIT,
}


def _header_name(line: bytes) -> bytes:
    name, separator, value = line[:-2].partition(b":")
    if not separator or _HEADER_NAME.fullmatch(name) is None or any(
            byte < 32 and byte != 9 or byte == 127 for byte in value):
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    return name.lower()


class _FramingReader:
    """Bound every stream call and share one budget across HTTP framing lines."""

    def __init__(self, stream) -> None:
        self.stream, self.remaining, self.lines = stream, MAX_HTTP_HEADER_BYTES, 0
        self.headers, self.status_line = True, True

    def readline(self, limit=-1):
        maximum = min(MAX_HTTP_HEADER_LINE + 1, self.remaining + 1)
        if limit >= 0:
            maximum = min(maximum, limit)
        line = self.stream.readline(maximum)
        self.remaining -= len(line)
        self.lines += 1
        if self.remaining < 0 or len(line) > MAX_HTTP_HEADER_LINE or self.lines > MAX_HTTP_HEADER_LINES or (
                not line.endswith(b"\r\n")):
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        if self.headers:
            if self.status_line:
                self.status_line = False
            elif line == b"\r\n":
                self.status_line = True  # another status line after an interim response
            else:
                _header_name(line)
        return line

    def read(self, size=-1):
        # Defense at the final stream seam even if a stdlib parser asks for an
        # unexpected size. No omitted, negative or oversized read can escape.
        if type(size) is not int or not 0 <= size <= MAX_JSON_BYTES + 1:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        return self.stream.read(size)

    def close(self):
        self.stream.close()

    def flush(self):
        self.stream.flush()


class _GmailReadResponse(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fp = self._framing_reader = _FramingReader(self.fp)
        self._remaining_body = MAX_JSON_BYTES

    def begin(self):
        self.debuglevel = 0
        super().begin()
        self._framing_reader.headers = False

    def _read_next_chunk_size(self):
        line = self.fp.readline()
        # int(line, 16) alone accepts signs. Extensions are outside this contract.
        if re.fullmatch(rb"[0-9A-Fa-f]{1,8}\r\n", line) is None:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        size = int(line[:-2], 16)
        if size > self._remaining_body:
            raise CredentialError(CredentialFailure.READ_TOO_LARGE)
        self._remaining_body -= size
        return size

    def _get_chunk_left(self):
        # stdlib discards two bytes without checking CRLF. Validate and charge
        # that delimiter to the same framing-byte budget before reading it.
        if self.chunk_left == 0:
            self._framing_reader.remaining -= 2
            if self._framing_reader.remaining < 0 or self._safe_read(2) != b"\r\n":
                raise CredentialError(CredentialFailure.READ_RESPONSE)
            self.chunk_left = None
        return super()._get_chunk_left()

    def _read_and_discard_trailer(self):
        while True:
            line = self.fp.readline()
            if line == b"\r\n":
                return
            # EOF, bare LF, folding and malformed fields fail closed; trailers
            # cannot override the already-validated response framing/type.
            if _header_name(line) in _FRAMING_FIELDS:
                raise CredentialError(CredentialFailure.READ_RESPONSE)


def _response_length(response: http.client.HTTPResponse) -> int | None:
    if response.headers.defects:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    headers: dict[str, list[str]] = {}
    for name, value in response.getheaders():
        headers.setdefault(name.lower(), []).append(value)

    def one(name, default=None):
        values = headers.get(name, [])
        if len(values) > 1:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        return values[0] if values else default

    content_type = one("content-type", "")
    if type(content_type) is not str or len(content_type) > 100 or _JSON_TYPE.fullmatch(content_type.strip()) is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if one("content-encoding", "identity").lower() != "identity":
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    length, transfer = one("content-length"), one("transfer-encoding")
    if transfer is not None and (transfer.lower() != "chunked" or length is not None):
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if length is None:
        return None
    if type(length) is not str or re.fullmatch(r"[0-9]{1,20}", length) is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if int(length) > MAX_JSON_BYTES:
        raise CredentialError(CredentialFailure.READ_TOO_LARGE)
    return int(length)


def decode_json(raw: bytes) -> dict[str, Any]:
    """Bound bytes, lexical nesting, integer size, nodes, and duplicate keys."""
    if type(raw) is not bytes or not raw or len(raw) > MAX_JSON_BYTES:
        raise CredentialError(CredentialFailure.READ_TOO_LARGE if raw else CredentialFailure.READ_RESPONSE)
    result = None
    try:
        text = raw.decode("utf-8")
        depth = 0
        quoted = escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError()
            elif character in "]}":
                depth -= 1
        def pairs(items):
            value = {}
            for key, child in items:
                if key in value or len(key) > 128:
                    raise ValueError()
                value[key] = child
            return value
        def invalid(_):
            raise ValueError()
        def integer(value):
            if len(value) > 20:
                raise ValueError()
            number = int(value)
            if not -(2**63) <= number <= 2**64 - 1:
                raise ValueError()
            return number
        parsed = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid,
                            parse_float=invalid, parse_int=integer)
        if type(parsed) is not dict:
            raise ValueError()
        pending, count = [parsed], 0
        while pending:
            value = pending.pop()
            count += 1
            if count > MAX_JSON_NODES:
                raise ValueError()
            if type(value) is dict:
                pending.extend(value.keys())
                pending.extend(value.values())
            elif type(value) is list:
                pending.extend(value)
            elif type(value) is str and any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
                raise ValueError()
        result = parsed
    except (ValueError, UnicodeError, RecursionError):
        pass
    if result is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    return result


class GmailReadTransport(Protocol):
    """Trusted host/test seam, not a generic HTTP client."""

    def list_messages(self, credential: CredentialLease, args: dict[str, Any]) -> dict[str, Any]: ...

    def read_message(self, credential: CredentialLease, message_id: str) -> dict[str, Any]: ...


class GmailReadHttp(RuntimeOnly):
    """Only two fixed Google v1 GET paths, always for the authenticated 'me'.

    Uses the reviewed OAuth TLS context, including no SSLKEYLOGFILE hook.
    http.client does not discover proxies or follow redirects. The ten-second
    timeout is per socket I/O, not a preemptive whole-operation deadline.
    """

    def __init__(self) -> None:
        self._owner_pid = os.getpid()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def list_messages(self, credential: CredentialLease, args: dict[str, Any]) -> dict[str, Any]:
        self._owner()
        return self._get(credential, search=search_arguments(args))

    def read_message(self, credential: CredentialLease, message_id: str) -> dict[str, Any]:
        self._owner()
        return self._get(credential, message_id=reference(message_id))

    def _get(self, credential: CredentialLease, *, search: dict[str, Any] | None = None,
             message_id: str | None = None) -> dict[str, Any]:
        self._owner()
        if type(credential) is not CredentialLease or (search is None) == (message_id is None):
            raise CredentialError(CredentialFailure.AUTHORITY)
        if search is not None:
            args = search_arguments(search)
            parameters = {"maxResults": str(args["max_results"]),
                          "fields": "messages(id,threadId),nextPageToken,resultSizeEstimate"}
            if "q" in args:
                parameters["q"] = args["q"]
            if "page_token" in args:
                parameters["pageToken"] = args["page_token"]
            path = _MESSAGES
        else:
            path = _MESSAGES + "/" + reference(message_id)
            # FULL is the parsed MIME representation, never RAW. Gmail may
            # include inline part data; only bounded text is decoded by us.
            parameters = {"format": "full", "fields": "id,threadId,labelIds,internalDate,snippet,payload"}
        path += "?" + urlencode(parameters, quote_via=quote, safe="")
        result = None
        failure = CredentialFailure.PROVIDER
        interruption = None
        try:
            connection = http.client.HTTPSConnection(_HOST, timeout=10, context=_oauth_tls_context())
            connection.debuglevel = 0  # override class-wide debug settings
            connection.response_class = _GmailReadResponse
            response = None
            try:
                self._owner()
                token = credential.reveal()
                # Even an accidental echo of the current token as a search or
                # identifier must not move authentication material into a URL.
                if token in path or quote(token, safe="") in path:
                    raise CredentialError(CredentialFailure.READ_ARGUMENTS)
                connection.request("GET", path, body=None, headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                })
                response = connection.getresponse()
                self._owner()
                if response.status != 200:
                    # Do not even read error bodies or Location headers.
                    raise CredentialError(_STATUS_FAILURES.get(response.status, CredentialFailure.PROVIDER))
                length = _response_length(response)
                raw = response.read(MAX_JSON_BYTES + 1)
                self._owner()
                if type(raw) is not bytes or len(raw) > MAX_JSON_BYTES:
                    raise CredentialError(CredentialFailure.READ_TOO_LARGE)
                if length is not None and len(raw) != length:
                    raise CredentialError(CredentialFailure.READ_RESPONSE)
                result = decode_json(raw)
            finally:
                self._owner()  # never touch an inherited socket in a child
                try:
                    if response is not None:
                        response.close()
                finally:
                    connection.close()
        except CredentialError as exc:
            failure = exc.reason
        except BaseException as exc:
            interruption = _interruption_type(exc)
        self._owner()
        if interruption is not None:
            raise interruption()
        if result is None:
            raise CredentialError(failure)
        return result
