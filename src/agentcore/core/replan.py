"""Replan bookkeeping for the orchestration loop.

Tracks how many times ``Planner.replan`` has been invoked for a run and keeps
the superseded plans, so state tells the whole story:

    original plan -> failure -> recovery -> replan -> new plan -> result

Like :mod:`agentcore.recovery.history`, this lives inside the existing
``AgentState.scratch`` dictionary rather than introducing a second store, so it
is serialised and restored by the existing state machinery for free.

The budget itself is *not* defined here: it comes from the existing
``RecoveryConfig.max_replans``. This module only counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentcore.tasks.models import Task
from agentcore.util.clock import utc_now


class ReplanOutcome(str, Enum):
    """What the orchestration loop should do after asking the planner."""

    #: A valid, materially new plan was adopted; execute it.
    ADOPTED = "adopted"
    #: The plan repeated an already-failed executable strategy. The attempt is
    #: spent, but the replan request stands — ask again if budget remains.
    RETRY = "retry"
    #: The planner produced nothing usable; stop and report honestly.
    ABANDON = "abandon"

    def __str__(self) -> str:
        return self.value

#: Key under ``AgentState.scratch`` holding the ledger.
SCRATCH_KEY = "replan"

#: Cap on archived plans kept in state. The counter stays exact regardless.
MAX_ARCHIVED_PLANS = 10


@dataclass
class ReplanRecord:
    """One replan cycle: why it happened and what came of it."""

    attempt: int
    reason: str
    outcome: str                      # "accepted" | "rejected" |
                                      # "rejected_duplicate" | "error"
    previous_task_id: str | None = None
    new_task_id: str | None = None
    detail: str = ""
    #: Safe description of the *proposed* strategy (tool names only, never
    #: argument values). Populated for rejections so the planner can later be
    #: told what it proposed; empty for legacy records and accepted replans.
    signature: str = ""
    at: str = field(default_factory=lambda: utc_now().isoformat())

    @property
    def rejected(self) -> bool:
        return self.outcome.startswith("rejected")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "reason": self.reason,
            "outcome": self.outcome,
            "previous_task_id": self.previous_task_id,
            "new_task_id": self.new_task_id,
            "detail": self.detail,
            "signature": self.signature,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReplanRecord:
        return cls(
            attempt=int(data.get("attempt", 0)),
            reason=data.get("reason", ""),
            outcome=data.get("outcome", ""),
            previous_task_id=data.get("previous_task_id"),
            new_task_id=data.get("new_task_id"),
            detail=data.get("detail", ""),
            # Absent in records written before this field existed.
            signature=data.get("signature", "") or "",
            at=data.get("at", ""),
        )


class ReplanLedger:
    """Read/modify/write view over the replan section of agent state."""

    def __init__(self, count: int = 0,
                 records: list[ReplanRecord] | None = None,
                 archived_plans: list[dict[str, Any]] | None = None) -> None:
        self.count = count
        self.records: list[ReplanRecord] = records or []
        self.archived_plans: list[dict[str, Any]] = archived_plans or []

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, state) -> ReplanLedger:
        raw = state.scratch.get(SCRATCH_KEY)
        if not isinstance(raw, dict):
            return cls()
        records = [ReplanRecord.from_dict(r)
                   for r in raw.get("records", []) if isinstance(r, dict)]
        archived = [p for p in raw.get("archived_plans", [])
                    if isinstance(p, dict)]
        return cls(count=int(raw.get("count", 0)), records=records,
                   archived_plans=archived)

    def save(self, state) -> None:
        state.scratch[SCRATCH_KEY] = {
            "count": self.count,
            "records": [r.to_dict() for r in self.records],
            "archived_plans": self.archived_plans,
        }

    # -- queries -----------------------------------------------------------
    def exhausted(self, max_replans: int) -> bool:
        """True when no further replan is permitted.

        ``count`` is the number of replans already *attempted*, so a run with
        ``max_replans = 1`` may call the planner exactly once.
        """
        return self.count >= max_replans

    def remaining(self, max_replans: int) -> int:
        return max(0, max_replans - self.count)

    # -- mutation ----------------------------------------------------------
    def begin(self, reason: str, previous: Task | None) -> int:
        """Consume one unit of budget and archive the plan being replaced.

        Called *before* the planner runs so that a planner that raises still
        counts as an attempted replan — that is what keeps a persistently
        failing planner bounded.
        """
        self.count += 1
        if previous is not None:
            self.archived_plans.append(previous.to_dict())
            if len(self.archived_plans) > MAX_ARCHIVED_PLANS:
                del self.archived_plans[:-MAX_ARCHIVED_PLANS]
        return self.count

    def finish(self, *, reason: str, outcome: str,
               previous_task_id: str | None = None,
               new_task_id: str | None = None, detail: str = "",
               signature: str = "") -> ReplanRecord:
        record = ReplanRecord(
            attempt=self.count, reason=reason, outcome=outcome,
            previous_task_id=previous_task_id, new_task_id=new_task_id,
            detail=detail, signature=signature)
        self.records.append(record)
        if len(self.records) > MAX_ARCHIVED_PLANS:
            del self.records[:-MAX_ARCHIVED_PLANS]
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "records": [r.to_dict() for r in self.records],
            "archived_plans": self.archived_plans,
        }
