"""CLI -> existing provider -> isolated browser; all I/O is synthetic."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket

import pytest

from agentcore import cli
from agentcore.browser import cli as host
from agentcore.browser.backend import PageLink, PageLinks, PageText
from agentcore.browser.composition import BROWSER_TOOL_NAMES, BrowserAgentComposition
from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.conversation.session import ConversationSession, JsonFileSessionStore
from agentcore.models.providers import openai_compatible
from agentcore.models.transport import HttpClient, RetryPolicy
from test_browser_session import FakeBackend
from test_models_provider import StrictToolTransport, http, ok_body

URL = "https://example.com/"
OTHER = "https://other.example.com/"
REPLY = "The page reports a new Python release."


def tool_response(*calls):
    body = ok_body()
    body["choices"][0] = {"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}
            for i, (name, arguments) in enumerate(calls)
        ]}}
    return http(200, body)


def open_response(url):
    return tool_response(("browser_open_url", {"url": url}))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    for name in os.environ:
        if name.startswith("AGENTCORE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("AGENTCORE_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTCORE_MODEL__PROVIDER", "openai_compatible")
    monkeypatch.setenv("AGENTCORE_MODEL__NAME", "synthetic-model")
    monkeypatch.setenv("AGENTCORE_MODEL__BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("AGENTCORE_MODEL__API_KEY_ENV", "BROWSER_CLI_TEST_KEY")
    monkeypatch.setenv("BROWSER_CLI_TEST_KEY", "synthetic-provider-credential")
    transport = StrictToolTransport()
    monkeypatch.setattr(openai_compatible, "HttpClient", lambda **kwargs: HttpClient(
        transport=transport, retry=RetryPolicy(max_attempts=1)))
    backends, compositions = [], []

    def compose(**kwargs):
        backend = FakeBackend()
        backend.text = PageText("Python release news. " + OTHER)
        backend.links = PageLinks((PageLink(OTHER, "Another page"),))
        backends.append(backend)
        composition = BrowserAgentComposition(**kwargs, _backend=backend)
        compositions.append(composition)
        return composition

    monkeypatch.setattr(host, "BrowserAgentComposition", compose)

    def no_network(*args, **kwargs):
        pytest.fail("automated CLI tests must not use network/DNS")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "socket", no_network)
    return transport, backends, compositions


def invoke(capsys, *, grants=(URL,), prompt="Read this page", session=None, json_mode=True):
    args = ["browser"]
    for url in grants:
        args += ["--allow-url", url]
    if session is not None:
        args += ["--session", session]
    if json_mode:
        args += ["--json"]
    status = cli.main(args + [prompt])
    captured = capsys.readouterr()
    if json_mode:
        assert not captured.err
        return status, json.loads(captured.out)
    return status, captured


def finish(transport, *responses, reply=REPLY):
    transport.responses.extend([*responses, http(200, ok_body(reply))])


def test_provider_tool_use_and_browser_only_persistence(harness, capsys, tmp_path, monkeypatch):
    transport, backends, compositions = harness
    monkeypatch.setattr(cli, "Agent", lambda *a, **k: pytest.fail("ordinary Agent constructed"))
    monkeypatch.setenv("AGENTCORE_CONVERSATION__DIRECTORY", str(tmp_path / "ordinary"))
    finish(transport, open_response(URL), tool_response(("browser_extract_text", {})))
    status, payload = invoke(capsys)
    assert status == 0 and payload == {
        "ok": True, "session_id": payload["session_id"], "reply": REPLY, "tools_executed": 2}
    assert backends[0].calls == ["start", "open_url", "current_page", "extract_text", "close"]
    assert len(transport.requests) == 3
    for request in transport.requests:
        tools = request["payload"]["tools"]
        assert {tool["function"]["name"] for tool in tools} == set(BROWSER_TOOL_NAMES)
        for tool in tools:
            schema = tool["function"]["parameters"]
            assert schema["additionalProperties"] is False
            assert set(schema["properties"]) == ({"url"} if tool["function"]["name"] == "browser_open_url" else set())
    messages = transport.requests[-1]["payload"]["messages"]
    assert any(m["role"] == "tool" and "UNTRUSTED_DATA" in m["content"] and "Python release news" in m["content"] for m in messages)
    browser = compositions[0]
    assert browser.executor._credential_runtime is None and browser.executor._action_store is None
    assert all(getattr(browser.conversation(), field) is None for field in ("memory", "capture", "resolver", "state"))
    saved = tmp_path / ".browser_sessions" / (payload["session_id"] + ".json")
    data = json.loads(saved.read_text())
    assert data["metadata"]["browser_read_isolated_v1"] is True
    assert not (tmp_path / "ordinary").exists()
    assert "synthetic-provider-credential" not in saved.read_text()
    assert "arguments" not in saved.read_text()


@pytest.mark.parametrize("proposed,grant,authorized", [
    (OTHER, URL, False),
    (URL, URL, True),
    ("https://EXAMPLE.com:443/#section", URL, True),
    (URL + "different", URL, False),
    (URL + "?q=private", URL, False),
    (URL + "?q=approved#one", URL + "?q=approved#two", True),
])
def test_only_exact_canonical_host_grant(harness, capsys, proposed, grant, authorized):
    transport, backends, _ = harness
    finish(transport, open_response(proposed))
    status, payload = invoke(capsys, grants=(grant,), prompt="Please open " + proposed)
    assert payload["ok"] is authorized
    assert status == (0 if authorized else 1)
    if authorized:
        assert backends[0].url == grant.split("#")[0]
    else:
        assert payload["error"] == BrowserFailure.NAVIGATION_NOT_AUTHORIZED.value
        assert payload["reply"] is None and backends[0].calls == []
        assert proposed not in json.dumps(payload)


def test_prompt_url_does_not_grant_and_prompt_is_model_input(harness, capsys):
    transport, backends, _ = harness
    finish(transport, open_response(OTHER))
    _, payload = invoke(capsys, prompt="Open " + OTHER)
    assert payload["error"] == BrowserFailure.NAVIGATION_NOT_AUTHORIZED.value
    assert backends[0].calls == []
    assert any(OTHER in m["content"] for m in transport.requests[0]["payload"]["messages"] if m["role"] == "user")
    assert all(URL not in m["content"] for m in transport.requests[0]["payload"]["messages"])


def test_page_links_and_text_cannot_grant(harness, capsys):
    transport, backends, _ = harness
    finish(transport, open_response(URL),
           tool_response(("browser_extract_text", {}), ("browser_list_links", {})),
           open_response(OTHER))
    _, payload = invoke(capsys)
    assert payload["error"] == BrowserFailure.NAVIGATION_NOT_AUTHORIZED.value
    assert backends[0].calls.count("open_url") == 1
    assert any(OTHER in m["content"] for m in transport.requests[-1]["payload"]["messages"] if m["role"] == "tool")


def test_resume_does_not_reauthorize_history(harness, capsys, tmp_path):
    transport, backends, _ = harness
    finish(transport, open_response(URL))
    _, first = invoke(capsys)
    finish(transport, open_response(URL))
    _, resumed = invoke(capsys, grants=(OTHER,), session=first["session_id"])
    assert resumed["session_id"] == first["session_id"]
    assert resumed["error"] == BrowserFailure.NAVIGATION_NOT_AUTHORIZED.value
    assert backends[1].calls == []
    messages = transport.requests[-1]["payload"]["messages"]
    assert any(m["role"] == "user" and "HISTORICAL TOOL RESULT" in m["content"] and "example.com" in m["content"] for m in messages)


def test_multiple_grants_authorize_only_listed_urls(harness, capsys):
    transport, backends, _ = harness
    finish(transport, tool_response(("browser_open_url", {"url": URL}),
                                   ("browser_open_url", {"url": OTHER})),
           open_response(OTHER + "not-listed"))
    _, payload = invoke(capsys, grants=(URL, OTHER))
    assert payload["error"] == BrowserFailure.NAVIGATION_NOT_AUTHORIZED.value
    assert payload["tools_executed"] == 2 and backends[0].calls.count("open_url") == 2


def test_grant_limit_reuses_authority_contract(harness, capsys):
    transport, backends, _ = harness
    _, payload = invoke(capsys, grants=(URL,) * 17)
    assert payload["error"] == BrowserFailure.CONTRACT.value
    assert backends[0].calls == [] and not transport.requests


def test_sixteen_grants_remain_supported(harness, capsys):
    transport, _, _ = harness
    grants = tuple(URL + str(i) for i in range(16))
    finish(transport, open_response(grants[-1]))
    assert invoke(capsys, grants=grants)[1]["ok"]


def test_invalid_grant_never_publishes_partial_authority(harness, capsys):
    transport, backends, _ = harness
    _, payload = invoke(capsys, grants=(URL, "https://user:secret@127.0.0.1/"))
    assert payload["error"] == BrowserFailure.DESTINATION.value
    assert backends[0].calls == [] and not transport.requests
    assert "secret" not in json.dumps(payload)


def test_unavailable_composition_has_no_fallback(harness, capsys, monkeypatch):
    transport, backends, _ = harness
    monkeypatch.setattr(FakeBackend, "network_isolated", False)
    _, payload = invoke(capsys)
    assert payload["error"] == BrowserFailure.NETWORK_BOUNDARY.value
    assert not transport.requests and backends[0].calls == []


def test_permission_policy_still_blocks_network_reads(harness, capsys, monkeypatch):
    transport, backends, _ = harness
    monkeypatch.setenv("AGENTCORE_PERMISSIONS__BLOCKED_CAPABILITIES", "net.read")
    finish(transport, open_response(URL))
    _, payload = invoke(capsys)
    assert not payload["ok"] and payload["tools_executed"] == 0
    assert backends[0].calls == []


def test_model_cannot_select_browser_settings(harness, capsys):
    transport, backends, _ = harness
    finish(transport, tool_response(("browser_open_url", {"url": URL, "headers": {"Authorization": "secret"}})))
    _, payload = invoke(capsys)
    assert payload["error"] == "browser_tool_failed"
    assert backends[0].calls == [] and "secret" not in json.dumps(payload)


@pytest.mark.parametrize("provider,name", [("none", "none"), ("unknown", "model"), ("openai", "none")])
def test_model_config_fails_before_browser_or_provider_io(harness, capsys, monkeypatch, provider, name):
    transport, backends, _ = harness
    monkeypatch.setenv("AGENTCORE_MODEL__PROVIDER", provider)
    monkeypatch.setenv("AGENTCORE_MODEL__NAME", name)
    _, payload = invoke(capsys)
    assert payload["error"] == "model_configuration_failed"
    assert not backends and not transport.requests


def test_no_ordinary_tool_fallback(harness, capsys):
    transport, backends, _ = harness
    finish(transport, tool_response(("read_file", {"path": "private.txt"})))
    _, payload = invoke(capsys)
    assert payload["error"] == "browser_tool_failed"
    assert payload["tools_executed"] == 0 and backends[0].calls == []
    assert "private.txt" not in json.dumps(payload) and "read_file" not in json.dumps(payload)


@pytest.mark.parametrize("session", ["missing", "../ordinary", "C:private", "a" * 129])
def test_missing_or_invalid_session_never_calls_model(harness, capsys, session):
    transport, backends, _ = harness
    _, payload = invoke(capsys, session=session)
    assert payload["error"] == "browser_session_invalid"
    assert not transport.requests and all(not backend.calls for backend in backends)


def test_ordinary_session_marker_rejected(harness, capsys, tmp_path):
    transport, backends, _ = harness
    store = JsonFileSessionStore(tmp_path / ".browser_sessions")
    store.save(ConversationSession(id="ordinary"))
    _, payload = invoke(capsys, session="ordinary")
    assert payload["error"] == BrowserFailure.CONTRACT.value
    assert not transport.requests and backends[0].calls == []


def test_forged_session_identity_rejected(harness, capsys, tmp_path):
    transport, backends, _ = harness
    path = tmp_path / ".browser_sessions"
    store = JsonFileSessionStore(path)
    session = ConversationSession(id="different", metadata={"browser_read_isolated_v1": True})
    store.save(session)
    (path / "different.json").rename(path / "requested.json")
    _, payload = invoke(capsys, session="requested")
    assert payload["error"] == "browser_session_invalid"
    assert not transport.requests and backends[0].calls == []


@pytest.mark.parametrize("failure", list(BrowserFailure))
def test_categorical_backend_failure_and_clean_human_error(harness, capsys, monkeypatch, failure):
    transport, backends, _ = harness

    def fail(self, url):
        raise BrowserError(failure)

    monkeypatch.setattr(FakeBackend, "open_url", fail)
    finish(transport, open_response(URL))
    status, captured = invoke(capsys, json_mode=False)
    assert status == 1 and not captured.out
    assert captured.err == host._MESSAGES[failure.value] + "\n"
    assert "Traceback" not in captured.err and URL not in captured.err


def test_cleanup_uncertainty_overrides_success(harness, capsys, monkeypatch):
    transport, _, _ = harness
    monkeypatch.setattr(FakeBackend, "close", lambda self: (_ for _ in ()).throw(RuntimeError("secret cleanup URL " + URL)))
    finish(transport, open_response(URL))
    status, payload = invoke(capsys)
    assert status == 1 and payload["error"] == BrowserFailure.CLEANUP.value
    assert payload["reply"] is None and payload["tools_executed"] == 1
    assert payload["session_id"] is not None
    assert "secret" not in json.dumps(payload) and URL not in json.dumps(payload)


def test_provider_failure_never_prints_exception_details(harness, capsys):
    transport, _, _ = harness
    transport.responses.append(RuntimeError("secret provider URL 192.0.2.1 Authorization: value"))
    _, payload = invoke(capsys)
    assert payload["error"] == "model_failed" and payload["reply"] is None
    assert "secret" not in json.dumps(payload) and "192.0.2.1" not in json.dumps(payload)


def test_config_error_is_categorical_before_browser_io(harness, capsys, monkeypatch):
    transport, backends, _ = harness
    monkeypatch.setenv("AGENTCORE_MODEL__BASE_URL", "secret-url")
    _, payload = invoke(capsys)
    assert payload["error"] == "model_configuration_failed"
    assert "secret-url" not in json.dumps(payload) and not backends and not transport.requests


def test_config_file_and_environment_precedence(harness, capsys, monkeypatch, tmp_path):
    transport, _, _ = harness
    config = tmp_path / "browser.toml"
    config.write_text('[model]\nprovider = "none"\nname = "from-file"\n')
    monkeypatch.delenv("AGENTCORE_MODEL__NAME")
    finish(transport)
    assert cli.main(["--config", str(config), "browser", "--allow-url", URL, "Read"]) == 0
    assert capsys.readouterr().out == REPLY + "\n"
    assert transport.requests[0]["payload"]["model"] == "from-file"


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("save_fails", [False, True])
def test_session_id_requires_final_save(harness, capsys, monkeypatch, tmp_path,
                                       resumed, json_mode, save_fails):
    transport, _, _ = harness
    session_id = "persisted" if resumed else None
    if resumed:
        JsonFileSessionStore(tmp_path / ".browser_sessions").save(
            ConversationSession(id=session_id, metadata={"browser_read_isolated_v1": True}))
    original_save = JsonFileSessionStore.save
    saves = []

    def save(store, session):
        saves.append(session.id)
        if save_fails and len(saves) == 2:
            raise OSError("private storage path")
        return original_save(store, session)

    monkeypatch.setattr(JsonFileSessionStore, "save", save)
    finish(transport)
    status, output = invoke(capsys, session=session_id, json_mode=json_mode)
    assert len(saves) == 2  # Autosave succeeds; the required final save decides publication.
    assert status == (1 if save_fails else 0)
    if json_mode:
        if save_fails:
            assert output["error"] == "browser_session_storage_failed"
            assert output["session_id"] is None and output["reply"] is None
        else:
            assert output["session_id"] == saves[-1] and output["reply"] == REPLY
    elif save_fails:
        assert not output.out
        assert output.err == host._MESSAGES["browser_session_storage_failed"] + "\n"
    else:
        assert output.out == REPLY + "\n" and not output.err


def test_all_saves_fail_without_publishing_id(harness, capsys, monkeypatch):
    transport, _, _ = harness
    monkeypatch.setattr(JsonFileSessionStore, "save", lambda *a: (_ for _ in ()).throw(OSError("private path")))
    finish(transport)
    _, payload = invoke(capsys)
    assert payload["error"] == "browser_session_storage_failed"
    assert payload["session_id"] is None


def test_tilde_workspace_uses_exact_canonical_directory(harness, capsys, monkeypatch, tmp_path):
    transport, _, _ = harness
    home = tmp_path / "home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AGENTCORE_WORKSPACE_DIR", "~/workspace")
    monkeypatch.chdir(tmp_path)
    finish(transport)
    _, payload = invoke(capsys)
    directory = host.load_config().canonical_workspace() / ".browser_sessions"
    assert directory == workspace / ".browser_sessions"
    assert (directory / (payload["session_id"] + ".json")).is_file()
    assert (directory / ("session-" + payload["session_id"] + ".lock")).is_file()
    assert not (tmp_path / "~").exists()
    assert all(str(directory) not in json.dumps(request["payload"]) for request in transport.requests)


def _process_browser_turn(workspace, session_id, phase, connection, json_mode=True):
    # Spawned OS processes run the real CLI/provider/composition with synthetic
    # I/O. Pipes stop the owner at the exact seam; no timing/sleep assumptions.
    with pytest.MonkeyPatch.context() as patch:
        transport, backends, _ = harness.__wrapped__(Path(workspace), patch)
        finish(transport, open_response(URL))
        reached = False
        exists_calls = 0
        saves = 0

        def pause(at):
            nonlocal reached
            if phase == at and not reached:
                reached = True
                connection.send(("held", os.getpid()))
                assert connection.poll(30), "owner release was not received"
                assert connection.recv() == "release"

        original_exists = JsonFileSessionStore.exists
        original_save = JsonFileSessionStore.save
        original_post = transport.post_json
        original_close = FakeBackend.close

        def exists(store, identifier):
            nonlocal exists_calls
            exists_calls += 1
            pause("exists")
            return original_exists(store, identifier)

        def save(store, session):
            nonlocal saves
            saves += 1
            if saves == 2:
                pause("final_save")
            return original_save(store, session)

        def post(*args, **kwargs):
            pause("model")
            return original_post(*args, **kwargs)

        def close(backend):
            pause("cleanup")
            return original_close(backend)

        patch.setattr(JsonFileSessionStore, "exists", exists)
        patch.setattr(JsonFileSessionStore, "save", save)
        patch.setattr(transport, "post_json", post)
        patch.setattr(FakeBackend, "close", close)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            args = ["browser", "--session", session_id, "--allow-url", URL]
            status = cli.main(args + (["--json"] if json_mode else []) + ["Read this page"])
        connection.send(("done", {"pid": os.getpid(), "status": status,
            "out": out.getvalue(), "err": err.getvalue(), "model_calls": len(transport.requests),
            "browser_calls": [call for backend in backends for call in backend.calls],
            "exists_calls": exists_calls, "saves": saves}))
    connection.close()


@pytest.mark.parametrize("phase", ["exists", "model", "final_save", "cleanup"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_resumed_session_lease_is_cross_process_and_spans_turn(tmp_path, phase, json_mode):
    store = JsonFileSessionStore(tmp_path / ".browser_sessions")
    for identifier in ("shared", "independent"):
        store.save(ConversationSession(id=identifier, metadata={"browser_read_isolated_v1": True}))
    context = multiprocessing.get_context("spawn")
    processes = []
    connections = []

    def start(identifier, hold=None):
        parent, child = context.Pipe()
        process = context.Process(target=_process_browser_turn,
            args=(str(tmp_path), identifier, hold, child, json_mode))
        processes.append(process)
        connections.append(parent)
        process.start()
        child.close()
        return parent, process

    def receive(connection, expected):
        assert connection.poll(25), "browser CLI process did not reach expected seam"
        kind, data = connection.recv()
        assert kind == expected
        return data

    try:
        owner, owner_process = start("shared", phase)
        owner_pid = receive(owner, "held")
        loser, loser_process = start("shared")
        blocked = receive(loser, "done")
        loser_process.join(5)
        assert loser_process.exitcode == 0 and blocked["pid"] != owner_pid
        assert blocked["status"] == 1
        assert blocked["model_calls"] == 0 and blocked["browser_calls"] == []
        assert blocked["exists_calls"] == 0 and blocked["saves"] == 0
        if json_mode:
            assert json.loads(blocked["out"]) == {"ok": False, "session_id": None,
                "reply": None, "tools_executed": 0, "error": "browser_session_busy"}
            assert not blocked["err"]
        else:
            assert not blocked["out"]
            assert blocked["err"] == host._MESSAGES["browser_session_busy"] + "\n"
        other, _ = start("independent")
        assert receive(other, "done")["status"] == 0
        owner.send("release")
        assert receive(owner, "done")["status"] == 0
        owner_process.join(5)
        assert owner_process.exitcode == 0
        resumed, _ = start("shared")
        assert receive(resumed, "done")["status"] == 0
        # Both completed turns survive; the losing process added no turn.
        assert sum(message.role == "user" for message in store.load("shared").messages) == 2
    finally:
        for process in processes:
            process.join(1)
            if process.is_alive():
                process.terminate()
                process.join(5)
        for connection in connections:
            connection.close()


@pytest.mark.parametrize("failure", ["model", "browser", "storage", "cleanup", "exception"])
def test_session_lease_released_after_failure(harness, capsys, monkeypatch, tmp_path, failure):
    transport, _, _ = harness
    directory = tmp_path / ".browser_sessions"
    JsonFileSessionStore(directory).save(ConversationSession(
        id="shared", metadata={"browser_read_isolated_v1": True}))
    if failure == "model":
        transport.responses.append(RuntimeError("private model error"))
    else:
        finish(transport, open_response(URL))
    if failure == "browser":
        monkeypatch.setattr(FakeBackend, "open_url", lambda *a: (_ for _ in ()).throw(BrowserError(BrowserFailure.TIMEOUT)))
    elif failure == "storage":
        monkeypatch.setattr(JsonFileSessionStore, "save", lambda *a: (_ for _ in ()).throw(OSError("private path")))
    elif failure == "cleanup":
        monkeypatch.setattr(FakeBackend, "close", lambda *a: (_ for _ in ()).throw(RuntimeError("private cleanup")))
    elif failure == "exception":
        monkeypatch.setattr(JsonFileSessionStore, "exists", lambda *a: (_ for _ in ()).throw(RuntimeError("unexpected")))
    if failure == "exception":
        with pytest.raises(RuntimeError, match="unexpected"):
            invoke(capsys, session="shared")
    else:
        status, payload = invoke(capsys, session="shared")
        assert status == 1
        assert (payload["session_id"] is None) == (failure == "storage")
    # A fresh native descriptor can own the same session after every exit.
    with host._session_lease(directory, "shared"):
        pass


def test_reply_is_bounded_and_terminal_controls_removed(harness, capsys):
    transport, _, _ = harness
    finish(transport, reply="\x1b" + "x" * 9000)
    _, payload = invoke(capsys)
    assert payload["ok"] and len(payload["reply"]) <= 8000
    assert "\x1b" not in payload["reply"] and "[truncated]" in payload["reply"]
