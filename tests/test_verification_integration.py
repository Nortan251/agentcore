"""Composite verifier, feature flag, security boundary and end-to-end flow."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.defaults import NullVerifier
from agentcore.core.interfaces import Verifier as VerifierProtocol
from agentcore.permissions.levels import PermissionLevel
from agentcore.planning.planner import LLMPlanner
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.state.models import AgentState
from agentcore.verification.context import VerificationContext
from agentcore.verification.verifier import Verifier


class FakeModel:
    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.calls.append({"prompt": prompt, "system": system,
                           "options": options})
        if not self.outputs:
            raise AssertionError("FakeModel exhausted")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)


def ctx(**overrides) -> VerificationContext:
    defaults = dict(
        subtask_id="sub_1", description="do the thing",
        status=TaskStatus.COMPLETED, objective="goal",
        tool_name="echo", tool_args={"message": "hi"},
        result="hi", has_result=True, tool_ok=True,
        expected_result="", requires_verification=False,
    )
    defaults.update(overrides)
    return VerificationContext(**defaults)


def cfg(tmp_path, *, verification=False, provider="none",
        level=PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(provider=provider,
                          name="m" if provider != "none" else "none"),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        features=FeatureFlags(verification=verification),
    ).validate()


def plan_json(*, message="hello", expected="hello", requires=True,
              objective="greet"):
    return json.dumps({"objective": objective, "steps": [{
        "id": "s1", "description": "say hello", "tool_name": "echo",
        "tool_args": {"message": message},
        "expected_result": expected,
        "requires_verification": requires}]})


# -- composite strategy -----------------------------------------------------

def test_satisfies_protocol():
    assert isinstance(Verifier(), VerifierProtocol)


def test_skips_when_nothing_to_verify():
    result = Verifier().verify_context(ctx())
    assert result.status is VerificationStatus.SKIPPED
    assert result.checked_by == "Verifier"


def test_deterministic_pass_does_not_consult_model():
    model = FakeModel()  # would raise if called
    result = Verifier(model=model).verify_context(
        ctx(expected_result="hi", requires_verification=True))
    assert result.status is VerificationStatus.PASSED
    assert result.checked_by == "DeterministicVerifier"
    assert model.calls == []


def test_deterministic_failure_does_not_consult_model():
    model = FakeModel()
    result = Verifier(model=model).verify_context(
        ctx(tool_ok=False, tool_error="denied", status=TaskStatus.FAILED))
    assert result.status is VerificationStatus.FAILED
    assert model.calls == []


def test_inconclusive_escalates_to_model():
    model = FakeModel({"verdict": "pass", "confidence": 0.9,
                       "reason": "the summary is present"})
    result = Verifier(model=model).verify_context(ctx(
        expected_result="a concise summary of quarterly revenue",
        result="summary", requires_verification=True))
    assert result.status is VerificationStatus.PASSED
    assert result.checked_by == "ModelAssistedVerifier"
    assert len(model.calls) == 1


def test_no_model_leaves_inconclusive_unverified():
    result = Verifier().verify_context(ctx(
        expected_result="a concise summary of quarterly revenue",
        result="summary", requires_verification=True))
    assert result.status is VerificationStatus.UNVERIFIED


def test_model_not_used_when_verification_not_requested():
    """Cost control: only steps the plan flagged escalate by default."""
    model = FakeModel()
    result = Verifier(model=model).verify_context(ctx(
        expected_result="a concise summary of quarterly revenue",
        result="summary", requires_verification=False))
    assert result.status is VerificationStatus.UNVERIFIED
    assert model.calls == []


def test_always_use_model_option():
    model = FakeModel({"verdict": "pass", "confidence": 0.9, "reason": "ok"})
    result = Verifier(model=model, always_use_model=True).verify_context(ctx(
        expected_result="a concise summary of quarterly revenue",
        result="summary", requires_verification=False))
    assert result.status is VerificationStatus.PASSED


def test_both_strategies_inconclusive_reports_both():
    model = FakeModel({"verdict": "uncertain", "reason": "insufficient data"})
    result = Verifier(model=model).verify_context(ctx(
        expected_result="a concise summary of quarterly revenue",
        result="summary", requires_verification=True))
    assert result.status is VerificationStatus.UNVERIFIED
    assert "insufficient data" in result.detail
    assert "deterministic:" in result.detail


def test_broken_model_cannot_turn_failure_into_success():
    model = FakeModel("garbage")
    result = Verifier(model=model).verify_context(
        ctx(tool_ok=False, status=TaskStatus.FAILED, tool_error="boom"))
    assert result.status is VerificationStatus.FAILED


def test_incomplete_work_never_escalates():
    model = FakeModel()
    result = Verifier(model=model).verify_context(
        ctx(status=TaskStatus.RUNNING, requires_verification=True))
    assert result.status is VerificationStatus.UNVERIFIED
    assert model.calls == []


def test_has_model_reports_capability():
    assert Verifier().has_model is False
    assert Verifier(model=FakeModel()).has_model is True


# -- feature flag -----------------------------------------------------------

def test_disabled_uses_null_verifier(tmp_path):
    agent = Agent(cfg(tmp_path, verification=False))
    assert isinstance(agent.verifier, NullVerifier)


def test_enabled_uses_real_verifier(tmp_path):
    agent = Agent(cfg(tmp_path, verification=True))
    assert isinstance(agent.verifier, Verifier)


def test_enabled_with_model_gets_model_assist(tmp_path):
    agent = Agent(cfg(tmp_path, verification=True, provider="echo"))
    assert isinstance(agent.verifier, Verifier)
    assert agent.verifier.has_model is True


def test_enabled_without_model_still_works(tmp_path):
    agent = Agent(cfg(tmp_path, verification=True, provider="none"))
    assert agent.verifier.has_model is False


def test_injected_verifier_wins_over_flag(tmp_path):
    class Custom:
        def verify(self, subtask, state):
            return VerificationResult(status=VerificationStatus.PASSED,
                                      checked_by="Custom")

    custom = Custom()
    for flag in (True, False):
        agent = Agent(cfg(tmp_path, verification=flag),
                      AgentDependencies(verifier=custom))
        assert agent.verifier is custom


def test_disabled_never_calls_the_model(tmp_path):
    """A disabled flag must not incur model cost."""
    model = FakeModel()  # raises if called
    agent = Agent(cfg(tmp_path, verification=False),
                  AgentDependencies(model=model))
    task = Task(objective="x")
    task.add_subtask(Subtask(description="e", tool_name="echo",
                             tool_args={"message": "hi"}))
    agent.run_task(task)
    assert model.calls == []
    assert task.subtasks[0].verification.status is VerificationStatus.SKIPPED


# -- security boundary ------------------------------------------------------

def test_verifier_holds_no_executor():
    verifier = Verifier(model=FakeModel())
    assert "executor" not in vars(verifier)
    assert not any("Executor" in type(v).__name__ for v in vars(verifier).values())


def test_verifier_does_not_execute_tools(tmp_path):
    """A verified step must not cause a second tool invocation."""
    from agentcore.tools.base import Tool, ToolResult, tool_spec

    class Counting(Tool):
        def __init__(self):
            self.runs = 0

        @property
        def spec(self):
            return tool_spec("counter", "Counts its runs.")

        def run(self, args, context):
            self.runs += 1
            return ToolResult.success("counted")

    tool = Counting()
    agent = Agent(cfg(tmp_path, verification=True),
                  AgentDependencies(tools=[tool]))
    task = Task(objective="count")
    sub = task.add_subtask(Subtask(description="count", tool_name="counter"))
    task.metadata["plan"] = {"annotations": {
        sub.id: {"expected_result": "counted", "requires_verification": True}}}
    agent.run_task(task)

    assert tool.runs == 1  # executed once, never re-run by verification
    assert sub.verification.status is VerificationStatus.PASSED


def test_verifier_does_not_change_permission_state(tmp_path):
    agent = Agent(cfg(tmp_path, verification=True,
                      level=PermissionLevel.OBSERVE))
    before = agent.policy.level
    task = Task(objective="x")
    task.add_subtask(Subtask(description="e", tool_name="echo",
                             tool_args={"message": "hi"}))
    agent.run_task(task)
    assert agent.policy.level is before


def test_verifier_does_not_mutate_the_result(tmp_path):
    agent = Agent(cfg(tmp_path, verification=True))
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="e", tool_name="echo",
                                   tool_args={"message": "hi"}))
    task.metadata["plan"] = {"annotations": {
        sub.id: {"expected_result": "totally different",
                 "requires_verification": True}}}
    agent.run_task(task)
    # verification failed, but the recorded result is untouched
    assert sub.result == "hi"
    assert sub.verification.status is VerificationStatus.FAILED


def test_failed_verification_does_not_change_task_status(tmp_path):
    """Verification judges; it does not drive control flow (that is recovery)."""
    agent = Agent(cfg(tmp_path, verification=True))
    task = Task(objective="x")
    sub = task.add_subtask(Subtask(description="e", tool_name="echo",
                                   tool_args={"message": "hi"}))
    task.metadata["plan"] = {"annotations": {
        sub.id: {"expected_result": "an unrelated outcome",
                 "requires_verification": True}}}
    report = agent.run_task(task)
    assert sub.status is TaskStatus.COMPLETED
    assert report.status is TaskStatus.COMPLETED
    assert sub.verification.status is VerificationStatus.FAILED


# -- end to end -------------------------------------------------------------

def test_e2e_plan_execute_verify_success(tmp_path):
    """Objective -> plan -> execute -> verify, ending PASSED."""
    model = FakeModel(plan_json(message="hello world", expected="hello world"))
    agent = Agent(cfg(tmp_path, verification=True))
    agent.planner = LLMPlanner(model, agent.registry)

    report = agent.execute("greet the world")

    assert report.status is TaskStatus.COMPLETED
    sub = agent.state.current_task.subtasks[0]
    assert sub.result == "hello world"
    assert sub.verification.status is VerificationStatus.PASSED
    assert sub.verification.checked_by == "DeterministicVerifier"
    # recorded in state as well as on the subtask
    assert agent.state.verifications[-1]["status"] == "passed"
    assert agent.state.verifications[-1]["subtask_id"] == sub.id


def test_e2e_plan_execute_verify_failure(tmp_path):
    """Same pipeline, but the result does not satisfy the expectation."""
    model = FakeModel(plan_json(message="goodbye",
                                expected="a warm greeting saying hello"))
    agent = Agent(cfg(tmp_path, verification=True))
    agent.planner = LLMPlanner(model, agent.registry)

    agent.execute("greet the world")

    sub = agent.state.current_task.subtasks[0]
    assert sub.result == "goodbye"
    assert sub.verification.status is VerificationStatus.FAILED
    assert agent.state.verifications[-1]["status"] == "failed"


def test_e2e_model_assisted_path(tmp_path):
    """Ambiguous evidence escalates to the model and is recorded."""
    planning = plan_json(message="Q3 summary ready",
                         expected="a concise summary of quarterly revenue")
    verdict = json.dumps({"verdict": "pass", "confidence": 0.88,
                          "reason": "the output reports the Q3 summary"})
    model = FakeModel(planning, verdict)
    agent = Agent(cfg(tmp_path, verification=True),
                  AgentDependencies(model=model))
    agent.planner = LLMPlanner(model, agent.registry)

    agent.execute("summarise revenue")

    sub = agent.state.current_task.subtasks[0]
    assert sub.verification.status is VerificationStatus.PASSED
    assert sub.verification.checked_by == "ModelAssistedVerifier"
    assert len(model.calls) == 2  # one plan, one verification


def test_e2e_unverifiable_stays_unverified(tmp_path):
    """No model and ambiguous evidence must not be reported as success."""
    model = FakeModel(plan_json(message="done",
                                expected="the quarterly revenue report was "
                                         "emailed to finance"))
    agent = Agent(cfg(tmp_path, verification=True))
    agent.planner = LLMPlanner(model, agent.registry)

    agent.execute("report revenue")

    status = agent.state.current_task.subtasks[0].verification.status
    assert status is not VerificationStatus.PASSED
    assert status in (VerificationStatus.UNVERIFIED, VerificationStatus.FAILED)


def test_verification_result_is_serialisable_and_survives_resume(tmp_path):
    from agentcore.state.store import JsonFileStateStore

    store = JsonFileStateStore(tmp_path / "runs")
    model = FakeModel(plan_json(message="hello", expected="hello"))
    agent = Agent(cfg(tmp_path, verification=True),
                  AgentDependencies(state_store=store))
    agent.planner = LLMPlanner(model, agent.registry)
    agent.execute("greet")
    run_id = agent.state.run_id
    agent.shutdown()

    resumed = Agent(cfg(tmp_path, verification=True),
                    AgentDependencies(state_store=store)).resume(run_id)
    sub = resumed.current_task.subtasks[0]
    assert sub.verification.status is VerificationStatus.PASSED
    assert sub.verification.checked_by == "DeterministicVerifier"
