from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ActionAdapterRegistry,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionState,
    ActionTransitionProof,
    EffectVerdict,
    InMemoryActionStore,
    NormalizedActionEffect,
    PreparedActionIntent,
)
from agentcore.config.schema import PermissionConfig
from agentcore.errors import ToolError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
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


NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


class DeterministicIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self.lock:
            number = self.counts.get(prefix, 0) + 1
            self.counts[prefix] = number
        return f"{prefix}_{number:012x}"


class MutationTool(Tool):
    def __init__(self) -> None:
        self.version = "1.0.0"
        self.runs = 0
        self.raise_after_effect = False

    @property
    def spec(self):
        return tool_spec(
            "stage5_mutation",
            "Perform one integrated Stage 5 test mutation.",
            version=self.version,
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=(Capability.FS_WRITE,),
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.runs += 1
        if self.raise_after_effect:
            raise TimeoutError("ambiguous test effect")
        return ToolResult.success("raw effect result")


class MutationAdapter:
    def __init__(self, tool: MutationTool) -> None:
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.stage5-mutation.v1",
            eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            reconciliation_capabilities=frozenset({Capability.FS_READ}),
        )

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="a" * 64,
            target_category="test.stage5-target",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The integrated test effect was confirmed.",
        )


def _policy(
    level=PermissionLevel.AUTONOMOUS,
    *,
    blocked=(),
) -> PermissionPolicy:
    return PermissionPolicy(PermissionConfig(
        level=level,
        blocked_capabilities=tuple(blocked),
    ))


def _build():
    clock = FixedClock(NOW)
    store = InMemoryActionStore(clock=clock)
    tool = MutationTool()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    adapters.register(tool, MutationAdapter(tool))
    executor = ToolExecutor(
        registry,
        _policy(),
        action_store=store,
        action_adapters=adapters,
        clock=clock,
        id_factory=DeterministicIds(),
        action_owner_id="process:stage5-test",
    )
    assert executor._mark_action_startup_healthy(store)
    return executor, store, tool


def _context(*, action_id=None, scope="test:stage5", key="step:one"):
    return ActionInvocationContext(
        action_id=action_id,
        deduplication_scope=(scope if action_id is None else None),
        deduplication_key=(key if action_id is None else None),
    )


def test_current_permission_deny_after_claim_blocks_first_physical_seam():
    executor, store, tool = _build()
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            executor.policy = _policy(blocked=(Capability.FS_WRITE,))

    blocked = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(),
        checkpoint=checkpoint,
    )
    record = store.load(blocked.action_id)

    assert calls == 4
    assert blocked.result.metadata["kind"] == "permission_denied"
    assert blocked.action_state == ActionState.IN_FLIGHT.value
    assert record.state is ActionState.IN_FLIGHT
    assert record.attempts[-1].status is ActionAttemptStatus.CLAIMED
    assert tool.runs == 0

    executor.policy = _policy()
    attached = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(),
    )
    assert attached.action_id == record.action_id
    assert attached.result.metadata["kind"] == "action_in_progress"
    assert len(store.load(record.action_id).attempts) == 1
    assert tool.runs == 0


def test_recovery_authorized_retry_rechecks_policy_after_fresh_claim():
    executor, store, tool = _build()
    tool.raise_after_effect = True
    first = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(),
    )
    unresolved = store.load(first.action_id)
    assert unresolved.state is ActionState.RECONCILE_REQUIRED
    assert tool.runs == 1

    retry_ready = unresolved.transition(
        ActionState.AUTHORIZED,
        transition_id="atx_0000000000ff",
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        evidence_refs=("probe:confirmed-absent",),
    )
    store.compare_and_swap(unresolved.action_id, unresolved.version, retry_ready)
    tool.raise_after_effect = False
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            executor.policy = _policy(blocked=(Capability.FS_WRITE,))

    blocked = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(action_id=unresolved.action_id),
        checkpoint=checkpoint,
    )
    after = store.load(unresolved.action_id)

    assert calls == 2
    assert blocked.result.metadata["kind"] == "permission_denied"
    assert after.state is ActionState.IN_FLIGHT
    assert len(after.attempts) == 2
    assert after.attempts[0].attempt_id != after.attempts[1].attempt_id
    assert after.attempts[-1].status is ActionAttemptStatus.CLAIMED
    assert tool.runs == 1


def test_new_approval_requirement_after_claim_blocks_without_historical_grant():
    executor, store, tool = _build()
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            executor.policy = _policy(PermissionLevel.CONFIRM)

    blocked = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(key="step:approval-change"),
        checkpoint=checkpoint,
    )
    record = store.load(blocked.action_id)

    assert blocked.result.metadata["kind"] == "approval_policy_changed"
    assert record.state is ActionState.IN_FLIGHT
    assert record.attempts[-1].status is ActionAttemptStatus.CLAIMED
    assert record.authority.approval_request_id is None
    assert tool.runs == 0


def test_tool_registration_drift_after_claim_blocks_ordinary_effect_seam():
    executor, store, tool = _build()
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            tool.version = "2.0.0"

    blocked = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(key="step:registration"),
        checkpoint=checkpoint,
    )
    record = store.load(blocked.action_id)

    assert blocked.result.metadata["kind"] == "action_adapter_invalid"
    assert record.state is ActionState.IN_FLIGHT
    assert record.attempts[-1].status is ActionAttemptStatus.CLAIMED
    assert tool.runs == 0


def test_tool_registry_replacement_after_claim_blocks_stale_effect_object():
    executor, store, tool = _build()
    replacement = MutationTool()
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            executor.registry.unregister("stage5_mutation")
            executor.registry.register(replacement)

    blocked = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(key="step:registry-replacement"),
        checkpoint=checkpoint,
    )
    record = store.load(blocked.action_id)

    assert blocked.result.metadata["kind"] == "action_adapter_invalid"
    assert record.state is ActionState.IN_FLIGHT
    assert record.attempts[-1].status is ActionAttemptStatus.CLAIMED
    assert tool.runs == 0
    assert replacement.runs == 0


@pytest.mark.parametrize("mutation", ["replace", "unregister"])
def test_registry_binding_cannot_change_after_final_authority_validation(
    monkeypatch,
    mutation,
):
    executor, store, tool = _build()
    replacement = MutationTool()
    validation_passed = threading.Event()
    mutation_finished = threading.Event()
    invocation = []
    invocation_errors = []
    registry_errors = []
    registry_mutated = []
    original_validate = executor._validate_current_execution_authority

    def validate_then_pause(**kwargs):
        decision = original_validate(**kwargs)
        validation_passed.set()
        assert mutation_finished.wait(5), "registry mutation did not finish"
        return decision

    monkeypatch.setattr(
        executor,
        "_validate_current_execution_authority",
        validate_then_pause,
    )

    def invoke():
        try:
            invocation.append(executor.invoke(
                "stage5_mutation",
                {"payload": "private"},
                action_context=_context(key=f"step:lease-{mutation}"),
            ))
        except BaseException as exc:  # pragma: no cover - asserted below
            invocation_errors.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    assert validation_passed.wait(5), "final authority validation did not run"
    try:
        if mutation == "replace":
            executor.registry.register(replacement, replace=True)
        else:
            executor.registry.unregister("stage5_mutation")
        registry_mutated.append(True)
    except ToolError as exc:
        registry_errors.append(exc)
    finally:
        mutation_finished.set()
    worker.join(5)

    assert not worker.is_alive(), "journaled invocation deadlocked"
    assert invocation_errors == []
    assert len(invocation) == 1 and invocation[0].ok
    assert tool.runs == 1 and replacement.runs == 0
    assert not registry_mutated, (
        "stale original tool entered the physical seam after its registry "
        f"binding was {mutation}d"
    )
    assert len(registry_errors) == 1
    assert registry_errors[0].details.get("reason") == (
        "execution_lease_active")
    assert executor.registry.get("stage5_mutation") is tool

    if mutation == "replace":
        assert executor.registry.register(replacement, replace=True)
        assert executor.registry.get("stage5_mutation") is replacement
    else:
        executor.registry.unregister("stage5_mutation")
        assert not executor.registry.has("stage5_mutation")


def test_concurrent_journaled_invocations_share_registry_execution_leases(
    monkeypatch,
):
    executor, _, tool = _build()
    replacement = MutationTool()
    seam_barrier = threading.Barrier(3)
    original_run = tool.run
    invocations = []
    invocation_errors = []

    def concurrent_run(args, context):
        seam_barrier.wait(5)
        seam_barrier.wait(5)
        return original_run(args, context)

    monkeypatch.setattr(tool, "run", concurrent_run)

    def invoke(key):
        try:
            invocations.append(executor.invoke(
                "stage5_mutation",
                {"payload": "private"},
                action_context=_context(key=key),
            ))
        except BaseException as exc:  # pragma: no cover - asserted below
            invocation_errors.append(exc)

    workers = [
        threading.Thread(target=invoke, args=("step:concurrent-one",)),
        threading.Thread(target=invoke, args=("step:concurrent-two",)),
    ]
    for worker in workers:
        worker.start()
    seam_barrier.wait(5)
    try:
        with pytest.raises(ToolError) as blocked:
            executor.registry.register(replacement, replace=True)
        assert blocked.value.details.get("reason") == (
            "execution_lease_active")
        assert executor.registry.get("stage5_mutation") is tool
    finally:
        seam_barrier.wait(5)
    for worker in workers:
        worker.join(5)

    assert all(not worker.is_alive() for worker in workers)
    assert invocation_errors == []
    assert len(invocations) == 2 and all(item.ok for item in invocations)
    assert tool.runs == 2 and replacement.runs == 0
    assert executor.registry.register(replacement, replace=True)


def test_caller_cannot_occupy_internal_duplicate_risk_deduplication_scope():
    executor, store, tool = _build()

    blocked = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(
            scope="agentcore:duplicate-risk:v1",
            key="approval:apr_000000000001",
        ),
    )

    assert blocked.result.metadata["kind"] == (
        "action_deduplication_scope_reserved")
    assert blocked.action_id is None
    assert store.list_actions() == []
    assert tool.runs == 0
