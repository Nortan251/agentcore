"""Recovery decision model.

The existing ``RecoveryStrategy`` Protocol returns a bool, which is enough for
the runner's retry hook but cannot express "replan" or "escalate". Rather than
replacing that stable Protocol, this module defines a richer decision model
that sits *behind* it: policies expose both ``decide()`` (full decision) and
``should_retry()`` (Protocol-compatible view of the same decision).

Nothing here performs an action. A decision is a recommendation that the
orchestration layer acts on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from agentcore.util.clock import utc_now


class FailureCategory(str, Enum):
    """Why the step is being considered for recovery."""

    #: Temporary fault; the same action may well succeed next time.
    TRANSIENT = "transient"
    #: Deterministic fault; repeating the identical call cannot help.
    PERSISTENT = "persistent"
    #: The tool ran, but verification showed the goal was not achieved.
    VERIFICATION_FAILED = "verification_failed"
    #: The tool ran, but there is not enough evidence to judge the outcome.
    UNVERIFIED = "unverified"
    #: Blocked by the permission policy. Never retried around.
    PERMISSION_DENIED = "permission_denied"
    #: The same failure keeps recurring, or attempts are exhausted.
    REPEATED = "repeated"
    #: Nothing is wrong.
    NONE = "none"

    def __str__(self) -> str:
        return self.value


class RecoveryAction(str, Enum):
    """What the orchestration layer should do next."""

    RETRY = "retry"
    REPLAN = "replan"
    ESCALATE = "escalate"
    STOP = "stop"
    NONE = "none"

    def __str__(self) -> str:
        return self.value


class RetrySafety(str, Enum):
    """Whether repeating the action could cause harm.

    ``UNKNOWN`` is deliberately distinct from ``UNSAFE``: the system did not
    establish that a repeat is harmful, only that it cannot prove it is safe.
    Both block an automatic retry, but they are reported differently.
    """

    SAFE = "safe"
    UNKNOWN = "unknown"
    UNSAFE = "unsafe"

    def __str__(self) -> str:
        return self.value


@dataclass
class Escalation:
    """A structured hand-off to a human.

    Deliberately transport-free: real notification channels (email, chat,
    desktop) are future integrations. This records *what* a human must decide.
    """

    reason: str
    question: str
    subtask_id: str | None = None
    attempted: list[str] = field(default_factory=list)
    unresolved: str = ""
    safe_to_pause: bool = True
    created_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "question": self.question,
            "subtask_id": self.subtask_id,
            "attempted": list(self.attempted),
            "unresolved": self.unresolved,
            "safe_to_pause": self.safe_to_pause,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Escalation:
        raw = data.get("created_at")
        return cls(
            reason=data.get("reason", ""),
            question=data.get("question", ""),
            subtask_id=data.get("subtask_id"),
            attempted=list(data.get("attempted", [])),
            unresolved=data.get("unresolved", ""),
            safe_to_pause=bool(data.get("safe_to_pause", True)),
            created_at=datetime.fromisoformat(raw) if raw else utc_now(),
        )


@dataclass
class RecoveryDecision:
    """The outcome of evaluating a failed or unverified step."""

    action: RecoveryAction
    category: FailureCategory
    reason: str
    subtask_id: str | None = None
    attempt: int = 0
    delay_seconds: float = 0.0
    safety: RetrySafety = RetrySafety.UNKNOWN
    escalation: Escalation | None = None
    created_at: datetime = field(default_factory=utc_now)

    # -- convenience -------------------------------------------------------
    @property
    def should_retry(self) -> bool:
        """Protocol-compatible view: only RETRY means "run it again"."""
        return self.action is RecoveryAction.RETRY

    @property
    def requests_replan(self) -> bool:
        return self.action is RecoveryAction.REPLAN

    @property
    def requests_escalation(self) -> bool:
        return self.action is RecoveryAction.ESCALATE

    @property
    def is_terminal(self) -> bool:
        """True when no further automatic progress is possible."""
        return self.action in (RecoveryAction.STOP, RecoveryAction.ESCALATE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "category": self.category.value,
            "reason": self.reason,
            "subtask_id": self.subtask_id,
            "attempt": self.attempt,
            "delay_seconds": round(self.delay_seconds, 3),
            "safety": self.safety.value,
            "escalation": self.escalation.to_dict() if self.escalation else None,
            "created_at": self.created_at.isoformat(),
        }

    # -- builders ----------------------------------------------------------
    @classmethod
    def retry(cls, category: FailureCategory, reason: str, *,
              subtask_id: str | None = None, attempt: int = 0,
              delay_seconds: float = 0.0,
              safety: RetrySafety = RetrySafety.SAFE) -> RecoveryDecision:
        return cls(RecoveryAction.RETRY, category, reason,
                   subtask_id=subtask_id, attempt=attempt,
                   delay_seconds=delay_seconds, safety=safety)

    @classmethod
    def replan(cls, category: FailureCategory, reason: str, *,
               subtask_id: str | None = None, attempt: int = 0,
               safety: RetrySafety = RetrySafety.UNKNOWN) -> RecoveryDecision:
        return cls(RecoveryAction.REPLAN, category, reason,
                   subtask_id=subtask_id, attempt=attempt, safety=safety)

    @classmethod
    def escalate(cls, category: FailureCategory, reason: str, *,
                 escalation: Escalation, subtask_id: str | None = None,
                 attempt: int = 0,
                 safety: RetrySafety = RetrySafety.UNKNOWN) -> RecoveryDecision:
        return cls(RecoveryAction.ESCALATE, category, reason,
                   subtask_id=subtask_id, attempt=attempt, safety=safety,
                   escalation=escalation)

    @classmethod
    def stop(cls, category: FailureCategory, reason: str, *,
             subtask_id: str | None = None, attempt: int = 0,
             safety: RetrySafety = RetrySafety.UNKNOWN) -> RecoveryDecision:
        return cls(RecoveryAction.STOP, category, reason,
                   subtask_id=subtask_id, attempt=attempt, safety=safety)

    @classmethod
    def none(cls, reason: str = "nothing to recover") -> RecoveryDecision:
        return cls(RecoveryAction.NONE, FailureCategory.NONE, reason,
                   safety=RetrySafety.SAFE)
