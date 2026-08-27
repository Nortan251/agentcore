from __future__ import annotations

import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ACTION_JOURNAL_SCHEMA_VERSION,
    ActionAdapterRegistry,
    ActionAdapterRegistrationError,
    ActionAdapterSpec,
    ActionEffectClass,
    ActionExecutionPermit,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionState,
    ActionTransitionProof,
    EffectVerdict,
    ExternalIdempotencyRequest,
    IdempotencyContract,
    InMemoryActionStore,
    JsonFileActionStore,
    NormalizedActionEffect,
    PreparedActionIntent,
    PROTECTED_STORAGE_SCHEMA_VERSION,
    StaticInstallationKeyProvider,
)
from agentcore.action_journal.errors import ActionValidationError
from agentcore.approvals import (
    APPROVAL_SCHEMA_VERSION,
    ApprovalInvocationContext,
    ApprovalState,
    DurableApprovalAuthority,
    HumanApprovalDecision,
    InMemoryApprovalStore,
)
from agentcore.config.schema import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import Decision, Outcome, PermissionPolicy
from agentcore.realtime import TURN_SCHEMA_VERSION
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


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
SENSITIVE = "task-43.2f-private-recipient@example.invalid"


class DeterministicIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()

    def __call__(self, prefix: str) -> str:
        with self.lock:
            value = self.counts.get(prefix, 0) + 1
            self.counts[prefix] = value
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


class MutablePolicy(PermissionPolicy):
    def __init__(self, *, allowed=True, level=PermissionLevel.AUTONOMOUS):
        super().__init__(PermissionConfig(level=level))
        self.allowed = allowed
        self.calls = 0

    def check(self, capabilities, *, description=""):
        self.calls += 1
        if self.allowed:
            return Decision(Outcome.ALLOW, "current test authority")
        return Decision(
            Outcome.DENY,
            "current test deny",
            capability=Capability.FS_WRITE,
        )


class FakeExternalTool(Tool):
    def __init__(self, *, lose_first_response=False, parameters=None) -> None:
        self.lose_first_response = lose_first_response
        self._parameters = parameters
        self.version = "1.0.0"
        self.runs = 0
        self.remote_effects = 0
        self.remote: dict[str, ToolResult] = {}
        self.permits: list[ActionExecutionPermit] = []
        self.run_hook = None
        self.lock = threading.Lock()

    @property
    def spec(self):
        return tool_spec(
            "external_mutation",
            "Perform one fake external mutation.",
            version=self.version,
            parameters=(self._parameters if self._parameters is not None else (
                ToolParameter("payload", ParamType.STRING),
            )),
            capabilities=(Capability.FS_WRITE,),
            read_only=False,
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        permit = context.action_execution_permit
        assert isinstance(permit, ActionExecutionPermit)
        permit.validate()
        with self.lock:
            self.runs += 1
            self.permits.append(permit)
            prior = self.remote.get(permit.idempotency.key)
            if prior is not None:
                result = prior
            else:
                self.remote_effects += 1
                result = ToolResult.success(
                    {"effect": self.remote_effects},
                    idempotency_key="provider-output-cannot-replace-key",
                )
                self.remote[permit.idempotency.key] = result
                lose = self.lose_first_response
                self.lose_first_response = False
                if lose:
                    raise TimeoutError("response lost after remote commit")
        if self.run_hook is not None:
            self.run_hook()
        return result


class FakeExternalAdapter:
    def __init__(self, tool: FakeExternalTool, *, intent_digest="a" * 64):
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.external-mutation.v1",
            eligibility=ActionJournalEligibility.EXTERNAL_ACCOUNT_MUTATION,
            effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        )
        self.intent_digest = intent_digest

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest=self.intent_digest,
            target_category="test.external-target",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.external-applied",
            summary="The fake provider confirmed the logical effect.",
        )


class FakeIdempotencyProvider:
    allows_indefinite_retention = False

    def __init__(self, clock: FixedClock, *, horizon=3600) -> None:
        self.clock = clock
        self.horizon = horizon
        self.establish_calls = 0
        self.validate_calls = 0
        self.execute_calls = 0
        self.validation_mode = "valid"
        self.contract = "fake.external.v1"
        self.scope = "fake:account:primary"
        self.fixed_key = None
        self.requests = []
        self.execute_hook = None

    def establish_contract(self, request):
        self.establish_calls += 1
        self.requests.append(request)
        return IdempotencyContract(
            key=(self.fixed_key or
                 f"idem_{request.action_id.removeprefix('act_')}"),
            contract=self.contract,
            scope=self.scope,
            valid_until=self.clock.now() + timedelta(seconds=self.horizon),
        )

    def validate_contract(self, request, stored_contract):
        self.validate_calls += 1
        self.requests.append(request)
        if self.validation_mode == "error":
            raise RuntimeError("provider contract unavailable")
        if self.validation_mode == "version":
            return replace(stored_contract, contract="fake.external.v2")
        if self.validation_mode == "scope":
            return replace(stored_contract, scope="fake:account:changed")
        if self.validation_mode == "key":
            return replace(stored_contract, key="idem_replacement")
        if self.validation_mode == "extend":
            return replace(
                stored_contract,
                valid_until=stored_contract.valid_until + timedelta(hours=1),
            )
        return stored_contract

    def execute_if_current(self, request, tool, cleaned, context, permit):
        self.execute_calls += 1
        request.validate()
        permit.validate()
        assert request.action_id == permit.action_id
        assert context.action_execution_permit is permit
        assert permit.idempotency.key not in cleaned.values()
        if self.execute_hook is not None:
            self.execute_hook()
        live_spec = tool.spec
        if live_spec.name != request.tool_name or \
                live_spec.version != request.tool_version or \
                live_spec.name != permit.tool_name or \
                live_spec.version != permit.tool_version:
            raise ValueError("live tool registration changed at request boundary")
        current = self.validate_contract(request, permit.idempotency)
        if current != permit.idempotency:
            raise ValueError("provider no longer affirms the durable contract")
        current.validate()
        if current.valid_until is None:
            if not self.allows_indefinite_retention:
                raise ValueError("provider retention is not explicit")
        elif self.clock.now() >= current.valid_until:
            raise ValueError("provider guarantee expired at request boundary")
        return tool.run(cleaned, context)


class DecisionGate:
    def decide(self, prompt):
        return HumanApprovalDecision.PENDING


def _context(*, key="step:one", action_id=None):
    return ActionInvocationContext(
        action_id=action_id,
        deduplication_scope="test:workflow" if action_id is None else None,
        deduplication_key=key if action_id is None else None,
    )


def _build(
    *,
    tool=None,
    provider=None,
    clock=None,
    store=None,
    policy=None,
    ids=None,
    logger=None,
    register_provider=True,
    approval_authority=None,
):
    clock = clock or FixedClock(NOW)
    tool = tool or FakeExternalTool()
    provider = provider or FakeIdempotencyProvider(clock)
    store = store or InMemoryActionStore(clock=clock)
    policy = policy or MutablePolicy()
    ids = ids or DeterministicIds()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    adapter = FakeExternalAdapter(tool)
    if register_provider:
        adapters.register(
            tool, adapter, idempotency_provider=provider)
    executor = ToolExecutor(
        registry,
        policy,
        action_store=store,
        action_adapters=adapters,
        approval_authority=approval_authority,
        clock=clock,
        id_factory=ids,
        action_owner_id="process:test",
        logger=logger or RecordingLogger(),
    )
    return executor, store, tool, provider, adapters


def _invoke(executor, *, context=None, checkpoint=None, approval=None,
            payload=SENSITIVE):
    return executor.invoke(
        "external_mutation",
        {"payload": payload},
        action_context=context or _context(),
        checkpoint=checkpoint,
        approval_context=approval,
    )


def _ambiguous_runtime(*, horizon=3600):
    clock = FixedClock(NOW)
    tool = FakeExternalTool(lose_first_response=True)
    provider = FakeIdempotencyProvider(clock, horizon=horizon)
    executor, store, tool, provider, adapters = _build(
        tool=tool, provider=provider, clock=clock)
    first = _invoke(executor)
    record = store.load(first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    return executor, store, tool, provider, adapters, clock, first, record


class MutateAtCheckpoint:
    def __init__(self, target, mutation) -> None:
        self.target = target
        self.mutation = mutation
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == self.target:
            self.mutation()


def test_missing_trusted_provider_creates_no_action_attempt_or_effect():
    tool = FakeExternalTool()
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    with pytest.raises(ActionAdapterRegistrationError):
        adapters.register(tool, FakeExternalAdapter(tool))
    store = InMemoryActionStore(clock=FixedClock(NOW))
    executor = ToolExecutor(
        registry, MutablePolicy(), action_store=store,
        action_adapters=adapters, clock=FixedClock(NOW),
        id_factory=DeterministicIds(), logger=RecordingLogger())
    blocked = _invoke(executor)
    assert blocked.result.metadata["kind"] == "action_adapter_missing"
    assert store.list_actions() == []
    assert tool.runs == tool.remote_effects == 0


@pytest.mark.parametrize("name", [
    "idempotency_key", "idempotency_contract", "idempotency_scope",
])
def test_model_visible_argument_cannot_select_idempotency_metadata(name):
    tool = FakeExternalTool(parameters=(
        ToolParameter("payload", ParamType.STRING),
        ToolParameter(name, ParamType.STRING),
    ))
    registry = ActionAdapterRegistry()
    with pytest.raises(ActionAdapterRegistrationError):
        registry.register(
            tool, FakeExternalAdapter(tool),
            idempotency_provider=FakeIdempotencyProvider(FixedClock(NOW)))


def test_undeclared_model_idempotency_argument_fails_before_contract_creation():
    executor, store, tool, provider, _ = _build()
    blocked = executor.invoke(
        "external_mutation",
        {"payload": SENSITIVE, "idempotency_key": "model-selected"},
        action_context=_context(),
    )
    assert blocked.result.metadata["kind"] == "validation"
    assert store.list_actions() == []
    assert provider.establish_calls == tool.runs == 0


def test_provider_seam_preserves_exact_tool_object_binding():
    executor, store, tool, provider, adapters = _build()
    replacement = FakeExternalTool()
    with pytest.raises(ActionAdapterRegistrationError) as exc:
        adapters.establish_external_idempotency(
            replacement,
            # The request shape is valid; exact object binding must reject
            # before any provider method is entered.
            ExternalIdempotencyRequest(
                action_id="act_fffffffffff1",
                action_kind="test.external-mutation.v1",
                tool_name="external_mutation",
                tool_version="1.0.0",
                effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
            ),
            tool_spec=replacement.spec,
        )
    assert exc.value.details["reason"] == "tool_identity_mismatch"
    assert provider.establish_calls == 0


def test_first_attempt_receives_exact_durable_key_and_output_cannot_replace_it():
    executor, store, tool, provider, _ = _build()
    completed = _invoke(executor)
    record = store.load(completed.action_id)
    assert completed.ok and record.state is ActionState.SUCCEEDED
    assert tool.permits[0].idempotency == record.idempotency
    assert record.idempotency.key.startswith("idem_")
    assert record.idempotency.key != "provider-output-cannot-replace-key"
    assert provider.establish_calls == provider.execute_calls == 1


def test_canonical_lost_response_retry_has_one_remote_effect_and_same_key():
    executor, store, tool, provider, _, _, first, ambiguous = (
        _ambiguous_runtime()
    )
    retry = _invoke(executor, context=_context(action_id=first.action_id))
    succeeded = store.load(first.action_id)
    assert retry.ok and succeeded.state is ActionState.SUCCEEDED
    assert succeeded.action_id == ambiguous.action_id == first.action_id
    assert len(succeeded.attempts) == 2
    assert succeeded.attempts[0].attempt_id != succeeded.attempts[1].attempt_id
    assert succeeded.attempts[0].invocation_id != (
        succeeded.attempts[1].invocation_id)
    assert tool.runs == 2 and tool.remote_effects == 1
    assert len(tool.remote) == 1
    assert {permit.idempotency.key for permit in tool.permits} == {
        succeeded.idempotency.key
    }
    assert provider.establish_calls == 1
    assert any(transition.proof is (
        ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE
    ) for transition in succeeded.transitions)
    assert succeeded.attempts[0].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN


@pytest.mark.parametrize("mode", ["key", "version", "scope", "extend"])
def test_provider_cannot_change_durable_contract_on_retry(mode):
    executor, store, tool, provider, _, _, first, before = _ambiguous_runtime()
    provider.validation_mode = mode
    blocked = _invoke(executor, context=_context(action_id=first.action_id))
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert store.load(first.action_id) == before
    assert tool.runs == tool.remote_effects == 1


def test_duplicate_attachment_mints_one_contract_and_one_effect():
    clock = FixedClock(NOW)
    tool = FakeExternalTool()
    entered = threading.Event()
    release = threading.Event()
    original_hook = tool.run_hook

    def pause():
        entered.set()
        assert release.wait(5)
        if original_hook is not None:
            original_hook()

    tool.run_hook = pause
    executor, store, tool, provider, _ = _build(tool=tool, clock=clock)
    results = []
    first = threading.Thread(target=lambda: results.append(_invoke(executor)))
    second = threading.Thread(target=lambda: results.append(_invoke(executor)))
    first.start()
    assert entered.wait(5)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(store.list_actions()) == 1
    assert len({result.action_id for result in results}) == 1
    assert provider.establish_calls == 1
    assert tool.remote_effects == 1


def test_separate_logical_actions_receive_different_action_ids_and_keys():
    executor, store, tool, provider, _ = _build()
    first = _invoke(executor, context=_context(key="step:one"))
    second = _invoke(executor, context=_context(key="step:two"))
    one = store.load(first.action_id)
    two = store.load(second.action_id)
    assert one.action_id != two.action_id
    assert one.idempotency.key != two.idempotency.key
    assert provider.establish_calls == tool.remote_effects == 2


def test_same_remote_scope_and_key_cannot_cross_action_contract_labels():
    clock = FixedClock(NOW)
    provider = FakeIdempotencyProvider(clock)
    provider.fixed_key = "idem_shared_remote_key"
    executor, store, tool, provider, _ = _build(
        clock=clock, provider=provider)
    first = _invoke(executor, context=_context(key="step:one"))
    assert first.ok

    provider.contract = "fake.external.v2"
    blocked = _invoke(executor, context=_context(key="step:two"))

    assert blocked.result.metadata["kind"] == "action_create_unverified"
    assert store.list_actions() == [first.action_id]
    assert provider.establish_calls == 2
    assert tool.runs == tool.remote_effects == 1


def test_missing_registration_after_restart_blocks_same_action_retry():
    executor, store, tool, provider, _, clock, first, before = (
        _ambiguous_runtime()
    )
    restarted, _, restarted_tool, _, _ = _build(
        tool=FakeExternalTool(), clock=clock, store=store,
        register_provider=False)
    blocked = _invoke(
        restarted, context=_context(action_id=first.action_id))
    assert blocked.result.metadata["kind"] == "action_adapter_missing"
    assert store.load(first.action_id) == before
    assert restarted_tool.runs == 0


def test_json_restart_validates_existing_contract_and_reuses_durable_key(tmp_path):
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    directory = tmp_path / "actions"
    first_store = JsonFileActionStore(directory, clock=clock)
    first_tool = FakeExternalTool(lose_first_response=True)
    first_provider = FakeIdempotencyProvider(clock)
    first_executor, _, _, _, _ = _build(
        tool=first_tool,
        provider=first_provider,
        clock=clock,
        ids=ids,
        store=first_store,
    )
    first = _invoke(first_executor)
    durable = first_store.load(first.action_id)
    assert durable.state is ActionState.RECONCILE_REQUIRED
    key = durable.idempotency.key
    first_store.close()

    reopened = JsonFileActionStore(directory, clock=clock)
    restarted_tool = FakeExternalTool()
    restarted_tool.remote = first_tool.remote
    restarted_tool.remote_effects = first_tool.remote_effects
    restarted_provider = FakeIdempotencyProvider(clock)
    restarted, _, _, _, _ = _build(
        tool=restarted_tool,
        provider=restarted_provider,
        clock=clock,
        ids=ids,
        store=reopened,
    )
    completed = _invoke(
        restarted, context=_context(action_id=first.action_id))
    final = reopened.load(first.action_id)
    assert completed.ok and final.idempotency.key == key
    assert restarted_provider.establish_calls == 0
    assert restarted_provider.validate_calls >= 2
    assert restarted_tool.remote_effects == 1
    assert len(final.attempts) == 2
    reopened.close()


def test_expired_contract_blocks_retry_without_attempt_or_effect():
    executor, store, tool, provider, _, clock, first, before = (
        _ambiguous_runtime(horizon=5)
    )
    clock.advance(5)
    blocked = _invoke(executor, context=_context(action_id=first.action_id))
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert store.load(first.action_id) == before
    assert len(before.attempts) == 1 and tool.runs == 1


def test_horizon_expiry_after_retry_proof_before_claim_fails_closed():
    executor, store, tool, provider, _, clock, first, before = (
        _ambiguous_runtime(horizon=5)
    )
    blocked = _invoke(
        executor,
        context=_context(action_id=first.action_id),
        checkpoint=lambda: clock.advance(5),
    )
    after = store.load(first.action_id)
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert after.state is ActionState.AUTHORIZED
    assert after.transitions[-1].proof is (
        ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE)
    assert len(after.attempts) == len(before.attempts) == 1
    assert tool.runs == 1


def _assert_retry_postclaim_change_blocks(mutation):
    executor, store, tool, provider, _, clock, first, before = (
        _ambiguous_runtime(horizon=5)
    )
    checkpoint = MutateAtCheckpoint(2, lambda: mutation(provider, tool, clock))
    blocked = _invoke(
        executor,
        context=_context(action_id=first.action_id),
        checkpoint=checkpoint,
    )
    after = store.load(first.action_id)
    assert checkpoint.calls == 2
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert after.state is ActionState.IN_FLIGHT
    assert after.state not in {ActionState.FAILED, ActionState.CANCELLED}
    assert len(before.attempts) == 1 and len(after.attempts) == 2
    assert before.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert after.attempts[-1].status.value == "claimed"
    assert provider.execute_calls == 1
    assert tool.runs == tool.remote_effects == 1
    return after


def test_expiry_after_retry_claim_never_enters_provider_execution():
    after = _assert_retry_postclaim_change_blocks(
        lambda provider, tool, clock: clock.advance(5))
    assert len(after.attempts) == 2  # No speculative third attempt.


def test_provider_scope_change_after_retry_claim_blocks_before_seam():
    _assert_retry_postclaim_change_blocks(
        lambda provider, tool, clock: setattr(
            provider, "validation_mode", "scope"))


def test_contract_version_retired_after_retry_claim_blocks_before_seam():
    _assert_retry_postclaim_change_blocks(
        lambda provider, tool, clock: setattr(
            provider, "validation_mode", "version"))


def test_provider_unavailable_after_retry_claim_blocks_before_seam():
    _assert_retry_postclaim_change_blocks(
        lambda provider, tool, clock: setattr(
            provider, "validation_mode", "error"))


@pytest.mark.parametrize("mode", ["key", "extend"])
def test_postclaim_final_check_rejects_key_or_horizon_rewrite(mode):
    _assert_retry_postclaim_change_blocks(
        lambda provider, tool, clock: setattr(
            provider, "validation_mode", mode))


def test_tool_version_change_after_retry_claim_blocks_before_seam():
    _assert_retry_postclaim_change_blocks(
        lambda provider, tool, clock: setattr(tool, "version", "2.0.0"))


def test_first_attempt_expiry_after_claim_never_enters_provider_execution():
    clock = FixedClock(NOW)
    executor, store, tool, provider, _ = _build(clock=clock)
    checkpoint = MutateAtCheckpoint(4, lambda: clock.advance(3600))
    blocked = _invoke(executor, checkpoint=checkpoint)
    record = store.load(blocked.action_id)

    assert checkpoint.calls == 4
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert record.state is ActionState.IN_FLIGHT
    assert record.state not in {ActionState.FAILED, ActionState.CANCELLED}
    assert len(record.attempts) == 1
    assert record.attempts[-1].status.value == "claimed"
    assert record.idempotency.valid_until == NOW + timedelta(hours=1)
    assert provider.establish_calls == 1
    assert provider.execute_calls == tool.runs == tool.remote_effects == 0


def test_provider_boundary_revalidates_after_executor_final_check():
    clock = FixedClock(NOW)
    provider = FakeIdempotencyProvider(clock, horizon=5)
    executor, store, tool, provider, _ = _build(
        clock=clock, provider=provider)
    provider.execute_hook = lambda: clock.advance(5)

    blocked = _invoke(executor)
    record = store.load(blocked.action_id)

    assert not blocked.ok
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert provider.execute_calls == 1
    assert tool.runs == tool.remote_effects == 0
    assert record.idempotency.valid_until == NOW + timedelta(seconds=5)


@pytest.mark.parametrize("mode", ["scope", "version", "error", "key", "extend"])
def test_provider_boundary_rejects_current_contract_drift(mode):
    clock = FixedClock(NOW)
    executor, store, tool, provider, _ = _build(clock=clock)
    provider.execute_hook = lambda: setattr(provider, "validation_mode", mode)

    blocked = _invoke(executor)
    record = store.load(blocked.action_id)

    assert not blocked.ok
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert provider.execute_calls == 1
    assert tool.runs == tool.remote_effects == 0


def test_provider_boundary_rejects_last_moment_tool_version_drift():
    clock = FixedClock(NOW)
    executor, store, tool, provider, _ = _build(clock=clock)
    provider.execute_hook = lambda: setattr(tool, "version", "2.0.0")

    blocked = _invoke(executor)
    record = store.load(blocked.action_id)

    assert not blocked.ok
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert provider.execute_calls == 1
    assert tool.runs == tool.remote_effects == 0


def test_current_permission_deny_blocks_retry_despite_valid_guarantee():
    policy = MutablePolicy()
    tool = FakeExternalTool(lose_first_response=True)
    executor, store, tool, provider, _ = _build(tool=tool, policy=policy)
    first = _invoke(executor)
    before = store.load(first.action_id)
    policy.allowed = False
    blocked = _invoke(executor, context=_context(action_id=first.action_id))
    assert blocked.result.metadata["kind"] == "permission_denied"
    assert store.load(first.action_id) == before
    assert tool.runs == 1


def test_sensitive_values_absent_from_public_runtime_surfaces():
    logger = RecordingLogger()
    executor, store, tool, provider, _ = _build(logger=logger)
    completed = _invoke(executor)
    record = store.load(completed.action_id)
    public = repr((record.to_dict(), completed.to_dict(), logger.events,
                   record.idempotency))
    assert SENSITIVE not in public
    assert SENSITIVE not in repr(provider.requests)


def test_contract_key_and_horizon_are_immutable_after_creation():
    executor, store, tool, provider, _, _, first, record = _ambiguous_runtime()
    authorized = record.transition(
        ActionState.AUTHORIZED,
        transition_id="atx_fffffffffff1",
        at=NOW,
        effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
        proof=ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE,
    )
    for changed in (
        replace(authorized.idempotency, key="idem_changed"),
        replace(authorized.idempotency, contract="fake.external.v2"),
        replace(authorized.idempotency, valid_until=(
            authorized.idempotency.valid_until + timedelta(hours=1))),
    ):
        with pytest.raises(ActionValidationError):
            store.compare_and_swap(
                record.action_id,
                record.version,
                replace(authorized, idempotency=changed),
            )


def test_provider_contract_creation_failure_leaves_no_action_or_attempt():
    clock = FixedClock(NOW)
    provider = FakeIdempotencyProvider(clock)

    def unavailable(request):
        raise RuntimeError("provider unavailable")

    provider.establish_contract = unavailable
    executor, store, tool, _, _ = _build(provider=provider, clock=clock)
    blocked = _invoke(executor)
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert store.list_actions() == []
    assert tool.runs == tool.remote_effects == 0


def test_unknown_indefinite_retention_fails_but_explicit_indefinite_succeeds():
    clock = FixedClock(NOW)
    provider = FakeIdempotencyProvider(clock)

    def indefinite(request):
        return IdempotencyContract(
            key=f"idem_{request.action_id.removeprefix('act_')}",
            contract="fake.external.indefinite.v1",
            scope="fake:account:primary",
            valid_until=None,
        )

    provider.establish_contract = indefinite
    blocked_executor, blocked_store, blocked_tool, _, _ = _build(
        provider=provider, clock=clock)
    blocked = _invoke(blocked_executor)
    assert blocked.result.metadata["kind"] == (
        "external_idempotency_contract_unavailable")
    assert blocked_store.list_actions() == [] and blocked_tool.runs == 0

    explicit = FakeIdempotencyProvider(clock)
    explicit.establish_contract = indefinite
    explicit.allows_indefinite_retention = True
    executor, store, tool, _, _ = _build(provider=explicit, clock=clock)
    completed = _invoke(executor)
    assert completed.ok
    assert store.load(completed.action_id).idempotency.valid_until is None
    assert tool.remote_effects == 1


def test_current_approval_requires_fresh_one_shot_and_historical_receipt_is_not_authority():
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    tool = FakeExternalTool(lose_first_response=True)
    provider = FakeIdempotencyProvider(clock)
    store = InMemoryActionStore(clock=clock)
    approvals = InMemoryApprovalStore()
    authority = DurableApprovalAuthority(
        approvals,
        StaticInstallationKeyProvider(b"i" * 32),
        principal_ref="principal:local-owner",
        scope_ref="workspace:test",
        gate=DecisionGate(),
        clock=clock,
        id_factory=ids,
    )
    executor, _, tool, _, _ = _build(
        tool=tool,
        provider=provider,
        clock=clock,
        store=store,
        policy=PermissionPolicy(PermissionConfig(
            level=PermissionLevel.CONFIRM)),
        ids=ids,
        approval_authority=authority,
    )
    pending_first = _invoke(executor)
    authority.record_decision(
        pending_first.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )
    first = _invoke(
        executor,
        approval=ApprovalInvocationContext(
            pending_first.approval_request_id),
    )
    assert store.load(first.action_id).state is ActionState.RECONCILE_REQUIRED
    assert approvals.load(pending_first.approval_request_id).state is (
        ApprovalState.CONSUMED)

    pending_retry = _invoke(
        executor, context=_context(action_id=first.action_id))
    assert pending_retry.result.metadata["kind"] == "approval_pending"
    assert pending_retry.approval_request_id != pending_first.approval_request_id
    old = _invoke(
        executor,
        context=_context(action_id=first.action_id),
        approval=ApprovalInvocationContext(
            pending_first.approval_request_id),
    )
    assert old.result.metadata["kind"] == "approval_consumed"
    assert tool.runs == 1

    authority.record_decision(
        pending_retry.approval_request_id,
        HumanApprovalDecision.APPROVE,
    )
    completed = _invoke(
        executor,
        context=_context(action_id=first.action_id),
        approval=ApprovalInvocationContext(
            pending_retry.approval_request_id),
    )
    assert completed.ok and tool.remote_effects == 1 and tool.runs == 2


class ResultPublicationFailingStore(InMemoryActionStore):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fail_publication = True

    def compare_and_swap(self, action_id, expected_version, next_record):
        if self.fail_publication and next_record.state in {
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.RECONCILE_REQUIRED,
        }:
            raise RuntimeError("injected publication failure")
        return super().compare_and_swap(
            action_id, expected_version, next_record)


def test_publication_failure_never_immediately_repeats_and_recovery_reuses_key():
    clock = FixedClock(NOW)
    ids = DeterministicIds()
    store = ResultPublicationFailingStore(clock=clock)
    tool = FakeExternalTool()
    provider = FakeIdempotencyProvider(clock)
    executor, _, tool, _, _ = _build(
        clock=clock, ids=ids, store=store, tool=tool, provider=provider)
    uncertain = _invoke(executor)
    inflight = store.load(uncertain.action_id)
    assert uncertain.result.metadata["kind"] == (
        "action_result_publication_uncertain")
    assert inflight.state is ActionState.IN_FLIGHT
    assert tool.runs == tool.remote_effects == 1

    store.fail_publication = False
    recovery = ActionRecoveryCoordinator(
        store,
        executor,
        clock=clock,
        id_factory=ids,
        owner_id="recovery:test",
    )
    recovery.recover_startup()
    recovered = store.load(inflight.action_id)
    assert recovered.state is ActionState.RECONCILE_REQUIRED
    retry = _invoke(
        executor, context=_context(action_id=inflight.action_id))
    final = store.load(inflight.action_id)
    assert retry.ok and final.state is ActionState.SUCCEEDED
    assert tool.runs == 2 and tool.remote_effects == 1
    assert {permit.idempotency.key for permit in tool.permits} == {
        final.idempotency.key
    }


def test_cancellation_after_external_seam_remains_ambiguous_not_cancelled():
    tool = FakeExternalTool()

    def cancel_after_commit():
        raise CancellationRequested("cancelled after seam")

    tool.run_hook = cancel_after_commit
    executor, store, tool, provider, _ = _build(tool=tool)
    with pytest.raises(CancellationRequested):
        _invoke(executor)
    record = store.load(store.list_actions()[0])
    assert record.state is ActionState.RECONCILE_REQUIRED
    assert record.attempts[-1].effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
    assert tool.remote_effects == 1


def test_store_mutex_is_not_held_across_provider_io():
    executor, store, tool, provider, _ = _build()
    observed = []

    def inspect_store_from_other_thread():
        thread = threading.Thread(
            target=lambda: observed.append(store.list_actions()))
        thread.start()
        thread.join(3)
        assert not thread.is_alive()

    provider.execute_hook = inspect_store_from_other_thread
    completed = _invoke(executor)
    assert completed.ok and observed == [[completed.action_id]]


def test_independent_external_actions_execute_in_parallel():
    barrier = threading.Barrier(2)
    tool = FakeExternalTool()
    tool.run_hook = lambda: barrier.wait(timeout=5)
    executor, store, tool, provider, _ = _build(tool=tool)
    results = []
    threads = [
        threading.Thread(target=lambda key=key: results.append(
            _invoke(executor, context=_context(key=key))))
        for key in ("parallel:one", "parallel:two")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(6)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2 and all(result.ok for result in results)
    assert len(store.list_actions()) == 2
    assert tool.remote_effects == provider.establish_calls == 2


class NonExternalTool(Tool):
    def __init__(self, *, raises=False) -> None:
        self.raises = raises
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "non_external_mutation",
            "Exercise the unchanged non-external path.",
            version="1.0.0",
            parameters=(ToolParameter("payload", ParamType.STRING),),
            capabilities=(Capability.FS_WRITE,),
            read_only=False,
        )

    def run(self, args, context):
        assert context.action_execution_permit is None
        self.runs += 1
        if self.raises:
            raise RuntimeError("ambiguous non-idempotent outcome")
        return ToolResult.success("local result")


class NonExternalAdapter:
    def __init__(self, tool, effect_class):
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind="test.non-external-mutation.v1",
            eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            effect_class=effect_class,
        )

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="b" * 64,
            target_category="test.local-target",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.local-applied",
            summary="The local effect was confirmed.",
        )


def _build_non_external(effect_class, *, raises=False):
    clock = FixedClock(NOW)
    tool = NonExternalTool(raises=raises)
    registry = ToolRegistry()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    adapters.register(tool, NonExternalAdapter(tool, effect_class))
    store = InMemoryActionStore(clock=clock)
    executor = ToolExecutor(
        registry,
        MutablePolicy(),
        action_store=store,
        action_adapters=adapters,
        clock=clock,
        id_factory=DeterministicIds(),
        logger=RecordingLogger(),
    )
    return executor, store, tool


def test_non_idempotent_ambiguous_action_remains_reconciliation_managed():
    executor, store, tool = _build_non_external(
        ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS,
        raises=True,
    )
    first = executor.invoke(
        "non_external_mutation", {"payload": SENSITIVE},
        action_context=ActionInvocationContext())
    record = store.load(first.action_id)
    assert record.state is ActionState.RECONCILE_REQUIRED
    attached = executor.invoke(
        "non_external_mutation", {"payload": SENSITIVE},
        action_context=ActionInvocationContext(action_id=first.action_id))
    assert attached.action_id == first.action_id
    assert tool.runs == 1


def test_locally_verifiable_action_execution_remains_unchanged():
    executor, store, tool = _build_non_external(
        ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE)
    completed = executor.invoke(
        "non_external_mutation", {"payload": SENSITIVE},
        action_context=ActionInvocationContext())
    assert completed.ok
    assert store.load(completed.action_id).state is ActionState.SUCCEEDED
    assert tool.runs == 1


def test_schema_versions_remain_one():
    assert ACTION_JOURNAL_SCHEMA_VERSION == 1
    assert TURN_SCHEMA_VERSION == 1
    assert PROTECTED_STORAGE_SCHEMA_VERSION == 1
    assert APPROVAL_SCHEMA_VERSION == 1


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX fork semantics")
def test_inherited_idempotency_registry_fails_closed_after_fork():
    clock = FixedClock(NOW)
    tool = FakeExternalTool()
    provider = FakeIdempotencyProvider(clock)
    registry = ActionAdapterRegistry()
    registry.register(
        tool, FakeExternalAdapter(tool), idempotency_provider=provider)
    pid = os.fork()
    if pid == 0:
        try:
            registry.resolve(tool)
        except ActionAdapterRegistrationError as exc:
            os._exit(0 if exc.details.get("reason") == "ownership_mismatch" else 2)
        os._exit(3)
    waited, status = os.waitpid(pid, 0)
    assert waited == pid and os.waitstatus_to_exitcode(status) == 0
