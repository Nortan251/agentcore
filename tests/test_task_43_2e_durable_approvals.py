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
    ActionAttemptStatus,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionProvenance,
    ActionState,
    ActionTransitionProof,
    AuthorityReceipt,
    EffectVerdict,
    InMemoryActionStore,
    InMemoryProtectedActionPayloadStore,
    NormalizedActionEffect,
    PreparedActionIntent,
    ReconciliationDisposition,
    ReconciliationMetadata,
    StaticInstallationKeyProvider,
    new_action,
)
from agentcore.approvals import (
    APPROVAL_SCHEMA_VERSION,
    ApprovalAuthorizationStatus,
    ApprovalInvocationContext,
    ApprovalState,
    ApprovalStore,
    ApprovalUnavailableError,
    ApprovalValidationError,
    ApprovalVersionConflictError,
    DurableApprovalAuthority,
    HumanApprovalDecision,
    InMemoryApprovalStore,
    JsonFileApprovalStore,
)
from agentcore.config.schema import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
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


NOW = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
KEY = StaticInstallationKeyProvider(b"k" * 32)
SENSITIVE = "task-43.2e-private-recipient@example.invalid"


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

    def debug(self, event, **fields):
        self._record(event, fields)

    def info(self, event, **fields):
        self._record(event, fields)

    def warning(self, event, **fields):
        self._record(event, fields)

    def error(self, event, **fields):
        self._record(event, fields)

    def exception(self, event, **fields):
        self._record(event, fields)


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


class MutationTool(Tool):
    def __init__(
        self,
        *,
        version="1.0.0",
        capabilities=(Capability.FS_WRITE,),
        run_hook=None,
    ) -> None:
        self.version = version
        self._capabilities = tuple(capabilities)
        self.run_hook = run_hook
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "approve_mutation",
            "Mutate one exact test target.",
            version=self.version,
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=self._capabilities,
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.runs += 1
        if self.run_hook is not None:
            return self.run_hook(args, context)
        return ToolResult.success("raw-provider-output")


class MutationAdapter:
    def __init__(
        self,
        tool: MutationTool,
        *,
        action_kind="test.durable-approval.v1",
        intent_digest="a" * 64,
        protected=False,
        protected_value=SENSITIVE,
    ) -> None:
        self.spec = type("SpecHolder", (), {})
        from agentcore.action_journal import ActionAdapterSpec

        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind=action_kind,
            eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
        )
        self.intent_digest = intent_digest
        self.protected = protected
        self.protected_value = protected_value

    def prepare_intent(self, cleaned, context):
        if self.protected:
            return PreparedActionIntent(
                intent_digest=self.intent_digest,
                target_category="test.private-target",
                approval_description=f"Approve sending to {self.protected_value}",
                requires_protected_payload=True,
                protected_payload={
                    "recipient": self.protected_value,
                    "body": self.protected_value,
                },
            )
        return PreparedActionIntent(
            intent_digest=self.intent_digest,
            target_category="test.target",
            approval_description="Approve one exact test mutation",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The exact mutation was confirmed.",
        )


def _confirm_policy(*, extra_blocked=()) -> PermissionPolicy:
    return PermissionPolicy(PermissionConfig(
        level=PermissionLevel.CONFIRM,
        blocked_capabilities=tuple(extra_blocked),
    ))


def _build(
    *,
    tool=None,
    adapter=None,
    gate=None,
    policy=None,
    action_store=None,
    approval_store=None,
    payload_store=None,
    clock=None,
    ids=None,
    logger=None,
    key=None,
    ttl=timedelta(minutes=5),
):
    clock = clock or FixedClock(NOW)
    ids = ids or DeterministicIds()
    tool = tool or MutationTool()
    adapter = adapter or MutationAdapter(tool)
    gate = gate or DecisionGate()
    policy = policy or _confirm_policy()
    action_store = action_store or InMemoryActionStore(clock=clock)
    approval_store = approval_store or InMemoryApprovalStore()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    adapters.register(tool, adapter)
    authority = DurableApprovalAuthority(
        approval_store,
        key or KEY,
        principal_ref="principal:local-owner",
        scope_ref="workspace:test",
        gate=gate,
        ttl=ttl,
        clock=clock,
        id_factory=ids,
    )
    executor = ToolExecutor(
        registry,
        policy,
        action_store=action_store,
        action_adapters=adapters,
        protected_payload_store=payload_store,
        approval_authority=authority,
        clock=clock,
        id_factory=ids,
        logger=logger or RecordingLogger(),
    )
    return executor, authority, approval_store, action_store, tool, gate


def _invoke(executor, *, approval_request_id=None, action_id=None,
            payload=SENSITIVE):
    return executor.invoke(
        "approve_mutation",
        {"payload": payload},
        action_context=(ActionInvocationContext(action_id=action_id)
                        if action_id is not None else None),
        approval_context=(ApprovalInvocationContext(approval_request_id)
                          if approval_request_id is not None else None),
    )


def _approve_pending(executor, authority):
    pending = _invoke(executor)
    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    approved = _invoke(
        executor, approval_request_id=pending.approval_request_id)
    return pending, approved


def _approval_path(directory: Path, request_id: str) -> Path:
    return directory / f"{request_id}.json"


def _rewrite_approval(path: Path, mutate) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    path.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return record


def _forge_approved(record: dict[str, Any]) -> None:
    record["state"] = "approved"
    record["version"] = 1
    record["decision_at"] = record["created_at"]
    record["consumed_at"] = None
    record["consumed_action_id"] = None
    record["receipt"] = {
        "receipt_id": "aprc_eeeeeeeeeeee",
        "request_id": record["request_id"],
        "action_id": record["action_id"],
        "binding_digest": record["binding_digest"],
        "policy_fingerprint": record["policy_fingerprint"],
        "principal_ref": record["principal_ref"],
        "scope_ref": record["scope_ref"],
        "semantics": record["semantics"],
        "approved_at": record["decision_at"],
        "expires_at": record["expires_at"],
    }


def test_pending_and_denied_create_no_action_attempt_or_effect():
    pending_executor, _, pending_store, actions, tool, _ = _build()
    pending = _invoke(pending_executor)
    assert pending.result.metadata["kind"] == "approval_pending"
    assert pending.action_id is None
    assert pending.approval_request_id.startswith("apr_")
    assert actions.list_actions() == []
    assert tool.runs == 0
    assert pending_store.load(pending.approval_request_id).state is (
        ApprovalState.PENDING)

    denied_gate = DecisionGate(HumanApprovalDecision.DENY)
    denied_executor, _, denied_store, actions, tool, _ = _build(gate=denied_gate)
    denied = _invoke(denied_executor)
    assert denied.result.metadata["kind"] == "approval_denied"
    assert denied_store.load(denied.approval_request_id).state is (
        ApprovalState.DENIED)
    assert actions.list_actions() == []
    assert tool.runs == 0


def test_approved_exact_intent_consumes_once_and_authority_carries_request_id():
    executor, authority, approvals, actions, tool, _ = _build()
    pending, approved = _approve_pending(executor, authority)

    assert approved.ok
    assert tool.runs == 1
    assert approvals.load(pending.approval_request_id).state is (
        ApprovalState.CONSUMED)
    record = actions.load(approved.action_id)
    assert record.authority.approval_request_id == pending.approval_request_id
    assert record.authority.expires_at is not None

    reused = _invoke(
        executor, approval_request_id=pending.approval_request_id)
    assert reused.result.metadata["kind"] == "approval_consumed"
    assert tool.runs == 1
    assert len(actions.list_actions()) == 1


@pytest.mark.parametrize(
    "change",
    ["intent", "action_kind", "tool_version", "capabilities", "policy"],
)
def test_changed_public_binding_rejects_existing_approval(change):
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    approvals = InMemoryApprovalStore()
    actions = InMemoryActionStore(clock=clock)
    first, _, _, _, first_tool, _ = _build(
        clock=clock, ids=ids, approval_store=approvals,
        action_store=actions)
    pending = _invoke(first)

    tool = MutationTool(
        version="2.0.0" if change == "tool_version" else "1.0.0",
        capabilities=((Capability.FS_WRITE, Capability.FS_DELETE)
                      if change == "capabilities"
                      else (Capability.FS_WRITE,)),
    )
    adapter = MutationAdapter(
        tool,
        action_kind=("test.changed-kind.v1"
                     if change == "action_kind"
                     else "test.durable-approval.v1"),
        intent_digest=("b" * 64 if change == "intent" else "a" * 64),
    )
    policy = _confirm_policy(
        extra_blocked=("net.write",) if change == "policy" else ())
    second, _, _, _, second_tool, _ = _build(
        tool=tool,
        adapter=adapter,
        policy=policy,
        clock=clock,
        ids=ids,
        approval_store=approvals,
        action_store=actions,
    )
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert first_tool.runs == second_tool.runs == 0
    assert actions.list_actions() == []
    assert approvals.load(pending.approval_request_id).state is (
        ApprovalState.PENDING)


def test_changed_protected_payload_digest_rejects_without_public_leak():
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    approvals = InMemoryApprovalStore()
    actions = InMemoryActionStore(clock=clock)
    payloads = InMemoryProtectedActionPayloadStore(KEY, id_factory=ids)
    first_tool = MutationTool()
    first, _, _, _, _, gate = _build(
        tool=first_tool,
        adapter=MutationAdapter(first_tool, protected=True,
                                protected_value="private:first"),
        clock=clock, ids=ids, approval_store=approvals,
        action_store=actions, payload_store=payloads)
    pending = _invoke(first)
    assert gate.prompts[0].description.endswith("private:first")

    second_tool = MutationTool()
    second, _, _, _, _, _ = _build(
        tool=second_tool,
        adapter=MutationAdapter(second_tool, protected=True,
                                protected_value="private:changed"),
        clock=clock, ids=ids, approval_store=approvals,
        action_store=actions, payload_store=payloads)
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert second_tool.runs == 0


def test_expired_approval_is_durable_and_never_runs():
    clock = FixedClock(NOW)
    executor, authority, approvals, actions, tool, _ = _build(
        clock=clock, ttl=timedelta(seconds=10))
    pending = _invoke(executor)
    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    clock.advance(11)

    expired = _invoke(
        executor, approval_request_id=pending.approval_request_id)
    assert expired.result.metadata["kind"] == "approval_expired"
    assert approvals.load(pending.approval_request_id).state is (
        ApprovalState.EXPIRED)
    assert actions.list_actions() == []
    assert tool.runs == 0


def test_approval_cannot_override_current_policy_deny():
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    approvals = InMemoryApprovalStore()
    actions = InMemoryActionStore(clock=clock)
    first, authority, _, _, _, _ = _build(
        clock=clock, ids=ids, approval_store=approvals,
        action_store=actions)
    pending = _invoke(first)
    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)

    denied_policy = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.OBSERVE))
    second, _, _, _, tool, _ = _build(
        policy=denied_policy, clock=clock, ids=ids,
        approval_store=approvals, action_store=actions)
    denied = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert denied.result.metadata["kind"] == "permission_denied"
    assert approvals.load(pending.approval_request_id).state is (
        ApprovalState.APPROVED)
    assert actions.list_actions() == []
    assert tool.runs == 0


def test_concurrent_consumption_has_exactly_one_winner():
    executor, authority, approvals, _, _, _ = _build()
    pending = _invoke(executor)
    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    request = approvals.load(pending.approval_request_id)
    binding = request.as_binding()
    barrier = threading.Barrier(3)
    results = []

    def consume():
        barrier.wait()
        results.append(authority.authorize(
            binding,
            description="exact concurrent approval",
            request_id=request.request_id,
        ).status)

    threads = [threading.Thread(target=consume, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()

    assert sorted(results, key=str) == sorted([
        ApprovalAuthorizationStatus.AUTHORIZED,
        ApprovalAuthorizationStatus.CONSUMED,
    ], key=str)
    assert approvals.load(request.request_id).state is ApprovalState.CONSUMED


def test_approval_stores_expose_no_unsealed_consume_operation():
    for store_type in (
        ApprovalStore,
        InMemoryApprovalStore,
        JsonFileApprovalStore,
    ):
        assert "consume" not in store_type.__dict__


def test_wrong_action_rejected_and_authority_consumes_exactly_once():
    executor, authority, approvals, _, _, _ = _build()
    pending = _invoke(executor)
    approved = authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    binding = approved.as_binding()

    with pytest.raises(ApprovalUnavailableError):
        authority.authorize(
            binding,
            description="wrong action must not consume approval",
            action_id="act_ffffffffffff",
            request_id=approved.request_id,
        )
    assert approvals.load(approved.request_id) == approved

    first = authority.authorize(
        binding,
        description="consume the exact approval once",
        request_id=approved.request_id,
    )
    second = authority.authorize(
        binding,
        description="the consumed approval cannot be reused",
        request_id=approved.request_id,
    )
    consumed = approvals.load(approved.request_id)

    assert first.status is ApprovalAuthorizationStatus.AUTHORIZED
    assert first.receipt == approved.receipt
    assert second.status is ApprovalAuthorizationStatus.CONSUMED
    assert consumed.state is ApprovalState.CONSUMED
    assert consumed.version == 2
    assert consumed.consumed_action_id == approved.action_id
    assert consumed.state_mac != approved.state_mac


def test_compare_and_swap_rejects_stale_authority_sealed_candidate():
    approved_executor, approved_authority, _, _, _, _ = _build(
        ids=DeterministicIds())
    approved_pending = _invoke(approved_executor)
    approved = approved_authority.record_decision(
        approved_pending.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )

    denied_executor, denied_authority, _, _, _, _ = _build(
        ids=DeterministicIds())
    denied_pending = _invoke(denied_executor)
    denied = denied_authority.record_decision(
        denied_pending.approval_request_id,
        HumanApprovalDecision.DENY,
    )

    base_executor, _, base_store, _, _, _ = _build(ids=DeterministicIds())
    base_pending = _invoke(base_executor)
    assert approved.request_id == denied.request_id == (
        base_pending.approval_request_id)
    assert approved.action_id == denied.action_id

    assert base_store.compare_and_swap(
        approved.request_id, 0, approved) == approved
    with pytest.raises(ApprovalVersionConflictError):
        base_store.compare_and_swap(denied.request_id, 0, denied)
    assert base_store.load(approved.request_id) == approved


def test_generic_model_arguments_cannot_select_approval_identity():
    executor, _, approvals, actions, tool, _ = _build()
    invocation = executor.invoke(
        "approve_mutation",
        {"payload": SENSITIVE, "approval_request_id": "apr_000000000001"},
    )
    assert invocation.result.metadata["kind"] == "validation"
    assert approvals.list_requests() == []
    assert actions.list_actions() == []
    assert tool.runs == 0


def test_missing_request_and_malformed_typed_context_fail_closed():
    executor, _, approvals, actions, tool, _ = _build()
    missing = _invoke(executor, approval_request_id="apr_ffffffffffff")
    assert missing.result.metadata["kind"] == "approval_unavailable"
    with pytest.raises(ApprovalValidationError):
        ApprovalInvocationContext("provider-selected").validate()
    assert approvals.list_requests() == []
    assert actions.list_actions() == []
    assert tool.runs == 0


def test_json_restart_preserves_approved_and_consumed_truth(tmp_path):
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    directory = tmp_path / "approvals"
    first_store = JsonFileApprovalStore(directory)
    first, authority, _, actions, _, _ = _build(
        clock=clock, ids=ids, approval_store=first_store)
    pending = _invoke(first)
    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    first_store.close()

    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, tool, _ = _build(
        clock=clock, ids=ids, approval_store=reopened,
        action_store=actions)
    assert reopened.load(pending.approval_request_id).state is (
        ApprovalState.APPROVED)
    completed = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert completed.ok, completed.to_dict()
    assert tool.runs == 1
    reopened.close()

    final_store = JsonFileApprovalStore(directory)
    third, _, _, _, final_tool, _ = _build(
        clock=clock, ids=ids, approval_store=final_store,
        action_store=actions)
    replay = _invoke(
        third, approval_request_id=pending.approval_request_id)
    assert replay.result.metadata["kind"] == "approval_consumed"
    assert final_store.load(pending.approval_request_id).state is (
        ApprovalState.CONSUMED)
    assert final_tool.runs == 0
    final_store.close()


def test_corrupt_and_duplicate_key_json_fail_closed_without_repair(tmp_path):
    for index, corrupt in enumerate(("{", '{"request_id":"x","request_id":"y"}')):
        directory = tmp_path / f"approval-{index}"
        store = JsonFileApprovalStore(directory)
        executor, _, _, actions, tool, _ = _build(approval_store=store)
        pending = _invoke(executor)
        store.close()
        path = directory / f"{pending.approval_request_id}.json"
        path.write_text(corrupt, encoding="utf-8")
        reopened = JsonFileApprovalStore(directory)
        second, _, _, _, second_tool, _ = _build(
            approval_store=reopened, action_store=actions)
        rejected = _invoke(
            second, approval_request_id=pending.approval_request_id)
        assert rejected.result.metadata["kind"] == "approval_unavailable"
        assert tool.runs == second_tool.runs == 0
        assert actions.list_actions() == []
        assert path.read_text(encoding="utf-8") == corrupt
        reopened.close()


def test_json_forged_pending_to_approved_fails_before_action_or_tool_run(tmp_path):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, actions, tool, _ = _build(approval_store=store)
    pending = _invoke(executor)
    store.close()

    path = _approval_path(directory, pending.approval_request_id)
    _rewrite_approval(path, _forge_approved)

    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert tool.runs == second_tool.runs == 0
    reopened.close()


def test_json_forged_denied_to_approved_fails_closed(tmp_path):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, actions, tool, gate = _build(
        approval_store=store,
        gate=DecisionGate(HumanApprovalDecision.DENY),
    )
    denied = _invoke(executor)
    assert denied.result.metadata["kind"] == "approval_denied"
    store.close()

    path = _approval_path(directory, denied.approval_request_id)
    _rewrite_approval(path, _forge_approved)

    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        second, approval_request_id=denied.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert tool.runs == second_tool.runs == 0
    assert gate.prompts
    reopened.close()


def test_json_forged_expired_to_approved_fails_closed(tmp_path):
    clock = FixedClock(NOW)
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, actions, tool, _ = _build(
        approval_store=store, clock=clock, ttl=timedelta(seconds=10))
    pending = _invoke(executor)
    clock.advance(11)
    expired = _invoke(executor, approval_request_id=pending.approval_request_id)
    assert expired.result.metadata["kind"] == "approval_expired"
    store.close()

    path = _approval_path(directory, pending.approval_request_id)
    _rewrite_approval(path, _forge_approved)

    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions, clock=clock,
        ttl=timedelta(seconds=10))
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert tool.runs == second_tool.runs == 0
    reopened.close()


def test_json_forged_consumed_to_approved_cannot_restore_authority(tmp_path):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, authority, _, actions, tool, _ = _build(approval_store=store)
    pending, completed = _approve_pending(executor, authority)
    assert completed.ok
    assert tool.runs == 1
    store.close()

    path = _approval_path(directory, pending.approval_request_id)
    _rewrite_approval(path, _forge_approved)

    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert len(actions.list_actions()) == 1
    assert tool.runs == 1
    assert second_tool.runs == 0
    reopened.close()


@pytest.mark.parametrize("field", ["decision_at", "receipt_id"])
def test_json_approved_state_mutation_fails_state_authentication(tmp_path, field):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, authority, _, actions, tool, _ = _build(approval_store=store)
    pending = _invoke(executor)
    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    store.close()

    path = _approval_path(directory, pending.approval_request_id)

    def mutate(record):
        if field == "decision_at":
            record["decision_at"] = "2026-08-27T09:00:01+00:00"
            record["receipt"]["approved_at"] = record["decision_at"]
        else:
            record["receipt"]["receipt_id"] = "aprc_ffffffffffff"

    _rewrite_approval(path, mutate)
    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert tool.runs == second_tool.runs == 0
    reopened.close()


@pytest.mark.parametrize("field", ["consumed_at", "consumed_action_id"])
def test_json_consumed_state_mutation_fails_closed(tmp_path, field):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, authority, _, actions, tool, _ = _build(approval_store=store)
    pending, completed = _approve_pending(executor, authority)
    assert completed.ok
    store.close()

    path = _approval_path(directory, pending.approval_request_id)

    def mutate(record):
        if field == "consumed_at":
            record["consumed_at"] = "2026-08-27T09:00:02+00:00"
        else:
            record["consumed_action_id"] = "act_ffffffffffff"

    _rewrite_approval(path, mutate)
    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert len(actions.list_actions()) == 1
    assert tool.runs == 1
    assert second_tool.runs == 0
    reopened.close()


@pytest.mark.parametrize("field", ["state", "version"])
def test_json_state_or_version_mutation_without_mac_fails_closed(tmp_path, field):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, actions, tool, _ = _build(approval_store=store)
    pending = _invoke(executor)
    store.close()

    path = _approval_path(directory, pending.approval_request_id)

    def mutate(record):
        if field == "state":
            record["state"] = "denied"
            record["version"] = 1
            record["decision_at"] = record["created_at"]
        else:
            record["version"] = 1

    _rewrite_approval(path, mutate)
    reopened = JsonFileApprovalStore(directory)
    second, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        second, approval_request_id=pending.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert tool.runs == second_tool.runs == 0
    reopened.close()


def test_json_state_mac_cannot_transfer_between_approval_requests(tmp_path):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, actions, tool, _ = _build(approval_store=store)
    first = _invoke(executor)
    second = _invoke(executor)
    store.close()

    first_path = _approval_path(directory, first.approval_request_id)
    second_path = _approval_path(directory, second.approval_request_id)
    first_record = json.loads(first_path.read_text(encoding="utf-8"))
    _rewrite_approval(
        second_path,
        lambda record: record.update(state_mac=first_record["state_mac"]),
    )

    reopened = JsonFileApprovalStore(directory)
    replacement, _, _, _, second_tool, _ = _build(
        approval_store=reopened, action_store=actions)
    rejected = _invoke(
        replacement, approval_request_id=second.approval_request_id)
    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert actions.list_actions() == []
    assert tool.runs == second_tool.runs == 0
    reopened.close()


def test_json_wrong_installation_key_after_restart_fails_closed(tmp_path):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, actions, tool, _ = _build(approval_store=store)
    pending = _invoke(executor)
    store.close()

    reopened = JsonFileApprovalStore(directory)
    _, wrong_authority, _, _, _, _ = _build(
        approval_store=reopened,
        action_store=actions,
        key=StaticInstallationKeyProvider(b"z" * 32),
    )
    with pytest.raises(ApprovalUnavailableError):
        wrong_authority.record_decision(
            pending.approval_request_id, HumanApprovalDecision.APPROVE)
    assert reopened.load(pending.approval_request_id).state is ApprovalState.PENDING
    assert actions.list_actions() == []
    assert tool.runs == 0
    reopened.close()


def test_json_reader_uses_single_bounded_handle(monkeypatch, tmp_path):
    directory = tmp_path / "approvals"
    store = JsonFileApprovalStore(directory)
    executor, _, _, _, _, _ = _build(approval_store=store)
    pending = _invoke(executor)

    def forbidden_read_bytes(_):
        raise AssertionError("approval records must not use Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    assert store.load(pending.approval_request_id).state is ApprovalState.PENDING
    store.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_forked_child_cannot_use_inherited_approval_store_or_authority(tmp_path):
    store = JsonFileApprovalStore(tmp_path / "approvals")
    executor, authority, _, _, _, _ = _build(approval_store=store)
    pending = _invoke(executor)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - POSIX child
        os.close(read_fd)
        outcomes = []
        for operation in (
            lambda: store.load(pending.approval_request_id),
            lambda: authority.record_decision(
                pending.approval_request_id, HumanApprovalDecision.APPROVE),
        ):
            try:
                operation()
                outcomes.append("unsafe")
            except Exception:
                outcomes.append("blocked")
        os.write(write_fd, ",".join(outcomes).encode("ascii"))
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    observed = os.read(read_fd, 64).decode("ascii")
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert status == 0
    assert observed == "blocked,blocked"
    store.close()


def test_sensitive_description_and_payload_are_absent_from_public_surfaces(tmp_path):
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    approval_store = JsonFileApprovalStore(tmp_path / "approvals")
    payload_store = InMemoryProtectedActionPayloadStore(KEY, id_factory=ids)
    tool = MutationTool()
    logger = RecordingLogger()
    executor, authority, _, actions, _, gate = _build(
        tool=tool,
        adapter=MutationAdapter(tool, protected=True),
        approval_store=approval_store,
        payload_store=payload_store,
        clock=clock,
        ids=ids,
        logger=logger,
    )
    pending, approved = _approve_pending(executor, authority)
    assert gate.prompts[0].description.endswith(SENSITIVE)
    durable_approval = (tmp_path / "approvals" /
                        f"{pending.approval_request_id}.json").read_text(
                            encoding="utf-8")
    public = json.dumps({
        "approval": durable_approval,
        "action": actions.load(approved.action_id).to_dict(),
        "invocation": approved.to_dict(),
        "logs": logger.events,
    }, default=str)
    assert SENSITIVE not in public
    assert "raw-provider-output" not in public
    approval_store.close()


def test_human_wait_and_tool_run_hold_no_approval_store_lock():
    approvals = InMemoryApprovalStore()
    gate_observed = []
    gate = DecisionGate(
        HumanApprovalDecision.APPROVE,
        hook=lambda prompt: gate_observed.append(approvals.list_requests()),
    )
    tool_observed = []
    tool = MutationTool(
        run_hook=lambda args, context: (
            tool_observed.append(approvals.list_requests())
            or ToolResult.success("raw")))
    executor, _, _, _, _, _ = _build(
        approval_store=approvals, gate=gate, tool=tool,
        adapter=MutationAdapter(tool))
    completed = _invoke(executor)
    assert completed.ok
    assert gate_observed == [[completed.approval_request_id]]
    assert tool_observed == [[completed.approval_request_id]]


def test_multi_capability_approval_binds_complete_exact_sorted_set():
    tool = MutationTool(capabilities=(Capability.FS_WRITE, Capability.FS_DELETE))
    executor, authority, approvals, actions, _, _ = _build(
        tool=tool, adapter=MutationAdapter(tool))
    pending = _invoke(executor)
    request = approvals.load(pending.approval_request_id)
    assert request.mutation_capabilities == ("fs.delete", "fs.write")
    authority.record_decision(
        request.request_id, HumanApprovalDecision.APPROVE)
    completed = _invoke(executor, approval_request_id=request.request_id)
    assert completed.ok
    assert actions.load(completed.action_id).mutation_capabilities == (
        "fs.delete", "fs.write")


def _seed_retry(store: InMemoryActionStore, *, approval_request_id=None):
    authority = AuthorityReceipt(
        receipt_id="auth_fffffffffff1",
        binding_digest="1" * 64,
        policy_fingerprint="2" * 64,
        scope_ref="historical:authority",
        authorized_at=NOW,
        approval_request_id=approval_request_id,
    )
    record = new_action(
        action_id="act_fffffffffff1",
        action_kind="test.durable-approval.v1",
        tool_name="approve_mutation",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=(),
        effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
        intent_digest="a" * 64,
        intent_ref=None,
        target_category="test.target",
        target_fingerprint=None,
        provenance=ActionProvenance(invocation_id="inv_fffffffffff1"),
        authority=authority,
        clock=FixedClock(NOW),
    )
    created = store.create(record)
    claimed = store.claim_attempt(
        created.action_id, created.version,
        attempt_id="aat_fffffffffff1",
        invocation_id="inv_fffffffffff1",
        owner_id="process:historical",
        owner_epoch=1,
        transition_id="atx_fffffffffff1",
    )
    unknown = claimed.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id="atx_fffffffffff2",
        at=NOW,
        attempt_status=ActionAttemptStatus.INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=ReconciliationMetadata(
            disposition=ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED),
    )
    store.compare_and_swap(claimed.action_id, claimed.version, unknown)
    retry = unknown.transition(
        ActionState.AUTHORIZED,
        transition_id="atx_fffffffffff3",
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
    )
    return store.compare_and_swap(unknown.action_id, unknown.version, retry)


def test_retry_requires_current_approval_and_historical_receipt_never_grants_it():
    clock = FixedClock(NOW)
    actions = InMemoryActionStore(clock=clock)
    retry = _seed_retry(actions, approval_request_id="apr_000000000099")
    executor, authority, approvals, _, tool, _ = _build(
        action_store=actions, clock=clock)

    pending = _invoke(executor, action_id=retry.action_id)
    assert pending.result.metadata["kind"] == "approval_pending"
    assert pending.approval_request_id != retry.authority.approval_request_id
    assert actions.load(retry.action_id) == retry
    assert tool.runs == 0

    authority.record_decision(
        pending.approval_request_id, HumanApprovalDecision.APPROVE)
    completed = executor.invoke(
        "approve_mutation",
        {"payload": SENSITIVE},
        action_context=ActionInvocationContext(action_id=retry.action_id),
        approval_context=ApprovalInvocationContext(
            pending.approval_request_id),
    )
    assert completed.ok, completed.to_dict()
    assert tool.runs == 1
    assert approvals.load(pending.approval_request_id).state is (
        ApprovalState.CONSUMED)
    # Schema v1 preserves the immutable historical receipt; retry approval is
    # enforced at the execution seam but intentionally not forged into history.
    assert actions.load(retry.action_id).authority == retry.authority


def test_retry_rejects_approval_minted_for_another_same_shaped_action():
    clock = FixedClock(NOW)
    actions = InMemoryActionStore(clock=clock)
    retry = _seed_retry(actions)
    executor, authority, approvals, _, tool, _ = _build(
        action_store=actions, clock=clock)

    unrelated = _invoke(executor)
    assert unrelated.result.metadata["kind"] == "approval_pending"
    unrelated_request = approvals.load(unrelated.approval_request_id)
    assert unrelated_request.action_id != retry.action_id
    authority.record_decision(
        unrelated.approval_request_id, HumanApprovalDecision.APPROVE)

    rejected = executor.invoke(
        "approve_mutation",
        {"payload": SENSITIVE},
        action_context=ActionInvocationContext(action_id=retry.action_id),
        approval_context=ApprovalInvocationContext(
            unrelated.approval_request_id),
    )

    assert rejected.result.metadata["kind"] == "approval_unavailable"
    assert approvals.load(unrelated.approval_request_id).state is (
        ApprovalState.APPROVED)
    assert actions.load(retry.action_id) == retry
    assert tool.runs == 0


def test_automatically_allowed_action_fabricates_no_approval_reference():
    policy = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.AUTONOMOUS))
    executor, _, approvals, actions, tool, _ = _build(policy=policy)
    completed = _invoke(executor)
    assert completed.ok
    assert completed.approval_request_id is None
    assert actions.load(completed.action_id).authority.approval_request_id is None
    assert approvals.list_requests() == []
    assert tool.runs == 1


def test_schema_versions_and_closed_approval_states_remain_explicit():
    assert APPROVAL_SCHEMA_VERSION == 1
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1
    assert PROTECTED_STORAGE_SCHEMA_VERSION == 1
    assert set(ApprovalState) == {
        ApprovalState.PENDING,
        ApprovalState.APPROVED,
        ApprovalState.DENIED,
        ApprovalState.CONSUMED,
        ApprovalState.EXPIRED,
    }


def test_focused_suite_uses_no_sleep_or_live_network():
    text = Path(__file__).read_text(encoding="utf-8")
    assert "time" + ".sleep" not in text
    assert "requests" + "." not in text
    assert "httpx" + "." not in text
