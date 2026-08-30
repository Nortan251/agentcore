"""Gmail uses the real registry, policy, executor, generation and data fences."""

import json
import logging
import os
import signal
import threading

import pytest

from conftest import journal_for_tools
from agentcore.config.schema import PermissionConfig
from agentcore.conversation.tool_use import ConversationToolRunner
from agentcore.credentials import (
    AccountIdentity, CredentialError, CredentialFailure, CredentialRuntime,
    GoogleAccessProfile, InMemoryCredentialVault, SecretMaterial,
)
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_READONLY_SCOPES, GOOGLE_ISSUER
from agentcore.integrations.gmail import GmailReadonlyTools
from agentcore.integrations.gmail.http import GmailReadHttp
from agentcore.models.messages import ToolCall
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import AlwaysApproveGate, PermissionPolicy
from agentcore.tools.base import FunctionTool, ToolContext, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from gmail_fakes import (
    FakeGmail, GMAIL_SCOPES, MESSAGE_ID, PRIVATE_QUERY, READ, SEARCH,
    connected, grant_gmail, setup,
)
from google_oauth_fakes import ACCESS, REFRESH, SUBJECT


@pytest.fixture
def ready(tmp_path):
    value = connected(tmp_path / "protected")
    runtime = setup(value, journal=journal_for_tools([]))
    yield runtime
    value.vault.close()


@pytest.mark.parametrize("name, args", [
    (SEARCH, {"q": "x" * 1025}), (SEARCH, {"q": "x\nprivate"}), (SEARCH, {"q": "x\rprivate"}),
    (SEARCH, {"q": "\x00"}), (SEARCH, {"q": "\x7f"}), (SEARCH, {"q": "\x85"}),
    (SEARCH, {"q": "\ud800"}), (SEARCH, {"q": None}),
    (SEARCH, {"max_results": 0}), (SEARCH, {"max_results": 51}), (SEARCH, {"max_results": True}),
    (SEARCH, {"max_results": 1.5}), (SEARCH, {"page_token": "x" * 2049}), (SEARCH, {"page_token": "x\n"}),
    (READ, {"message_id": ""}), (READ, {"message_id": "x" * 129}), (READ, {"message_id": "../message"}),
    (READ, {"message_id": "https://attacker.test"}), (READ, {"message_id": "x?userId=other"}),
    (READ, {"message_id": "x/y"}), (READ, {"message_id": "x%2fy"}), (READ, {"message_id": "é"}),
], ids=[f"invalid-{i}" for i in range(22)])
def test_arguments_are_bounded_before_permission_vault_or_provider(ready, monkeypatch, name, args):
    def forbidden(*args, **kwargs):
        pytest.fail("malformed arguments reached permission or credential resolution")
    monkeypatch.setattr(ready.executor.policy, "check", forbidden)
    monkeypatch.setattr(ready.google.vault, "_lease", forbidden)
    result = ready.executor.invoke(name, args)
    assert not result.ok and result.result.metadata["kind"] == "validation"
    assert not ready.transport.calls


@pytest.mark.parametrize("selector", [
    "account", "userId", "email", "provider", "issuer", "subject", "scope", "scopes",
    "profile", "access_profile", "client_id", "redirect_uri", "url", "path", "format",
    "access_token", "credential_reference", "allow_authenticated_reads",
])
@pytest.mark.parametrize("name", [SEARCH, READ])
def test_model_cannot_select_account_endpoint_credentials_or_consent(ready, selector, name):
    args = {} if name == SEARCH else {"message_id": MESSAGE_ID}
    args[selector] = "model-selected"
    result = ready.executor.invoke(name, args)
    assert result.result.metadata["kind"] == "validation"
    assert not ready.transport.calls
    assert len(ready.google.transport.exchanges) == 1 and not ready.google.transport.refreshes


@pytest.mark.parametrize("maximum", [None, 1, 50])
def test_bounded_search_default_and_caps_never_page_or_read_automatically(ready, maximum):
    args = {} if maximum is None else {"max_results": maximum}
    result = ready.executor.invoke(SEARCH, args)
    assert result.ok and len(ready.transport.calls) == 1
    assert ready.transport.calls[0][:2] == ("list", {"max_results": maximum or 10})
    assert result.result.output["next_page_token"]


def test_provider_cannot_exceed_requested_result_count(ready):
    ready.transport.list_result["messages"] *= 2
    result = ready.executor.invoke(SEARCH, {"max_results": 1})
    assert result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert len(ready.transport.calls) == 1


@pytest.mark.parametrize("change", ["count", "id", "thread", "page", "estimate"])
def test_untrusted_list_references_are_validated(ready, change):
    raw = ready.transport.list_result
    if change == "count":
        raw["messages"] *= 51
    elif change == "id":
        raw["messages"][0]["id"] = "../unsafe"
    elif change == "thread":
        raw["messages"][0]["threadId"] = "unsafe?query"
    elif change == "page":
        raw["nextPageToken"] = "x" * 2049
    else:
        raw["resultSizeEstimate"] = True
    result = ready.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value


def test_explicit_gmail_opt_in_capabilities_and_pure_read_journal_boundary(ready):
    assert set(ready.registry.names()) == {SEARCH, READ}
    assert all(tool.spec.read_only and tool.capabilities == frozenset({
        Capability.NET_READ, Capability.CREDENTIAL_ACCESS}) for tool in ready.gmail.tools)
    store = ready.executor._action_store
    assert store.list_actions() == []
    assert ready.executor.invoke(SEARCH).ok
    assert ready.executor.invoke(READ, {"message_id": MESSAGE_ID}).ok
    assert store.list_actions() == []
    ready.executor.policy = PermissionPolicy(allow_authenticated_reads=False)
    assert ready.executor.invoke(SEARCH).result.metadata["kind"] == "permission_denied"
    public = FunctionTool(tool_spec("public_read", "Public network fixture.", capabilities=(Capability.NET_READ,)),
                          lambda *_: "public")
    ready.registry.register(public)
    assert ready.executor.invoke(public.name).ok
    assert len(ready.transport.calls) == 2
    for level in PermissionLevel:
        policy = PermissionPolicy(PermissionConfig(level=level), allow_authenticated_reads=True,
                                  approval_gate=AlwaysApproveGate())
        for capability in (Capability.EMAIL_SEND, Capability.PAYMENT, Capability.SYSTEM_ADMIN):
            assert not policy.check((capability,)).allowed


@pytest.mark.parametrize("blocked", [Capability.NET_READ, Capability.CREDENTIAL_ACCESS])
def test_explicit_capability_blocks_still_win(ready, blocked):
    ready.executor.policy = PermissionPolicy(PermissionConfig(blocked_capabilities=(str(blocked),)),
                                             allow_authenticated_reads=True)
    assert ready.executor.invoke(SEARCH).result.metadata["kind"] == "permission_denied"
    assert not ready.transport.calls


def test_broader_host_supplied_gmail_token_is_not_accepted_as_readonly():
    vault = InMemoryCredentialVault()
    try:
        info = vault.register(AccountIdentity("google", GOOGLE_ISSUER, SUBJECT), SecretMaterial(ACCESS),
                              scopes=GOOGLE_GMAIL_READONLY_SCOPES | {"https://www.googleapis.com/auth/gmail.modify"})
        runtime, transport = CredentialRuntime(vault), FakeGmail()
        gmail = GmailReadonlyTools(transport=transport)
        gmail.bind(runtime, info.reference)
        registry = ToolRegistry()
        registry.register_all(list(gmail.tools))
        executor = ToolExecutor(registry, PermissionPolicy(allow_authenticated_reads=True), credential_runtime=runtime)
        assert executor.invoke(SEARCH).result.metadata["kind"] == CredentialFailure.SCOPE_MISMATCH.value
        assert not transport.calls
    finally:
        vault.close()


def test_tools_cannot_run_directly_using_historical_or_fabricated_authority(ready):
    context = ToolContext(metadata={"scopes": GMAIL_SCOPES, "allow_authenticated_reads": True,
                                    "credential_reference": ready.google.info.reference})
    for tool in ready.gmail.tools:
        with pytest.raises(CredentialError) as error:
            tool.run({}, context)
        assert error.value.reason is CredentialFailure.AUTHORITY
    assert not ready.transport.calls


def test_refresh_requires_explicit_new_generation_binding_and_disconnect_revokes_it(ready):
    assert ready.executor.invoke(SEARCH).ok
    new = ready.google.service.refresh(ready.google.info.reference)
    assert new.account_id == ready.google.info.account_id
    assert new.connection_id == ready.google.info.connection_id
    assert new.generation == ready.google.info.generation + 1
    assert ready.executor.invoke(SEARCH).result.metadata["kind"] == CredentialFailure.STALE.value
    assert len(ready.transport.calls) == 1
    ready.gmail.bind(ready.runtime, new.reference, replace_existing=True)
    assert ready.executor.invoke(READ, {"message_id": MESSAGE_ID}).ok
    assert ready.transport.calls[-1][-1] == "replacement-" + ACCESS
    ready.google.service.disconnect(new.reference)
    assert ready.executor.invoke(SEARCH).result.metadata["kind"] == CredentialFailure.REVOKED.value
    assert ready.executor.invoke(READ, {"message_id": MESSAGE_ID}).result.metadata["kind"] == CredentialFailure.REVOKED.value
    assert len(ready.transport.calls) == 2


def test_expiry_does_not_refresh_or_open_browser_during_tool_invocation(ready):
    ready.google.clock.advance(3601)
    assert ready.executor.invoke(SEARCH).result.metadata["kind"] == CredentialFailure.EXPIRED.value
    assert not ready.google.transport.refreshes and len(ready.google.transport.exchanges) == 1
    new = ready.google.service.ensure_current(ready.google.info.reference)
    ready.gmail.bind(ready.runtime, new.reference, replace_existing=True)
    assert ready.executor.invoke(SEARCH).ok


@pytest.mark.parametrize("scope", [None, "openid email profile", "openid https://www.googleapis.com/auth/gmail.readonly",
                                  GMAIL_SCOPES + " https://www.googleapis.com/auth/gmail.send"])
def test_refresh_cannot_publish_incomplete_or_broader_gmail_profile(ready, scope):
    old = ready.google.info
    if scope is None:
        del ready.google.transport.refreshed["scope"]
        new = ready.google.service.refresh(old.reference)
        assert new.scopes == GOOGLE_GMAIL_READONLY_SCOPES
    else:
        ready.google.transport.refreshed["scope"] = scope
        with pytest.raises(CredentialError):
            ready.google.service.refresh(old.reference)
        assert ready.google.service.restore().credential == old
        assert not ready.google.service.restore().can_refresh
        # Truthful existing access remains usable until its already-bound expiry.
        assert ready.executor.invoke(SEARCH).ok
        ready.google.clock.advance(3601)
        assert not ready.executor.invoke(SEARCH).ok


def test_reconnect_requires_new_concrete_tools_and_explicit_registry_replacement(ready):
    new = grant_gmail(ready.google)
    assert ready.executor.invoke(SEARCH).result.metadata["kind"] == CredentialFailure.STALE.value
    with pytest.raises(CredentialError) as error:
        ready.gmail.bind(ready.runtime, new.reference, replace_existing=True)
    assert error.value.reason is CredentialFailure.IDENTITY_MISMATCH
    replacement = GmailReadonlyTools(transport=ready.transport)
    replacement.bind(ready.runtime, new.reference, replace_existing=True)
    ready.registry.register_all(list(replacement.tools), replace=True)
    assert ready.executor.invoke(SEARCH).ok


def test_active_gmail_read_holds_leases_without_holding_metadata_mutexes(ready):
    entered, release = threading.Event(), threading.Event()
    results = []
    def pause():
        entered.set()
        assert release.wait(5)
    ready.transport.on_read = pause
    thread = threading.Thread(target=lambda: results.append(ready.executor.invoke(READ, {"message_id": MESSAGE_ID})),
                              daemon=True)
    thread.start()
    assert entered.wait(2)
    try:
        for lock in (ready.google.vault._lock, ready.runtime._lock, ready.executor._policy_lock, ready.registry._lock):
            assert lock.acquire(blocking=False)
            lock.release()
        for action in (
            lambda: ready.google.service.refresh(ready.google.info.reference),
            lambda: ready.google.service.disconnect(ready.google.info.reference),
            lambda: ready.google.service.connect(profile=GoogleAccessProfile.GMAIL_READONLY),
            lambda: setattr(ready.executor, "policy", PermissionPolicy()),
        ):
            with pytest.raises(CredentialError) as error:
                action()
            assert error.value.reason is CredentialFailure.BUSY
        assert ready.google.service.restore().credential == ready.google.info
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and results[0].ok
    ready.google.service.disconnect(ready.google.info.reference)
    assert not ready.executor.invoke(SEARCH).ok


@pytest.mark.parametrize("surface", ["typed", "subclass", "forged_reason"])
def test_safe_provider_categories_preserve_no_exception_text_or_notes(ready, surface, caplog):
    caplog.set_level(logging.DEBUG, logger="agentcore")
    class Subclass(CredentialError):
        pass
    error = (Subclass if surface == "subclass" else CredentialError)(CredentialFailure.READ_RATE_LIMIT)
    error.args = (ACCESS + PRIVATE_QUERY,)
    error.add_note(REFRESH)
    if surface == "forged_reason":
        error.reason = ACCESS
    ready.transport.error = error
    result = ready.executor.invoke(SEARCH)
    expected = CredentialFailure.READ_RATE_LIMIT if surface == "typed" else CredentialFailure.PROVIDER
    assert result.result.metadata["kind"] == expected.value
    public = json.dumps(result.to_dict()) + caplog.text + repr([r.__dict__ for r in caplog.records])
    for secret in (ACCESS, REFRESH, PRIVATE_QUERY):
        assert secret not in public


def test_search_query_is_not_logged_even_when_model_repeats_it_in_call_id(ready, caplog):
    caplog.set_level(logging.DEBUG, logger="agentcore")
    runner = ConversationToolRunner(ready.executor, ready.registry)
    outcome = runner.run_calls([ToolCall(name=SEARCH, arguments={"q": PRIVATE_QUERY}, id=PRIVATE_QUERY)]).outcomes[0]
    assert outcome.ok
    assert PRIVATE_QUERY not in caplog.text
    assert PRIVATE_QUERY not in repr([r.__dict__ for r in caplog.records])
    assert PRIVATE_QUERY not in json.dumps(outcome.describe())


def test_gmail_pid_fences_precede_credential_transport_and_runtime_access(ready, monkeypatch):
    http = GmailReadHttp()
    pid = os.getpid()
    actions = (
        lambda: ready.gmail.tools,
        lambda: ready.gmail.bind(ready.runtime, ready.google.info.reference),
        lambda: ready.gmail._providers[0].read({}, None),
        lambda: ready.gmail._providers[1].read({}, None),
        lambda: http.list_messages(None, {}),
        lambda: http.read_message(None, MESSAGE_ID),
        lambda: ready.runtime._snapshot(ready.gmail._tools[0]),
    )
    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: pid + 1)
        for action in actions:
            with pytest.raises(CredentialError) as error:
                action()
            assert error.value.reason is CredentialFailure.OWNERSHIP
    assert not ready.transport.calls


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_real_fork_rejects_inherited_gmail_authority_before_poisoned_locks(ready):
    import select

    http = GmailReadHttp()
    locked, release = threading.Event(), threading.Event()
    def hold():
        with ready.google.vault._lock, ready.runtime._lock, ready.executor._policy_lock:
            locked.set()
            release.wait(10)
    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert locked.wait(2)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        rejected = 0
        for action in (
            lambda: ready.gmail.tools,
            lambda: ready.gmail._providers[0].read({}, None),
            lambda: ready.gmail._providers[1].read({}, None),
            lambda: http.list_messages(None, {}),
            lambda: ready.runtime._snapshot(ready.gmail._tools[0]),
            lambda: ready.google.service.ensure_current(ready.google.info.reference),
        ):
            try:
                action()
            except CredentialError as error:
                rejected += error.reason is CredentialFailure.OWNERSHIP
        os.write(write_fd, str(rejected).encode())
        os._exit(0)
    os.close(write_fd)
    reaped = False
    try:
        assert select.select([read_fd], [], [], 5)[0], "inherited lock touched"
        assert os.read(read_fd, 16) == b"6"
        assert os.waitpid(child, 0)[1] == 0
        reaped = True
    finally:
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(read_fd)
        release.set()
        thread.join(2)
    assert not thread.is_alive() and not ready.transport.calls
