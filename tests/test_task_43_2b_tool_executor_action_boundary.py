from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION,
    ActionAdapterRegistry,
    ActionAdapterRegistrationError,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionOutcome,
    ActionProvenance,
    ActionState,
    ActionTransitionProof,
    AuthorityReceipt,
    EffectVerdict,
    InMemoryActionStore,
    JsonFileActionStore,
    NormalizedActionEffect,
    PreparedActionIntent,
    ReconciliationDisposition,
    ReconciliationMetadata,
    authority_binding_digest,
    new_action,
    permission_policy_fingerprint,
)
from agentcore.action_journal.errors import ActionStoreError
from agentcore.config.schema import PermissionConfig
from agentcore.errors import AgentError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import Decision, Outcome, PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import (
    ParamType,
    Tool,
    ToolContext,
    ToolParameter,
    ToolResult,
    tool_spec,
)
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
WAIT = 30.0
SECRET = "task-43.2b-secret-sentinel"
RAW_RESULT = "task-43.2b-raw-provider-result"
RAW_EXCEPTION = "task-43.2b-raw-exception"


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self._lock:
            number = self._counts.get(prefix, 0) + 1
            self._counts[prefix] = number
        return f"{prefix}_{number:012x}"


class RecordingLogger:
    def __init__(self, events=None, context=None) -> None:
        self.events = events if events is not None else []
        self.context = dict(context or {})

    def bind(self, **context):
        return RecordingLogger(self.events, {**self.context, **context})

    def _record(self, level, event, fields):
        self.events.append((level, event, {**self.context, **fields}))

    def debug(self, event, **fields):
        self._record("debug", event, fields)

    def info(self, event, **fields):
        self._record("info", event, fields)

    def warning(self, event, **fields):
        self._record("warning", event, fields)

    def error(self, event, **fields):
        self._record("error", event, fields)

    def exception(self, event, **fields):
        self._record("exception", event, fields)


class RecordingPolicy(PermissionPolicy):
    def __init__(self, events=None, *, allowed=True, after_check=None) -> None:
        super().__init__(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
        self.events = events if events is not None else []
        self.allowed = allowed
        self.after_check = after_check
        self.calls = 0

    def check(self, capabilities, *, description=""):
        self.calls += 1
        self.events.append("permission")
        decision = (
            Decision(Outcome.ALLOW, "test allow")
            if self.allowed
            else Decision(
                Outcome.DENY,
                "test deny",
                capability=Capability.FS_WRITE,
            )
        )
        if self.after_check is not None:
            self.after_check()
        return decision


class EffectTool(Tool):
    def __init__(
        self,
        *,
        name: str = "mutate",
        version: str = "1.0.0",
        read_only: bool = False,
        capabilities=(Capability.FS_WRITE,),
        result: ToolResult | None = None,
        run_hook=None,
        validate_events=None,
    ) -> None:
        self._name = name
        self._version = version
        self._read_only = read_only
        self._capabilities = tuple(capabilities)
        self._result = result or ToolResult.success(RAW_RESULT)
        self._run_hook = run_hook
        self._validate_events = validate_events
        self.runs = 0
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec(
            self._name,
            "Trusted test effect.",
            version=self._version,
            parameters=(
                ToolParameter("payload", ParamType.STRING),
                ToolParameter(
                    "eligibility",
                    ParamType.STRING,
                    required=False,
                ),
                ToolParameter(
                    "effect_class",
                    ParamType.STRING,
                    required=False,
                ),
            ),
            capabilities=self._capabilities,
            read_only=self._read_only,
        )

    def validate_args(self, args):
        if self._validate_events is not None:
            self._validate_events.append("validate")
        return super().validate_args(args)

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        with self._lock:
            self.runs += 1
        if self._run_hook is not None:
            return self._run_hook(args, context)
        return self._result


class FakeAdapter:
    def __init__(
        self,
        tool: EffectTool,
        *,
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
        reconciliation_capabilities=frozenset({Capability.FS_READ}),
        prepared: PreparedActionIntent | None = None,
        normalized: NormalizedActionEffect | None = None,
        events=None,
        normalize_error: BaseException | None = None,
    ) -> None:
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.mutation.v1",
            eligibility=eligibility,
            effect_class=(effect_class if eligibility.requires_journal else None),
            reconciliation_capabilities=reconciliation_capabilities,
        )
        self.prepared = prepared or PreparedActionIntent(
            intent_digest="a" * 64,
            target_category="test.target",
        )
        self.normalized = normalized or NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The effect was confirmed.",
            evidence_refs=("test:receipt:1",),
        )
        self.events = events if events is not None else []
        self.normalize_error = normalize_error
        self.preflight_calls = 0
        self.normalize_calls = 0

    def prepare_intent(self, cleaned, context):
        self.preflight_calls += 1
        self.events.append("preflight")
        return self.prepared

    def normalize_effect(self, result):
        self.normalize_calls += 1
        self.events.append("normalize")
        if self.normalize_error is not None:
            raise self.normalize_error
        return self.normalized


def _context(*, dedup=True) -> ActionInvocationContext:
    return ActionInvocationContext(
        turn_id="turn_000000000001",
        operation_id="op_000000000001",
        run_id="run_000000000001",
        task_id="task_000000000001",
        subtask_id="sub_000000000001",
        schedule_occurrence_id="schedule:test:1",
        deduplication_scope="test:workflow",
        deduplication_key="step:1",
    ) if dedup else ActionInvocationContext()


_DEFAULT_STORE = object()


def _build_executor(
    tool: EffectTool | None = None,
    *,
    adapter: FakeAdapter | None | object = None,
    store=_DEFAULT_STORE,
    policy: PermissionPolicy | None = None,
    logger=None,
    ids=None,
    clock=None,
):
    tool = tool or EffectTool()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    resolved_adapter = FakeAdapter(tool) if adapter is None else adapter
    if resolved_adapter is not False:
        adapters.register(tool, resolved_adapter)
    resolved_clock = clock or FixedClock(NOW)
    resolved_store = (
        InMemoryActionStore(clock=resolved_clock)
        if store is _DEFAULT_STORE
        else store
    )
    executor = ToolExecutor(
        registry,
        policy or RecordingPolicy(),
        action_store=resolved_store,
        action_adapters=adapters,
        clock=resolved_clock,
        id_factory=ids or DeterministicIds(),
        action_owner_id="process:test",
        action_owner_epoch=1,
        logger=logger or RecordingLogger(),
    )
    return executor, resolved_store, adapters, resolved_adapter


def _invoke(executor, *, context=None, checkpoint=None, args=None):
    return executor.invoke(
        "mutate",
        args or {"payload": SECRET},
        checkpoint=checkpoint,
        action_context=context,
        untrusted_meta=SECRET,
    )


def _outcome(verdict: EffectVerdict) -> ActionOutcome:
    return ActionOutcome(
        verdict=verdict,
        result_class="test.result",
        summary="Safe durable truth.",
        partial_categories=(
            ("first_step",)
            if verdict is EffectVerdict.CONFIRMED_PARTIAL
            else ()
        ),
    )


def _seed_record(
    store,
    *,
    state: ActionState,
    number: int = 90,
    with_previous_attempt: bool = False,
):
    record = new_action(
        action_kind="test.mutation.v1",
        tool_name="mutate",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=("fs.read",),
        effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
        intent_digest="a" * 64,
        intent_ref=None,
        target_category="test.target",
        target_fingerprint=None,
        provenance=ActionProvenance(invocation_id=f"inv_{number:012x}"),
        authority=AuthorityReceipt(
            receipt_id=f"auth_{number:012x}",
            binding_digest="b" * 64,
            policy_fingerprint="c" * 64,
            scope_ref=f"live:inv_{number:012x}",
            authorized_at=NOW,
        ),
        deduplication_scope="test:workflow",
        deduplication_key="step:1",
        clock=FixedClock(NOW),
        id_factory=lambda prefix: f"{prefix}_{number:012x}",
    )
    record = store.create(record)
    if state is ActionState.AUTHORIZED and not with_previous_attempt:
        return record
    if state is ActionState.CANCELLED:
        candidate = record.transition(
            state,
            transition_id=f"atx_{number + 1:012x}",
            at=NOW,
        )
        return store.compare_and_swap(record.action_id, record.version, candidate)
    claimed = store.claim_attempt(
        record.action_id,
        record.version,
        attempt_id=f"aat_{number:012x}",
        invocation_id=f"inv_{number:012x}",
        owner_id="process:seed",
        owner_epoch=1,
        transition_id=f"atx_{number + 1:012x}",
    )
    if state is ActionState.IN_FLIGHT:
        return claimed
    if state is ActionState.AUTHORIZED and with_previous_attempt:
        candidate = claimed.transition(
            ActionState.AUTHORIZED,
            transition_id=f"atx_{number + 2:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.NOT_INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        )
    elif state is ActionState.SUCCEEDED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 2:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
            terminal_outcome=_outcome(EffectVerdict.CONFIRMED_APPLIED),
        )
    elif state is ActionState.FAILED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 2:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            terminal_outcome=_outcome(EffectVerdict.CONFIRMED_NOT_APPLIED),
        )
    elif state is ActionState.RECONCILE_REQUIRED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 2:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=ReconciliationMetadata(
                disposition=ReconciliationDisposition.PROBE_LATER,
            ),
        )
    else:  # pragma: no cover - helper misuse
        raise AssertionError(state)
    return store.compare_and_swap(claimed.action_id, claimed.version, candidate)


class ExplodingStore:
    def __getattr__(self, name):
        raise AssertionError(f"ActionStore fast-path access: {name}")


class ExplodingAdapterRegistry:
    def resolve(self, *args, **kwargs):
        raise AssertionError("pure-read fast path consulted action adapters")


class StoreWrapper:
    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class GuardObservationStore(StoreWrapper):
    def __init__(self, wrapped) -> None:
        super().__init__(wrapped)
        self._calls = 0
        self._lock = threading.Lock()
        self.second_attempted = threading.Event()

    @contextmanager
    def execution_guard(self, action_id):
        with self._lock:
            self._calls += 1
            if self._calls == 2:
                self.second_attempted.set()
        with self.wrapped.execution_guard(action_id):
            yield


def test_pure_read_fast_path_order_and_store_is_never_touched():
    events: list[str] = []
    tool = EffectTool(
        name="read",
        read_only=True,
        capabilities=(Capability.FS_READ,),
        validate_events=events,
        run_hook=lambda args, context: events.append("run") or ToolResult.success("ok"),
    )
    registry = ToolRegistry()
    registry.register(tool)
    policy = RecordingPolicy(events)
    executor = ToolExecutor(
        registry,
        policy,
        action_store=ExplodingStore(),
        action_adapters=ExplodingAdapterRegistry(),
        logger=RecordingLogger(),
    )

    def checkpoint():
        events.append("checkpoint")

    invocation = executor.invoke(
        "read", {"payload": SECRET}, checkpoint=checkpoint,
    )

    assert invocation.ok
    assert events == ["validate", "checkpoint", "permission", "checkpoint", "run"]
    assert invocation.action_id is None
    assert invocation.attempt_id is None


def test_ephemeral_control_fast_path_order_and_store_is_never_touched():
    events: list[str] = []
    tool = EffectTool(
        capabilities=(Capability.APP_CONTROL,),
        validate_events=events,
        run_hook=lambda args, context: events.append("run") or ToolResult.success("ok"),
    )
    adapter = FakeAdapter(
        tool,
        eligibility=ActionJournalEligibility.EPHEMERAL_CONTROL,
        reconciliation_capabilities=frozenset(),
        events=events,
    )
    executor, _, _, _ = _build_executor(
        tool, adapter=adapter, store=ExplodingStore(),
        policy=RecordingPolicy(events),
    )

    invocation = _invoke(
        executor,
        checkpoint=lambda: events.append("checkpoint"),
    )

    assert invocation.ok
    assert events == ["validate", "checkpoint", "permission", "checkpoint", "run"]
    assert adapter.preflight_calls == 0
    assert adapter.normalize_calls == 0
    assert invocation.action_id is None


def test_ephemeral_control_still_respects_permission_denial():
    tool = EffectTool(capabilities=(Capability.APP_CONTROL,))
    adapter = FakeAdapter(
        tool,
        eligibility=ActionJournalEligibility.EPHEMERAL_CONTROL,
        reconciliation_capabilities=frozenset(),
    )
    executor, _, _, _ = _build_executor(
        tool, adapter=adapter, store=ExplodingStore(),
        policy=RecordingPolicy(allowed=False),
    )

    invocation = _invoke(executor)

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "permission_denied"
    assert tool.runs == 0


def test_unregistered_mutation_fails_closed_before_permission_or_run():
    tool = EffectTool()
    policy = RecordingPolicy()
    executor, store, _, _ = _build_executor(
        tool, adapter=False, policy=policy,
    )

    invocation = _invoke(executor)

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_adapter_missing"
    assert policy.calls == 0
    assert tool.runs == 0
    assert store.list_actions() == []


def test_model_arguments_cannot_downgrade_or_choose_effect_class():
    tool = EffectTool()
    executor, store, _, _ = _build_executor(tool)

    invocation = _invoke(
        executor,
        context=_context(),
        args={
            "payload": SECRET,
            "eligibility": "ephemeral_control",
            "effect_class": "externally_idempotent",
        },
    )
    record = store.load(invocation.action_id)

    assert tool.runs == 1
    assert record.eligibility is ActionJournalEligibility.DURABLE_LOCAL_MUTATION
    assert record.effect_class is ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE


def test_wrong_tool_version_and_duplicate_adapter_registration_fail_closed():
    tool = EffectTool()
    adapter = FakeAdapter(tool)
    adapters = ActionAdapterRegistry()
    adapters.register(tool, adapter)
    with pytest.raises(ActionAdapterRegistrationError):
        adapters.register(tool, adapter)

    replacement = EffectTool(version="2.0.0")
    registry = ToolRegistry()
    registry.register(replacement)
    executor = ToolExecutor(
        registry,
        RecordingPolicy(),
        action_store=InMemoryActionStore(clock=FixedClock(NOW)),
        action_adapters=adapters,
        logger=RecordingLogger(),
    )
    invocation = executor.invoke("mutate", {"payload": SECRET})
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_adapter_version_mismatch"
    assert replacement.runs == 0


def test_registered_classification_is_an_immutable_snapshot():
    tool = EffectTool()
    adapter = FakeAdapter(tool)
    adapters = ActionAdapterRegistry()
    adapters.register(tool, adapter)
    adapter.spec = replace(
        adapter.spec,
        eligibility=ActionJournalEligibility.EPHEMERAL_CONTROL,
        effect_class=None,
        reconciliation_capabilities=frozenset(),
    )
    store = InMemoryActionStore(clock=FixedClock(NOW))
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(
        registry,
        RecordingPolicy(),
        action_store=store,
        action_adapters=adapters,
        clock=FixedClock(NOW),
        id_factory=DeterministicIds(),
        action_owner_id="process:test",
        logger=RecordingLogger(),
    )

    invocation = _invoke(executor, context=_context())

    assert invocation.ok
    assert invocation.action_id is not None
    assert store.load(invocation.action_id).eligibility is (
        ActionJournalEligibility.DURABLE_LOCAL_MUTATION
    )


@pytest.mark.parametrize(
    ("capability", "eligibility"),
    [
        (Capability.EMAIL_SEND, ActionJournalEligibility.EPHEMERAL_CONTROL),
        (Capability.FS_WRITE, ActionJournalEligibility.EPHEMERAL_CONTROL),
        (Capability.PAYMENT, ActionJournalEligibility.EXTERNAL_ACCOUNT_MUTATION),
    ],
)
def test_ephemeral_durable_and_financial_registrations_fail_closed(
    capability, eligibility,
):
    tool = EffectTool(capabilities=(capability,))
    adapter = FakeAdapter(
        tool,
        eligibility=eligibility,
        effect_class=ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS,
        reconciliation_capabilities=frozenset(),
    )
    with pytest.raises(ActionAdapterRegistrationError):
        ActionAdapterRegistry().register(tool, adapter)


@pytest.mark.parametrize(
    ("read_only", "eligibility", "effect_class", "reconciliation_caps"),
    [
        (
            True,
            ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            frozenset({Capability.FS_READ}),
        ),
        (
            False,
            ActionJournalEligibility.PURE_READ,
            None,
            frozenset(),
        ),
        (
            False,
            ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            None,
            frozenset({Capability.FS_READ}),
        ),
    ],
)
def test_contradictory_adapter_registration_fails_closed(
    read_only, eligibility, effect_class, reconciliation_caps,
):
    tool = EffectTool(
        read_only=read_only,
        capabilities=(Capability.FS_READ,) if read_only else (Capability.FS_WRITE,),
    )
    adapter = FakeAdapter(tool)
    adapter.spec = ActionAdapterSpec(
        tool_name=tool.spec.name,
        tool_version=tool.spec.version,
        action_kind="test.mutation.v1",
        eligibility=eligibility,
        effect_class=effect_class,
        reconciliation_capabilities=reconciliation_caps,
    )
    with pytest.raises(ActionAdapterRegistrationError):
        ActionAdapterRegistry().register(tool, adapter)


def test_journal_required_without_store_fails_closed_after_current_permission():
    tool = EffectTool()
    policy = RecordingPolicy()
    executor, _, _, adapter = _build_executor(tool, store=None, policy=policy)

    invocation = _invoke(executor)

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_store_unavailable"
    assert adapter.preflight_calls == 1
    assert policy.calls == 1
    assert tool.runs == 0


def test_preflight_precedes_permission_and_cannot_enter_tool_run():
    events: list[str] = []
    tool = EffectTool(run_hook=lambda args, context: events.append("run") or ToolResult.success())
    adapter = FakeAdapter(tool, events=events)
    executor, _, _, _ = _build_executor(
        tool, adapter=adapter, policy=RecordingPolicy(events, allowed=False),
    )

    invocation = _invoke(executor)

    assert not invocation.ok
    assert events == ["preflight", "permission"]
    assert tool.runs == 0


def test_permission_denial_creates_no_action_and_no_physical_attempt():
    executor, store, _, _ = _build_executor(policy=RecordingPolicy(allowed=False))

    invocation = _invoke(executor, context=_context())

    assert invocation.result.metadata["kind"] == "permission_denied"
    assert store.list_actions() == []


def test_cancellation_before_permission_creates_no_action_or_effect():
    executor, store, _, _ = _build_executor()

    def cancelled():
        raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    with pytest.raises(CancellationRequested):
        _invoke(executor, checkpoint=cancelled)
    assert store.list_actions() == []


def test_cancellation_after_permission_before_action_create_creates_no_action():
    cancelled = threading.Event()
    policy = RecordingPolicy(after_check=cancelled.set)
    executor, store, _, _ = _build_executor(policy=policy)

    def checkpoint():
        if cancelled.is_set():
            raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    with pytest.raises(CancellationRequested):
        _invoke(executor, checkpoint=checkpoint)
    assert store.list_actions() == []


def test_fresh_action_is_authorized_then_exact_claim_is_readable_before_run():
    observations = []
    holder = {}

    def run(args, context):
        record = holder["store"].load(holder["action_id"])
        observations.append(record)
        return ToolResult.success(RAW_RESULT)

    tool = EffectTool(run_hook=run)
    executor, store, _, _ = _build_executor(tool)
    holder["store"] = store
    original_create = store.create

    def create(record):
        created = original_create(record)
        holder["action_id"] = created.action_id
        observations.append(created)
        return created

    store.create = create

    invocation = _invoke(executor, context=_context())

    assert invocation.ok
    assert observations[0].state is ActionState.AUTHORIZED
    assert observations[0].attempts == ()
    in_flight = observations[1]
    assert in_flight.state is ActionState.IN_FLIGHT
    assert len(in_flight.attempts) == 1
    assert in_flight.attempts[0].attempt_id == invocation.attempt_id
    assert in_flight.attempts[0].invocation_id == invocation.invocation_id


def test_claim_reload_failure_blocks_tool_after_durable_claim():
    wrapped = InMemoryActionStore(clock=FixedClock(NOW))

    class NoClaimReloadStore(StoreWrapper):
        def __init__(self, store):
            super().__init__(store)
            self.claimed = False

        def claim_attempt(self, *args, **kwargs):
            result = self.wrapped.claim_attempt(*args, **kwargs)
            self.claimed = True
            return result

        def load(self, action_id):
            if self.claimed:
                raise ActionStoreError("claim reload blocked")
            return self.wrapped.load(action_id)

    store = NoClaimReloadStore(wrapped)
    tool = EffectTool()
    executor, _, _, _ = _build_executor(tool, store=store)

    invocation = _invoke(executor, context=_context())

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_claim_unverified"
    assert tool.runs == 0
    assert wrapped.list_nonterminal()[0].state is ActionState.IN_FLIGHT


def test_store_general_mutex_is_not_held_during_tool_run():
    entered = threading.Event()
    release = threading.Event()
    result = []

    def run(args, context):
        entered.set()
        assert release.wait(WAIT)
        return ToolResult.success(RAW_RESULT)

    tool = EffectTool(run_hook=run)
    executor, store, _, _ = _build_executor(tool)

    thread = threading.Thread(
        target=lambda: result.append(_invoke(executor, context=_context())),
        daemon=True,
    )
    thread.start()
    assert entered.wait(WAIT)
    loaded = threading.Event()
    loader = threading.Thread(
        target=lambda: (store.list_nonterminal(), loaded.set()),
        daemon=True,
    )
    loader.start()
    assert loaded.wait(WAIT), "store mutex remained held during Tool.run"
    release.set()
    thread.join(WAIT)
    loader.join(WAIT)
    assert result[0].ok


def test_duplicate_callers_attach_and_only_one_physical_run_occurs():
    entered = threading.Event()
    release = threading.Event()

    def run(args, context):
        entered.set()
        assert release.wait(WAIT)
        return ToolResult.success(RAW_RESULT)

    tool = EffectTool(run_hook=run)
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    store = GuardObservationStore(underlying)
    executor, _, _, _ = _build_executor(tool, store=store)
    start = threading.Barrier(3)
    results = []

    def caller():
        start.wait()
        results.append(_invoke(executor, context=_context()))

    first = threading.Thread(target=caller, daemon=True)
    second = threading.Thread(target=caller, daemon=True)
    first.start()
    second.start()
    start.wait()
    assert entered.wait(WAIT)
    assert store.second_attempted.wait(WAIT)
    assert tool.runs == 1
    release.set()
    first.join(WAIT)
    second.join(WAIT)

    assert len(results) == 2
    assert tool.runs == 1
    assert results[0].action_id == results[1].action_id
    assert all(result.ok for result in results)


def test_json_store_domain_guard_serializes_two_executor_instances(tmp_path):
    first_store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    second_store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    entered = threading.Event()
    release = threading.Event()
    tool = EffectTool(
        run_hook=lambda args, context: (
            entered.set(),
            release.wait(WAIT),
            ToolResult.success(RAW_RESULT),
        )[-1],
    )
    ids = DeterministicIds()
    first, _, _, _ = _build_executor(tool, store=first_store, ids=ids)
    second, _, _, _ = _build_executor(tool, store=second_store, ids=ids)
    results = []
    try:
        t1 = threading.Thread(
            target=lambda: results.append(_invoke(first, context=_context())),
            daemon=True,
        )
        t2 = threading.Thread(
            target=lambda: results.append(_invoke(second, context=_context())),
            daemon=True,
        )
        t1.start()
        assert entered.wait(WAIT)
        t2.start()
        release.set()
        t1.join(WAIT)
        t2.join(WAIT)
        assert tool.runs == 1
        assert len(results) == 2
    finally:
        first_store.close()
        second_store.close()


def test_different_action_ids_are_not_globally_serialized():
    both_entered = threading.Barrier(3)
    release = threading.Event()

    def run(args, context):
        both_entered.wait()
        assert release.wait(WAIT)
        return ToolResult.success(RAW_RESULT)

    tool = EffectTool(run_hook=run)
    executor, store, _, _ = _build_executor(tool)
    results = []
    first = threading.Thread(
        target=lambda: results.append(_invoke(executor, context=_context(dedup=False))),
        daemon=True,
    )
    second = threading.Thread(
        target=lambda: results.append(_invoke(executor, context=_context(dedup=False))),
        daemon=True,
    )
    first.start()
    second.start()
    both_entered.wait()
    assert tool.runs == 2
    release.set()
    first.join(WAIT)
    second.join(WAIT)
    assert len(store.list_actions()) == 2


@pytest.mark.parametrize(
    ("state", "previous_attempt", "kind"),
    [
        (ActionState.SUCCEEDED, False, "action_succeeded"),
        (ActionState.FAILED, False, "action_failed"),
        (ActionState.CANCELLED, False, "action_cancelled"),
        (ActionState.IN_FLIGHT, False, "action_in_progress"),
        (
            ActionState.RECONCILE_REQUIRED,
            False,
            "action_reconciliation_required",
        ),
        (ActionState.AUTHORIZED, False, "action_pending"),
        (
            ActionState.AUTHORIZED,
            True,
            "action_retry_recovery_required",
        ),
    ],
)
def test_attached_action_never_executes_in_first_attempt_slice(
    state, previous_attempt, kind,
):
    store = InMemoryActionStore(clock=FixedClock(NOW))
    seeded = _seed_record(
        store,
        state=state,
        with_previous_attempt=previous_attempt,
    )
    tool = EffectTool()
    executor, _, _, _ = _build_executor(tool, store=store)

    invocation = _invoke(executor, context=_context())

    assert tool.runs == 0
    assert invocation.action_id == seeded.action_id
    assert invocation.action_state == state.value
    assert invocation.result.metadata["kind"] == kind
    assert invocation.ok is (state is ActionState.SUCCEEDED)


def test_final_checkpoint_cancellation_publishes_not_invoked_then_propagates():
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    tool = EffectTool()
    executor, store, _, _ = _build_executor(tool)

    with pytest.raises(CancellationRequested):
        _invoke(executor, context=_context(), checkpoint=checkpoint)

    record = store.list_actions() and store.load(store.list_actions()[0])
    assert record.state is ActionState.CANCELLED
    assert record.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert record.transitions[-1].proof is ActionTransitionProof.LIVE_OWNER_NOT_INVOKED
    assert tool.runs == 0


def test_cancellation_after_authorized_before_claim_terminalizes_without_attempt():
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    executor, store, _, _ = _build_executor()
    with pytest.raises(CancellationRequested):
        _invoke(executor, context=_context(), checkpoint=checkpoint)
    record = store.load(store.list_actions()[0])
    assert record.state is ActionState.CANCELLED
    assert record.attempts == ()


@pytest.mark.parametrize(
    ("normalized", "expected_state", "expected_ok"),
    [
        (
            NormalizedActionEffect(
                verdict=EffectVerdict.CONFIRMED_APPLIED,
                result_class="test.applied",
                summary="Applied.",
                evidence_refs=("receipt:applied",),
            ),
            ActionState.SUCCEEDED,
            True,
        ),
        (
            NormalizedActionEffect(
                verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
                result_class="test.not_applied",
                summary="Not applied.",
                evidence_refs=("receipt:absent",),
            ),
            ActionState.FAILED,
            False,
        ),
        (
            NormalizedActionEffect(
                verdict=EffectVerdict.CONFIRMED_PARTIAL,
                result_class="test.partial",
                summary="Partially applied.",
                evidence_refs=("receipt:partial",),
                partial_categories=("first_step",),
            ),
            ActionState.FAILED,
            False,
        ),
        (
            NormalizedActionEffect(
                verdict=EffectVerdict.OUTCOME_UNKNOWN,
                result_class="test.unknown",
                summary="Verification is required.",
                evidence_refs=("receipt:unknown",),
                reconciliation_disposition=ReconciliationDisposition.PROBE_LATER,
            ),
            ActionState.RECONCILE_REQUIRED,
            False,
        ),
    ],
)
def test_trusted_effect_normalization_publishes_before_return(
    normalized, expected_state, expected_ok,
):
    tool = EffectTool(result=ToolResult.failure("generic result is not truth"))
    adapter = FakeAdapter(tool, normalized=normalized)
    executor, store, _, _ = _build_executor(tool, adapter=adapter)

    invocation = _invoke(executor, context=_context())
    record = store.load(invocation.action_id)

    assert record.state is expected_state
    assert invocation.action_state == expected_state.value
    assert invocation.ok is expected_ok
    assert record.attempts[-1].status is ActionAttemptStatus.INVOKED
    assert record.attempts[-1].effect_verdict is normalized.verdict
    assert record.evidence_refs == normalized.evidence_refs
    if normalized.verdict is EffectVerdict.CONFIRMED_PARTIAL:
        assert record.terminal_outcome.partial_categories == ("first_step",)


def test_toolresult_ok_and_failure_do_not_independently_establish_truth():
    no_effect = NormalizedActionEffect(
        verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        result_class="trusted.no_effect",
        summary="No effect.",
    )
    ok_tool = EffectTool(result=ToolResult.success("generic ok"))
    ok_adapter = FakeAdapter(ok_tool, normalized=no_effect)
    ok_executor, ok_store, _, _ = _build_executor(ok_tool, adapter=ok_adapter)
    first = _invoke(ok_executor, context=_context())
    assert ok_store.load(first.action_id).state is ActionState.FAILED

    applied = NormalizedActionEffect(
        verdict=EffectVerdict.CONFIRMED_APPLIED,
        result_class="trusted.applied",
        summary="Applied.",
    )
    failed_tool = EffectTool(result=ToolResult.failure("generic failure"))
    failed_adapter = FakeAdapter(failed_tool, normalized=applied)
    failed_executor, failed_store, _, _ = _build_executor(
        failed_tool, adapter=failed_adapter,
    )
    second = _invoke(failed_executor, context=_context())
    assert failed_store.load(second.action_id).state is ActionState.SUCCEEDED


@pytest.mark.parametrize(
    "failure",
    [RuntimeError(RAW_EXCEPTION), AgentError(RAW_EXCEPTION)],
)
def test_exception_after_seam_becomes_reconciliation_without_raw_text(failure):
    logger = RecordingLogger()

    def run(args, context):
        raise failure

    tool = EffectTool(run_hook=run)
    executor, store, _, _ = _build_executor(tool, logger=logger)

    invocation = _invoke(executor, context=_context())
    record = store.load(invocation.action_id)

    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.attempts[-1].status is ActionAttemptStatus.INVOKED
    assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert RAW_EXCEPTION not in repr(logger.events)
    assert RAW_EXCEPTION not in repr(invocation.to_dict())
    assert RAW_EXCEPTION not in repr(record.to_dict())


def test_normalizer_exception_becomes_reconciliation():
    tool = EffectTool()
    adapter = FakeAdapter(
        tool,
        normalize_error=RuntimeError(RAW_EXCEPTION),
    )
    executor, store, _, _ = _build_executor(tool, adapter=adapter)
    invocation = _invoke(executor, context=_context())
    assert store.load(invocation.action_id).state is ActionState.RECONCILE_REQUIRED
    assert not invocation.ok


def test_cancellation_from_inside_tool_becomes_reconciliation_then_propagates():
    def run(args, context):
        raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    tool = EffectTool(run_hook=run)
    executor, store, _, _ = _build_executor(tool)

    with pytest.raises(CancellationRequested):
        _invoke(executor, context=_context())

    record = store.load(store.list_actions()[0])
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.attempts[-1].status is ActionAttemptStatus.INVOKED
    assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN


def test_cancellation_observed_after_tool_return_cannot_erase_success():
    cancelled = threading.Event()

    def run(args, context):
        cancelled.set()
        return ToolResult.success(RAW_RESULT)

    tool = EffectTool(run_hook=run)
    executor, store, _, _ = _build_executor(tool)
    invocation = _invoke(executor, context=_context())
    assert cancelled.is_set()
    assert store.load(invocation.action_id).state is ActionState.SUCCEEDED


class ResultCasFailureStore(StoreWrapper):
    def compare_and_swap(self, action_id, expected_version, next_record):
        if next_record.state is not ActionState.IN_FLIGHT:
            raise ActionStoreError("result publication failed")
        return self.wrapped.compare_and_swap(action_id, expected_version, next_record)


def test_result_publication_failure_never_repeats_or_claims_done():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    store = ResultCasFailureStore(underlying)
    tool = EffectTool()
    executor, _, _, _ = _build_executor(tool, store=store)

    invocation = _invoke(executor, context=_context())

    assert tool.runs == 1
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_result_publication_uncertain"
    assert invocation.action_state == ActionState.IN_FLIGHT.value
    assert underlying.list_nonterminal()[0].state is ActionState.IN_FLIGHT


def test_result_reload_failure_never_claims_durable_success():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))

    class ResultReloadFailureStore(StoreWrapper):
        def __init__(self, wrapped):
            super().__init__(wrapped)
            self.result_written = False

        def compare_and_swap(self, action_id, expected_version, next_record):
            accepted = self.wrapped.compare_and_swap(
                action_id, expected_version, next_record,
            )
            if next_record.state is not ActionState.IN_FLIGHT:
                self.result_written = True
            return accepted

        def load(self, action_id):
            if self.result_written:
                raise ActionStoreError("result reload failed")
            return self.wrapped.load(action_id)

    tool = EffectTool()
    executor, _, _, _ = _build_executor(
        tool, store=ResultReloadFailureStore(underlying),
    )
    invocation = _invoke(executor, context=_context())

    assert tool.runs == 1
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_result_publication_uncertain"
    assert invocation.action_state == ActionState.IN_FLIGHT.value
    assert underlying.list_actions()
    assert underlying.load(underlying.list_actions()[0]).state is ActionState.SUCCEEDED


def test_uncertain_claim_commit_exact_reload_permits_one_physical_call():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))

    class ClaimCommittedThenRaised(StoreWrapper):
        def claim_attempt(self, *args, **kwargs):
            self.wrapped.claim_attempt(*args, **kwargs)
            raise ActionStoreError("uncertain claim write")

    tool = EffectTool()
    executor, _, _, _ = _build_executor(
        tool, store=ClaimCommittedThenRaised(underlying),
    )
    invocation = _invoke(executor, context=_context())
    assert invocation.ok
    assert tool.runs == 1


def test_claim_failure_without_exact_proof_blocks_physical_call():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))

    class ClaimNotCommitted(StoreWrapper):
        def claim_attempt(self, *args, **kwargs):
            raise ActionStoreError("claim failed")

    tool = EffectTool()
    executor, _, _, _ = _build_executor(tool, store=ClaimNotCommitted(underlying))
    invocation = _invoke(executor, context=_context())
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_claim_unverified"
    assert tool.runs == 0


def test_claim_reload_must_prove_exact_live_owner():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))

    class WrongOwnerClaimStore(StoreWrapper):
        def __init__(self, wrapped):
            super().__init__(wrapped)
            self.claimed = False

        def claim_attempt(self, *args, **kwargs):
            claimed = self.wrapped.claim_attempt(*args, **kwargs)
            self.claimed = True
            return claimed

        def load(self, action_id):
            record = self.wrapped.load(action_id)
            if not self.claimed:
                return record
            attempt = replace(record.attempts[-1], owner_id="process:other")
            return replace(record, attempts=(*record.attempts[:-1], attempt))

    tool = EffectTool()
    executor, _, _, _ = _build_executor(
        tool, store=WrongOwnerClaimStore(underlying),
    )
    invocation = _invoke(executor, context=_context())

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_claim_unverified"
    assert tool.runs == 0


def test_uncertain_create_commit_exact_reload_can_continue_safely():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))

    class CreateCommittedThenRaised(StoreWrapper):
        def create(self, record):
            self.wrapped.create(record)
            raise ActionStoreError("uncertain create write")

    tool = EffectTool()
    executor, _, _, _ = _build_executor(
        tool, store=CreateCommittedThenRaised(underlying),
    )
    invocation = _invoke(executor, context=_context())
    assert invocation.ok
    assert tool.runs == 1


def test_corrupt_create_return_cannot_attach_unrelated_action():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    unrelated = _seed_record(
        underlying,
        state=ActionState.SUCCEEDED,
        number=180,
    )

    class UnrelatedCreateStore(StoreWrapper):
        def create(self, record):
            return unrelated

    tool = EffectTool()
    executor, _, _, _ = _build_executor(
        tool, store=UnrelatedCreateStore(underlying),
    )
    invocation = _invoke(executor, context=_context(dedup=False))

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_create_unverified"
    assert tool.runs == 0


def test_mutation_invocation_journal_and_logs_exclude_raw_arguments_and_results():
    logger = RecordingLogger()
    tool = EffectTool(result=ToolResult.success(RAW_RESULT, provider_raw=RAW_RESULT))
    executor, store, _, _ = _build_executor(tool, logger=logger)

    invocation = _invoke(executor, context=_context())
    record = store.load(invocation.action_id)
    combined = repr((logger.events, invocation.to_dict(), record.to_dict()))

    assert invocation.args == {"payload": "***"}
    assert SECRET not in combined
    assert RAW_RESULT not in combined
    assert record.intent_ref is None


def test_mutation_argument_log_is_redacted_even_when_adapter_is_missing():
    logger = RecordingLogger()
    tool = EffectTool()
    executor, _, _, _ = _build_executor(
        tool, adapter=False, logger=logger,
    )
    invocation = _invoke(executor)
    assert not invocation.ok
    assert SECRET not in repr(logger.events)
    assert SECRET not in repr(invocation.to_dict())


def test_unknown_mutation_argument_name_cannot_smuggle_a_secret():
    logger = RecordingLogger()
    tool = EffectTool()
    executor, _, _, _ = _build_executor(tool, logger=logger)

    invocation = executor.invoke("mutate", {SECRET: "value"})
    combined = repr((logger.events, invocation.to_dict()))

    assert not invocation.ok
    assert invocation.args == {}
    assert SECRET not in combined
    assert tool.runs == 0


def test_mutation_validation_exception_is_content_free():
    class ExplodingValidationTool(EffectTool):
        def validate_args(self, args):
            raise RuntimeError(SECRET)

    logger = RecordingLogger()
    tool = ExplodingValidationTool()
    executor, _, _, _ = _build_executor(tool, logger=logger)

    invocation = _invoke(executor)
    combined = repr((logger.events, invocation.to_dict()))

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "validation"
    assert SECRET not in combined
    assert tool.runs == 0


def test_protected_payload_requirement_fails_closed_without_fake_storage():
    tool = EffectTool()
    adapter = FakeAdapter(
        tool,
        prepared=PreparedActionIntent(
            intent_digest="a" * 64,
            intent_ref="protected:new:payload",
            target_category="test.target",
            requires_protected_payload=True,
        ),
    )
    executor, store, _, _ = _build_executor(tool, adapter=adapter)
    invocation = _invoke(executor, context=_context())
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "protected_payload_unavailable"
    assert store.list_actions() == []
    assert tool.runs == 0


def test_action_store_corruption_blocks_mutation_but_not_independent_read():
    class CorruptStore:
        def create(self, record):
            raise ActionStoreError("corrupt")

    mutation = EffectTool()
    executor, _, _, _ = _build_executor(mutation, store=CorruptStore())
    blocked = _invoke(executor, context=_context())
    assert not blocked.ok
    assert mutation.runs == 0

    read = EffectTool(
        name="read",
        read_only=True,
        capabilities=(Capability.FS_READ,),
        result=ToolResult.success("read"),
    )
    registry = ToolRegistry()
    registry.register(read)
    read_executor = ToolExecutor(
        registry,
        RecordingPolicy(),
        action_store=CorruptStore(),
        logger=RecordingLogger(),
    )
    assert read_executor.invoke("read", {"payload": "safe"}).ok


def test_authority_receipt_and_policy_fingerprint_are_deterministically_bound():
    policy = PermissionPolicy(
        PermissionConfig(
            level=PermissionLevel.AUTONOMOUS,
            capability_overrides={"fs.write": PermissionLevel.CONFIRM},
            blocked_capabilities=("net.write",),
        )
    )
    executor, store, _, _ = _build_executor(policy=policy)
    invocation = _invoke(executor, context=_context())
    record = store.load(invocation.action_id)
    receipt = record.authority

    assert receipt.scope_ref == f"live:{invocation.invocation_id}"
    assert receipt.approval_request_id is None
    assert receipt.expires_at is None
    assert receipt.policy_fingerprint == permission_policy_fingerprint(policy)
    assert receipt.binding_digest == authority_binding_digest(
        action_kind=record.action_kind,
        intent_digest=record.intent_digest,
        mutation_capabilities=record.mutation_capabilities,
        tool_name=record.tool_name,
        tool_version=record.tool_version,
        invocation_id=invocation.invocation_id,
        authority_scope=receipt.scope_ref,
        policy_fingerprint=receipt.policy_fingerprint,
    )

    changed = PermissionPolicy(
        PermissionConfig(level=PermissionLevel.OBSERVE),
    )
    assert permission_policy_fingerprint(changed) != receipt.policy_fingerprint


def test_trusted_dedup_context_controls_identity_and_context_meta_does_not():
    tool = EffectTool()
    executor, store, _, _ = _build_executor(tool)
    first = _invoke(executor, context=None)
    second = _invoke(executor, context=None)
    assert first.action_id != second.action_id
    assert tool.runs == 2

    third = _invoke(executor, context=_context())
    fourth = _invoke(executor, context=_context())
    assert third.action_id == fourth.action_id
    assert tool.runs == 3
    assert len(store.list_actions()) == 3


def test_every_journaled_caller_gets_fresh_invocation_identity():
    executor, _, _, _ = _build_executor()
    first = _invoke(executor, context=_context())
    second = _invoke(executor, context=_context())
    assert first.invocation_id.startswith("inv_")
    assert second.invocation_id.startswith("inv_")
    assert first.invocation_id != second.invocation_id
    assert first.action_id == second.action_id


def test_no_duplicate_risk_acknowledgement_is_fabricated():
    executor, store, _, _ = _build_executor()
    invocation = _invoke(executor, context=_context())
    assert store.load(invocation.action_id).duplicate_risk_acknowledgement is None


def test_schema_versions_and_nonjournaled_serialization_are_preserved():
    read = EffectTool(
        name="read",
        read_only=True,
        capabilities=(Capability.FS_READ,),
        result=ToolResult.success("ok"),
    )
    registry = ToolRegistry()
    registry.register(read)
    invocation = ToolExecutor(
        registry,
        RecordingPolicy(),
        logger=RecordingLogger(),
    ).invoke("read", {"payload": "safe"})
    payload = invocation.to_dict()
    assert payload["action_id"] is None
    assert payload["attempt_id"] is None
    assert payload["action_state"] is None
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1


def test_no_time_sleep_or_live_network_in_focused_boundary_tests():
    text = Path(__file__).read_text(encoding="utf-8")
    assert "time" + ".sleep" not in text
    assert "requests" + "." not in text
    assert "httpx" + "." not in text
