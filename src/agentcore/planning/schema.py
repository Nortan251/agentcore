"""Planner output schema and strict parsing.

This is the contract the model must satisfy. It is deliberately a *separate*
intermediate representation rather than the model writing ``Task``/``Subtask``
directly: raw model output is untrusted, so it is parsed into
:class:`PlanStep` objects, validated, and only then converted into the real
task models by :mod:`agentcore.planning.planner`.

Wire shape (``SCHEMA_VERSION`` 1)::

    {
      "objective": "...",
      "steps": [
        {
          "id": "s1",
          "description": "...",
          "tool_name": "echo" | null,
          "tool_args": {...},
          "depends_on": ["s0"],
          "expected_result": "...",
          "requires_verification": false
        }
      ]
    }

Step ids are model-local strings ("s1"): they are used to express dependencies
inside the plan and are then mapped onto real generated ``Subtask`` ids.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agentcore.planning.errors import PlanParseError, PlanSchemaError

#: Bump when the wire format changes incompatibly.
SCHEMA_VERSION = 1

#: Hard ceiling on plan size — a defence against runaway model output.
MAX_STEPS = 50

_ACTION_BINDING_TOKEN = re.compile(r"^wfb_[0-9a-f]{12}$")


@dataclass
class PlanStep:
    """One validated step of a proposed plan."""

    id: str
    description: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    expected_result: str = ""
    requires_verification: bool = False
    action_binding_token: str | None = None

    @property
    def is_executable(self) -> bool:
        """True when the step names a tool and can therefore be run."""
        return bool(self.tool_name)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "description": self.description,
            "tool_name": self.tool_name,
            "tool_args": dict(self.tool_args),
            "depends_on": list(self.depends_on),
            "expected_result": self.expected_result,
            "requires_verification": self.requires_verification,
        }
        if self.action_binding_token is not None:
            result["action_binding_token"] = self.action_binding_token
        return result


@dataclass
class Plan:
    """A parsed, structurally valid plan. Not yet semantically validated."""

    objective: str
    steps: list[PlanStep] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]

    def executable_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.is_executable]

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "schema_version": self.schema_version,
            "steps": [s.to_dict() for s in self.steps],
        }


def json_schema() -> dict[str, Any]:
    """JSON Schema describing the expected output.

    Passed to the model both as ``response_format`` (for providers that enforce
    schemas) and rendered into the prompt (for those that do not).
    """
    return {
        "type": "object",
        "required": ["objective", "steps"],
        "additionalProperties": False,
        "properties": {
            "objective": {"type": "string"},
            "steps": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_STEPS,
                "items": {
                    "type": "object",
                    "required": ["id", "description"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "tool_name": {"type": ["string", "null"]},
                        "tool_args": {"type": "object"},
                        "depends_on": {"type": "array",
                                       "items": {"type": "string"}},
                        "expected_result": {"type": "string"},
                        "requires_verification": {"type": "boolean"},
                        "action_binding_token": {
                            "type": "string",
                            "pattern": "^wfb_[0-9a-f]{12}$",
                        },
                    },
                },
            },
        },
    }


def response_format() -> dict[str, Any]:
    """``ModelRequest.response_format`` value requesting the plan schema."""
    return {
        "type": "json_schema",
        "json_schema": {"name": "agent_plan", "strict": True,
                        "schema": json_schema()},
    }


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull a single JSON object out of raw model text.

    Tolerates the two formatting habits models have even under a schema
    instruction — markdown fences and surrounding prose — but never *guesses*
    at content. Anything that is not a JSON object raises.
    """
    if not text or not text.strip():
        raise PlanParseError("model returned empty output")

    candidate = text.strip()

    # Strip ```json ... ``` fences.
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]  # drop opening fence (and any language tag)
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span, for prose-wrapped replies.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise PlanParseError(
                "model output contained no JSON object",
                preview=_preview(candidate)) from None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PlanParseError("model output was not valid JSON",
                                 reason=str(exc),
                                 preview=_preview(candidate)) from exc

    if not isinstance(parsed, dict):
        raise PlanParseError("model output was not a JSON object",
                             got=type(parsed).__name__)
    return parsed


def parse_plan(payload: dict[str, Any]) -> Plan:
    """Convert a raw payload into a :class:`Plan`, enforcing the schema.

    Strict by design: unknown fields, wrong types and missing required keys are
    all rejected rather than coerced, so malformed output can never be silently
    reshaped into something that looks valid.
    """
    objective = payload.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise PlanSchemaError("plan is missing a non-empty 'objective'",
                              field="objective")

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise PlanSchemaError("plan 'steps' must be a list", field="steps",
                              got=type(raw_steps).__name__)
    if not raw_steps:
        raise PlanSchemaError("plan contains no steps", field="steps")
    if len(raw_steps) > MAX_STEPS:
        raise PlanSchemaError("plan exceeds the maximum number of steps",
                              field="steps", count=len(raw_steps),
                              maximum=MAX_STEPS)

    steps: list[PlanStep] = []
    for index, raw in enumerate(raw_steps):
        steps.append(_parse_step(raw, index))
    return Plan(objective=objective.strip(), steps=steps)


def _parse_step(raw: Any, index: int) -> PlanStep:
    if not isinstance(raw, dict):
        raise PlanSchemaError("plan step must be an object", step=index,
                              got=type(raw).__name__)

    step_id = raw.get("id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise PlanSchemaError("step is missing a non-empty 'id'", step=index,
                              field="id")

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PlanSchemaError("step is missing a non-empty 'description'",
                              step=index, field="description")

    tool_name = raw.get("tool_name")
    if tool_name is not None and not isinstance(tool_name, str):
        raise PlanSchemaError("step 'tool_name' must be a string or null",
                              step=index, field="tool_name",
                              got=type(tool_name).__name__)
    if isinstance(tool_name, str):
        tool_name = tool_name.strip() or None

    tool_args = raw.get("tool_args", {})
    if tool_args is None:
        tool_args = {}
    if not isinstance(tool_args, dict):
        raise PlanSchemaError("step 'tool_args' must be an object", step=index,
                              field="tool_args", got=type(tool_args).__name__)
    if any(not isinstance(key, str) for key in tool_args):
        raise PlanSchemaError("step 'tool_args' keys must be strings",
                              step=index, field="tool_args")

    depends_on = raw.get("depends_on", [])
    if depends_on is None:
        depends_on = []
    if not isinstance(depends_on, list) or any(
            not isinstance(d, str) for d in depends_on):
        raise PlanSchemaError("step 'depends_on' must be a list of strings",
                              step=index, field="depends_on")

    expected_result = raw.get("expected_result", "")
    if expected_result is None:
        expected_result = ""
    if not isinstance(expected_result, str):
        raise PlanSchemaError("step 'expected_result' must be a string",
                              step=index, field="expected_result")

    requires_verification = raw.get("requires_verification", False)
    if requires_verification is None:
        requires_verification = False
    if not isinstance(requires_verification, bool):
        raise PlanSchemaError("step 'requires_verification' must be a boolean",
                              step=index, field="requires_verification")

    action_binding_token = raw.get("action_binding_token")
    if action_binding_token is not None and (
        not isinstance(action_binding_token, str)
        or not _ACTION_BINDING_TOKEN.fullmatch(action_binding_token)
    ):
        raise PlanSchemaError(
            "step 'action_binding_token' is invalid",
            step=index,
            field="action_binding_token",
        )

    return PlanStep(
        id=step_id.strip(),
        description=description.strip(),
        tool_name=tool_name,
        tool_args=dict(tool_args),
        depends_on=[d.strip() for d in depends_on if d.strip()],
        expected_result=expected_result.strip(),
        requires_verification=requires_verification,
        action_binding_token=action_binding_token,
    )


def _preview(text: str, limit: int = 200) -> str:
    """Short, safe excerpt of bad output for error details."""
    condensed = " ".join(text.split())
    return condensed[:limit] + ("…" if len(condensed) > limit else "")
