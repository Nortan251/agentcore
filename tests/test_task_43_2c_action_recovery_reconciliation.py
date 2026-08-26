from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION,
    ActionAdapterRegistry,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionOutcome,
    ActionProvenance,
    ActionReconciliationResult,
    ActionReconciliationStatus,
    ActionRecoveryClaim,
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
    new_action,
    new_action_transition_id,
)
from agentcore.action_journal.errors import ActionStoreError
from agentcore.config.schema import AgentConfig, LoggingConfig, PermissionConfig
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.defaults import NullVerifier
from agentcore.core.runner import TaskRunner
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import Decision, Outcome, PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.errors import CancellationRequested
from agentcore.recovery import ActionRecoveryCoordinator, ActionRecoveryStatus
from agentcore.state.models import AgentState
from agentcore.tasks.models import Subtask, Task, TaskStatus
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


NOW = datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
WAIT = 30.0
SECRET = "task-43.2c-secret-sentinel"
RAW_PROBE = "task-43.2c-raw-probe-sentinel"


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self._lock:
            number = self._counts.get(prefix, 0) + 1
            self._counts[prefix] = number
        return f"{prefix}_{number:012x}"


class MatrixPolicy(PermissionPolicy):
    def __init__(self, allowed: Mapping[Capability, bool]) -> None:
        super().__init__(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
        self.allowed = dict(allowed)
        self.calls: list[frozenset[Capability]] = []

    def check(self, capabilities, *, description=""):
        requested = frozenset(capabilities)
        self.calls.append(requested)
        for capability in sorted(requested, key=str):
            if not self.allowed.get(capability, False):
                return Decision(
                    Outcome.DENY,
                    "categorical test denial",
                    capability=capability,
                )
        return Decision(Outcome.ALLOW, "categorical test allow")


class MutationTool(Tool):
    def __init__(self, *, version="1.0.0", run_hook=None) -> None:
        self._version = version
        self._run_hook = run_hook
        self.runs = 0
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec(
            "mutate",
            "Trusted local mutation.",
            version=self._version,
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=(Capability.FS_WRITE,),
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        with self._lock:
            self.runs += 1
        if self._run_hook is not None:
            return self._run_hook(args, context)
        return ToolResult.success("provider raw must not establish truth")


class ReadTool(Tool):
    @property
    def spec(self):
        return tool_spec(
            "read",
            "Independent harmless read.",
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=(Capability.FS_READ,),
            read_only=True,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("read-ok")


class MutationAdapter:
    def __init__(self, tool: MutationTool) -> None:
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.local-mutation.v1",
            eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            reconciliation_capabilities=frozenset({Capability.FS_READ}),
        )
        self.normalized = NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The mutation was confirmed.",
            evidence_refs=("effect:confirmed",),
        )

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="a" * 64,
            target_category="test.target",
        )

    def normalize_effect(self, result):
        return self.normalized


class FakeReconciler:
    def __init__(self, result=None, *, hook=None) -> None:
        self.result = result or ActionReconciliationResult(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="probe.applied",
            summary="The existing effect was confirmed.",
            evidence_refs=("probe:confirmed",),
        )
        self.hook = hook
        self.calls = 0
        self.contexts = []

    def probe(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.hook is not None:
            return self.hook(context)
        return self.result


def _outcome(verdict: EffectVerdict) -> ActionOutcome:
    return ActionOutcome(
        verdict=verdict,
        result_class="test.result",
        summary="Safe bounded action truth.",
        partial_categories=(
            ("first_step",)
            if verdict is EffectVerdict.CONFIRMED_PARTIAL
            else ()
        ),
    )


def _seed(
    store,
    *,
    number: int,
    state: ActionState,
    reconciliation_claim: ReconciliationMetadata | None = None,
):
    record = new_action(
        action_kind="test.local-mutation.v1",
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
        deduplication_scope="workflow:test",
        deduplication_key=f"step:{number}",
        clock=FixedClock(NOW),
        id_factory=lambda prefix: f"{prefix}_{number:012x}",
    )
    record = store.create(record)
    if state is ActionState.AUTHORIZED:
        return record
    claimed = store.claim_attempt(
        record.action_id,
        record.version,
        attempt_id=f"aat_{number:012x}",
        invocation_id=f"inv_{number:012x}",
        owner_id="process:prior",
        owner_epoch=1,
        transition_id=f"atx_{number + 1:012x}",
    )
    if state is ActionState.IN_FLIGHT:
        return claimed
    if state is ActionState.RECONCILE_REQUIRED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 2:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=(
                reconciliation_claim
                or ReconciliationMetadata(
                    disposition=ReconciliationDisposition.PROBE_LATER,
                )
            ),
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
    elif state is ActionState.CANCELLED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 2:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.NOT_INVOKED,
            proof=ActionTransitionProof.LIVE_OWNER_NOT_INVOKED,
        )
    else:  # pragma: no cover - helper misuse
        raise AssertionError(state)
    return store.compare_and_swap(claimed.action_id, claimed.version, candidate)


def _authorize_retry(store, *, number: int = 10):
    unresolved = _seed(
        store,
        number=number,
        state=ActionState.RECONCILE_REQUIRED,
    )
    candidate = unresolved.transition(
        ActionState.AUTHORIZED,
        transition_id=f"atx_{number + 3:012x}",
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        evidence_refs=(f"probe:absent:{number}",),
    )
    return store.compare_and_swap(
        unresolved.action_id,
        unresolved.version,
        candidate,
    )


def _build(
    *,
    store=None,
    policy=None,
    tool=None,
    reconciler=...,
    ids=None,
):
    clock = FixedClock(NOW)
    tool = tool or MutationTool()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    adapter = MutationAdapter(tool)
    if reconciler is ...:
        reconciler = FakeReconciler()
    adapters.register(tool, adapter, reconciler=reconciler)
    resolved_store = store or InMemoryActionStore(clock=clock)
    resolved_policy = policy or MatrixPolicy({
        Capability.FS_READ: True,
        Capability.FS_WRITE: True,
    })
    resolved_ids = ids or DeterministicIds()
    executor = ToolExecutor(
        registry,
        resolved_policy,
        action_store=resolved_store,
        action_adapters=adapters,
        clock=clock,
        id_factory=resolved_ids,
        action_owner_id="process:current",
        action_owner_epoch=2,
    )
    coordinator = ActionRecoveryCoordinator(
        resolved_store,
        executor,
        clock=clock,
        id_factory=resolved_ids,
        owner_id="recovery:current",
    )
    return executor, coordinator, resolved_store, tool, adapter, reconciler


def _claim(coordinator, record) -> ActionRecoveryClaim:
    claim = coordinator.claim(
        record.action_id,
        expected_version=record.version,
    )
    assert isinstance(claim, ActionRecoveryClaim)
    return claim


def _invoke_resume(executor, record, *, checkpoint=None):
    return executor.invoke(
        "mutate",
        {"payload": SECRET},
        checkpoint=checkpoint,
        action_context=ActionInvocationContext(
            action_id=record.action_id,
            deduplication_scope=record.deduplication_scope,
            deduplication_key=record.deduplication_key,
        ),
    )


def test_startup_moves_prior_in_flight_to_reconciliation_without_retry():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    prior = _seed(store, number=1, state=ActionState.IN_FLIGHT)
    _, coordinator, _, tool, _, _ = _build(store=store)

    report = coordinator.recover_startup()

    recovered = store.load(prior.action_id)
    assert report.quarantined is False
    assert report.items[0].status is ActionRecoveryStatus.RECONCILE_REQUIRED
    assert recovered.state is ActionState.RECONCILE_REQUIRED
    assert recovered.attempts[-1].status is ActionAttemptStatus.POSSIBLY_INVOKED
    assert recovered.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert tool.runs == 0


def test_startup_preserves_authorized_no_attempt_retry_proof_and_terminals():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    waiting = _seed(store, number=2, state=ActionState.AUTHORIZED)
    retry = _authorize_retry(store, number=3)
    terminals = [
        _seed(store, number=4, state=ActionState.SUCCEEDED),
        _seed(store, number=5, state=ActionState.FAILED),
        _seed(store, number=6, state=ActionState.CANCELLED),
    ]
    before = {record.action_id: record.to_dict() for record in terminals}
    _, coordinator, _, tool, _, _ = _build(store=store)

    report = coordinator.recover_startup()

    statuses = {item.action_id: item.status for item in report.items}
    assert statuses[waiting.action_id] is ActionRecoveryStatus.WAITING_OWNER
    assert statuses[retry.action_id] is ActionRecoveryStatus.RETRY_READY
    assert store.load(waiting.action_id) == waiting
    assert store.load(retry.action_id) == retry
    assert {record.action_id: store.load(record.action_id).to_dict()
            for record in terminals} == before
    assert tool.runs == 0


def test_startup_fences_a_prior_reconciliation_claim_by_epoch_not_time():
    old = ReconciliationMetadata(
        disposition=ReconciliationDisposition.PROBE_LATER,
        claim_owner_id="recovery:old",
        claim_epoch=7,
        claimed_at=NOW,
        probe_count=2,
    )
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(
        store,
        number=7,
        state=ActionState.RECONCILE_REQUIRED,
        reconciliation_claim=old,
    )
    _, coordinator, _, _, _, _ = _build(store=store)

    coordinator.recover_startup()

    recovered = store.load(record.action_id)
    assert recovered.reconciliation.claim_owner_id is None
    assert recovered.reconciliation.claim_epoch == 8
    assert recovered.reconciliation.probe_count == 2


def test_concurrent_recovery_claim_race_has_one_exact_owner():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=8, state=ActionState.RECONCILE_REQUIRED)
    executor, first, _, _, _, _ = _build(store=store)
    second = ActionRecoveryCoordinator(
        store,
        executor,
        clock=FixedClock(NOW),
        id_factory=DeterministicIds(),
        owner_id="recovery:second",
    )
    barrier = threading.Barrier(3)
    results = []

    def claim(coordinator):
        barrier.wait()
        results.append(coordinator.claim(
            record.action_id,
            expected_version=record.version,
        ))

    threads = [
        threading.Thread(target=claim, args=(first,), daemon=True),
        threading.Thread(target=claim, args=(second,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(WAIT)

    claims = [item for item in results if isinstance(item, ActionRecoveryClaim)]
    assert len(claims) == 1
    loaded = store.load(record.action_id)
    assert loaded.version == claims[0].record_version
    assert loaded.reconciliation.claim_owner_id == claims[0].claim_owner_id
    assert loaded.reconciliation.claim_epoch == claims[0].claim_epoch


def test_probe_uses_current_read_permission_not_historical_mutation_allow():
    policy = MatrixPolicy({
        Capability.FS_READ: True,
        Capability.FS_WRITE: False,
    })
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=9, state=ActionState.RECONCILE_REQUIRED)
    _, coordinator, _, tool, _, reconciler = _build(
        store=store,
        policy=policy,
    )
    claim = _claim(coordinator, record)

    report = coordinator.reconcile(claim)

    assert report.status is ActionReconciliationStatus.SETTLED_SUCCEEDED
    assert reconciler.calls == 1
    assert policy.calls == [frozenset({Capability.FS_READ})]
    assert store.load(record.action_id).state is ActionState.SUCCEEDED
    assert tool.runs == 0


def test_current_probe_denial_does_not_enter_reconciler_or_settle_truth():
    policy = MatrixPolicy({
        Capability.FS_READ: False,
        Capability.FS_WRITE: True,
    })
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=11, state=ActionState.RECONCILE_REQUIRED)
    _, coordinator, _, tool, _, reconciler = _build(
        store=store,
        policy=policy,
    )
    claim = _claim(coordinator, record)

    report = coordinator.reconcile(claim)

    assert report.status is ActionReconciliationStatus.PROBE_DENIED
    assert reconciler.calls == 0
    assert store.load(record.action_id).state is ActionState.RECONCILE_REQUIRED
    assert store.load(record.action_id).reconciliation.claim_owner_id is None
    assert tool.runs == 0


@pytest.mark.parametrize(
    ("result", "expected_status", "expected_state"),
    [
        (
            ActionReconciliationResult(
                verdict=EffectVerdict.CONFIRMED_APPLIED,
                result_class="probe.applied",
                summary="Applied.",
                evidence_refs=("probe:applied",),
            ),
            ActionReconciliationStatus.SETTLED_SUCCEEDED,
            ActionState.SUCCEEDED,
        ),
        (
            ActionReconciliationResult(
                verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
                result_class="probe.absent",
                summary="Absent.",
                evidence_refs=("probe:absent",),
            ),
            ActionReconciliationStatus.SETTLED_FAILED,
            ActionState.FAILED,
        ),
        (
            ActionReconciliationResult(
                verdict=EffectVerdict.CONFIRMED_PARTIAL,
                result_class="probe.partial",
                summary="Partially applied.",
                evidence_refs=("probe:partial",),
                partial_categories=("first_step",),
            ),
            ActionReconciliationStatus.SETTLED_FAILED,
            ActionState.FAILED,
        ),
        (
            ActionReconciliationResult(
                verdict=EffectVerdict.OUTCOME_UNKNOWN,
                result_class="probe.unknown",
                summary="Still unknown.",
                reconciliation_disposition=(
                    ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
                ),
            ),
            ActionReconciliationStatus.UNRESOLVED,
            ActionState.RECONCILE_REQUIRED,
        ),
    ],
)
def test_reconciliation_verdicts_settle_only_legal_categorical_truth(
    result, expected_status, expected_state,
):
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=12, state=ActionState.RECONCILE_REQUIRED)
    reconciler = FakeReconciler(result)
    _, coordinator, _, tool, _, _ = _build(
        store=store,
        reconciler=reconciler,
    )

    report = coordinator.reconcile(_claim(coordinator, record))

    assert report.status is expected_status
    assert store.load(record.action_id).state is expected_state
    assert tool.runs == 0


def test_definitive_no_effect_can_authorize_then_retry_only_through_executor():
    no_effect = ActionReconciliationResult(
        verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        result_class="probe.absent",
        summary="The effect is definitively absent.",
        evidence_refs=("probe:definitive-absence",),
        retry_safe=True,
    )
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=13, state=ActionState.RECONCILE_REQUIRED)
    executor, coordinator, _, tool, _, _ = _build(
        store=store,
        reconciler=FakeReconciler(no_effect),
    )

    reconciled = coordinator.reconcile(_claim(coordinator, record))
    authorized = store.load(record.action_id)
    retried = _invoke_resume(executor, authorized)

    assert reconciled.status is ActionReconciliationStatus.RETRY_AUTHORIZED
    assert authorized.state is ActionState.AUTHORIZED
    assert authorized.transitions[-1].proof is (
        ActionTransitionProof.DEFINITIVE_NO_EFFECT
    )
    assert retried.ok
    assert retried.action_id == record.action_id
    assert tool.runs == 1
    final = store.load(record.action_id)
    assert final.state is ActionState.SUCCEEDED
    assert len(final.attempts) == 2
    assert final.attempts[0].attempt_id != final.attempts[1].attempt_id
    assert final.attempts[0].invocation_id != final.attempts[1].invocation_id


def test_old_allow_and_current_mutation_deny_blocks_proven_safe_retry():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    authorized = _authorize_retry(store, number=14)
    policy = MatrixPolicy({
        Capability.FS_READ: True,
        Capability.FS_WRITE: False,
    })
    executor, _, _, tool, _, _ = _build(store=store, policy=policy)

    invocation = _invoke_resume(executor, authorized)

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "permission_denied"
    assert policy.calls == [frozenset({Capability.FS_WRITE})]
    assert store.load(authorized.action_id) == authorized
    assert tool.runs == 0


def test_authorized_no_attempt_still_requires_current_mutation_permission():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    waiting = _seed(store, number=46, state=ActionState.AUTHORIZED)
    policy = MatrixPolicy({
        Capability.FS_READ: True,
        Capability.FS_WRITE: False,
    })
    executor, _, _, tool, _, _ = _build(store=store, policy=policy)

    invocation = _invoke_resume(executor, waiting)

    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "permission_denied"
    assert policy.calls == [frozenset({Capability.FS_WRITE})]
    assert tool.runs == 0
    assert store.load(waiting.action_id) == waiting


@pytest.mark.parametrize("checkpoint_number", [1, 2, 3, 4])
def test_cancellation_at_every_pre_retry_checkpoint_prevents_physical_call(
    checkpoint_number,
):
    store = InMemoryActionStore(clock=FixedClock(NOW))
    authorized = _authorize_retry(store, number=20 + checkpoint_number)
    executor, _, _, tool, _, _ = _build(store=store)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == checkpoint_number:
            raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    with pytest.raises(CancellationRequested):
        _invoke_resume(executor, authorized, checkpoint=checkpoint)

    assert tool.runs == 0
    loaded = store.load(authorized.action_id)
    assert loaded.state in {ActionState.AUTHORIZED, ActionState.CANCELLED}
    if loaded.attempts[-1].attempt_id != authorized.attempts[-1].attempt_id:
        assert loaded.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED


def test_duplicate_concurrent_retry_callers_claim_one_fresh_attempt():
    entered = threading.Event()
    release = threading.Event()

    def run(args, context):
        entered.set()
        assert release.wait(WAIT)
        return ToolResult.success("raw")

    tool = MutationTool(run_hook=run)
    store = InMemoryActionStore(clock=FixedClock(NOW))
    authorized = _authorize_retry(store, number=30)
    executor, _, _, _, _, _ = _build(store=store, tool=tool)
    barrier = threading.Barrier(3)
    results = []

    def caller():
        barrier.wait()
        results.append(_invoke_resume(executor, authorized))

    threads = [threading.Thread(target=caller, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    assert entered.wait(WAIT)
    assert tool.runs == 1
    release.set()
    for thread in threads:
        thread.join(WAIT)

    assert len(results) == 2
    assert all(result.ok for result in results)
    assert {result.action_id for result in results} == {authorized.action_id}
    assert tool.runs == 1
    assert len(store.load(authorized.action_id).attempts) == 2


def test_stale_probe_result_cannot_publish_after_claim_epoch_changes():
    entered = threading.Event()
    release = threading.Event()

    def probe(context):
        entered.set()
        assert release.wait(WAIT)
        return ActionReconciliationResult(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="probe.applied",
            summary="Applied.",
            evidence_refs=("probe:late",),
        )

    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=31, state=ActionState.RECONCILE_REQUIRED)
    _, coordinator, _, _, _, _ = _build(
        store=store,
        reconciler=FakeReconciler(hook=probe),
    )
    first_claim = _claim(coordinator, record)
    reports = []
    worker = threading.Thread(
        target=lambda: reports.append(coordinator.reconcile(first_claim)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(WAIT)

    claimed = store.load(record.action_id)
    fenced = claimed.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id="atx_000000000200",
        at=NOW,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=ReconciliationMetadata(
            disposition=ReconciliationDisposition.PROBE_LATER,
            claim_epoch=claimed.reconciliation.claim_epoch + 1,
            probe_count=claimed.reconciliation.probe_count,
        ),
    )
    store.compare_and_swap(claimed.action_id, claimed.version, fenced)
    next_claim = _claim(coordinator, fenced)
    release.set()
    worker.join(WAIT)

    loaded = store.load(record.action_id)
    assert reports[0].status is ActionReconciliationStatus.STALE_CLAIM
    assert loaded.state is ActionState.RECONCILE_REQUIRED
    assert loaded.reconciliation.claim_epoch == next_claim.claim_epoch
    assert loaded.reconciliation.claim_owner_id == next_claim.claim_owner_id
    assert "probe:late" not in loaded.evidence_refs


def test_stale_reconciliation_claim_never_becomes_retry_authority():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=47, state=ActionState.RECONCILE_REQUIRED)
    executor, coordinator, _, tool, _, reconciler = _build(store=store)
    claim = _claim(coordinator, record)
    claimed = store.load(record.action_id)
    fenced = claimed.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id="atx_0000000000ff",
        at=NOW,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=ReconciliationMetadata(
            disposition=ReconciliationDisposition.PROBE_LATER,
            claim_epoch=claim.claim_epoch + 1,
            probe_count=claimed.reconciliation.probe_count,
        ),
    )
    store.compare_and_swap(claimed.action_id, claimed.version, fenced)

    report = coordinator.reconcile(claim)
    invocation = _invoke_resume(executor, fenced)

    assert report.status is ActionReconciliationStatus.STALE_CLAIM
    assert not invocation.ok
    assert invocation.action_state == ActionState.RECONCILE_REQUIRED.value
    assert reconciler.calls == 0
    assert tool.runs == 0


class StoreWrapper:
    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class ResultCasThenReloadFails(StoreWrapper):
    def __init__(self, wrapped) -> None:
        super().__init__(wrapped)
        self.fail_next_load = False

    def compare_and_swap(self, action_id, expected_version, next_record):
        result = self.wrapped.compare_and_swap(
            action_id,
            expected_version,
            next_record,
        )
        if next_record.state is not ActionState.RECONCILE_REQUIRED:
            self.fail_next_load = True
        return result

    def load(self, action_id):
        if self.fail_next_load:
            self.fail_next_load = False
            raise ActionStoreError("reload failed with secret", raw=SECRET)
        return self.wrapped.load(action_id)


class ResultCasFails(StoreWrapper):
    def compare_and_swap(self, action_id, expected_version, next_record):
        if next_record.state is not ActionState.RECONCILE_REQUIRED:
            raise ActionStoreError("result CAS failed", raw=SECRET)
        return self.wrapped.compare_and_swap(
            action_id,
            expected_version,
            next_record,
        )


@pytest.mark.parametrize(
    ("wrapper", "durable_state"),
    [
        (ResultCasThenReloadFails, ActionState.SUCCEEDED),
        (ResultCasFails, ActionState.RECONCILE_REQUIRED),
    ],
)
def test_reconciliation_result_publication_faults_never_repeat_probe(
    wrapper, durable_state,
):
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(underlying, number=32, state=ActionState.RECONCILE_REQUIRED)
    store = wrapper(underlying)
    _, coordinator, _, _, _, reconciler = _build(store=store)

    report = coordinator.reconcile(_claim(coordinator, record))

    assert report.status is ActionReconciliationStatus.PUBLICATION_UNCERTAIN
    assert reconciler.calls == 1
    assert underlying.load(record.action_id).state is durable_state
    assert SECRET not in repr(report)


@pytest.mark.parametrize("failure", [RuntimeError(SECRET), {"raw": RAW_PROBE}])
def test_probe_exception_or_malformed_result_stays_unresolved_without_raw(failure):
    def probe(context):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=33, state=ActionState.RECONCILE_REQUIRED)
    _, coordinator, _, _, _, reconciler = _build(
        store=store,
        reconciler=FakeReconciler(hook=probe),
    )

    report = coordinator.reconcile(_claim(coordinator, record))

    assert report.status is ActionReconciliationStatus.PROBE_FAILED
    loaded = store.load(record.action_id)
    assert loaded.state is ActionState.RECONCILE_REQUIRED
    assert loaded.reconciliation.claim_owner_id is None
    assert reconciler.calls == 1
    assert SECRET not in repr(report)
    assert RAW_PROBE not in repr(report)
    assert SECRET not in str(loaded.to_dict())
    assert RAW_PROBE not in str(loaded.to_dict())


def test_probe_cancellation_cannot_fabricate_terminal_truth():
    def probe(context):
        raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = _seed(store, number=34, state=ActionState.RECONCILE_REQUIRED)
    _, coordinator, _, _, _, reconciler = _build(
        store=store,
        reconciler=FakeReconciler(hook=probe),
    )
    claim = _claim(coordinator, record)

    with pytest.raises(CancellationRequested):
        coordinator.reconcile(claim)

    loaded = store.load(record.action_id)
    assert loaded.state is ActionState.RECONCILE_REQUIRED
    assert loaded.terminal_outcome is None
    assert loaded.reconciliation.claim_owner_id is None
    assert reconciler.calls == 1


def test_registry_version_mismatch_and_missing_reconciler_fail_closed():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    mismatched = _seed(store, number=35, state=ActionState.RECONCILE_REQUIRED)
    wrong_tool = MutationTool(version="2.0.0")
    _, wrong_coordinator, _, _, _, wrong_reconciler = _build(
        store=store,
        tool=wrong_tool,
    )
    mismatch_report = wrong_coordinator.reconcile(
        _claim(wrong_coordinator, mismatched)
    )
    assert mismatch_report.status is (
        ActionReconciliationStatus.ADAPTER_UNAVAILABLE
    )
    assert wrong_reconciler.calls == 0
    assert store.load(mismatched.action_id).state is ActionState.RECONCILE_REQUIRED

    second_store = InMemoryActionStore(clock=FixedClock(NOW))
    missing = _seed(second_store, number=36, state=ActionState.RECONCILE_REQUIRED)
    _, missing_coordinator, _, _, _, _ = _build(
        store=second_store,
        reconciler=None,
    )
    missing_report = missing_coordinator.reconcile(
        _claim(missing_coordinator, missing)
    )
    assert missing_report.status is (
        ActionReconciliationStatus.ADAPTER_UNAVAILABLE
    )
    assert second_store.load(missing.action_id).state is (
        ActionState.RECONCILE_REQUIRED
    )


class ClaimRaisesAfterCommit(StoreWrapper):
    def claim_attempt(self, *args, **kwargs):
        self.wrapped.claim_attempt(*args, **kwargs)
        raise ActionStoreError("claim acknowledgement lost")


class ClaimNeverCommits(StoreWrapper):
    def claim_attempt(self, *args, **kwargs):
        raise ActionStoreError("claim failed")


@pytest.mark.parametrize(
    ("wrapper", "runs"),
    [(ClaimRaisesAfterCommit, 1), (ClaimNeverCommits, 0)],
)
def test_safe_retry_claim_faults_require_exact_durable_proof(wrapper, runs):
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    authorized = _authorize_retry(underlying, number=37)
    store = wrapper(underlying)
    executor, _, _, tool, _, _ = _build(store=store)

    invocation = _invoke_resume(executor, authorized)

    assert tool.runs == runs
    if runs:
        assert invocation.ok
        assert underlying.load(authorized.action_id).state is ActionState.SUCCEEDED
    else:
        assert not invocation.ok
        assert invocation.result.metadata["kind"] == "action_claim_unverified"
        assert underlying.load(authorized.action_id) == authorized


def test_corrupt_recovery_scan_quarantines_mutation_but_read_path_survives(
    tmp_path,
):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    record = _seed(store, number=38, state=ActionState.AUTHORIZED)
    payload_path = tmp_path / f"{record.action_id}.json"
    payload_path.write_text("{\"schema_version\":999}", encoding="utf-8")
    executor, coordinator, _, tool, _, _ = _build(store=store)

    report = coordinator.recover_startup()
    mutation = executor.invoke("mutate", {"payload": SECRET})

    assert report.quarantined
    assert report.error_category == "journal_corrupt"
    assert not mutation.ok
    assert tool.runs == 0

    read_registry = ToolRegistry()
    read_registry.register(ReadTool())
    read_executor = ToolExecutor(
        read_registry,
        MatrixPolicy({Capability.FS_READ: True}),
        action_store=store,
    )
    assert read_executor.invoke("read", {"payload": "safe"}).ok
    store.close()


def test_agent_classifies_in_flight_actions_before_runner_is_available(tmp_path):
    store = InMemoryActionStore(clock=FixedClock(NOW))
    prior = _seed(store, number=48, state=ActionState.IN_FLIGHT)
    tool = MutationTool()
    adapters = ActionAdapterRegistry()
    adapters.register(
        tool,
        MutationAdapter(tool),
        reconciler=FakeReconciler(),
    )
    config = AgentConfig(
        workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
    ).validate()

    agent = Agent(
        config,
        AgentDependencies(
            tools=[tool],
            action_store=store,
            action_adapters=adapters,
        ),
    )

    assert agent.action_recovery is not None
    assert agent.action_recovery_startup is not None
    assert not agent.action_recovery_startup.quarantined
    assert store.load(prior.action_id).state is ActionState.RECONCILE_REQUIRED
    assert tool.runs == 0
    agent.shutdown()


def test_probe_holds_no_general_store_mutex_and_other_action_can_retry():
    entered = threading.Event()
    release = threading.Event()

    def probe(context):
        entered.set()
        assert release.wait(WAIT)
        return ActionReconciliationResult(
            verdict=EffectVerdict.OUTCOME_UNKNOWN,
            result_class="probe.unknown",
            summary="The effect remains unknown.",
            reconciliation_disposition=ReconciliationDisposition.PROBE_LATER,
        )

    store = InMemoryActionStore(clock=FixedClock(NOW))
    unresolved = _seed(
        store,
        number=49,
        state=ActionState.RECONCILE_REQUIRED,
    )
    retry_ready = _authorize_retry(store, number=50)
    executor, coordinator, _, tool, _, _ = _build(
        store=store,
        reconciler=FakeReconciler(hook=probe),
    )
    claim = _claim(coordinator, unresolved)
    reports = []
    probe_worker = threading.Thread(
        target=lambda: reports.append(coordinator.reconcile(claim)),
        daemon=True,
    )
    probe_worker.start()
    assert entered.wait(WAIT)

    store_read = threading.Event()
    reader = threading.Thread(
        target=lambda: (store.list_nonterminal(), store_read.set()),
        daemon=True,
    )
    retry_results = []
    retry_done = threading.Event()

    def retry_other_action():
        retry_results.append(_invoke_resume(executor, retry_ready))
        retry_done.set()

    retry_worker = threading.Thread(target=retry_other_action, daemon=True)
    reader.start()
    retry_worker.start()
    try:
        assert store_read.wait(WAIT), "probe held the ActionStore mutex"
        assert retry_done.wait(WAIT), "probe serialized a separate action"
    finally:
        release.set()
    reader.join(WAIT)
    retry_worker.join(WAIT)
    probe_worker.join(WAIT)

    assert retry_results[0].ok
    assert tool.runs == 1
    assert reports[0].status is ActionReconciliationStatus.UNRESOLVED


def test_taskrunner_persists_safe_action_binding_across_replanned_task_identity():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    executor, _, _, tool, _, _ = _build(store=store)
    runner = TaskRunner(executor, verifier=NullVerifier())
    state = AgentState()
    workflow_step = "sub_000000000abc"
    first = Task(
        objective="first plan",
        subtasks=[Subtask(
            id=workflow_step,
            description="perform the trusted workflow step",
            tool_name="mutate",
            tool_args={"payload": SECRET},
        )],
    )

    first_report = runner.run(first, state)
    action_id = state.tool_calls[-1]["action_id"]
    second = Task(
        objective="replacement plan",
        subtasks=[Subtask(
            id=workflow_step,
            description="carry forward the same trusted workflow step",
            tool_name="mutate",
            tool_args={"payload": SECRET},
        )],
    )
    second_report = runner.run(second, state)

    assert first_report.status is TaskStatus.COMPLETED
    assert second_report.status is TaskStatus.COMPLETED
    assert tool.runs == 1
    assert state.tool_calls[-1]["action_id"] == action_id
    assert len(store.list_actions()) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX fork semantics")
def test_fresh_process_recovers_prior_in_flight_without_invocation(tmp_path):
    if not hasattr(os, "fork"):
        pytest.skip("fork unavailable")
    parent_store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    prior = _seed(parent_store, number=51, state=ActionState.IN_FLIGHT)
    action_id = prior.action_id
    parent_store.close()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child assertion reported through pipe
        os.close(read_fd)
        child_store = None
        try:
            child_store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
            _, coordinator, _, tool, _, _ = _build(store=child_store)
            report = coordinator.recover_startup()
            recovered = child_store.load(action_id)
            payload = (
                f"{report.quarantined}:"
                f"{recovered.state.value}:"
                f"{recovered.attempts[-1].status.value}:"
                f"{tool.runs}"
            ).encode("ascii")
        except Exception as exc:
            payload = f"ERROR:{type(exc).__name__}".encode("ascii")
        finally:
            if child_store is not None:
                child_store.close()
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    payload = os.read(read_fd, 256)
    os.close(read_fd)
    os.waitpid(pid, 0)

    reopened = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    try:
        recovered = reopened.load(action_id)
        assert payload == b"False:reconcile_required:possibly_invoked:0"
        assert recovered.state is ActionState.RECONCILE_REQUIRED
    finally:
        reopened.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fork semantics")
def test_forked_child_rejects_inherited_recovery_coordinator(tmp_path):
    if not hasattr(os, "fork"):
        pytest.skip("fork unavailable")
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    _seed(store, number=39, state=ActionState.RECONCILE_REQUIRED)
    _, coordinator, _, _, _, _ = _build(store=store)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child assertion reported through pipe
        os.close(read_fd)
        try:
            coordinator.recover_startup()
        except Exception as exc:
            os.write(write_fd, type(exc).__name__.encode("ascii"))
        else:
            os.write(write_fd, b"NO_ERROR")
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    payload = os.read(read_fd, 128)
    os.close(read_fd)
    os.waitpid(pid, 0)
    store.close()
    assert payload == b"ActionStoreOwnershipError"


def test_schema_versions_privacy_and_determinism_remain_frozen():
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1
    text = Path(__file__).read_text(encoding="utf-8")
    assert "time" + ".sleep" not in text
    assert "requests" + "." not in text
    assert "httpx" + "." not in text
