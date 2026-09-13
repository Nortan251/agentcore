"""Model-facing Gmail read contract: semantic inputs and minimal outputs.

The HTTP adapter still speaks Gmail's provider query language internally.  A
model never does.  This module is the deterministic translation and projection
boundary between those two shapes; it performs no I/O and holds no authority.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.integrations.gmail.limits import search_arguments
from agentcore.integrations.gmail.message import normalize_message, normalize_search


DEFAULT_RECENT_DAYS = 7
MAX_RECENT_DAYS = 30
DEFAULT_AGENT_RESULTS = 5
MAX_AGENT_RESULTS = 10
MAX_SENDER_CHARS = 254
MAX_SUBJECT_TERMS_CHARS = 256
MAX_SEARCH_TEXT_CHARS = 256
MAX_AGENT_BODY_CHARS = 4000
MAX_AGENT_RESULT_BYTES = 24_000

# Quotes, backslashes and Gmail's operator punctuation are deliberately
# excluded.  Values are inserted only inside a host-generated quoted operand,
# so a model cannot terminate that operand or introduce another operator.
SEMANTIC_TEXT_PATTERN = (
    r'[^\x00-\x1f\x7f-\x9f\ud800-\udfff"\\:(){}\[\]]{1,256}'
)
SENDER_PATTERN = (
    r'[^\x00-\x1f\x7f-\x9f\ud800-\udfff"\\:(){}\[\]]{1,254}'
)

_SEMANTIC_FIELDS = frozenset({
    "recent_days", "sender", "subject_terms", "search_text", "max_results",
})


def semantic_search_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """Validate semantic fields and build the sole internal Gmail query."""
    if type(args) is not dict or not set(args) <= _SEMANTIC_FIELDS:
        raise CredentialError(CredentialFailure.READ_ARGUMENTS)
    days = args.get("recent_days", DEFAULT_RECENT_DAYS)
    maximum = args.get("max_results", DEFAULT_AGENT_RESULTS)
    if type(days) is not int or not 1 <= days <= MAX_RECENT_DAYS:
        raise CredentialError(CredentialFailure.READ_ARGUMENTS)
    if type(maximum) is not int or not 1 <= maximum <= MAX_AGENT_RESULTS:
        raise CredentialError(CredentialFailure.READ_ARGUMENTS)

    values = {
        "sender": (SENDER_PATTERN, MAX_SENDER_CHARS),
        "subject_terms": (SEMANTIC_TEXT_PATTERN, MAX_SUBJECT_TERMS_CHARS),
        "search_text": (SEMANTIC_TEXT_PATTERN, MAX_SEARCH_TEXT_CHARS),
    }
    cleaned: dict[str, str] = {}
    for name, (pattern, limit) in values.items():
        if name not in args:
            continue
        value = args[name]
        if type(value) is not str or len(value) > limit or re.fullmatch(pattern, value) is None:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        cleaned[name] = value

    pieces = [f"newer_than:{days}d"]
    if "sender" in cleaned:
        pieces.append(f'from:"{cleaned["sender"]}"')
    if "subject_terms" in cleaned:
        pieces.append(f'subject:"{cleaned["subject_terms"]}"')
    if "search_text" in cleaned:
        pieces.append(f'"{cleaned["search_text"]}"')
    # Reuse the lower transport contract as a final independent bound.
    return search_arguments({"q": " ".join(pieces), "max_results": maximum})


def project_search(raw: Any, maximum: int) -> dict[str, Any]:
    """Return unique message references without provider pagination fields."""
    normalized = normalize_search(raw, maximum)
    identifiers = [item["id"] for item in normalized["messages"]]
    if len(set(identifiers)) != len(identifiers):
        raise CredentialError(CredentialFailure.READ_RESPONSE)
    estimated = normalized.get("result_size_estimate", len(identifiers))
    return {
        "message_ids": identifiers,
        "returned": len(identifiers),
        "more_available": (
            "next_page_token" in normalized or estimated > len(identifiers)
        ),
    }


def _text(value: str, *, characters: int, escaped: int) -> tuple[str, bool]:
    """Bound both visible characters and deterministic JSON expansion."""
    result: list[str] = []
    remaining = escaped
    for character in value[:characters]:
        number = ord(character)
        cost = (12 if number > 0xFFFF else 6 if number > 0x7F else
                2 if character in '\n\t"\\' else 1)
        if remaining < cost:
            break
        remaining -= cost
        result.append(character)
    text = "".join(result)
    return text, len(text) < len(value)


def project_message(raw: Any, message_id: str) -> dict[str, Any]:
    """Project one normalized message onto the first-stage agent surface.

    Attachment names, MIME types, sizes, labels, thread ids, provider history
    ids and pagination state are all intentionally absent.
    """
    normalized = normalize_message(raw, message_id)
    truncated = bool(normalized["truncated"])
    headers: dict[str, str] = {}
    limits = {
        "from": (320, 1600),
        "to": (320, 1600),
        "cc": (320, 1600),
        "reply_to": (320, 1600),
        "subject": (256, 1600),
        "date": (128, 512),
    }
    for name, (characters, escaped) in limits.items():
        if name not in normalized["headers"]:
            continue
        value, shortened = _text(
            normalized["headers"][name], characters=characters, escaped=escaped)
        headers[name] = value
        truncated = truncated or shortened

    snippet, short_snippet = _text(
        normalized["snippet"], characters=256, escaped=1600)
    body, short_body = _text(
        normalized["body"], characters=MAX_AGENT_BODY_CHARS, escaped=12_000)
    truncated = truncated or short_snippet or short_body
    result = {
        "message_id": normalized["id"],
        "headers": headers,
        "snippet": snippet,
        "body": body,
        "body_source": normalized["body_source"],
        "has_attachments": bool(
            normalized["attachments"]
            or normalized["attachments_omitted"]
            or normalized["omitted_body_parts"]
        ),
        "truncated": truncated,
    }
    if len(json.dumps(result, ensure_ascii=True).encode("ascii")) > MAX_AGENT_RESULT_BYTES:
        raise CredentialError(CredentialFailure.READ_CONTENT)
    return result
