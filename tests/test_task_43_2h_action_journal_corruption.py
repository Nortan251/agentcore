from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION,
    INSTALLATION_KEY_SCHEMA_VERSION,
    PROTECTED_STORAGE_SCHEMA_VERSION,
    ActionAdapterRegistry,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionDomainHealthState,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionOutcome,
    ActionProvenance,
    ActionState,
    ActionStoreOwnershipError,
    ActionTransitionProof,
    AuthorityReceipt,
    DuplicateRiskAcknowledgement,
    EffectVerdict,
    IdempotencyContract,
    InMemoryActionStore,
    InMemoryEvidenceStore,
    InMemoryProtectedActionPayloadStore,
    JsonFileActionStore,
    MAX_ACTION_RECORD_BYTES,
    NormalizedActionEffect,
    PreparedActionIntent,
    ReconciliationDisposition,
    ReconciliationMetadata,
    StaticInstallationKeyProvider,
    new_action,
)
from agentcore.approvals import APPROVAL_SCHEMA_VERSION
from agentcore.config.schema import AgentConfig, LoggingConfig, PermissionConfig
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
from agentcore.recovery import ActionRecoveryCoordinator, ActionRecoveryStatus
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


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SECRET = "task-43.2h-protected-secret-sentinel"
KEY = StaticInstallationKeyProvider(b"h" * 32)


class DeterministicIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        number = self.counts.get(prefix, 0) + 1
        self.counts[prefix] = number
        return f"{prefix}_{number:012x}"


class RecordingPolicy(PermissionPolicy):
    def __init__(self) -> None:
        super().__init__(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
        self.calls: list[frozenset[Capability]] = []

    def check(self, capabilities, *, description=""):
        self.calls.append(frozenset(capabilities))
        return super().check(capabilities, description=description)


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def bind(self, **fields):
        return self

    def info(self, event, **fields):
        self.events.append((event, fields))

    def debug(self, event, **fields):
        self.events.append((event, fields))

    def warning(self, event, **fields):
        self.events.append((event, fields))

    def error(self, event, **fields):
        self.events.append((event, fields))


class MutationTool(Tool):
    def __init__(
        self,
        name: str = "mutate",
        capability: Capability = Capability.FS_WRITE,
    ) -> None:
        self._name = name
        self._capability = capability
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            self._name,
            "Trusted mutation used by the corruption tests.",
            version="1.0.0",
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=(self._capability,),
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.runs += 1
        return ToolResult.success("physical mutation completed")


class ReadTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "read",
            "Independent read used by the corruption tests.",
            parameters=(ToolParameter("query", ParamType.STRING),),
            capabilities=(Capability.FS_READ,),
            read_only=True,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.runs += 1
        return ToolResult.success("read-ok")


class MutationAdapter:
    def __init__(
        self,
        tool: MutationTool,
        *,
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
    ) -> None:
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind=f"test.{tool.spec.name}.v1",
            eligibility=eligibility,
            effect_class=(
                ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE
                if eligibility.requires_journal
                else None
            ),
            reconciliation_capabilities=(
                frozenset({Capability.FS_READ})
                if eligibility.requires_journal
                else frozenset()
            ),
        )

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="a" * 64,
            target_category="test.target",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The mutation was confirmed.",
        )


def _runtime(
    store,
    *,
    payload_store=None,
    evidence_store=None,
    max_startup_actions=256,
):
    ids = DeterministicIds()
    mutation = MutationTool()
    read = ReadTool()
    control = MutationTool("control", Capability.APP_CONTROL)
    registry = ToolRegistry()
    registry.register_all([mutation, read, control])
    adapters = ActionAdapterRegistry()
    adapters.register(mutation, MutationAdapter(mutation))
    adapters.register(
        control,
        MutationAdapter(
            control,
            eligibility=ActionJournalEligibility.EPHEMERAL_CONTROL,
        ),
    )
    policy = RecordingPolicy()
    logger = RecordingLogger()
    executor = ToolExecutor(
        registry,
        policy,
        action_store=store,
        action_adapters=adapters,
        protected_payload_store=payload_store,
        evidence_store=evidence_store,
        clock=FixedClock(NOW),
        id_factory=ids,
        action_owner_id="process:test",
        logger=logger,
    )
    coordinator = ActionRecoveryCoordinator(
        store,
        executor,
        clock=FixedClock(NOW),
        id_factory=ids,
        owner_id="recovery:test",
        max_startup_actions=max_startup_actions,
    )
    return {
        "executor": executor,
        "coordinator": coordinator,
        "mutation": mutation,
        "read": read,
        "control": control,
        "policy": policy,
        "logger": logger,
        "store": store,
    }


def _invoke_mutation(runtime):
    return runtime["executor"].invoke(
        "mutate",
        {"payload": SECRET},
        action_context=ActionInvocationContext(
            deduplication_scope="test:runtime",
            deduplication_key="step:one",
        ),
    )


def _new_record(
    number: int,
    *,
    intent_ref=None,
    intent_digest="a" * 64,
    deduplication_scope=None,
    deduplication_key=None,
    effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
    idempotency=None,
    predecessor_action_id=None,
    acknowledgement=None,
):
    return new_action(
        action_id=f"act_{number:012x}",
        action_kind="test.corruption-mutation.v1",
        tool_name="mutate",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=("fs.read",),
        effect_class=effect_class,
        intent_digest=intent_digest,
        intent_ref=intent_ref,
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
        deduplication_scope=deduplication_scope,
        deduplication_key=deduplication_key,
        idempotency=idempotency,
        predecessor_action_id=predecessor_action_id,
        duplicate_risk_acknowledgement=acknowledgement,
        clock=FixedClock(NOW),
    )


def _seed(store, number: int, state: ActionState, **record_kwargs):
    record = store.create(_new_record(number, **record_kwargs))
    if state is ActionState.AUTHORIZED:
        return record
    claimed = store.claim_attempt(
        record.action_id,
        record.version,
        attempt_id=f"aat_{number:012x}",
        invocation_id=f"inv_{number:012x}",
        owner_id="process:prior",
        owner_epoch=1,
        transition_id=f"atx_{number:012x}",
    )
    if state is ActionState.IN_FLIGHT:
        return claimed
    if state is ActionState.RECONCILE_REQUIRED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 100:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            reconciliation=ReconciliationMetadata(
                disposition=ReconciliationDisposition.PROBE_LATER),
        )
    elif state is ActionState.SUCCEEDED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 100:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_APPLIED,
            terminal_outcome=ActionOutcome(
                verdict=EffectVerdict.CONFIRMED_APPLIED,
                result_class="test.applied",
                summary="The effect was confirmed.",
            ),
        )
    elif state is ActionState.FAILED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 100:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            terminal_outcome=ActionOutcome(
                verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
                result_class="test.not-applied",
                summary="No effect was confirmed.",
            ),
        )
    elif state is ActionState.CANCELLED:
        candidate = claimed.transition(
            state,
            transition_id=f"atx_{number + 100:012x}",
            at=NOW,
            attempt_status=ActionAttemptStatus.NOT_INVOKED,
            proof=ActionTransitionProof.LIVE_OWNER_NOT_INVOKED,
        )
    else:  # pragma: no cover - helper misuse
        raise AssertionError(state)
    return store.compare_and_swap(
        claimed.action_id, claimed.version, candidate)


def _json_path(tmp_path: Path, record) -> Path:
    return tmp_path / f"{record.action_id}.json"


def _rewrite(path: Path, mutate) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _assert_quarantined_without_effect(runtime):
    report = runtime["coordinator"].recover_startup()
    invocation = _invoke_mutation(runtime)
    assert report.quarantined
    assert report.error_category == "journal_corrupt"
    assert runtime["executor"].action_domain_health.state is (
        ActionDomainHealthState.QUARANTINED)
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_journal_quarantined"
    assert runtime["mutation"].runs == 0
    return report, invocation


def test_pending_startup_gate_and_healthy_empty_startup():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    runtime = _runtime(store)

    pending = _invoke_mutation(runtime)
    assert not pending.ok
    assert pending.result.metadata == {
        "kind": "action_journal_quarantined",
        "quarantine_category": "startup_validation_incomplete",
    }
    assert store.list_actions() == []
    assert runtime["mutation"].runs == 0

    report = runtime["coordinator"].recover_startup()
    assert not report.quarantined
    assert report.items == ()
    assert runtime["executor"].action_domain_health.state is (
        ActionDomainHealthState.HEALTHY)
    assert _invoke_mutation(runtime).ok
    assert runtime["mutation"].runs == 1


def test_healthy_startup_truth_classification_and_no_automatic_effect():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    waiting = _seed(store, 1, ActionState.AUTHORIZED)
    owner_lost = _seed(store, 2, ActionState.IN_FLIGHT)
    unresolved = _seed(store, 3, ActionState.RECONCILE_REQUIRED)
    terminals = [
        _seed(store, 4, ActionState.SUCCEEDED),
        _seed(store, 5, ActionState.FAILED),
        _seed(store, 6, ActionState.CANCELLED),
    ]
    terminal_before = {record.action_id: record for record in terminals}
    runtime = _runtime(store)

    report = runtime["coordinator"].recover_startup()

    statuses = {item.action_id: item.status for item in report.items}
    assert statuses[waiting.action_id] is ActionRecoveryStatus.WAITING_OWNER
    assert statuses[owner_lost.action_id] is (
        ActionRecoveryStatus.RECONCILE_REQUIRED)
    assert statuses[unresolved.action_id] is (
        ActionRecoveryStatus.RECONCILE_REQUIRED)
    recovered = store.load(owner_lost.action_id)
    assert recovered.state is ActionState.RECONCILE_REQUIRED
    assert recovered.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert store.load(unresolved.action_id) == unresolved
    assert {key: store.load(key) for key in terminal_before} == terminal_before
    assert report.terminal_count == 3
    assert runtime["mutation"].runs == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "malformed_json",
        "duplicate_json_key",
        "unknown_schema",
        "malformed_schema",
        "invalid_state",
        "invalid_field_type",
        "missing_field",
        "unknown_field",
        "malformed_timestamp",
        "invalid_version_history",
        "illegal_transition",
        "identity_mismatch",
        "oversized_record",
    ],
)
def test_json_representation_schema_and_history_corruption_is_quarantined(
    tmp_path,
    corruption,
):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    state = (
        ActionState.IN_FLIGHT
        if corruption == "illegal_transition"
        else ActionState.AUTHORIZED
    )
    record = _seed(store, 10, state)
    path = _json_path(tmp_path, record)
    if corruption == "malformed_json":
        path.write_bytes(b'{"secret":"' + SECRET.encode() + b'"')
    elif corruption == "duplicate_json_key":
        encoded = path.read_text(encoding="utf-8")
        path.write_text(
            encoded[:-1] + ',"schema_version":1}', encoding="utf-8")
    elif corruption == "unknown_schema":
        _rewrite(path, lambda data: data.__setitem__("schema_version", 999))
    elif corruption == "malformed_schema":
        _rewrite(path, lambda data: data.__setitem__("schema_version", "1"))
    elif corruption == "invalid_state":
        _rewrite(path, lambda data: data.__setitem__("state", "CORRUPT"))
    elif corruption == "invalid_field_type":
        _rewrite(path, lambda data: data.__setitem__("attempts", {}))
    elif corruption == "missing_field":
        _rewrite(path, lambda data: data.pop("authority"))
    elif corruption == "unknown_field":
        _rewrite(path, lambda data: data.__setitem__("raw_secret", SECRET))
    elif corruption == "malformed_timestamp":
        _rewrite(path, lambda data: data.__setitem__("updated_at", "tomorrow"))
    elif corruption == "invalid_version_history":
        _rewrite(path, lambda data: data.__setitem__("version", 7))
    elif corruption == "illegal_transition":
        _rewrite(
            path,
            lambda data: data["transitions"][0].__setitem__(
                "from_state", "FAILED"),
        )
    elif corruption == "identity_mismatch":
        _rewrite(path, lambda data: data.__setitem__(
            "action_id", "act_00000000000b"))
    elif corruption == "oversized_record":
        path.write_bytes(b"x" * (MAX_ACTION_RECORD_BYTES + 1))
    before = path.read_bytes()
    runtime = _runtime(store)

    _, invocation = _assert_quarantined_without_effect(runtime)

    assert path.read_bytes() == before
    surfaces = repr((invocation.result.to_dict(), runtime["logger"].events))
    assert SECRET not in surfaces
    store.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_deduplication",
        "duplicate_external_idempotency",
        "duplicate_attempt",
        "missing_predecessor",
        "predecessor_cycle",
    ],
)
def test_cross_record_identity_corruption_is_quarantined(corruption):
    store = InMemoryActionStore(clock=FixedClock(NOW))
    if corruption == "duplicate_external_idempotency":
        first = _seed(
            store,
            20,
            ActionState.AUTHORIZED,
            effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
            idempotency=IdempotencyContract(
                key="remote-key-one",
                contract="provider.v1",
                scope="account:one",
            ),
        )
        second = _seed(
            store,
            21,
            ActionState.AUTHORIZED,
            effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
            idempotency=IdempotencyContract(
                key="remote-key-two",
                contract="provider.v2",
                scope="account:one",
            ),
        )
        store._data[second.action_id]["idempotency"]["key"] = (
            first.idempotency.key)
    elif corruption == "duplicate_attempt":
        first = _seed(store, 20, ActionState.IN_FLIGHT)
        second = _seed(store, 21, ActionState.IN_FLIGHT)
        duplicate_id = first.attempts[-1].attempt_id
        store._data[second.action_id]["attempts"][0]["attempt_id"] = duplicate_id
        store._data[second.action_id]["transitions"][0]["attempt_id"] = duplicate_id
    else:
        first = _seed(
            store,
            20,
            ActionState.AUTHORIZED,
            deduplication_scope="workflow:cross",
            deduplication_key="step:first",
        )
        second = _seed(
            store,
            21,
            ActionState.AUTHORIZED,
            deduplication_scope="workflow:cross",
            deduplication_key="step:second",
        )
        if corruption == "duplicate_deduplication":
            store._data[second.action_id]["deduplication_key"] = (
                first.deduplication_key)
        elif corruption == "missing_predecessor":
            store._data[second.action_id]["predecessor_action_id"] = (
                "act_000000000999")
        elif corruption == "predecessor_cycle":
            store._data[first.action_id]["predecessor_action_id"] = (
                second.action_id)
            store._data[second.action_id]["predecessor_action_id"] = (
                first.action_id)
    runtime = _runtime(store)
    _assert_quarantined_without_effect(runtime)


@pytest.mark.parametrize(
    "reused_field",
    ["acknowledgement_id", "approval_request_id"],
)
def test_duplicate_risk_ownership_corruption_is_quarantined(reused_field):
    store = InMemoryActionStore(clock=FixedClock(NOW))
    predecessor = _seed(store, 30, ActionState.RECONCILE_REQUIRED)
    acknowledgements = [
        DuplicateRiskAcknowledgement(
            acknowledgement_id=f"dra_{number:012x}",
            predecessor_action_id=predecessor.action_id,
            approval_request_id=f"apr_risk_{number}",
            binding_digest=("d" if number == 31 else "e") * 64,
            acknowledged_at=NOW,
        )
        for number in (31, 32)
    ]
    first = store.create(_new_record(
        31,
        predecessor_action_id=predecessor.action_id,
        acknowledgement=acknowledgements[0],
    ))
    second = store.create(_new_record(
        32,
        predecessor_action_id=predecessor.action_id,
        acknowledgement=acknowledgements[1],
    ))
    store._data[second.action_id]["duplicate_risk_acknowledgement"][
        reused_field
    ] = store._data[first.action_id]["duplicate_risk_acknowledgement"][
        reused_field
    ]
    runtime = _runtime(store)
    _assert_quarantined_without_effect(runtime)


@pytest.mark.parametrize(
    "failure",
    ["missing", "corrupt", "wrong_action", "wrong_digest", "malformed_ref"],
)
def test_nonterminal_protected_payload_dependency_quarantines(failure):
    ids = DeterministicIds()
    payloads = InMemoryProtectedActionPayloadStore(KEY, id_factory=ids)
    action_id = "act_000000000028"
    stored = payloads.create(action_id, {"body": SECRET})
    if failure == "wrong_action":
        stored = payloads.create(
            "act_000000000029", {"body": "other"})
    intent_ref = stored.reference
    intent_digest = stored.intent_digest
    if failure == "malformed_ref":
        intent_ref = "pap_bad"
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = store.create(_new_record(
        40,
        intent_ref=intent_ref,
        intent_digest=("f" * 64 if failure == "wrong_digest" else intent_digest),
    ))
    if failure == "missing":
        payloads._objects.pop(stored.reference)
    elif failure == "corrupt":
        payloads._objects[stored.reference] = (
            b'{"secret":"' + SECRET.encode() + b'"}')
    runtime = _runtime(store, payload_store=payloads)

    _, invocation = _assert_quarantined_without_effect(runtime)

    assert SECRET not in repr((invocation.result.to_dict(), runtime["logger"].events))
    assert store._data[record.action_id] == record.to_dict()


def _record_with_attempt_evidence(
    store,
    evidence_store,
    *,
    evidence_action_id="act_000000000032",
    evidence_attempt_id="aat_000000000032",
    evidence_digest="a" * 64,
):
    record = store.create(_new_record(50))
    claimed = store.claim_attempt(
        record.action_id,
        record.version,
        attempt_id="aat_000000000032",
        invocation_id="inv_000000000032",
        owner_id="process:prior",
        owner_epoch=1,
        transition_id="atx_000000000032",
    )
    evidence = evidence_store.create_attempt_evidence(
        evidence_action_id,
        evidence_attempt_id,
        evidence_digest,
        {"body": SECRET},
    )
    candidate = claimed.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id="atx_000000000132",
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        evidence_refs=(evidence.reference,),
        reconciliation=ReconciliationMetadata(
            disposition=ReconciliationDisposition.PROBE_LATER),
    )
    return store.compare_and_swap(
        claimed.action_id, claimed.version, candidate), evidence


@pytest.mark.parametrize(
    "failure",
    ["missing", "corrupt", "wrong_action", "wrong_attempt", "wrong_digest"],
)
def test_nonterminal_attempt_evidence_dependency_quarantines(failure):
    evidence = InMemoryEvidenceStore(KEY, id_factory=DeterministicIds())
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record, stored = _record_with_attempt_evidence(
        store,
        evidence,
        evidence_action_id=(
            "act_000000000033" if failure == "wrong_action"
            else "act_000000000032"
        ),
        evidence_attempt_id=(
            "aat_000000000033" if failure == "wrong_attempt"
            else "aat_000000000032"
        ),
        evidence_digest=("f" * 64 if failure == "wrong_digest" else "a" * 64),
    )
    if failure == "missing":
        evidence._objects.pop(stored.reference)
    elif failure == "corrupt":
        evidence._objects[stored.reference] = (
            b'{"secret":"' + SECRET.encode() + b'"}')
    runtime = _runtime(store, evidence_store=evidence)

    _, invocation = _assert_quarantined_without_effect(runtime)

    assert store._data[record.action_id] == record.to_dict()
    assert SECRET not in repr((invocation.result.to_dict(), runtime["logger"].events))


def test_nonterminal_reconciliation_claim_evidence_mismatch_quarantines():
    evidence = InMemoryEvidenceStore(KEY, id_factory=DeterministicIds())
    store = InMemoryActionStore(clock=FixedClock(NOW))
    record = store.create(_new_record(60))
    claimed = store.claim_attempt(
        record.action_id,
        record.version,
        attempt_id="aat_00000000003c",
        invocation_id="inv_00000000003c",
        owner_id="process:prior",
        owner_epoch=1,
        transition_id="atx_00000000003c",
    )
    unresolved = claimed.transition(
        ActionState.RECONCILE_REQUIRED,
        transition_id="atx_00000000013c",
        at=NOW,
        attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        reconciliation=ReconciliationMetadata(
            disposition=ReconciliationDisposition.PROBE_LATER,
            claim_epoch=1,
        ),
    )
    unresolved = store.compare_and_swap(
        claimed.action_id, claimed.version, unresolved)
    wrong = evidence.create_reconciliation_evidence(
        unresolved.action_id,
        2,
        unresolved.intent_digest,
        {"body": SECRET},
    )
    retry_ready = unresolved.transition(
        ActionState.AUTHORIZED,
        transition_id="atx_00000000023c",
        at=NOW,
        effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
        proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
        evidence_refs=(wrong.reference,),
    )
    retry_ready = store.compare_and_swap(
        unresolved.action_id, unresolved.version, retry_ready)
    runtime = _runtime(store, evidence_store=evidence)

    _assert_quarantined_without_effect(runtime)
    assert store._data[retry_ready.action_id] == retry_ready.to_dict()


def test_terminal_action_does_not_require_historical_protected_blob():
    payloads = InMemoryProtectedActionPayloadStore(
        KEY, id_factory=DeterministicIds())
    action_id = "act_000000000046"
    stored = payloads.create(action_id, {"body": SECRET})
    store = InMemoryActionStore(clock=FixedClock(NOW))
    terminal = _seed(
        store,
        70,
        ActionState.SUCCEEDED,
        intent_ref=stored.reference,
        intent_digest=stored.intent_digest,
    )
    payloads._objects.pop(stored.reference)
    runtime = _runtime(store, payload_store=payloads)

    report = runtime["coordinator"].recover_startup()

    assert not report.quarantined
    assert report.terminal_count == 1
    assert store.load(terminal.action_id) == terminal


@pytest.mark.parametrize("store_kind", ["memory", "json"])
def test_startup_bound_quarantines_without_partial_enable_or_recovery(
    tmp_path,
    store_kind,
):
    store = (
        InMemoryActionStore(clock=FixedClock(NOW))
        if store_kind == "memory"
        else JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    )
    owner_lost = _seed(store, 80, ActionState.IN_FLIGHT)
    _seed(store, 81, ActionState.AUTHORIZED)
    _seed(store, 82, ActionState.AUTHORIZED)
    runtime = _runtime(store, max_startup_actions=2)

    report = runtime["coordinator"].recover_startup()
    blocked = _invoke_mutation(runtime)

    assert report.quarantined
    assert report.error_category == "startup_scan_bound_exceeded"
    assert blocked.result.metadata["quarantine_category"] == (
        "startup_scan_bound_exceeded")
    assert store.load(owner_lost.action_id) == owner_lost
    assert runtime["mutation"].runs == 0
    assert len(store.list_actions()) == 3
    store.close()


@pytest.mark.parametrize("corrupt_number", [90, 92])
def test_corruption_order_never_partially_classifies_or_enables(
    tmp_path,
    corrupt_number,
):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    first = _seed(store, 90, ActionState.IN_FLIGHT)
    _seed(store, 91, ActionState.AUTHORIZED)
    if corrupt_number == 92:
        corrupt = _seed(store, 92, ActionState.AUTHORIZED)
    else:
        corrupt = first
    path = _json_path(tmp_path, corrupt)
    path.write_text("{", encoding="utf-8")
    runtime = _runtime(store)

    _assert_quarantined_without_effect(runtime)

    if corrupt.action_id != first.action_id:
        assert store._read_unlocked(
            _json_path(tmp_path, first), first.action_id) == first
    store.close()


def test_domain_change_between_scan_and_enable_quarantines():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))

    class ChangingStore:
        def __init__(self) -> None:
            self.audits = 0

        def __getattr__(self, name):
            return getattr(underlying, name)

        def audit(self, *, maximum_records):
            self.audits += 1
            if self.audits == 2:
                underlying.create(_new_record(95))
            return underlying.audit(maximum_records=maximum_records)

    store = ChangingStore()
    runtime = _runtime(store)

    _assert_quarantined_without_effect(runtime)

    assert underlying.list_actions() == ["act_00000000005f"]
    assert runtime["mutation"].runs == 0


def test_quarantine_allows_real_read_and_ephemeral_control_but_stays_sticky(
    tmp_path,
):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    record = _seed(store, 100, ActionState.AUTHORIZED)
    path = _json_path(tmp_path, record)
    path.write_text("{", encoding="utf-8")
    runtime = _runtime(store)
    report = runtime["coordinator"].recover_startup()

    read = runtime["executor"].invoke("read", {"query": "safe"})
    control = runtime["executor"].invoke("control", {"payload": "safe"})
    path.unlink()
    read_again = runtime["executor"].invoke("read", {"query": "safe"})
    blocked = _invoke_mutation(runtime)

    assert report.quarantined
    assert read.ok and read_again.ok and runtime["read"].runs == 2
    assert control.ok and runtime["control"].runs == 1
    assert frozenset({Capability.FS_READ}) in runtime["policy"].calls
    assert frozenset({Capability.FS_WRITE}) not in runtime["policy"].calls
    assert not blocked.ok and runtime["mutation"].runs == 0
    assert runtime["executor"].action_domain_health.state is (
        ActionDomainHealthState.QUARANTINED)
    assert runtime["coordinator"].recover_startup() is report
    assert not runtime["executor"]._mark_action_startup_healthy(store)
    second = ActionRecoveryCoordinator(
        store,
        runtime["executor"],
        clock=FixedClock(NOW),
        id_factory=DeterministicIds(),
        owner_id="recovery:replacement",
    ).recover_startup()
    assert second.quarantined
    assert runtime["executor"].action_domain_health.state is (
        ActionDomainHealthState.QUARANTINED)
    store.close()


def test_quarantine_preempts_every_journal_authority_context_and_repeats():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    runtime = _runtime(store)
    runtime["executor"].quarantine_action_mutations("journal_corrupt")

    results = [
        runtime["executor"].invoke(
            "mutate",
            {"payload": SECRET},
            action_context=ActionInvocationContext(
                action_id="act_000000000001",
                deduplication_scope="attacker:ignored",
                deduplication_key="ignored",
            ),
            approval_context=object(),
            duplicate_risk_context=object(),
            idempotency_key="provider-claim",
        )
        for _ in range(3)
    ]

    assert all(not result.ok for result in results)
    assert {result.result.metadata["kind"] for result in results} == {
        "action_journal_quarantined"}
    assert store.list_actions() == []
    assert runtime["mutation"].runs == 0


def test_store_ownership_failure_remains_categorical_and_pending():
    class OwnedElsewhereStore:
        def audit(self, *, maximum_records):
            raise ActionStoreOwnershipError(
                "action store is owned by another process")

    store = OwnedElsewhereStore()
    runtime = _runtime(store)

    with pytest.raises(ActionStoreOwnershipError):
        runtime["coordinator"].recover_startup()

    assert runtime["executor"].action_domain_health.state is (
        ActionDomainHealthState.VALIDATION_PENDING)
    blocked = _invoke_mutation(runtime)
    assert not blocked.ok
    assert runtime["mutation"].runs == 0


def test_ownership_loss_during_startup_publication_is_not_relabelled():
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    owner_lost = _seed(underlying, 104, ActionState.IN_FLIGHT)

    class OwnershipLostStore:
        def __getattr__(self, name):
            return getattr(underlying, name)

        def compare_and_swap(self, *args, **kwargs):
            raise ActionStoreOwnershipError(
                "action store is owned by another process")

    store = OwnershipLostStore()
    runtime = _runtime(store)

    with pytest.raises(ActionStoreOwnershipError):
        runtime["coordinator"].recover_startup()

    assert underlying.load(owner_lost.action_id) == owner_lost
    assert runtime["executor"].action_domain_health.state is (
        ActionDomainHealthState.VALIDATION_PENDING)
    assert runtime["mutation"].runs == 0


def test_agent_construction_degrades_without_store_or_direct_run_fallback(
    tmp_path,
):
    domain = tmp_path / "actions"
    store = JsonFileActionStore(domain, clock=FixedClock(NOW))
    record = _seed(store, 105, ActionState.AUTHORIZED)
    path = _json_path(domain, record)
    before = path.read_bytes()
    path.write_text("{", encoding="utf-8")
    corrupt = path.read_bytes()
    mutation = MutationTool()
    read = ReadTool()
    adapters = ActionAdapterRegistry()
    adapters.register(mutation, MutationAdapter(mutation))
    agent = Agent(
        AgentConfig(
            workspace_dir=str(tmp_path),
            permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
            logging=LoggingConfig(level="CRITICAL"),
        ),
        AgentDependencies(
            tools=[mutation, read],
            action_store=store,
            action_adapters=adapters,
        ),
    )
    try:
        assert agent.action_recovery_startup is not None
        assert agent.action_recovery_startup.quarantined
        assert agent.executor._action_store is store
        assert agent.executor.action_domain_health.state is (
            ActionDomainHealthState.QUARANTINED)
        assert agent.executor.invoke("read", {"query": "safe"}).ok
        blocked = agent.executor.invoke(
            "mutate",
            {"payload": SECRET},
            action_context=ActionInvocationContext(
                deduplication_scope="test:agent",
                deduplication_key="step:one",
            ),
        )
        assert not blocked.ok
        assert mutation.runs == 0
        assert path.read_bytes() == corrupt
        assert path.read_bytes() != before
    finally:
        agent.shutdown()
        store.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork required")
def test_inherited_store_and_recovery_remain_process_fenced():
    store = InMemoryActionStore(clock=FixedClock(NOW))
    runtime = _runtime(store)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - authoritative on POSIX CI
        os.close(read_fd)
        try:
            runtime["coordinator"].recover_startup()
        except ActionStoreOwnershipError:
            result = b"ownership_mismatch"
        else:
            result = b"unsafe"
        os.write(write_fd, result)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 128)
    os.close(read_fd)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid and os.waitstatus_to_exitcode(status) == 0
    assert result == b"ownership_mismatch"


def test_store_audit_is_read_only_and_unknown_schema_remains_untouched(tmp_path):
    store = JsonFileActionStore(tmp_path, clock=FixedClock(NOW))
    record = _seed(store, 110, ActionState.AUTHORIZED)
    path = _json_path(tmp_path, record)
    _rewrite(path, lambda data: data.__setitem__("schema_version", 2))
    before = {candidate.name: candidate.read_bytes()
              for candidate in tmp_path.glob("*.json")}
    runtime = _runtime(store)

    _assert_quarantined_without_effect(runtime)

    after = {candidate.name: candidate.read_bytes()
             for candidate in tmp_path.glob("*.json")}
    assert after == before
    store.close()


def test_frozen_schemas_and_exact_action_states():
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert APPROVAL_SCHEMA_VERSION == 1
    assert PROTECTED_STORAGE_SCHEMA_VERSION == 1
    assert INSTALLATION_KEY_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1
    assert {state.name for state in ActionState} == {
        "AUTHORIZED",
        "IN_FLIGHT",
        "RECONCILE_REQUIRED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }
