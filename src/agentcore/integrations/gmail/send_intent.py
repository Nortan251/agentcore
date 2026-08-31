"""Bounded, local-only Gmail send normalization and deterministic MIME v1."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any

from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.credentials.models import CredentialInfo, CredentialState


MAX_RECIPIENTS = 20
MAX_ADDRESS_BYTES = 254
MAX_RECIPIENT_BYTES = 4096
MAX_SUBJECT_BYTES = 1024
MAX_BODY_BYTES = 65_536
MAX_MIME_BYTES = 131_072
SEND_PROFILE_VERSION = "gmail-send.v1"
MIME_VERSION = 1
PREPARATION_SCOPE = "gmail.send.preparation.v1"
_PREPARATION = re.compile(r"gsp_[0-9a-f]{64}")
_MESSAGE_ID = re.compile(r"<[0-9a-f]{64}@agentcore\.invalid>")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_ATEXT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'*+-/=?^_`{|}~.")
_POLICY = SMTP.clone(utf8=False, cte_type="7bit", max_line_length=78, raise_on_defect=True)
_FIELDS = {"schema_version", "preparation_id", "account", "profile_version", "mime_version",
           "message", "message_id", "date", "mime_base64", "mime_sha256"}


class SendIntentError(ValueError):
    """Content-free validation failure, never an address/parser exception."""

    def __init__(self) -> None:
        super().__init__("The protected Gmail send intent is invalid.")


def _text(value: Any, maximum: int, *, body: bool = False) -> str:
    if type(value) is not str or len(value) > maximum:
        raise SendIntentError()
    allowed = "\t\r\n" if body else ""
    if any((unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"})
           and ch not in allowed for ch in value):
        raise SendIntentError()
    if len(value.encode("utf-8")) > maximum:
        raise SendIntentError()
    return value


def normalize_address(value: Any) -> str:
    value = _text(value, MAX_ADDRESS_BYTES)
    value = value.strip(" ")
    if not value.isascii() or value.count("@") != 1:
        raise SendIntentError()
    local, domain = value.split("@")
    if not local or len(local) > 64 or any(ch not in _ATEXT for ch in local):
        raise SendIntentError()
    labels = domain.split(".")
    if len(labels) < 2 or any(_DOMAIN_LABEL.fullmatch(label) is None for label in labels):
        raise SendIntentError()
    parsed = None
    try:
        parsed = Address(addr_spec=value)
    except Exception:
        pass
    if parsed is None or parsed.addr_spec != value or parsed.username != local or parsed.domain != domain:
        raise SendIntentError()
    return local + "@" + domain.lower()


def _recipient_key(value: str) -> str:
    local, domain = value.lower().split("@")
    # Conservative duplicate rejection, not an outbound address rewrite.
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return local + "@" + domain


def normalize_send_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
    if type(args) is not dict or not {"to", "subject", "body"} <= set(args) or not (
            set(args) <= {"to", "cc", "bcc", "subject", "body"}):
        raise SendIntentError()
    result: dict[str, Any] = {}
    seen: set[str] = set()
    total = 0
    for role in ("to", "cc", "bcc"):
        values = args.get(role, [])
        if type(values) not in (list, tuple) or len(values) > MAX_RECIPIENTS:
            raise SendIntentError()
        result[role] = []
        for value in values:
            address = normalize_address(value)
            key = _recipient_key(address)
            total += len(address)
            if key in seen or total > MAX_RECIPIENT_BYTES or len(seen) >= MAX_RECIPIENTS:
                raise SendIntentError()
            seen.add(key)
            result[role].append(address)
    if not result["to"]:
        raise SendIntentError()
    result["subject"] = _text(args["subject"], MAX_SUBJECT_BYTES)
    body = _text(args["body"], MAX_BODY_BYTES, body=True).replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    body = body.replace("\n", "\r\n")
    result["body"] = _text(body, MAX_BODY_BYTES, body=True)
    return result


def _date(value: Any) -> str:
    if type(value) is not str or len(value) > 40:
        raise SendIntentError()
    parsed = None
    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        pass
    if parsed is None or parsed.tzinfo is not timezone.utc or format_datetime(parsed, usegmt=True) != value:
        raise SendIntentError()
    return value


def build_mime(message: Mapping[str, Any], *, sender: str, message_id: str, date: str) -> bytes:
    """No randomness, clock, I/O, discovery, or model-selected header names."""
    normalized = normalize_send_arguments(message)
    sender = normalize_address(sender)
    if type(message_id) is not str or _MESSAGE_ID.fullmatch(message_id) is None:
        raise SendIntentError()
    date = _date(date)
    mail = EmailMessage(policy=_POLICY)
    mail["From"] = Address(addr_spec=sender)
    for role in ("to", "cc", "bcc"):
        if normalized[role]:
            mail[role.title()] = tuple(Address(addr_spec=value) for value in normalized[role])
    mail["Subject"] = normalized["subject"]
    mail["Date"] = date
    mail["Message-ID"] = message_id
    mail.set_content(normalized["body"], subtype="plain", charset="utf-8", cte="base64")
    encoded = mail.as_bytes(policy=_POLICY)
    if len(encoded) > MAX_MIME_BYTES or any(len(line) > 998 for line in encoded.split(b"\r\n")):
        raise SendIntentError()
    return encoded


def make_send_payload(args: Mapping[str, Any], info: CredentialInfo, *, sender: str,
                      now: datetime) -> dict[str, Any]:
    if type(info) is not CredentialInfo or info.provider != "google" or (
            info.state is not CredentialState.ACTIVE or info.scopes != GOOGLE_GMAIL_SEND_SCOPES):
        raise SendIntentError()
    if type(now) is not datetime or now.tzinfo is not timezone.utc:
        raise SendIntentError()
    info.require_current(now)
    message = normalize_send_arguments(args)
    account = {"provider": "google", "account_id": info.account_id,
               "connection_id": info.connection_id, "sender": normalize_address(sender)}
    marker = "<" + secrets.token_hex(32) + "@agentcore.invalid>"
    date = format_datetime(now.replace(microsecond=0), usegmt=True)
    mime = build_mime(message, sender=account["sender"], message_id=marker, date=date)
    payload = {
        "schema_version": 1, "preparation_id": "gsp_" + secrets.token_hex(32),
        "account": account, "profile_version": SEND_PROFILE_VERSION, "mime_version": MIME_VERSION,
        "message": message, "message_id": marker, "date": date,
        "mime_base64": base64.b64encode(mime).decode("ascii"),
        "mime_sha256": hashlib.sha256(mime).hexdigest(),
    }
    return validate_send_payload(payload)


def validate_send_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if type(payload) is not dict or set(payload) != _FIELDS:
        raise SendIntentError()
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or (
            type(payload["mime_version"]) is not int or payload["mime_version"] != MIME_VERSION
            or payload["profile_version"] != SEND_PROFILE_VERSION):
        raise SendIntentError()
    preparation = payload["preparation_id"]
    if type(preparation) is not str or _PREPARATION.fullmatch(preparation) is None:
        raise SendIntentError()
    account = payload["account"]
    if type(account) is not dict or set(account) != {"provider", "account_id", "connection_id", "sender"}:
        raise SendIntentError()
    if account["provider"] != "google" or any(type(account[name]) is not str or re.fullmatch(pattern, account[name]) is None
            for name, pattern in (("account_id", r"acc_[0-9a-f]{32}"), ("connection_id", r"con_[0-9a-f]{32}"))):
        raise SendIntentError()
    sender = normalize_address(account["sender"])
    message = normalize_send_arguments(payload["message"])
    if sender != account["sender"] or message != payload["message"]:
        raise SendIntentError()
    mime = build_mime(message, sender=sender, message_id=payload["message_id"], date=payload["date"])
    if type(payload["mime_base64"]) is not str or len(payload["mime_base64"]) > 4 * ((MAX_MIME_BYTES + 2) // 3) or (
            base64.b64encode(mime).decode("ascii") != payload["mime_base64"]
            or hashlib.sha256(mime).hexdigest() != payload["mime_sha256"]):
        raise SendIntentError()
    # All shapes/types are now bounded plain JSON. Detach from caller objects.
    return json.loads(json.dumps(payload, ensure_ascii=True, allow_nan=False))


def message_semantics(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fields the future account-bound probe must prove, including Bcc."""
    return {"sender": payload["account"]["sender"], "message": payload["message"],
            "message_id": payload["message_id"], "date": payload["date"], "mime_version": payload["mime_version"]}


def message_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(message_semantics(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(b"agentcore.gmail.send-message.v1\x00" + encoded).hexdigest()


def repeat_intent_equivalent(predecessor: Mapping[str, Any], successor: Mapping[str, Any]) -> bool:
    """Pure comparison; Stage 5 still owns both approvals and execution."""
    before, after = validate_send_payload(predecessor), validate_send_payload(successor)
    return (all(before[key] == after[key] for key in (
                "schema_version", "account", "profile_version", "mime_version", "message"))
            and before["preparation_id"] != after["preparation_id"]
            and before["message_id"] != after["message_id"])
