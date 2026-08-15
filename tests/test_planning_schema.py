"""Tests for planner output parsing and the strict plan schema."""

from __future__ import annotations

import json

import pytest

from agentcore.planning.errors import PlanParseError, PlanSchemaError
from agentcore.planning.schema import (
    MAX_STEPS,
    SCHEMA_VERSION,
    extract_json_object,
    json_schema,
    parse_plan,
    response_format,
)


def payload(**overrides):
    base = {"objective": "do a thing",
            "steps": [{"id": "s1", "description": "step one",
                       "tool_name": "echo", "tool_args": {"message": "hi"}}]}
    base.update(overrides)
    return base


# -- JSON extraction --------------------------------------------------------

def test_plain_json_object():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_markdown_fenced_json():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_fenced_without_language_tag():
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_prose_wrapped_json():
    text = 'Sure! Here is the plan:\n{"a": 1}\nHope that helps.'
    assert extract_json_object(text) == {"a": 1}


def test_empty_output_rejected():
    for value in ("", "   ", "\n"):
        with pytest.raises(PlanParseError):
            extract_json_object(value)


def test_non_json_text_rejected():
    with pytest.raises(PlanParseError) as exc:
        extract_json_object("I cannot help with that request.")
    assert "no JSON object" in exc.value.message


def test_broken_json_rejected():
    with pytest.raises(PlanParseError):
        extract_json_object('{"a": 1,,,}')


def test_json_array_rejected():
    with pytest.raises(PlanParseError) as exc:
        extract_json_object('[1, 2, 3]')
    assert "not a JSON object" in exc.value.message


def test_error_preview_is_truncated():
    with pytest.raises(PlanParseError) as exc:
        extract_json_object("x" * 500)
    assert len(exc.value.details["preview"]) <= 201


# -- schema parsing ---------------------------------------------------------

def test_minimal_valid_plan():
    plan = parse_plan(payload())
    assert plan.objective == "do a thing"
    assert plan.schema_version == SCHEMA_VERSION
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.id == "s1"
    assert step.tool_name == "echo"
    assert step.is_executable


def test_all_fields_parsed():
    plan = parse_plan(payload(steps=[{
        "id": "s1", "description": "d", "tool_name": "echo",
        "tool_args": {"message": "m"}, "depends_on": [],
        "expected_result": "an echo", "requires_verification": True}]))
    step = plan.steps[0]
    assert step.expected_result == "an echo"
    assert step.requires_verification is True


def test_optional_fields_default():
    plan = parse_plan(payload(steps=[{"id": "s1", "description": "d"}]))
    step = plan.steps[0]
    assert step.tool_name is None
    assert step.tool_args == {}
    assert step.depends_on == []
    assert step.expected_result == ""
    assert step.requires_verification is False
    assert not step.is_executable


def test_explicit_nulls_treated_as_defaults():
    plan = parse_plan(payload(steps=[{
        "id": "s1", "description": "d", "tool_name": None, "tool_args": None,
        "depends_on": None, "expected_result": None,
        "requires_verification": None}]))
    assert plan.steps[0].tool_args == {}
    assert plan.steps[0].depends_on == []


def test_whitespace_is_stripped():
    plan = parse_plan(payload(objective="  goal  ", steps=[
        {"id": " s1 ", "description": " d ", "tool_name": " echo "}]))
    assert plan.objective == "goal"
    assert plan.steps[0].id == "s1"
    assert plan.steps[0].tool_name == "echo"


def test_blank_tool_name_becomes_none():
    plan = parse_plan(payload(steps=[
        {"id": "s1", "description": "d", "tool_name": "   "}]))
    assert plan.steps[0].tool_name is None


# -- schema rejections ------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {},                                              # nothing
    {"steps": []},                                   # no objective
    {"objective": "", "steps": [{"id": "s", "description": "d"}]},
    {"objective": "   ", "steps": [{"id": "s", "description": "d"}]},
    {"objective": 42, "steps": [{"id": "s", "description": "d"}]},
])
def test_missing_or_bad_objective_rejected(bad):
    with pytest.raises(PlanSchemaError):
        parse_plan(bad)


def test_missing_steps_rejected():
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan({"objective": "x"})
    assert exc.value.details["field"] == "steps"


def test_steps_wrong_type_rejected():
    with pytest.raises(PlanSchemaError):
        parse_plan({"objective": "x", "steps": {"id": "s1"}})


def test_empty_steps_rejected():
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan({"objective": "x", "steps": []})
    assert "no steps" in exc.value.message


def test_too_many_steps_rejected():
    steps = [{"id": f"s{i}", "description": "d"} for i in range(MAX_STEPS + 1)]
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan({"objective": "x", "steps": steps})
    assert exc.value.details["maximum"] == MAX_STEPS


def test_step_not_an_object_rejected():
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan(payload(steps=["just a string"]))
    assert exc.value.details["step"] == 0


@pytest.mark.parametrize("step,field", [
    ({"description": "d"}, "id"),
    ({"id": "", "description": "d"}, "id"),
    ({"id": 5, "description": "d"}, "id"),
    ({"id": "s1"}, "description"),
    ({"id": "s1", "description": ""}, "description"),
    ({"id": "s1", "description": 99}, "description"),
])
def test_required_step_fields_rejected(step, field):
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan(payload(steps=[step]))
    assert exc.value.details["field"] == field


@pytest.mark.parametrize("extra,field", [
    ({"tool_name": 123}, "tool_name"),
    ({"tool_args": "not a dict"}, "tool_args"),
    ({"tool_args": [1, 2]}, "tool_args"),
    ({"depends_on": "s0"}, "depends_on"),
    ({"depends_on": [1, 2]}, "depends_on"),
    ({"expected_result": {"a": 1}}, "expected_result"),
    ({"requires_verification": "yes"}, "requires_verification"),
])
def test_wrong_field_types_rejected(extra, field):
    step = {"id": "s1", "description": "d", **extra}
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan(payload(steps=[step]))
    assert exc.value.details["field"] == field


def test_index_reported_for_bad_step():
    steps = [{"id": "s1", "description": "ok"}, {"id": "s2"}]
    with pytest.raises(PlanSchemaError) as exc:
        parse_plan(payload(steps=steps))
    assert exc.value.details["step"] == 1


# -- schema documents -------------------------------------------------------

def test_json_schema_is_serialisable_and_strict():
    schema = json_schema()
    json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["objective", "steps"]
    assert schema["properties"]["steps"]["maxItems"] == MAX_STEPS


def test_response_format_requests_the_schema():
    fmt = response_format()
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "agent_plan"
    assert fmt["json_schema"]["schema"] == json_schema()


def test_plan_helpers():
    plan = parse_plan(payload(steps=[
        {"id": "s1", "description": "a", "tool_name": "echo",
         "tool_args": {"message": "x"}},
        {"id": "s2", "description": "b"}]))
    assert plan.step_ids() == ["s1", "s2"]
    assert len(plan.executable_steps()) == 1
    assert plan.to_dict()["schema_version"] == SCHEMA_VERSION
