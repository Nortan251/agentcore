"""Agent orchestrator: wires configuration, tools, permissions, state and
execution together.

The agent owns *composition*, not behaviour. Planning, verification, recovery,
memory and model access are injected, so a later task can supply a real planner
or a local computer-control tool without editing this class.
"""

from __future__ import annotations

import inspect
import threading
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
from agentcore.core.runner import RunControl, RunReport, TaskRunner
from agentcore.errors import AgentError, StateError
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import WorkspaceMismatchError
from agentcore.observability.events import Events
from agentcore.observability.logging import configure_logging, get_logger
from agentcore.permissions.policy import ApprovalGate, PermissionPolicy
from agentcore.models.messages import Message, Role
from agentcore.realtime.cancellation import CancellationReason, CancellationSource
from agentcore.realtime.errors import CancellationRequested, TurnValidationError
from agentcore.realtime.turn import Deadline
from agentcore.state.models import AgentPhase, AgentState
from agentcore.state.store import InMemoryStateStore, StateStore
from agentcore.tasks.models import Task, TaskStatus
from agentcore.tools.base import Tool
from agentcore.tools.builtin import default_tools
from agentcore.tools.executor import ToolExecutor, ToolInvocation
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import Clock


def _accepts_control_kwarg(func: Any) -> bool:
    """Check if func accepts control kwarg explicitly (not via **kwargs)."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return False
    param = sig.parameters.get("control")
    if param is None:
        return False
    return param.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


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
    #: Realtime turn coordinator (Task 42.3). Strictly opt-in: nothing is ever
    #: constructed automatically, no ``JsonFileTurnStore`` is created, and with
    #: ``None`` the agent, conversation and CLI behave exactly as before. An
    #: injected durable store is host-owned; its ownership failures surface
    #: categorically rather than degrading to in-memory state.
    turn_coordinator: Any | None = None


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

        self.workspace = WorkspaceGuard(
            self.config.canonical_workspace(),
            config=self.config.filesystem,
        )

        self.executor = ToolExecutor(
            self.registry, self.policy,
            workspace=self.workspace,
            tool_settings=self.config.tools.settings,
        )

        self._injected_planner = deps.planner
        self._injected_verifier = deps.verifier
        self._injected_recovery = deps.recovery
        self.state_store: StateStore = deps.state_store or InMemoryStateStore()
        self.memory: MemoryStore | None = deps.memory or self._build_memory()
        self.model: ModelProvider | None = deps.model or self._build_model()
        self.planner: Planner = self._build_planner()
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
        self.turn_coordinator: Any | None = deps.turn_coordinator
        self._turns: Any | None = self._build_turn_bridge()
        self.state = AgentState(workspace=self.workspace.identity)

        # Task 42.4c: autonomous cancellation root and active-run marker.
        # One autonomous run per Agent is supported; concurrent runs are
        # rejected categorically rather than sharing singular AgentState.
        # Shutdown requests cancellation but does not concurrently persist
        # half-mutated state while a run is active.
        self._autonomy_root = CancellationSource()
        self._autonomy_lock = threading.RLock()
        self._active_run = False

        self.log.info(Events.AGENT_STARTED, run_id=self.state.run_id,
                      permission_level=str(self.policy.level),
                      tools=self.registry.names(),
                      planner=type(self.planner).__name__,
                      features=self.config.features.enabled_flags())

    # -- realtime turns ----------------------------------------------------
    def _build_turn_bridge(self) -> Any | None:
        if self.turn_coordinator is None:
            return None

        from agentcore.conversation.turns import TurnBridge

        bridge = TurnBridge(
            self.turn_coordinator,
            turn_deadline_seconds=(
                self.config.conversation.turn_deadline_seconds),
            logger=get_logger("conversation.turns"))
        self.log.info(Events.TURN_BRIDGE_CONFIGURED,
                      coordinator=type(self.turn_coordinator).__name__)
        return bridge

    def reconcile_turns(self) -> tuple:
        if self._turns is None:
            return ()
        return self._turns.reconcile()

    # -- recovery ----------------------------------------------------------
    def _build_recovery(self) -> RecoveryStrategy:
        if self._injected_recovery is not None:
            return self._injected_recovery
        if not self.config.features.recovery:
            return NoRetryStrategy()

        from agentcore.recovery.policy import RecoveryPolicy

        policy = RecoveryPolicy(self.config.recovery, registry=self.registry)
        self.log.info(Events.RECOVERY_CONFIGURED,
                      max_attempts=policy.config.max_attempts,
                      max_replans=policy.config.max_replans)
        return policy

    # -- verifier ----------------------------------------------------------
    def _build_verifier(self) -> Verifier:
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
                memory=self.memory,
                max_memories=self.config.memory.max_planning_memories,
                max_memory_chars=self.config.memory.max_planning_memory_chars)
        except PlanningError as exc:  # pragma: no cover
            self.log.warning(Events.PLANNER_UNAVAILABLE,
                             error=type(exc).__name__, message=exc.message)
            return PassthroughPlanner()
        self.log.info(Events.PLANNER_CONFIGURED, planner="LLMPlanner")
        return planner

    # -- model -------------------------------------------------------------
    def _build_model(self) -> ModelProvider | None:
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
        if not self.config.features.memory:
            return None

        from agentcore.memory.errors import MemoryError as MemoryLayerError
        from agentcore.memory.store import build_memory_store

        try:
            store = build_memory_store(self.config)
        except (MemoryLayerError, OSError) as exc:
            self.log.warning(Events.MEMORY_UNREADABLE,
                             error=type(exc).__name__)
            return None
        self.log.info(Events.MEMORY_CONFIGURED,
                      backend=type(store).__name__,
                      directory=str(getattr(store, "directory", "")),
                      max_records=self.config.memory.max_records)
        return store

    def _build_memory_capture(self) -> Any | None:
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
        if self.memory is None:
            return None

        from agentcore.memory.resolve import MemoryResolver

        return MemoryResolver(self.memory, conflicts=self.memory_conflicts,
                              logger=get_logger("memory.resolve"))

    def _build_conflict_store(self) -> Any | None:
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
        if self.memory_resolver is None:
            return []
        return self.memory_resolver.list_pending_conflicts(limit=limit)

    def resolve_memory_conflict(self, conflict_id: str, choice: Any, *,
                                operator: str | None = None) -> Any:
        from agentcore.memory.resolve import ResolutionOutcome, ResolutionStatus

        if self.memory_resolver is None:
            return ResolutionOutcome(
                status=ResolutionStatus.FAILED,
                reason="memory is not enabled for this agent")
        return self.memory_resolver.resolve_conflict(
            conflict_id, choice, operator=operator)

    def conversation(self, *, sessions: Any = None, tools: Any = None,
                     system_prompt: str | None = None) -> Any:
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
            turns=self._turns,
            config=self.config.conversation,
            system_prompt=system_prompt or SYSTEM_PROMPT,
            logger=get_logger("conversation.runtime"))

    def _build_tool_runner(self) -> Any | None:
        if not self.config.conversation.tools_enabled:
            return None

        from agentcore.conversation.tool_use import ConversationToolRunner

        return ConversationToolRunner(
            self.executor, self.registry, policy=self.policy,
            max_result_chars=self.config.conversation.max_tool_result_chars,
            logger=get_logger("conversation.tools"))

    def correct_memory(self, turns: Any, *, memory_id: str | None = None) -> Any:
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
        from agentcore.memory.resolve import ResolutionOutcome, ResolutionStatus

        if self.memory_resolver is None:
            return ResolutionOutcome(
                status=ResolutionStatus.FAILED,
                reason="memory is not enabled for this agent")
        return self.memory_resolver.apply(decision, conflict=conflict)

    def remember_from(self, turns: Any, *, persist: bool = True) -> Any:
        if self.memory_capture is None:
            from agentcore.memory.capture import CaptureResult

            return CaptureResult()
        return self.memory_capture.capture(
            turns, run_id=self.state.run_id, persist=persist)

    def memory_info(self) -> dict[str, Any] | None:
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
            except Exception:  # pragma: no cover
                info["records"] = None
        return info

    # -- tools -------------------------------------------------------------
    def register_tool(self, tool: Tool, *, replace: bool = False) -> bool:
        return self.registry.register(tool, replace=replace)

    def available_tools(self) -> list[dict[str, Any]]:
        return self.registry.describe()

    def call_tool(self, name: str, args: dict[str, Any] | None = None
                  ) -> ToolInvocation:
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

    def run_task(self, task: Task | None = None, *,
                 deadline: Deadline | None = None,
                 clock: Clock | None = None) -> RunReport:
        """Execute a task, replanning when recovery asks for a new approach.

        Task 42.4c: adds optional trusted deadline/clock and cooperative
        cancellation via autonomous root. One run per Agent is supported;
        concurrent runs are rejected categorically.

        Deadline requires clock, validated by RunControl. No deadline config
        is introduced; host/test injection only.
        """
        # Validate deadline requires clock before acquiring active-run marker
        if deadline is not None and clock is None:
            raise TurnValidationError("deadline requires a clock")
        if deadline is not None and not isinstance(deadline, Deadline):
            raise TurnValidationError("deadline is invalid")

        # One autonomous run per Agent
        with self._autonomy_lock:
            if self._active_run:
                raise StateError("autonomous run already active",
                                 run_id=self.state.run_id)
            self._active_run = True

        child = self._autonomy_root.child()
        try:
            control = RunControl(cancellation=child.token,
                                 deadline=deadline,
                                 clock=clock)

            if task is not None:
                self.assign(task)
            if self.state.current_task is None:
                raise ValueError("no task assigned; pass a task or call assign() first")

            max_replans = self.config.recovery.max_replans
            current = self.state.current_task
            if current.status.is_terminal:
                raise StateError("task has already finished; assign a new task",
                                 task_id=current.id, status=current.status.value)

            # Checkpoint before first execution (A)
            control.checkpoint()

            report = self._execute_once(control=control)

            while report.replan_requested:
                # Checkpoint before entering each replan iteration
                control.checkpoint()

                ledger = ReplanLedger.load(self.state)
                if ledger.exhausted(max_replans):
                    self.log.info(Events.REPLAN_EXHAUSTED,
                                  replans=ledger.count, max_replans=max_replans)
                    self.state.update_resume_point(
                        note="replan budget exhausted; awaiting human input")
                    break

                # Checkpoint before planner.replan
                control.checkpoint()
                outcome = self._replan(ledger, report, control=control)
                if outcome is ReplanOutcome.ABANDON:
                    break
                if outcome is ReplanOutcome.RETRY:
                    continue

                # Before executing accepted replacement plan
                control.checkpoint()
                report = self._execute_once(resumed=True, control=control)

            self._finalise_run(report)
            return report

        except CancellationRequested as exc:
            # Cancellation settlement: task CANCELLED, not generic failure
            current = self.state.current_task
            if current is not None and not current.status.is_terminal:
                try:
                    current.transition_to(TaskStatus.CANCELLED)
                except Exception:
                    pass
            # If report not yet defined (cancel before first execute), create one
            if 'report' not in locals():
                tid = current.id if current is not None else "unknown"
                report = RunReport(task_id=tid, status=TaskStatus.CANCELLED)
            report.status = TaskStatus.CANCELLED
            reason = exc.reason if isinstance(exc.reason, CancellationReason) else CancellationReason.INTERNAL_FAILURE
            report.cancellation_reason = reason.value
            report.replan_requested = False
            self.state.set_active_subtask(None)
            self._finalise_run(report)
            return report

        finally:
            child.close()
            with self._autonomy_lock:
                self._active_run = False

    # -- loop internals ----------------------------------------------------
    def _execute_once(self, *, resumed: bool = False,
                      control: RunControl | None = None) -> RunReport:
        """Run the current task once through the existing runner.

        Task 42.4c: accepts RunControl, handles CancellationRequested as
        control outcome, not generic ERROR.
        """
        control = control or RunControl()
        current = self.state.current_task
        assert current is not None
        # Checkpoint before entering execution
        control.checkpoint()
        self.state.transition_phase(AgentPhase.EXECUTING, note="run_task")
        if resumed:
            self.log.info(Events.REPLAN_EXECUTION_RESUMED, task_id=current.id)
        try:
            if _accepts_control_kwarg(self.runner.run):
                return self.runner.run(current, self.state, control=control)
            return self.runner.run(current, self.state)
        except CancellationRequested as exc:
            # Expected cancellation, not generic failure
            reason = exc.reason if isinstance(exc.reason, CancellationReason) else CancellationReason.INTERNAL_FAILURE
            report = RunReport(task_id=current.id, status=TaskStatus.CANCELLED,
                               cancellation_reason=reason.value)
            if not current.status.is_terminal:
                try:
                    current.transition_to(TaskStatus.CANCELLED)
                except Exception:
                    pass
            self.state.set_active_subtask(None)
            return report
        except Exception as exc:  # noqa: BLE001
            self.state.record_error(exc)
            self.state.transition_phase(AgentPhase.ERROR, note=str(exc))
            self.checkpoint()
            raise

    def _replan(self, ledger: ReplanLedger,
                report: RunReport,
                *, control: RunControl | None = None) -> ReplanOutcome:
        """Ask the planner for a new approach and judge the result.

        Task 42.4c: checkpoints before/after planner.replan and before adopting.
        If planner returns after cancellation, reject new task.
        """
        control = control or RunControl()
        previous = self.state.current_task
        assert previous is not None
        reason = self._replan_reason(report)

        attempt = ledger.begin(reason, previous)
        ledger.save(self.state)

        if self.state.phase is not AgentPhase.PLANNING:
            self.state.transition_phase(AgentPhase.RECOVERING,
                                        note="replan requested")
        self.state.transition_phase(AgentPhase.PLANNING, note=reason)
        self.log.info(Events.REPLAN_STARTED, attempt=attempt,
                      max_replans=self.config.recovery.max_replans,
                      task_id=previous.id, reason=reason)

        # Checkpoint before planner.replan
        control.checkpoint()

        try:
            new_task = self.planner.replan(previous, self.state)
        except CancellationRequested as exc:
            # Cancellation during planner.replan: reject returned plan, cancel current
            self.state.record_error(exc)
            ledger.finish(reason=reason, outcome="cancelled",
                          previous_task_id=previous.id,
                          detail=f"cancelled: {exc.reason.value if isinstance(exc.reason, CancellationReason) else 'unknown'}")
            ledger.save(self.state)
            # Cancel current task
            if not previous.status.is_terminal:
                try:
                    previous.transition_to(TaskStatus.CANCELLED)
                except Exception:
                    pass
            raise exc
        except Exception as exc:  # noqa: BLE001
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

        # Checkpoint after planner returns, before adopting
        try:
            control.checkpoint()
        except CancellationRequested as exc:
            # Late plan rejected
            ledger.finish(reason=reason, outcome="cancelled",
                          previous_task_id=previous.id,
                          detail="late plan after cancellation")
            ledger.save(self.state)
            if not previous.status.is_terminal:
                try:
                    previous.transition_to(TaskStatus.CANCELLED)
                except Exception:
                    pass
            raise exc

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
            return ReplanOutcome.RETRY

        # Accepted: checkpoint before adopting
        control.checkpoint()

        ledger.finish(reason=reason, outcome="accepted",
                      previous_task_id=previous.id, new_task_id=new_task.id)
        ledger.save(self.state)

        self.state.set_task(new_task)
        report.replan_requested = False
        self.log.info(Events.REPLAN_PLAN_ACCEPTED, attempt=attempt,
                      previous_task_id=previous.id, new_task_id=new_task.id,
                      subtasks=len(new_task.subtasks))
        self.log.info(Events.REPLAN_SUCCEEDED, attempt=attempt)
        return ReplanOutcome.ADOPTED

    def _reject_reason(self, new_task: Task | None,
                       previous: Task,
                       ledger: ReplanLedger | None = None) -> str | None:
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
        if not isinstance(new_task, Task):
            return ""
        try:
            return describe_signature(plan_signature(new_task))
        except Exception:  # pragma: no cover
            return ""

    def _duplicate_reason(self, new_task: Task,
                          ledger: ReplanLedger) -> str | None:
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
        """Settle the agent phase once the loop has stopped.

        Task 42.4c: CANCELLED settles to DONE with truthful categorical note,
        using existing phases only. Handles intermediate PLANNING/ERROR phases
        via IDLE stepping to avoid illegal transitions.
        """
        def _to_done(note: str) -> None:
            try:
                self.state.transition_phase(AgentPhase.DONE, note=note)
                return
            except StateError:
                pass
            # PLANNING -> IDLE -> DONE is valid; ERROR -> IDLE -> DONE etc.
            try:
                if self.state.phase is not AgentPhase.IDLE:
                    self.state.transition_phase(AgentPhase.IDLE, note=f"reset before {note}")
            except Exception:
                pass
            try:
                self.state.transition_phase(AgentPhase.DONE, note=note)
            except Exception:
                # Best-effort: if still illegal (e.g., already DONE), just record note
                self.state.update_resume_point(note=note)

        if report.status is TaskStatus.COMPLETED:
            _to_done("task completed")
        elif report.status is TaskStatus.FAILED:
            try:
                self.state.transition_phase(AgentPhase.ERROR, note="task failed")
            except StateError:
                _to_done("task failed")
        elif report.status is TaskStatus.CANCELLED:
            reason = report.cancellation_reason or "cancelled"
            _to_done(f"task cancelled: {reason}")
        else:
            self.state.update_resume_point(note="task blocked")
        self.checkpoint()

    def execute(self, objective: str, *,
                context: dict[str, Any] | None = None,
                deadline: Deadline | None = None,
                clock: Clock | None = None) -> RunReport:
        """Convenience: plan then run, with optional deadline."""
        task = self.create_task(objective, context=context)
        return self.run_task(task, deadline=deadline, clock=clock)

    # -- state -------------------------------------------------------------
    def checkpoint(self) -> None:
        self.state_store.save(self.state)
        self.log.debug(Events.STATE_CHECKPOINT, **self.state.summary())

    def resume(self, run_id: str) -> AgentState:
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
        describe = getattr(self.model, "describe", None)
        if describe is None:
            return None
        return describe()

    def shutdown(self) -> None:
        # Task 42.4c: cancel autonomous root with AGENT_SHUTDOWN
        try:
            self._autonomy_root.cancel(CancellationReason.AGENT_SHUTDOWN)
        except Exception:
            pass
        if self._turns is not None:
            self._turns.shutdown()
        # Checkpoint safety: do not concurrently persist half-mutated state
        # while autonomous run is active; active run will terminalize and
        # checkpoint after observing cancellation.
        should_checkpoint = False
        with self._autonomy_lock:
            if not self._active_run:
                should_checkpoint = True
        if should_checkpoint:
            self.checkpoint()
        self.log.info(Events.AGENT_STOPPED, **self.state.summary())


def build_agent(config: AgentConfig | None = None,
                tools: Iterable[Tool] | None = None,
                **deps: Any) -> Agent:
    return Agent(config, AgentDependencies(
        tools=list(tools) if tools is not None else None, **deps))
