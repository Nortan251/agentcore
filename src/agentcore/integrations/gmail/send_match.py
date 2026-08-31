"""Private conservative comparison of one bounded Gmail FULL candidate.

Only the frozen plain-text MIME v1 is supported. Missing or transformed fields
never establish a match. No attachment content, HTML, URL or resource is read.
"""

from __future__ import annotations

import base64
import re
from datetime import timezone
from email.header import decode_header
from email.parser import HeaderParser
from email.policy import default
from email.utils import format_datetime, parsedate_to_datetime

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.integrations.gmail.send import GmailProbeCandidate, GmailProbeRequest, _provider_id
from agentcore.integrations.gmail.send_intent import MAX_BODY_BYTES, normalize_address, normalize_send_arguments


_SELECTED = frozenset({"from", "to", "cc", "bcc", "subject", "date", "message-id",
                       "mime-version", "content-type", "content-transfer-encoding"})
_REQUIRED = _SELECTED - {"cc", "bcc"}
_PLAIN_UTF8 = re.compile(r'text/plain\s*;\s*charset\s*=\s*(?:utf-8|"utf-8")', re.IGNORECASE)


def _headers(rows):
    if type(rows) is not list or len(rows) > 200:
        raise ValueError()
    headers, total = {}, 0
    for row in rows:
        if type(row) is not dict or set(row) != {"name", "value"}:
            raise ValueError()
        name, value = row["name"], row["value"]
        if type(name) is not str or re.fullmatch(r"[A-Za-z0-9-]{1,128}", name) is None or (
                type(value) is not str or len(value) > 4096):
            raise ValueError()
        total += len(name.encode("utf-8")) + len(value.encode("utf-8"))
        if total > 32_768:
            raise ValueError()
        name = name.lower()
        # Extra transport headers are inert. Unsupported message semantics
        # cannot silently be ignored when certifying this direct-send intent.
        if name.startswith(("resent-", "content-")) and name not in _SELECTED or name in {
                "sender", "reply-to", "in-reply-to", "references"}:
            raise ValueError()
        if name not in _SELECTED:
            continue
        if name in headers:
            raise ValueError()
        value = re.sub(r"\r\n(?=[ \t])", "", value)
        if "\r" in value or "\n" in value or any(ord(ch) < 32 and ch != "\t" for ch in value):
            raise ValueError()
        headers[name] = value
    if not _REQUIRED <= set(headers) or "bcc" in headers:
        raise ValueError()
    return headers


def _subject(value):
    parsed = HeaderParser(policy=default.clone(raise_on_defect=True)).parsestr("Subject: " + value + "\r\n\r\n")
    header = parsed["Subject"]
    if parsed.defects or header is None or header.defects:
        raise ValueError()
    parts = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            charset = (charset or "ascii").lower()
            if charset not in {"utf-8", "us-ascii", "ascii"}:
                raise ValueError()
            part = part.decode(charset, errors="strict")
        parts.append(part)
    decoded = "".join(parts)
    # Require both stdlib interpretations to agree; never use replacement
    # decoding to turn invalid header bytes into an apparently exact subject.
    if decoded != str(header):
        raise ValueError()
    return decoded


def _date(value):
    when = parsedate_to_datetime(value)
    if when.tzinfo is not timezone.utc:
        raise ValueError()
    canonical = format_datetime(when, usegmt=True)
    # EmailMessage serializes UTC as +0000; protected v1 stores GMT. Both
    # represent exactly the same second. No fuzzy/internalDate time window.
    if value not in {canonical, format_datetime(when)}:
        raise ValueError()
    return canonical


def _body(value):
    if type(value) is not dict or set(value) != {"size", "data"}:
        raise ValueError()  # includes attachmentId: never fetch external parts
    size, data = value["size"], value["data"]
    if type(size) is not int or not 0 <= size <= MAX_BODY_BYTES or type(data) is not str or (
            len(data) > 4 * ((MAX_BODY_BYTES + 2) // 3) or re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data) is None):
        raise ValueError()
    stripped = data.rstrip("=")
    padding = "=" * (-len(stripped) % 4)
    if data not in {stripped, stripped + padding}:
        raise ValueError()
    decoded = base64.b64decode(stripped + padding, altchars=b"-_", validate=True)
    if len(decoded) != size or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != stripped:
        raise ValueError()
    return decoded.decode("utf-8", errors="strict")


def candidate_from_message(message, request: GmailProbeRequest, message_id: str, thread_id: str) -> GmailProbeCandidate:
    if type(message) is not dict or not {"id", "threadId", "labelIds", "payload"} <= set(message) or (
            not set(message) <= {"id", "threadId", "labelIds", "payload"}):
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    if _provider_id(message["id"]) != message_id or _provider_id(message["threadId"]) != thread_id:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    labels = message["labelIds"]
    if type(labels) is not list or len(labels) > 100:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    for label in labels:
        _provider_id(label)
    sent, proven = "SENT" in labels, False
    try:
        payload = message["payload"]
        if type(payload) is not dict or not set(payload) <= {"mimeType", "filename", "headers", "body", "parts"}:
            raise ValueError()
        if payload.get("mimeType") != "text/plain" or payload.get("filename", "") != "" or payload.get("parts", []) != []:
            raise ValueError()
        headers = _headers(payload.get("headers"))
        if headers["mime-version"] != "1.0" or _PLAIN_UTF8.fullmatch(headers["content-type"]) is None or (
                headers["content-transfer-encoding"].lower() != "base64"):
            raise ValueError()
        to = [normalize_address(part) for part in headers["to"].split(",")]
        cc = [normalize_address(part) for part in headers["cc"].split(",")] if "cc" in headers else []
        semantics = {
            "sender": normalize_address(headers["from"]),
            "message": normalize_send_arguments({"to": to, "cc": cc, "bcc": [],
                "subject": _subject(headers["subject"]), "body": _body(payload.get("body"))}),
            "message_id": headers["message-id"], "date": _date(headers["date"]), "mime_version": 1,
        }
        proven = semantics == request.semantics
    except Exception:
        # Do not export even a mismatching candidate's private header/body.
        pass
    return GmailProbeCandidate(message_id, thread_id, sent, proven,
                              request.semantics if proven else {})
