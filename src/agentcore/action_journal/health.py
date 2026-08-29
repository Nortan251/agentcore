"""Typed runtime health for the durable action domain.

This is deliberately separate from :class:`ActionState`: quarantine describes
whether this runtime may trust the whole journal, never the lifecycle truth of
one action record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionDomainHealthState(str, Enum):
    """Whether journal-required mutations may enter their durable path."""

    VALIDATION_PENDING = "validation_pending"
    HEALTHY = "healthy"
    QUARANTINED = "quarantined"

    def __str__(self) -> str:
        return self.value


_QUARANTINE_CATEGORIES = frozenset({
    "journal_corrupt",
    "startup_scan_bound_exceeded",
})


@dataclass(frozen=True)
class ActionDomainHealth:
    """Immutable snapshot of the executor's startup mutation gate."""

    state: ActionDomainHealthState
    error_category: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ActionDomainHealthState):
            raise TypeError("action domain health state is invalid")
        if self.state is ActionDomainHealthState.QUARANTINED:
            if self.error_category not in _QUARANTINE_CATEGORIES:
                raise ValueError("action domain quarantine category is invalid")
        elif self.error_category is not None:
            raise ValueError(
                "healthy or pending action domain carries a quarantine "
                "category")

    @classmethod
    def validation_pending(cls) -> ActionDomainHealth:
        return cls(ActionDomainHealthState.VALIDATION_PENDING)

    @classmethod
    def healthy(cls) -> ActionDomainHealth:
        return cls(ActionDomainHealthState.HEALTHY)

    @classmethod
    def quarantined(cls, category: str) -> ActionDomainHealth:
        safe_category = (
            category
            if category in _QUARANTINE_CATEGORIES
            else "journal_corrupt"
        )
        return cls(
            ActionDomainHealthState.QUARANTINED,
            error_category=safe_category,
        )


__all__ = [
    "ActionDomainHealth",
    "ActionDomainHealthState",
]
