"""Browser history never enters ordinary Agent, even through storage aliases."""

import errno
import json
import multiprocessing
import os
import subprocess
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agentcore import cli
from agentcore.browser import BrowserAgentComposition, BrowserConfig
from agentcore.browser.backend import PageText
from agentcore.conversation.errors import SessionDomainError
from agentcore.conversation.runtime import ConversationRuntime
from agentcore.conversation.session import (
    BROWSER_SESSION_MARKER, ConversationSession, InMemorySessionStore, JsonFileSessionStore,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.models.messages import Message, ModelResponse, Role
from agentcore.tools.base import FunctionTool, ToolResult, tool_spec
from test_browser_composition import Model, wants
from test_browser_session import FakeBackend
from test_cli_browser import _process_browser_turn
from test_conversation_runtime import FakeModel, config

SENTINEL = "BROWSER-HISTORY-MUST-NOT-CROSS-DOMAIN"


class Spy:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append(name)
            raise AssertionError("ordinary memory/capture invoked")
        return call


def ordinary_agent(tmp_path, directory):
    settings = config(tmp_path)
    settings = replace(settings, conversation=replace(settings.conversation,
        directory=str(directory), tools_enabled=True))
    model = Model([wants("ordinary_callback"), ModelResponse(text="Ordinary reply")])
    memory, capture, resolver = Spy(), Spy(), Spy()
    calls = []
    tool = FunctionTool(tool_spec("ordinary_callback", "ordinary tool"),
                        lambda *a: calls.append(True) or ToolResult.success("called"))
    agent = Agent(settings, AgentDependencies(model=model, tools=[tool], memory=memory,
                                            memory_capture=capture, memory_resolver=resolver))
    return agent, model, calls, (memory, capture, resolver)


def assert_no_ordinary_work(model, tools, spies):
    assert model.requests == [] and tools == []
    assert all(spy.calls == [] for spy in spies)


@pytest.mark.parametrize("failure,allowed", [
    (OSError(errno.ENAMETOOLONG, "synthetic unaddressable ID"), True),
    (PermissionError(errno.EACCES, "synthetic permission denial"), False),
    (OSError(errno.EIO, "synthetic lookup uncertainty"), False),
    (SessionDomainError("conversation_session_domain_mismatch"), False),
])
def test_supplied_session_lookup_only_ignores_unaddressable_ids(tmp_path, monkeypatch, failure, allowed):
    store = JsonFileSessionStore(tmp_path / "ordinary")
    model = FakeModel("normal reply")
    runtime = ConversationRuntime(model=model, sessions=store)
    session = runtime.start()
    before = session.to_dict()

    def exists(*args):
        raise failure

    monkeypatch.setattr(store, "exists", exists)
    if allowed:
        result = runtime.send("ordinary turn", session=session)
        assert result.ok and model.requests
    else:
        with pytest.raises(type(failure)):
            runtime.send("ordinary turn", session=session)
        assert model.requests == [] and session.to_dict() == before
    assert list(store.directory.glob("*.json")) == []


@pytest.mark.parametrize("storage", ["json", "memory"])
def test_competing_composition_domains_cannot_both_bind_store(tmp_path, monkeypatch, storage):
    import agentcore.conversation.session as persistence
    store = JsonFileSessionStore(tmp_path / "shared") if storage == "json" else InMemorySessionStore()
    barrier = threading.Barrier(2)
    original = persistence.validate_session_directory

    def validate(*args, **kwargs):
        original(*args, **kwargs)
        barrier.wait(timeout=5)

    def bind(domain):
        try:
            store.bind_domain(domain=domain)
            return domain
        except SessionDomainError:
            return None

    monkeypatch.setattr(persistence, "validate_session_directory", validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, ("ordinary", "browser_read")))
    assert results.count(None) == 1
    assert store._session_domain == next(value for value in results if value is not None)


@pytest.fixture
def persisted_browser(tmp_path):
    directory = tmp_path / ".browser_sessions"
    backend = FakeBackend()
    backend.text = PageText(SENTINEL)
    with BrowserAgentComposition(model=Model([wants("browser_extract_text"), ModelResponse(text="Read")]),
            config=BrowserConfig(enabled=True), sessions_directory=directory, _backend=backend) as browser:
        result = browser.conversation().send("Read the browser page")
        assert result.ok and result.tools_executed == 1
        assert result.session.metadata[BROWSER_SESSION_MARKER] is True
    path = directory / (result.session.id + ".json")
    assert SENTINEL in path.read_text(encoding="utf-8")
    return directory, result.session, path, path.read_bytes()


@pytest.mark.parametrize("operation", ["start", "send_id", "send_object", "save_browser", "save_forged"])
def test_direct_agent_rejects_browser_domain_before_work(tmp_path, persisted_browser, operation):
    directory, session, path, before = persisted_browser
    agent, model, calls, spies = ordinary_agent(tmp_path, tmp_path / "ordinary")
    runtime = agent.conversation()
    # Even replacing a runtime's store after composition cannot bypass checks.
    runtime.sessions = JsonFileSessionStore(directory)
    snapshot = session.to_dict()
    if operation.startswith("save"):
        candidate = session if operation == "save_browser" else ConversationSession(id=session.id)
        assert runtime.save(candidate) is False
    else:
        with pytest.raises(SessionDomainError, match="^conversation_session_domain_mismatch$") as error:
            if operation == "start":
                runtime.start(session_id=session.id)
            elif operation == "send_id":
                runtime.send("ordinary turn", session_id=session.id)
            else:
                runtime.send("ordinary turn", session=session)
        assert error.value.details == {}
    assert_no_ordinary_work(model, calls, spies)
    assert session.to_dict() == snapshot and path.read_bytes() == before
    assert list(directory.glob("*.json")) == [path]


def test_direct_agent_refuses_to_compose_with_reserved_store(tmp_path, persisted_browser):
    directory, _, path, before = persisted_browser
    agent, model, calls, spies = ordinary_agent(tmp_path, directory)
    with pytest.raises(SessionDomainError):
        agent.conversation()
    assert_no_ordinary_work(model, calls, spies)
    assert path.read_bytes() == before


def test_reserved_domain_cannot_fall_back_when_storage_is_unavailable(
        tmp_path, persisted_browser, monkeypatch):
    import agentcore.conversation.session as persistence
    directory, _, path, before = persisted_browser
    agent, model, calls, spies = ordinary_agent(tmp_path, directory)
    monkeypatch.setattr(persistence, "build_session_store",
                        lambda *a: pytest.fail("foreign storage constructed before domain rejection"))
    with pytest.raises(SessionDomainError):
        agent.conversation()
    assert_no_ordinary_work(model, calls, spies)
    assert path.read_bytes() == before


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("force_new", [False, True])
def test_chat_categorical_alias_rejection(tmp_path, persisted_browser, monkeypatch, capsys,
                                        json_mode, force_new):
    directory, session, path, before = persisted_browser
    agent, model, calls, spies = ordinary_agent(tmp_path, directory)
    monkeypatch.setattr(cli, "Agent", lambda *a: agent)
    monkeypatch.setattr(cli, "load_config", lambda *a: agent.config)
    args = ["chat", "--session", session.id, "--message", "ordinary turn"]
    status = cli.main(args + (["--json"] if json_mode else []) + (["--new"] if force_new else []))
    captured = capsys.readouterr()
    assert status == 2
    if json_mode:
        assert json.loads(captured.out) == {"ok": False, "error": "conversation_session_domain_mismatch"}
        assert not captured.err
    else:
        assert not captured.out
        assert captured.err == "Conversation session belongs to a different session domain.\n"
    assert SENTINEL not in captured.out + captured.err and session.id not in captured.out + captured.err
    assert_no_ordinary_work(model, calls, spies)
    assert path.read_bytes() == before and list(directory.glob("*.json")) == [path]


@pytest.mark.parametrize("storage", ["json", "memory"])
def test_marker_boundary_outside_reserved_directory(tmp_path, persisted_browser, storage, caplog):
    _, session, _, _ = persisted_browser
    store = (JsonFileSessionStore(tmp_path / "other") if storage == "json" else InMemorySessionStore())
    store.save(session)
    snapshot = (store.directory / (session.id + ".json")).read_bytes() if storage == "json" else dict(store._data)
    agent, model, calls, spies = ordinary_agent(tmp_path, tmp_path / "ordinary")
    runtime = agent.conversation(sessions=store)
    with pytest.raises(SessionDomainError):
        runtime.start(session_id=session.id)
    with pytest.raises(SessionDomainError):
        runtime.send("ordinary", session=session)
    assert runtime.save(ConversationSession(id=session.id)) is False
    assert_no_ordinary_work(model, calls, spies)
    # The public bound store cannot be used as an escape hatch either.
    for access in (lambda: store.load(session.id),
                   lambda: store.load(session.id, session_domain="browser_read"),
                   lambda: store.save(ConversationSession(id=session.id)),
                   lambda: store.delete(session.id),
                   lambda: store.bind_domain(domain="browser_read")):
        with pytest.raises(SessionDomainError):
            access()
    after = (store.directory / (session.id + ".json")).read_bytes() if storage == "json" else store._data
    assert after == snapshot
    assert SENTINEL not in caplog.text


def test_filesystem_alias_cannot_bypass_reserved_domain(tmp_path, persisted_browser):
    directory, session, path, before = persisted_browser
    alias = tmp_path / "alias"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(directory)],
                       check=True, capture_output=True, timeout=10)
    else:
        alias.symlink_to(directory, target_is_directory=True)
    agent, model, calls, spies = ordinary_agent(tmp_path, alias)
    with pytest.raises(SessionDomainError):
        agent.conversation().send("ordinary", session_id=session.id)
    assert_no_ordinary_work(model, calls, spies)
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["start", "send", "store_save"])
def test_reserved_directory_rejects_before_browser_first_save(tmp_path, operation):
    directory = tmp_path / ".browser_sessions"
    agent, model, calls, spies = ordinary_agent(tmp_path, tmp_path / "ordinary")
    runtime = agent.conversation()
    if operation == "store_save":
        runtime.sessions.directory = directory
    else:
        runtime.sessions = JsonFileSessionStore(directory)
    with pytest.raises(SessionDomainError):
        if operation == "start":
            runtime.start(session_id="not_yet_saved")
        elif operation == "send":
            runtime.send("ordinary", session=ConversationSession(id="not_yet_saved"))
        else:
            runtime.sessions.save(ConversationSession(id="not_yet_saved"))
    assert_no_ordinary_work(model, calls, spies)
    assert list(directory.glob("*.json")) == []


def test_foreign_corrupt_history_never_falls_back_to_fresh(tmp_path, persisted_browser, caplog):
    _, session, _, _ = persisted_browser
    store = JsonFileSessionStore(tmp_path / "other")
    store.save(session)
    path = store.directory / (session.id + ".json")
    data = json.loads(path.read_text())
    data["messages"][0]["role"] = SENTINEL
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    agent, model, calls, spies = ordinary_agent(tmp_path, store.directory)
    with pytest.raises(SessionDomainError):
        agent.conversation().send("ordinary", session_id=session.id)
    assert_no_ordinary_work(model, calls, spies)
    assert path.read_bytes() == before and SENTINEL not in caplog.text


def test_normal_ordinary_and_browser_resumes_remain_compatible(tmp_path, persisted_browser):
    directory, browser_session, _, _ = persisted_browser
    settings = config(tmp_path, memory=False)
    settings = replace(settings, conversation=replace(settings.conversation,
        directory=str(tmp_path / "ordinary")))
    model = FakeModel("normal reply")
    agent = Agent(settings, AgentDependencies(model=model, tools=[]))
    first = agent.conversation().send("normal history")
    resumed = agent.conversation().send("normal continuation", session_id=first.session.id)
    assert resumed.ok and resumed.session.id == first.session.id
    assert [m.content for m in resumed.session.messages] == [
        "normal history", "normal reply", "normal continuation", "normal reply"]
    browser_model = FakeModel("browser continuation")
    with BrowserAgentComposition(model=browser_model, config=BrowserConfig(enabled=True),
            sessions_directory=directory, _backend=FakeBackend()) as browser:
        result = browser.conversation().send("continue", session_id=browser_session.id)
    assert result.ok and result.session.id == browser_session.id
    assert SENTINEL in browser_model.prompt_text()
    assert any(m.role is Role.TOOL for m in result.session.messages)


def test_concurrent_browser_lease_cannot_be_bypassed_by_ordinary_agent(tmp_path, persisted_browser):
    directory, session, path, before = persisted_browser
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_process_browser_turn,
                              args=(str(tmp_path), session.id, "model", child))
    process.start()
    child.close()
    try:
        assert parent.poll(25)
        assert parent.recv()[0] == "held"
        agent, model, calls, spies = ordinary_agent(tmp_path, directory)
        with pytest.raises(SessionDomainError):
            agent.conversation().send("overwrite browser", session_id=session.id)
        assert_no_ordinary_work(model, calls, spies)
        assert path.read_bytes() == before
        parent.send("release")
        assert parent.poll(25)
        kind, finished = parent.recv()
        assert kind == "done" and finished["status"] == 0
        process.join(25)
        assert process.exitcode == 0
        saved = JsonFileSessionStore(directory).load(session.id)
        assert saved.metadata[BROWSER_SESSION_MARKER] is True
        assert [m.content for m in saved.messages if m.role is Role.USER] == [
            "Read the browser page", "Read this page"]
        assert SENTINEL in path.read_text(encoding="utf-8")
        assert "overwrite browser" not in path.read_text(encoding="utf-8")
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        parent.close()
