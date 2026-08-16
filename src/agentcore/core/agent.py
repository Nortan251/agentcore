"""Agent orchestrator: wires configuration, tools, permissions, state and
execution together.

The agent owns *composition*, not behaviour. Planning, verification, recovery,
memory and model access are injected, so a later task can supply a real planner
or a local computer-control tool without editing this class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from agentcore.config.schema import AgentConfig
from agentcore.core.defaults import NoRetryStrategy, NullVerifier, PassthroughPlanner
from agentcore.core.interfaces import (
    MemoryStore,
    ModelProvider,
    Planner,
    RecoveryStrategy,
    Verifier,
)
from agentcore.core.plan_signature import (
    describe_signature,
    find_duplicate,
    plan_signature,
)
from agentcore.core.replan import ReplanLedger, ReplanOutcome
from agentcore.core.runner import RunReport, TaskRunner
from agentcore.errors import AgentError, StateError
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import WorkspaceMismatchError
from agentcore.observability.events import Events
from agentcore.observability.logging import configure_logging, get_logger
from agentcore.permissions.policy import ApprovalGate, PermissionPolicy
from agentcore.models.messages import Message, Role
from agentcore.state.models import AgentPhase, AgentState
from agentcore.state.store import InMemoryStateStore, StateStore
from agentcore.tasks.models import Task, TaskStatus
from agentcore.tools.base import Tool
from agentcore.tools.builtin import default_tools
from agentcore.tools.executor import ToolExecutor, ToolInvocation
from agentcore.tools.registry import ToolRegistry


@dataclass
class AgentDependencies:
    """Optional injected collaborators. Anything omitted gets a safe default."""

    tools: Sequence[Tool] | None = None
    state_store: StateStore | None = None
    planner: Planner | None = None
    verifier: Verifier | None = None
    recovery: RecoveryStrategy | None = None
    approval_gate: ApprovalGate | None = None
    memory: MemoryStore | None = None
    #: Conversational memory capture. Optional and injectable; when omitted
    #: one is built only if a memory store exists and capture is enabled.
    memory_capture: Any | None = None
    #: Applies capture decisions and corrections. Injectable; built only when
    #: a memory store exists.
    memory_resolver: Any | None = None
    #: Durable pending memory conflicts. Injectable; built only when a memory
    #: store exists.
    memory_conflicts: Any | None = None
    model: ModelProvider | None = None


class Agent:
    """The composition root for one agent instance."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        deps: AgentDependencies | None = None,
    ) -> None:
        self.config = (config or AgentConfig()).validate()
        deps = deps or AgentDependencies()
        configure_logging(self.config.logging)
        self.log = get_logger("core.agent", agent=self.config.agent_name,
                              environment=self.config.environment)

        self.policy = PermissionPolicy(self.config.permissions,
                                       approval_gate=deps.approval_gate)
        self.registry = ToolRegistry(self.config.tools)
        self.registry.register_all(list(
            default_tools() if deps.tools is None else deps.tools))

        # The canonical workspace root is established exactly once, here, and
        # handed to tools through the guard. Nothing downstream re-derives it.
        self.workspace = WorkspaceGuard(
            self.config.canonical_workspace(),
            config=self.config.filesystem,
        )

        self.executor = ToolExecutor(
            self.registry, self.policy,
            workspace=self.workspace,
            tool_settings=self.config.tools.settings,
        )

        # Planner is resolved after the model, since the LLM planner needs it.
        self._injected_planner = deps.planner
        # Verifier is resolved after the model (see _build_verifier).
        self._injected_verifier = deps.verifier
        # Recovery is resolved after the model/registry (see _build_recovery).
        self._injected_recovery = deps.recovery
        self.state_store: StateStore = deps.state_store or InMemoryStateStore()
        # Durable memory: injected store wins, else built only when the
        # feature flag is on. With the flag off this stays None and nothing
        # is ever written, exactly as before this layer existed.
        self.memory: MemoryStore | None = deps.memory or self._build_memory()
        # An injected provider always wins; otherwise one is built from config.
        # With the default ``provider = "none"`` this stays None, so an agent
        # without model configuration behaves exactly as before.
        self.model: ModelProvider | None = deps.model or self._build_model()
        self.planner: Planner = self._build_planner()
        # Capture needs the model, so it is resolved after it.
        # Conflicts are built first: capture and the resolver both take one.
        self.memory_conflicts: Any | None = (
            deps.memory_conflicts or self._build_conflict_store())
        self.memory_capture: Any | None = (
            deps.memory_capture or self._build_memory_capture())
        self.memory_resolver: Any | None = (
            deps.memory_resolver or self._build_memory_resolver())
        self.verifier: Verifier = self._build_verifier()
        self.recovery: RecoveryStrategy = self._build_recovery()

        self.runner = TaskRunner(self.executor, verifier=self.verifier,
                                 recovery=self.recovery,
                                 logger=get_logger("core.runner"))
        # New runs are pinned to this workspace from the outset.
        self.state = AgentState(workspace=self.workspace.identity)
        self.log.info(Events.AGENT_STARTED, run_id=self.state.run_id,
                      permission_level=str(self.policy.level),
                      tools=self.registry.names(),
                      planner=type(self.planner).__name__,
                      features=self.config.features.enabled_flags())

    # -- recovery ----------------------------------------------------------
    def _build_recovery(self) -> RecoveryStrategy:
        """Choose the recovery strategy, honouring injection and the flag.

        Precedence mirrors the planner and verifier:
          1. an explicitly injected strategy (always wins, flag irrelevant)
          2. the real ``RecoveryPolicy`` when ``features.recovery`` is on
          3. the existing ``NoRetryStrategy`` otherwise

        With the flag off — the default — behaviour is unchanged: nothing is
        ever retried, replanned or escalated.
        """
        if self._injected_recovery is not None:
            return self._injected_recovery
        if not self.config.features.recovery:
            return NoRetryStrategy()

        from agentcore.recovery.policy import RecoveryPolicy

        # The registry is supplied so retry safety can be judged from the real
        # tool contracts rather than assumed.
        policy = RecoveryPolicy(self.config.recovery, registry=self.registry)
        self.log.info(Events.RECOVERY_CONFIGURED,
                      max_attempts=policy.config.max_attempts,
                      max_replans=policy.config.max_replans)
        return policy

    # -- verifier ----------------------------------------------------------
    def _build_verifier(self) -> Verifier:
        """Choose the verifier, honouring injection and the feature flag.

        Precedence mirrors the planner:
          1. an explicitly injected verifier (always wins, flag irrelevant)
          2. the real ``Verifier`` when ``features.verification`` is on — with
             model-assisted escalation only if a model is available
          3. the existing ``NullVerifier`` otherwise

        With the flag off — the default — behaviour is unchanged: SKIPPED, and
        no model is ever consulted.
        """
        if self._injected_verifier is not None:
            return self._injected_verifier
        if not self.config.features.verification:
            return NullVerifier()

        from agentcore.verification.verifier import Verifier as RealVerifier

        verifier = RealVerifier(model=self.model)
        self.log.info(Events.VERIFIER_CONFIGURED, verifier="Verifier",
                      model_assisted=verifier.has_model)
        return verifier

    # -- planner -----------------------------------------------------------
    def _build_planner(self) -> Planner:
        """Choose the planner, honouring injection and the feature flag.

        Precedence:
          1. an explicitly injected planner (always wins, flag irrelevant)
          2. the LLM planner, when ``features.planning`` is on AND a model is
             available
          3. the existing ``PassthroughPlanner`` otherwise

        With the flag off — the default — behaviour is byte-for-byte what it
        was before this task.
        """
        if self._injected_planner is not None:
            return self._injected_planner
        if not self.config.features.planning:
            return PassthroughPlanner()
        if self.model is None:
            self.log.warning(Events.PLANNER_UNAVAILABLE,
                             reason="planning enabled but no model configured")
            return PassthroughPlanner()

        from agentcore.planning.errors import PlanningError
        from agentcore.planning.planner import LLMPlanner

        try:
            planner = LLMPlanner(
                self.model, self.registry,
                max_replans=self.config.recovery.max_replans,
                # Memory is handed to the planner only when a store exists —
                # i.e. when features.memory is on or one was injected. With no
                # store the planner never looks memory up at all.
                memory=self.memory,
                max_memories=self.config.memory.max_planning_memories,
                max_memory_chars=self.config.memory.max_planning_memory_chars)
        except PlanningError as exc:  # pragma: no cover - defensive
            self.log.warning(Events.PLANNER_UNAVAILABLE,
                             error=type(exc).__name__, message=exc.message)
            return PassthroughPlanner()
        self.log.info(Events.PLANNER_CONFIGURED, planner="LLMPlanner")
        return planner

    # -- model -------------------------------------------------------------
    def _build_model(self) -> ModelProvider | None:
        """Construct a provider from configuration, if one is configured.

        Imported lazily so the core package never depends on a concrete vendor
        implementation at import time. A misconfigured provider is logged and
        downgraded to ``None`` rather than preventing the agent from starting —
        the model is optional until a Planner requires it.
        """
        provider = (self.config.model.provider or "").strip().lower()
        if not provider or provider == "none":
            return None
        from agentcore.models.errors import ModelError
        from agentcore.models.factory import create_provider

        try:
            model = create_provider(self.config.model)
        except ModelError as exc:
            self.log.warning(Events.MODEL_UNAVAILABLE, provider=provider,
                             error=type(exc).__name__, message=exc.message)
            return None
        self.log.info(Events.MODEL_CONFIGURED, **model.describe())
        return model

    # -- memory ------------------------------------------------------------
    def _build_memory(self) -> MemoryStore | None:
        """Construct the durable memory store, if the feature is enabled.

        Precedence mirrors every other seam:
          1. an explicitly injected store (handled by the caller; always wins)
          2. the JSON-file store when ``features.memory`` is on
          3. ``None`` otherwise

        With the flag off — the default — the agent holds no memory store, so
        it cannot read or write durable memory even by accident. Imported
        lazily so the core package never depends on a concrete backend at
        import time, matching ``_build_model``.
        """
        if not self.config.features.memory:
            return None

        from agentcore.memory.errors import MemoryError as MemoryLayerError
        from agentcore.memory.store import build_memory_store

        try:
            store = build_memory_store(self.config)
        except (MemoryLayerError, OSError) as exc:
            # A misconfigured memory directory must not stop the agent from
            # starting; memory is optional to every existing code path.
            self.log.warning(Events.MEMORY_UNREADABLE,
                             error=type(exc).__name__)
            return None
        self.log.info(Events.MEMORY_CONFIGURED,
                      backend=type(store).__name__,
                      directory=str(getattr(store, "directory", "")),
                      max_records=self.config.memory.max_records)
        return store

    def _build_memory_capture(self) -> Any | None:
        """Build conversational capture, if there is a store and it is enabled.

        Capture is strictly subordinate to the memory store: with no store
        there is nowhere to write, so it stays ``None`` and no capture can
        occur. ``memory.capture_enabled`` lets an operator keep memory for
        retrieval while refusing automatic capture.

        Building it does not make the agent capture anything — nothing calls
        it implicitly. It is an explicit entry point (:meth:`remember_from`).
        """
        if self.memory is None or not self.config.memory.capture_enabled:
            return None

        from agentcore.memory.capture import CapturePolicy, MemoryCapture

        policy = CapturePolicy(
            redact_keys=self.config.logging.redact_keys,
            max_content_chars=self.config.memory.max_capture_chars,
            allow_inferred=self.config.memory.capture_inferred)
        return MemoryCapture(
            self.memory, policy=policy, model=self.model,
            conflicts=self.memory_conflicts,
            max_candidates=self.config.memory.max_capture_candidates,
            max_content_chars=self.config.memory.max_capture_chars,
            logger=get_logger("memory.capture"))

    def _build_memory_resolver(self) -> Any | None:
        """Build the correction/resolution layer, if there is a store.

        Unlike capture, this is *not* gated on ``capture_enabled``: an
        operator must be able to correct or delete durable memory even when
        automatic capture is switched off. With no store there is nothing to
        resolve, so it stays ``None``.
        """
        if self.memory is None:
            return None

        from agentcore.memory.resolve import MemoryResolver

        return MemoryResolver(self.memory, conflicts=self.memory_conflicts,
                              logger=get_logger("memory.resolve"))

    def _build_conflict_store(self) -> Any | None:
        """Durable pending conflicts, alongside the memory store.

        Not gated on ``capture_enabled``: an operator must still be able to
        see and settle conflicts recorded earlier even if automatic capture is
        later switched off.
        """
        if self.memory is None:
            return None

        from agentcore.memory.conflicts import build_conflict_store

        try:
            return build_conflict_store(self.config)
        except OSError as exc:
            self.log.warning(Events.MEMORY_UNREADABLE,
                             component="conflicts", error=type(exc).__name__)
            return None

    def pending_memory_conflicts(self, *, limit: int | None = None) -> list:
        """Unresolved memory contradictions awaiting an explicit decision."""
        if self.memory_resolver is None:
            return []
        return self.memory_resolver.list_pending_conflicts(limit=limit)

    def resolve_memory_conflict(self, conflict_id: str, choice: Any, *,
                                operator: str | None = None) -> Any:
        """Settle a persisted conflict. Never resolves one implicitly."""
        from agentcore.memory.resolve import ResolutionOutcome, ResolutionStatus

        if self.memory_resolver is None:
            return ResolutionOutcome(
                status=ResolutionStatus.FAILED,
                reason="memory is not enabled for this agent")
        return self.memory_resolver.resolve_conflict(
            conflict_id, choice, operator=operator)

    def conversation(self, *, sessions: Any = None, tools: Any = None,
                     system_prompt: str | None = None) -> Any:
        """Build a :class:`ConversationRuntime` from this agent's components.

        The Agent is already the composition root — it owns the model, the
        memory store, capture and the resolver — so the runtime is assembled
        from what is here rather than constructing anything itself. Sessions
        persist to ``<workspace_dir>/conversations`` unless a store is
        injected.

        Memory participation follows ``features.memory`` exactly: with it off
        the agent holds no store, so the runtime simply converses without
        retrieval or capture.
        """
        from agentcore.conversation.runtime import (
            SYSTEM_PROMPT,
            ConversationRuntime,
        )
        from agentcore.conversation.session import build_session_store

        store = sessions
        if store is None:
            try:
                store = build_session_store(self.config)
            except OSError as exc:
                self.log.warning(Events.CONVERSATION_FAILED,
                                 stage="session_store",
                                 error=type(exc).__name__)
                store = None
        return ConversationRuntime(
            model=self.model, memory=self.memory,
            capture=self.memory_capture, resolver=self.memory_resolver,
            sessions=store, tools=tools if tools is not None
            else self._build_tool_runner(), state=self.state,
            config=self.config.conversation,
            system_prompt=system_prompt or SYSTEM_PROMPT,
            logger=get_logger("conversation.runtime"))

    def _build_tool_runner(self) -> Any | None:
        """Conversational tool use, when ``conversation.tools_enabled`` is on.

        Hands the runner this agent's *existing* executor, registry and
        policy — the same objects the autonomous runner uses — so there is one
        execution pipeline and one permission gate, not two.
        """
        if not self.config.conversation.tools_enabled:
            return None

        from agentcore.conversation.tool_use import ConversationToolRunner

        return ConversationToolRunner(
            self.executor, self.registry, policy=self.policy,
            max_result_chars=self.config.conversation.max_tool_result_chars,
            logger=get_logger("conversation.tools"))

    def correct_memory(self, turns: Any, *, memory_id: str | None = None) -> Any:
        """Act on a conversational correction ("forget that", "no longer true").

        Returns a ``ResolutionOutcome``. An ambiguous or unmatched target
        yields ``UNRESOLVED`` and changes nothing — never a best guess.
        """
        from agentcore.memory.resolve import (
            ResolutionOutcome,
            ResolutionStatus,
            parse_correction,
        )

        if self.memory_resolver is None:
            return ResolutionOutcome(
                status=ResolutionStatus.FAILED,
                reason="memory is not enabled for this agent")

        messages = [turns] if isinstance(turns, Message) else list(turns or [])
        for message in messages:
            if getattr(message, "role", None) is not Role.USER:
                continue
            request = parse_correction(getattr(message, "content", ""),
                                       run_id=self.state.run_id)
            if request is not None:
                return self.memory_resolver.resolve_correction(
                    request, memory_id=memory_id)
        return ResolutionOutcome(
            status=ResolutionStatus.NO_ACTION,
            reason="no correction was requested")

    def resolve_memory(self, decision: Any, *, conflict: Any = None) -> Any:
        """Apply a ``CaptureDecision`` — including a conflict resolution."""
        from agentcore.memory.resolve import ResolutionOutcome, ResolutionStatus

        if self.memory_resolver is None:
            return ResolutionOutcome(
                status=ResolutionStatus.FAILED,
                reason="memory is not enabled for this agent")
        return self.memory_resolver.apply(decision, conflict=conflict)

    def remember_from(self, turns: Any, *, persist: bool = True) -> Any:
        """Consider conversation turns for durable memory. Explicit by design.

        This is the integration seam for conversational capture. It is *not*
        wired into ``run_task``/``execute``: the project has no persistent
        conversation loop yet, and inventing one purely to force automatic
        capture would be exactly the "store everything" behaviour this layer
        exists to avoid.

        Returns a ``CaptureResult`` (empty when capture is unavailable), so a
        caller can always inspect what was decided and why.
        """
        if self.memory_capture is None:
            from agentcore.memory.capture import CaptureResult

            return CaptureResult()
        return self.memory_capture.capture(
            turns, run_id=self.state.run_id, persist=persist)

    def memory_info(self) -> dict[str, Any] | None:
        """Non-sensitive description of the memory backend, or None.

        Reports shape only — backend, location and record count — never any
        remembered content.
        """
        if self.memory is None:
            return None
        info: dict[str, Any] = {"backend": type(self.memory).__name__}
        directory = getattr(self.memory, "directory", None)
        if directory is not None:
            info["directory"] = str(directory)
        info["capture"] = self.memory_capture is not None
        counter = getattr(self.memory, "count", None)
        if callable(counter):
            try:
                info["records"] = counter()
            except Exception:  # pragma: no cover - never break status()
                info["records"] = None
        return info

    # -- tools -------------------------------------------------------------
    def register_tool(self, tool: Tool, *, replace: bool = False) -> bool:
        """Add a tool at runtime. This is the integration point for later
        adapters (e.g. a local Windows control client)."""
        return self.registry.register(tool, replace=replace)

    def available_tools(self) -> list[dict[str, Any]]:
        return self.registry.describe()

    def call_tool(self, name: str, args: dict[str, Any] | None = None
                  ) -> ToolInvocation:
        """Invoke a single tool outside of a task, recording it in state."""
        invocation = self.executor.invoke(name, args, run_id=self.state.run_id)
        self.state.record_tool_call(invocation)
        return invocation

    # -- tasks -------------------------------------------------------------
    def create_task(self, objective: str, *,
                    context: dict[str, Any] | None = None) -> Task:
        task = self.planner.plan(objective, context=context)
        task.validate()
        self.log.info(Events.TASK_CREATED, task_id=task.id, objective=objective,
                      subtasks=len(task.subtasks))
        return task

    def assign(self, task: Task) -> None:
        task.validate()
        self.state.set_task(task)

    def run_task(self, task: Task | None = None) -> RunReport:
        """Execute a task, replanning when recovery asks for a new approach.

        The loop is ``execute -> (recovery says replan?) -> replan -> execute``,
        bounded by ``config.recovery.max_replans``. Each iteration runs the
        existing runner, so verification, permissions and recovery all apply
        unchanged on every attempt.
        """
        if task is not None:
            self.assign(task)
        if self.state.current_task is None:
            raise ValueError("no task assigned; pass a task or call assign() first")

        max_replans = self.config.recovery.max_replans
        current = self.state.current_task
        if current.status.is_terminal:
            raise StateError("task has already finished; assign a new task",
                             task_id=current.id, status=current.status.value)

        report = self._execute_once()

        while report.replan_requested:
            ledger = ReplanLedger.load(self.state)
            if ledger.exhausted(max_replans):
                self.log.info(Events.REPLAN_EXHAUSTED,
                              replans=ledger.count, max_replans=max_replans)
                self.state.update_resume_point(
                    note="replan budget exhausted; awaiting human input")
                break

            outcome = self._replan(ledger, report)
            if outcome is ReplanOutcome.ABANDON:
                # Rejected or errored; the previous task and its history are
                # intact and the report still reflects the failure.
                break
            if outcome is ReplanOutcome.RETRY:
                # A duplicate of an already-failed strategy. The attempt is
                # spent, but the request stands: ask again while budget remains
                # (the guard at the top of the loop terminates it).
                continue

            report = self._execute_once(resumed=True)

        self._finalise_run(report)
        return report

    # -- loop internals ----------------------------------------------------
    def _execute_once(self, *, resumed: bool = False) -> RunReport:
        """Run the current task once through the existing runner."""
        current = self.state.current_task
        assert current is not None  # guaranteed by run_task
        self.state.transition_phase(AgentPhase.EXECUTING, note="run_task")
        if resumed:
            self.log.info(Events.REPLAN_EXECUTION_RESUMED, task_id=current.id)
        try:
            return self.runner.run(current, self.state)
        except Exception as exc:  # noqa: BLE001 - state must reflect the failure
            self.state.record_error(exc)
            self.state.transition_phase(AgentPhase.ERROR, note=str(exc))
            self.checkpoint()
            raise

    def _replan(self, ledger: ReplanLedger,
                report: RunReport) -> ReplanOutcome:
        """Ask the planner for a new approach and judge the result.

        Budget is consumed *before* the planner runs, so a planner that raises
        or misbehaves still counts as an attempt and cannot loop forever.

        Returns :class:`ReplanOutcome`: ``ADOPTED`` (execute the new plan),
        ``RETRY`` (duplicate strategy refused, ask again if budget remains) or
        ``ABANDON`` (unusable output; stop).
        """
        previous = self.state.current_task
        assert previous is not None
        reason = self._replan_reason(report)

        attempt = ledger.begin(reason, previous)
        ledger.save(self.state)

        # After a duplicate rejection the loop re-enters while already in
        # PLANNING, and PLANNING -> RECOVERING is not a legal transition. The
        # recovery step has already happened in that case, so only pass through
        # RECOVERING when coming from execution.
        if self.state.phase is not AgentPhase.PLANNING:
            self.state.transition_phase(AgentPhase.RECOVERING,
                                        note="replan requested")
        self.state.transition_phase(AgentPhase.PLANNING, note=reason)
        self.log.info(Events.REPLAN_STARTED, attempt=attempt,
                      max_replans=self.config.recovery.max_replans,
                      task_id=previous.id, reason=reason)

        try:
            new_task = self.planner.replan(previous, self.state)
        except Exception as exc:  # noqa: BLE001 - a bad planner must not crash the run
            self.state.record_error(exc)
            ledger.finish(reason=reason, outcome="error",
                          previous_task_id=previous.id,
                          detail=f"{type(exc).__name__}: {exc}")
            ledger.save(self.state)
            self.log.warning(Events.REPLAN_FAILED, attempt=attempt,
                             error=type(exc).__name__,
                             message=getattr(exc, "message", str(exc)))
            self._abandon_replan(report, "planner raised during replan")
            return ReplanOutcome.ABANDON

        rejection = self._reject_reason(new_task, previous)
        if rejection is not None:
            ledger.finish(reason=reason, outcome="rejected",
                          previous_task_id=previous.id, detail=rejection,
                          signature=self._proposal_signature(new_task))
            ledger.save(self.state)
            self.log.warning(Events.REPLAN_REJECTED, attempt=attempt,
                             detail=rejection)
            self._abandon_replan(report, f"replan rejected: {rejection}")
            return ReplanOutcome.ABANDON

        # Structurally sound, but is it the same failed strategy again? A
        # duplicate is refused *before* execution. Unlike a structural
        # rejection this leaves the request live, so the loop asks the planner
        # again while budget remains — the attempt is already paid for.
        duplicate = self._duplicate_reason(new_task, ledger)
        if duplicate is not None:
            ledger.finish(reason=reason, outcome="rejected_duplicate",
                          previous_task_id=previous.id, detail=duplicate,
                          signature=self._proposal_signature(new_task))
            ledger.save(self.state)
            self.log.warning(Events.REPLAN_REJECTED, attempt=attempt,
                             detail=duplicate, kind="duplicate")
            self.state.update_resume_point(
                note="replan rejected: duplicate of a failed approach")
            # Keep the previous task in place; nothing about it changed.
            return ReplanOutcome.RETRY

        # Accepted: carry the objective forward and swap in the new plan. The
        # previous task is already archived in the ledger, so no history is lost.
        ledger.finish(reason=reason, outcome="accepted",
                      previous_task_id=previous.id, new_task_id=new_task.id)
        ledger.save(self.state)

        self.state.set_task(new_task)
        report.replan_requested = False  # this request has now been serviced
        self.log.info(Events.REPLAN_PLAN_ACCEPTED, attempt=attempt,
                      previous_task_id=previous.id, new_task_id=new_task.id,
                      subtasks=len(new_task.subtasks))
        self.log.info(Events.REPLAN_SUCCEEDED, attempt=attempt)
        return ReplanOutcome.ADOPTED

    def _reject_reason(self, new_task: Task | None,
                       previous: Task,
                       ledger: ReplanLedger | None = None) -> str | None:
        """Validate a replanned task. Returns a reason string if unusable.

        The planner is expected to validate its own output; this is the
        orchestration-level backstop that also catches structural problems a
        planner cannot see — notably a planner that hands back the very task
        that just failed, which would loop forever.

        When ``ledger`` is supplied it additionally rejects a plan whose
        *executable signature* matches an archived plan that already failed for
        a non-transient reason: being told about past failures does not stop a
        model repeating them, so the duplicate is refused before it can run.
        """
        if new_task is None:
            return "planner returned no task"
        if not isinstance(new_task, Task):
            return f"planner returned {type(new_task).__name__}, not a Task"
        if new_task is previous or new_task.id == previous.id:
            return "planner returned the same task; no new approach proposed"
        if not new_task.subtasks:
            return "replanned task has no subtasks"
        if new_task.status.is_terminal:
            return f"replanned task is already '{new_task.status}'"
        try:
            new_task.validate()
        except AgentError as exc:
            return f"replanned task failed validation: {exc.message}"
        return None

    @staticmethod
    def _proposal_signature(new_task: Task | None) -> str:
        """Safe description of a proposed strategy: tool names only.

        Recorded on rejections so a later replan can be told *what* it
        proposed. Argument values are never included — see
        :func:`describe_signature`.
        """
        if not isinstance(new_task, Task):
            return ""
        try:
            return describe_signature(plan_signature(new_task))
        except Exception:  # pragma: no cover - never block a rejection
            return ""

    def _duplicate_reason(self, new_task: Task,
                          ledger: ReplanLedger) -> str | None:
        """Reject a plan that repeats an already-failed executable strategy."""
        match = find_duplicate(new_task, ledger.archived_plans)
        if match is None:
            return None
        self.log.warning(Events.REPLAN_DUPLICATE_REJECTED,
                         matched_task_id=match.task_id,
                         category=str(match.category),
                         occurrences=match.occurrences,
                         signature=describe_signature(match.signature))
        return match.reason()

    def _abandon_replan(self, report: RunReport, note: str) -> None:
        """Leave the run in an honest failed/blocked state after a bad replan."""
        report.replan_requested = False
        report.escalated = True
        self.state.update_resume_point(note=note)

    @staticmethod
    def _replan_reason(report: RunReport) -> str:
        for decision in reversed(report.decisions):
            if decision.get("action") == "replan":
                return str(decision.get("reason", "recovery requested a replan"))
        return "recovery requested a replan"

    def _finalise_run(self, report: RunReport) -> None:
        """Settle the agent phase once the loop has stopped."""
        if report.status is TaskStatus.COMPLETED:
            self.state.transition_phase(AgentPhase.DONE, note="task completed")
        elif report.status is TaskStatus.FAILED:
            self.state.transition_phase(AgentPhase.ERROR, note="task failed")
        else:
            self.state.update_resume_point(note="task blocked")
        self.checkpoint()

    def execute(self, objective: str, *,
                context: dict[str, Any] | None = None) -> RunReport:
        """Convenience: plan then run."""
        return self.run_task(self.create_task(objective, context=context))

    # -- state -------------------------------------------------------------
    def checkpoint(self) -> None:
        self.state_store.save(self.state)
        self.log.debug(Events.STATE_CHECKPOINT, **self.state.summary())

    def resume(self, run_id: str) -> AgentState:
        """Load a persisted run, refusing one that belongs elsewhere.

        A resumed autonomous task must not silently execute against a
        different project directory, so the run's pinned workspace is compared
        against this agent's canonical root *before* the state is adopted.
        Nothing runs on mismatch: ``self.state`` is left untouched, so no tool
        can execute.

        Legacy runs recorded before pinning carry ``workspace=None`` and are
        accepted unchanged.
        """
        loaded = self.state_store.load(run_id)
        pinned = getattr(loaded, "workspace", None)
        if not self.workspace.matches(pinned):
            self.log.warning(Events.STATE_RESUMED, run_id=run_id,
                             workspace_mismatch=True)
            raise WorkspaceMismatchError(
                "run belongs to a different workspace; refusing to resume",
                run_id=run_id, run_workspace=pinned,
                current_workspace=self.workspace.identity)
        self.state = loaded
        if self.state.workspace is None:
            # Adopt the current workspace so the next checkpoint is pinned.
            self.state.workspace = self.workspace.identity
        self.log.info(Events.STATE_RESUMED, **self.state.summary())
        return self.state

    def reset(self) -> AgentState:
        self.state = AgentState(workspace=self.workspace.identity)
        return self.state

    def status(self) -> dict[str, Any]:
        return {
            **self.state.summary(),
            "permission_level": str(self.policy.level),
            "tools": self.registry.names(),
            "model": self.model_info(),
            "memory": self.memory_info(),
        }

    def model_info(self) -> dict[str, Any] | None:
        """Non-sensitive description of the active provider, or None."""
        describe = getattr(self.model, "describe", None)
        if describe is None:
            return None
        return describe()

    def shutdown(self) -> None:
        self.checkpoint()
        self.log.info(Events.AGENT_STOPPED, **self.state.summary())


def build_agent(config: AgentConfig | None = None,
                tools: Iterable[Tool] | None = None,
                **deps: Any) -> Agent:
    """Small factory for the common case."""
    return Agent(config, AgentDependencies(
        tools=list(tools) if tools is not None else None, **deps))
