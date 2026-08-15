"""Tests for evidence-only deterministic verification and context building."""

from __future__ import annotations

import pytest

from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationStatus,
)
from agentcore.verification.context import build_context
from agentcore.verification.deterministic import (
    DeterministicVerifier,
    expectation_match,
)


def ctx(**overrides):
    """Build a VerificationContext directly for focused unit tests."""
    from agentcore.verification.context import VerificationContext

    defaults = dict(
        subtask_id="sub_1", description="say hello",
        status=TaskStatus.COMPLETED, objective="greet",
        tool_name="echo", tool_args={"message": "hello"},
        result="hello", has_result=True, tool_ok=True,
        expected_result="", requires_verification=False,
    )
    defaults.update(overrides)
    return VerificationContext(**defaults)


@pytest.fixture
def verifier() -> DeterministicVerifier:
    return DeterministicVerifier()


# -- expectation matching ---------------------------------------------------

def test_exact_substring_is_a_full_match():
    assert expectation_match("hello world", "the output is hello world") == 1.0


def test_token_overlap_scored():
    score = expectation_match("a file named report", "created report file")
    assert 0 < score <= 1.0


def test_no_overlap_scores_zero():
    assert expectation_match("a green circle", "purple 12345") == 0.0


def test_stopwords_do_not_inflate_score():
    """An expectation of only stopwords carries no signal."""
    assert expectation_match("the result of the output", "totally unrelated") == 0.0


# -- clear success / failure ------------------------------------------------

def test_failed_status_is_verification_failure(verifier):
    result = verifier.verify_context(ctx(status=TaskStatus.FAILED,
                                         tool_ok=False,
                                         tool_error="tool exploded"))
    assert result.status is VerificationStatus.FAILED
    assert "tool exploded" in result.detail


def test_tool_reported_failure_is_failure(verifier):
    result = verifier.verify_context(ctx(tool_ok=False,
                                         tool_error="permission denied"))
    assert result.status is VerificationStatus.FAILED


def test_expectation_satisfied_passes(verifier):
    result = verifier.verify_context(ctx(expected_result="hello",
                                         result="hello", has_result=True))
    assert result.status is VerificationStatus.PASSED
    assert result.checked_by == "DeterministicVerifier"


def test_expectation_clearly_unsatisfied_fails(verifier):
    result = verifier.verify_context(ctx(
        expected_result="a sorted list of customers",
        result="42", has_result=True))
    assert result.status is VerificationStatus.FAILED


def test_expectation_with_no_result_fails(verifier):
    result = verifier.verify_context(ctx(expected_result="something",
                                         result=None, has_result=False))
    assert result.status is VerificationStatus.FAILED
    assert "no result" in result.detail


def test_success_without_expectation_passes(verifier):
    """A successful call with nothing specific required is verifiable."""
    result = verifier.verify_context(ctx(expected_result="",
                                         requires_verification=False))
    assert result.status is VerificationStatus.PASSED


# -- conservatism: unknown is not success -----------------------------------

def test_partial_match_is_unverified_not_passed(verifier):
    """Ambiguous evidence must escalate, never be assumed successful."""
    result = verifier.verify_context(ctx(
        expected_result="a report containing quarterly revenue figures",
        result="report", has_result=True))
    assert result.status is VerificationStatus.UNVERIFIED
    assert "inconclusive" in result.detail


def test_verification_required_but_no_expectation_is_unverified(verifier):
    result = verifier.verify_context(ctx(requires_verification=True,
                                         expected_result=""))
    assert result.status is VerificationStatus.UNVERIFIED


def test_incomplete_subtask_is_unverified(verifier):
    result = verifier.verify_context(ctx(status=TaskStatus.RUNNING))
    assert result.status is VerificationStatus.UNVERIFIED
    assert "not completed" in result.detail


def test_no_tool_executed_is_unverified(verifier):
    result = verifier.verify_context(ctx(tool_name=None, tool_ok=None))
    assert result.status is VerificationStatus.UNVERIFIED
    assert "no execution evidence" in result.detail


def test_unknown_tool_outcome_is_not_a_pass(verifier):
    """Absence of a recorded outcome is not evidence of success."""
    result = verifier.verify_context(ctx(tool_ok=None, expected_result=""))
    assert result.status is VerificationStatus.UNVERIFIED


def test_completion_alone_never_passes_a_stated_expectation(verifier):
    """Reaching COMPLETED is not proof the expectation was met."""
    result = verifier.verify_context(ctx(
        status=TaskStatus.COMPLETED, tool_ok=True,
        expected_result="the database was migrated to version 5",
        result="ok", has_result=True))
    assert result.status is not VerificationStatus.PASSED


# -- context building -------------------------------------------------------

def _state_with_plan(expected="hello", requires=True):
    task = Task(objective="greet")
    sub = task.add_subtask(Subtask(description="say hello", tool_name="echo",
                                   tool_args={"message": "hello"}))
    task.metadata["plan"] = {"schema_version": 1, "annotations": {
        sub.id: {"expected_result": expected,
                 "requires_verification": requires}}}
    state = AgentState()
    state.set_task(task)
    return task, sub, state


def test_context_reads_planner_annotations():
    task, sub, state = _state_with_plan("hello there", True)
    context = build_context(sub, state)
    assert context.expected_result == "hello there"
    assert context.requires_verification is True
    assert context.objective == "greet"


def test_context_without_plan_metadata_is_safe():
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="d", tool_name="echo"))
    state = AgentState()
    state.set_task(task)
    context = build_context(sub, state)
    assert context.expected_result == ""
    assert context.requires_verification is False


def test_context_ignores_malformed_plan_metadata():
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="d", tool_name="echo"))
    task.metadata["plan"] = "not a dict"
    state = AgentState()
    state.set_task(task)
    assert build_context(sub, state).expected_result == ""


def test_context_picks_up_recorded_tool_call():
    task, sub, state = _state_with_plan()
    state.set_active_subtask(sub.id)
    state.record_tool_call({"tool": "echo",
                            "result": {"ok": True, "output": "hello"}})
    context = build_context(sub, state)
    assert context.tool_ok is True


def test_context_reports_tool_failure():
    task, sub, state = _state_with_plan()
    state.set_active_subtask(sub.id)
    state.record_tool_call({"tool": "echo",
                            "result": {"ok": False, "error": "nope"}})
    context = build_context(sub, state)
    assert context.tool_ok is False
    assert context.tool_error == "nope"


def test_context_redacts_secrets_in_tool_args():
    """Args reach model prompts, so they use the existing redaction rules."""
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="d", tool_name="echo",
                                   tool_args={"api_key": "sk-secret-value",
                                              "message": "hi"}))
    state = AgentState()
    state.set_task(task)
    payload = build_context(sub, state).to_dict()
    assert payload["tool_args"]["api_key"] == "***"
    assert payload["tool_args"]["message"] == "hi"
    import json
    assert "sk-secret-value" not in json.dumps(payload)


def test_context_truncates_large_output():
    from agentcore.verification.context import MAX_OUTPUT_CHARS

    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="d", tool_name="echo"))
    sub.result = "y" * (MAX_OUTPUT_CHARS * 3)
    state = AgentState()
    state.set_task(task)
    assert len(build_context(sub, state).result_text()) == MAX_OUTPUT_CHARS


def test_context_serialises_structured_results():
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="d", tool_name="workspace_info"))
    sub.result = {"workspace_dir": "/tmp", "exists": True}
    state = AgentState()
    state.set_task(task)
    assert "workspace_dir" in build_context(sub, state).result_text()


def test_verifier_protocol_entrypoint():
    """The Protocol's verify(subtask, state) form works end to end."""
    task, sub, state = _state_with_plan("hello", True)
    sub.transition_to(TaskStatus.RUNNING)
    sub.result = "hello"
    sub.transition_to(TaskStatus.COMPLETED)
    result = DeterministicVerifier().verify(sub, state)
    assert result.status is VerificationStatus.PASSED
