"""Planning layer: objective -> model reasoning -> validated Task.

Satisfies the existing ``agentcore.core.interfaces.Planner`` Protocol and
depends only on the neutral ``ModelProvider`` abstraction and the live
``ToolRegistry`` — never on a vendor API or a hard-coded tool list.

The planner proposes work. It never executes tools and cannot affect
permission decisions.
"""

from agentcore.planning.errors import (
    PlanGenerationError,
    PlanningError,
    PlanParseError,
    PlanSchemaError,
    PlanValidationError,
)
from agentcore.planning.planner import LLMPlanner
from agentcore.planning.prompt import (
    build_system_prompt,
    build_user_prompt,
    format_tool_catalogue,
)
from agentcore.planning.schema import (
    MAX_STEPS,
    SCHEMA_VERSION,
    Plan,
    PlanStep,
    extract_json_object,
    json_schema,
    parse_plan,
    response_format,
)
from agentcore.planning.validator import validate_plan

__all__ = [
    "LLMPlanner",
    # schema
    "Plan",
    "PlanStep",
    "parse_plan",
    "extract_json_object",
    "json_schema",
    "response_format",
    "SCHEMA_VERSION",
    "MAX_STEPS",
    # validation
    "validate_plan",
    # prompt
    "build_system_prompt",
    "build_user_prompt",
    "format_tool_catalogue",
    # errors
    "PlanningError",
    "PlanGenerationError",
    "PlanParseError",
    "PlanSchemaError",
    "PlanValidationError",
]
