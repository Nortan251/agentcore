"""Reviewed Gmail bounds and inert argument validation, with no I/O."""

from __future__ import annotations

import re
from typing import Any

from agentcore.credentials.errors import CredentialError, CredentialFailure


DEFAULT_RESULTS = 10
MAX_RESULTS = 50
MAX_QUERY_CHARS = 1024
MAX_PAGE_TOKEN_CHARS = 2048
MAX_ID_CHARS = 128
ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"
# Opaque page tokens can contain punctuation, but never whitespace/control
# characters. They are always query-encoded, never interpolated into a path.
PAGE_TOKEN_PATTERN = r"[\x21-\x7e]{1,2048}"
QUERY_PATTERN = r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]*"

MAX_JSON_BYTES = 1_048_576
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 16_384
MAX_MIME_DEPTH = 8
MAX_MIME_PARTS = 100
MAX_HEADERS = 200
MAX_HEADER_NAME_CHARS = 128
MAX_HEADER_VALUE_CHARS = 4096
MAX_PART_BYTES = 65_536
MAX_BODY_BYTES = 131_072
MAX_BODY_CHARS = 8000
MAX_OUTPUT_CHARS = 12_000
MAX_OUTPUT_ESCAPED_CHARS = 40_000
MAX_ATTACHMENTS = 20
MAX_LABELS = 16


def reference(value: Any, *, response: bool = False) -> str:
    if type(value) is not str or len(value) > MAX_ID_CHARS or re.fullmatch(ID_PATTERN, value) is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE if response else CredentialFailure.READ_ARGUMENTS)
    return value


def page_token(value: Any, *, response: bool = False) -> str:
    if type(value) is not str or len(value) > MAX_PAGE_TOKEN_CHARS or re.fullmatch(PAGE_TOKEN_PATTERN, value) is None:
        raise CredentialError(CredentialFailure.READ_RESPONSE if response else CredentialFailure.READ_ARGUMENTS)
    return value


def search_arguments(args: dict[str, Any]) -> dict[str, Any]:
    if type(args) is not dict or not set(args) <= {"q", "max_results", "page_token"}:
        raise CredentialError(CredentialFailure.READ_ARGUMENTS)
    maximum = args.get("max_results", DEFAULT_RESULTS)
    if type(maximum) is not int or not 1 <= maximum <= MAX_RESULTS:
        raise CredentialError(CredentialFailure.READ_ARGUMENTS)
    result: dict[str, Any] = {"max_results": maximum}
    if "q" in args:
        query = args["q"]
        if type(query) is not str or len(query) > MAX_QUERY_CHARS or re.fullmatch(QUERY_PATTERN, query) is None:
            raise CredentialError(CredentialFailure.READ_ARGUMENTS)
        result["q"] = query
    if "page_token" in args:
        result["page_token"] = page_token(args["page_token"])
    return result
