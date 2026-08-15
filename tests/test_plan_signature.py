"""Executable-plan signatures, normalisation and duplicate detection."""

from __future__ import annotations

import pytest

from agentcore.core.plan_signature import (
    BLOCKING_CATEGORIES,
    NON_BLOCKING_CATEGORIES,
    DuplicateMatch,
    classify_archived_plan,
    describe_signature,
    find_duplicate,
    normalize_args,
    normalize_value,
    plan_signature,
    signature_from_dict,
)
from agentcore.recovery.decision import FailureCategory
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)


# -- helpers ----------------------------------------------------------------

def task_with(*steps, objective="goal") -> Task:
    """steps: (tool_name, args) or (tool_name, args, description)."""
    task = Task(objective=objective)
    for i, step in enumerate(steps):
        tool, args = step[0], step[1]
        description = step[2] if len(step) > 2 else f"step {i}"
        task.add_subtask(Subtask(description=description, tool_name=tool,
                                 tool_args=dict(args) if args else {}))
    return task


def failed_archive(task: Task, *, error: str | None = None,
                   error_type: str = "ToolFailure", retryable: bool = False,
                   verification: VerificationStatus | None = None) -> dict:
    """Run a task's subtasks to a failed/verified state and serialise it."""
    for sub in task.subtasks:
        sub.transition_to(TaskStatus.RUNNING)
        if error is not None:
            sub.record_error(ErrorRecord(type=error_type, message=error,
                                         retryable=retryable))
            sub.transition_to(TaskStatus.FAILED)
        else:
            sub.result = "output"
            sub.transition_to(TaskStatus.COMPLETED)
        if verification is not None:
            sub.verification = VerificationResult(status=verification,
                                                  checked_by="V",
                                                  detail="detail")
    return task.to_dict()


# ===========================================================================
# Normalisation
# ===========================================================================

def test_dict_key_order_is_normalised():
    assert normalize_value({"a": 1, "b": 2}) == normalize_value({"b": 2, "a": 1})


def test_nested_dict_key_order_is_normalised():
    left = {"outer": {"x": 1, "y": {"p": 1, "q": 2}}}
    right = {"outer": {"y": {"q": 2, "p": 1}, "x": 1}}
    assert normalize_value(left) == normalize_value(right)


def test_list_order_is_significant():
    assert normalize_value([1, 2]) != normalize_value([2, 1])


def test_tuple_and_list_are_equivalent():
    assert normalize_value([1, 2]) == normalize_value((1, 2))


@pytest.mark.parametrize("value", ["text", 42, 3.5, None])
def test_primitives_pass_through(value):
    assert normalize_value(value) == value


@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_deterministic_and_tagged(value):
    """Booleans are tagged so True cannot collide with the integer 1."""
    assert normalize_value(value) == normalize_value(value)
    assert normalize_value(value) != value  # tagged, not raw


def test_bool_and_int_stay_distinct():
    """bool is an int subclass; True must not equal 1 in a signature."""
    assert normalize_value({"f": True}) != normalize_value({"f": 1})


def test_distinct_values_produce_distinct_forms():
    assert normalize_value({"a": 1}) != normalize_value({"a": 2})
    assert normalize_value({"a": 1}) != normalize_value({"b": 1})


def test_sets_are_order_independent():
    assert normalize_value({1, 2, 3}) == normalize_value({3, 2, 1})


def test_unrepresentable_value_is_marked_not_coerced():
    class Custom:
        pass

    normalised = normalize_value(Custom())
    assert normalised == "<unrepresentable>"


def test_deep_nesting_is_bounded():
    deep: dict = {"k": "v"}
    for _ in range(30):
        deep = {"k": deep}
    assert normalize_value(deep) is not None    # does not recurse forever


def test_normalize_args_rejects_non_mapping():
    assert normalize_args("not a dict") == ()
    assert normalize_args(None) == ()
    assert normalize_args([1, 2]) == ()


# ===========================================================================
# Signatures
# ===========================================================================

def test_signature_captures_tools_and_args():
    task = task_with(("echo", {"message": "hi"}))
    assert plan_signature(task) == (("echo", (("message", "hi"),)),)


def test_signature_ignores_descriptions_and_ids():
    a = task_with(("echo", {"m": 1}, "write the thing"))
    b = task_with(("echo", {"m": 1}, "a totally different wording"))
    assert a.id != b.id
    assert plan_signature(a) == plan_signature(b)


def test_signature_ignores_key_ordering():
    a = task_with(("echo", {"a": 1, "b": 2}))
    b = task_with(("echo", {"b": 2, "a": 1}))
    assert plan_signature(a) == plan_signature(b)


def test_signature_differs_by_tool():
    assert plan_signature(task_with(("echo", {}))) != \
        plan_signature(task_with(("other", {})))


def test_signature_differs_by_argument():
    assert plan_signature(task_with(("echo", {"m": "a"}))) != \
        plan_signature(task_with(("echo", {"m": "b"})))


def test_signature_differs_by_step_order():
    a = task_with(("one", {}), ("two", {}))
    b = task_with(("two", {}), ("one", {}))
    assert plan_signature(a) != plan_signature(b)


def test_signature_differs_by_added_step():
    a = task_with(("echo", {"m": 1}))
    b = task_with(("echo", {"m": 1}), ("echo", {"m": 2}))
    assert plan_signature(a) != plan_signature(b)


def test_toolless_steps_are_ignored():
    """A prose-only step executes nothing and cannot distinguish strategies."""
    with_prose = Task(objective="g")
    with_prose.add_subtask(Subtask(description="think about it"))
    with_prose.add_subtask(Subtask(description="do", tool_name="echo",
                                   tool_args={"m": 1}))
    assert plan_signature(with_prose) == plan_signature(
        task_with(("echo", {"m": 1})))


def test_empty_plan_has_empty_signature():
    assert plan_signature(Task(objective="g")) == ()


def test_signature_from_dict_matches_live_task():
    task = task_with(("echo", {"b": 2, "a": 1}), ("other", {"x": [1, 2]}))
    assert signature_from_dict(task.to_dict()) == plan_signature(task)


def test_signature_from_dict_handles_corrupt_input():
    assert signature_from_dict({}) == ()
    assert signature_from_dict({"subtasks": "nope"}) == ()
    assert signature_from_dict({"subtasks": [None, 5, {"tool_name": None}]}) == ()


def test_describe_signature_lists_tools_only():
    task = task_with(("echo", {"api_key": "sk-secret"}), ("other", {}))
    described = describe_signature(plan_signature(task))
    assert described == "echo -> other"
    assert "sk-secret" not in described


def test_describe_empty_signature():
    assert describe_signature(()) == "(no executable steps)"


# ===========================================================================
# Archived-plan classification
# ===========================================================================

def test_verification_failure_classified():
    archive = failed_archive(task_with(("echo", {})),
                             verification=VerificationStatus.FAILED)
    assert classify_archived_plan(archive) is FailureCategory.VERIFICATION_FAILED


def test_transient_error_classified():
    archive = failed_archive(task_with(("echo", {})), error="timeout",
                             retryable=True)
    assert classify_archived_plan(archive) is FailureCategory.TRANSIENT


def test_persistent_error_classified():
    archive = failed_archive(task_with(("echo", {})), error="bad args",
                             error_type="validation")
    assert classify_archived_plan(archive) is FailureCategory.PERSISTENT


def test_permission_denial_classified():
    archive = failed_archive(task_with(("echo", {})), error="denied",
                             error_type="permission_denied")
    assert classify_archived_plan(archive) is FailureCategory.PERMISSION_DENIED


def test_corrupt_archive_classifies_as_none():
    assert classify_archived_plan({"garbage": True}) is FailureCategory.NONE


def test_category_sets_are_disjoint_and_complete():
    assert not (BLOCKING_CATEGORIES & NON_BLOCKING_CATEGORIES)
    assert BLOCKING_CATEGORIES | NON_BLOCKING_CATEGORIES == set(FailureCategory)


# ===========================================================================
# Duplicate detection
# ===========================================================================

def test_duplicate_after_verification_failure_is_blocked():
    original = task_with(("echo", {"m": "same"}))
    archive = failed_archive(original, verification=VerificationStatus.FAILED)
    proposal = task_with(("echo", {"m": "same"}))

    match = find_duplicate(proposal, [archive])
    assert match is not None
    assert match.blocking
    assert match.category is FailureCategory.VERIFICATION_FAILED
    assert match.task_id == original.id


def test_duplicate_after_transient_failure_is_allowed():
    """A timeout says nothing about the approach; repeating is legitimate."""
    archive = failed_archive(task_with(("echo", {"m": "same"})),
                             error="timeout", retryable=True)
    proposal = task_with(("echo", {"m": "same"}))
    assert find_duplicate(proposal, [archive]) is None


def test_duplicate_after_persistent_failure_is_blocked():
    archive = failed_archive(task_with(("echo", {"m": "x"})),
                             error="bad args", error_type="validation")
    assert find_duplicate(task_with(("echo", {"m": "x"})), [archive]) is not None


def test_duplicate_after_permission_denial_is_blocked():
    archive = failed_archive(task_with(("echo", {"m": "x"})),
                             error="denied", error_type="permission_denied")
    match = find_duplicate(task_with(("echo", {"m": "x"})), [archive])
    assert match is not None
    assert match.category is FailureCategory.PERMISSION_DENIED


def test_unverified_outcome_does_not_block():
    """Insufficient evidence must not cause a false rejection."""
    archive = failed_archive(task_with(("echo", {"m": "x"})),
                             verification=VerificationStatus.UNVERIFIED)
    assert find_duplicate(task_with(("echo", {"m": "x"})), [archive]) is None


def test_no_failure_evidence_does_not_block():
    plain = task_with(("echo", {"m": "x"})).to_dict()
    assert find_duplicate(task_with(("echo", {"m": "x"})), [plain]) is None


def test_different_plan_is_allowed():
    archive = failed_archive(task_with(("echo", {"m": "a"})),
                             verification=VerificationStatus.FAILED)
    assert find_duplicate(task_with(("echo", {"m": "b"})), [archive]) is None
    assert find_duplicate(task_with(("other", {"m": "a"})), [archive]) is None


def test_reordered_keys_still_match():
    archive = failed_archive(task_with(("echo", {"a": 1, "b": 2})),
                             verification=VerificationStatus.FAILED)
    assert find_duplicate(task_with(("echo", {"b": 2, "a": 1})),
                          [archive]) is not None


def test_different_description_still_matches():
    archive = failed_archive(task_with(("echo", {"m": 1}, "approach one")),
                             verification=VerificationStatus.FAILED)
    proposal = task_with(("echo", {"m": 1}, "a brand new sounding approach"))
    assert find_duplicate(proposal, [archive]) is not None


def test_empty_proposal_is_never_a_duplicate():
    archive = failed_archive(task_with(("echo", {})),
                             verification=VerificationStatus.FAILED)
    assert find_duplicate(Task(objective="g"), [archive]) is None


def test_no_archives_means_no_duplicate():
    assert find_duplicate(task_with(("echo", {})), []) is None


def test_corrupt_archives_are_skipped():
    archives = ["nope", 42, None, {"subtasks": "bad"}]
    assert find_duplicate(task_with(("echo", {})), archives) is None


def test_repeated_occurrences_are_counted():
    a = failed_archive(task_with(("echo", {"m": 1})),
                       verification=VerificationStatus.FAILED)
    b = failed_archive(task_with(("echo", {"m": 1})),
                       verification=VerificationStatus.FAILED)
    match = find_duplicate(task_with(("echo", {"m": 1})), [a, b])
    assert match is not None
    assert match.occurrences == 2
    assert "failed 2 times" in match.reason()


def test_blocking_match_wins_over_transient_match():
    """One transient and one verification failure of the same plan: block."""
    transient = failed_archive(task_with(("echo", {"m": 1})),
                               error="timeout", retryable=True)
    verified = failed_archive(task_with(("echo", {"m": 1})),
                              verification=VerificationStatus.FAILED)
    assert find_duplicate(task_with(("echo", {"m": 1})),
                          [transient, verified]) is not None


def test_rejection_reason_is_explanatory_and_secret_free():
    original = task_with(("echo", {"api_key": "sk-leak-me", "m": 1}))
    archive = failed_archive(original, verification=VerificationStatus.FAILED)
    match = find_duplicate(task_with(("echo", {"api_key": "sk-leak-me",
                                               "m": 1})), [archive])
    assert match is not None
    reason = match.reason()
    assert "same executable strategy" in reason
    assert original.id in reason                    # which attempt matched
    assert "verification_failed" in reason          # why it was non-transient
    assert "different executable strategy" in reason
    assert "sk-leak-me" not in reason               # no secrets


def test_duplicate_match_is_a_value_object():
    match = DuplicateMatch(task_id="t", category=FailureCategory.PERSISTENT,
                           signature=(("echo", ()),))
    assert match.blocking is True
    assert DuplicateMatch(task_id="t", category=FailureCategory.TRANSIENT,
                          signature=()).blocking is False
