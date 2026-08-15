"""Structured failure-history extraction and rendering for replanning."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.core.replan import ReplanLedger
from agentcore.planning.failure_context import (
    MAX_HISTORY_ATTEMPTS,
    MAX_RECOVERY_DECISIONS,
    MAX_STEPS_PER_ATTEMPT,
    MAX_TEXT,
    AttemptSummary,
    FailureContext,
    StepSummary,
    build_failure_context,
)
from agentcore.planning.prompt import (
    REPLAN_GUIDANCE,
    build_system_prompt,
    build_user_prompt,
)
from agentcore.recovery.decision import (
    Escalation,
    FailureCategory,
    RecoveryDecision,
)
from agentcore.recovery.history import RecoveryHistory
from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)

SECRET = "sk-super-secret-value"


# -- fixtures ---------------------------------------------------------------

def failed_task(objective="goal", *, tool="echo", args=None,
                error="boom", verification=VerificationStatus.FAILED) -> Task:
    task = Task(objective=objective)
    sub = task.add_subtask(Subtask(description="do the thing", tool_name=tool,
                                   tool_args=args or {"message": "hi"}))
    sub.transition_to(TaskStatus.RUNNING)
    if error:
        sub.record_error(ErrorRecord(type="ToolFailure", message=error))
        sub.transition_to(TaskStatus.FAILED)
    else:
        sub.result = "some output"
        sub.transition_to(TaskStatus.COMPLETED)
    sub.verification = VerificationResult(status=verification,
                                          checked_by="V", detail="detail here")
    return task


def state_with(task: Task) -> AgentState:
    state = AgentState()
    state.set_task(task)
    return state


def archive(state: AgentState, task: Task, *, reason="verification failed",
            outcome="accepted") -> None:
    ledger = ReplanLedger.load(state)
    ledger.begin(reason, task)
    ledger.finish(reason=reason, outcome=outcome,
                  previous_task_id=task.id, new_task_id="task_new")
    ledger.save(state)


def add_recovery(state: AgentState, subtask_id: str,
                 category: FailureCategory, action="replan") -> None:
    history = RecoveryHistory.load(state)
    builder = {"replan": RecoveryDecision.replan,
               "retry": RecoveryDecision.retry}.get(action)
    if builder is not None:
        decision = builder(category, f"{action} because {category}",
                           subtask_id=subtask_id)
    else:
        decision = RecoveryDecision.escalate(
            category, f"escalate because {category}",
            escalation=Escalation("r", "q"), subtask_id=subtask_id)
    history.record(subtask_id, decision, signature=f"sig-{category}")
    history.save(state)


# -- extraction: latest attempt --------------------------------------------

def test_latest_attempt_is_captured():
    task = failed_task()
    context = build_failure_context(task, state_with(task))

    assert context.latest is not None
    assert context.latest.task_id == task.id
    assert len(context.latest.steps) == 1
    step = context.latest.steps[0]
    assert step.tool_name == "echo"
    assert step.error == "boom"
    assert "failed" in step.verification


def test_objective_is_carried():
    task = failed_task("record the launch date")
    assert build_failure_context(task, state_with(task)).objective == \
        "record the launch date"


def test_no_history_yields_minimal_context():
    task = failed_task()
    context = build_failure_context(task, state_with(task))
    assert context.previous == []
    assert context.replan_count == 0
    assert context.has_history is True     # the latest failure counts


def test_empty_task_is_handled():
    task = Task(objective="nothing")
    context = build_failure_context(task, state_with(task))
    assert context.latest is not None
    assert context.latest.steps == []


# -- extraction: earlier attempts ------------------------------------------

def test_previous_attempts_are_extracted():
    first = failed_task(tool="echo", args={"message": "approach A"})
    state = state_with(first)
    archive(state, first)

    second = failed_task(tool="echo", args={"message": "approach B"})
    state.set_task(second)
    context = build_failure_context(second, state)

    assert len(context.previous) == 1
    assert context.previous[0].steps[0].tool_args["message"] == "approach A"
    assert context.latest.steps[0].tool_args["message"] == "approach B"


def test_latest_attempt_is_not_duplicated_in_history():
    """The orchestrator archives the failing plan before calling replan."""
    task = failed_task()
    state = state_with(task)
    archive(state, task)                     # archives the *current* task

    context = build_failure_context(task, state)
    assert context.previous == [], "the latest attempt must not repeat"
    assert context.latest.task_id == task.id


def test_previous_attempts_are_most_recent_first():
    state = AgentState()
    ids = []
    for i in range(3):
        task = failed_task(tool="echo", args={"message": f"approach {i}"})
        ids.append(task.id)
        state.set_task(task)
        archive(state, task)
    current = failed_task()
    state.set_task(current)

    context = build_failure_context(current, state)
    order = [a.task_id for a in context.previous]
    assert order == list(reversed(ids))


def test_replan_reason_is_included():
    task = failed_task()
    state = state_with(task)
    archive(state, task, reason="the approach cannot work")
    state.set_task(failed_task())

    context = build_failure_context(state.current_task, state)
    assert context.previous[0].reason == "the approach cannot work"


def test_rejected_replan_detail_is_included():
    task = failed_task()
    state = state_with(task)
    ledger = ReplanLedger.load(state)
    ledger.begin("r", task)
    ledger.finish(reason="r", outcome="rejected", previous_task_id=task.id,
                  detail="planner returned the same task")
    ledger.save(state)
    state.set_task(failed_task())

    context = build_failure_context(state.current_task, state)
    assert "same task" in context.previous[0].detail


def test_previous_outcome_reports_plan_status_not_replan_outcome():
    """A failed approach must not be described as 'accepted'."""
    task = failed_task()
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.FAILED)
    state = state_with(task)
    archive(state, task, outcome="accepted")
    state.set_task(failed_task())

    context = build_failure_context(state.current_task, state)
    assert context.previous[0].outcome == "failed"


# -- extraction: recovery history ------------------------------------------

def test_recovery_decisions_are_extracted():
    task = failed_task()
    state = state_with(task)
    add_recovery(state, task.subtasks[0].id, FailureCategory.VERIFICATION_FAILED)

    context = build_failure_context(task, state)
    assert context.recovery_decisions
    assert context.recovery_decisions[0]["category"] == "verification_failed"
    assert context.failure_categories == ["verification_failed"]


@pytest.mark.parametrize("category,flag", [
    (FailureCategory.PERMISSION_DENIED, "permission_blocked"),
    (FailureCategory.REPEATED, "repeated_failure"),
])
def test_category_flags(category, flag):
    task = failed_task()
    state = state_with(task)
    add_recovery(state, task.subtasks[0].id, category)
    context = build_failure_context(task, state)
    assert getattr(context, flag) is True


def test_only_transient_detected():
    task = failed_task()
    state = state_with(task)
    add_recovery(state, task.subtasks[0].id, FailureCategory.TRANSIENT,
                 action="retry")
    context = build_failure_context(task, state)
    assert context.only_transient is True


def test_mixed_categories_are_not_only_transient():
    task = failed_task()
    state = state_with(task)
    sid = task.subtasks[0].id
    add_recovery(state, sid, FailureCategory.TRANSIENT, action="retry")
    add_recovery(state, sid, FailureCategory.PERSISTENT)
    context = build_failure_context(task, state)
    assert context.only_transient is False
    assert set(context.failure_categories) == {"transient", "persistent"}


def test_replan_budget_reported():
    task = failed_task()
    state = state_with(task)
    archive(state, task)
    context = build_failure_context(state.current_task, state, max_replans=3)
    assert context.replan_count == 1
    assert context.replans_remaining == 2


# -- robustness -------------------------------------------------------------

def test_missing_scratch_is_safe():
    task = failed_task()
    state = AgentState()
    state.set_task(task)
    state.scratch.clear()
    context = build_failure_context(task, state)
    assert context.previous == []
    assert context.recovery_decisions == []


@pytest.mark.parametrize("corrupt", [
    "not a dict", 12345, [], {"count": "many"}, {"records": "nope"},
    {"archived_plans": "nope"}, {"archived_plans": [None, 5, "x"]},
])
def test_corrupt_replan_scratch_does_not_crash(corrupt):
    task = failed_task()
    state = state_with(task)
    state.scratch["replan"] = corrupt
    context = build_failure_context(task, state)
    assert context.latest is not None


@pytest.mark.parametrize("corrupt", [
    "not a dict", [], {"s1": "bad"}, {"s1": {"decisions": "bad"}},
    {"s1": {"decisions": [None, "x"]}},
])
def test_corrupt_recovery_scratch_does_not_crash(corrupt):
    task = failed_task()
    state = state_with(task)
    state.scratch["recovery"] = corrupt
    context = build_failure_context(task, state)
    assert context.recovery_decisions == []


def test_archived_plan_missing_subtasks_is_safe():
    task = failed_task()
    state = state_with(task)
    state.scratch["replan"] = {"count": 1, "records": [],
                               "archived_plans": [{"id": "old"}]}
    context = build_failure_context(task, state)
    assert context.previous[0].steps == []


# -- bounds -----------------------------------------------------------------

def test_history_attempts_are_bounded():
    state = AgentState()
    for i in range(MAX_HISTORY_ATTEMPTS * 3):
        task = failed_task(tool="echo", args={"message": f"a{i}"})
        state.set_task(task)
        archive(state, task)
    current = failed_task()
    state.set_task(current)

    context = build_failure_context(current, state)
    assert len(context.previous) == MAX_HISTORY_ATTEMPTS


def test_steps_per_attempt_are_bounded():
    task = Task(objective="many")
    for i in range(MAX_STEPS_PER_ATTEMPT * 2):
        task.add_subtask(Subtask(description=f"step {i}", tool_name="echo"))
    context = build_failure_context(task, state_with(task))
    assert len(context.latest.steps) == MAX_STEPS_PER_ATTEMPT


def test_recovery_decisions_are_bounded():
    task = failed_task()
    state = state_with(task)
    sid = task.subtasks[0].id
    for _ in range(MAX_RECOVERY_DECISIONS * 3):
        add_recovery(state, sid, FailureCategory.TRANSIENT, action="retry")
    context = build_failure_context(task, state)
    assert len(context.recovery_decisions) == MAX_RECOVERY_DECISIONS


def test_long_text_is_truncated():
    long_error = "E" * (MAX_TEXT * 5)
    task = failed_task(error=long_error)
    context = build_failure_context(task, state_with(task))
    assert len(context.latest.steps[0].error) <= MAX_TEXT + 1


def test_large_result_is_truncated():
    task = failed_task(error="")
    task.subtasks[0].result = "R" * 5000
    context = build_failure_context(task, state_with(task))
    assert len(context.latest.steps[0].result) <= 121


# -- redaction --------------------------------------------------------------

def test_secrets_in_tool_args_are_redacted():
    task = failed_task(args={"api_key": SECRET, "message": "hello"})
    context = build_failure_context(task, state_with(task))
    payload = json.dumps(context.to_dict())
    assert SECRET not in payload
    assert "***" in payload
    assert "hello" in payload


def test_secrets_in_archived_plans_are_redacted():
    old = failed_task(args={"token": SECRET, "message": "old"})
    state = state_with(old)
    archive(state, old)
    state.set_task(failed_task())

    context = build_failure_context(state.current_task, state)
    assert SECRET not in json.dumps(context.to_dict())


def test_secret_absent_from_rendered_prompt():
    task = failed_task(args={"password": SECRET, "message": "hi"})
    context = build_failure_context(task, state_with(task))
    prompt = build_user_prompt("goal", [{"name": "echo"}],
                               context=context.to_dict())
    assert SECRET not in prompt


# -- prompt rendering -------------------------------------------------------

def build_prompt_for(context: FailureContext) -> str:
    return build_user_prompt(context.objective or "goal",
                             [{"name": "echo", "description": "Echo."}],
                             context=context.to_dict())


def test_prompt_labels_latest_and_earlier_sections():
    old = failed_task(tool="echo", args={"message": "approach A"},
                      error="A failed")
    state = state_with(old)
    archive(state, old, reason="approach A was ineffective")
    new = failed_task(tool="echo", args={"message": "approach B"},
                      error="B failed")
    state.set_task(new)

    prompt = build_prompt_for(build_failure_context(new, state))

    assert "LATEST FAILED ATTEMPT" in prompt
    assert "EARLIER FAILED ATTEMPTS" in prompt
    # both approaches and both reasons are visible
    assert "approach A" in prompt and "approach B" in prompt
    assert "A failed" in prompt and "B failed" in prompt
    assert "approach A was ineffective" in prompt
    # the latest section precedes the earlier one
    assert prompt.index("LATEST FAILED ATTEMPT") < prompt.index(
        "EARLIER FAILED ATTEMPTS")


def test_prompt_includes_recovery_classification():
    task = failed_task()
    state = state_with(task)
    add_recovery(state, task.subtasks[0].id, FailureCategory.PERSISTENT)
    prompt = build_prompt_for(build_failure_context(task, state))

    assert "RECORDED FAILURE CATEGORIES" in prompt
    assert "persistent" in prompt
    assert "RECOVERY DECISIONS ALREADY TAKEN" in prompt
    assert "retry_safety=" in prompt


def test_prompt_includes_verification_detail():
    task = failed_task(error="")
    context = build_failure_context(task, state_with(task))
    assert "verification: failed" in build_prompt_for(context)


def test_ordinary_planning_prompt_has_no_history_sections():
    prompt = build_user_prompt("goal", [{"name": "echo"}])
    assert "LATEST FAILED ATTEMPT" not in prompt
    assert "EARLIER FAILED ATTEMPTS" not in prompt


def test_non_history_context_still_rendered_as_json():
    prompt = build_user_prompt("goal", [{"name": "echo"}],
                               context={"source": "cli"})
    assert "ADDITIONAL CONTEXT" in prompt
    assert "cli" in prompt


# -- system guidance --------------------------------------------------------

def test_replan_system_prompt_adds_guidance():
    base = build_system_prompt()
    replan = build_system_prompt(replanning=True)
    assert "REPLANNING" not in base
    assert REPLAN_GUIDANCE in replan
    assert base in replan          # base rules are preserved


def test_guidance_discourages_repeating_failed_approaches():
    guidance = build_system_prompt(replanning=True)
    assert "Do not repeat an approach the evidence shows has already failed" \
        in guidance


def test_guidance_permits_repeating_after_transient_failure():
    """The instruction must not demand a different plan unconditionally."""
    guidance = build_system_prompt(replanning=True)
    assert "transient" in guidance
    assert "repeating it is acceptable" in guidance


def test_guidance_forbids_permission_workarounds():
    guidance = build_system_prompt(replanning=True)
    assert "permission_denied" in guidance
    assert "circumvent" in guidance


def test_guidance_distinguishes_all_failure_types():
    guidance = build_system_prompt(replanning=True)
    for term in ("transient", "persistent", "verification failed", "repeated",
                 "permission_denied"):
        assert term in guidance


# -- dataclasses ------------------------------------------------------------

def test_step_summary_omits_empty_fields():
    payload = StepSummary(description="d", status="failed").to_dict()
    assert "error" not in payload and "tool_name" not in payload


def test_attempt_summary_serialisation():
    payload = AttemptSummary(attempt=1, task_id="t", outcome="failed",
                             steps=[StepSummary(description="d")],
                             reason="r").to_dict()
    assert payload["reason"] == "r"
    assert payload["steps"][0]["description"] == "d"


def test_failure_context_dict_marks_replanning():
    context = FailureContext(objective="o")
    assert context.to_dict()["replanning"] is True
    assert context.has_history is False
