"""Credential use through real registry/policy/executor and Agent composition."""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import pickle
import signal
import threading
import traceback
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import journal_for_tools
from agentcore.approvals import ApprovalInvocationContext
from agentcore.config.schema import AgentConfig, ConversationConfig, LoggingConfig, PermissionConfig
from agentcore.conversation.session import JsonFileSessionStore
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.credentials import (
    AccountIdentity,
    CredentialError,
    CredentialFailure,
    CredentialRuntime,
    InMemoryCredentialVault,
    SecretMaterial,
)
from agentcore.credentials.runtime import CredentialExecution
from agentcore.models.messages import ModelResponse, Role, ToolCall
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, PermissionPolicy
from agentcore.realtime.cancellation import CancellationReason
from agentcore.realtime.errors import CancellationRequested
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import Subtask, Task
from agentcore.tools.authenticated import AuthenticatedReadTool
from agentcore.tools.base import FunctionTool, ParamType, ToolContext, ToolParameter, ToolResult, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError
from agentcore.util.clock import FixedClock


SECRET = 'authenticated-sentinel-44-0+/="never-public'
ROTATED = "rotated-authenticated-sentinel-44-0"
SCOPES = frozenset({"items.read"})
IDENTITY = AccountIdentity("test", "https://issuer.test", "verified-subject")
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)


class CountingVault(InMemoryCredentialVault):
    def __init__(self):
        super().__init__(clock=FixedClock(NOW))
        self.resolutions = 0

    @contextmanager
    def _lease(self, *args, **kwargs):
        with super()._lease(*args, **kwargs) as lease:
            self.resolutions += 1
            yield lease


class Provider:
    provider_id = "test"

    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback
        self.lease = None
        self.arguments = None

    def read(self, args, credential):
        self.calls += 1
        self.lease = credential
        self.arguments = args
        if self.callback is not None:
            return self.callback(args, credential)
        assert credential.reveal() in (SECRET, ROTATED)
        return ToolResult.success({"items": [{"id": "item-one", "title": "safe result"}]})


def read_tool(name="account_read", *, parameter_type=ParamType.STRING):
    return AuthenticatedReadTool(tool_spec(
        name, "Read items from the explicitly connected test account.",
        parameters=(ToolParameter("query", parameter_type),),
        capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS),
    ))


def setup_runtime(callback=None, *, parameter_type=ParamType.STRING):
    vault = CountingVault()
    info = vault.register(IDENTITY, SecretMaterial(SECRET), scopes=SCOPES)
    runtime = CredentialRuntime(vault)
    tool = read_tool(parameter_type=parameter_type)
    provider = Provider(callback)
    runtime.bind(tool, reference=info.reference, provider=provider, required_scopes=SCOPES)
    registry = ToolRegistry()
    registry.register(tool)
    return SimpleNamespace(vault=vault, info=info, runtime=runtime, tool=tool,
                           provider=provider, registry=registry)


def executor_for(bundle, *, blocked=(), policy=None, logger=None, allow_authenticated_reads=True):
    executor = ToolExecutor(
        bundle.registry,
        policy or PermissionPolicy(PermissionConfig(blocked_capabilities=tuple(blocked)),
                                   allow_authenticated_reads=allow_authenticated_reads),
        credential_runtime=bundle.runtime,
        logger=logger,
    )
    bundle.executor = executor
    return executor


def invoke(bundle, **kwargs):
    return bundle.executor.invoke(bundle.tool.name, {"query": "safe query"}, **kwargs)


def test_execution_preserves_validation_policy_run_and_current_resolution_order(monkeypatch):
    events = []
    bundle = setup_runtime(lambda args, lease: (
        events.append("provider"), ToolResult.success("read complete")
    )[1])
    original_validate, original_run = bundle.tool.validate_args, bundle.tool.run

    def validate(args):
        events.append("validate")
        return original_validate(args)

    def run(args, context):
        events.append("run")
        assert bundle.vault.resolutions == 0
        assert SECRET not in repr(context) + repr(args)
        return original_run(args, context)

    class Policy(PermissionPolicy):
        calls = 0

        def check(self, *args, **kwargs):
            self.calls += 1
            events.append("permission")
            assert bundle.vault.resolutions == int(self.calls == 3)
            assert bundle.provider.calls == 0
            return super().check(*args, **kwargs)

    monkeypatch.setattr(bundle.tool, "validate_args", validate)
    monkeypatch.setattr(bundle.tool, "run", run)
    executor_for(bundle, policy=Policy(allow_authenticated_reads=True))
    result = invoke(bundle, checkpoint=lambda: events.append("checkpoint"))
    assert result.ok and result.decision.allowed
    assert events == ["validate", "checkpoint", "permission", "checkpoint",
                      "run", "checkpoint", "permission", "checkpoint",
                      "permission", "provider"]
    assert bundle.vault.resolutions == 1
    assert result.args == {"query": "***"}
    assert result.action_id is None and result.attempt_id is None
    assert SECRET not in json.dumps(result.to_dict())
    assert bundle.provider.arguments == {"query": "safe query"}


def test_existing_connection_or_historical_approval_does_not_grant_permission():
    bundle = setup_runtime()
    executor_for(bundle, blocked=(Capability.NET_READ,))
    result = invoke(bundle, approval_context=ApprovalInvocationContext("apr_" + "1" * 12))
    assert not result.ok and result.result.metadata["kind"] == "permission_denied"
    assert bundle.vault.resolutions == 0 and bundle.provider.calls == 0
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS),
                              approval_gate=AlwaysApproveGate())
    assert not policy.check({Capability.CREDENTIAL_ACCESS}).allowed
    assert not policy.check({Capability.EMAIL_SEND}).allowed


@pytest.mark.parametrize("level", list(PermissionLevel))
@pytest.mark.parametrize("blocked", [(), (Capability.CREDENTIAL_ACCESS,)])
def test_net_read_without_credential_authority_never_resolves_or_calls_provider(level, blocked):
    bundle = setup_runtime()
    policy = PermissionPolicy(PermissionConfig(level=level, blocked_capabilities=blocked),
                              approval_gate=AlwaysApproveGate())
    assert policy.check({Capability.NET_READ}).allowed
    assert not policy.check({Capability.CREDENTIAL_ACCESS}).allowed
    executor_for(bundle, policy=policy)
    result = invoke(bundle, approval_context=ApprovalInvocationContext("apr_" + "1" * 12))
    assert not result.ok and result.result.metadata["kind"] == "permission_denied"
    assert bundle.vault.resolutions == 0 and bundle.provider.calls == 0
    assert bundle.provider.lease is None


@pytest.mark.parametrize("level", list(PermissionLevel))
def test_explicit_host_permission_allows_only_the_bound_pure_read(level):
    bundle = setup_runtime()
    policy = PermissionPolicy(PermissionConfig(level=level), allow_authenticated_reads=True)
    executor_for(bundle, policy=policy)
    result = invoke(bundle)
    assert result.ok and result.decision.allowed
    assert bundle.provider.calls == 1 and bundle.vault.resolutions == 1
    assert bundle.tool.spec.read_only is True
    assert bundle.tool.capabilities == {Capability.NET_READ, Capability.CREDENTIAL_ACCESS}
    assert result.action_id is None and result.attempt_id is None
    assert not policy.check({Capability.CREDENTIAL_ACCESS}).allowed


@pytest.mark.parametrize("opt_in", [False, True])
def test_ordinary_net_reads_keep_working_without_credential_services(opt_in):
    calls = []
    ordinary = FunctionTool(tool_spec("public_read", "Read public data",
                                     capabilities=(Capability.NET_READ,)),
                            lambda args, context: calls.append(context) or "public data")
    registry = ToolRegistry()
    registry.register(ordinary)
    executor = ToolExecutor(registry, PermissionPolicy(allow_authenticated_reads=opt_in))
    result = executor.invoke(ordinary.name)
    assert result.ok and result.result.output == "public data"
    assert len(calls) == 1 and calls[0].credential_execution is None


def test_generic_read_tool_cannot_claim_the_bound_credential_context():
    calls = []
    generic = FunctionTool(tool_spec("generic_secret", "Unreviewed secret reader",
                                    capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS)),
                           lambda args, context: calls.append(context))
    registry = ToolRegistry()
    registry.register(generic)
    executor = ToolExecutor(registry, PermissionPolicy(allow_authenticated_reads=True))
    result = executor.invoke(generic.name, authenticated_read=True, allow_authenticated_reads=True)
    assert not result.ok and result.result.metadata["kind"] == "permission_denied"
    assert calls == []


def test_model_metadata_cannot_enable_the_host_credential_opt_in():
    bundle = setup_runtime()
    executor_for(bundle, allow_authenticated_reads=False)
    result = invoke(bundle, authenticated_read=True, allow_authenticated_reads=True,
                    capabilities=["net.read", "credential.access"], approved=True)
    assert not result.ok and result.result.metadata["kind"] == "permission_denied"
    assert bundle.vault.resolutions == 0 and bundle.provider.calls == 0


@pytest.mark.parametrize("restore_previous", [False, True])
@pytest.mark.parametrize("check_number", [2, 3])
def test_policy_replacement_after_evaluation_invalidates_dispatch(
    monkeypatch, restore_previous, check_number,
):
    bundle = setup_runtime()
    executor_for(bundle)
    original_policy = bundle.executor.policy
    original_check = original_policy.check
    checked, release = threading.Event(), threading.Event()
    calls = 0
    results = []

    def gated_check(*args, **kwargs):
        nonlocal calls
        decision = original_check(*args, **kwargs)
        calls += 1
        if calls == check_number:
            assert decision.allowed
            checked.set()
            assert release.wait(5)
        return decision

    monkeypatch.setattr(original_policy, "check", gated_check)
    worker = threading.Thread(target=lambda: results.append(invoke(bundle)), daemon=True)
    worker.start()
    try:
        assert checked.wait(2)
        bundle.executor.policy = PermissionPolicy(PermissionConfig(
            blocked_capabilities=(Capability.NET_READ,)))
        if restore_previous:
            bundle.executor.policy = original_policy
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results[0].result.metadata["kind"] == "permission_denied"
    assert bundle.provider.calls == 0 and bundle.provider.lease is None


@pytest.mark.parametrize("seam", ["provider_identity", "credential_expiry"])
def test_policy_replacement_after_final_check_is_busy_until_dispatch_finishes(monkeypatch, seam):
    from agentcore.credentials import runtime as credential_runtime
    from agentcore.credentials.vault import CredentialLease

    bundle = setup_runtime()
    executor_for(bundle)
    original_policy = bundle.executor.policy
    target, name, final_check = (
        (credential_runtime, "_provider_identity", 3) if seam == "provider_identity"
        else (CredentialLease, "_check_current", 2)
    )
    original_check = getattr(target, name)
    checked, release = threading.Event(), threading.Event()
    calls = 0
    results = []

    def gated_check(value):
        nonlocal calls
        result = original_check(value)
        calls += 1
        if calls == final_check:
            checked.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(target, name, gated_check)
    worker = threading.Thread(target=lambda: results.append(invoke(bundle)), daemon=True)
    worker.start()
    try:
        assert checked.wait(2)
        assert bundle.provider.calls == 0
        with pytest.raises(CredentialError) as error:
            bundle.executor.policy = PermissionPolicy(PermissionConfig(
                blocked_capabilities=(Capability.NET_READ,)))
        assert error.value.reason is CredentialFailure.BUSY
        assert bundle.executor.policy is original_policy
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and results[0].ok
    assert bundle.provider.calls == 1
    bundle.executor.policy = PermissionPolicy(PermissionConfig(
        blocked_capabilities=(Capability.NET_READ,)))
    assert invoke(bundle).result.metadata["kind"] == "permission_denied"
    assert bundle.provider.calls == 1


@pytest.mark.parametrize("failure", ["provider", "unsafe_result", "identity", "expiry"])
def test_policy_dispatch_lease_is_released_after_failure(failure, monkeypatch):
    def provider(args, lease):
        if failure == "provider":
            raise RuntimeError(lease.reveal())
        return ToolResult.success(lease.reveal())

    bundle = setup_runtime(provider)
    executor_for(bundle)
    if failure in {"identity", "expiry"}:
        from agentcore.credentials import runtime as credential_runtime
        expiring = bundle.vault.rotate(
            bundle.info.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES,
            expires_at=NOW + timedelta(seconds=1),
        )
        bundle.info = expiring
        bundle.runtime.bind(bundle.tool, reference=expiring.reference, provider=bundle.provider,
                            required_scopes=SCOPES, replace_existing=True)
        original_identity = credential_runtime._provider_identity
        calls = 0

        def fail_last_identity(provider):
            nonlocal calls
            calls += 1
            if calls == 3:
                if failure == "identity":
                    provider.provider_id = "changed"
                else:
                    bundle.vault._clock.advance(1)
            return original_identity(provider)

        monkeypatch.setattr(credential_runtime, "_provider_identity", fail_last_identity)
    result = invoke(bundle)
    assert not result.ok
    assert bundle.provider.calls == int(failure in {"provider", "unsafe_result"})
    assert SECRET not in json.dumps(result.to_dict())
    bundle.executor.policy = PermissionPolicy()
    bundle.vault.revoke(bundle.info.reference)
    bundle.registry.unregister(bundle.tool.name)


def test_permission_revoked_at_last_checkpoint_blocks_plaintext_resolution():
    bundle = setup_runtime()
    executor_for(bundle)
    checks = 0

    def checkpoint():
        nonlocal checks
        checks += 1
        if checks == 4:
            bundle.executor.policy = PermissionPolicy(PermissionConfig(
                blocked_capabilities=(Capability.NET_READ,)))

    result = invoke(bundle, checkpoint=checkpoint)
    assert checks == 4
    assert result.result.metadata["kind"] == "permission_denied"
    assert bundle.vault.resolutions == 1 and bundle.provider.calls == 0


def test_permission_revoked_while_vault_lookup_waits_blocks_provider_dispatch(monkeypatch):
    bundle = setup_runtime()
    executor_for(bundle)
    original = bundle.vault._lease
    entered, release = threading.Event(), threading.Event()
    results = []

    @contextmanager
    def gated_lease(*args, **kwargs):
        with original(*args, **kwargs) as lease:
            entered.set()
            assert release.wait(5)
            yield lease

    monkeypatch.setattr(bundle.vault, "_lease", gated_lease)
    worker = threading.Thread(target=lambda: results.append(invoke(bundle)), daemon=True)
    worker.start()
    assert entered.wait(2)
    try:
        bundle.executor.policy = PermissionPolicy(PermissionConfig(
            blocked_capabilities=(Capability.NET_READ,)))
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert bundle.provider.calls == 0
    assert results[0].result.metadata["kind"] == "permission_denied"


def test_expiry_during_vault_lookup_blocks_provider_dispatch(monkeypatch):
    bundle = setup_runtime()
    expiring = bundle.vault.rotate(
        bundle.info.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES,
        expires_at=NOW + timedelta(seconds=1),
    )
    bundle.runtime.bind(bundle.tool, reference=expiring.reference,
                        provider=bundle.provider, required_scopes=SCOPES, replace_existing=True)
    executor_for(bundle)
    original = bundle.vault._lease

    @contextmanager
    def delayed_lease(*args, **kwargs):
        with original(*args, **kwargs) as lease:
            bundle.vault._clock.advance(1)
            yield lease

    monkeypatch.setattr(bundle.vault, "_lease", delayed_lease)
    result = invoke(bundle)
    assert bundle.provider.calls == 0
    assert result.result.metadata["kind"] == CredentialFailure.EXPIRED.value


def test_provider_identity_change_during_lookup_does_not_retarget_credentials(monkeypatch):
    bundle = setup_runtime()
    executor_for(bundle)
    original = bundle.vault._lease

    @contextmanager
    def changed_identity(*args, **kwargs):
        with original(*args, **kwargs) as lease:
            bundle.provider.provider_id = "changed"
            yield lease

    monkeypatch.setattr(bundle.vault, "_lease", changed_identity)
    result = invoke(bundle)
    assert result.result.metadata["kind"] == CredentialFailure.IDENTITY_MISMATCH.value
    assert bundle.provider.calls == 0


def test_host_cancellation_after_lookup_propagates_and_releases_both_leases():
    bundle = setup_runtime()
    executor_for(bundle)
    checks = 0

    def checkpoint():
        nonlocal checks
        checks += 1
        if checks == 4:
            raise CancellationRequested(CancellationReason.USER_INTERRUPT)

    with pytest.raises(CancellationRequested):
        invoke(bundle, checkpoint=checkpoint)
    assert bundle.vault.resolutions == 1 and bundle.provider.calls == 0
    bundle.executor.policy = PermissionPolicy()
    bundle.vault.revoke(bundle.info.reference)
    bundle.registry.unregister(bundle.tool.name)


@pytest.mark.parametrize("changed", ["identity", "expiry"])
def test_last_host_checkpoint_cannot_dispatch_changed_or_expired_binding(changed):
    bundle = setup_runtime()
    expiring = bundle.vault.rotate(
        bundle.info.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES,
        expires_at=NOW + timedelta(seconds=1),
    )
    bundle.runtime.bind(bundle.tool, reference=expiring.reference,
                        provider=bundle.provider, required_scopes=SCOPES, replace_existing=True)
    executor_for(bundle)
    checks = 0

    def checkpoint():
        nonlocal checks
        checks += 1
        if checks == 4:
            if changed == "identity":
                bundle.provider.provider_id = "changed"
            else:
                bundle.vault._clock.advance(1)

    result = invoke(bundle, checkpoint=checkpoint)
    expected = (CredentialFailure.IDENTITY_MISMATCH if changed == "identity"
                else CredentialFailure.EXPIRED)
    assert bundle.provider.calls == 0
    assert result.result.metadata["kind"] == expected.value


@pytest.mark.parametrize("when", ["enter", "exit"])
def test_backend_cancellation_is_not_mistaken_for_host_checkpoint(when, monkeypatch):
    bundle = setup_runtime()
    executor_for(bundle)
    original = bundle.vault._lease

    @contextmanager
    def broken_lease(*args, **kwargs):
        with original(*args, **kwargs) as lease:
            if when == "enter":
                raise CancellationRequested(CancellationReason.INTERNAL_FAILURE, detail=SECRET)
            yield lease
            raise CancellationRequested(CancellationReason.INTERNAL_FAILURE, detail=SECRET)

    monkeypatch.setattr(bundle.vault, "_lease", broken_lease)
    result = invoke(bundle)
    assert result.result.metadata["kind"] == CredentialFailure.UNAVAILABLE.value
    assert SECRET not in json.dumps(result.to_dict())
    assert bundle.provider.calls == int(when == "exit")
    bundle.vault.revoke(bundle.info.reference)
    bundle.registry.unregister(bundle.tool.name)


def test_backend_metadata_failure_never_exposes_exception_text_or_chain():
    class BrokenVault:
        def _bind_runtime(self, authority):
            raise RuntimeError(SECRET)

    with pytest.raises(CredentialError) as error:
        CredentialRuntime(BrokenVault())
    assert error.value.reason is CredentialFailure.UNAVAILABLE
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None


def test_revoke_after_permission_stops_before_provider_call():
    bundle = setup_runtime()
    executor_for(bundle)
    checks = 0

    def checkpoint():
        nonlocal checks
        checks += 1
        if checks == 2:
            bundle.vault.revoke(bundle.info.reference)

    result = invoke(bundle, checkpoint=checkpoint)
    assert result.result.metadata["kind"] == CredentialFailure.REVOKED.value
    assert bundle.vault.resolutions == 0 and bundle.provider.calls == 0


def test_rotation_does_not_silently_rebind_an_already_authorized_invocation():
    bundle = setup_runtime()
    executor_for(bundle)
    checks = 0
    replacement = None

    def checkpoint():
        nonlocal checks, replacement
        checks += 1
        if checks == 2:
            replacement = bundle.vault.rotate(bundle.info.reference, IDENTITY,
                                               SecretMaterial(ROTATED), scopes=SCOPES)
            bundle.runtime.bind(bundle.tool, reference=replacement.reference,
                                provider=bundle.provider, required_scopes=SCOPES,
                                replace_existing=True)

    result = invoke(bundle, checkpoint=checkpoint)
    assert result.result.metadata["kind"] == CredentialFailure.BINDING.value
    assert bundle.vault.resolutions == 0 and bundle.provider.calls == 0
    assert invoke(bundle).ok
    assert bundle.provider.calls == 1


def test_rotation_without_explicit_host_rebind_leaves_old_binding_stale():
    bundle = setup_runtime()
    executor_for(bundle)
    bundle.vault.rotate(bundle.info.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES)
    result = invoke(bundle)
    assert result.result.metadata["kind"] == CredentialFailure.STALE.value
    assert bundle.provider.calls == 0


def test_same_named_replacement_tool_never_inherits_credential_binding():
    bundle = setup_runtime()
    executor_for(bundle)
    bundle.registry.register(read_tool(), replace=True)
    result = invoke(bundle)
    assert result.result.metadata["kind"] == CredentialFailure.BINDING.value
    assert bundle.provider.calls == 0


def test_same_tool_cannot_be_rebound_to_another_account():
    bundle = setup_runtime()
    other = bundle.vault.register(replace(IDENTITY, subject="other"),
                                  SecretMaterial(ROTATED), scopes=SCOPES)
    with pytest.raises(CredentialError) as error:
        bundle.runtime.bind(bundle.tool, reference=other.reference,
                            provider=bundle.provider, required_scopes=SCOPES,
                            replace_existing=True)
    assert error.value.reason is CredentialFailure.IDENTITY_MISMATCH
    executor_for(bundle)
    assert invoke(bundle).ok
    assert bundle.provider.lease.info.account_id == bundle.info.account_id


def test_current_provider_identity_is_checked_at_the_request_boundary():
    bundle = setup_runtime()
    executor_for(bundle)
    bundle.provider.provider_id = "different"
    result = invoke(bundle)
    assert result.result.metadata["kind"] == CredentialFailure.IDENTITY_MISMATCH.value
    assert bundle.provider.calls == 0 and bundle.vault.resolutions == 0


@pytest.mark.parametrize("missing", ["runtime", "binding", "closed_vault", "closed_runtime"])
def test_missing_or_unavailable_services_fail_closed_without_fallback(missing):
    bundle = setup_runtime()
    if missing == "runtime":
        bundle.executor = ToolExecutor(bundle.registry, PermissionPolicy())
    else:
        executor_for(bundle)
    if missing == "binding":
        bundle.runtime._bindings.clear()
    elif missing == "closed_vault":
        bundle.vault.close()
    elif missing == "closed_runtime":
        bundle.runtime.close()
    result = invoke(bundle)
    assert not result.ok and result.result.metadata["kind"].startswith("credential_")
    assert bundle.provider.calls == 0


@pytest.mark.parametrize("key", ["credential_ref", "access_token", "connection_id", "account_id",
                               "scope", "provider", "capability", "capabilities", "credential_use",
                               "authenticated_read", "allow_authenticated_reads", SECRET])
def test_model_cannot_supply_credential_or_account_authority_through_arguments(key, caplog):
    bundle = setup_runtime()
    executor_for(bundle)
    with caplog.at_level(logging.DEBUG, logger="agentcore"):
        result = bundle.executor.invoke(bundle.tool.name,
                                        {"query": "safe", key: SECRET}, arbitrary=SECRET)
    assert result.result.metadata["kind"] == "validation"
    assert bundle.provider.calls == 0 and bundle.vault.resolutions == 0
    assert SECRET not in repr(result.to_dict()) + caplog.text
    assert SECRET not in repr([record.__dict__ for record in caplog.records])


@pytest.mark.parametrize("key", ["access_token", "capabilities", "credential_use",
                               "authenticated_read", "allow_authenticated_reads"])
def test_nested_credential_fields_are_rejected_before_permission_or_provider(key):
    bundle = setup_runtime(parameter_type=ParamType.OBJECT)
    executor_for(bundle)
    result = bundle.executor.invoke(bundle.tool.name, {"query": {key: SECRET}})
    assert result.result.metadata["kind"] == "validation"
    assert bundle.provider.calls == 0


@pytest.mark.parametrize("spec", [
    tool_spec("bad", "bad", capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS),
              read_only=False),
    tool_spec("bad", "bad", capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS,
                                          Capability.NET_WRITE)),
    tool_spec("bad", "bad", capabilities=(Capability.NET_READ,)),
    tool_spec("bad", "bad", capabilities=(Capability.CREDENTIAL_ACCESS,)),
    tool_spec("bad", "bad", capabilities=()),
    tool_spec("bad", "bad", capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS),
              parameters=(ToolParameter("credential_ref"),)),
])
def test_unreviewed_authenticated_mutation_or_secret_schemas_are_unavailable(spec):
    with pytest.raises(CredentialError):
        AuthenticatedReadTool(spec)


@pytest.mark.parametrize("surface", ["output", "nested", "key", "metadata", "error",
                                   "exception", "base64", "object", "secret_field"])
def test_provider_secret_output_and_exceptions_never_reach_invocation_or_logs(surface, caplog):
    def provider(args, lease):
        secret = lease.reveal()
        if surface == "exception":
            raise RuntimeError(f"Authorization: Bearer {secret}")
        if surface == "error":
            return ToolResult.failure(secret, body=secret)
        if surface == "metadata":
            return ToolResult.success("safe", note=secret)
        if surface == "nested":
            return ToolResult.success({"items": [{"description": f"prefix {secret} suffix"}]})
        if surface == "key":
            return ToolResult.success({secret: "safe"})
        if surface == "base64":
            return ToolResult.success(base64.b64encode(secret.encode()).decode())
        if surface == "object":
            class Unsafe:
                def __repr__(self):
                    pytest.fail("provider object repr executed")
                def to_dict(self):
                    pytest.fail("provider serializer executed")
            return ToolResult.success(Unsafe())
        if surface == "secret_field":
            return ToolResult.success({"refresh_token": "another-secret-not-yet-known"})
        return ToolResult.success(secret)

    bundle = setup_runtime(provider)
    executor_for(bundle)
    with caplog.at_level(logging.DEBUG, logger="agentcore"):
        result = invoke(bundle)
    assert bundle.provider.calls == 1 and bundle.vault.resolutions == 1
    assert not result.ok
    assert result.result.metadata["kind"] in {
        CredentialFailure.PROVIDER.value, CredentialFailure.UNSAFE_RESULT.value,
    }
    assert SECRET not in json.dumps(result.to_dict()) + caplog.text
    assert "another-secret-not-yet-known" not in json.dumps(result.to_dict())
    assert all(record.exc_info is None for record in caplog.records)
    assert SECRET not in repr([record.__dict__ for record in caplog.records])


def test_successful_result_is_detached_from_provider_owned_mutable_data():
    raw = {"items": [{"title": "initial"}]}
    bundle = setup_runtime(lambda args, lease: ToolResult.success(raw))
    executor_for(bundle)
    result = invoke(bundle)
    raw["items"][0]["title"] = SECRET
    assert result.result.output == {"items": [{"title": "initial"}]}


@pytest.mark.parametrize("identifier", ["reference", "account_id", "connection_id"])
@pytest.mark.parametrize("surface", ["output", "metadata"])
@pytest.mark.parametrize("encoded", [False, True])
def test_active_binding_identifiers_are_withheld_from_provider_results(
    identifier, surface, encoded, caplog,
):
    echoed = []

    def provider(args, lease):
        value = getattr(lease.info, identifier)
        if encoded:
            value = base64.b64encode(value.encode("utf-8")).decode("ascii")
        echoed.append(value)
        payload = {"items": [{"id": value}]}
        return (ToolResult.success(payload) if surface == "output" else
                ToolResult(ok=True, output="safe", metadata=payload))

    bundle = setup_runtime(provider)
    executor_for(bundle)
    result = invoke(bundle)
    assert not result.ok
    assert result.result.metadata["kind"] == CredentialFailure.UNSAFE_RESULT.value
    public = json.dumps(result.to_dict()) + caplog.text
    assert echoed[0] not in public
    assert getattr(bundle.info, identifier) not in public
    bundle.vault.revoke(bundle.info.reference)


def test_numeric_credential_cannot_escape_as_a_json_number():
    bundle = setup_runtime(lambda args, lease: ToolResult.success(int(lease.reveal())))
    rotated = bundle.vault.rotate(bundle.info.reference, IDENTITY,
                                   SecretMaterial("68472915402858327"), scopes=SCOPES)
    bundle.runtime.bind(bundle.tool, reference=rotated.reference,
                        provider=bundle.provider, required_scopes=SCOPES, replace_existing=True)
    executor_for(bundle)
    result = invoke(bundle)
    assert not result.ok
    assert "68472915402858327" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(("credential", "normalize"), [
    ("00012345", int),
    ("+00012345", int),
    ("-00012345", int),
    ("00012345", float),
    ("00123.4500", float),
    ("0001.2345e4", float),
    ("00012345", lambda value: str(int(value))),
    ("00012345", lambda value: str(float(value))),
    ("9007199254740993", float),
    ("0001.2345e4", lambda value: int(float(value))),
])
def test_normalized_numeric_credential_is_withheld_from_public_result(credential, normalize, caplog):
    bundle = setup_runtime(lambda args, lease: ToolResult.success(
        {"items": [{"value": normalize(lease.reveal())}]}))
    rotated = bundle.vault.rotate(bundle.info.reference, IDENTITY,
                                  SecretMaterial(credential), scopes=SCOPES)
    bundle.runtime.bind(bundle.tool, reference=rotated.reference,
                        provider=bundle.provider, required_scopes=SCOPES, replace_existing=True)
    executor_for(bundle)
    result = invoke(bundle)
    assert not result.ok
    assert result.result.metadata["kind"] == CredentialFailure.UNSAFE_RESULT.value
    public = json.dumps(result.to_dict()) + caplog.text
    assert credential not in public
    assert str(normalize(credential)) not in public
    bundle.vault.revoke(rotated.reference)


def test_unrelated_numeric_result_is_still_supported():
    bundle = setup_runtime(lambda args, lease: ToolResult.success({"count": 12345, "ratio": 12.5}))
    executor_for(bundle)
    result = invoke(bundle)
    assert result.ok and result.result.output == {"count": 12345, "ratio": 12.5}


@pytest.mark.parametrize("key", ["output", "cls"])
def test_valid_metadata_keys_are_preserved_without_keyword_binding(key):
    metadata = {key: {"items": ["safe"]}}
    bundle = setup_runtime(lambda args, lease: ToolResult(
        ok=True, output={"items": []}, metadata=metadata))
    executor_for(bundle)
    result = invoke(bundle)
    assert result.ok
    assert result.result.metadata == {key: {"items": ["safe"]}}
    metadata[key]["items"][0] = SECRET
    assert result.result.metadata == {key: {"items": ["safe"]}}


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_provider_interruptions_preserve_control_flow_without_secret_text(interruption):
    def provider(args, lease):
        raise interruption(lease.reveal())

    bundle = setup_runtime(provider)
    executor_for(bundle)
    with pytest.raises(interruption) as error:
        invoke(bundle)
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert error.value.__context__ is None and error.value.__cause__ is None
    # Cleanup still releases both the credential and registry leases.
    bundle.executor.policy = PermissionPolicy()
    bundle.vault.revoke(bundle.info.reference)
    bundle.registry.unregister(bundle.tool.name)


def test_direct_tool_run_forged_authority_and_second_executor_cannot_resolve():
    bundle = setup_runtime()
    with pytest.raises(CredentialError) as error:
        bundle.tool.run({"query": "safe"}, ToolContext())
    assert error.value.reason is CredentialFailure.AUTHORITY
    executor_for(bundle)
    with pytest.raises(CredentialError):
        ToolExecutor(bundle.registry, PermissionPolicy(), credential_runtime=bundle.runtime)
    with pytest.raises(CredentialError):
        CredentialRuntime(bundle.vault)
    with pytest.raises(CredentialError):
        CredentialExecution(bundle.runtime, bundle.runtime._snapshot(bundle.tool),
                            {"query": "safe"}, lambda: None, authority=object())
    assert bundle.provider.calls == 0 and bundle.vault.resolutions == 0


def test_permit_is_one_use_argument_bound_nonserializable_and_expires_after_run(monkeypatch):
    bundle = setup_runtime()
    executor_for(bundle)
    run = bundle.tool.run
    captured = []

    def instrumented(args, context):
        permit = context.credential_execution
        captured.append(permit)
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(permit)
        with pytest.raises(TypeError):
            asdict(context)
        with pytest.raises(CredentialError):
            permit.invoke(bundle.tool, {"query": "changed"})
        with pytest.raises(CredentialError):
            bundle.runtime._read(permit)
        result = run(args, context)
        with pytest.raises(CredentialError):
            run(args, context)
        with pytest.raises(CredentialError):
            bundle.runtime._read(permit)
        return result

    monkeypatch.setattr(bundle.tool, "run", instrumented)
    assert invoke(bundle).ok
    assert bundle.provider.calls == 1
    with pytest.raises(CredentialError) as error:
        captured[0].invoke(bundle.tool, {"query": "safe query"})
    assert error.value.reason is CredentialFailure.AUTHORITY
    assert SECRET not in repr(captured[0])


def test_provider_exception_chain_is_removed_before_crossing_tool_boundary(monkeypatch):
    def provider(args, lease):
        try:
            raise ValueError(lease.reveal())
        except ValueError as exc:
            raise RuntimeError(lease.reveal()) from exc

    bundle = setup_runtime(provider)
    executor_for(bundle)
    original = bundle.tool.run
    captured = []

    def run(args, context):
        try:
            return original(args, context)
        except CredentialError as exc:
            captured.append(exc)
            raise

    monkeypatch.setattr(bundle.tool, "run", run)
    assert not invoke(bundle).ok
    assert len(captured) == 1
    assert captured[0].__context__ is None and captured[0].__cause__ is None
    assert SECRET not in "".join(traceback.format_exception(captured[0]))


def test_active_provider_keeps_metadata_locks_free_but_blocks_binding_and_credential_changes():
    entered, release, mutation_done = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []

    def provider(args, lease):
        entered.set()
        assert release.wait(5)
        return ToolResult.success("safe")

    bundle = setup_runtime(provider)
    executor_for(bundle)

    def call():
        results.append(invoke(bundle))

    def mutate():
        try:
            assert bundle.executor.policy.check({Capability.NET_READ}).allowed
            public = FunctionTool(tool_spec("public_read", "Public data",
                                            capabilities=(Capability.NET_READ,)),
                                  lambda args, context: "public data")
            bundle.registry.register(public)
            assert bundle.executor.invoke(public.name).ok
            other = bundle.vault.register(replace(IDENTITY, subject="other"),
                                          SecretMaterial(ROTATED), scopes=SCOPES)
            other_tool = read_tool("other_read")
            bundle.runtime.bind(other_tool, reference=other.reference,
                                provider=Provider(), required_scopes=SCOPES)
            bundle.registry.register(other_tool)
            for operation in (
                lambda: setattr(bundle.executor, "policy", PermissionPolicy()),
                lambda: bundle.vault.revoke(bundle.info.reference),
                lambda: bundle.vault.rotate(bundle.info.reference, IDENTITY,
                                           SecretMaterial(ROTATED), scopes=SCOPES),
                lambda: bundle.runtime.bind(bundle.tool, reference=bundle.info.reference,
                                            provider=bundle.provider, required_scopes=SCOPES,
                                            replace_existing=True),
                bundle.runtime.close,
                bundle.vault.close,
            ):
                with pytest.raises(CredentialError) as error:
                    operation()
                assert error.value.reason is CredentialFailure.BUSY
            with pytest.raises(ToolRegistryLeaseError):
                bundle.registry.unregister(bundle.tool.name)
            second = invoke(bundle)
            assert second.result.metadata["kind"] == CredentialFailure.BUSY.value
        except BaseException as exc:
            errors.append(exc)
        finally:
            mutation_done.set()

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    assert entered.wait(2)
    mutator = threading.Thread(target=mutate, daemon=True)
    mutator.start()
    try:
        assert mutation_done.wait(3), "metadata lock held during provider I/O"
    finally:
        release.set()
        worker.join(3)
        mutator.join(3)
    assert errors == [] and results[0].ok
    assert bundle.provider.calls == 1
    bundle.vault.revoke(bundle.info.reference)
    assert invoke(bundle).result.metadata["kind"] == CredentialFailure.REVOKED.value


def test_policy_pid_fence_precedes_inherited_mutex_and_dispatch_cleanup(monkeypatch):
    bundle = setup_runtime()
    executor = executor_for(bundle)
    authority = executor._check_credential_authority(bundle.tool, bundle.tool.spec, None)
    authority.__enter__()

    class PoisonLock:
        def __enter__(self):
            pytest.fail("inherited policy lock acquired")

    pid = os.getpid()
    monkeypatch.setattr(executor, "_policy_lock", PoisonLock())
    monkeypatch.setattr(os, "getpid", lambda: pid + 1)
    for operation in (
        lambda: executor.policy,
        lambda: setattr(executor, "policy", PermissionPolicy()),
        executor._policy_snapshot,
        lambda: authority.__exit__(None, None, None),
    ):
        with pytest.raises(CredentialError) as error:
            operation()
        assert error.value.reason is CredentialFailure.OWNERSHIP


def test_runtime_pid_fence_precedes_inherited_lock_and_executor_permit_cleanup(monkeypatch):
    bundle = setup_runtime()
    executor_for(bundle)
    binding = bundle.runtime._snapshot(bundle.tool)
    execution = bundle.runtime._execution(bundle.tool, binding, {"query": "safe"},
                                          lambda: None, authority=bundle.executor._credential_authority)
    permit = execution.__enter__()

    class PoisonLock:
        def __enter__(self):
            pytest.fail("inherited runtime lock acquired")

    original_lock = bundle.runtime._lock
    bundle.runtime._lock = PoisonLock()
    pid = os.getpid()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: pid + 1)
        for operation in (
            lambda: bundle.runtime._snapshot(bundle.tool),
            lambda: bundle.runtime._attach_executor(object()),
            lambda: permit.invoke(bundle.tool, {"query": "safe"}),
            lambda: execution.__exit__(None, None, None),
            bundle.runtime.close,
        ):
            with pytest.raises(CredentialError) as error:
                operation()
            assert error.value.reason is CredentialFailure.OWNERSHIP
    bundle.runtime._lock = original_lock


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_real_fork_cannot_use_inherited_runtime_or_permit_and_cannot_hang(monkeypatch):
    import select

    bundle = setup_runtime()
    executor_for(bundle)
    original = bundle.tool.run

    def run(args, context):
        locked, release = threading.Event(), threading.Event()
        authority = bundle.executor._check_credential_authority(bundle.tool, bundle.tool.spec, None)
        authority.__enter__()

        def hold_lock():
            with bundle.runtime._lock, bundle.executor._policy_lock:
                locked.set()
                release.wait(10)

        worker = threading.Thread(target=hold_lock, daemon=True)
        worker.start()
        assert locked.wait(2)
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            rejected = 0
            for operation in (
                lambda: bundle.runtime._snapshot(bundle.tool),
                lambda: context.credential_execution.invoke(bundle.tool, args),
                context.credential_execution._finish,
                lambda: bundle.executor.policy,
                lambda: setattr(bundle.executor, "policy", PermissionPolicy()),
                lambda: authority.__exit__(None, None, None),
            ):
                try:
                    operation()
                except CredentialError as exc:
                    rejected += exc.reason is CredentialFailure.OWNERSHIP
            os.write(write_fd, str(rejected).encode("ascii"))
            os._exit(0)
        os.close(write_fd)
        reaped = False
        try:
            assert select.select([read_fd], [], [], 5)[0], "inherited metadata lock was touched"
            assert os.read(read_fd, 16) == b"6"
            assert os.waitpid(child, 0)[1] == 0
            reaped = True
        finally:
            if not reaped:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
            os.close(read_fd)
            release.set()
            worker.join(2)
            authority.__exit__(None, None, None)
        return original(args, context)

    monkeypatch.setattr(bundle.tool, "run", run)
    assert invoke(bundle).ok and bundle.provider.calls == 1


@pytest.mark.parametrize("leak", [False, True])
@pytest.mark.parametrize("authorized", [False, True])
def test_agent_conversation_run_journal_and_json_history_never_contain_credentials(
    tmp_path, leak, authorized,
):
    bundle = setup_runtime(
        (lambda args, lease: ToolResult.success(lease.reveal())) if leak else None)
    model = SimpleNamespace(requests=[], replies=[
        ModelResponse(text="", tool_calls=[ToolCall(id="call-one", name=bundle.tool.name,
                                          arguments={"query": "safe"})]),
        ModelResponse(text="Read handled."),
    ])

    def generate(request):
        model.requests.append(request)
        return model.replies.pop(0)

    model.generate = generate
    mutator = FunctionTool(tool_spec("test_mutation", "Test durable action",
                                     capabilities=(Capability.FS_WRITE,), read_only=False),
                           lambda args, context: "safe mutation result")
    journal = journal_for_tools([mutator])
    state_store = JsonFileStateStore(tmp_path / "states")
    session_store = JsonFileSessionStore(tmp_path / "sessions")
    log_path = tmp_path / "events.log"
    config = AgentConfig(
        workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.CONFIRM),
        conversation=ConversationConfig(tools_enabled=True),
        logging=LoggingConfig(level="DEBUG", file_path=str(log_path)),
    )
    agent = Agent(config, AgentDependencies(
        tools=[bundle.tool, mutator], model=model, state_store=state_store,
        credential_runtime=bundle.runtime, allow_authenticated_reads=authorized,
        approval_gate=AlwaysApproveGate(), **journal,
    ))
    runtime = agent.conversation(sessions=session_store)
    assert runtime.tools.executor is agent.executor is agent.runner.executor
    mutation = agent.executor.invoke("test_mutation")
    assert mutation.ok and mutation.action_id is not None
    before = journal["action_store"].load(mutation.action_id).to_dict()
    approval_store = agent.executor._approval_authority._store
    approval_ids = approval_store.list_requests()
    assert len(approval_ids) == 1
    approval = approval_store.load(approval_ids[0]).to_dict()
    response = runtime.send("Read the connected account items.")
    assert response.tools_executed == int(authorized and not leak)
    assert bundle.provider.calls == int(authorized)
    assert response.tool_outcomes[0].invocation.ok is (authorized and not leak)
    assert any(message.role is Role.TOOL for message in model.requests[1].messages)
    task = Task(objective="Read account items", subtasks=[Subtask(
        description="Read items", tool_name=bundle.tool.name,
        tool_args={"query": "safe"},
    )])
    agent.run_task(task)
    agent.checkpoint()
    persisted = state_store.load(agent.state.run_id)
    assert persisted.tool_calls
    assert journal["action_store"].load(mutation.action_id).to_dict() == before
    assert bundle.provider.calls == 2 * int(authorized)
    public = json.dumps([
        agent.registry.describe(), agent.state.to_dict(), before, approval,
        [request.to_dict() for request in model.requests],
    ], default=str)
    for handler in logging.getLogger("agentcore").handlers:
        handler.flush()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            public += path.read_text(encoding="utf-8")
    assert SECRET not in public and "verified-subject" not in public
    assert bundle.info.reference not in public
    # Live credentials plus saved sessions/state and a consumed approval still
    # cannot replace the host's current credential-use authorization.
    agent.executor.policy = PermissionPolicy(config.permissions,
                                             approval_gate=AlwaysApproveGate())
    blocked = agent.executor.invoke(bundle.tool.name, {"query": "safe"},
                                    approval_context=ApprovalInvocationContext(approval_ids[0]))
    assert blocked.result.metadata["kind"] == "permission_denied"
    assert bundle.provider.calls == 2 * int(authorized)
    assert journal["action_store"].load(mutation.action_id).to_dict() == before
    # Loaded state, an existing action, and a historical read result provide
    # no live credential runtime to a new executor.
    without_runtime = ToolExecutor(agent.registry, agent.policy)
    denied = without_runtime.invoke(bundle.tool.name, {"query": "safe"},
                                    approval_context=ApprovalInvocationContext(approval_ids[0]))
    assert denied.result.metadata["kind"] == CredentialFailure.UNAVAILABLE.value
    configure_logging(LoggingConfig(level="CRITICAL"))
