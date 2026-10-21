"""Foreground reads: deterministic native, protocol, authority and privacy seams."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agentcore import cli
from agentcore.config.schema import ToolConfig
from agentcore.desktop import service
from agentcore.desktop.chat import ConsoleNotepadApproval, NotepadConversationRunner, desktop_notices
from agentcore.errors import PolicyLeaseBusyError
from agentcore.integrations import computer_control as control
from agentcore.integrations.base import IntegrationStatus
from agentcore.models.messages import ModelResponse, ToolCall
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.base import ExternalReadTool, ToolSpec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError


@pytest.fixture
def observation(monkeypatch):
    calls = []
    def request(self, operation):
        calls.append(operation)
        if operation == "health":
            return "ready"
        if operation == "open_notepad":
            return "launch_accepted"
        return {"status": "observed", "app": "Notepad"}
    monkeypatch.setattr(control.ComputerControlAdapter, "_request", request)
    adapter = control.ComputerControlAdapter({"enabled": True})
    assert adapter.connect() is IntegrationStatus.CONNECTED
    calls.clear()
    return adapter, calls


def executor_for(adapter, *, level=PermissionLevel.OBSERVE, blocked=(), enabled=(), disabled=()):
    registry = ToolRegistry(ToolConfig(enabled=enabled, disabled=disabled))
    adapter.register_into(registry)
    policy = PermissionPolicy(PermissionConfig(level=level, blocked_capabilities=blocked),
                              approval_gate=ConsoleNotepadApproval())
    return ToolExecutor(registry, policy)


@pytest.mark.parametrize("level", list(PermissionLevel))
def test_observation_uses_screen_permission_without_notepad_approval(observation, monkeypatch, level):
    adapter, calls = observation
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("read prompted for Notepad approval"))
    executor = executor_for(adapter, level=level)
    tool = executor.registry.get("get_foreground_app")
    assert isinstance(tool, ExternalReadTool)
    assert tool.capabilities == {Capability.SCREEN_OBSERVE} and tool.spec.read_only
    assert tool.spec.parameters == ()
    runner = NotepadConversationRunner(executor, executor.registry, notepad_tool=adapter.provided_tools()[0])
    outcome = runner.run_calls([ToolCall("read", tool.name, {})]).outcomes[0]
    assert outcome.ok and outcome.invocation.action_id is None
    assert outcome.invocation.result.output == {"status": "observed", "app": "Notepad"}
    assert calls == ["get_foreground_app"]


@pytest.mark.parametrize("args", [{"pid": 1}, {"window": 2}, {"path": "secret"}, {"approved": True}])
def test_arguments_refused_before_observation(observation, args):
    adapter, calls = observation
    assert executor_for(adapter).invoke("get_foreground_app", args).result.metadata["kind"] == "validation"
    assert not calls


def test_screen_denial_and_tool_filters(observation):
    adapter, calls = observation
    executor = executor_for(adapter, blocked=("screen.observe",))
    assert executor.invoke("get_foreground_app", {}).result.metadata["kind"] == "permission_denied"
    assert executor_for(adapter, enabled=("get_foreground_app",)).registry.names() == ["get_foreground_app"]
    assert executor_for(adapter, enabled=("open_notepad",)).registry.names() == ["open_notepad"]
    assert "get_foreground_app" not in executor_for(adapter, disabled=("get_foreground_app",)).registry
    assert not calls


def test_disabled_or_disconnected_adapter_never_observes(monkeypatch):
    monkeypatch.setattr(control.ComputerControlAdapter, "_request", lambda *a: pytest.fail("disabled read"))
    for settings in ({}, {"enabled": True}):
        adapter = control.ComputerControlAdapter(settings)
        assert adapter.provided_tools() == ()
        assert not adapter.get_foreground_app().ok


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(window=101, thread=10, pid=20, session=3, process_session=3,
                            app="Notepad", available=True, closed=[], queried=[])
    class Handle:
        def __init__(self, label): self.label = label
        def Close(self): state.closed.append(self.label)
    def open_process(access, inherit, pid):
        assert (access, inherit, pid) == (0x1000, False, 20)
        return Handle("process")
    modules = {
        "win32api": SimpleNamespace(OpenProcess=open_process),
        "win32con": SimpleNamespace(TOKEN_QUERY=8),
        "win32gui": SimpleNamespace(GetForegroundWindow=lambda: state.window,
                                     GetClassName=lambda _: "CabinetWClass"),
        "win32process": SimpleNamespace(GetWindowThreadProcessId=lambda _: (state.thread, state.pid),
                                         GetExitCodeProcess=lambda _: 259),
        "win32security": SimpleNamespace(OpenProcessToken=lambda p, a: Handle("token"), TokenSessionId=12,
                                          GetTokenInformation=lambda t, c: state.process_session),
        "win32ts": SimpleNamespace(ProcessIdToSessionId=lambda _: state.session),
    }
    for name, module in modules.items(): monkeypatch.setitem(sys.modules, name, module)
    def app(process):
        state.queried.append(True)
        return state.app, None if state.app else "executable_not_allowlisted"
    monkeypatch.setattr(service, "_process_identity", app)
    monkeypatch.setattr(service, "_desktop_available", lambda: state.available)
    monkeypatch.setattr(service, "availability", lambda: "ready")
    return state, modules


@pytest.mark.parametrize("app", list(service.APP_NAMES.values()))
def test_foreground_native_identification_and_cleanup(native, app):
    state, _ = native
    state.app = app
    assert service.handle_request({"v": 1, "operation": "get_foreground_app"}) == {"status": "observed", "app": app}
    assert state.closed == ["token", "process"]


@pytest.mark.parametrize("case,expected", [("unrecognized", "unknown"), ("no_window", "unknown"),
                                          ("cross_session", "inaccessible"), ("locked", "desktop_unavailable")])
def test_native_unknown_and_session_refusals(native, case, expected):
    state, _ = native
    if case == "unrecognized": state.app = None
    if case == "no_window": state.window = 0
    if case == "cross_session": state.process_session = 4
    if case == "locked": state.available = False
    result = {"status": expected, "app": None}
    if case in {"unrecognized", "no_window"}:
        result["reason"] = "executable_not_allowlisted" if case == "unrecognized" else "no_foreground_window"
    assert service.handle_request({"v": 1, "operation": "get_foreground_app"}) == result
    if case in {"cross_session", "no_window"}: assert not state.queried


def test_native_focus_changes_and_shell_are_unknown(native, monkeypatch):
    state, modules = native
    def change_focus(process):
        state.window = 102
        return "Notepad", None
    monkeypatch.setattr(service, "_process_identity", change_focus)
    assert service._foreground_app() == {"status": "unknown", "app": None, "reason": "foreground_changed"}
    monkeypatch.setattr(service, "_process_identity", lambda _: ("File Explorer", None))
    modules["win32gui"].GetClassName = lambda _: "Shell_TrayWnd"
    assert service._foreground_app() == {"status": "unknown", "app": None, "reason": "explorer_shell"}


def test_inaccessible_native_errors_never_expose_details(native):
    _, modules = native
    def denied(*args): raise OSError("C:/private/document secret-title https://secret.invalid pid=999")
    modules["win32api"].OpenProcess = denied
    assert service.handle_request({"v": 1, "operation": "get_foreground_app"}) == {"status": "inaccessible", "app": None}


@pytest.mark.parametrize("status", ["desktop_unavailable", "unsupported_os", "service_unavailable"])
def test_availability_refusal_precedes_native_read(monkeypatch, status):
    monkeypatch.setattr(service, "availability", lambda: status)
    monkeypatch.setattr(service, "_foreground_app", lambda: pytest.fail("read after refusal"))
    assert service.handle_request({"v": 1, "operation": "get_foreground_app"}) == {"status": status, "app": None}


@pytest.mark.parametrize("payload", [{"v": 1, "operation": "get_foreground_app", "pid": 4},
                                      {"v": 1, "operation": {}}, {"v": 2, "operation": "get_foreground_app"}])
def test_invalid_request_never_observes(monkeypatch, payload):
    monkeypatch.setattr(service, "availability", lambda: pytest.fail("invalid request accessed desktop"))
    assert service.handle_request(payload) == "invalid_request"


@pytest.mark.parametrize("raw", [b'{"v":1,"v":1,"operation":"get_foreground_app"}', b"x" * 257, b"\xff"])
def test_strict_decoder(raw):
    with pytest.raises(ValueError): service.decode_message(raw)


@pytest.mark.parametrize("payload", [
    b"", b"invalid", b"x" * 257,
    b'{"v":1,"status":"observed","app":"Notepad","title":"secret"}',
    b'{"v":true,"status":"observed","app":"Notepad"}',
    b'{"v":1,"status":[],"app":null}', b'{"v":1,"status":"observed","app":[]}',
    b'{"v":1,"status":"observed","app":"C:/private/evil.exe"}',
    b'{"v":1,"status":"observed","app":null}', b'{"v":1,"status":"unknown","app":"Notepad"}',
    b'{"v":1,"status":"launch_accepted","app":null}', b'{"v":1,"status":"unknown"}',
    b'{"v":1,"status":"observed","app":"Notepad","app":"Notepad"}',
])
def test_invalid_responses_never_fabricate_identity(monkeypatch, payload):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(control, "_run_service", lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload))
    assert control.ComputerControlAdapter()._request("get_foreground_app") == {"status": "service_unavailable", "app": None}


@pytest.mark.parametrize("status,app", [("observed", "Notepad"), ("unknown", None), ("inaccessible", None),
                                       ("desktop_unavailable", None), ("unsupported_os", None)])
def test_valid_response_schema(monkeypatch, status, app):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    raw = json.dumps({"v": 1, "status": status, "app": app}).encode()
    monkeypatch.setattr(control, "_run_service", lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw))
    assert control.ComputerControlAdapter()._request("get_foreground_app") == {"status": status, "app": app}


@pytest.mark.parametrize("error", [OSError, subprocess.TimeoutExpired])
def test_lost_response_is_unavailable(monkeypatch, error):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    def lost(*a, **k): raise error("private-service-details", 10)
    monkeypatch.setattr(control, "_run_service", lost)
    assert control.ComputerControlAdapter()._request("get_foreground_app") == {"status": "service_unavailable", "app": None}


def test_transport_enforces_capture_bound_and_timeout():
    result = control._run_service([sys.executable, "-I", "-c", "import sys; sys.stdout.write('x'*1000000)"],
                                 input=b"{}", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    assert len(result.stdout) == 257
    with pytest.raises(subprocess.TimeoutExpired):
        control._run_service([sys.executable, "-I", "-c", "import time; time.sleep(10)"],
                             input=b"{}", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.2)


def test_registry_and_policy_remain_leased_through_native_io(observation, monkeypatch):
    adapter, _ = observation
    executor = executor_for(adapter)
    entered, release = threading.Event(), threading.Event()
    def wait(self, operation):
        entered.set()
        assert release.wait(5)
        return {"status": "observed", "app": "Notepad"}
    monkeypatch.setattr(control.ComputerControlAdapter, "_request", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(executor.invoke, "get_foreground_app", {})
        assert entered.wait(5)
        try:
            with pytest.raises(ToolRegistryLeaseError): executor.registry.unregister("get_foreground_app")
            with pytest.raises(ToolRegistryLeaseError): executor.registry.register(control.GetForegroundAppTool(adapter), replace=True)
            with pytest.raises(PolicyLeaseBusyError): executor.policy = PermissionPolicy()
        finally: release.set()
        assert future.result().ok
    executor.policy = PermissionPolicy()
    executor.registry.unregister("get_foreground_app")


def test_policy_aba_before_dispatch_is_denied(observation):
    adapter, calls = observation
    executor = executor_for(adapter)
    old = executor.policy
    count = 0
    def checkpoint():
        nonlocal count
        count += 1
        if count == 2:
            executor.policy = PermissionPolicy(PermissionConfig(blocked_capabilities=("screen.observe",)))
            executor.policy = old
    assert executor.invoke("get_foreground_app", {}, checkpoint=checkpoint).result.metadata["kind"] == "permission_denied"
    assert not calls


def test_concurrent_observations_are_separate_requests(observation):
    adapter, calls = observation
    executor = executor_for(adapter)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: executor.invoke("get_foreground_app", {}), range(12)))
    assert all(r.ok and r.result.output == {"status": "observed", "app": "Notepad"} for r in results)
    assert calls == ["get_foreground_app"] * 12


def test_observation_does_not_clear_notepad_uncertainty(observation):
    adapter, _ = observation
    adapter._uncertain = True
    executor = executor_for(adapter)
    assert executor.invoke("get_foreground_app", {}).ok
    assert adapter.open_notepad().metadata["kind"] == "launch_unknown"


class ObservationModel:
    def __init__(self): self.requests = []
    def generate(self, request):
        self.requests.append(request)
        if len(self.requests) == 1 and request.tools:
            return ModelResponse(text="", tool_calls=(ToolCall("observe", "get_foreground_app", {}),))
        return ModelResponse(text="Observed the application using the tool.")


def config_for(tmp_path, enabled):
    config = tmp_path / "agent.toml"
    config.write_text(f'workspace_dir = "{tmp_path.as_posix()}"\n[permissions]\nlevel = "observe"\n'
                      '[conversation]\ntools_enabled = true\n[tools]\n'
                      f'enabled = {json.dumps(enabled)}\n[logging]\nlevel = "CRITICAL"\n')
    return ["--config", str(config)]


def test_chat_tool_only_enablement_and_authoritative_observation(observation, tmp_path, monkeypatch, capsys):
    _, calls = observation
    model = ObservationModel()
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: model)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("read prompted"))
    assert cli.main(config_for(tmp_path, ["get_foreground_app"]) + ["chat", "--enable-desktop", "--message", "Which app?", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == ["health", "get_foreground_app"]
    assert [t["name"] for t in model.requests[0].tools] == ["get_foreground_app"]
    assert "Observed foreground application: Notepad." in str(output["notices"])
    assert output["desktop_observations"] == [{"status": "observed", "app": "Notepad"}]
    assert output["desktop_tool_calls"] == [{"name": "get_foreground_app", "arguments": {},
        "status": "ok", "executor_returned": True, "executed": True}]
    assert "Notepad" in str(model.requests[-1])


def test_without_desktop_opt_in_neither_tool_is_exposed(observation, tmp_path, monkeypatch, capsys):
    _, calls = observation
    model = ObservationModel()
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: model)
    assert cli.main(config_for(tmp_path, ["get_foreground_app", "open_notepad"]) + ["chat", "--message", "Which app?"]) == 0
    capsys.readouterr()
    assert not calls and not model.requests[0].tools


@pytest.mark.parametrize("native,reply", [
    ({"status": "observed", "app": "Notepad"}, "You are using Notepad."),
    ({"status": "unknown", "app": None}, "The foreground application is unknown."),
])
def test_configured_json_provider_reaches_conversation_executor_and_native_boundary(
        observation, tmp_path, monkeypatch, capsys, caplog, native, reply):
    from agentcore.models import factory
    from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
    from test_models_provider import FakeTransport, http, ok_body

    _, calls = observation
    def request(self, operation):
        calls.append(operation)
        assert operation in {"health", "get_foreground_app"}
        return "ready" if operation == "health" else dict(native)
    monkeypatch.setattr(control.ComputerControlAdapter, "_request", request)
    secret = "task49-test-header-only-credential"
    monkeypatch.setenv("TASK49_TEST_KEY", secret)
    call_body = {"choices": [{"message": {"content": None, "tool_calls": [{
        "id": "observe49", "type": "function", "function": {
            "name": "get_foreground_app", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
    transport = FakeTransport([http(200, call_body), http(200, ok_body(reply))])
    monkeypatch.setitem(factory._REGISTRY, "openai_compatible",
                        lambda config: OpenAICompatibleProvider(config, transport=transport))
    args = config_for(tmp_path, ["get_foreground_app"])
    with Path(args[1]).open("a", encoding="utf-8") as config:
        config.write('\n[model]\nprovider = "openai_compatible"\nname = "configured-test-model"\n'
                     'base_url = "https://example.invalid/v1"\napi_key_env = "TASK49_TEST_KEY"\n'
                     'enable_thinking = false\nmax_retries = 1\nmax_output_tokens = 128\ntimeout_seconds = 20\n')
    assert cli.main(args + ["chat", "--enable-desktop", "--message",
                            "Jarvis, what application am I using right now?", "--json"]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["reply"] == reply and payload["desktop_observations"] == [native]
    assert payload["desktop_tool_calls"] == [{"name": "get_foreground_app", "arguments": {},
        "status": "ok", "executor_returned": True, "executed": True}]
    assert calls == ["health", "get_foreground_app"]
    assert len(transport.requests) == 2
    for sent in transport.requests:
        assert sent["payload"]["stream"] is False
        assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert sent["timeout"] == 20 and sent["payload"]["max_tokens"] == 128
    assert [t["function"]["name"] for t in transport.requests[0]["payload"]["tools"]] == ["get_foreground_app"]
    followup = transport.requests[1]["payload"]["messages"]
    assert followup[-2]["tool_calls"][0]["id"] == followup[-1]["tool_call_id"] == "observe49"
    assert native["status"] in followup[-1]["content"]
    assert secret not in output.out + output.err + caplog.text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_model_prose_cannot_create_desktop_evidence(observation, tmp_path, monkeypatch, capsys):
    _, calls = observation
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: SimpleNamespace(
        generate=lambda request: ModelResponse(text="I observed Notepad using get_foreground_app.")))
    assert cli.main(config_for(tmp_path, ["get_foreground_app"]) + [
        "chat", "--enable-desktop", "--message", "Which app?", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == ["health"]
    assert "desktop_observations" not in payload and "desktop_tool_calls" not in payload


def test_desktop_call_metadata_omits_invalid_arguments_ids_and_unknown_names():
    from agentcore.conversation.tool_use import ToolCallOutcome
    from agentcore.desktop.chat import desktop_tool_calls

    secret = "private-call-data"
    outcomes = [ToolCallOutcome(ToolCall(secret, "get_foreground_app", {"path": secret}), "denied"),
                ToolCallOutcome(ToolCall(secret, secret, {}), "denied")]
    evidence = desktop_tool_calls(SimpleNamespace(tool_outcomes=outcomes))
    assert evidence == [{"name": "get_foreground_app", "arguments": None,
                         "status": "denied", "executor_returned": False, "executed": False}]
    assert secret not in json.dumps(evidence)


def test_private_response_never_reaches_model_logs_or_persistence(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    secret = "PRIVATE_TITLE_DOCUMENT_URL_PID_SECRET"
    def run(command, **kwargs):
        request = service.decode_message(kwargs["input"])
        payload = ({"v": 1, "status": "ready"} if request["operation"] == "health" else
                   {"v": 1, "status": "observed", "app": "Notepad", "title": secret})
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())
    monkeypatch.setattr(control, "_run_service", run)
    model = ObservationModel()
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: model)
    assert cli.main(config_for(tmp_path, ["get_foreground_app"]) + ["chat", "--enable-desktop", "--message", "Which app?", "--json"]) == 0
    output = capsys.readouterr()
    assert secret not in output.out + output.err + caplog.text + str(model.requests)
    for path in tmp_path.rglob("*"):
        if path.is_file(): assert secret.encode() not in path.read_bytes()
    assert "Observed foreground application" not in output.out


def test_service_stdio_closed_response(monkeypatch, capsys):
    monkeypatch.setattr(service, "availability", lambda: "ready")
    monkeypatch.setattr(service, "_foreground_app", lambda: {"status": "unknown", "app": None})
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"v":1,"operation":"get_foreground_app"}')))
    assert service.main() == 0
    raw = capsys.readouterr().out.encode()
    assert len(raw) <= 256 and service.decode_message(raw) == {"v": 1, "status": "unknown", "app": None}


@pytest.mark.parametrize("image,expected", [(r"C:\Windows\System32\NOTEPAD.EXE", "Notepad"),
    (r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2508_x64__8wekyb3d8bbwe\Notepad\Notepad.exe", "Notepad"),
    (r"C:\private\chrome.exe", "Google Chrome"), (r"C:\private\msedge.exe", "Microsoft Edge"),
    (r"C:\private\Code.exe", "Visual Studio Code"), (r"C:\Windows\explorer.exe", "File Explorer"),
    (r"C:\private\WindowsTerminal.exe", "Windows Terminal"), (r"C:\private\secret-document.exe", None)])
def test_native_query_reduces_image_to_closed_label(monkeypatch, image, expected):
    import ctypes
    class Query:
        def __call__(self, handle, flags, buffer, size):
            assert (handle, flags) == (42, 0) and len(buffer) == 32768
            buffer.value = image
            size._obj.value = len(image)
            return 1
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(QueryFullProcessImageNameW=Query()), raising=False)
    assert service._process_identity(42) == (expected, None if expected else "executable_not_allowlisted")


@pytest.mark.parametrize("reason", sorted(service.UNKNOWN_REASONS))
def test_reason_is_operator_metadata_not_model_content(monkeypatch, reason):
    adapter = control.ComputerControlAdapter({"enabled": True})
    adapter.status = IntegrationStatus.CONNECTED
    monkeypatch.setattr(adapter, "_request", lambda _: {"status": "unknown", "app": None, "reason": reason})
    executor = executor_for(adapter)
    runner = NotepadConversationRunner(executor, executor.registry, notepad_tool=adapter.provided_tools()[0])
    outcome = runner.run_calls([ToolCall("observe", "get_foreground_app", {})]).outcomes[0]
    assert outcome.ok and reason not in outcome.content
    assert outcome.invocation.result.output == {"status": "unknown", "app": None}
    assert cli._turn_payload(SimpleNamespace(session=SimpleNamespace(id="test"), ok=True, reply=None,
        error=None, memories_created=[], memories_updated=[], memories_used=0, conflicts=[], memory_error=None,
        tool_outcomes=[outcome]))["desktop_observations"] == [{"status": "unknown", "app": None, "reason": reason}]


@pytest.mark.parametrize("reason,status", [("C:/private/secret", "unknown"), ([], "unknown"),
    ("foreground_changed", "observed"), ("no_foreground_window", "inaccessible")])
def test_invalid_or_misplaced_reason_is_refused(monkeypatch, reason, status):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    raw = json.dumps({"v": 1, "status": status, "app": "Notepad" if status == "observed" else None,
                      "reason": reason}).encode()
    monkeypatch.setattr(control, "_run_service", lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw))
    assert control.ComputerControlAdapter()._request("get_foreground_app") == {"status": "service_unavailable", "app": None}


def test_valid_reason_survives_strict_transport_validation(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    raw = b'{"v":1,"status":"unknown","app":null,"reason":"foreground_changed"}'
    monkeypatch.setattr(control, "_run_service", lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw))
    assert control.ComputerControlAdapter()._request("get_foreground_app") == {
        "status": "unknown", "app": None, "reason": "foreground_changed"}


def test_process_exit_and_missing_window_identity_are_distinct(native):
    state, modules = native
    modules["win32process"].GetExitCodeProcess = lambda _: 0
    assert service._foreground_app() == {"status": "unknown", "app": None, "reason": "foreground_process_exited"}
    state.pid = 0
    assert service._foreground_app() == {"status": "unknown", "app": None, "reason": "window_process_unavailable"}


@pytest.mark.parametrize("sequence", [list, tuple], ids=["native-list", "tuple"])
def test_stable_native_identity_compares_ids_independently_of_sequence_type(native, sequence):
    state, modules = native
    modules["win32process"].GetWindowThreadProcessId = lambda _: sequence((state.thread, state.pid))
    assert service._foreground_app() == {"status": "observed", "app": "Notepad"}


@pytest.mark.parametrize("change", ["session0", "inactive", "station", "locked", "detached"])
def test_native_desktop_session_rejections(monkeypatch, change):
    closed = []
    desktop = SimpleNamespace(CloseDesktop=lambda: closed.append(True))
    def input_desktop(*args):
        if change == "detached": raise OSError("private desktop error")
        return desktop
    modules = {
        "win32con": SimpleNamespace(UOI_NAME=2, DESKTOP_READOBJECTS=1),
        "win32ts": SimpleNamespace(ProcessIdToSessionId=lambda _: 0 if change == "session0" else 3,
                                    WTS_CURRENT_SERVER_HANDLE=0, WTSConnectState=8, WTSActive=0,
                                    WTSQuerySessionInformation=lambda *a: 4 if change == "inactive" else 0),
        "win32service": SimpleNamespace(GetProcessWindowStation=lambda: "station",
            GetUserObjectInformation=lambda obj, _: ("OtherStation" if change == "station" else "WinSta0") if obj == "station" else ("Winlogon" if change == "locked" else "Default"),
            OpenInputDesktop=input_desktop),
    }
    for name, module in modules.items(): monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion", lambda: SimpleNamespace(platform_version=(10, 0, 26100), product_type=1), raising=False)
    assert service.availability() == "desktop_unavailable"
    assert closed == ([True] if change == "locked" else [])


@pytest.mark.parametrize("version,product", [((10, 0, 19045), 1), ((10, 0, 26100), 3)])
def test_windows_10_and_server_refused(monkeypatch, version, product):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion", lambda: SimpleNamespace(platform_version=version, product_type=product), raising=False)
    monkeypatch.setattr(service, "_desktop_available", lambda: pytest.fail("unsupported native read"))
    assert service.availability() == "unsupported_os"


def test_foreground_statuses_stay_distinct(observation, monkeypatch):
    adapter, _ = observation
    for category in service.OBSERVATION_STATUSES - {"observed"}:
        monkeypatch.setattr(adapter, "_request", lambda _, c=category: {"status": c, "app": None})
        result = executor_for(adapter).invoke("get_foreground_app", {}).result
        assert result.metadata["kind"] == category
        assert result.ok == (category == "unknown")


def test_registry_replacement_at_pre_dispatch_checkpoint_is_blocked(observation):
    adapter, calls = observation
    executor = executor_for(adapter)
    def checkpoint():
        executor.registry.register(control.GetForegroundAppTool(adapter), replace=True)
    result = executor.invoke("get_foreground_app", {}, checkpoint=checkpoint).result
    assert not result.ok and not calls


def test_observation_failure_has_no_app_notice():
    for category in ("unknown", "inaccessible", "service_unavailable", "permission_denied"):
        outcome = SimpleNamespace(call=ToolCall("read", "get_foreground_app", {}), invocation=SimpleNamespace(
            result=control.ToolResult.failure("unavailable", kind=category)))
        notice = desktop_notices(SimpleNamespace(tool_outcomes=[outcome]))[0]
        assert "Observed foreground application" not in notice


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows limited-query handle contract")
def test_native_self_process_query_and_handle_cleanup():
    import os
    import win32api
    import win32con
    import win32security
    process = win32api.OpenProcess(0x1000, False, os.getpid())
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        try:
            assert type(win32security.GetTokenInformation(token, win32security.TokenSessionId)) is int
        finally:
            token.Close()
        assert service._process_identity(process) == (None, "executable_not_allowlisted")  # pytest's Python process is not a recognized app.
    finally:
        process.Close()
