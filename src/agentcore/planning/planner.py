"""LLM-backed planner.

Pipeline::

    objective
      -> tool catalogue (registry.describe())
      -> prompt + response_format
      -> ModelProvider.complete()      [vendor-neutral]
      -> extract + parse JSON          [strict schema]
      -> validate against registry     [rejection-only]
      -> Task / Subtask objects        [existing models]

The planner **proposes**; it never executes. It holds no reference to
``ToolExecutor`` and never calls ``Tool.run``: the only thing it does with the
registry is read the catalogue and validate arguments. Execution, capability
checks and approval remain entirely with the existing runner/permission layer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Mapping

from agentcore.core.action_binding import (
    ACTION_BINDING_OFFERS_CONTEXT_KEY,
    ReplanActionBindingOffer,
)
from agentcore.core.interfaces import ModelProvider
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.planning.errors import PlanGenerationError, PlanningError
from agentcore.planning.failure_context import build_failure_context
from agentcore.planning.memory_context import (
    DEFAULT_MAX_CONTENT_CHARS,
    DEFAULT_MAX_MEMORIES,
    attach_memory_context,
    build_memory_context,
)
from agentcore.planning.prompt import build_system_prompt, build_user_prompt
from agentcore.planning.schema import (
    Plan,
    PlanStep,
    extract_json_object,
    parse_plan,
    response_format,
)
from agentcore.planning.validator import validate_plan
from agentcore.state.models import AgentState
from agentcore.tasks.models import Subtask, Task
from agentcore.tools.registry import ToolRegistry


class LLMPlanner:
    """Satisfies the existing ``core.interfaces.Planner`` Protocol."""

    def __init__(
        self,
        model: ModelProvider,
        registry: ToolRegistry,
        *,
        temperature: float = 0.0,
        require_executable: bool = True,
        max_replans: int | None = None,
        memory: Any | None = None,
        max_memories: int = DEFAULT_MAX_MEMORIES,
        max_memory_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        logger: StructuredLogger | None = None,
    ) -> None:
        if model is None:
            raise PlanningError("LLMPlanner requires a model provider")
        self.model = model
        self.registry = registry
        # Durable memory is optional. ``None`` — the default, and what the
        # Agent supplies when ``features.memory`` is off — means no lookup is
        # ever attempted and the prompt is exactly what it was before.
        self.memory = memory
        self.max_memories = max_memories
        self.max_memory_chars = max_memory_chars
        # Default to 0.0: planning should be as deterministic as the provider
        # allows, which also makes behaviour reproducible in tests.
        self.temperature = temperature
        self.require_executable = require_executable
        # Purely informational: lets the replan prompt tell the model how many
        # further attempts remain. None means "do not mention it".
        self.max_replans = max_replans
        self._log = logger or get_logger("planning.planner")

    # -- Planner Protocol --------------------------------------------------
    def plan(self, objective: str, *,
             context: dict[str, Any] | None = None,
             replanning: bool = False) -> Task:
        """Produce a validated :class:`Task` for ``objective``.

        ``replanning`` only selects failure-aware system guidance; the output
        schema and every validation step are identical either way.
        """
        if not objective or not objective.strip():
            raise PlanningError("objective must not be empty")

        objective = objective.strip()
        catalogue = self.registry.describe()
        log = self._log.bind(objective=objective, tools=len(catalogue))
        log.info(Events.PLAN_REQUESTED)

        # The single memory-retrieval point. ``replan()`` delegates here, so
        # both paths get identical retrieval, bounding and safety framing
        # without a second integration.
        context = attach_memory_context(context, self._recall(objective))

        text = self._ask_model(objective, catalogue, context, log,
                               replanning=replanning)
        plan = self._parse(text, log)
        validate_plan(plan, self.registry,
                      require_executable=self.require_executable)
        task = self.to_task(plan, context=context)

        log.info(Events.PLAN_CREATED, task_id=task.id,
                 steps=len(plan.steps),
                 executable=len(plan.executable_steps()))
        return task

    def replan(
        self,
        task: Task,
        state: AgentState,
        *,
        action_bindings: Sequence[ReplanActionBindingOffer] = (),
    ) -> Task:
        """Re-plan the objective using the structured history in state.

        Reads the persisted :class:`ReplanLedger` and :class:`RecoveryHistory`
        so the model can see *what was already tried and why it failed*, rather
        than only the latest error. No extra model call is made: the enriched
        context rides along with the single planning request.

        History extraction is best-effort — a replan must not fail merely
        because history is missing or corrupt.
        """
        try:
            failure = build_failure_context(task, state,
                                            max_replans=self.max_replans)
            context: dict[str, Any] = failure.to_dict()
            categories = failure.failure_categories
        except Exception as exc:  # noqa: BLE001 - history must never block a replan
            self._log.warning(Events.PLAN_FAILED, stage="failure_context",
                              error=type(exc).__name__, message=str(exc))
            context = {"replanning": True, "objective": task.objective}
            categories = []

        safe_offers: list[dict[str, str]] = []
        for offer in action_bindings:
            if not isinstance(offer, ReplanActionBindingOffer):
                raise PlanningError("replan action binding offer is invalid")
            safe_offers.append(offer.validate().to_model_context())
        if safe_offers:
            context[ACTION_BINDING_OFFERS_CONTEXT_KEY] = safe_offers

        self._log.info(Events.PLAN_REVISED, task_id=task.id,
                       run_id=state.run_id,
                       earlier_attempts=len(context.get(
                           "earlier_failed_attempts", [])),
                       failure_categories=categories)
        return self.plan(task.objective, context=context, replanning=True)

    # -- memory ------------------------------------------------------------
    def _recall(self, objective: str) -> dict[str, Any] | None:
        """Retrieve bounded memory context for ``objective``, or None.

        A thin delegation to :func:`build_memory_context`, which owns the
        interface call, the bounds and the safety framing. Retrieval is
        best-effort by construction: planning must never fail because durable
        memory is unavailable.
        """
        if self.memory is None:
            return None
        return build_memory_context(
            self.memory, objective,
            max_memories=self.max_memories,
            max_content_chars=self.max_memory_chars,
            logger=self._log)

    # -- pipeline stages ---------------------------------------------------
    def _ask_model(self, objective: str, catalogue: list[dict[str, Any]],
                   context: Mapping[str, Any] | None,
                   log: StructuredLogger, *, replanning: bool = False) -> str:
        """Call the model through the neutral Protocol.

        Uses ``complete()`` with ``response_format`` so any provider satisfying
        the existing Protocol works; providers that honour JSON schemas will
        enforce it, and strict parsing catches those that do not.
        """
        try:
            return self.model.complete(
                build_user_prompt(objective, catalogue, context=context),
                system=build_system_prompt(replanning=replanning),
                temperature=self.temperature,
                response_format=response_format(),
            )
        except PlanningError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise provider failures
            log.warning(Events.PLAN_FAILED, stage="generation",
                        error=type(exc).__name__,
                        message=getattr(exc, "message", str(exc)))
            raise PlanGenerationError(
                "model failed to produce a plan",
                reason=getattr(exc, "message", str(exc)),
                error_type=type(exc).__name__) from exc

    def _parse(self, text: str, log: StructuredLogger) -> Plan:
        try:
            plan = parse_plan(extract_json_object(text))
        except PlanningError as exc:
            log.warning(Events.PLAN_FAILED, stage="parse",
                        error=type(exc).__name__, message=exc.message,
                        **exc.details)
            raise
        log.debug(Events.PLAN_PARSED, steps=len(plan.steps))
        return plan

    # -- conversion --------------------------------------------------------
    def to_task(self, plan: Plan, *,
                context: Mapping[str, Any] | None = None) -> Task:
        """Convert a validated plan into existing Task/Subtask objects.

        Two details matter:

        * Model-local step ids ("s1") are mapped onto generated ``Subtask``
          ids, so nothing model-controlled leaks into identifiers.
        * ``Task.add_subtask`` requires dependencies to exist already, so
          subtasks are inserted in topological order.
        """
        metadata = dict(context or {})
        metadata.pop(ACTION_BINDING_OFFERS_CONTEXT_KEY, None)
        task = Task(objective=plan.objective, metadata=metadata)
        subtasks = {step.id: self._to_subtask(step) for step in plan.steps}
        id_map = {step_id: sub.id for step_id, sub in subtasks.items()}

        for step in self._topological_order(plan):
            subtask = subtasks[step.id]
            subtask.depends_on = [id_map[d] for d in step.depends_on]
            task.add_subtask(subtask)

        # The Task model has no fields for expected_result /
        # requires_verification, so rather than dropping them they are recorded
        # in metadata keyed by the real subtask id. A future verification layer
        # can consume this without any model change.
        task.metadata["plan"] = {
            "schema_version": plan.schema_version,
            "annotations": {
                id_map[step.id]: {
                    "expected_result": step.expected_result,
                    "requires_verification": step.requires_verification,
                    **(
                        {"action_binding_token": step.action_binding_token}
                        if step.action_binding_token is not None
                        else {}
                    ),
                }
                for step in plan.steps
                if (
                    step.expected_result
                    or step.requires_verification
                    or step.action_binding_token is not None
                )
            },
        }

        task.validate()  # belt and braces: reuse the existing invariant check
        return task

    @staticmethod
    def _to_subtask(step: PlanStep) -> Subtask:
        return Subtask(
            description=step.description,
            tool_name=step.tool_name,
            tool_args=dict(step.tool_args),
        )

    @staticmethod
    def _topological_order(plan: Plan) -> list[PlanStep]:
        """Order steps so every dependency precedes its dependents.

        The graph is already known to be acyclic (validation ran first), so
        Kahn's algorithm always consumes every node.
        """
        by_id = {s.id: s for s in plan.steps}
        remaining = {s.id: set(s.depends_on) for s in plan.steps}
        ordered: list[PlanStep] = []

        while remaining:
            ready = [sid for sid, deps in remaining.items() if not deps]
            if not ready:  # pragma: no cover - prevented by validation
                raise PlanningError("plan dependencies could not be ordered",
                                    remaining=sorted(remaining))
            # Preserve the model's original ordering among ready steps.
            ready.sort(key=lambda sid: plan.step_ids().index(sid))
            for sid in ready:
                ordered.append(by_id[sid])
                del remaining[sid]
            for deps in remaining.values():
                deps.difference_update(ready)
        return ordered
