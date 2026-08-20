"""Task 42.3: opt-in conversational turn-lifecycle integration.

Every case enters through the public ``Agent`` composition root and keeps the
real registry, ``ToolExecutor``, argument validation, permission policy,
filesystem guard, session store and realtime turn store. Only the model
provider is scripted.

Concurrency is synchronised with events and condition variables, never with
sleeps: each waiter blocks on a specific state change and fails loudly on
timeout.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentcore.config.schema import (
    AgentConfig,
    ConversationConfig,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
    ToolConfig,
)
from agentcore.conversation.turns import (
    SUPERSEDED_TURN_ERROR,
    StageLease,
    TurnBridge,
)
from agentcore.core.agent import Agent, AgentDependencies, build_agent
from agentcore.errors import AgentError
from agentcore.models.messages import ModelResponse, Role, ToolCall
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.observability.logging import configure_logging
from agentcore.realtime import (
    CancellationReason,
    Deadline,
    InMemoryTurnStore,
    JsonFileTurnStore,
    TurnCoordinator,
    TurnPhase,
    TurnStoreOwnershipError,
    TurnValidationError,
    derive_session_key,
    new_operation_id,
)
from agentcore.realtime.turn import TurnController
from agentcore.tools.base import Tool, ToolResult, tool_spec
from agentcore.tools.builtin import default_tools
from agentcore.tools.builtin.inspect import InspectTool
from agentcore.util.clock import FixedClock

#: Bound for every synchronisation wait. Reaching it is a test failure, not a
#: retry: the condition it guards is deterministic.
WAIT = 30.0

RAW_SESSION_IDS = [
    "conv_0123456789ab",
    "my session with spaces",
    "проект-café-\U0001f9e0",
    "project#1?x=2",
    ".",
    "..",
    "../../etc/passwd",
    "s" * 400,
]

#: Ids distinctive enough that a substring search over a serialised record is
#: meaningful. "." and ".." occur naturally in ISO timestamps, so they are
#: covered by the exact-field assertions instead.
DISTINCTIVE_RAW_IDS = [raw for raw in RAW_SESSION_IDS if len(raw) > 2]


# ---------------------------------------------------------------------------
# scripted collaborators
# ---------------------------------------------------------------------------
class ScriptedModel:
    """Replay responses, record requests, and optionally gate a call."""

    def __init__(self, *steps: ModelResponse) -> None:
        self.steps = list(steps)
        self.requests: list = []
        self.calls = 0
        self.overlaps = 0
        self._active = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        #: call index (1-based) -> release event; the call blocks until set.
        self.gates: dict[int, threading.Event] = {}
        self.entered: dict[int, threading.Event] = {}
        #: call index -> exception instance raised instead of responding.
        self.raises: dict[int, BaseException] = {}

    def gate(self, index: int) -> threading.Event:
        self.gates[index] = threading.Event()
        self.entered[index] = threading.Event()
        return self.gates[index]

    def generate(self, request):
        with self._condition:
            self.calls += 1
            index = self.calls
            self.requests.append(request)
            self._active += 1
            if self._active > 1:
                self.overlaps += 1
            self._condition.notify_all()
        try:
            if index in self.entered:
                self.entered[index].set()
            if index in self.gates:
                assert self.gates[index].wait(WAIT), "model gate never released"
            if index in self.raises:
                raise self.raises[index]
            if not self.steps:
                raise AssertionError("ScriptedModel exhausted")
            return self.steps.pop(0)
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def wait_entered(self, index: int) -> None:
        assert self.entered[index].wait(WAIT), f"model call {index} never started"


class RecordingCoordinator:
    """Delegates to the real coordinator and signals completed admissions.

    Only ``admit`` and ``store`` are used by the bridge, so this observes the
    real seam without replacing any admission logic.
    """

    def __init__(self, inner: TurnCoordinator) -> None:
        self.inner = inner
        self._condition = threading.Condition()
        self.admissions: list = []

    @property
    def store(self):
        return self.inner.store

    def admit(self, session_id, **kwargs):
        admission = self.inner.admit(session_id, **kwargs)
        with self._condition:
            self.admissions.append(admission)
            self._condition.notify_all()
        return admission

    def wait_for(self, count: int):
        with self._condition:
            ready = self._condition.wait_for(
                lambda: len(self.admissions) >= count, WAIT)
        assert ready, f"only {len(self.admissions)} admissions, wanted {count}"
        return self.admissions[count - 1]


class GateTool(Tool):
    """Read-only tool that blocks inside ``run`` so a tool boundary is stable."""

    def __init__(self) -> None:
        self.runs = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec("gate", "Blocks until released.", parameters=[],
                         capabilities=[Capability.FS_READ], read_only=True)

    def run(self, args, context):
        with self._lock:
            self.runs += 1
        self.entered.set()
        assert self.release.wait(WAIT), "gate tool never released"
        return ToolResult.success("gated")


class RecordingInspectTool(InspectTool):
    """Observe calls while delegating every decision to the real tool."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, args, context):
        self.calls.append(dict(args))
        return super().run(args, context)


class ExplodingStore:
    """A durable store whose ownership has been lost mid-flight."""

    def __init__(self, inner) -> None:
        self._inner = inner

    @property
    def admission_coordination(self):
        return self._inner.admission_coordination

    def create(self, record):
        raise TurnStoreOwnershipError(
            "turn store instance belongs to another process")

    def load(self, turn_id):
        raise TurnStoreOwnershipError(
            "turn store instance belongs to another process")

    def compare_and_swap(self, turn_id, expected_version, next_record):
        raise TurnStoreOwnershipError(
            "turn store instance belongs to another process")

    def list_turns(self):
        raise TurnStoreOwnershipError(
            "turn store instance belongs to another process")

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


@contextmanager
def capture_events():
    """Capture structured agentcore logs.

    Entered *after* the agent is built: ``Agent.__init__`` calls
    ``configure_logging``, which replaces every handler on the namespace.
    """
    root = logging.getLogger("agentcore")
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    for existing in root.handlers:
        handler.setFormatter(existing.formatter)
    previous = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
        configure_logging(LoggingConfig(level="CRITICAL"))


# ---------------------------------------------------------------------------
# composition helpers
# ---------------------------------------------------------------------------
def _config(tmp_path: Path, *, tools_enabled: bool = False,
            level: PermissionLevel = PermissionLevel.AUTONOMOUS,
            overrides: dict | None = None,
            blocked: tuple = (), log_level: str = "CRITICAL") -> AgentConfig:
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path / "workspace"),
        model=ModelConfig(),
        tools=ToolConfig(),
        permissions=PermissionConfig(level=level,
                                     capability_overrides=overrides or {},
                                     blocked_capabilities=blocked),
        logging=LoggingConfig(level=log_level),
        conversation=ConversationConfig(
            directory=str(tmp_path / "conversations"),
            tools_enabled=tools_enabled),
    ).validate()


def _agent(config: AgentConfig, model, *, coordinator=None, tools=None
           ) -> Agent:
    return Agent(config, AgentDependencies(model=model, tools=tools,
                                           turn_coordinator=coordinator))


def _opted_in(tmp_path: Path, model, *, tools_enabled: bool = False,
              tools=None, level: PermissionLevel = PermissionLevel.AUTONOMOUS,
              overrides: dict | None = None, blocked: tuple = (), store=None,
              log_level: str = "CRITICAL"):
    store = store if store is not None else InMemoryTurnStore()
    coordinator = RecordingCoordinator(TurnCoordinator(store))
    agent = _agent(_config(tmp_path, tools_enabled=tools_enabled, level=level,
                           overrides=overrides, blocked=blocked,
                           log_level=log_level),
                   model, coordinator=coordinator, tools=tools)
    return agent, agent.conversation(), store, coordinator


def _reply(text: str, *, tool_calls=()) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=list(tool_calls))


def _records(store) -> list:
    return [store.load(turn_id) for turn_id in store.list_turns()]


def _texts(session) -> list[str]:
    return [m.content for m in session.messages]


# ===========================================================================
# 1. non-opt-in regression
# ===========================================================================
def test_without_a_coordinator_nothing_realtime_is_constructed(tmp_path):
    agent = _agent(_config(tmp_path), ScriptedModel(_reply("hi")))
    assert agent.turn_coordinator is None
    assert agent._turns is None
    assert agent.conversation().turns is None
    assert agent.reconcile_turns() == ()


def test_legacy_path_behaviour_is_unchanged(tmp_path):
    model = ScriptedModel(_reply("legacy reply"))
    agent = _agent(_config(tmp_path), model)
    runtime = agent.conversation()
    session = runtime.start()

    result = runtime.send("hello", session=session)

    assert result.ok
    assert result.reply.content == "legacy reply"
    assert result.turn_id is None
    assert result.turn_generation is None
    assert result.turn_phase is None
    assert "turn_id" not in result.describe()
    assert _texts(session) == ["hello", "legacy reply"]
    assert runtime.sessions.load(session.id).messages[-1].content == \
        "legacy reply"


_IMPORT_PROBE = """
import sys
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.config.schema import AgentConfig, LoggingConfig
config = AgentConfig(environment="test", workspace_dir=sys.argv[1],
                     logging=LoggingConfig(level="CRITICAL")).validate()
agent = Agent(config, AgentDependencies())
agent.conversation()
agent.shutdown()
realtime = [name for name in sys.modules if name.startswith("agentcore.realtime")]
print("REALTIME_MODULES=" + ",".join(sorted(realtime)))
"""


def test_the_legacy_path_never_imports_the_realtime_package(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(
        Path(__file__).resolve().parent.parent / "src")
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, str(tmp_path / "workspace")],
        capture_output=True, text=True, timeout=WAIT, env=environment,
        check=False)
    assert completed.returncode == 0, completed.stderr
    assert "REALTIME_MODULES=" in completed.stdout
    assert completed.stdout.strip().endswith("REALTIME_MODULES=")


def test_no_turn_store_directory_or_lock_is_ever_created(tmp_path):
    agent = _agent(_config(tmp_path), ScriptedModel(_reply("hi")))
    agent.conversation().send("hello")
    agent.shutdown()

    locks = list(tmp_path.rglob(".agentcore-turn-store.lock"))
    turn_files = list(tmp_path.rglob("turn_*.json"))
    assert locks == []
    assert turn_files == []


# ===========================================================================
# 2. opt-in happy path
# ===========================================================================
def test_opted_in_turn_completes_and_preserves_conversation_behaviour(tmp_path):
    model = ScriptedModel(_reply("opted-in reply"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()

    result = runtime.send("hello", session=session)

    assert result.ok
    assert result.reply.content == "opted-in reply"
    assert _texts(session) == ["hello", "opted-in reply"]
    assert runtime.sessions.load(session.id).messages[-1].content == \
        "opted-in reply"

    records = _records(store)
    assert len(records) == 1
    record = records[0]
    assert record.phase is TurnPhase.COMPLETED
    assert record.final_terminal is TurnPhase.COMPLETED
    assert record.cancellation_reason is None
    assert record.generation == 1
    assert result.turn_id == record.turn_id
    assert result.turn_generation == 1
    assert result.turn_phase == "completed"
    assert result.describe()["turn_phase"] == "completed"


def test_turn_phase_ledger_follows_the_approved_state_machine(tmp_path):
    model = ScriptedModel(_reply("done"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    runtime.send("hello")

    record = _records(store)[0]
    assert [t.to_phase for t in record.transitions] == [
        TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.FINALIZING,
        TurnPhase.COMPLETED,
    ]
    assert record.deadline_at is None


def test_successive_turns_increment_the_session_generation(tmp_path):
    model = ScriptedModel(_reply("one"), _reply("two"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()

    first = runtime.send("a", session=session)
    second = runtime.send("b", session=session)

    assert (first.turn_generation, second.turn_generation) == (1, 2)
    assert {r.phase for r in _records(store)} == {TurnPhase.COMPLETED}


# ===========================================================================
# 3. opaque session correlation
# ===========================================================================
@pytest.mark.parametrize("raw", RAW_SESSION_IDS)
def test_raw_session_ids_map_to_a_safe_opaque_key(tmp_path, raw):
    model = ScriptedModel(_reply("ok"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()
    session.id = raw

    result = runtime.send("hello", session=session)

    assert result.ok
    record = _records(store)[0]
    expected = derive_session_key(raw)
    assert record.session_id == expected
    assert expected.startswith("session_")
    assert len(expected) == len("session_") + 32


@pytest.mark.parametrize("raw", DISTINCTIVE_RAW_IDS)
def test_raw_session_id_never_reaches_durable_turn_state(tmp_path, raw):
    model = ScriptedModel(_reply("ok"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()
    session.id = raw
    runtime.send("hello", session=session)

    record = _records(store)[0]
    serialised = json.dumps(record.to_dict(), ensure_ascii=False)
    assert raw not in serialised
    assert record.session_id == derive_session_key(raw)
    assert derive_session_key(raw) in serialised


@pytest.mark.parametrize("raw", RAW_SESSION_IDS)
def test_durable_session_field_is_only_ever_the_derived_key(tmp_path, raw):
    model = ScriptedModel(_reply("ok"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()
    session.id = raw
    runtime.send("hello", session=session)

    record = _records(store)[0]
    assert record.session_id == derive_session_key(raw)
    assert record.session_id != raw
    assert record.run_id is None and record.task_id is None


def test_session_key_is_deterministic_collision_free_and_one_way():
    keys = {raw: derive_session_key(raw) for raw in RAW_SESSION_IDS}
    assert len(set(keys.values())) == len(RAW_SESSION_IDS)
    for raw, key in keys.items():
        assert derive_session_key(raw) == key
        assert raw not in key
    # Byte-exact input: visually equal but differently encoded ids differ.
    assert derive_session_key("cafe\u0301") != derive_session_key("caf\u00e9")


@pytest.mark.parametrize("raw", ["", None, 0])
def test_empty_or_non_string_session_id_is_a_categorical_caller_error(raw):
    with pytest.raises(TurnValidationError) as excinfo:
        derive_session_key(raw)
    assert "session id is required" in str(excinfo.value)
    assert isinstance(excinfo.value, AgentError)


def test_empty_session_id_fails_the_turn_categorically(tmp_path):
    model = ScriptedModel(_reply("ok"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()
    session.id = ""

    with pytest.raises(TurnValidationError):
        runtime.send("hello", session=session)
    assert store.list_turns() == []
    assert model.calls == 0


def test_raw_session_id_is_absent_from_realtime_errors(tmp_path):
    raw = "secret session name"
    with pytest.raises(TurnValidationError) as excinfo:
        derive_session_key("")
    assert raw not in str(excinfo.value)
    assert raw not in json.dumps(excinfo.value.details, default=str)


def test_conversation_layer_still_accepts_raw_ids_unchanged(tmp_path):
    model = ScriptedModel(*[_reply("ok") for _ in RAW_SESSION_IDS])
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    for raw in RAW_SESSION_IDS:
        session = runtime.start()
        session.id = raw
        assert runtime.send("hi", session=session).ok
        # Raw identity is untouched at the conversation layer.
        assert session.id == raw


# ===========================================================================
# 4. supersession while the predecessor is physically running
# ===========================================================================
def test_supersession_queues_the_successor_and_cancels_the_predecessor(tmp_path):
    model = ScriptedModel(_reply("first reply"), _reply("second reply"))
    release_first = model.gate(1)
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    session = runtime.start()

    results: dict[str, object] = {}

    def send(name, text):
        results[name] = runtime.send(text, session=session)

    first = threading.Thread(target=send, args=("first", "one"), daemon=True)
    first.start()
    model.wait_entered(1)                       # turn 1 holds the stage lease

    second = threading.Thread(target=send, args=("second", "two"), daemon=True)
    second.start()
    admission = coordinator.wait_for(2)         # turn 2 admitted, not started

    # The successor is durably queued while the predecessor is still inside
    # its synchronous provider call.
    assert admission.admitted.phase is TurnPhase.QUEUED
    assert admission.superseded_active is not None
    predecessor = store.load(coordinator.admissions[0].admitted.turn_id)
    assert predecessor.phase is TurnPhase.CANCELLING
    assert predecessor.cancellation_reason is CancellationReason.SUPERSEDED
    assert model.calls == 1                     # successor has not started

    release_first.set()
    first.join(WAIT)
    second.join(WAIT)
    assert not first.is_alive() and not second.is_alive()

    stale = store.load(coordinator.admissions[0].admitted.turn_id)
    fresh = store.load(admission.admitted.turn_id)

    assert stale.phase is TurnPhase.CANCELLED
    assert stale.final_terminal is TurnPhase.CANCELLED
    assert stale.cancellation_reason is CancellationReason.SUPERSEDED
    assert fresh.phase is TurnPhase.COMPLETED
    assert fresh.generation == stale.generation + 1

    assert not results["first"].ok
    assert results["first"].error == SUPERSEDED_TURN_ERROR
    assert results["first"].reply is None
    assert results["first"].turn_phase == "cancelled"
    assert results["second"].ok
    assert results["second"].reply.content == "second reply"

    # The stale turn persisted no assistant output, in memory or on disk.
    assert "first reply" not in _texts(session)
    assert _texts(session) == ["one", "two", "second reply"]
    assert "first reply" not in [
        m.content for m in runtime.sessions.load(session.id).messages]


def test_only_one_successor_may_queue_per_session(tmp_path):
    model = ScriptedModel(_reply("a"), _reply("b"), _reply("c"))
    release = model.gate(1)
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    session = runtime.start()
    results: dict[str, object] = {}

    def send(name, text):
        results[name] = runtime.send(text, session=session)

    threads = [threading.Thread(target=send, args=("one", "1"), daemon=True)]
    threads[0].start()
    model.wait_entered(1)
    for name in ("two", "three"):
        thread = threading.Thread(target=send, args=(name, name), daemon=True)
        threads.append(thread)
        thread.start()
        coordinator.wait_for(len(threads))

    release.set()
    for thread in threads:
        thread.join(WAIT)
        assert not thread.is_alive()

    phases = sorted(r.phase.value for r in _records(store))
    assert phases == ["cancelled", "cancelled", "completed"]
    # Every accepted input keeps its own durable terminal record.
    assert len(_records(store)) == 3
    assert sum(1 for r in results.values() if r.ok) == 1


# ===========================================================================
# 5. cross-session admission vs per-Agent execution serialization
# ===========================================================================
def test_sessions_admit_independently_but_execution_is_serialized(tmp_path):
    model = ScriptedModel(_reply("alpha"), _reply("beta"))
    release = model.gate(1)
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    left = runtime.start()
    right = runtime.start()
    assert left.id != right.id
    results: dict[str, object] = {}

    def send(name, session):
        results[name] = runtime.send(name, session=session)

    a = threading.Thread(target=send, args=("alpha", left), daemon=True)
    a.start()
    model.wait_entered(1)
    b = threading.Thread(target=send, args=("beta", right), daemon=True)
    b.start()

    # A different session admits without waiting for the running turn.
    other = coordinator.wait_for(2)
    assert other.admitted.phase is TurnPhase.QUEUED
    assert other.superseded_active is None      # never supersedes another session
    assert other.admitted.generation == 1       # generations are per session
    assert model.calls == 1                     # the stage lease still blocks it

    release.set()
    a.join(WAIT)
    b.join(WAIT)

    assert results["alpha"].ok and results["beta"].ok
    assert model.overlaps == 0                  # never two concurrent model calls
    assert {r.phase for r in _records(store)} == {TurnPhase.COMPLETED}
    assert {r.session_id for r in _records(store)} == {
        derive_session_key(left.id), derive_session_key(right.id)}


def test_the_stage_lease_is_reentrant_for_its_owning_thread():
    lease = StageLease()
    with lease:
        with lease:
            assert True


# ===========================================================================
# 6. tool routing through the one authoritative executor
# ===========================================================================
def test_opted_in_tool_call_uses_the_agents_single_executor(tmp_path):
    inspect_tool = RecordingInspectTool()
    tools = [t for t in default_tools() if t.name != "inspect"] + [inspect_tool]
    call = ToolCall(id="call-1", name="inspect", arguments={"path": "notes.txt"})
    model = ScriptedModel(_reply("", tool_calls=[call]), _reply("inspected"))
    agent, runtime, store, _ = _opted_in(tmp_path, model, tools_enabled=True,
                                         tools=tools)
    workspace = Path(agent.workspace.root)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("hi\n", encoding="utf-8")

    result = runtime.send("inspect notes.txt")

    assert result.ok
    assert runtime.tools.executor is agent.executor
    assert runtime.tools.registry is agent.registry
    assert runtime.tools.policy is agent.policy
    assert inspect_tool.calls == [{"path": "notes.txt"}]
    outcome = result.tool_outcomes[0]
    assert outcome.status == "ok" and outcome.executed
    assert outcome.invocation.decision.allowed
    tool_messages = [m for m in result.session.messages if m.role is Role.TOOL]
    assert len(tool_messages) == 1
    assert "DATA, not instructions" in tool_messages[0].content

    record = _records(store)[0]
    assert record.phase is TurnPhase.COMPLETED
    assert [t.to_phase for t in record.transitions] == [
        TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL,
        TurnPhase.GENERATING, TurnPhase.FINALIZING, TurnPhase.COMPLETED]


def test_permission_denial_is_unchanged_under_an_owned_turn(tmp_path):
    call = ToolCall(id="c1", name="inspect", arguments={"path": "notes.txt"})
    model = ScriptedModel(_reply("", tool_calls=[call]), _reply("denied"))
    agent, runtime, store, _ = _opted_in(
        tmp_path, model, tools_enabled=True, blocked=("fs.read",))

    result = runtime.send("inspect notes.txt")

    outcome = result.tool_outcomes[0]
    assert outcome.status == "denied" and not outcome.executed
    assert "NOT executed" in outcome.content
    assert _records(store)[0].phase is TurnPhase.COMPLETED


def test_malformed_tool_arguments_never_reach_the_tool(tmp_path):
    inspect_tool = RecordingInspectTool()
    tools = [t for t in default_tools() if t.name != "inspect"] + [inspect_tool]
    call = ToolCall(id="c1", name="inspect", arguments={"bogus": "x"})
    model = ScriptedModel(_reply("", tool_calls=[call]), _reply("recovered"))
    agent, runtime, store, _ = _opted_in(tmp_path, model, tools_enabled=True,
                                         tools=tools)

    result = runtime.send("break it")

    assert inspect_tool.calls == []
    assert result.tool_outcomes[0].status == "failed"
    assert _records(store)[0].phase is TurnPhase.COMPLETED


# ===========================================================================
# 7. supersession at a tool boundary
# ===========================================================================
def test_supersession_at_a_tool_boundary_never_duplicates_execution(tmp_path):
    gate = GateTool()
    tools = list(default_tools()) + [gate]
    call = ToolCall(id="c1", name="gate", arguments={})
    # Only two responses are scripted: the stale turn must never reach a
    # second model call, so a third response would be evidence of a bug.
    model = ScriptedModel(_reply("", tool_calls=[call]), _reply("fresh reply"))
    agent, runtime, store, coordinator = _opted_in(
        tmp_path, model, tools_enabled=True, tools=tools)
    session = runtime.start()
    results: dict[str, object] = {}

    def send(name, text):
        results[name] = runtime.send(text, session=session)

    first = threading.Thread(target=send, args=("first", "run gate"),
                             daemon=True)
    first.start()
    assert gate.entered.wait(WAIT), "gate tool never started"

    stale_id = coordinator.admissions[0].admitted.turn_id
    assert store.load(stale_id).phase is TurnPhase.EXECUTING_TOOL

    second = threading.Thread(target=send, args=("second", "never mind"),
                              daemon=True)
    second.start()
    fresh_admission = coordinator.wait_for(2)
    assert store.load(stale_id).phase is TurnPhase.CANCELLING

    gate.release.set()
    first.join(WAIT)
    second.join(WAIT)

    assert gate.runs == 1                      # no duplicate tool execution
    stale = store.load(stale_id)
    assert stale.phase is TurnPhase.CANCELLED
    assert stale.cancellation_reason is CancellationReason.SUPERSEDED
    assert store.load(fresh_admission.admitted.turn_id).phase is \
        TurnPhase.COMPLETED

    assert not results["first"].ok
    assert results["first"].reply is None
    assert results["second"].ok
    # The stale turn never made a second model call after its tool returned.
    assert model.calls == 2
    # Neither a stale assistant message nor its tool message entered history.
    assert [m for m in session.messages if m.role is Role.TOOL] == []
    assert _texts(session) == ["run gate", "never mind", "fresh reply"]


# ===========================================================================
# 8. provider failure
# ===========================================================================
def test_provider_failure_terminalizes_as_failed_internal_failure(tmp_path):
    model = ScriptedModel()
    model.raises[1] = RuntimeError("provider exploded")
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()

    result = runtime.send("hello", session=session)

    # Legacy outward failure semantics are preserved.
    assert not result.ok
    assert result.reply is None
    assert "the model failed to produce a reply" in result.error

    record = _records(store)[0]
    assert record.phase is TurnPhase.FAILED
    assert record.final_terminal is TurnPhase.FAILED
    assert record.cancellation_reason is CancellationReason.INTERNAL_FAILURE
    # The user message survives; no assistant message was fabricated.
    assert _texts(session) == ["hello"]
    assert [m.content for m in runtime.sessions.load(session.id).messages] == \
        ["hello"]


def test_empty_reply_failure_also_terminalizes(tmp_path):
    model = ScriptedModel(_reply("   "))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    result = runtime.send("hello")
    assert not result.ok
    assert _records(store)[0].phase is TurnPhase.FAILED


def test_oversized_message_leaves_no_nonterminal_turn(tmp_path):
    model = ScriptedModel(_reply("never used"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)

    result = runtime.send("x" * 100_000)

    assert not result.ok
    assert "message rejected" in result.error
    assert model.calls == 0
    assert _records(store)[0].phase is TurnPhase.FAILED


def test_unexpected_exception_cannot_strand_a_nonterminal_turn(tmp_path):
    model = ScriptedModel()
    model.raises[1] = KeyboardInterrupt()
    agent, runtime, store, _ = _opted_in(tmp_path, model)

    with pytest.raises(KeyboardInterrupt):
        runtime.send("hello")

    record = _records(store)[0]
    assert record.is_terminal
    assert record.phase is TurnPhase.FAILED
    assert record.cancellation_reason is CancellationReason.INTERNAL_FAILURE


def test_a_failed_turn_does_not_block_the_next_one(tmp_path):
    model = ScriptedModel(_reply("recovered"))
    model.raises[1] = RuntimeError("boom")
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()

    assert not runtime.send("first", session=session).ok
    second = runtime.send("second", session=session)

    assert second.ok
    assert sorted(r.phase.value for r in _records(store)) == \
        ["completed", "failed"]


# ===========================================================================
# 9. approval stays non-blocking
# ===========================================================================
def test_approval_required_is_non_blocking_and_completes_the_turn(tmp_path):
    class MutatingTool(Tool):
        def __init__(self) -> None:
            self.runs = 0

        @property
        def spec(self):
            return tool_spec("danger_write", "Write something.", parameters=[],
                             capabilities=[Capability.FS_WRITE],
                             read_only=False)

        def run(self, args, context):
            self.runs += 1
            return ToolResult.success("wrote")

    mutating = MutatingTool()
    tools = list(default_tools()) + [mutating]
    call = ToolCall(id="c1", name="danger_write", arguments={})
    model = ScriptedModel(_reply("", tool_calls=[call]),
                          _reply("I could not do that"))
    agent, runtime, store, _ = _opted_in(tmp_path, model, tools_enabled=True,
                                         tools=tools,
                                         level=PermissionLevel.CONFIRM)

    result = runtime.send("please write")

    assert result.ok                                  # the turn still completes
    assert result.needs_tool_approval
    assert mutating.runs == 0
    assert result.tool_outcomes[0].status == "approval_required"
    # The existing PendingAction escalation path is untouched.
    assert len(agent.state.pending_actions) == 1
    assert agent.state.pending_actions[0].tool_name == "danger_write"

    record = _records(store)[0]
    assert record.phase is TurnPhase.COMPLETED
    assert TurnPhase.WAITING_APPROVAL not in {t.to_phase
                                              for t in record.transitions}


# ===========================================================================
# 10. reconciliation and restart safety
# ===========================================================================
def test_interrupted_turn_reconciles_to_failure_and_never_resumes(tmp_path):
    model = ScriptedModel(_reply("after restart"))
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    session = runtime.start()

    # Interrupt a real, admitted, activated turn: the process is presumed to
    # have died before terminalization.
    admission = coordinator.inner.admit(derive_session_key(session.id))
    controller = TurnController(store, admission.admitted.turn_id)
    controller.transition(TurnPhase.QUEUED, TurnPhase.GENERATING,
                          expected_version=admission.admitted.version,
                          operation_id=new_operation_id())
    assert store.load(admission.admitted.turn_id).phase is TurnPhase.GENERATING

    reconciled = agent.reconcile_turns()

    assert len(reconciled) == 1
    record = store.load(admission.admitted.turn_id)
    assert record.phase is TurnPhase.FAILED
    assert record.cancellation_reason is CancellationReason.INTERNAL_FAILURE
    assert model.calls == 0                     # nothing was resumed
    assert _texts(session) == []

    # A later turn is admissible again and starts a fresh generation.
    result = runtime.send("hello", session=session)
    assert result.ok
    assert result.turn_generation == 2


def test_interrupted_finalizing_success_reconciles_to_failure(tmp_path):
    model = ScriptedModel()
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    key = derive_session_key("conv_0123456789ab")
    admission = coordinator.inner.admit(key)
    controller = TurnController(store, admission.admitted.turn_id)
    snapshot = controller.transition(TurnPhase.QUEUED, TurnPhase.GENERATING,
                                     expected_version=1,
                                     operation_id=new_operation_id())
    controller.transition(TurnPhase.GENERATING, TurnPhase.FINALIZING,
                          expected_version=snapshot.version,
                          operation_id=new_operation_id(),
                          requested_terminal=TurnPhase.COMPLETED)

    agent.reconcile_turns()

    record = store.load(admission.admitted.turn_id)
    assert record.phase is TurnPhase.FAILED          # never manufactured success
    assert record.requested_terminal is TurnPhase.COMPLETED


def test_durable_turns_survive_a_store_reopen_and_reconcile(tmp_path):
    directory = tmp_path / "turns"
    model = ScriptedModel(_reply("first"))
    store = JsonFileTurnStore(directory)
    try:
        agent, runtime, _, coordinator = _opted_in(tmp_path, model, store=store)
        session = runtime.start()
        assert runtime.send("hello", session=session).ok
        admission = coordinator.inner.admit(derive_session_key("later"))
        TurnController(store, admission.admitted.turn_id).transition(
            TurnPhase.QUEUED, TurnPhase.GENERATING,
            expected_version=admission.admitted.version,
            operation_id=new_operation_id())
    finally:
        store.close()

    reopened = JsonFileTurnStore(directory)
    try:
        bridge = TurnBridge(TurnCoordinator(reopened))
        bridge.reconcile()
        phases = sorted(reopened.load(t).phase.value
                        for t in reopened.list_turns())
        assert phases == ["completed", "failed"]
    finally:
        reopened.close()


# ===========================================================================
# 11. durable store ownership is host-owned and fails categorically
# ===========================================================================
_OWNERSHIP_PROBE = """
import sys
from agentcore.realtime import JsonFileTurnStore, TurnStoreOwnershipError
try:
    JsonFileTurnStore(sys.argv[1])
except TurnStoreOwnershipError:
    print("OWNERSHIP_REFUSED")
else:
    print("OWNERSHIP_GRANTED")
"""


def test_a_second_process_cannot_own_the_same_durable_turn_store(tmp_path):
    directory = tmp_path / "turns"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(
        Path(__file__).resolve().parent.parent / "src")
    store = JsonFileTurnStore(directory)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _OWNERSHIP_PROBE, str(directory)],
            capture_output=True, text=True, timeout=WAIT, env=environment,
            check=False)
    finally:
        store.close()
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OWNERSHIP_REFUSED"


def test_store_ownership_loss_is_categorical_and_never_degrades(tmp_path):
    model = ScriptedModel(_reply("never reached"))
    inner = InMemoryTurnStore()
    coordinator = TurnCoordinator(ExplodingStore(inner))
    agent = _agent(_config(tmp_path), model, coordinator=coordinator)
    runtime = agent.conversation()

    with pytest.raises(TurnStoreOwnershipError):
        runtime.send("hello")

    # No silent in-memory fallback and no model work behind the failure.
    assert model.calls == 0
    assert inner.list_turns() == []


# ===========================================================================
# 12. stale fences
# ===========================================================================
def test_a_stale_generation_cannot_mutate_session_or_result(tmp_path):
    model = ScriptedModel(_reply("stale"), _reply("fresh"))
    release = model.gate(1)
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    session = runtime.start()

    def send(text, sink):
        sink.append(runtime.send(text, session=session))

    stale_results: list = []
    fresh_results: list = []
    first = threading.Thread(target=send, args=("one", stale_results),
                             daemon=True)
    first.start()
    model.wait_entered(1)
    second = threading.Thread(target=send, args=("two", fresh_results),
                              daemon=True)
    second.start()
    stale_id = coordinator.admissions[0].admitted.turn_id
    coordinator.wait_for(2)

    # Once superseded, the old generation owns nothing: it holds the lease but
    # its record has already moved on, so its own commit must lose.
    assert store.load(stale_id).phase is TurnPhase.CANCELLING
    # Its user message is the only thing it legitimately contributed.
    assert _texts(session) == ["one"]

    release.set()
    first.join(WAIT)
    second.join(WAIT)

    stale = stale_results[0]
    assert not stale.ok and stale.reply is None
    assert stale.turn_phase == "cancelled"
    assert stale.turn_id == stale_id
    assert "stale" not in _texts(session)
    persisted = runtime.sessions.load(session.id)
    assert "stale" not in [m.content for m in persisted.messages]
    assert fresh_results[0].ok
    assert fresh_results[0].turn_generation == 2


def test_shutdown_requests_cooperative_cancellation(tmp_path):
    model = ScriptedModel(_reply("late reply"))
    release = model.gate(1)
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    session = runtime.start()
    results: list = []

    thread = threading.Thread(
        target=lambda: results.append(runtime.send("hello", session=session)),
        daemon=True)
    thread.start()
    model.wait_entered(1)

    agent.shutdown()          # cooperative: does not abort the provider call
    release.set()
    thread.join(WAIT)

    record = _records(store)[0]
    assert record.is_terminal
    assert record.phase is TurnPhase.CANCELLED
    assert record.cancellation_reason is CancellationReason.AGENT_SHUTDOWN
    assert not results[0].ok
    assert "late reply" not in _texts(session)


# ===========================================================================
# 13. composition seam
# ===========================================================================
def test_build_agent_forwards_the_coordinator_dependency(tmp_path):
    coordinator = TurnCoordinator(InMemoryTurnStore())
    agent = build_agent(_config(tmp_path), model=ScriptedModel(_reply("hi")),
                        turn_coordinator=coordinator)
    assert agent.turn_coordinator is coordinator
    assert agent.conversation().turns is not None
    assert agent.conversation().send("hello").ok


def test_one_stage_lease_and_session_lock_are_shared_by_every_runtime(tmp_path):
    model = ScriptedModel(_reply("a"), _reply("b"))
    agent, first, store, _ = _opted_in(tmp_path, model)
    second = agent.conversation()
    assert first.turns is second.turns
    assert first.turns.stage_lease is second.turns.stage_lease
    assert first.turns.session_lock is second.turns.session_lock


def test_turn_records_carry_no_conversation_content(tmp_path):
    secret = "sentinel-user-text-42-3"
    model = ScriptedModel(_reply("sentinel-assistant-text-42-3"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    session = runtime.start()
    session.id = "raw-session-id-sentinel"
    runtime.send(secret, session=session)

    serialised = json.dumps(_records(store)[0].to_dict(), ensure_ascii=False)
    for forbidden in (secret, "sentinel-assistant-text-42-3",
                      "raw-session-id-sentinel", str(agent.workspace.root)):
        assert forbidden not in serialised
    assert {t.note for t in _records(store)[0].transitions} == {""}


# ===========================================================================
# 14. Deadline.unbounded() scaffolding (no timeout enforcement)
# ===========================================================================
def test_unbounded_deadline_never_expires_and_carries_no_timestamps():
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    deadline = Deadline.unbounded()

    assert deadline.is_unbounded
    assert deadline.monotonic_expires_at is None
    assert deadline.expires_at_utc is None
    assert deadline.remaining(clock) == math.inf
    assert not deadline.expired(clock)
    clock.advance(10_000_000)
    assert not deadline.expired(clock)


def test_a_child_may_shorten_an_unbounded_deadline_but_never_extend_one():
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    child = Deadline.unbounded().child(5, clock=clock)

    assert not child.is_unbounded
    assert child.remaining(clock) == 5
    clock.advance(5)
    assert child.expired(clock)
    # A bounded parent is still never extended by a longer child request.
    clock2 = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    parent = Deadline.after(3, clock=clock2)
    assert parent.child(99, clock=clock2).remaining(clock2) == 3


def test_a_half_specified_deadline_is_rejected():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(TurnValidationError):
        Deadline(1.0, None)
    with pytest.raises(TurnValidationError):
        Deadline(None, now)


def test_owned_turns_persist_no_deadline_at_all(tmp_path):
    model = ScriptedModel(_reply("ok"))
    agent, runtime, store, _ = _opted_in(tmp_path, model)
    runtime.send("hello")
    assert _records(store)[0].deadline_at is None


# ===========================================================================
# 15. the session stays admissible across supersession cycles
# ===========================================================================
def test_repeated_supersession_never_corrupts_session_admission(tmp_path):
    model = ScriptedModel(*[_reply(f"reply {index}") for index in range(8)])
    agent, runtime, store, coordinator = _opted_in(tmp_path, model)
    session = runtime.start()

    for cycle in range(3):
        # Two model calls per cycle: the gated predecessor and its successor.
        gated_call = 2 * cycle + 1
        release = model.gate(gated_call)
        results: dict[str, object] = {}

        def send(name, text):
            results[name] = runtime.send(text, session=session)

        first = threading.Thread(target=send,
                                 args=(f"a{cycle}", f"a{cycle}"), daemon=True)
        first.start()
        model.wait_entered(gated_call)
        second = threading.Thread(target=send,
                                  args=(f"b{cycle}", f"b{cycle}"), daemon=True)
        second.start()
        coordinator.wait_for(2 * cycle + 2)
        release.set()
        first.join(WAIT)
        second.join(WAIT)
        assert not first.is_alive() and not second.is_alive()

    # A superseded predecessor always terminalizes before its successor runs,
    # so the coordinator never sees inconsistent nonterminal ordering.
    records = _records(store)
    assert len(records) == 6
    assert all(record.is_terminal for record in records)
    assert sorted(r.phase.value for r in records) == \
        ["cancelled"] * 3 + ["completed"] * 3
    assert [r.generation for r in sorted(records, key=lambda r: r.generation)] \
        == [1, 2, 3, 4, 5, 6]

    # And an ordinary turn still works afterwards.
    assert runtime.send("finally", session=session).ok


# ===========================================================================
# 16. observability privacy
# ===========================================================================
def test_turn_events_never_log_raw_session_ids_or_content(tmp_path):
    raw = "raw-id-sentinel with spaces"
    model = ScriptedModel(_reply("assistant-sentinel-text"))
    agent, runtime, store, _ = _opted_in(tmp_path, model, log_level="DEBUG")
    session = runtime.start()
    session.id = raw

    with capture_events() as buffer:
        runtime.send("user-sentinel-text", session=session)

    lines = [json.loads(line) for line in
             buffer.getvalue().splitlines() if line.strip()]
    turn_events = [line for line in lines
                   if str(line.get("event", "")).startswith("turn.")]
    assert turn_events, "no turn lifecycle events were emitted"
    assert {"turn.admitted", "turn.model_started", "turn.finalization_started",
            "turn.finalized"} <= {line["event"] for line in turn_events}
    for line in turn_events:
        payload = json.dumps(line)
        assert raw not in payload
        assert "user-sentinel-text" not in payload
        assert "assistant-sentinel-text" not in payload
        assert derive_session_key(raw) in payload
