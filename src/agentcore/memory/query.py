"""Deterministic retrieval: the query, the result, and the ranking rule.

There is no semantic similarity here and no invented relevance score. Every
match is explainable in one sentence, and the ordering is fully determined by
the data — the same store and query always produce the same list, which is what
makes memory testable before embeddings exist.

Ranking (applied in order, highest first)
-----------------------------------------
1. **Match strength** - an exact tag match beats an exact content match, which
   beats a substring match. A caller asking for tag ``python`` means something
   more precise than a record that merely contains the word.
2. **Scope specificity** - SESSION > PROJECT > GLOBAL. A fact recorded for
   *this* project outranks a general one.
3. **Trust** - USER_PROVIDED > VERIFIED > INFERRED > UNCERTAIN.
4. **Recency** - newer ``updated_at`` first.
5. **Id** - a stable tiebreak so ties never reorder between runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Sequence

from agentcore.memory.models import (
    MemoryRecord,
    MemoryScope,
    MemoryType,
    _aware,
    normalise_tags,
)


class MatchKind(str, Enum):
    """Why a record matched — reported, never guessed."""

    #: The query string equals one of the record's tags.
    TAG = "tag"
    #: The query equals the whole content (case-insensitive, trimmed).
    EXACT = "exact"
    #: The query appears somewhere in the content.
    SUBSTRING = "substring"
    #: No text query was supplied; the record matched the filters alone.
    FILTER = "filter"

    @property
    def strength(self) -> int:
        return _MATCH_STRENGTH[self]

    def __str__(self) -> str:
        return self.value


_MATCH_STRENGTH: dict[MatchKind, int] = {
    MatchKind.TAG: 3,
    MatchKind.EXACT: 2,
    MatchKind.SUBSTRING: 1,
    MatchKind.FILTER: 0,
}


@dataclass(frozen=True)
class MemoryQuery:
    """What to look for. Every field is optional and ANDed together.

    An empty query matches every live record, which is how a caller lists
    memory without a second API.
    """

    #: Free text compared against tags and content, case-insensitively.
    text: str | None = None
    types: tuple[MemoryType, ...] = ()
    scopes: tuple[MemoryScope, ...] = ()
    #: Restrict to a namespace. ``include_global`` decides whether broader
    #: global facts come along, which is usually what a caller wants.
    scope_key: str | None = None
    include_global: bool = True
    #: A record must carry *all* of these tags.
    tags: tuple[str, ...] = ()
    limit: int | None = None
    #: Expired records are hidden by default; retention is deterministic, so
    #: an inspector can still ask to see them.
    include_expired: bool = False

    def matches_filters(self, record: MemoryRecord, *,
                        now: datetime | None = None) -> bool:
        if not self.include_expired and record.is_expired(now=now):
            return False
        if self.types and record.type not in self.types:
            return False
        if self.scopes and record.scope not in self.scopes:
            return False
        if self.scope_key is not None:
            is_global = record.scope is MemoryScope.GLOBAL
            if is_global:
                if not self.include_global:
                    return False
            elif record.scope_key != self.scope_key:
                return False
        if self.tags:
            wanted = set(normalise_tags(self.tags))
            if not wanted.issubset(set(record.tags)):
                return False
        return True

    def match_kind(self, record: MemoryRecord) -> MatchKind | None:
        """How this record matches the text, or None if it does not."""
        if not (self.text or "").strip():
            return MatchKind.FILTER
        needle = " ".join(self.text.split()).lower()
        if needle in record.tags:
            return MatchKind.TAG
        content = " ".join(record.content.split()).lower()
        if content == needle:
            return MatchKind.EXACT
        if needle in content:
            return MatchKind.SUBSTRING
        return None


@dataclass(frozen=True)
class MemoryHit:
    """One retrieved record plus why it was retrieved.

    ``reason`` is a plain sentence rather than a number: it states the actual
    cause of the match instead of implying a similarity the system cannot
    compute.
    """

    record: MemoryRecord
    match: MatchKind
    reason: str

    @property
    def id(self) -> str:
        return self.record.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "match": self.match.value,
            "reason": self.reason,
        }

    def describe(self) -> dict[str, Any]:
        """Log-safe: identity and match, never the content."""
        return {**self.record.describe(), "match": self.match.value}


@dataclass(frozen=True)
class MemorySearchResult:
    """The outcome of a search: ordered hits plus honest counts."""

    hits: tuple[MemoryHit, ...] = ()
    #: Matches before ``limit`` was applied, so a caller can tell it is seeing
    #: a truncated view rather than everything.
    total_matched: int = 0
    truncated: bool = False
    query: MemoryQuery = field(default_factory=MemoryQuery)

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def __bool__(self) -> bool:
        return bool(self.hits)

    @property
    def records(self) -> tuple[MemoryRecord, ...]:
        return tuple(hit.record for hit in self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "total_matched": self.total_matched,
            "truncated": self.truncated,
        }


def _reason_for(kind: MatchKind, query: MemoryQuery) -> str:
    text = " ".join((query.text or "").split())
    if kind is MatchKind.TAG:
        return f"tag matches '{text}'"
    if kind is MatchKind.EXACT:
        return f"content exactly matches '{text}'"
    if kind is MatchKind.SUBSTRING:
        return f"content contains '{text}'"
    parts = []
    if query.types:
        parts.append("type " + "/".join(t.value for t in query.types))
    if query.scopes:
        parts.append("scope " + "/".join(s.value for s in query.scopes))
    if query.scope_key:
        parts.append(f"scope key '{query.scope_key}'")
    if query.tags:
        parts.append("tags " + ",".join(normalise_tags(query.tags)))
    return "matched filters: " + "; ".join(parts) if parts else "no filters applied"


def rank_key(hit: MemoryHit) -> tuple:
    """The documented ordering, as a sort key (descending)."""
    record = hit.record
    return (
        hit.match.strength,
        record.scope.specificity,
        record.trust.rank,
        _aware(record.updated_at).timestamp(),
    )


def search_records(records: Iterable[MemoryRecord], query: MemoryQuery, *,
                   now: datetime | None = None,
                   max_results: int | None = None) -> MemorySearchResult:
    """Apply ``query`` to ``records`` using the documented deterministic rules.

    Pure: it reads nothing and writes nothing, so any backend can reuse it and
    the ranking cannot drift between implementations.
    """
    hits: list[MemoryHit] = []
    for record in records:
        if not query.matches_filters(record, now=now):
            continue
        kind = query.match_kind(record)
        if kind is None:
            continue
        hits.append(MemoryHit(record=record, match=kind,
                              reason=_reason_for(kind, query)))

    # Sort by the rank key descending, then by id ascending as a stable
    # tiebreak. Two passes keep both directions correct without inverting ids.
    hits.sort(key=lambda h: h.record.id)
    hits.sort(key=rank_key, reverse=True)

    total = len(hits)
    limit = query.limit if query.limit is not None else max_results
    if limit is not None and limit >= 0:
        kept = hits[:limit]
    else:
        kept = hits
    return MemorySearchResult(hits=tuple(kept), total_matched=total,
                              truncated=len(kept) < total, query=query)
