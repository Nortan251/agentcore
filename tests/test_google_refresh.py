"""Deterministic refresh races and real executor/Agent credential isolation."""

import json
import threading
import traceback
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import journal_for_tools
from agentcore.config.schema import AgentConfig, ConversationConfig, LoggingConfig, PermissionConfig
from agentcore.conversation.session import JsonFileSessionStore
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.credentials import CredentialError, CredentialFailure, CredentialRuntime, CredentialState
from agentcore.credentials.google_protocol import GOOGLE_IDENTITY_SCOPES
from agentcore.credentials.protected import ConnectionState
from agentcore.models.messages import ModelResponse, ToolCall
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, PermissionPolicy
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import Subtask, Task
from agentcore.tools.authenticated import AuthenticatedReadTool
from agentcore.tools.base import FunctionTool, ParamType, ToolParameter, ToolResult, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from google_oauth_fakes import ACCESS, CLIENT_SECRET, ID_TOKEN, REFRESH, SUBJECT, bundle


@pytest.fixture
def connected(tmp_path):
    value = bundle(tmp_path / "protected")
    value.info = value.service.connect()
    yield value
    value.vault.close()


def runtime_for(value, *, callback=None):
    class Provider:
        provider_id = "google"
        def __init__(self):
            self.calls = []
        def read(self, args, credential):
            token = credential.reveal()
            assert REFRESH not in token
            self.calls.append(token)
            return callback(credential) if callback else ToolResult.success({"identity_read": "test only"})
    tool = AuthenticatedReadTool(tool_spec(
        "synthetic_identity_read", "Synthetic authenticated identity read; no Google API call.",
        parameters=(ToolParameter("query", ParamType.STRING),),
        capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS)))
    provider = Provider()
    runtime = CredentialRuntime(value.vault)
    runtime.bind(tool, reference=value.info.reference, provider=provider, required_scopes=frozenset({"openid"}))
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, PermissionPolicy(allow_authenticated_reads=True), credential_runtime=runtime)
    return SimpleNamespace(tool=tool, provider=provider, runtime=runtime, registry=registry, executor=executor)


def invoke(runtime):
    return runtime.executor.invoke(runtime.tool.name, {"query": "safe"})


def paused_call(action, hook_owner, hook_name):
    entered, release = threading.Event(), threading.Event()
    results = []
    def pause():
        entered.set()
        assert release.wait(5), "test did not release remote call"
    setattr(hook_owner, hook_name, pause)
    def worker():
        try:
            results.append(action())
        except CredentialError as error:
            results.append(error.reason)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(2), "remote call not reached"
    return thread, release, results


def finish(thread, release):
    release.set()
    thread.join(3)
    assert not thread.is_alive(), "worker did not finish"


def test_verified_refresh_publishes_new_access_only_generation_and_requires_rebind(connected):
    runtime = runtime_for(connected)
    assert invoke(runtime).ok
    refreshed = connected.service.refresh(connected.info.reference)
    assert connected.verifier.calls == 2  # initial authorization and refresh proof
    assert refreshed.generation == 2 and refreshed.connection_id == connected.info.connection_id
    assert refreshed.account_id == connected.info.account_id
    assert connected.vault.describe(connected.info.reference).state is CredentialState.REPLACED
    assert not invoke(runtime).ok
    runtime.runtime.bind(runtime.tool, reference=refreshed.reference, provider=runtime.provider,
                         required_scopes=frozenset({"openid"}), replace_existing=True)
    assert invoke(runtime).ok
    assert runtime.provider.calls == [ACCESS, "replacement-" + ACCESS]


@pytest.mark.parametrize("change", [
    "subject", "issuer", "audience", "expired", "invalid_token", "invalid_verifier_result", "scope",
])
def test_refresh_cannot_change_account_audience_scope_or_publish_invalid_proof(connected, change):
    runtime = runtime_for(connected)
    if change == "subject":
        connected.verifier.changes["sub"] = "other-stable-subject"
    elif change == "issuer":
        connected.verifier.changes["iss"] = "https://wrong.test"
    elif change == "audience":
        connected.verifier.changes["aud"] = "wrong-client"
    elif change == "expired":
        connected.verifier.changes["exp"] = int(connected.clock.now().timestamp())
    elif change == "invalid_token":
        connected.verifier.error = CredentialError(CredentialFailure.IDENTITY_TOKEN)
    elif change == "invalid_verifier_result":
        connected.verifier.verify = lambda *_: None
    else:
        connected.transport.refreshed["scope"] = "openid https://www.googleapis.com/auth/gmail.readonly"
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(connected.info.reference)
    expected = CredentialFailure.SCOPE_MISMATCH if change == "scope" else (
        CredentialFailure.IDENTITY_MISMATCH if change == "subject" else CredentialFailure.IDENTITY_TOKEN
    )
    assert error.value.reason is expected
    assert connected.vault.describe(connected.info.reference) == connected.info
    assert connected.vault._data.identity.subject == SUBJECT
    assert connected.vault.status().state is ConnectionState.CURRENT
    assert not connected.vault.status().can_refresh
    assert invoke(runtime).ok and runtime.provider.calls == [ACCESS]


def test_refresh_without_id_token_preserves_established_identity_without_reverification(connected):
    original = connected.info
    identity = connected.vault._data.identity
    verifier_calls = connected.verifier.calls
    connected.transport.refreshed["expires_in"] = 1800
    del connected.transport.refreshed["id_token"]

    refreshed = connected.service.refresh(original.reference)

    assert connected.verifier.calls == verifier_calls
    assert connected.vault._data.identity == identity
    assert refreshed.account_id == original.account_id
    assert refreshed.connection_id == original.connection_id
    assert refreshed.generation == original.generation + 1
    assert refreshed.reference != original.reference
    assert refreshed.scopes == original.scopes
    assert refreshed.expires_at == connected.clock.now() + timedelta(seconds=1800)


def test_refresh_scope_omission_means_unchanged_and_reduction_does_not_broaden(connected):
    del connected.transport.refreshed["scope"]
    refreshed = connected.service.refresh(connected.info.reference)
    assert refreshed.scopes == GOOGLE_IDENTITY_SCOPES
    connected.transport.refreshed["scope"] = "openid"
    reduced = connected.service.refresh(refreshed.reference)
    assert reduced.scopes == frozenset({"openid"})
    connected.transport.refreshed["scope"] = "openid email"
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(reduced.reference)
    assert error.value.reason is CredentialFailure.SCOPE_MISMATCH
    assert connected.vault.describe(reduced.reference) == reduced


def test_simultaneous_refresh_has_one_flight_and_no_metadata_lock_during_http(connected):
    thread, release, results = paused_call(lambda: connected.service.refresh(connected.info.reference),
                                          connected.transport, "on_refresh")
    try:
        assert connected.vault._lock.acquire(blocking=False)
        connected.vault._lock.release()
        assert connected.vault.describe(connected.info.reference) == connected.info
        with pytest.raises(CredentialError) as error:
            connected.service.refresh(connected.info.reference)
        assert error.value.reason is CredentialFailure.BUSY
    finally:
        finish(thread, release)
    assert len(connected.transport.refreshes) == 1
    assert len(results) == 1 and results[0].generation == 2
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(connected.info.reference)
    assert error.value.reason is CredentialFailure.STALE
    assert connected.vault.describe(results[0].reference) == results[0]


def test_simultaneous_expiry_triggered_refresh_does_not_create_two_generations(connected):
    connected.clock.advance(3601)
    thread, release, results = paused_call(lambda: connected.service.ensure_current(connected.info.reference),
                                          connected.transport, "on_refresh")
    try:
        with pytest.raises(CredentialError) as error:
            connected.service.ensure_current(connected.info.reference)
        assert error.value.reason is CredentialFailure.BUSY
    finally:
        finish(thread, release)
    assert results[0].generation == 2 and len(connected.transport.refreshes) == 1


@pytest.mark.parametrize("intervention", ["disconnect", "reconnect", "failed_reconnect_aba"])
def test_stale_refresh_cannot_overwrite_disconnect_or_new_authorization(connected, intervention):
    connected.transport.refreshed["refresh_token"] = "rotated-" + REFRESH
    thread, release, results = paused_call(lambda: connected.service.refresh(connected.info.reference),
                                          connected.transport, "on_refresh")
    try:
        if intervention == "disconnect":
            connected.service.disconnect(connected.info.reference)
        elif intervention == "reconnect":
            new = connected.service.connect()
            assert new.connection_id != connected.info.connection_id
        else:
            connected.verifier.changes["sub"] = "wrong-subject"
            with pytest.raises(CredentialError):
                connected.service.connect()
            connected.verifier.changes.clear()
        before = connected.vault._file.read_current()
    finally:
        finish(thread, release)
    assert results == [CredentialFailure.STALE]
    assert connected.vault._file.read_current() == before
    if intervention == "disconnect":
        assert connected.vault.status().state is ConnectionState.DISCONNECTED
    elif intervention == "reconnect":
        assert connected.vault.status().credential == new
    else:
        assert connected.vault.status().credential == connected.info


def test_late_authorization_exchange_cannot_undo_local_disconnect(tmp_path):
    value = bundle(tmp_path / "protected")
    thread, release, results = paused_call(value.service.connect, value.transport, "on_exchange")
    try:
        value.service.disconnect(None)
        before = value.vault._file.read_current()
    finally:
        finish(thread, release)
    try:
        assert results == [CredentialFailure.STALE]
        assert value.vault._file.read_current() == before
        assert value.service.restore().state is ConnectionState.DISCONNECTED
    finally:
        value.vault.close()


def test_refresh_reservation_blocks_provider_without_exposing_refresh_token(connected):
    runtime = runtime_for(connected)
    thread, release, _ = paused_call(lambda: connected.service.refresh(connected.info.reference),
                                   connected.transport, "on_refresh")
    try:
        result = invoke(runtime)
        assert not result.ok and not runtime.provider.calls
        assert result.result.metadata["kind"] == CredentialFailure.BUSY.value
    finally:
        finish(thread, release)


def test_provider_waiting_after_lease_blocks_refresh_and_disconnect_until_done(connected):
    entered, release = threading.Event(), threading.Event()
    def read(_):
        entered.set()
        assert release.wait(5)
        return ToolResult.success("safe")
    runtime = runtime_for(connected, callback=read)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(invoke(runtime)), daemon=True)
    thread.start()
    assert entered.wait(2)
    try:
        for action in (lambda: connected.service.refresh(connected.info.reference),
                       lambda: connected.service.disconnect(connected.info.reference)):
            with pytest.raises(CredentialError) as error:
                action()
            assert error.value.reason is CredentialFailure.BUSY
        assert connected.transport.refreshes == []
        assert connected.vault._lock.acquire(blocking=False)
        connected.vault._lock.release()
    finally:
        finish(thread, release)
    assert outcomes[0].ok
    connected.service.disconnect(connected.info.reference)
    assert not invoke(runtime).ok and runtime.provider.calls == [ACCESS]


def test_provider_waiting_before_lease_cannot_adopt_refreshed_generation(connected, monkeypatch):
    runtime = runtime_for(connected)
    original = runtime.runtime._read
    entered, release = threading.Event(), threading.Event()
    outcomes = []
    def read(self, permit):
        entered.set()
        assert release.wait(5)
        return original(permit)
    monkeypatch.setattr(CredentialRuntime, "_read", read)
    thread = threading.Thread(target=lambda: outcomes.append(invoke(runtime)), daemon=True)
    thread.start()
    assert entered.wait(2)
    try:
        replacement = connected.service.refresh(connected.info.reference)
        assert replacement.generation == 2
    finally:
        finish(thread, release)
    assert not outcomes[0].ok and not runtime.provider.calls


def test_refresh_timeout_preserves_current_access_and_reports_uncertainty(connected, caplog):
    runtime = runtime_for(connected)
    def fail():
        raise TimeoutError(REFRESH)
    connected.transport.on_refresh = fail
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(connected.info.reference)
    assert error.value.reason is CredentialFailure.OAUTH_UNCERTAIN
    assert error.value.__context__ is None
    assert connected.vault.status().state is ConnectionState.CURRENT
    assert not connected.vault.status().can_refresh
    assert invoke(runtime).ok
    assert REFRESH not in "".join(traceback.format_exception(error.value)) + caplog.text


def test_refresh_identity_expiring_while_verification_waits_cannot_publish(connected):
    old_verify = connected.verifier.verify
    def verify(token, client_id):
        claims = old_verify(token, client_id)
        connected.clock.advance(3601)
        return claims
    connected.verifier.verify = verify
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(connected.info.reference)
    assert error.value.reason is CredentialFailure.IDENTITY_TOKEN
    assert connected.vault.describe(connected.info.reference) == connected.info


def test_clock_expiry_after_validation_before_publish_is_checked_again(connected):
    original = connected.vault._publish
    def publish(*args, **kwargs):
        connected.clock.advance(3601)
        return original(*args, **kwargs)
    connected.vault._publish = publish
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(connected.info.reference)
    assert error.value.reason is CredentialFailure.EXPIRED
    assert connected.vault.describe(connected.info.reference) == connected.info


def test_expiry_during_durable_commit_keeps_refresh_but_never_publishes_expired_access(connected):
    original = connected.vault._file.replace
    def delayed(data):
        original(data)
        connected.clock.advance(3601)
    connected.vault._file.replace = delayed
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(connected.info.reference)
    assert error.value.reason is CredentialFailure.EXPIRED
    status = connected.vault.status()
    assert status.credential.generation == 2
    assert status.state is ConnectionState.REFRESH_REQUIRED
    assert connected.vault._access is None and connected.vault._data.refresh is not None


def test_refresh_token_expiry_requires_consent_without_revoking_valid_access(tmp_path):
    value = bundle(tmp_path / "protected")
    value.transport.initial["refresh_token_expires_in"] = 60
    try:
        info = value.service.connect()
        value.clock.advance(61)
        assert value.service.restore().state is ConnectionState.CURRENT
        assert not value.service.restore().can_refresh
        with pytest.raises(CredentialError) as error:
            value.service.refresh(info.reference)
        assert error.value.reason is CredentialFailure.RECONNECT
        assert not value.transport.refreshes
    finally:
        value.vault.close()


def test_wall_clock_rollback_does_not_extend_access_lifetime(connected):
    runtime = runtime_for(connected)
    connected.clock._now -= timedelta(hours=2)
    connected.clock._mono += 3601
    assert connected.vault.status().state is ConnectionState.REFRESH_REQUIRED
    assert not invoke(runtime).ok and not runtime.provider.calls


def test_reentrant_protector_cannot_disconnect_or_publish_during_commit(connected):
    original = connected.protector.protect
    rejected = []
    def protect(*args, **kwargs):
        with pytest.raises(CredentialError) as error:
            connected.service.disconnect(connected.info.reference)
        rejected.append(error.value.reason)
        return original(*args, **kwargs)
    connected.protector.protect = protect
    new = connected.service.refresh(connected.info.reference)
    assert new.generation == 2 and rejected == [CredentialFailure.BUSY]


@pytest.mark.parametrize("authorized", [False, True])
def test_google_connection_has_no_authority_without_host_opt_in_and_is_absent_from_all_public_state(tmp_path, authorized):
    value = bundle(tmp_path / "protected")
    info = value.service.connect()
    value.info = info
    class Provider:
        provider_id = "google"
        def __init__(self):
            self.calls = 0
        def read(self, args, lease):
            self.calls += 1
            assert lease.reveal() == ACCESS
            return ToolResult.success({"safe": "identity response"})
    provider = Provider()
    tool = AuthenticatedReadTool(tool_spec("identity_read", "Synthetic identity provider.",
        capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS)))
    runtime = CredentialRuntime(value.vault)
    runtime.bind(tool, reference=info.reference, provider=provider, required_scopes=frozenset({"openid"}))
    model = SimpleNamespace(requests=[], replies=[
        ModelResponse(text="", tool_calls=[ToolCall(id="read-one", name=tool.name, arguments={})]),
        ModelResponse(text="Handled.")])
    def generate(request):
        model.requests.append(request)
        return model.replies.pop(0)
    model.generate = generate
    mutator = FunctionTool(tool_spec("synthetic_mutation", "Test-only journal fixture.",
                          capabilities=(Capability.FS_WRITE,), read_only=False), lambda *_: "safe mutation")
    public = FunctionTool(tool_spec("public_read", "Test-only public read.", capabilities=(Capability.NET_READ,)),
                         lambda *_: "public")
    journal = journal_for_tools([mutator])
    states = JsonFileStateStore(tmp_path / "states")
    sessions = JsonFileSessionStore(tmp_path / "sessions")
    log_path = tmp_path / "events.log"
    agent = Agent(AgentConfig(workspace_dir=str(tmp_path),
                             permissions=PermissionConfig(level=PermissionLevel.CONFIRM),
                             conversation=ConversationConfig(tools_enabled=True),
                             logging=LoggingConfig(level="DEBUG", file_path=str(log_path))),
                  AgentDependencies(tools=[tool, mutator, public], model=model, state_store=states,
                                    credential_runtime=runtime, allow_authenticated_reads=authorized,
                                    approval_gate=AlwaysApproveGate(), **journal))
    try:
        mutation = agent.executor.invoke(mutator.name)
        assert mutation.ok
        before = journal["action_store"].load(mutation.action_id).to_dict()
        approvals = agent.executor._approval_authority._store
        approval = approvals.load(approvals.list_requests()[0]).to_dict()
        conversation = agent.conversation(sessions=sessions)
        response = conversation.send("Read this identity.")
        assert response.tools_executed == int(authorized)
        agent.run_task(Task(objective="Read identity", subtasks=[Subtask(description="Read", tool_name=tool.name, tool_args={})]))
        assert provider.calls == 2 * int(authorized)
        assert agent.executor.invoke(public.name).ok
        value.service.disconnect(info.reference)
        assert not agent.executor.invoke(tool.name).ok
        agent.checkpoint()
        snapshot = states.load(agent.state.run_id)
        assert snapshot.tool_calls
        assert journal["action_store"].load(mutation.action_id).to_dict() == before
        assert not agent.executor.invoke(tool.name).ok
        serialized = json.dumps({"state": snapshot.to_dict(), "approval": approval, "journal": before,
                                 "configuration": asdict(agent.config)}, default=str)
        serialized += repr(model.requests) + log_path.read_text()
        session_files = list((tmp_path / "sessions").glob("*.json"))
        assert session_files
        serialized += "".join(path.read_text() for path in session_files)
        for secret in (ACCESS, REFRESH, ID_TOKEN, CLIENT_SECRET):
            assert secret not in serialized
        for capability in (Capability.EMAIL_SEND, Capability.PAYMENT, Capability.SYSTEM_ADMIN):
            assert not agent.executor.policy.check((capability,)).allowed
    finally:
        value.vault.close()
        configure_logging(LoggingConfig(level="CRITICAL"))
