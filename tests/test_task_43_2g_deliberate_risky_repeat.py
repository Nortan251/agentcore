from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION,
    PROTECTED_STORAGE_SCHEMA_VERSION,
    ActionAdapterRegistry,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionExecutionPermit,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionOutcome,
    ActionProvenance,
    ActionState,
    ActionTransitionProof,
    AuthorityReceipt,
    DuplicateRiskAcknowledgement,
    DuplicateRiskInvocationContext,
    EffectVerdict,
    IdempotencyContract,
    InMemoryActionStore,
    InMemoryProtectedActionPayloadStore,
    JsonFileActionStore,
    NormalizedActionEffect,
    PreparedActionIntent,
    ReconciliationDisposition,
    ReconciliationMetadata,
    StaticInstallationKeyProvider,
    new_action,
)
from agentcore.action_journal.errors import ActionStoreError, ActionValidationError
from agentcore.approvals import (
    APPROVAL_SCHEMA_VERSION,
    ApprovalInvocationContext,
    ApprovalState,
    DurableApprovalAuthority,
    HumanApprovalDecision,
    InMemoryApprovalStore,
    JsonFileApprovalStore,
    ApprovalUnavailableError,
)
from agentcore.config.schema import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.errors import CancellationRequested
from agentcore.recovery.action import ActionRecoveryCoordinator
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


NOW = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
SECRET = "task-43.2g-private-recipient@example.invalid"
KEY = StaticInstallationKeyProvider(b"r" * 32)


class DeterministicIds:
    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self._lock:
            value = self._counts.get(prefix, 0) + 1
            self._counts[prefix] = value
        return f"{prefix}_{value:012x}"


class RecordingLogger:
    def __init__(self, events=None, context=None) -> None:
        self.events = events if events is not None else []
        self.context = dict(context or {})

    def bind(self, **context):
        return RecordingLogger(self.events, {**self.context, **context})

    def _record(self, event, fields):
        self.events.append((event, {**self.context, **fields}))

    def debug(self, event, **fields): self._record(event, fields)
    def info(self, event, **fields): self._record(event, fields)
    def warning(self, event, **fields): self._record(event, fields)
    def error(self, event, **fields): self._record(event, fields)
    def exception(self, event, **fields): self._record(event, fields)


class DecisionGate:
    def __init__(self, decision=HumanApprovalDecision.PENDING, *, hook=None):
        self.decision = decision
        self.hook = hook
        self.prompts = []

    def decide(self, prompt):
        self.prompts.append(prompt)
        if self.hook is not None:
            self.hook(prompt)
        return self.decision


class RiskyTool(Tool):
    def __init__(self, *, lose_first_response=True, run_hook=None) -> None:
        self.lose_first_response = lose_first_response
        self.run_hook = run_hook
        self.runs = 0
        self.remote_effects = 0
        self.keys: list[str | None] = []
        self._lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec(
            "risky_mutation",
            "Perform one exact fake mutation.",
            version="1.0.0",
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=(Capability.FS_WRITE,),
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        if self.run_hook is not None:
            self.run_hook()
        permit = context.action_execution_permit
        key = permit.idempotency.key if permit is not None else None
        with self._lock:
            self.runs += 1
            self.keys.append(key)
            self.remote_effects += 1
            if self.lose_first_response:
                self.lose_first_response = False
                raise TimeoutError("lost fake response")
        return ToolResult.success({"effect": self.remote_effects})


class RiskyAdapter:
    def __init__(
        self,
        tool: RiskyTool,
        *,
        effect_class=ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS,
        intent_digest="a" * 64,
        protected=False,
        target_fingerprint="b" * 64,
    ) -> None:
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.risky-mutation.v1",
            eligibility=(
                ActionJournalEligibility.EXTERNAL_ACCOUNT_MUTATION
                if effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT
                else ActionJournalEligibility.LONG_RUNNING_OR_AMBIGUOUS_EFFECT
            ),
            effect_class=effect_class,
        )
        self.intent_digest = intent_digest
        self.protected = protected
        self.target_fingerprint = target_fingerprint

    def prepare_intent(self, cleaned, context):
        if self.protected:
            return PreparedActionIntent(
                intent_digest=self.intent_digest,
                target_category="test.private-target",
                target_fingerprint=self.target_fingerprint,
                approval_description="Approve one private fake mutation.",
                requires_protected_payload=True,
                protected_payload={"recipient": cleaned["payload"]},
            )
        return PreparedActionIntent(
            intent_digest=self.intent_digest,
            target_category="test.target",
            target_fingerprint=self.target_fingerprint,
            approval_description="Approve one fake mutation.",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The new logical effect was confirmed.",
        )


class FakeIdempotencyProvider:
    allows_indefinite_retention = False

    def __init__(self, clock: FixedClock, *, horizon=60) -> None:
        self.clock = clock
        self.horizon = horizon
        self.establish_hook = None
        self.validate_hook = None
        self.establish_calls = 0
        self.validate_calls = 0
        self.execute_calls = 0
        self.available = True
        self.remote: dict[str, ToolResult] = {}

    def establish_contract(self, request):
        self.establish_calls += 1
        if not self.available:
            raise RuntimeError("provider unavailable")
        contract = IdempotencyContract(
            key=f"idem_{request.action_id.removeprefix('act_')}",
            contract="fake.risky.v1",
            scope="fake:account:primary",
            valid_until=self.clock.now() + timedelta(seconds=self.horizon),
        )
        if self.establish_hook is not None:
            self.establish_hook()
        return contract

    def validate_contract(self, request, stored_contract):
        self.validate_calls += 1
        if not self.available:
            raise RuntimeError("provider unavailable")
        if self.validate_hook is not None:
            self.validate_hook()
        return stored_contract

    def execute_if_current(self, request, tool, cleaned, context, permit):
        self.execute_calls += 1
        current = self.validate_contract(request, permit.idempotency)
        if current != permit.idempotency:
            raise ValueError("durable provider contract changed")
        if current.valid_until is not None and self.clock.now() >= (
                current.valid_until):
            raise ValueError("provider guarantee expired")
        previous = self.remote.get(current.key)
        if previous is not None:
            return previous
        try:
            result = tool.run(cleaned, context)
        except TimeoutError:
            self.remote[current.key] = ToolResult.success(
                {"effect": tool.remote_effects})
            raise
        self.remote[current.key] = result
        return result


class ResultPublicationFailingStore(InMemoryActionStore):
    def __init__(self, *, clock) -> None:
        super().__init__(clock=clock)
        self.fail_action_id = None

    def compare_and_swap(self, action_id, expected_version, next_record):
        if action_id == self.fail_action_id and next_record.state is (
                ActionState.SUCCEEDED):
            raise ActionStoreError("injected result publication failure")
        return super().compare_and_swap(
            action_id, expected_version, next_record)


def _policy(level=PermissionLevel.AUTONOMOUS, *, blocked=()):
    return PermissionPolicy(PermissionConfig(
        level=level,
        blocked_capabilities=tuple(blocked),
    ))


def _build(
    *,
    effect_class=ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS,
    gate=None,
    policy=None,
    clock=None,
    ids=None,
    store=None,
    tool=None,
    adapter=None,
    provider=None,
    approval_store=None,
    payload_store=None,
    logger=None,
    ttl=timedelta(minutes=5),
):
    clock = clock or FixedClock(NOW)
    ids = ids or DeterministicIds()
    gate = gate or DecisionGate()
    tool = tool or RiskyTool()
    adapter = adapter or RiskyAdapter(tool, effect_class=effect_class)
    store = store or InMemoryActionStore(clock=clock)
    approval_store = approval_store or InMemoryApprovalStore()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    if effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT:
        provider = provider or FakeIdempotencyProvider(clock)
        adapters.register(tool, adapter, idempotency_provider=provider)
    else:
        adapters.register(tool, adapter)
    authority = DurableApprovalAuthority(
        approval_store,
        KEY,
        principal_ref="principal:local-owner",
        scope_ref="workspace:test",
        gate=gate,
        ttl=ttl,
        clock=clock,
        id_factory=ids,
    )
    logger = logger or RecordingLogger()
    executor = ToolExecutor(
        registry,
        policy or _policy(),
        action_store=store,
        action_adapters=adapters,
        protected_payload_store=payload_store,
        approval_authority=authority,
        clock=clock,
        id_factory=ids,
        action_owner_id="process:test",
        logger=logger,
    )
    assert executor._mark_action_startup_healthy(store)
    return {
        "executor": executor,
        "store": store,
        "tool": tool,
        "adapter": adapter,
        "provider": provider,
        "authority": authority,
        "approval_store": approval_store,
        "gate": gate,
        "clock": clock,
        "ids": ids,
        "logger": logger,
    }


def _invoke(
    runtime,
    *,
    action_context=None,
    risk_context=None,
    approval_request_id=None,
    payload=SECRET,
    checkpoint=None,
    extra_args=None,
):
    args = {"payload": payload}
    args.update(extra_args or {})
    return runtime["executor"].invoke(
        "risky_mutation",
        args,
        action_context=action_context,
        duplicate_risk_context=risk_context,
        approval_context=(
            ApprovalInvocationContext(approval_request_id)
            if approval_request_id is not None else None
        ),
        checkpoint=checkpoint,
        untrusted_metadata=SECRET,
    )


def _ambiguous(runtime=None):
    runtime = runtime or _build()
    first = _invoke(
        runtime,
        action_context=ActionInvocationContext(
            deduplication_scope="test:workflow",
            deduplication_key="step:a",
        ),
    )
    record = runtime["store"].load(first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    return runtime, first, record


def _risk_context(predecessor, *, successor="act_eeeeeeeeeeee", request=None):
    return DuplicateRiskInvocationContext(
        predecessor_action_id=predecessor.action_id,
        successor_action_id=successor,
        approval_request_id=request,
    )


def _approve_risk(runtime, predecessor, *, successor="act_eeeeeeeeeeee"):
    pending = _invoke(
        runtime,
        risk_context=_risk_context(predecessor, successor=successor),
    )
    assert pending.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_required")
    runtime["authority"].record_decision(
        pending.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )
    context = _risk_context(
        predecessor,
        successor=successor,
        request=pending.approval_request_id,
    )
    return pending, context


def _outcome(verdict: EffectVerdict) -> ActionOutcome:
    return ActionOutcome(
        verdict=verdict,
        result_class="test.settled",
        summary="The predecessor truth was authoritatively settled.",
    )


def _settle_predecessor(runtime, predecessor, state: ActionState):
    current = runtime["store"].load(predecessor.action_id)
    if state is ActionState.SUCCEEDED:
        verdict = EffectVerdict.CONFIRMED_APPLIED
    elif state is ActionState.FAILED:
        verdict = EffectVerdict.CONFIRMED_NOT_APPLIED
    elif state is ActionState.AUTHORIZED:
        verdict = EffectVerdict.CONFIRMED_NOT_APPLIED
    else:
        raise AssertionError("unsupported settlement state")
    candidate = current.transition(
        state,
        transition_id=runtime["ids"]("atx"),
        at=runtime["clock"].now(),
        effect_verdict=verdict,
        proof=(
            ActionTransitionProof.DEFINITIVE_NO_EFFECT
            if state is ActionState.AUTHORIZED
            else ActionTransitionProof.NONE
        ),
        terminal_outcome=(
            _outcome(verdict)
            if state in {ActionState.SUCCEEDED, ActionState.FAILED}
            else None
        ),
    )
    return runtime["store"].compare_and_swap(
        current.action_id, current.version, candidate)


def _fresh_record(
    number: int,
    *,
    state: ActionState = ActionState.AUTHORIZED,
    predecessor_action_id=None,
    acknowledgement=None,
):
    action_id = f"act_{number:012x}"
    record = new_action(
        action_id=action_id,
        action_kind="test.risky-mutation.v1",
        tool_name="risky_mutation",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.LONG_RUNNING_OR_AMBIGUOUS_EFFECT,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=(),
        effect_class=(
            ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS),
        intent_digest="a" * 64,
        intent_ref=None,
        target_category="test.target",
        target_fingerprint="b" * 64,
        provenance=ActionProvenance(
            invocation_id=f"inv_{number:012x}"),
        authority=AuthorityReceipt(
            receipt_id=f"auth_{number:012x}",
            binding_digest=f"{number:064x}",
            policy_fingerprint=f"{number + 1:064x}",
            scope_ref=f"scope:test:{number}",
            authorized_at=NOW,
        ),
        predecessor_action_id=predecessor_action_id,
        duplicate_risk_acknowledgement=acknowledgement,
        clock=FixedClock(NOW),
    )
    if state is ActionState.CANCELLED:
        return record.transition(
            ActionState.CANCELLED,
            transition_id=f"atx_{number:012x}",
            at=NOW,
        )
    if state is ActionState.FAILED:
        verdict = EffectVerdict.CONFIRMED_NOT_APPLIED
        return record.transition(
            ActionState.FAILED,
            transition_id=f"atx_{number:012x}",
            at=NOW,
            effect_verdict=verdict,
            terminal_outcome=_outcome(verdict),
        )
    return record


def test_canonical_ambiguous_effect_then_acknowledged_new_action_runs_twice():
    runtime, first, predecessor = _ambiguous()
    predecessor_before = predecessor.to_dict()
    pending, context = _approve_risk(runtime, predecessor)

    successor_call = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(successor_call.action_id)

    assert successor_call.ok
    assert successor.action_id != predecessor.action_id
    assert successor.predecessor_action_id == predecessor.action_id
    assert isinstance(
        successor.duplicate_risk_acknowledgement,
        DuplicateRiskAcknowledgement,
    )
    assert successor.duplicate_risk_acknowledgement.approval_request_id == (
        pending.approval_request_id)
    assert successor.state is ActionState.SUCCEEDED
    assert runtime["tool"].remote_effects == 2
    assert runtime["store"].load(predecessor.action_id).to_dict() == (
        predecessor_before)
    assert first.action_id != successor_call.action_id


def test_valid_risk_acknowledgement_cannot_bypass_journal_quarantine():
    runtime, _, predecessor = _ambiguous()
    pending, context = _approve_risk(runtime, predecessor)
    before = predecessor.to_dict()
    runtime["executor"].quarantine_action_mutations("journal_corrupt")

    blocked = _invoke(runtime, risk_context=context)

    assert blocked.result.metadata["kind"] == "action_journal_quarantined"
    assert runtime["approval_store"].load(
        pending.approval_request_id).state is ApprovalState.APPROVED
    assert runtime["store"].load(predecessor.action_id).to_dict() == before
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_ambiguous_predecessor_without_trusted_risk_context_does_not_repeat():
    runtime, _, predecessor = _ambiguous()
    attached = _invoke(
        runtime,
        action_context=ActionInvocationContext(action_id=predecessor.action_id),
    )
    assert not attached.ok
    assert attached.action_id == predecessor.action_id
    assert runtime["tool"].remote_effects == 1
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_model_repeat_anyway_argument_cannot_trigger_risky_repeat():
    runtime, _, predecessor = _ambiguous()
    result = _invoke(runtime, extra_args={"repeat_anyway": True})
    assert result.result.metadata["kind"] == "validation"
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_model_predecessor_argument_cannot_create_linkage():
    runtime, _, predecessor = _ambiguous()
    result = _invoke(
        runtime,
        extra_args={"predecessor_action_id": predecessor.action_id},
    )
    assert result.result.metadata["kind"] == "validation"
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_pending_risk_approval_creates_no_successor_or_effect():
    runtime, _, predecessor = _ambiguous()
    pending = _invoke(runtime, risk_context=_risk_context(predecessor))
    assert pending.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_required")
    assert pending.action_id == "act_eeeeeeeeeeee"
    assert pending.approval_request_id is not None
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_denied_risk_approval_creates_no_successor():
    runtime, _, predecessor = _ambiguous(_build(
        gate=DecisionGate(HumanApprovalDecision.DENY)))
    denied = _invoke(runtime, risk_context=_risk_context(predecessor))
    assert denied.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_denied")
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_autonomous_mutation_policy_still_requires_risk_acknowledgement():
    runtime, _, predecessor = _ambiguous(_build(
        policy=_policy(PermissionLevel.AUTONOMOUS)))
    pending = _invoke(runtime, risk_context=_risk_context(predecessor))
    assert pending.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_required")
    assert len(runtime["approval_store"].list_requests()) == 1
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_expired_risk_acknowledgement_creates_no_successor():
    runtime, _, predecessor = _ambiguous(_build(ttl=timedelta(seconds=5)))
    pending, context = _approve_risk(runtime, predecessor)
    runtime["clock"].advance(5)
    expired = _invoke(runtime, risk_context=context)
    assert expired.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_expired")
    assert expired.approval_request_id == pending.approval_request_id
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_risk_ack_expiring_during_provider_setup_creates_no_successor():
    runtime, _, predecessor = _ambiguous(_build(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        ttl=timedelta(seconds=5),
    ))
    runtime["clock"].advance(60)
    _, context = _approve_risk(runtime, predecessor)
    provider = runtime["provider"]
    provider.establish_hook = lambda: runtime["clock"].advance(5)
    expired = _invoke(runtime, risk_context=context)

    assert expired.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_expired")
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_current_permission_deny_blocks_even_after_valid_risk_acknowledgement():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    runtime["executor"].policy = _policy(
        PermissionLevel.AUTONOMOUS,
        blocked=(Capability.FS_WRITE,),
    )
    denied = _invoke(runtime, risk_context=context)
    assert denied.result.metadata["kind"] == "permission_denied"
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_permission_deny_during_risk_decision_blocks_successor_creation():
    gate = DecisionGate(HumanApprovalDecision.APPROVE)
    runtime, _, predecessor = _ambiguous(_build(gate=gate))

    def deny_current_policy(_prompt):
        runtime["executor"].policy = _policy(
            PermissionLevel.AUTONOMOUS,
            blocked=(Capability.FS_WRITE,),
        )

    gate.hook = deny_current_policy
    denied = _invoke(runtime, risk_context=_risk_context(predecessor))

    assert denied.result.metadata["kind"] == "permission_denied"
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_permission_deny_after_successor_claim_blocks_first_effect_seam():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            runtime["executor"].policy = _policy(
                PermissionLevel.AUTONOMOUS,
                blocked=(Capability.FS_WRITE,),
            )

    denied = _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    successor = runtime["store"].load(context.successor_action_id)

    assert denied.result.metadata["kind"] == "permission_denied"
    assert successor.state is ActionState.IN_FLIGHT
    assert runtime["tool"].remote_effects == 1


def test_current_confirm_policy_requires_separate_normal_approval():
    runtime, _, predecessor = _ambiguous()
    runtime["executor"].policy = _policy(PermissionLevel.CONFIRM)
    risk_pending, context = _approve_risk(runtime, predecessor)

    normal_pending = _invoke(runtime, risk_context=context)
    assert normal_pending.result.metadata["kind"] == "approval_pending"
    assert normal_pending.approval_request_id != risk_pending.approval_request_id
    assert normal_pending.action_id == context.successor_action_id
    assert runtime["store"].list_actions() == [predecessor.action_id]

    runtime["authority"].record_decision(
        normal_pending.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )
    completed = _invoke(
        runtime,
        risk_context=context,
        approval_request_id=normal_pending.approval_request_id,
    )
    successor = runtime["store"].load(completed.action_id)
    assert completed.ok
    assert successor.authority.approval_request_id == (
        normal_pending.approval_request_id)
    assert successor.duplicate_risk_acknowledgement.approval_request_id == (
        risk_pending.approval_request_id)


def test_predecessor_normal_approval_cannot_authorize_successor_risk():
    runtime = _build(policy=_policy(PermissionLevel.CONFIRM))
    first_pending = _invoke(
        runtime,
        action_context=ActionInvocationContext(
            deduplication_scope="test:workflow",
            deduplication_key="step:a",
        ),
    )
    runtime["authority"].record_decision(
        first_pending.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )
    first = _invoke(
        runtime,
        action_context=ActionInvocationContext(
            deduplication_scope="test:workflow",
            deduplication_key="step:a",
        ),
        approval_request_id=first_pending.approval_request_id,
    )
    predecessor = runtime["store"].load(first.action_id)
    forged_continuity = _risk_context(
        predecessor,
        request=first_pending.approval_request_id,
    )
    rejected = _invoke(runtime, risk_context=forged_continuity)
    assert rejected.result.metadata["kind"] == "duplicate_risk_binding_invalid"
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_risk_approval_cannot_satisfy_normal_mutation_approval():
    runtime, _, predecessor = _ambiguous()
    runtime["executor"].policy = _policy(PermissionLevel.CONFIRM)
    risk_pending, context = _approve_risk(runtime, predecessor)
    rejected = _invoke(
        runtime,
        risk_context=context,
        approval_request_id=risk_pending.approval_request_id,
    )
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_normal_mutation_approval_cannot_substitute_for_risk_acknowledgement():
    runtime = _build(policy=_policy(PermissionLevel.CONFIRM))
    normal_pending = _invoke(runtime)
    runtime["authority"].record_decision(
        normal_pending.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )
    runtime["tool"].lose_first_response = True
    first = _invoke(
        runtime,
        approval_request_id=normal_pending.approval_request_id,
    )
    predecessor = runtime["store"].load(first.action_id)
    rejected = _invoke(
        runtime,
        risk_context=_risk_context(
            predecessor,
            request=normal_pending.approval_request_id,
        ),
    )
    assert rejected.result.metadata["kind"] == "duplicate_risk_binding_invalid"
    assert len(runtime["store"].list_actions()) == 1


def test_successor_has_fresh_invocation_attempt_and_authority_identity():
    runtime, first, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    second = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(second.action_id)
    assert successor.provenance.invocation_id != predecessor.provenance.invocation_id
    assert successor.attempts[0].invocation_id == second.invocation_id
    assert successor.attempts[0].attempt_id != predecessor.attempts[0].attempt_id
    assert successor.authority != predecessor.authority
    assert second.invocation_id != first.invocation_id


def test_predecessor_unknown_truth_remains_unchanged_after_successor_success():
    runtime, _, predecessor = _ambiguous()
    before = predecessor.to_dict()
    _, context = _approve_risk(runtime, predecessor)
    _invoke(runtime, risk_context=context)
    after = runtime["store"].load(predecessor.action_id)
    assert after.to_dict() == before
    assert after.state is ActionState.RECONCILE_REQUIRED
    assert after.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN


def test_same_acknowledgement_concurrent_callers_create_one_successor():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    barrier = threading.Barrier(2)
    results = []

    def call():
        barrier.wait()
        results.append(_invoke(runtime, risk_context=context))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not [thread for thread in threads if thread.is_alive()]
    assert len(runtime["store"].list_actions()) == 2
    successor_ids = {
        result.action_id for result in results
        if result.action_id != predecessor.action_id
    }
    assert successor_ids == {context.successor_action_id}
    assert runtime["tool"].remote_effects == 2


def test_separate_acknowledgements_create_separate_successors():
    runtime, _, predecessor = _ambiguous()
    _, first_context = _approve_risk(
        runtime, predecessor, successor="act_dddddddddddd")
    first_repeat = _invoke(runtime, risk_context=first_context)
    _, second_context = _approve_risk(
        runtime, predecessor, successor="act_cccccccccccc")
    second_repeat = _invoke(runtime, risk_context=second_context)
    assert first_repeat.action_id != second_repeat.action_id
    assert len(runtime["store"].list_actions()) == 3
    assert runtime["tool"].remote_effects == 3


def test_acknowledgement_cannot_replay_against_different_predecessor():
    runtime, _, predecessor = _ambiguous()
    pending, _ = _approve_risk(runtime, predecessor)
    runtime["tool"].lose_first_response = True
    other = _invoke(
        runtime,
        action_context=ActionInvocationContext(
            deduplication_scope="test:workflow",
            deduplication_key="step:other",
        ),
    )
    other_predecessor = runtime["store"].load(other.action_id)
    rejected = _invoke(
        runtime,
        risk_context=_risk_context(
            other_predecessor,
            request=pending.approval_request_id,
        ),
    )
    assert rejected.result.metadata["kind"] == "duplicate_risk_binding_invalid"
    assert len(runtime["store"].list_actions()) == 2


def test_acknowledgement_cannot_replay_against_different_successor_id():
    runtime, _, predecessor = _ambiguous()
    pending, _ = _approve_risk(
        runtime, predecessor, successor="act_dddddddddddd")
    rejected = _invoke(
        runtime,
        risk_context=_risk_context(
            predecessor,
            successor="act_cccccccccccc",
            request=pending.approval_request_id,
        ),
    )
    assert rejected.result.metadata["kind"] == "duplicate_risk_binding_invalid"
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_acknowledgement_cannot_survive_changed_canonical_intent():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    runtime["adapter"].intent_digest = "c" * 64
    rejected = _invoke(runtime, risk_context=context)
    assert rejected.result.metadata["kind"] == "duplicate_risk_binding_invalid"
    assert runtime["store"].list_actions() == [predecessor.action_id]


def test_changed_target_cannot_masquerade_as_same_deliberate_repeat():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    runtime["adapter"].target_fingerprint = "d" * 64
    rejected = _invoke(runtime, risk_context=context)
    assert rejected.result.metadata["kind"] == "duplicate_risk_binding_invalid"
    assert runtime["tool"].remote_effects == 1


def test_live_external_idempotency_uses_same_action_retry_not_risky_repeat():
    runtime, _, predecessor = _ambiguous(_build(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        gate=DecisionGate(HumanApprovalDecision.APPROVE),
    ))
    rejected = _invoke(runtime, risk_context=_risk_context(predecessor))
    assert rejected.result.metadata["kind"] == (
        "duplicate_risk_same_action_retry_available")
    assert runtime["gate"].prompts == []
    assert runtime["store"].list_actions() == [predecessor.action_id]
    assert runtime["tool"].remote_effects == 1


def test_external_successor_receives_new_key_not_predecessor_key():
    runtime, _, predecessor = _ambiguous(_build(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT))
    predecessor_key = predecessor.idempotency.key
    runtime["clock"].advance(60)
    _, context = _approve_risk(runtime, predecessor)
    completed = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(completed.action_id)
    assert completed.ok
    assert successor.idempotency.key != predecessor_key
    assert runtime["tool"].keys == [
        predecessor_key,
        successor.idempotency.key,
    ]


def test_future_safe_retry_of_successor_reuses_only_successor_key():
    runtime, _, predecessor = _ambiguous(_build(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT))
    predecessor_key = predecessor.idempotency.key
    runtime["clock"].advance(60)
    _, context = _approve_risk(runtime, predecessor)
    runtime["tool"].lose_first_response = True
    ambiguous_successor_call = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(ambiguous_successor_call.action_id)
    successor_key = successor.idempotency.key
    assert successor.state is ActionState.RECONCILE_REQUIRED

    retried = _invoke(
        runtime,
        action_context=ActionInvocationContext(action_id=successor.action_id),
    )
    settled = runtime["store"].load(successor.action_id)
    assert retried.ok
    assert settled.state is ActionState.SUCCEEDED
    assert predecessor_key != successor_key
    assert runtime["tool"].keys == [predecessor_key, successor_key]
    assert runtime["tool"].remote_effects == 2


def test_predecessor_succeeds_after_ack_before_successor_seam_blocks_effect():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            _settle_predecessor(runtime, predecessor, ActionState.SUCCEEDED)

    blocked = _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    successor = runtime["store"].load(context.successor_action_id)
    assert blocked.result.metadata["kind"] == (
        "duplicate_risk_predecessor_changed")
    assert successor.state is ActionState.IN_FLIGHT
    assert len(successor.attempts) == 1
    assert runtime["tool"].remote_effects == 1
    assert runtime["store"].load(predecessor.action_id).state is (
        ActionState.SUCCEEDED)


def test_predecessor_version_change_after_ack_fails_closed():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            current = runtime["store"].load(predecessor.action_id)
            candidate = current.transition(
                ActionState.RECONCILE_REQUIRED,
                transition_id=runtime["ids"]("atx"),
                at=runtime["clock"].now(),
                effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
                reconciliation=replace(
                    current.reconciliation,
                    disposition=ReconciliationDisposition.UNRESOLVABLE,
                ),
            )
            runtime["store"].compare_and_swap(
                current.action_id, current.version, candidate)

    blocked = _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    assert blocked.result.metadata["kind"] == (
        "duplicate_risk_predecessor_changed")
    assert runtime["tool"].remote_effects == 1
    assert runtime["store"].load(context.successor_action_id).state is (
        ActionState.IN_FLIGHT)


def test_risk_approval_expiry_after_successor_claim_blocks_final_seam():
    runtime, _, predecessor = _ambiguous(_build(ttl=timedelta(seconds=5)))
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            runtime["clock"].advance(5)

    blocked = _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    successor = runtime["store"].load(context.successor_action_id)
    assert blocked.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_expired")
    assert successor.state is ActionState.IN_FLIGHT
    assert runtime["tool"].remote_effects == 1


def test_risk_expiry_during_final_provider_check_blocks_external_seam():
    runtime, _, predecessor = _ambiguous(_build(
        effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        ttl=timedelta(seconds=5),
    ))
    runtime["clock"].advance(60)
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    def expire_once():
        runtime["provider"].validate_hook = None
        runtime["clock"].advance(5)

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            runtime["provider"].validate_hook = expire_once

    blocked = _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    successor = runtime["store"].load(context.successor_action_id)

    assert blocked.result.metadata["kind"] == (
        "duplicate_risk_acknowledgement_expired")
    assert successor.state is ActionState.IN_FLIGHT
    assert runtime["provider"].execute_calls == 1
    assert runtime["tool"].remote_effects == 1


def test_confirmed_not_applied_uses_safe_same_action_path_not_risky_repeat():
    runtime, _, predecessor = _ambiguous()
    authorized = _settle_predecessor(
        runtime, predecessor, ActionState.AUTHORIZED)
    risky = _invoke(runtime, risk_context=_risk_context(authorized))
    assert risky.result.metadata["kind"] == (
        "duplicate_risk_predecessor_changed")
    resumed = _invoke(
        runtime,
        action_context=ActionInvocationContext(action_id=authorized.action_id),
    )
    assert resumed.ok
    assert runtime["store"].list_actions() == [authorized.action_id]
    assert runtime["tool"].remote_effects == 2


@pytest.mark.parametrize("terminal", [ActionState.SUCCEEDED, ActionState.FAILED])
def test_resolved_predecessor_cannot_be_risky_repeat_source(terminal):
    runtime, _, predecessor = _ambiguous()
    settled = _settle_predecessor(runtime, predecessor, terminal)
    rejected = _invoke(runtime, risk_context=_risk_context(settled))
    assert rejected.result.metadata["kind"] == (
        "duplicate_risk_predecessor_changed")
    assert len(runtime["store"].list_actions()) == 1
    assert runtime["tool"].remote_effects == 1


def test_cancelled_predecessor_cannot_be_risky_repeat_source():
    runtime = _build()
    fresh = runtime["store"].create(_fresh_record(0x901))
    cancelled = runtime["store"].compare_and_swap(
        fresh.action_id,
        fresh.version,
        fresh.transition(
            ActionState.CANCELLED,
            transition_id="atx_000000000901",
            at=NOW,
        ),
    )
    rejected = _invoke(runtime, risk_context=_risk_context(cancelled))
    assert rejected.result.metadata["kind"] == (
        "duplicate_risk_predecessor_changed")
    assert runtime["tool"].runs == 0


def test_owner_loss_in_flight_cannot_bypass_recovery_with_risky_repeat():
    runtime = _build()
    fresh = runtime["store"].create(_fresh_record(0x902))
    claimed = runtime["store"].claim_attempt(
        fresh.action_id,
        fresh.version,
        attempt_id="aat_000000000902",
        invocation_id="inv_000000000903",
        owner_id="process:lost-owner",
        owner_epoch=1,
        transition_id="atx_000000000902",
    )
    rejected = _invoke(runtime, risk_context=_risk_context(claimed))
    assert rejected.result.metadata["kind"] == (
        "duplicate_risk_predecessor_changed")
    assert runtime["store"].load(claimed.action_id).state is (
        ActionState.IN_FLIGHT)
    assert runtime["tool"].runs == 0


def test_non_idempotent_ambiguous_repeat_requires_explicit_ack():
    runtime, _, predecessor = _ambiguous()
    no_ack = _invoke(
        runtime,
        action_context=ActionInvocationContext(action_id=predecessor.action_id),
    )
    assert no_ack.action_state == ActionState.RECONCILE_REQUIRED.value
    assert runtime["tool"].remote_effects == 1
    _, context = _approve_risk(runtime, predecessor)
    with_ack = _invoke(runtime, risk_context=context)
    assert with_ack.ok
    assert runtime["tool"].remote_effects == 2


def test_protected_secret_is_absent_from_risk_public_surfaces():
    ids = DeterministicIds()
    payload_store = InMemoryProtectedActionPayloadStore(KEY, id_factory=ids)
    tool = RiskyTool()
    adapter = RiskyAdapter(tool, protected=True)
    runtime, _, predecessor = _ambiguous(_build(
        ids=ids,
        tool=tool,
        adapter=adapter,
        payload_store=payload_store,
    ))
    pending, context = _approve_risk(runtime, predecessor)
    completed = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(completed.action_id)
    approval_records = [
        runtime["approval_store"].load(request_id).to_dict()
        for request_id in runtime["approval_store"].list_requests()
    ]
    public = json.dumps(
        {
            "predecessor": predecessor.to_dict(),
            "successor": successor.to_dict(),
            "ack": successor.duplicate_risk_acknowledgement.to_dict(),
            "approvals": approval_records,
            "pending": pending.to_dict(),
            "completed": completed.to_dict(),
            "logs": runtime["logger"].events,
        },
        sort_keys=True,
        default=str,
    )
    assert SECRET not in public
    assert successor.intent_ref is not None


def test_store_rejects_same_acknowledgement_on_two_successors():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    completed = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(completed.action_id)
    duplicate = new_action(
        action_id="act_bbbbbbbbbbbb",
        action_kind=successor.action_kind,
        tool_name=successor.tool_name,
        tool_version=successor.tool_version,
        eligibility=successor.eligibility,
        mutation_capabilities=successor.mutation_capabilities,
        reconciliation_capabilities=successor.reconciliation_capabilities,
        effect_class=successor.effect_class,
        intent_digest=successor.intent_digest,
        intent_ref=None,
        target_category=successor.target_category,
        target_fingerprint=successor.target_fingerprint,
        provenance=ActionProvenance(invocation_id="inv_bbbbbbbbbbbb"),
        authority=AuthorityReceipt(
            receipt_id="auth_bbbbbbbbbbbb",
            binding_digest="b" * 64,
            policy_fingerprint="c" * 64,
            scope_ref="scope:duplicate",
            authorized_at=NOW,
        ),
        predecessor_action_id=predecessor.action_id,
        duplicate_risk_acknowledgement=(
            successor.duplicate_risk_acknowledgement),
        deduplication_scope=_risk_scope(),
        deduplication_key="approval:other",
        clock=FixedClock(NOW),
    )
    with pytest.raises(ActionStoreError):
        runtime["store"].create(duplicate)


def _risk_scope() -> str:
    return "agentcore:duplicate-risk:v1"


def test_store_rejects_same_risk_approval_with_changed_ack_id():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    completed = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(completed.action_id)
    changed_ack = replace(
        successor.duplicate_risk_acknowledgement,
        acknowledgement_id="dra_bbbbbbbbbbbb",
    )
    duplicate = new_action(
        action_id="act_bbbbbbbbbbbb",
        action_kind=successor.action_kind,
        tool_name=successor.tool_name,
        tool_version=successor.tool_version,
        eligibility=successor.eligibility,
        mutation_capabilities=successor.mutation_capabilities,
        reconciliation_capabilities=successor.reconciliation_capabilities,
        effect_class=successor.effect_class,
        intent_digest=successor.intent_digest,
        intent_ref=None,
        target_category=successor.target_category,
        target_fingerprint=successor.target_fingerprint,
        provenance=ActionProvenance(invocation_id="inv_bbbbbbbbbbbb"),
        authority=AuthorityReceipt(
            receipt_id="auth_bbbbbbbbbbbb",
            binding_digest="b" * 64,
            policy_fingerprint="c" * 64,
            scope_ref="scope:duplicate",
            authorized_at=NOW,
        ),
        predecessor_action_id=predecessor.action_id,
        duplicate_risk_acknowledgement=changed_ack,
        deduplication_scope=_risk_scope(),
        deduplication_key="approval:other",
        clock=FixedClock(NOW),
    )
    with pytest.raises(ActionStoreError):
        runtime["store"].create(duplicate)


def test_store_rejects_acknowledgement_predecessor_mismatch():
    acknowledgement = DuplicateRiskAcknowledgement(
        acknowledgement_id="dra_aaaaaaaaaaaa",
        predecessor_action_id="act_aaaaaaaaaaaa",
        approval_request_id="apr_aaaaaaaaaaaa",
        binding_digest="a" * 64,
        acknowledged_at=NOW,
    )
    with pytest.raises(ActionValidationError):
        new_action(
            action_id="act_bbbbbbbbbbbb",
            action_kind="test.risky-mutation.v1",
            tool_name="risky_mutation",
            tool_version="1.0.0",
            eligibility=(
                ActionJournalEligibility.LONG_RUNNING_OR_AMBIGUOUS_EFFECT),
            mutation_capabilities=("fs.write",),
            reconciliation_capabilities=(),
            effect_class=(
                ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS),
            intent_digest="a" * 64,
            intent_ref=None,
            target_category="test.target",
            target_fingerprint="b" * 64,
            provenance=ActionProvenance(
                invocation_id="inv_bbbbbbbbbbbb"),
            authority=AuthorityReceipt(
                receipt_id="auth_bbbbbbbbbbbb",
                binding_digest="b" * 64,
                policy_fingerprint="c" * 64,
                scope_ref="scope:test",
                authorized_at=NOW,
            ),
            predecessor_action_id="act_cccccccccccc",
            duplicate_risk_acknowledgement=acknowledgement,
            clock=FixedClock(NOW),
        )


def test_generic_predecessor_link_without_risk_ack_remains_valid():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    predecessor = store.create(_fresh_record(0x910))
    linked = store.create(new_action(
        action_id="act_000000000911",
        action_kind="test.risky-mutation.v1",
        tool_name="risky_mutation",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.LONG_RUNNING_OR_AMBIGUOUS_EFFECT,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=(),
        effect_class=ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS,
        intent_digest="a" * 64,
        intent_ref=None,
        target_category="test.target",
        target_fingerprint="b" * 64,
        provenance=ActionProvenance(invocation_id="inv_000000000911"),
        authority=AuthorityReceipt(
            receipt_id="auth_000000000911",
            binding_digest="d" * 64,
            policy_fingerprint="e" * 64,
            scope_ref="scope:generic-link",
            authorized_at=NOW,
        ),
        predecessor_action_id=predecessor.action_id,
        clock=FixedClock(NOW),
    ))
    assert linked.predecessor_action_id == predecessor.action_id
    assert linked.duplicate_risk_acknowledgement is None


def test_predecessor_cycle_cannot_be_introduced_by_cas():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    first = store.create(_fresh_record(0x920))
    second = store.create(new_action(
        action_id="act_000000000921",
        action_kind=first.action_kind,
        tool_name=first.tool_name,
        tool_version=first.tool_version,
        eligibility=first.eligibility,
        mutation_capabilities=first.mutation_capabilities,
        reconciliation_capabilities=first.reconciliation_capabilities,
        effect_class=first.effect_class,
        intent_digest=first.intent_digest,
        intent_ref=None,
        target_category=first.target_category,
        target_fingerprint=first.target_fingerprint,
        provenance=ActionProvenance(invocation_id="inv_000000000921"),
        authority=AuthorityReceipt(
            receipt_id="auth_000000000921",
            binding_digest="f" * 64,
            policy_fingerprint="1" * 64,
            scope_ref="scope:cycle",
            authorized_at=NOW,
        ),
        predecessor_action_id=first.action_id,
        clock=FixedClock(NOW),
    ))
    cancelled = first.transition(
        ActionState.CANCELLED,
        transition_id="atx_000000000920",
        at=NOW,
    )
    with pytest.raises(ActionValidationError):
        store.compare_and_swap(
            first.action_id,
            first.version,
            replace(cancelled, predecessor_action_id=second.action_id),
        )


def test_successor_preseam_cancellation_never_changes_predecessor():
    runtime, _, predecessor = _ambiguous()
    before = predecessor.to_dict()
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise CancellationRequested(CancellationReason.OPERATOR_CANCEL)

    with pytest.raises(CancellationRequested):
        _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    successor = runtime["store"].load(context.successor_action_id)
    assert successor.state is ActionState.CANCELLED
    assert successor.attempts[-1].status is ActionAttemptStatus.NOT_INVOKED
    assert runtime["store"].load(predecessor.action_id).to_dict() == before
    assert runtime["tool"].remote_effects == 1


def test_successor_result_publication_failure_never_repeats_effect():
    clock = FixedClock(NOW)
    store = ResultPublicationFailingStore(clock=clock)
    runtime, _, predecessor = _ambiguous(_build(clock=clock, store=store))
    _, context = _approve_risk(runtime, predecessor)
    store.fail_action_id = context.successor_action_id
    uncertain = _invoke(runtime, risk_context=context)
    assert uncertain.result.metadata["kind"] == (
        "action_result_publication_uncertain")
    assert runtime["tool"].remote_effects == 2
    attached = _invoke(
        runtime,
        action_context=ActionInvocationContext(
            action_id=context.successor_action_id),
    )
    assert not attached.ok
    assert runtime["tool"].remote_effects == 2


def test_successor_recovery_truth_is_independent_from_predecessor():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    runtime["tool"].lose_first_response = True
    second = _invoke(runtime, risk_context=context)
    successor = runtime["store"].load(second.action_id)
    assert successor.state is ActionState.RECONCILE_REQUIRED
    report = ActionRecoveryCoordinator(
        runtime["store"], runtime["executor"]).recover_startup()
    assert {item.action_id for item in report.items} == {
        predecessor.action_id,
        successor.action_id,
    }
    assert runtime["store"].load(predecessor.action_id).attempts != (
        runtime["store"].load(successor.action_id).attempts)


def test_human_and_physical_boundaries_hold_no_general_store_mutex():
    gate = DecisionGate(HumanApprovalDecision.APPROVE)
    runtime, _, predecessor = _ambiguous(_build(gate=gate))

    def prove_accessible():
        finished = threading.Event()

        def inspect():
            runtime["store"].list_actions()
            runtime["approval_store"].list_requests()
            finished.set()

        thread = threading.Thread(target=inspect)
        thread.start()
        thread.join(timeout=2)
        assert finished.is_set()

    gate.hook = lambda prompt: prove_accessible()
    runtime["tool"].run_hook = prove_accessible
    completed = _invoke(runtime, risk_context=_risk_context(predecessor))
    assert completed.ok


def test_restart_revalidates_consumed_risk_before_first_attempt(tmp_path: Path):
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    action_dir = tmp_path / "actions"
    approval_dir = tmp_path / "approvals"
    action_store = JsonFileActionStore(action_dir, clock=clock)
    approval_store = JsonFileApprovalStore(approval_dir)
    runtime, _, predecessor = _ambiguous(_build(
        clock=clock,
        ids=ids,
        store=action_store,
        approval_store=approval_store,
    ))
    _, context = _approve_risk(runtime, predecessor)
    calls = 0

    class SimulatedCrash(BaseException):
        pass

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        _invoke(runtime, risk_context=context, checkpoint=checkpoint)
    created = action_store.load(context.successor_action_id)
    assert created.state is ActionState.AUTHORIZED
    assert created.attempts == ()
    action_store.close()
    approval_store.close()

    reopened_actions = JsonFileActionStore(action_dir, clock=clock)
    reopened_approvals = JsonFileApprovalStore(approval_dir)
    restarted = _build(
        clock=clock,
        ids=ids,
        store=reopened_actions,
        approval_store=reopened_approvals,
        tool=runtime["tool"],
        adapter=runtime["adapter"],
    )
    resumed = _invoke(
        restarted,
        action_context=ActionInvocationContext(
            action_id=context.successor_action_id),
    )
    assert resumed.ok
    assert reopened_actions.load(context.successor_action_id).state is (
        ActionState.SUCCEEDED)
    assert reopened_actions.load(predecessor.action_id).state is (
        ActionState.RECONCILE_REQUIRED)
    reopened_actions.close()
    reopened_approvals.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_inherited_duplicate_risk_authority_fails_closed_after_fork():
    runtime, _, predecessor = _ambiguous()
    _, context = _approve_risk(runtime, predecessor)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child result reported through pipe
        os.close(read_fd)
        try:
            runtime["authority"].binding(
                action_kind="agentcore.duplicate_risk.v1",
                intent_digest="a" * 64,
                mutation_capabilities=("fs.write",),
                tool_name="risky_mutation",
                tool_version="1.0.0",
                policy_fingerprint="b" * 64,
            )
        except ApprovalUnavailableError:
            os.write(write_fd, b"closed")
            os._exit(0)
        os._exit(2)
    os.close(write_fd)
    payload = os.read(read_fd, 32)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert payload == b"closed"
    context.validate()


def test_ordinary_new_action_fabricates_no_duplicate_risk_metadata():
    runtime = _build(tool=RiskyTool(lose_first_response=False))
    ordinary = _invoke(
        runtime,
        action_context=ActionInvocationContext(
            deduplication_scope="test:workflow",
            deduplication_key="ordinary:new",
        ),
    )
    record = runtime["store"].load(ordinary.action_id)
    assert record.predecessor_action_id is None
    assert record.duplicate_risk_acknowledgement is None


def test_schema_versions_remain_one_and_no_risky_repeat_state_is_added():
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert APPROVAL_SCHEMA_VERSION == 1
    assert PROTECTED_STORAGE_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1
    assert {state.value for state in ActionState} == {
        "authorized", "in_flight", "reconcile_required",
        "succeeded", "failed", "cancelled",
    }
