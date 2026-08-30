"""Bounded Gmail MIME normalization. All returned mail remains untrusted data."""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.integrations.gmail.limits import (
    MAX_ATTACHMENTS, MAX_BODY_BYTES, MAX_BODY_CHARS, MAX_HEADERS,
    MAX_HEADER_NAME_CHARS, MAX_HEADER_VALUE_CHARS, MAX_LABELS, MAX_MIME_DEPTH,
    MAX_MIME_PARTS, MAX_OUTPUT_CHARS, MAX_OUTPUT_ESCAPED_CHARS, MAX_PART_BYTES,
    page_token, reference,
)


_HEADERS = {
    "from": "from", "to": "to", "cc": "cc", "reply-to": "reply_to",
    "subject": "subject", "date": "date", "message-id": "message_id",
}
_CHARSETS = {
    "utf-8": "utf-8", "utf8": "utf-8", "us-ascii": "ascii", "ascii": "ascii",
    "iso-8859-1": "iso-8859-1", "iso8859-1": "iso-8859-1", "latin1": "iso-8859-1",
    "windows-1252": "cp1252", "cp1252": "cp1252",
}
_MIME_TYPE = re.compile(r"[a-zA-Z0-9!#$&^_.+-]{1,64}/[a-zA-Z0-9!#$&^_.+-]{1,64}")


def _unsafe() -> None:
    raise CredentialError(CredentialFailure.READ_CONTENT)


def _string(value: Any, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        _unsafe()
    return value


def _size(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        _unsafe()
    return value


def _decode_text(data: bytes, charset: str | None) -> str:
    # Do not send arbitrary MIME charset names to Python's codec registry:
    # non-text transforms and unbounded decompression codecs are not allowed.
    encoding = _CHARSETS.get(charset.lower() if type(charset) is str else "utf-8")
    if encoding is None:
        _unsafe()
    result = None
    try:
        result = data.decode(encoding, errors="strict")
    except UnicodeError:
        pass
    if result is None:
        _unsafe()
    return result


def _clean(text: str, *, header: bool = False) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in {"Cc", "Cf", "Cs"})
    return " ".join(text.split()) if header else text.strip()


def _header_text(value: str) -> str:
    parts = decode_header(value)
    if len(parts) > MAX_HEADERS:
        _unsafe()
    return " ".join(_decode_text(part, charset) if type(part) is bytes else part
                    for part, charset in parts)


class _InertHTML(HTMLParser):
    """Non-rendering text extraction; no attributes, resources or code APIs."""

    _HIDDEN = frozenset({"head", "script", "style", "template", "iframe", "object", "embed",
                         "svg", "math", "noscript"})
    _BLOCKS = frozenset({"p", "div", "br", "li", "tr", "table", "section", "article",
                         "h1", "h2", "h3", "h4", "pre", "blockquote", "hr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden: list[str] = []
        self.chunks: list[str] = []
        self.events = 0

    def _event(self) -> None:
        self.events += 1
        if self.events > 8192:
            _unsafe()

    def handle_starttag(self, tag, attrs):
        self._event()
        if tag in self._HIDDEN:
            self.hidden.append(tag)
            if len(self.hidden) > 64:
                _unsafe()
        elif not self.hidden and tag in self._BLOCKS:
            self.chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self._event()
        if tag in self.hidden:
            while self.hidden:
                if self.hidden.pop() == tag:
                    break
        elif not self.hidden and tag in self._BLOCKS:
            self.chunks.append("\n")

    def handle_data(self, data):
        self._event()
        if not self.hidden:
            self.chunks.append(data)

    def handle_comment(self, data):
        self._event()

    def handle_decl(self, decl):
        self._event()


@dataclass(frozen=True)
class _Body:
    text: str = ""
    source: str = "none"
    has_plain: bool = False


class _Mime:
    def __init__(self) -> None:
        self.parts = self.headers = self.decoded = self.omitted = 0
        self.attachments: list[dict[str, Any]] = []
        self.message_headers: dict[str, list[str]] = {}

    def walk(self, part: Any, depth: int = 0, *, include: bool = True) -> _Body:
        self.parts += 1
        if type(part) is not dict or depth > MAX_MIME_DEPTH or self.parts > MAX_MIME_PARTS:
            _unsafe()
        mime = _string(part.get("mimeType", ""), 129).lower()
        if _MIME_TYPE.fullmatch(mime) is None:
            _unsafe()
        filename = _string(part.get("filename", ""), 1024)
        headers = part.get("headers", [])
        if type(headers) is not list or len(headers) + self.headers > MAX_HEADERS:
            _unsafe()
        self.headers += len(headers)
        control = Message()
        for header in headers:
            if type(header) is not dict:
                _unsafe()
            name = _string(header.get("name"), MAX_HEADER_NAME_CHARS).lower()
            value = _string(header.get("value"), MAX_HEADER_VALUE_CHARS)
            if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_\x60|~-]+", name):
                _unsafe()
            if depth == 0 and name in _HEADERS:
                self.message_headers.setdefault(_HEADERS[name], []).append(value)
            if name in {"content-type", "content-disposition"}:
                if name in control:  # ambiguous MIME control headers
                    _unsafe()
                control[name] = value
        body = part.get("body", {})
        children = part.get("parts", [])
        if type(body) is not dict or type(children) is not list or len(children) > MAX_MIME_PARTS:
            _unsafe()
        size = _size(body.get("size", 0))
        if "data" in body and type(body["data"]) is not str:
            _unsafe()
        if "attachmentId" in body:
            _string(body["attachmentId"], 2048)  # withheld; never dereferenced
        multipart = mime.startswith("multipart/")
        # A filename parameter still marks attachment content when Gmail's
        # convenience filename field is empty. Do not decode filename codecs
        # or attached text to manufacture a display name.
        named_part = (control.get_param("filename", header="content-disposition") is not None
                      or control.get_param("name", header="content-type") is not None)
        attached = bool(filename) or named_part or control.get_content_disposition() == "attachment" or (
            not multipart and mime not in {"text/plain", "text/html"})
        external_body = bool(body.get("attachmentId")) and not body.get("data")
        if include and (attached or external_body):
            self.attachments.append({"filename": filename, "mime_type": mime, "size": size})
            if external_body and not attached:
                self.omitted += 1
        results = [self.walk(child, depth + 1, include=include and not attached)
                   for child in children]
        if not include or attached or external_body:
            return _Body()
        if multipart:
            if body.get("data"):
                _unsafe()  # never decode a container as a leaf
            if mime == "multipart/alternative":
                # Presence of plain text wins even if that alternative is empty.
                return next((value for value in results if value.has_plain),
                            next((value for value in results if value.source != "none"), _Body()))
            selected = [value for value in results if value.source != "none"]
            if not selected:
                return _Body()
            return _Body("\n\n".join(value.text for value in selected),
                         selected[0].source if len({value.source for value in selected}) == 1 else "mixed",
                         any(value.has_plain for value in selected))
        if children:
            _unsafe()
        data = body.get("data", "")
        if len(data) > ((MAX_PART_BYTES + 2) // 3) * 4 or re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data) is None:
            _unsafe()
        bare = data.rstrip("=")
        padding = (-len(bare)) % 4
        if len(bare) % 4 == 1 or ("=" in data and len(data) - len(bare) != padding):
            _unsafe()
        decoded = None
        try:
            decoded = base64.b64decode(bare + "=" * padding, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            pass
        if decoded is None or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != bare:
            _unsafe()
        self.decoded += len(decoded)
        if len(decoded) > MAX_PART_BYTES or self.decoded > MAX_BODY_BYTES:
            _unsafe()
        # get_content_charset() decodes RFC 2231 parameters through a codec
        # selected by the message before returning the charset name. This
        # slice accepts only ordinary, allowlisted charset parameters.
        charset = control.get_param("charset")
        if charset is not None and type(charset) is not str:
            _unsafe()
        text = _decode_text(decoded, charset)
        if mime == "text/plain":
            return _Body(text, "plain", True)
        html = _InertHTML()
        html.feed(text)
        html.close()
        return _Body("".join(html.chunks), "html_to_text")


class _Output:
    """Budget actual text plus its ASCII JSON expansion (emoji may use 12)."""

    def __init__(self) -> None:
        self.characters = MAX_OUTPUT_CHARS
        self.escaped = MAX_OUTPUT_ESCAPED_CHARS
        self.truncation: set[str] = set()

    def text(self, value: str, limit: int, category: str, *, header: bool = False) -> str:
        cleaned = _clean(value, header=header)
        result = []
        for character in cleaned[:limit]:
            number = ord(character)
            cost = (12 if number > 0xFFFF else 6 if number > 0x7F else
                    2 if character in '\n\t"\\' else 1)
            if self.characters <= 0 or self.escaped < cost:
                self.truncation.add("output_budget")
                break
            self.characters -= 1
            self.escaped -= cost
            result.append(character)
        if len(result) < len(cleaned):
            self.truncation.add(category)
        return "".join(result)


def normalize_search(raw: Any, maximum: int) -> dict[str, Any]:
    if type(raw) is not dict:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    messages = raw.get("messages", [])
    if type(messages) is not list or len(messages) > maximum:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    result: dict[str, Any] = {"messages": []}
    for value in messages:
        if type(value) is not dict:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        result["messages"].append({"id": reference(value.get("id"), response=True),
                                   "thread_id": reference(value.get("threadId"), response=True)})
    if "nextPageToken" in raw:
        result["next_page_token"] = page_token(raw["nextPageToken"], response=True)
    if "resultSizeEstimate" in raw:
        estimate = raw["resultSizeEstimate"]
        if type(estimate) is not int or not 0 <= estimate <= 2**32 - 1:
            raise CredentialError(CredentialFailure.READ_RESPONSE)
        result["result_size_estimate"] = estimate
    return result


def normalize_message(raw: Any, message_id: str) -> dict[str, Any]:
    """Project one FULL response onto a small, detached public message."""
    if type(raw) is not dict or reference(raw.get("id"), response=True) != message_id:
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    output = _Output()
    result: dict[str, Any] = {
        "id": output.text(message_id, 128, "identifiers"),
        "thread_id": output.text(reference(raw.get("threadId"), response=True), 128, "identifiers"),
    }
    labels = raw.get("labelIds", [])
    if type(labels) is not list or len(labels) > 100:
        _unsafe()
    for label in labels:
        reference(label, response=True)
    result["label_ids"] = [output.text(label, 128, "labels") for label in labels[:MAX_LABELS]]
    if len(labels) > MAX_LABELS:
        output.truncation.add("labels")
    date = raw.get("internalDate")
    if date is not None and (type(date) is not str or re.fullmatch(r"[0-9]{1,19}", date) is None or int(date) > 2**63 - 1):
        _unsafe()
    result["internal_date"] = date
    result["snippet"] = output.text(_string(raw.get("snippet", ""), 4096), 256, "snippet")
    mime = _Mime()
    body = mime.walk(raw.get("payload"))
    result["headers"] = {
        name: output.text("; ".join(_header_text(value) for value in values), 512, "headers", header=True)
        for name, values in mime.message_headers.items()
    }
    result["body"] = output.text(body.text, MAX_BODY_CHARS, "body")
    result["body_source"] = body.source
    result["omitted_body_parts"] = mime.omitted
    result["attachments"] = [{
        "filename": output.text(item["filename"], 128, "attachment_metadata", header=True),
        "mime_type": item["mime_type"],
        "size": item["size"],
    } for item in mime.attachments[:MAX_ATTACHMENTS]]
    if len(mime.attachments) > MAX_ATTACHMENTS:
        output.truncation.add("attachments")
    result["attachments_omitted"] = max(0, len(mime.attachments) - MAX_ATTACHMENTS)
    result["truncated"] = bool(output.truncation)
    result["truncation"] = sorted(output.truncation)
    # Constants/keys/structure fit in the margin above the text budget. Keep
    # an explicit final guard below CredentialRuntime's 65,536-byte envelope.
    if len(json.dumps(result, ensure_ascii=True).encode("ascii")) > 48_000:
        _unsafe()
    return result
