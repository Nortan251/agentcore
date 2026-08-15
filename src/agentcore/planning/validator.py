"""Semantic validation of a parsed plan.

Separated from parsing so the rules are testable in isolation and so the
distinction between "malformed output" and "unusable plan" stays visible in
errors.

Validation is *rejection-only*: nothing here repairs, substitutes or invents
content. A plan that does not validate never becomes a Task.

Note on scope: this checks that a proposed tool call is well-formed and refers
to a real tool. It deliberately does **not** decide whether the action is
permitted — that remains the job of ``PermissionPolicy`` at execution time, so
the planner cannot widen what the agent is allowed to do.
"""

from __future__ import annotations

from agentcore.errors import ToolNotFoundError, ToolValidationError
from agentcore.planning.errors import PlanValidationError
from agentcore.planning.schema import Plan
from agentcore.tools.registry import ToolRegistry


def validate_plan(plan: Plan, registry: ToolRegistry, *,
                  require_executable: bool = True) -> Plan:
    """Validate a plan against the live tool registry.

    Checks, in order: unique ids, known tools, valid arguments, resolvable and
    acyclic dependencies, and the presence of at least one executable step.

    Returns the plan unchanged on success; raises
    :class:`PlanValidationError` otherwise.
    """
    _validate_unique_ids(plan)
    _validate_tools_and_args(plan, registry)
    _validate_dependencies(plan)
    if require_executable:
        _validate_has_executable_work(plan)
    return plan


def _validate_unique_ids(plan: Plan) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for step in plan.steps:
        if step.id in seen:
            duplicates.append(step.id)
        seen.add(step.id)
    if duplicates:
        raise PlanValidationError("plan contains duplicate step ids",
                                  duplicates=sorted(set(duplicates)))


def _validate_tools_and_args(plan: Plan, registry: ToolRegistry) -> None:
    """Reject unknown tools and arguments the tool itself would refuse.

    Argument checking reuses ``Tool.validate_args`` — the same code path the
    executor uses — so the planner can never accept a call the executor would
    later reject.
    """
    available = registry.names()
    for step in plan.steps:
        if step.tool_name is None:
            if step.tool_args:
                raise PlanValidationError(
                    "step supplies tool_args without naming a tool",
                    step_id=step.id)
            continue

        try:
            tool = registry.get(step.tool_name)
        except ToolNotFoundError as exc:
            raise PlanValidationError(
                f"plan references unregistered tool '{step.tool_name}'",
                step_id=step.id, tool=step.tool_name,
                available=available) from exc

        try:
            tool.validate_args(step.tool_args)
        except ToolValidationError as exc:
            # Nest the tool's own details rather than splatting them: keys such
            # as "tool" would otherwise collide with the arguments below.
            raise PlanValidationError(
                f"invalid arguments for tool '{step.tool_name}': {exc.message}",
                step_id=step.id, tool=step.tool_name,
                reason=exc.message, tool_details=dict(exc.details)) from exc


def _validate_dependencies(plan: Plan) -> None:
    """Reject unknown dependencies, self-dependencies and cycles.

    Cycles are caught here rather than relying on ``Task.validate`` because the
    plan is rejected *before* any Task is built.
    """
    ids = set(plan.step_ids())
    graph: dict[str, list[str]] = {}

    for step in plan.steps:
        for dep in step.depends_on:
            if dep == step.id:
                raise PlanValidationError("step depends on itself",
                                          step_id=step.id)
            if dep not in ids:
                raise PlanValidationError(
                    f"step depends on unknown step '{dep}'",
                    step_id=step.id, dependency=dep, known=sorted(ids))
        graph[step.id] = list(step.depends_on)

    _assert_acyclic(graph)


def _assert_acyclic(graph: dict[str, list[str]]) -> None:
    """Iterative DFS cycle detection (mirrors ``Task._assert_acyclic``)."""
    state: dict[str, int] = {}  # 0 = visiting, 1 = done

    def visit(start: str) -> None:
        stack: list[tuple[str, list[str]]] = [(start, [])]
        while stack:
            node, trail = stack.pop()
            mark = state.get(node)
            if mark == 1:
                continue
            if mark == 0:
                # Completed the post-visit for this node.
                state[node] = 1
                continue
            state[node] = 0
            # Re-push to finalise after children are processed.
            stack.append((node, trail))
            for dep in graph.get(node, []):
                if state.get(dep) == 0:
                    cycle = trail + [node, dep]
                    raise PlanValidationError(
                        "plan contains a dependency cycle",
                        cycle=_trim_cycle(cycle, dep))
                if state.get(dep) != 1:
                    stack.append((dep, trail + [node]))

    for node in graph:
        if state.get(node) != 1:
            visit(node)


def _trim_cycle(trail: list[str], start: str) -> list[str]:
    if start in trail:
        return trail[trail.index(start):]
    return trail


def _validate_has_executable_work(plan: Plan) -> None:
    """A plan with no tool-backed step cannot advance the objective."""
    if not plan.executable_steps():
        raise PlanValidationError(
            "plan contains no executable step (no step names a tool)",
            steps=len(plan.steps))
