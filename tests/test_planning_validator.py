"""Tests for semantic plan validation against the live tool registry."""

from __future__ import annotations

import pytest

from agentcore.planning.errors import PlanValidationError
from agentcore.planning.schema import Plan, PlanStep, parse_plan
from agentcore.planning.validator import validate_plan
from agentcore.tools.builtin import default_tools
from agentcore.tools.registry import ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(default_tools())
    return reg


def plan_of(*steps: PlanStep) -> Plan:
    return Plan(objective="goal", steps=list(steps))


def echo_step(step_id="s1", **overrides) -> PlanStep:
    defaults = dict(id=step_id, description="say hi", tool_name="echo",
                    tool_args={"message": "hi"})
    defaults.update(overrides)
    return PlanStep(**defaults)


# -- happy path -------------------------------------------------------------

def test_valid_single_step(registry):
    plan = plan_of(echo_step())
    assert validate_plan(plan, registry) is plan


def test_valid_dependency_chain(registry):
    plan = plan_of(echo_step("s1"),
                   echo_step("s2", depends_on=["s1"]),
                   echo_step("s3", depends_on=["s1", "s2"]))
    validate_plan(plan, registry)


def test_diamond_dependencies_allowed(registry):
    plan = plan_of(echo_step("a"),
                   echo_step("b", depends_on=["a"]),
                   echo_step("c", depends_on=["a"]),
                   echo_step("d", depends_on=["b", "c"]))
    validate_plan(plan, registry)


def test_tool_defaults_are_accepted(registry):
    """Optional args may be omitted; the tool supplies defaults."""
    validate_plan(plan_of(echo_step(tool_args={"message": "hi"})), registry)


def test_toolless_step_allowed_alongside_executable_work(registry):
    plan = plan_of(echo_step("s1"),
                   PlanStep(id="s2", description="ask the user for input"))
    validate_plan(plan, registry)


# -- unknown tools ----------------------------------------------------------

def test_unregistered_tool_rejected(registry):
    plan = plan_of(PlanStep(id="s1", description="hack",
                            tool_name="delete_everything"))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert exc.value.details["tool"] == "delete_everything"
    assert exc.value.details["step_id"] == "s1"
    assert "echo" in exc.value.details["available"]


def test_hallucinated_tool_among_valid_ones_rejected(registry):
    plan = plan_of(echo_step("s1"),
                   PlanStep(id="s2", description="x", tool_name="browser"))
    with pytest.raises(PlanValidationError):
        validate_plan(plan, registry)


def test_tool_removed_from_registry_is_rejected(registry):
    """Validation reflects the registry at validation time, not a snapshot."""
    plan = plan_of(echo_step())
    validate_plan(plan, registry)
    registry.unregister("echo")
    with pytest.raises(PlanValidationError):
        validate_plan(plan, registry)


def test_tool_args_without_tool_rejected(registry):
    plan = plan_of(PlanStep(id="s1", description="d", tool_name=None,
                            tool_args={"message": "x"}))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert "without naming a tool" in exc.value.message


# -- argument validation ----------------------------------------------------

def test_missing_required_argument_rejected(registry):
    plan = plan_of(echo_step(tool_args={}))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert exc.value.details["tool"] == "echo"


def test_unknown_argument_rejected(registry):
    plan = plan_of(echo_step(tool_args={"message": "hi", "colour": "red"}))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert "colour" in str(exc.value.details)


def test_wrong_argument_type_rejected(registry):
    plan = plan_of(echo_step(tool_args={"message": 12345}))
    with pytest.raises(PlanValidationError):
        validate_plan(plan, registry)


def test_validation_reuses_the_tools_own_rules(registry):
    """Same code path as the executor, so no plan can pass here and fail there."""
    bad = {"message": "hi", "uppercase": "yes"}  # bool expected
    plan = plan_of(echo_step(tool_args=bad))
    with pytest.raises(PlanValidationError):
        validate_plan(plan, registry)
    with pytest.raises(Exception):
        registry.get("echo").validate_args(bad)


# -- identifiers ------------------------------------------------------------

def test_duplicate_ids_rejected(registry):
    plan = plan_of(echo_step("s1"), echo_step("s1"))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert exc.value.details["duplicates"] == ["s1"]


# -- dependencies -----------------------------------------------------------

def test_unknown_dependency_rejected(registry):
    plan = plan_of(echo_step("s1", depends_on=["nope"]))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert exc.value.details["dependency"] == "nope"


def test_self_dependency_rejected(registry):
    plan = plan_of(echo_step("s1", depends_on=["s1"]))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert "depends on itself" in exc.value.message


def test_two_step_cycle_rejected(registry):
    plan = plan_of(echo_step("s1", depends_on=["s2"]),
                   echo_step("s2", depends_on=["s1"]))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert "cycle" in exc.value.message


def test_long_cycle_rejected(registry):
    plan = plan_of(echo_step("a", depends_on=["c"]),
                   echo_step("b", depends_on=["a"]),
                   echo_step("c", depends_on=["b"]))
    with pytest.raises(PlanValidationError):
        validate_plan(plan, registry)


def test_deep_chain_does_not_overflow(registry):
    """Iterative cycle detection must handle deep graphs."""
    steps = [echo_step("s0")]
    steps += [echo_step(f"s{i}", depends_on=[f"s{i-1}"]) for i in range(1, 40)]
    validate_plan(plan_of(*steps), registry)


# -- executable work --------------------------------------------------------

def test_plan_with_no_executable_step_rejected(registry):
    plan = plan_of(PlanStep(id="s1", description="think about it"))
    with pytest.raises(PlanValidationError) as exc:
        validate_plan(plan, registry)
    assert "no executable step" in exc.value.message


def test_executable_requirement_can_be_relaxed(registry):
    plan = plan_of(PlanStep(id="s1", description="think about it"))
    validate_plan(plan, registry, require_executable=False)


def test_empty_registry_rejects_tool_use():
    plan = plan_of(echo_step())
    with pytest.raises(PlanValidationError):
        validate_plan(plan, ToolRegistry())


# -- integration with parsing ----------------------------------------------

def test_parse_then_validate(registry):
    plan = parse_plan({
        "objective": "greet",
        "steps": [{"id": "s1", "description": "greet", "tool_name": "echo",
                   "tool_args": {"message": "hello"}}]})
    validate_plan(plan, registry)
