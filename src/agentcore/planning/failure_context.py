"""Structured failure history rendered as replanning context.

The orchestration layer already persists everything needed to replan well:

* :class:`agentcore.core.replan.ReplanLedger` — superseded plans, replan
  reasons and outcomes (accepted / rejected / error).
* :class:`agentcore.recovery.history.RecoveryHistory` — failure categories,
  retry-safety verdicts and the decisions recovery took.

Until now the planner ignored both and sent only a coarse summary, so repeated
replans could re-propose an approach already shown to fail. This module reads
that existing state and turns it into bounded, redacted context.

Boundaries:

* **Read-only.** Nothing here writes state; the planner does not own memory.
* **Bounded.** Reuses the caps the state layer already enforces rather than
  inventing another history system, and truncates every rendered string.
* **Redacted.** Tool arguments pass through the project's existing log
  redaction before they can reach a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from agentcore.observability.logging import redact
from agentcore.tasks.models import Task

#: Historical attempts rendered, most recent first. The ledger already caps
#: what it stores (MAX_ARCHIVED_PLANS); this bounds what reaches the model.
MAX_HISTORY_ATTEMPTS = 5

#: Steps rendered per historical attempt.
MAX_STEPS_PER_ATTEMPT = 8

#: Recovery decisions rendered in total, most recent first.
MAX_RECOVERY_DECISIONS = 8

#: Rejected proposals rendered, most recent first. The ledger already caps
#: stored records (MAX_ARCHIVED_PLANS); this bounds what reaches the model.
MAX_REJECTED_PROPOSALS = 5

#: Character cap for any single rendered string (descriptions, reasons,
#: results, error messages).
MAX_TEXT = 200


def _clip(value: Any, limit: int = MAX_TEXT) -> str:
    """Collapse whitespace and truncate. Never returns None."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _safe_args(args: Any) -> dict[str, Any]:
    """Redact tool arguments before they can reach a model prompt."""
    if not isinstance(args, Mapping):
        return {}
    cleaned = redact(dict(args))
    # Values may themselves be large; clip anything stringy.
    return {str(k): (_clip(v, 80) if isinstance(v, str) else v)
            for k, v in cleaned.items()}


@dataclass
class StepSummary:
    """One step of an attempted plan, as the model should see it."""

    description: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    error: str = ""
    verification: str = ""
    result: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"description": self.description,
                                   "status": self.status}
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.tool_args:
            payload["tool_args"] = self.tool_args
        if self.error:
            payload["error"] = self.error
        if self.verification:
            payload["verification"] = self.verification
        if self.result:
            payload["result"] = self.result
        return payload


@dataclass
class AttemptSummary:
    """One previously attempted approach and why it did not work."""

    attempt: int
    task_id: str | None
    outcome: str                       # failed | rejected | error | superseded
    steps: list[StepSummary] = field(default_factory=list)
    reason: str = ""                   # why a replan was requested
    detail: str = ""                   # why the replan itself was rejected

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt": self.attempt,
            "outcome": self.outcome,
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class RejectedProposal:
    """A plan the orchestrator refused *before* it ever ran.

    Deliberately distinct from :class:`AttemptSummary`: an executed failure and
    a refused proposal are different facts, and conflating them would tell the
    model something untrue about what happened.
    """

    attempt: int
    reason: str                    # why it was refused
    kind: str = "rejected"         # "rejected" | "rejected_duplicate"
    strategy: str = ""             # tool names only, never argument values

    #: Always false; a rejected proposal never reached the runner.
    executed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt": self.attempt,
            "kind": self.kind,
            "reason": self.reason,
            "executed": self.executed,
        }
        if self.strategy:
            payload["strategy"] = self.strategy
        return payload


@dataclass
class FailureContext:
    """Everything the planner should know before proposing a new approach."""

    objective: str = ""
    latest: AttemptSummary | None = None
    previous: list[AttemptSummary] = field(default_factory=list)
    rejected: list[RejectedProposal] = field(default_factory=list)
    recovery_decisions: list[dict[str, Any]] = field(default_factory=list)
    failure_categories: list[str] = field(default_factory=list)
    replan_count: int = 0
    replans_remaining: int | None = None

    @property
    def has_history(self) -> bool:
        return bool(self.latest or self.previous or self.recovery_decisions
                    or self.rejected)

    @property
    def has_rejections(self) -> bool:
        return bool(self.rejected)

    @property
    def permission_blocked(self) -> bool:
        return "permission_denied" in self.failure_categories

    @property
    def repeated_failure(self) -> bool:
        return "repeated" in self.failure_categories

    @property
    def only_transient(self) -> bool:
        """True when every recorded category is a transient fault.

        This is the case where re-proposing the same approach is legitimate.
        """
        return bool(self.failure_categories) and set(
            self.failure_categories) == {"transient"}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "replanning": True,          # kept: callers/tests key off this
            "objective": self.objective,
            "replan_attempt": self.replan_count,
        }
        if self.replans_remaining is not None:
            payload["replans_remaining"] = self.replans_remaining
        if self.latest is not None:
            payload["latest_failed_attempt"] = self.latest.to_dict()
        if self.previous:
            payload["earlier_failed_attempts"] = [a.to_dict()
                                                  for a in self.previous]
        if self.rejected:
            payload["rejected_proposals"] = [r.to_dict() for r in self.rejected]
        if self.recovery_decisions:
            payload["recovery_decisions"] = self.recovery_decisions
        if self.failure_categories:
            payload["failure_categories"] = self.failure_categories
        return payload


# -- extraction -------------------------------------------------------------

def _summarise_subtask(sub: Any) -> StepSummary:
    """Summarise a live :class:`Subtask` object."""
    error = sub.errors[-1].message if getattr(sub, "errors", None) else ""
    verification = ""
    verdict = getattr(sub, "verification", None)
    if verdict is not None and str(verdict.status) != "unverified":
        verification = f"{verdict.status}: {verdict.detail}"
    elif verdict is not None and verdict.detail:
        verification = f"{verdict.status}: {verdict.detail}"
    return StepSummary(
        description=_clip(sub.description),
        tool_name=sub.tool_name,
        tool_args=_safe_args(sub.tool_args),
        status=str(sub.status),
        error=_clip(error),
        verification=_clip(verification),
        result=_clip(sub.result, 120) if sub.result is not None else "",
    )


def _summarise_plan_dict(data: Mapping[str, Any]) -> list[StepSummary]:
    """Summarise a serialised task (an archived plan) defensively."""
    steps: list[StepSummary] = []
    raw_steps = data.get("subtasks")
    if not isinstance(raw_steps, list):
        return steps
    for raw in raw_steps[:MAX_STEPS_PER_ATTEMPT]:
        if not isinstance(raw, Mapping):
            continue
        errors = raw.get("errors")
        error = ""
        if isinstance(errors, list) and errors:
            last = errors[-1]
            if isinstance(last, Mapping):
                error = str(last.get("message", ""))
        verification = ""
        verdict = raw.get("verification")
        if isinstance(verdict, Mapping):
            status = str(verdict.get("status", ""))
            if status and status != "unverified":
                verification = f"{status}: {verdict.get('detail', '')}"
        steps.append(StepSummary(
            description=_clip(raw.get("description")),
            tool_name=raw.get("tool_name") if isinstance(
                raw.get("tool_name"), str) else None,
            tool_args=_safe_args(raw.get("tool_args")),
            status=_clip(raw.get("status"), 32),
            error=_clip(error),
            verification=_clip(verification),
        ))
    return steps


#: Substrings that suggest a stored detail may carry an argument value. The
#: orchestrator's own rejection reasons are argument-free by construction, but
#: a validation message could quote one, so such details are replaced rather
#: than rendered.
_UNSAFE_DETAIL_MARKERS: tuple[str, ...] = (
    "api_key", "apikey", "token", "password", "secret", "authorization",
)


def _safe_detail(detail: Any) -> str:
    """Return a rejection detail that cannot carry a credential.

    Rejection reasons are generated by the orchestration layer and name only
    tools, but they are free text, so anything mentioning a credential-shaped
    key is withheld instead of rendered.
    """
    text = _clip(detail)
    lowered = text.lower()
    if any(marker in lowered for marker in _UNSAFE_DETAIL_MARKERS):
        return "<rejection detail withheld: may contain sensitive values>"
    return text


def _collect_rejections(ledger: Any) -> list[RejectedProposal]:
    """Extract proposals the orchestrator refused before execution.

    Reads the existing ledger records — no second store. Tolerant of legacy
    records written before ``signature`` existed and of malformed entries.
    """
    rejections: list[RejectedProposal] = []
    for record in getattr(ledger, "records", []) or []:
        try:
            outcome = str(getattr(record, "outcome", ""))
            if not outcome.startswith("rejected"):
                continue
            rejections.append(RejectedProposal(
                attempt=int(getattr(record, "attempt", 0) or 0),
                kind=outcome,
                reason=_safe_detail(getattr(record, "detail", "")),
                strategy=_clip(getattr(record, "signature", "") or "", 120),
            ))
        except Exception:  # pragma: no cover - a bad record must not crash
            continue
    # Most recent first, then bound what reaches the model.
    rejections.reverse()
    return rejections[:MAX_REJECTED_PROPOSALS]


def _collect_recovery(state: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Pull recovery decisions and failure categories from persisted state."""
    from agentcore.recovery.history import RecoveryHistory

    try:
        history = RecoveryHistory.load(state)
    except Exception:  # pragma: no cover - defensive against corrupt state
        return [], []

    decisions: list[dict[str, Any]] = []
    categories: list[str] = []
    for record in history.to_dict().values():
        if not isinstance(record, Mapping):
            continue
        entries = record.get("decisions")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            category = str(entry.get("category", ""))
            decisions.append({
                "action": str(entry.get("action", "")),
                "category": category,
                "safety": str(entry.get("safety", "")),
                "attempt": entry.get("attempt", 0),
                "reason": _clip(entry.get("reason")),
            })
            if category and category not in categories:
                categories.append(category)

    # Most recent first, then bound what reaches the model.
    decisions.reverse()
    return decisions[:MAX_RECOVERY_DECISIONS], categories


def build_failure_context(task: Task, state: Any, *,
                          max_replans: int | None = None) -> FailureContext:
    """Assemble replanning context from the task and persisted state.

    Tolerant by design: missing, partial or corrupt history yields a smaller
    context rather than an exception, because a replan must never fail merely
    because history could not be read.
    """
    from agentcore.core.replan import ReplanLedger

    context = FailureContext(objective=getattr(task, "objective", ""))

    # 1. The attempt that just failed.
    subtasks = list(getattr(task, "subtasks", []) or [])
    context.latest = AttemptSummary(
        attempt=0,
        task_id=getattr(task, "id", None),
        outcome=str(getattr(task, "status", "failed")),
        steps=[_summarise_subtask(s) for s in subtasks[:MAX_STEPS_PER_ATTEMPT]],
    )

    # 2. Earlier attempts, from the replan ledger.
    try:
        ledger = ReplanLedger.load(state)
    except Exception:  # pragma: no cover - defensive
        ledger = ReplanLedger()

    context.replan_count = ledger.count
    if max_replans is not None:
        context.replans_remaining = ledger.remaining(max_replans)

    records_by_task: dict[str, Any] = {}
    for record in ledger.records:
        if record.previous_task_id:
            records_by_task[record.previous_task_id] = record

    latest_id = getattr(task, "id", None)
    previous: list[AttemptSummary] = []
    for index, plan in enumerate(ledger.archived_plans, start=1):
        if not isinstance(plan, Mapping):
            continue
        plan_id = plan.get("id")
        # The orchestrator archives the failing plan *before* calling replan,
        # so the newest archive is the attempt reported as "latest". Skip it
        # rather than describing the same attempt twice.
        if plan_id and plan_id == latest_id:
            continue
        record = records_by_task.get(plan_id) if plan_id else None
        # ``record.outcome`` describes the *replan* ("accepted"/"rejected"),
        # not the fate of this plan. Report the plan's own status so the model
        # is not told a failed approach was "accepted".
        outcome = _clip(plan.get("status"), 32) or "superseded"
        previous.append(AttemptSummary(
            attempt=index,
            task_id=plan_id if isinstance(plan_id, str) else None,
            outcome=outcome,
            steps=_summarise_plan_dict(plan),
            reason=_clip(record.reason) if record else "",
            detail=_clip(record.detail) if record else "",
        ))

    # Most recent first, bounded.
    previous.reverse()
    context.previous = previous[:MAX_HISTORY_ATTEMPTS]

    # 3. Proposals the orchestrator refused before they could run. These are
    #    kept separate from `previous`, which only holds executed attempts.
    context.rejected = _collect_rejections(ledger)

    # 4. Recovery classifications.
    context.recovery_decisions, context.failure_categories = _collect_recovery(
        state)
    return context
