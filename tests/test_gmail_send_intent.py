import base64
import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

import pytest

from agentcore.credentials import CredentialInfo
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.integrations.gmail.send_intent import (
    MAX_BODY_BYTES, MAX_MIME_BYTES, SendIntentError, build_mime, make_send_payload,
    normalize_address, normalize_send_arguments, repeat_intent_equivalent, validate_send_payload,
)
from gmail_send_fakes import ARGS, SENDER


NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)
INFO = CredentialInfo("crd_" + "1" * 32, "con_" + "2" * 32, "acc_" + "3" * 32,
                      "google", GOOGLE_GMAIL_SEND_SCOPES, 1)


def payload(args=None, info=INFO):
    return make_send_payload(dict(ARGS if args is None else args), info, sender=SENDER, now=NOW)


def test_plain_text_mime_is_bounded_utf8_crlf_deterministic_and_preserves_private_bcc():
    args = {**ARGS, "subject": "Subject café ☕", "body": "line1\nline2\rline3\r\n"}
    value = payload(args)
    raw = base64.b64decode(value["mime_base64"])
    assert len(raw) < MAX_MIME_BYTES
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert all(len(line) <= 998 for line in raw.split(b"\r\n"))
    assert build_mime(value["message"], sender=SENDER, message_id=value["message_id"], date=value["date"]) == raw
    parsed = BytesParser(policy=policy.SMTP).parsebytes(raw)
    assert not parsed.defects and not parsed.is_multipart()
    assert parsed.get_content_type() == "text/plain" and parsed.get_content_charset() == "utf-8"
    assert parsed["Content-Transfer-Encoding"] == "base64"
    assert parsed["Bcc"] == "blind@sample.invalid"  # Submission MIME, not recipient copies.
    assert str(parsed["Subject"]) == args["subject"]
    assert parsed.get_payload(decode=True).decode("utf-8") == "line1\r\nline2\r\nline3\r\n"
    assert str(parsed["Message-ID"]) == value["message_id"]
    assert validate_send_payload(json.loads(json.dumps(value))) == value


def test_fresh_actions_have_random_markers_and_existing_payload_never_regenerates_one():
    first, second = payload(), payload()
    assert first["message_id"] != second["message_id"]
    assert first["preparation_id"] != second["preparation_id"]
    assert len(first["message_id"].split("@")[0]) == 65
    assert validate_send_payload(first)["message_id"] == first["message_id"]
    assert repeat_intent_equivalent(first, second)
    assert not repeat_intent_equivalent(first, first)


def test_recipient_normalization_is_deterministic_and_preserves_roles_and_local_case():
    args = {"to": [" User@EXAMPLE.INVALID "], "subject": "", "body": ""}
    cleaned = normalize_send_arguments(args)
    assert cleaned == {"to": ["User@example.invalid"], "cc": [], "bcc": [], "subject": "", "body": "\r\n"}
    assert normalize_send_arguments(cleaned) == cleaned


@pytest.mark.parametrize("address", [
    "evil@example.invalid\r\nBcc: victim@example.invalid", "evil@example.invalid\n", "a\x00@sample.invalid",
    "Display <a@sample.invalid>", "group:a@sample.invalid;", "a(comment)@sample.invalid", '"a b"@sample.invalid',
    "a..b@sample.invalid", ".a@sample.invalid", "a.@sample.invalid", "a@[127.0.0.1]", "a@localhost",
    "a@-bad.invalid", "a@bad-.invalid", "a@bad..invalid", "a@bücher.invalid", "é@sample.invalid",
    "a@sample.invalid,b@sample.invalid", "a@sample.invalid\u202e", "a" * 65 + "@sample.invalid",
], ids=lambda value: "address-case")
def test_invalid_or_ambiguous_addresses_fail_without_parser_content(address):
    with pytest.raises(SendIntentError) as error:
        normalize_address(address)
    assert address not in str(error.value)
    assert error.value.__context__ is None


@pytest.mark.parametrize("other", ["RECIPIENT@sample.invalid", "recipient@SAMPLE.INVALID"])
def test_duplicates_across_roles_are_rejected(other):
    with pytest.raises(SendIntentError):
        normalize_send_arguments({**ARGS, "bcc": [other]})


def test_consumer_gmail_alias_duplicates_are_rejected_without_rewriting_delivery_addresses():
    with pytest.raises(SendIntentError):
        normalize_send_arguments({**ARGS, "to": ["first.last+tag@gmail.com"], "bcc": ["FIRSTLAST@googlemail.com"]})


@pytest.mark.parametrize("key,value", [
    ("subject", "x" * 1025), ("subject", "x\r\nInjected: yes"), ("subject", "x\u0085y"),
    ("subject", "☕" * 342), ("body", "x" * 65535), ("body", "x\x00y"), ("body", "x\u202ey"),
    ("body", "\ud800"), ("to", []), ("to", ["a@sample.invalid"] * 21),
], ids=("subject-bytes", "subject-crlf", "subject-control", "subject-utf8", "body-bytes",
        "body-control", "body-format", "body-surrogate", "no-to", "recipient-count"))
def test_bounds_and_controls(key, value):
    with pytest.raises(SendIntentError):
        normalize_send_arguments({**ARGS, key: value})


def test_maximum_utf8_body_and_recipient_byte_budget():
    value = payload({**ARGS, "body": "x" * (MAX_BODY_BYTES - 2)})
    assert len(value["message"]["body"].encode()) == MAX_BODY_BYTES
    domain = ".".join(["d" * 50] * 3) + ".invalid"
    addresses = [f"{index:02d}" + "a" * 62 + "@" + domain for index in range(20)]
    with pytest.raises(SendIntentError):
        normalize_send_arguments({**ARGS, "to": addresses, "cc": [], "bcc": []})


@pytest.mark.parametrize("key", ["raw", "mime_base64", "headers", "from", "date", "message_id", "account", "provider",
                                  "scopes", "url", "attachments", "html", "thread_id", "approval", "allow_email_send"])
def test_model_cannot_supply_raw_mime_or_authority_or_other_headers(key):
    with pytest.raises(SendIntentError):
        normalize_send_arguments({**ARGS, key: "forged"})


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("mime_version", 2), ("profile_version", "gmail-compose.v1"),
                                        ("message_id", "<model@sample.invalid>"), ("date", "invalid"), ("mime_sha256", "0" * 64)])
def test_tampered_or_unknown_protected_schema_fails(field, value):
    item = payload()
    item[field] = value
    with pytest.raises(SendIntentError):
        validate_send_payload(item)


@pytest.mark.parametrize("field", ["to", "cc", "bcc", "subject", "body", "account"])
def test_semantic_changes_are_not_equivalent_risky_repeat(field):
    before = payload()
    args = copy.deepcopy(ARGS)
    info = INFO
    if field in ("to", "cc", "bcc"):
        args[field] = ["different@sample.invalid"]
    elif field == "account":
        info = replace(INFO, account_id="acc_" + "4" * 32)
    else:
        args[field] = "different content"
    assert not repeat_intent_equivalent(before, payload(args, info))


@pytest.mark.parametrize("field", ["to", "cc", "bcc", "subject", "body", "account_id", "connection_id", "sender", "message_id", "mime_version"])
def test_protected_digest_binds_every_send_semantic_without_regenerating_other_random_fields(field):
    import hashlib
    from agentcore.action_journal import InMemoryProtectedActionPayloadStore, StaticInstallationKeyProvider
    from agentcore.integrations.gmail.send_intent import build_mime
    store = InMemoryProtectedActionPayloadStore(StaticInstallationKeyProvider(bytes(range(32))))
    before = payload()
    after = copy.deepcopy(before)
    if field in {"to", "cc", "bcc"}:
        after["message"][field] = ["different@sample.invalid"]
    elif field in {"subject", "body"}:
        after["message"][field] = "different\r\n" if field == "body" else "different"
    elif field in {"account_id", "connection_id"}:
        after["account"][field] = ("acc_" if field == "account_id" else "con_") + "e" * 32
    elif field == "sender":
        after["account"][field] = "different@sample.invalid"
    elif field == "message_id":
        after[field] = "<" + "e" * 64 + "@agentcore.invalid>"
    else:
        after[field] = 2
    raw = build_mime(after["message"], sender=after["account"]["sender"], message_id=after["message_id"], date=after["date"])
    after["mime_base64"] = base64.b64encode(raw).decode("ascii")
    after["mime_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        assert before["preparation_id"] == after["preparation_id"] and before["date"] == after["date"]
        assert store.intent_digest(before) != store.intent_digest(after)
        if field != "mime_version":
            validate_send_payload(after)
    finally:
        store.close()
