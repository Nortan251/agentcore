"""Recovery history.

Records what recovery has already tried, so the policy can detect repeating
failures and enforce replan/escalation budgets instead of looping blindly.

There is no second source of truth: the history lives inside the existing
``AgentState.scratch`` dictionary, which is already serialised by
``AgentState.to_dict`` and restored by ``from_dict``. That means recovery
history survives checkpoint/resume for free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentcore.recovery.decision import RecoveryAction, RecoveryDecision
from agentcore.state.models import AgentState
from agentcore.util.clock import utc_now

#: Key under ``AgentState.scratch`` where the history is stored.
SCRATCH_KEY = "recovery"

#: Cap on retained entries per subtask, so a long run cannot grow state without
#: bound. Counters are kept separately and remain exact.
MAX_ENTRIES_PER_SUBTASK = 20


@dataclass
class SubtaskRecoveryRecord:
    """Everything recovery knows about one subtask's troubled history."""

    decisions: list[dict[str, Any]] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    retries: int = 0
    replans: int = 0
    escalations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": self.decisions,
            "signatures": self.signatures,
            "retries": self.retries,
            "replans": self.replans,
            "escalations": self.escalations,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubtaskRecoveryRecord:
        return cls(
            decisions=list(data.get("decisions", [])),
            signatures=list(data.get("signatures", [])),
            retries=int(data.get("retries", 0)),
            replans=int(data.get("replans", 0)),
            escalations=int(data.get("escalations", 0)),
        )


class RecoveryHistory:
    """Read/modify/write view over the recovery section of agent state."""

    def __init__(self, records: dict[str, SubtaskRecoveryRecord] | None = None
                 ) -> None:
        self._records: dict[str, SubtaskRecoveryRecord] = records or {}

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, state: AgentState) -> RecoveryHistory:
        raw = state.scratch.get(SCRATCH_KEY)
        if not isinstance(raw, dict):
            return cls()
        records = {
            subtask_id: SubtaskRecoveryRecord.from_dict(entry)
            for subtask_id, entry in raw.items()
            if isinstance(entry, dict)
        }
        return cls(records)

    def save(self, state: AgentState) -> None:
        state.scratch[SCRATCH_KEY] = {
            subtask_id: record.to_dict()
            for subtask_id, record in self._records.items()
        }

    # -- queries -----------------------------------------------------------
    def record_for(self, subtask_id: str) -> SubtaskRecoveryRecord:
        return self._records.setdefault(subtask_id, SubtaskRecoveryRecord())

    def retries(self, subtask_id: str) -> int:
        return self.record_for(subtask_id).retries

    def replans(self, subtask_id: str) -> int:
        return self.record_for(subtask_id).replans

    def escalations(self, subtask_id: str) -> int:
        return self.record_for(subtask_id).escalations

    def repeats(self, subtask_id: str, signature: str) -> bool:
        """True when this exact failure has already been seen for this subtask.

        This is what distinguishes "try again, conditions may differ" from
        "the identical thing failed identically", which no retry budget should
        paper over.
        """
        if signature == "none":
            return False
        return signature in self.record_for(subtask_id).signatures

    def summary(self, subtask_id: str) -> list[str]:
        """Human-readable list of what has been tried, for escalations."""
        record = self.record_for(subtask_id)
        return [f"{entry.get('action')}: {entry.get('reason')}"
                for entry in record.decisions]

    # -- mutation ----------------------------------------------------------
    def record(self, subtask_id: str, decision: RecoveryDecision, *,
               signature: str = "") -> None:
        """Append a decision and update the counters it affects."""
        if decision.action is RecoveryAction.NONE:
            return

        record = self.record_for(subtask_id)
        record.decisions.append({
            "action": decision.action.value,
            "category": decision.category.value,
            "reason": decision.reason,
            "attempt": decision.attempt,
            "safety": decision.safety.value,
            "at": utc_now().isoformat(),
        })
        if len(record.decisions) > MAX_ENTRIES_PER_SUBTASK:
            del record.decisions[:-MAX_ENTRIES_PER_SUBTASK]

        if signature and signature != "none":
            record.signatures.append(signature)
            if len(record.signatures) > MAX_ENTRIES_PER_SUBTASK:
                del record.signatures[:-MAX_ENTRIES_PER_SUBTASK]

        if decision.action is RecoveryAction.RETRY:
            record.retries += 1
        elif decision.action is RecoveryAction.REPLAN:
            record.replans += 1
        elif decision.action is RecoveryAction.ESCALATE:
            record.escalations += 1

    def to_dict(self) -> dict[str, Any]:
        return {sid: rec.to_dict() for sid, rec in self._records.items()}
