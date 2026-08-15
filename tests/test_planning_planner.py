"""Tests for the LLM planner: model interaction, conversion, safety boundary."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.core.interfaces import Planner
from agentcore.models.errors import ModelAuthError, ModelTimeoutError
from agentcore.permissions.levels import Capability
from agentcore.planning.errors import (
    PlanGenerationError,
    PlanningError,
    PlanParseError,
    PlanSchemaError,
    PlanValidationError,
)
from agentcore.planning.planner import LLMPlanner
from agentcore.tasks.models import TaskStatus
from agentcore.tools.base import Tool, ToolContext, ToolResult, tool_spec
from agentcore.tools.builtin import default_tools
from agentcore.tools.registry import ToolRegistry


class FakeModel:
    """Returns scripted output and records how it was called."""

    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.calls.append({"prompt": prompt, "system": system,
                           "options": options})
        if not self.outputs:
            raise AssertionError("FakeModel ran out of scripted outputs")
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output if isinstance(output, str) else json.dumps(output)


class ExplodingTool(Tool):
    """Fails the test if the planner ever executes a tool."""

    def __init__(self) -> None:
        self.run_count = 0

    @property
    def spec(self):
        return tool_spec("danger", "Mutating tool.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context: ToolContext) -> ToolResult:
        self.run_count += 1
        raise AssertionError("planner must never execute a tool")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(default_tools())
    return reg


def plan_payload(*steps: dict[str, Any], objective: str = "goal"
                 ) -> dict[str, Any]:
    return {"objective": objective, "steps": list(steps)}


def echo(step_id: str = "s1", message: str = "hi", **extra: Any
         ) -> dict[str, Any]:
    step = {"id": step_id, "description": f"echo {message}",
            "tool_name": "echo", "tool_args": {"message": message}}
    step.update(extra)
    return step


# -- protocol ---------------------------------------------------------------

def test_planner_satisfies_protocol(registry):
    planner = LLMPlanner(FakeModel(), registry)
    assert isinstance(planner, Planner)
    assert hasattr(planner, "plan") and hasattr(planner, "replan")


def test_requires_a_model(registry):
    with pytest.raises(PlanningError):
        LLMPlanner(None, registry)


def test_empty_objective_rejected(registry):
    with pytest.raises(PlanningError):
        LLMPlanner(FakeModel(), registry).plan("   ")


# -- successful planning ----------------------------------------------------

def test_single_step_plan(registry):
    model = FakeModel(plan_payload(echo(), objective="say hi"))
    task = LLMPlanner(model, registry).plan("say hi")
    assert task.objective == "say hi"
    assert len(task.subtasks) == 1
    assert task.subtasks[0].tool_name == "echo"
    assert task.subtasks[0].tool_args == {"message": "hi"}
    assert task.status is TaskStatus.PENDING


def test_multi_step_plan_with_dependencies(registry):
    model = FakeModel(plan_payload(
        echo("s1", "one"),
        echo("s2", "two", depends_on=["s1"]),
        echo("s3", "three", depends_on=["s2"])))
    task = LLMPlanner(model, registry).plan("count")
    assert len(task.subtasks) == 3
    # dependencies are remapped to real subtask ids
    assert task.subtasks[1].depends_on == [task.subtasks[0].id]
    assert task.subtasks[2].depends_on == [task.subtasks[1].id]
    # and only the first step is ready
    assert [s.description for s in task.ready_subtasks()] == ["echo one"]


def test_steps_reordered_topologically(registry):
    """Dependencies may be declared before their prerequisites appear."""
    model = FakeModel(plan_payload(
        echo("s2", "second", depends_on=["s1"]),
        echo("s1", "first")))
    task = LLMPlanner(model, registry).plan("ordering")
    assert [s.description for s in task.subtasks] == ["echo first", "echo second"]
    task.validate()


def test_model_local_ids_never_leak(registry):
    model = FakeModel(plan_payload(echo("s1"), echo("s2", depends_on=["s1"])))
    task = LLMPlanner(model, registry).plan("x")
    for sub in task.subtasks:
        assert sub.id.startswith("sub_")
        assert sub.id not in ("s1", "s2")


def test_toolless_step_preserved(registry):
    model = FakeModel(plan_payload(
        echo("s1"),
        {"id": "s2", "description": "ask the user", "depends_on": ["s1"]}))
    task = LLMPlanner(model, registry).plan("x")
    assert task.subtasks[1].tool_name is None


def test_annotations_stored_in_metadata(registry):
    model = FakeModel(plan_payload(
        echo("s1", expected_result="an echo", requires_verification=True)))
    task = LLMPlanner(model, registry).plan("x")
    annotations = task.metadata["plan"]["annotations"]
    entry = annotations[task.subtasks[0].id]
    assert entry["expected_result"] == "an echo"
    assert entry["requires_verification"] is True


def test_context_recorded_in_metadata(registry):
    model = FakeModel(plan_payload(echo()))
    task = LLMPlanner(model, registry).plan("x", context={"source": "cli"})
    assert task.metadata["source"] == "cli"


def test_fenced_json_output_accepted(registry):
    model = FakeModel("```json\n" + json.dumps(plan_payload(echo())) + "\n```")
    assert len(LLMPlanner(model, registry).plan("x").subtasks) == 1


# -- tool catalogue ---------------------------------------------------------

def test_prompt_contains_registered_tools(registry):
    model = FakeModel(plan_payload(echo()))
    LLMPlanner(model, registry).plan("x")
    prompt = model.calls[0]["prompt"]
    assert "echo" in prompt
    assert "workspace_info" in prompt
    assert "Text to echo back." in prompt          # parameter description
    assert "VALID TOOL NAMES" in prompt


def test_dynamically_registered_tool_appears(registry):
    """Future tools must show up with no planner change."""
    class Extra(Tool):
        @property
        def spec(self):
            return tool_spec("brand_new_tool", "A tool added at runtime.")

        def run(self, args, context):
            return ToolResult.success("ok")

    registry.register(Extra())
    model = FakeModel(plan_payload(echo()))
    LLMPlanner(model, registry).plan("x")
    assert "brand_new_tool" in model.calls[0]["prompt"]
    assert "A tool added at runtime." in model.calls[0]["prompt"]


def test_empty_registry_still_produces_a_prompt():
    model = FakeModel(plan_payload({"id": "s1", "description": "think"}))
    planner = LLMPlanner(model, ToolRegistry(), require_executable=False)
    planner.plan("x")
    assert "No tools are currently available" in model.calls[0]["prompt"]


def test_system_prompt_forbids_inventing_tools(registry):
    model = FakeModel(plan_payload(echo()))
    LLMPlanner(model, registry).plan("x")
    system = model.calls[0]["system"]
    assert "Never invent a tool" in system
    assert "never execute" in system.lower()


def test_structured_output_requested(registry):
    model = FakeModel(plan_payload(echo()))
    LLMPlanner(model, registry).plan("x")
    options = model.calls[0]["options"]
    assert options["response_format"]["type"] == "json_schema"
    assert options["temperature"] == 0.0


# -- rejection of bad model output ------------------------------------------

def test_unknown_tool_rejected(registry):
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "x", "tool_name": "rm_rf"}))
    with pytest.raises(PlanValidationError) as exc:
        LLMPlanner(model, registry).plan("x")
    assert exc.value.details["tool"] == "rm_rf"


def test_invalid_arguments_rejected(registry):
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "x", "tool_name": "echo",
         "tool_args": {"wrong": "arg"}}))
    with pytest.raises(PlanValidationError):
        LLMPlanner(model, registry).plan("x")


def test_invalid_json_rejected(registry):
    with pytest.raises(PlanParseError):
        LLMPlanner(FakeModel("I refuse."), registry).plan("x")


def test_empty_output_rejected(registry):
    with pytest.raises(PlanParseError):
        LLMPlanner(FakeModel(""), registry).plan("x")


def test_missing_fields_rejected(registry):
    with pytest.raises(PlanSchemaError):
        LLMPlanner(FakeModel({"objective": "x"}), registry).plan("x")


def test_wrong_types_rejected(registry):
    with pytest.raises(PlanSchemaError):
        LLMPlanner(FakeModel({"objective": "x", "steps": "nope"}),
                   registry).plan("x")


def test_empty_plan_rejected(registry):
    with pytest.raises(PlanSchemaError):
        LLMPlanner(FakeModel(plan_payload()), registry).plan("x")


def test_cyclic_plan_rejected(registry):
    model = FakeModel(plan_payload(
        echo("s1", depends_on=["s2"]), echo("s2", depends_on=["s1"])))
    with pytest.raises(PlanValidationError) as exc:
        LLMPlanner(model, registry).plan("x")
    assert "cycle" in exc.value.message


def test_unknown_dependency_rejected(registry):
    model = FakeModel(plan_payload(echo("s1", depends_on=["ghost"])))
    with pytest.raises(PlanValidationError):
        LLMPlanner(model, registry).plan("x")


def test_duplicate_ids_rejected(registry):
    model = FakeModel(plan_payload(echo("s1"), echo("s1", "other")))
    with pytest.raises(PlanValidationError):
        LLMPlanner(model, registry).plan("x")


def test_plan_without_executable_work_rejected(registry):
    model = FakeModel(plan_payload({"id": "s1", "description": "ponder"}))
    with pytest.raises(PlanValidationError) as exc:
        LLMPlanner(model, registry).plan("x")
    assert "no executable step" in exc.value.message


# -- model failures ---------------------------------------------------------

def test_model_error_becomes_generation_error(registry):
    model = FakeModel(ModelTimeoutError("provider timed out"))
    with pytest.raises(PlanGenerationError) as exc:
        LLMPlanner(model, registry).plan("x")
    assert exc.value.details["error_type"] == "ModelTimeoutError"
    assert exc.value.retryable is True


def test_auth_error_surfaces_without_leaking(registry):
    model = FakeModel(ModelAuthError("credential rejected"))
    with pytest.raises(PlanGenerationError) as exc:
        LLMPlanner(model, registry).plan("x")
    assert "credential rejected" in json.dumps(exc.value.to_dict())


def test_unexpected_exception_is_wrapped(registry):
    model = FakeModel(RuntimeError("boom"))
    with pytest.raises(PlanGenerationError):
        LLMPlanner(model, registry).plan("x")


# -- safety boundary --------------------------------------------------------

def test_planner_never_executes_tools(registry):
    """Planning a mutating tool must not run it."""
    danger = ExplodingTool()
    registry.register(danger)
    model = FakeModel(plan_payload(
        {"id": "s1", "description": "do damage", "tool_name": "danger"}))
    task = LLMPlanner(model, registry).plan("x")

    assert danger.run_count == 0
    assert task.subtasks[0].tool_name == "danger"
    assert task.subtasks[0].status is TaskStatus.PENDING
    assert task.subtasks[0].result is None


def test_planner_holds_no_executor(registry):
    planner = LLMPlanner(FakeModel(), registry)
    attrs = vars(planner)
    assert "executor" not in attrs
    assert not any("Executor" in type(v).__name__ for v in attrs.values())


def test_planner_does_not_consult_permissions(registry):
    """Permission decisions stay at execution time, not planning time."""
    planner = LLMPlanner(FakeModel(plan_payload(echo())), registry)
    assert "policy" not in vars(planner)
    task = planner.plan("x")
    # Nothing in the produced task grants or records any approval.
    assert task.metadata.get("permission") is None


def test_planned_task_is_inert_until_executed(registry):
    model = FakeModel(plan_payload(echo()))
    task = LLMPlanner(model, registry).plan("x")
    assert task.status is TaskStatus.PENDING
    assert all(s.status is TaskStatus.PENDING for s in task.subtasks)
    assert all(s.attempts == 0 for s in task.subtasks)


# -- replan -----------------------------------------------------------------

def test_replan_reuses_objective_and_passes_failures(registry):
    from agentcore.state.models import AgentState

    model = FakeModel(plan_payload(echo("s1"), objective="original goal"))
    planner = LLMPlanner(model, registry)
    task = planner.plan("original goal")

    sub = task.subtasks[0]
    sub.transition_to(TaskStatus.RUNNING)
    sub.record_error(ValueError("it broke"))
    sub.transition_to(TaskStatus.FAILED)

    model.outputs.append(plan_payload(echo("s1", "retry"),
                                      objective="original goal"))
    new_task = planner.replan(task, AgentState())

    assert new_task.objective == "original goal"
    assert new_task.id != task.id
    prompt = model.calls[-1]["prompt"]
    assert "replanning" in prompt
    assert "it broke" in prompt
