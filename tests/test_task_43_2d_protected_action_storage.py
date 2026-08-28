from __future__ import annotations

import json
import os
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
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionProvenance,
    ActionReconciliationResult,
    ActionState,
    AuthorityReceipt,
    EffectVerdict,
    InMemoryActionStore,
    JsonFileEvidenceStore,
    JsonFileInstallationKeyProvider,
    JsonFileProtectedActionPayloadStore,
    NormalizedActionEffect,
    PreparedActionIntent,
    PROTECTED_STORAGE_ENCRYPTED_AT_REST,
    PROTECTED_STORAGE_SCHEMA_VERSION,
    ReconciliationDisposition,
    ReconciliationMetadata,
    StaticInstallationKeyProvider,
    is_evidence_reference,
    is_protected_payload_reference,
    new_action,
)
from agentcore.action_journal.errors import (
    ActionStoreError,
    InstallationKeyError,
    ProtectedObjectAlreadyExistsError,
    ProtectedObjectCorruptError,
    ProtectedObjectNotFoundError,
    ProtectedObjectTooLargeError,
    ProtectedReferenceError,
    ProtectedStoreOwnershipError,
)
from agentcore.config.schema import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import Decision, Outcome, PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
from agentcore.recovery.action import ActionRecoveryCoordinator
from agentcore.state.models import AgentState
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


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SECRET = "task-43.2d-private-recipient@example.invalid"
BODY = "task-43.2d-private-message-body"
EVIDENCE_SECRET = "task-43.2d-private-provider-evidence"
KEY = bytes(range(32))


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


class AllowPolicy(PermissionPolicy):
    def __init__(self) -> None:
        super().__init__(PermissionConfig(level=PermissionLevel.AUTONOMOUS))

    def check(self, capabilities, *, description=""):
        return Decision(Outcome.ALLOW, "test allow")


class ProtectedTool(Tool):
    def __init__(self, *, result: ToolResult | None = None) -> None:
        self._result = result or ToolResult.success("safe-provider-result")
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "protected_mutation",
            "Trusted protected mutation test.",
            version="1.0.0",
            parameters=(
                ToolParameter("recipient", ParamType.STRING),
                ToolParameter("body", ParamType.STRING),
            ),
            capabilities=(Capability.FS_WRITE,),
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        self.runs += 1
        return self._result


class ProtectedAdapter:
    def __init__(
        self,
        tool: ProtectedTool,
        *,
        protected_payload: Mapping[str, Any] | None = None,
        normalized: NormalizedActionEffect | None = None,
        selected_ref: str | None = None,
    ) -> None:
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.protected-mutation.v1",
            eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            reconciliation_capabilities=frozenset({Capability.FS_READ}),
        )
        self._payload = protected_payload or {
            "recipient": SECRET,
            "message_body": BODY,
        }
        self._normalized = normalized or NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The protected mutation was confirmed.",
            protected_evidence=({"provider_receipt": EVIDENCE_SECRET},),
        )
        self._selected_ref = selected_ref

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="a" * 64,
            intent_ref=self._selected_ref,
            target_category="test.target",
            requires_protected_payload=True,
            protected_payload=(None if self._selected_ref else self._payload),
        )

    def normalize_effect(self, result):
        return self._normalized


def _key_provider() -> StaticInstallationKeyProvider:
    return StaticInstallationKeyProvider(KEY)


def _payload_store(tmp_path: Path, ids=None):
    return JsonFileProtectedActionPayloadStore(
        tmp_path / "payloads", _key_provider(),
        id_factory=ids or DeterministicIds(),
    )


def _evidence_store(tmp_path: Path, ids=None):
    return JsonFileEvidenceStore(
        tmp_path / "evidence", _key_provider(),
        id_factory=ids or DeterministicIds(),
    )


def _build_executor(
    tmp_path: Path,
    *,
    action_store=None,
    adapter: ProtectedAdapter | None = None,
    payload_store=...,
    evidence_store=...,
    logger=None,
    ids=None,
):
    ids = ids or DeterministicIds()
    tool = ProtectedTool()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    resolved_adapter = adapter or ProtectedAdapter(tool)
    adapters.register(tool, resolved_adapter)
    resolved_payload_store = (
        _payload_store(tmp_path, ids) if payload_store is ... else payload_store)
    resolved_evidence_store = (
        _evidence_store(tmp_path, ids) if evidence_store is ... else evidence_store)
    store = action_store or InMemoryActionStore(clock=FixedClock(NOW))
    executor = ToolExecutor(
        registry,
        AllowPolicy(),
        action_store=store,
        action_adapters=adapters,
        protected_payload_store=resolved_payload_store,
        evidence_store=resolved_evidence_store,
        clock=FixedClock(NOW),
        id_factory=ids,
        action_owner_id="process:test",
        action_owner_epoch=1,
        logger=logger or RecordingLogger(),
    )
    assert executor._mark_action_startup_healthy(store)
    return executor, store, tool, resolved_payload_store, resolved_evidence_store


def _invoke(executor: ToolExecutor, *, key="step:one"):
    return executor.invoke(
        "protected_mutation",
        {"recipient": SECRET, "body": BODY},
        action_context=ActionInvocationContext(
            deduplication_scope="test:protected",
            deduplication_key=key,
        ),
    )


def _protected_record(payload_store, *, number: int = 1):
    action_id = f"act_{number:012x}"
    stored = payload_store.create(
        action_id, {"recipient": SECRET, "message_body": BODY})
    record = new_action(
        action_kind="test.protected-mutation.v1",
        tool_name="protected_mutation",
        tool_version="1.0.0",
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        mutation_capabilities=("fs.write",),
        reconciliation_capabilities=("fs.read",),
        effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
        intent_digest=stored.intent_digest,
        intent_ref=stored.reference,
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
        action_id=action_id,
        clock=FixedClock(NOW),
        id_factory=lambda prefix: f"{prefix}_{number:012x}",
    )
    return record, stored


def test_schema_versions_and_plaintext_at_rest_contract_are_explicit():
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1
    assert PROTECTED_STORAGE_SCHEMA_VERSION == 1
    assert PROTECTED_STORAGE_ENCRYPTED_AT_REST is False


def test_installation_key_create_reopens_identically_and_missing_fails_closed(
    tmp_path,
):
    path = tmp_path / "install" / "integrity-key.json"
    provider = JsonFileInstallationKeyProvider(path)
    first = provider.create()
    assert provider.create() == first
    assert JsonFileInstallationKeyProvider(path).load() == first
    assert SECRET not in repr(first)

    path.unlink()
    with pytest.raises(InstallationKeyError, match="missing"):
        provider.load()


@pytest.mark.parametrize(
    "encoded",
    [
        b"{",
        b'{"schema_version":1,"schema_version":1,"key":"bad"}',
        b'{"schema_version":1,"key":"bad"}',
    ],
)
def test_installation_key_corruption_never_repairs_or_regenerates(tmp_path, encoded):
    path = tmp_path / "key.json"
    path.write_bytes(encoded)
    before = path.read_bytes()
    with pytest.raises(InstallationKeyError, match="corrupt"):
        JsonFileInstallationKeyProvider(path).load()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("reference", "payload", "evidence"),
    [
        ("pap_000000000001", True, False),
        ("pev_000000000001", False, True),
        ("pap_00000000000g", False, False),
        ("pap_000000000001extra", False, False),
        ("protected:payload", False, False),
        ("", False, False),
    ],
)
def test_opaque_reference_namespaces_validate_strictly(reference, payload, evidence):
    assert is_protected_payload_reference(reference) is payload
    assert is_evidence_reference(reference) is evidence


def test_payload_is_bound_to_action_purpose_content_and_key(tmp_path):
    store = _payload_store(tmp_path)
    first = store.create(
        "act_000000000001", {"recipient": SECRET, "message_body": BODY})
    assert first.intent_digest != (
        __import__("hashlib").sha256(SECRET.encode()).hexdigest())

    with pytest.raises(ProtectedObjectCorruptError, match="binding"):
        store.load(first.reference, action_id="act_000000000002")
    with pytest.raises(ProtectedReferenceError):
        store.load("pev_000000000001", action_id=first.action_id)
    with pytest.raises(ProtectedObjectCorruptError, match="intent binding"):
        store.load(
            first.reference,
            action_id=first.action_id,
            expected_intent_digest="f" * 64,
        )

    other_key = JsonFileProtectedActionPayloadStore(
        tmp_path / "payloads", StaticInstallationKeyProvider(b"x" * 32))
    with pytest.raises(ProtectedObjectCorruptError, match="integrity"):
        other_key.load(first.reference, action_id=first.action_id)


def test_payload_atomic_reopen_and_immutable_detached_reads(tmp_path):
    ids = DeterministicIds()
    directory = tmp_path / "payloads"
    first_store = JsonFileProtectedActionPayloadStore(
        directory, _key_provider(), id_factory=ids)
    source = {"recipient": SECRET, "nested": {"items": [BODY]}}
    stored = first_store.create("act_000000000001", source)
    source["recipient"] = "changed"
    source["nested"]["items"].append("changed")
    first_store.close()

    reopened = JsonFileProtectedActionPayloadStore(
        directory, _key_provider(), id_factory=ids)
    loaded = reopened.load(stored.reference, action_id=stored.action_id)
    assert loaded == stored
    assert loaded.payload == {
        "recipient": SECRET,
        "nested": {"items": [BODY]},
    }
    assert list(directory.glob("*.tmp")) == []


def test_payload_reference_is_immutable_and_duplicate_create_rejected(tmp_path):
    ids = lambda prefix: f"{prefix}_000000000001"
    store = _payload_store(tmp_path, ids)
    store.create("act_000000000001", {"value": SECRET})
    with pytest.raises(ProtectedObjectAlreadyExistsError):
        store.create("act_000000000001", {"value": "replacement"})


def test_oversized_and_credential_bearing_objects_are_rejected_without_secret_leak(
    tmp_path,
):
    store = _payload_store(tmp_path)
    with pytest.raises(ProtectedObjectTooLargeError) as oversized:
        store.create("act_000000000001", {"content": "x" * 1_100_000})
    assert SECRET not in str(oversized.value)

    credential_keys = (
        "access_token",
        "provider_api_token",
        "private_key",
        "auth",
        "client_secret",
        "credentials",
    )
    for number, key in enumerate(credential_keys, start=2):
        with pytest.raises(ProtectedObjectCorruptError) as credential:
            store.create(f"act_{number:012x}", {key: SECRET})
        assert SECRET not in str(credential.value)


def test_oversized_text_is_rejected_before_json_serialization(tmp_path, monkeypatch):
    import agentcore.action_journal.protected_storage as protected_storage

    store = _payload_store(tmp_path)

    def serialization_would_be_too_late(*args, **kwargs):
        raise AssertionError("oversized content reached json.dumps")

    monkeypatch.setattr(protected_storage.json, "dumps", serialization_would_be_too_late)
    with pytest.raises(ProtectedObjectTooLargeError):
        store.create("act_000000000001", {"content": "x" * 1_100_000})


@pytest.mark.parametrize(
    "replacement",
    [
        b"{",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1}',
    ],
)
def test_partial_duplicate_key_and_malformed_payload_files_fail_closed(
    tmp_path, replacement,
):
    store = _payload_store(tmp_path)
    stored = store.create("act_000000000001", {"value": SECRET})
    path = tmp_path / "payloads" / f"{stored.reference}.json"
    path.write_bytes(replacement)
    with pytest.raises(ProtectedObjectCorruptError):
        store.load(stored.reference, action_id=stored.action_id)


def test_missing_payload_fails_closed_without_repair(tmp_path):
    store = _payload_store(tmp_path)
    with pytest.raises(ProtectedObjectNotFoundError, match="missing"):
        store.load("pap_000000000001", action_id="act_000000000001")
    assert list((tmp_path / "payloads").glob("pap_*.json")) == []


def test_evidence_is_bound_to_action_attempt_epoch_intent_and_namespace(tmp_path):
    store = _evidence_store(tmp_path)
    attempt = store.create_attempt_evidence(
        "act_000000000001", "aat_000000000001", "a" * 64,
        {"receipt": EVIDENCE_SECRET})
    probe = store.create_reconciliation_evidence(
        "act_000000000001", 3, "a" * 64,
        {"probe": EVIDENCE_SECRET})

    wrong_attempts = [
        {"action_id": "act_000000000002", "attempt_id": "aat_000000000001",
         "intent_digest": "a" * 64},
        {"action_id": "act_000000000001", "attempt_id": "aat_000000000002",
         "intent_digest": "a" * 64},
        {"action_id": "act_000000000001", "attempt_id": "aat_000000000001",
         "intent_digest": "b" * 64},
    ]
    for binding in wrong_attempts:
        with pytest.raises(ProtectedObjectCorruptError, match="binding"):
            store.load_attempt_evidence(attempt.reference, **binding)

    with pytest.raises(ProtectedObjectCorruptError, match="binding"):
        store.load_reconciliation_evidence(
            probe.reference,
            action_id="act_000000000001",
            claim_epoch=4,
            intent_digest="a" * 64,
        )
    with pytest.raises(ProtectedObjectCorruptError, match="binding"):
        store.load_reconciliation_evidence(
            attempt.reference,
            action_id="act_000000000001",
            claim_epoch=1,
            intent_digest="a" * 64,
        )
    with pytest.raises(ProtectedReferenceError):
        store.load_attempt_evidence(
            "pap_000000000001",
            action_id="act_000000000001",
            attempt_id="aat_000000000001",
            intent_digest="a" * 64,
        )


def test_two_actions_and_same_action_reload_remain_isolated(tmp_path):
    store = _payload_store(tmp_path)
    first = store.create("act_000000000001", {"recipient": SECRET})
    second = store.create("act_000000000002", {"recipient": "other"})
    assert first.reference != second.reference
    assert store.load(first.reference, action_id=first.action_id) == first
    assert store.load(first.reference, action_id=first.action_id) == first
    with pytest.raises(ProtectedObjectCorruptError):
        store.load(first.reference, action_id=second.action_id)
    with pytest.raises(ProtectedObjectCorruptError):
        store.load(second.reference, action_id=first.action_id)


def test_evidence_object_alone_never_establishes_action_truth(tmp_path):
    payloads = _payload_store(tmp_path)
    evidence = _evidence_store(tmp_path)
    action_store = InMemoryActionStore(clock=FixedClock(NOW))
    record, _ = _protected_record(payloads)
    action_store.create(record)
    stored = evidence.create_attempt_evidence(
        record.action_id, "aat_000000000001", record.intent_digest,
        {"receipt": EVIDENCE_SECRET})
    assert stored.reference not in action_store.load(record.action_id).evidence_refs
    assert action_store.load(record.action_id).state is ActionState.AUTHORIZED


def test_executor_keeps_payload_and_evidence_out_of_public_surfaces(tmp_path):
    logger = RecordingLogger()
    executor, store, tool, payloads, evidence = _build_executor(
        tmp_path, logger=logger)
    invocation = _invoke(executor)
    record = store.load(invocation.action_id)
    state = AgentState(run_id="run_000000000001")
    state.record_tool_call(invocation)
    public = repr((record.to_dict(), invocation.to_dict(), state.to_dict(), logger.events))

    assert invocation.ok
    assert tool.runs == 1
    assert is_protected_payload_reference(record.intent_ref)
    assert all(is_evidence_reference(ref) for ref in record.evidence_refs)
    assert SECRET not in public
    assert BODY not in public
    assert EVIDENCE_SECRET not in public
    assert SECRET not in repr(payloads)
    assert EVIDENCE_SECRET not in repr(evidence)
    executor.validate_action_protected_storage(record)


def test_provider_cannot_select_payload_or_evidence_references(tmp_path):
    tool = ProtectedTool()
    selected = ProtectedAdapter(tool, selected_ref="pap_000000000001")
    executor, store, tool, _, _ = _build_executor(tmp_path, adapter=selected)
    invocation = _invoke(executor)
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "protected_reference_untrusted"
    assert store.list_actions() == []
    assert tool.runs == 0

    for reference in ("pev_000000000001", "pap_000000000001", "pap_bad"):
        with pytest.raises(Exception, match="trusted storage"):
            NormalizedActionEffect(
                verdict=EffectVerdict.CONFIRMED_APPLIED,
                result_class="test.applied",
                summary="safe",
                evidence_refs=(reference,),
            ).validate(selected.spec)
        with pytest.raises(Exception, match="trusted storage"):
            ActionReconciliationResult(
                verdict=EffectVerdict.CONFIRMED_APPLIED,
                result_class="test.applied",
                summary="safe",
                evidence_refs=(reference,),
            ).validate()


def test_protected_payload_durable_when_action_create_fails_but_never_executes(
    tmp_path,
):
    class FailingActionStore:
        def create(self, record):
            raise ActionStoreError("create failed")

    executor, _, tool, _, _ = _build_executor(
        tmp_path, action_store=FailingActionStore())
    invocation = _invoke(executor)
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_create_unverified"
    assert tool.runs == 0
    assert len(list((tmp_path / "payloads").glob("pap_*.json"))) == 1


def test_action_reference_missing_or_corrupt_blocks_trusted_validation(tmp_path):
    payloads = _payload_store(tmp_path)
    record, stored = _protected_record(payloads)
    executor, _, tool, _, evidence = _build_executor(
        tmp_path, payload_store=payloads)
    path = tmp_path / "payloads" / f"{stored.reference}.json"
    path.unlink()
    with pytest.raises(ProtectedObjectNotFoundError):
        executor.validate_action_protected_storage(record)
    assert tool.runs == 0

    replacement = payloads.create(
        "act_000000000002", {"recipient": "replacement"})
    corrupt = replace(
        record, intent_ref=replacement.reference,
        intent_digest=replacement.intent_digest)
    with pytest.raises(ProtectedObjectCorruptError):
        executor.validate_action_protected_storage(corrupt)
    evidence.close()


def test_restart_with_malformed_protected_reference_quarantines_mutation(tmp_path):
    payloads = _payload_store(tmp_path)
    valid, _ = _protected_record(payloads)
    malformed = replace(valid, intent_ref="pap_bad")
    action_store = InMemoryActionStore(clock=FixedClock(NOW))
    action_store.create(malformed)
    executor, _, tool, _, evidence = _build_executor(
        tmp_path,
        action_store=action_store,
        payload_store=payloads,
    )
    coordinator = ActionRecoveryCoordinator(
        action_store,
        executor,
        clock=FixedClock(NOW),
        id_factory=DeterministicIds(),
        owner_id="recovery:test",
    )

    report = coordinator.recover_startup()

    assert report.quarantined
    assert report.error_category == "journal_corrupt"
    assert action_store.load(malformed.action_id) == malformed
    assert tool.runs == 0
    evidence.close()


def test_startup_revalidates_authoritative_record_inside_action_guard(tmp_path):
    class GuardObservedStore:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped
            self.guard_requested = threading.Event()

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        @contextmanager
        def execution_guard(self, action_id):
            self.guard_requested.set()
            with self.wrapped.execution_guard(action_id):
                yield

    payloads = _payload_store(tmp_path)
    valid, _ = _protected_record(payloads)
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    underlying.create(valid)
    guarded = GuardObservedStore(underlying)
    executor, _, tool, _, evidence = _build_executor(
        tmp_path,
        action_store=guarded,
        payload_store=payloads,
    )
    coordinator = ActionRecoveryCoordinator(
        guarded,
        executor,
        clock=FixedClock(NOW),
        id_factory=DeterministicIds(),
        owner_id="recovery:test",
    )
    reports = []

    with underlying.execution_guard(valid.action_id):
        worker = threading.Thread(
            target=lambda: reports.append(coordinator.recover_startup()),
            daemon=True,
        )
        worker.start()
        assert guarded.guard_requested.wait(5.0)
        claimed = underlying.claim_attempt(
            valid.action_id,
            valid.version,
            attempt_id="aat_000000000001",
            invocation_id="inv_000000000001",
            owner_id="process:test",
            owner_epoch=1,
            transition_id="atx_000000000001",
        )
        malformed = claimed.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id="atx_000000000002",
            at=NOW,
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            evidence_refs=("pap_bad",),
            reconciliation=ReconciliationMetadata(
                disposition=ReconciliationDisposition.PROBE_LATER),
        )
        underlying.compare_and_swap(
            claimed.action_id, claimed.version, malformed)

    worker.join(5.0)
    assert not worker.is_alive()
    assert reports[0].quarantined
    assert reports[0].error_category == "journal_corrupt"
    assert underlying.load(valid.action_id) == malformed
    assert tool.runs == 0
    evidence.close()


class StoreWrapper:
    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    @contextmanager
    def execution_guard(self, action_id):
        with self.wrapped.execution_guard(action_id):
            yield


class ResultCasFails(StoreWrapper):
    def compare_and_swap(self, action_id, expected_version, next_record):
        if next_record.state is not ActionState.IN_FLIGHT:
            raise ActionStoreError("publication failed")
        return self.wrapped.compare_and_swap(
            action_id, expected_version, next_record)


class ResultCasCommitsThenRaises(StoreWrapper):
    def compare_and_swap(self, action_id, expected_version, next_record):
        result = self.wrapped.compare_and_swap(
            action_id, expected_version, next_record)
        if next_record.state is not ActionState.IN_FLIGHT:
            raise ActionStoreError("caller crashed after durable CAS")
        return result


def test_evidence_written_but_journal_cas_failure_preserves_uncertain_truth(tmp_path):
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    executor, _, tool, _, _ = _build_executor(
        tmp_path, action_store=ResultCasFails(underlying))
    invocation = _invoke(executor)
    loaded = underlying.load(invocation.action_id)
    assert not invocation.ok
    assert invocation.result.metadata["kind"] == "action_result_publication_uncertain"
    assert loaded.state is ActionState.IN_FLIGHT
    assert loaded.evidence_refs == ()
    assert tool.runs == 1
    assert len(list((tmp_path / "evidence").glob("pev_*.json"))) == 1


def test_journal_cas_success_before_caller_failure_is_observed_as_truth(tmp_path):
    underlying = InMemoryActionStore(clock=FixedClock(NOW))
    executor, _, tool, _, _ = _build_executor(
        tmp_path, action_store=ResultCasCommitsThenRaises(underlying))
    invocation = _invoke(executor)
    loaded = underlying.load(invocation.action_id)
    assert invocation.ok
    assert loaded.state is ActionState.SUCCEEDED
    assert len(loaded.evidence_refs) == 1
    assert tool.runs == 1


def test_stale_epoch_and_cross_action_evidence_cannot_be_published(tmp_path):
    evidence = _evidence_store(tmp_path)
    first = evidence.create_reconciliation_evidence(
        "act_000000000001", 1, "a" * 64, {"probe": EVIDENCE_SECRET})
    second = evidence.create_reconciliation_evidence(
        "act_000000000002", 2, "b" * 64, {"probe": "other"})
    for reference, action_id, epoch, digest in [
        (first.reference, first.action_id, 2, first.intent_digest),
        (first.reference, second.action_id, 1, first.intent_digest),
        (second.reference, first.action_id, 2, second.intent_digest),
    ]:
        with pytest.raises(ProtectedObjectCorruptError, match="binding"):
            evidence.load_reconciliation_evidence(
                reference,
                action_id=action_id,
                claim_epoch=epoch,
                intent_digest=digest,
            )


def test_atomic_replace_failure_leaves_no_final_or_partial_object(tmp_path, monkeypatch):
    import agentcore.action_journal.protected_storage as protected_storage

    store = _payload_store(tmp_path)

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(protected_storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        store.create("act_000000000001", {"recipient": SECRET})
    assert list((tmp_path / "payloads").glob("pap_*.json")) == []
    assert list((tmp_path / "payloads").glob("*.tmp")) == []


def test_json_reads_are_bounded_on_one_open_handle(tmp_path, monkeypatch):
    store = _payload_store(tmp_path)
    stored = store.create("act_000000000001", {"recipient": SECRET})

    def unbounded_read_forbidden(self):
        raise AssertionError("Path.read_bytes would be an unbounded read")

    monkeypatch.setattr(Path, "read_bytes", unbounded_read_forbidden)
    assert store.load(stored.reference, action_id=stored.action_id) == stored


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX os.fork")
def test_forked_child_cannot_use_inherited_payload_or_evidence_store(tmp_path):
    payloads = _payload_store(tmp_path)
    evidence = _evidence_store(tmp_path)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - executed only in POSIX child
        os.close(read_fd)
        results = []
        for operation in (
            lambda: payloads.intent_digest({"value": "child"}),
            lambda: evidence.create_reconciliation_evidence(
                "act_000000000001", 1, "a" * 64, {"value": "child"}),
        ):
            try:
                operation()
            except ProtectedStoreOwnershipError:
                results.append("fenced")
            except BaseException:
                results.append("wrong-error")
            else:
                results.append("usable")
        os.write(write_fd, ",".join(results).encode("ascii"))
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        observed = os.read(read_fd, 128).decode("ascii")
    finally:
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert observed == "fenced,fenced"
