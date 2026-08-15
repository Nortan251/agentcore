"""Deterministic executable-plan signatures and duplicate detection.

The planner is shown its previous failures (task 7), but being *told* is not
the same as being *prevented*. This module lets the orchestration layer detect
that a freshly proposed plan is the same executable strategy as one that
already failed, and refuse it before it can run.

An **executable signature** captures what a plan would actually *do*:

    ((tool_name, normalised_args), ...)   in dependency-respecting order

Prose is deliberately excluded — descriptions, expected results, rationale,
generated ids and timestamps all vary between model calls without changing
behaviour, so including them would make every duplicate look novel.

Matching is exact and deterministic: no embeddings, no fuzzy thresholds, no
extra model call. It answers one narrow question — "would this run the same
calls with the same arguments?" — and is conservative everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agentcore.recovery.decision import FailureCategory
from agentcore.tasks.models import Task

#: Categories where repeating the identical executable strategy is pointless
#: or unsafe. A duplicate matching one of these is rejected.
BLOCKING_CATEGORIES: frozenset[FailureCategory] = frozenset({
    FailureCategory.PERSISTENT,
    FailureCategory.VERIFICATION_FAILED,
    FailureCategory.PERMISSION_DENIED,
    FailureCategory.REPEATED,
})

#: Categories where repeating the same plan is legitimate: a transient fault
#: says nothing about the approach, and an unverified outcome is insufficient
#: evidence to condemn it.
NON_BLOCKING_CATEGORIES: frozenset[FailureCategory] = frozenset({
    FailureCategory.TRANSIENT,
    FailureCategory.UNVERIFIED,
    FailureCategory.NONE,
})

#: Marker for a value that cannot be normalised deterministically. Two such
#: values never compare equal, so an unrepresentable argument makes the plan
#: look *different* — the conservative direction (allow, don't wrongly reject).
_UNREPRESENTABLE = "<unrepresentable>"


def normalize_value(value: Any, _depth: int = 0) -> Any:
    """Convert a tool-argument value into a deterministic, comparable form.

    Mappings become key-sorted tuples so ``{"a":1,"b":2}`` and ``{"b":2,"a":1}``
    are identical. Sequences keep their order, because argument order is
    semantically meaningful. Primitives pass through unchanged.

    Anything else (a custom object, a cyclic structure, excessive nesting) is
    marked unrepresentable rather than coerced via ``str()``, which could make
    two genuinely different values collide.
    """
    if _depth > 10:
        return _UNREPRESENTABLE
    # bool is an int subclass *and* True == 1, so simply returning the value
    # would make {"flag": True} and {"flag": 1} identical signatures. Tag it so
    # the two stay distinct — tools treat them as different argument types.
    if isinstance(value, bool):
        return ("bool", value)
    if value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        # Integral floats compare equal to their int form in Python, so keep
        # the numeric value; this makes 1.0 and 1 the same argument, which is
        # the behaviour a tool would see.
        return value
    if isinstance(value, Mapping):
        return tuple(sorted(
            ((str(k), normalize_value(v, _depth + 1)) for k, v in value.items()),
            key=lambda item: item[0]))
    if isinstance(value, (list, tuple)):
        return tuple(normalize_value(v, _depth + 1) for v in value)
    if isinstance(value, (set, frozenset)):
        # Sets have no meaningful order; sort by repr for a stable form.
        try:
            return tuple(sorted(
                (normalize_value(v, _depth + 1) for v in value), key=repr))
        except Exception:  # pragma: no cover - defensive
            return _UNREPRESENTABLE
    return _UNREPRESENTABLE


def normalize_args(args: Any) -> tuple:
    """Normalise a tool-argument mapping into a comparable tuple."""
    if not isinstance(args, Mapping):
        return ()
    return normalize_value(args)  # type: ignore[return-value]


#: One executable step: the tool and its normalised arguments.
StepSignature = tuple[str, tuple]

#: A whole plan's executable intent.
PlanSignature = tuple[StepSignature, ...]


def _ordered_steps(subtasks: Sequence[Any], *,
                   from_dict: bool) -> list[tuple[str, Any]]:
    """Return executable ``(tool_name, tool_args)`` pairs in a stable order.

    Subtasks are stored in dependency-respecting insertion order by
    ``Task.add_subtask`` / ``LLMPlanner.to_task``, so that order is used
    directly. Steps with no tool are skipped: they execute nothing, so they
    cannot distinguish two executable strategies.
    """
    steps: list[tuple[str, Any]] = []
    for sub in subtasks:
        if from_dict:
            if not isinstance(sub, Mapping):
                continue
            tool = sub.get("tool_name")
            args = sub.get("tool_args")
        else:
            tool = getattr(sub, "tool_name", None)
            args = getattr(sub, "tool_args", None)
        if not isinstance(tool, str) or not tool:
            continue
        steps.append((tool, args))
    return steps


def plan_signature(task: Task) -> PlanSignature:
    """Executable signature of a live :class:`Task`."""
    return tuple(
        (tool, normalize_args(args))
        for tool, args in _ordered_steps(getattr(task, "subtasks", []) or [],
                                         from_dict=False))


def signature_from_dict(data: Mapping[str, Any]) -> PlanSignature:
    """Executable signature of a serialised task (an archived plan)."""
    subtasks = data.get("subtasks")
    if not isinstance(subtasks, list):
        return ()
    return tuple(
        (tool, normalize_args(args))
        for tool, args in _ordered_steps(subtasks, from_dict=True))


def describe_signature(signature: PlanSignature) -> str:
    """Short, secret-free description of a signature, for rejection reasons.

    Only tool names are included — argument values may contain sensitive data
    and are never rendered here.
    """
    if not signature:
        return "(no executable steps)"
    return " -> ".join(tool for tool, _ in signature)


def classify_archived_plan(data: Mapping[str, Any]) -> FailureCategory:
    """Classify why an archived plan failed, reusing the recovery taxonomy.

    The archived plan is a serialised ``Task``, so it is rehydrated and passed
    through the existing :func:`agentcore.recovery.classify.classify` rather
    than re-implementing failure classification here.

    Returns the most blocking category found across the plan's steps, or
    ``NONE`` when nothing conclusive is recorded.
    """
    from agentcore.recovery.classify import classify

    try:
        task = Task.from_dict(dict(data))
    except Exception:
        # Corrupt archive: no evidence, so no grounds to reject.
        return FailureCategory.NONE

    worst = FailureCategory.NONE
    for subtask in task.subtasks:
        try:
            category = classify(subtask)
        except Exception:  # pragma: no cover - defensive
            continue
        if category is FailureCategory.PERMISSION_DENIED:
            return category          # most decisive; stop looking
        if category in BLOCKING_CATEGORIES:
            worst = category
        elif worst is FailureCategory.NONE and category in NON_BLOCKING_CATEGORIES:
            worst = category
    return worst


@dataclass(frozen=True)
class DuplicateMatch:
    """An archived plan whose executable signature matches a new proposal."""

    task_id: str | None
    category: FailureCategory
    signature: PlanSignature
    occurrences: int = 1

    @property
    def blocking(self) -> bool:
        return self.category in BLOCKING_CATEGORIES

    def reason(self) -> str:
        """Explain the rejection without exposing argument values."""
        where = f"attempt '{self.task_id}'" if self.task_id else "an earlier attempt"
        repeat = (f" and has now failed {self.occurrences} times"
                  if self.occurrences > 1 else "")
        return (
            f"the proposed plan has the same executable strategy as {where} "
            f"({describe_signature(self.signature)}), which failed with a "
            f"non-transient '{self.category}' failure{repeat}; propose a "
            f"different executable strategy (different tools, ordering or "
            f"arguments), not a rewording of the same one")


def find_duplicate(new_task: Task,
                   archived_plans: Sequence[Mapping[str, Any]],
                   *, current_task_id: str | None = None
                   ) -> DuplicateMatch | None:
    """Find a blocking archived plan matching ``new_task``'s signature.

    Conservative by design — ``None`` (allow the plan) is returned whenever:

    * the new plan has no executable steps to compare;
    * no archived signature matches;
    * the matching archive failed only transiently, was unverified, or carries
      no conclusive failure evidence.

    A false rejection would stall a legitimate retry, so ambiguity always
    resolves in favour of letting the plan run — the permission, verification
    and recovery layers remain the real safety net.
    """
    signature = plan_signature(new_task)
    if not signature:
        return None

    matches: list[tuple[Mapping[str, Any], FailureCategory]] = []
    for plan in archived_plans:
        if not isinstance(plan, Mapping):
            continue
        if signature_from_dict(plan) != signature:
            continue
        matches.append((plan, classify_archived_plan(plan)))

    if not matches:
        return None

    blocking = [(plan, category) for plan, category in matches
                if category in BLOCKING_CATEGORIES]
    if not blocking:
        # Every match failed transiently / inconclusively: repeating is fair.
        return None

    # Report the most recent blocking match; archives are appended in order.
    plan, category = blocking[-1]
    task_id = plan.get("id")
    return DuplicateMatch(
        task_id=task_id if isinstance(task_id, str) else None,
        category=category,
        signature=signature,
        occurrences=len(blocking),
    )
