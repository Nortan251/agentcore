"""Unit tests for classification, retry safety, backoff and the decision model."""

from __future__ import annotations

import pytest

from agentcore.errors import ConfigError
from agentcore.permissions.levels import Capability
from agentcore.recovery.classify import (
    assess_retry_safety,
    classify,
    failed_before_side_effect,
    failure_signature,
    is_recoverable_state,
)
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.decision import (
    Escalation,
    FailureCategory,
    RecoveryAction,
    RecoveryDecision,
    RetrySafety,
)
from agentcore.recovery.history import RecoveryHistory
from agentcore.recovery.policy import RecoveryPolicy
from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    ErrorRecord,
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import Tool, ToolResult, tool_spec
from agentcore.tools.registry import ToolRegistry


# -- fixtures ---------------------------------------------------------------

class ReadOnlyTool(Tool):
    @property
    def spec(self):
        return tool_spec("reader", "Reads things.", read_only=True)

    def run(self, args, context):
        return ToolResult.success("ok")


class WriterTool(Tool):
    @property
    def spec(self):
        return tool_spec("writer", "Writes things.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        return ToolResult.success("ok")


class DestroyerTool(Tool):
    @property
    def spec(self):
        return tool_spec("destroyer", "Deletes things.",
                         capabilities=[Capability.FS_DELETE], read_only=False)

    def run(self, args, context):
        return ToolResult.success("ok")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all([ReadOnlyTool(), WriterTool(), DestroyerTool()])
    return reg


def failed_subtask(tool="reader", *, error_type="ToolFailure",
                   message="boom", retryable=True, attempts=1,
                   details=None) -> Subtask:
    sub = Subtask(description="do a thing", tool_name=tool)
    sub.attempts = attempts
    sub.transition_to(TaskStatus.RUNNING)
    sub.record_error(ErrorRecord(type=error_type, message=message,
                                 retryable=retryable, details=details or {}))
    sub.transition_to(TaskStatus.FAILED)
    return sub


def verified_subtask(status: VerificationStatus, tool="reader",
                     attempts=1) -> Subtask:
    sub = Subtask(description="do a thing", tool_name=tool)
    sub.attempts = attempts
    sub.transition_to(TaskStatus.RUNNING)
    sub.result = "something"
    sub.transition_to(TaskStatus.COMPLETED)
    sub.verification = VerificationResult(status=status, checked_by="t",
                                          detail="detail here")
    return sub


def state_with(subtask: Subtask) -> AgentState:
    task = Task(objective="goal")
    task.add_subtask(subtask)
    state = AgentState()
    state.set_task(task)
    return state


# -- configuration ----------------------------------------------------------

def test_backoff_is_exponential_and_capped():
    cfg = RecoveryConfig(base_delay_seconds=1.0, backoff_factor=2.0,
                         max_delay_seconds=5.0)
    assert [cfg.delay_for(i) for i in (1, 2, 3, 4, 10)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_backoff_zero_for_invalid_attempt():
    assert RecoveryConfig().delay_for(0) == 0.0


def test_config_validation():
    RecoveryConfig().validate()
    for bad in (RecoveryConfig(max_attempts=0),
                RecoveryConfig(base_delay_seconds=-1),
                RecoveryConfig(backoff_factor=0.5),
                RecoveryConfig(max_delay_seconds=-1),
                RecoveryConfig(max_replans=-1)):
        with pytest.raises(ConfigError):
            bad.validate()


# -- classification ---------------------------------------------------------

def test_transient_error_classified():
    assert classify(failed_subtask(retryable=True)) is FailureCategory.TRANSIENT


def test_non_retryable_error_is_persistent():
    assert classify(failed_subtask(retryable=False)) is FailureCategory.PERSISTENT


@pytest.mark.parametrize("error_type", ["validation", "ToolValidationError",
                                        "ToolNotFoundError",
                                        "unhandled_exception"])
def test_known_persistent_types(error_type):
    sub = failed_subtask(error_type=error_type, retryable=True)
    assert classify(sub) is FailureCategory.PERSISTENT


def test_permission_denial_classified():
    sub = failed_subtask(error_type="permission_denied", retryable=False)
    assert classify(sub) is FailureCategory.PERMISSION_DENIED


def test_permission_denial_detected_via_metadata_kind():
    sub = failed_subtask(error_type="ToolFailure",
                         details={"kind": "permission_denied"})
    assert classify(sub) is FailureCategory.PERMISSION_DENIED


def test_permission_denial_beats_repeated():
    sub = failed_subtask(error_type="permission_denied")
    assert classify(sub, repeated=True) is FailureCategory.PERMISSION_DENIED


def test_repeated_flag_classified():
    assert classify(failed_subtask(), repeated=True) is FailureCategory.REPEATED


def test_verification_failure_classified():
    sub = verified_subtask(VerificationStatus.FAILED)
    assert classify(sub) is FailureCategory.VERIFICATION_FAILED


def test_unverified_classified():
    sub = verified_subtask(VerificationStatus.UNVERIFIED)
    assert classify(sub) is FailureCategory.UNVERIFIED


def test_clean_subtask_has_no_category():
    sub = verified_subtask(VerificationStatus.PASSED)
    assert classify(sub) is FailureCategory.NONE


# -- retry safety -----------------------------------------------------------

def test_read_only_tool_is_safe(registry):
    assert assess_retry_safety(failed_subtask("reader"),
                               registry) is RetrySafety.SAFE


def test_mutating_tool_is_unknown_not_safe(registry):
    """Cannot prove the side effect did not already happen."""
    assert assess_retry_safety(failed_subtask("writer"),
                               registry) is RetrySafety.UNKNOWN


def test_irreversible_tool_is_unsafe(registry):
    assert assess_retry_safety(failed_subtask("destroyer"),
                               registry) is RetrySafety.UNSAFE


def test_unknown_tool_is_unknown(registry):
    assert assess_retry_safety(failed_subtask("ghost"),
                               registry) is RetrySafety.UNKNOWN


def test_no_registry_means_unknown():
    assert assess_retry_safety(failed_subtask("reader"),
                               None) is RetrySafety.UNKNOWN


def test_toolless_subtask_is_safe(registry):
    sub = Subtask(description="think")
    assert assess_retry_safety(sub, registry) is RetrySafety.SAFE


def test_failure_before_side_effect_detected():
    assert failed_before_side_effect(failed_subtask(error_type="validation"))
    assert failed_before_side_effect(
        failed_subtask(error_type="permission_denied"))
    assert not failed_before_side_effect(failed_subtask(error_type="ToolFailure"))


# -- signatures & recoverable state -----------------------------------------

def test_signature_stable_for_same_error():
    a, b = failed_subtask(message="same"), failed_subtask(message="same")
    assert failure_signature(a) == failure_signature(b)


def test_signature_differs_for_different_errors():
    assert failure_signature(failed_subtask(message="a")) != \
        failure_signature(failed_subtask(message="b"))


def test_signature_covers_verification():
    sig = failure_signature(verified_subtask(VerificationStatus.FAILED))
    assert sig.startswith("verification:failed")


def test_recoverable_state_rules():
    assert is_recoverable_state(failed_subtask())
    assert is_recoverable_state(verified_subtask(VerificationStatus.FAILED))
    assert not is_recoverable_state(verified_subtask(VerificationStatus.PASSED))
    cancelled = Subtask(description="x")
    cancelled.transition_to(TaskStatus.CANCELLED)
    assert not is_recoverable_state(cancelled)


# -- decisions --------------------------------------------------------------

def test_transient_read_only_failure_retries(registry):
    policy = RecoveryPolicy(RecoveryConfig(base_delay_seconds=0.25),
                            registry=registry)
    sub = failed_subtask("reader")
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.RETRY
    assert decision.should_retry is True
    assert decision.delay_seconds == 0.25
    assert decision.safety is RetrySafety.SAFE


def test_transient_mutating_failure_does_not_auto_retry(registry):
    """Unknown safety must never become a silent repeat."""
    policy = RecoveryPolicy(registry=registry)
    sub = failed_subtask("writer")
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is not RecoveryAction.RETRY
    assert decision.action is RecoveryAction.REPLAN


def test_irreversible_failure_escalates(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = failed_subtask("destroyer")
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.ESCALATE
    assert decision.escalation is not None
    assert "irreversible" in decision.reason


def test_permission_denial_escalates_and_never_retries(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = failed_subtask("writer", error_type="permission_denied",
                         retryable=False)
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.ESCALATE
    assert decision.category is FailureCategory.PERMISSION_DENIED
    assert decision.should_retry is False


def test_persistent_failure_replans(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = failed_subtask("reader", error_type="validation", retryable=False)
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.REPLAN


def test_persistent_failure_escalates_when_no_replans(registry):
    policy = RecoveryPolicy(RecoveryConfig(max_replans=0), registry=registry)
    sub = failed_subtask("reader", error_type="validation", retryable=False)
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.ESCALATE


def test_attempts_exhausted_escalates(registry):
    policy = RecoveryPolicy(RecoveryConfig(max_attempts=2, max_replans=0),
                            registry=registry)
    sub = failed_subtask("reader", attempts=2)
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.ESCALATE
    assert "exhausted" in decision.reason


def test_exhaustion_can_stop_instead_of_escalating(registry):
    policy = RecoveryPolicy(
        RecoveryConfig(max_attempts=1, max_replans=0,
                       escalate_on_exhaustion=False), registry=registry)
    sub = failed_subtask("reader", attempts=1)
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.STOP


def test_verification_failure_retries_when_safe(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = verified_subtask(VerificationStatus.FAILED, "reader")
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.RETRY
    assert decision.category is FailureCategory.VERIFICATION_FAILED


def test_verification_retry_can_be_disabled(registry):
    policy = RecoveryPolicy(RecoveryConfig(retry_verification_failures=False),
                            registry=registry)
    sub = verified_subtask(VerificationStatus.FAILED, "reader")
    assert policy.decide(sub, state_with(sub)).action is RecoveryAction.REPLAN


def test_unverified_does_not_retry_by_default(registry):
    """Unknown is not success, but nor is it a reason to blindly repeat."""
    policy = RecoveryPolicy(registry=registry)
    sub = verified_subtask(VerificationStatus.UNVERIFIED, "reader")
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.REPLAN
    assert decision.category is FailureCategory.UNVERIFIED


def test_unverified_retries_when_configured(registry):
    policy = RecoveryPolicy(RecoveryConfig(retry_unverified=True),
                            registry=registry)
    sub = verified_subtask(VerificationStatus.UNVERIFIED, "reader")
    assert policy.decide(sub, state_with(sub)).action is RecoveryAction.RETRY


def test_passed_verification_needs_no_recovery(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = verified_subtask(VerificationStatus.PASSED, "reader")
    decision = policy.decide(sub, state_with(sub))
    assert decision.action is RecoveryAction.NONE
    assert decision.should_retry is False


def test_cancelled_subtask_needs_no_recovery(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = Subtask(description="x", tool_name="reader")
    sub.transition_to(TaskStatus.CANCELLED)
    assert policy.decide(sub, state_with(sub)).action is RecoveryAction.NONE


def test_repeated_identical_failure_stops_retrying(registry):
    """The same failure twice means another attempt is pointless."""
    policy = RecoveryPolicy(RecoveryConfig(max_attempts=10), registry=registry)
    sub = failed_subtask("reader", message="identical")
    state = state_with(sub)

    first = policy.decide(sub, state)
    assert first.action is RecoveryAction.RETRY

    sub.attempts += 1
    sub.transition_to(TaskStatus.PENDING)
    sub.transition_to(TaskStatus.RUNNING)
    sub.record_error(ErrorRecord(type="ToolFailure", message="identical",
                                 retryable=True))
    sub.transition_to(TaskStatus.FAILED)

    second = policy.decide(sub, state)
    assert second.action is not RecoveryAction.RETRY
    assert second.category is FailureCategory.REPEATED


# -- Protocol compatibility -------------------------------------------------

def test_satisfies_recovery_strategy_protocol(registry):
    from agentcore.core.interfaces import RecoveryStrategy

    policy = RecoveryPolicy(registry=registry)
    assert isinstance(policy, RecoveryStrategy)
    sub = failed_subtask("reader")
    assert policy.should_retry(sub, state_with(sub)) is True


def test_should_retry_false_for_escalation(registry):
    policy = RecoveryPolicy(registry=registry)
    sub = failed_subtask("destroyer")
    assert policy.should_retry(sub, state_with(sub)) is False


# -- history ----------------------------------------------------------------

def test_history_counts_decisions():
    history = RecoveryHistory()
    history.record("s1", RecoveryDecision.retry(FailureCategory.TRANSIENT, "r"))
    history.record("s1", RecoveryDecision.replan(FailureCategory.PERSISTENT, "r"))
    history.record("s1", RecoveryDecision.escalate(
        FailureCategory.REPEATED, "r", escalation=Escalation("a", "b")))
    assert history.retries("s1") == 1
    assert history.replans("s1") == 1
    assert history.escalations("s1") == 1


def test_history_ignores_none_decisions():
    history = RecoveryHistory()
    history.record("s1", RecoveryDecision.none())
    assert history.summary("s1") == []


def test_history_detects_repeats():
    history = RecoveryHistory()
    history.record("s1", RecoveryDecision.retry(FailureCategory.TRANSIENT, "r"),
                   signature="error:X:boom")
    assert history.repeats("s1", "error:X:boom")
    assert not history.repeats("s1", "error:Y:other")
    assert not history.repeats("s1", "none")


def test_history_is_bounded():
    from agentcore.recovery.history import MAX_ENTRIES_PER_SUBTASK

    history = RecoveryHistory()
    for i in range(MAX_ENTRIES_PER_SUBTASK * 2):
        history.record("s1", RecoveryDecision.retry(
            FailureCategory.TRANSIENT, f"r{i}"), signature=f"sig{i}")
    record = history.record_for("s1")
    assert len(record.decisions) == MAX_ENTRIES_PER_SUBTASK
    assert len(record.signatures) == MAX_ENTRIES_PER_SUBTASK
    assert record.retries == MAX_ENTRIES_PER_SUBTASK * 2  # counter stays exact


def test_history_round_trips_through_state():
    state = AgentState()
    history = RecoveryHistory()
    history.record("s1", RecoveryDecision.retry(FailureCategory.TRANSIENT, "r"),
                   signature="sig")
    history.save(state)

    restored = RecoveryHistory.load(AgentState.from_dict(state.to_dict()))
    assert restored.retries("s1") == 1
    assert restored.repeats("s1", "sig")


def test_history_tolerates_missing_or_bad_scratch():
    assert RecoveryHistory.load(AgentState()).retries("s1") == 0
    state = AgentState()
    state.scratch["recovery"] = "not a dict"
    assert RecoveryHistory.load(state).retries("s1") == 0


def test_decision_serialisation():
    decision = RecoveryDecision.escalate(
        FailureCategory.REPEATED, "reason",
        escalation=Escalation("why", "what now?", subtask_id="s1"))
    payload = decision.to_dict()
    assert payload["action"] == "escalate"
    assert payload["escalation"]["question"] == "what now?"
    assert decision.is_terminal


def test_escalation_round_trip():
    original = Escalation("reason", "question", subtask_id="s1",
                          attempted=["retry: x"], unresolved="still broken")
    restored = Escalation.from_dict(original.to_dict())
    assert restored.reason == "reason"
    assert restored.attempted == ["retry: x"]
    assert restored.safe_to_pause is True
