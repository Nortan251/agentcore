"""No test launches Notepad; real stdio and native probes are separate from visibility."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agentcore import cli
from agentcore.action_journal.adapters import ActionAdapterRegistry
from agentcore.desktop import service
from agentcore.desktop.chat import ConsoleNotepadApproval, NotepadConversationRunner, desktop_notices
from agentcore.integrations import computer_control as control
from agentcore.integrations.base import IntegrationStatus
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.models.messages import ModelResponse, ToolCall
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError


@pytest.fixture
def connected(monkeypatch):
    calls = []

    def request(self, operation):
        calls.append(operation)
        return "ready" if operation == "health" else "launch_accepted"

    monkeypatch.setattr(control.ComputerControlAdapter, "_request", request)
    adapter = control.ComputerControlAdapter({"enabled": True})
    assert adapter.connect() is IntegrationStatus.CONNECTED
    calls.clear()
    return adapter, calls


def executor_for(adapter, *, level=PermissionLevel.CONFIRM, gate=None, blocked=()):
    registry = ToolRegistry()
    assert adapter.register_into(registry) == 2
    tool = adapter.provided_tools()[0]
    actions = ActionAdapterRegistry()
    actions.register(tool, control.NotepadActionAdapter())
    executor = ToolExecutor(registry, PermissionPolicy(
        PermissionConfig(level=level, blocked_capabilities=blocked), approval_gate=gate),
        action_adapters=actions)
    return executor, tool


def test_registration_and_accepted_result(connected, monkeypatch):
    adapter, calls = connected
    monkeypatch.setattr("builtins.input", lambda _: "open notepad")
    executor, tool = executor_for(adapter, gate=ConsoleNotepadApproval())
    assert tool.spec.parameters == ()
    assert tool.capabilities == {Capability.APP_CONTROL, Capability.PROCESS_SPAWN}
    invocation = executor.invoke("open_notepad", {})
    assert invocation.ok and invocation.action_id is None
    assert invocation.result.metadata["kind"] == "launch_accepted"
    assert "visible window is not verified" in invocation.result.output
    assert calls == ["open_notepad"]


@pytest.mark.parametrize("level,blocked", [
    (PermissionLevel.OBSERVE, ()), (PermissionLevel.PROPOSE, ()),
    (PermissionLevel.CONFIRM, ()),
    (PermissionLevel.AUTONOMOUS, ("app.control",)),
    (PermissionLevel.AUTONOMOUS, ("process.spawn",)),
])
def test_policy_denial_never_dispatches(connected, level, blocked):
    adapter, calls = connected
    executor, _ = executor_for(adapter, level=level, blocked=blocked)
    invocation = executor.invoke("open_notepad", {})
    assert invocation.result.metadata["kind"] == "permission_denied"
    assert calls == []


@pytest.mark.parametrize("answer", ["", "no", "yes", "Jarvis, open Notepad."])
def test_only_explicit_operator_phrase_approves(connected, monkeypatch, answer):
    adapter, calls = connected
    monkeypatch.setattr("builtins.input", lambda _: answer)
    executor, _ = executor_for(adapter, gate=ConsoleNotepadApproval())
    assert not executor.invoke("open_notepad", {}).ok
    assert not calls


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_missing_operator_denies(monkeypatch, error):
    def interrupt(_):
        raise error()
    monkeypatch.setattr("builtins.input", interrupt)
    assert not ConsoleNotepadApproval().request(Capability.APP_CONTROL, "")
    assert not ConsoleNotepadApproval().request(Capability.FS_WRITE, "")


@pytest.mark.parametrize("args", [{"command": "calc.exe"}, {"path": "C:/evil.exe"},
                                  {"application": "notepad"}, {"approved": True}])
def test_arguments_cannot_widen_action(connected, monkeypatch, args):
    adapter, calls = connected
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("approval before validation"))
    executor, _ = executor_for(adapter, gate=ConsoleNotepadApproval())
    assert not executor.invoke("open_notepad", args).ok
    assert calls == []


def test_missing_or_replaced_trusted_registration_denied(connected):
    adapter, calls = connected
    registry = ToolRegistry()
    adapter.register_into(registry)
    executor = ToolExecutor(registry, PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS)))
    assert executor.invoke("open_notepad", {}).result.metadata["kind"] == "action_adapter_missing"
    executor, tool = executor_for(adapter, level=PermissionLevel.AUTONOMOUS)
    executor.registry.register(control.OpenNotepadTool(adapter), replace=True)
    assert not executor.invoke(tool.name, {}).ok
    assert not calls


@pytest.mark.parametrize("aba", [False, True])
def test_policy_changed_during_approval_blocks_dispatch(connected, aba):
    adapter, calls = connected
    class Gate:
        def request_many(self, capabilities, description):
            old = executor.policy
            executor.policy = PermissionPolicy()
            if aba:
                executor.policy = old
            return True
    executor, _ = executor_for(adapter, gate=Gate())
    assert executor.invoke("open_notepad", {}).result.metadata["kind"] == "permission_denied"
    assert not calls


def test_registry_and_policy_leases_span_service_io(connected, monkeypatch):
    adapter, calls = connected
    entered, release = threading.Event(), threading.Event()
    def request(operation):
        calls.append(operation)
        entered.set()
        assert release.wait(5)
        return "launch_accepted"
    monkeypatch.setattr(adapter, "_request", request)
    executor, tool = executor_for(adapter, level=PermissionLevel.AUTONOMOUS)
    from agentcore.errors import PolicyLeaseBusyError
    with ThreadPoolExecutor() as pool:
        future = pool.submit(executor.invoke, "open_notepad", {})
        try:
            assert entered.wait(5)
            with pytest.raises(ToolRegistryLeaseError):
                executor.registry.unregister(tool.name)
            with pytest.raises(ToolRegistryLeaseError):
                executor.registry.register(control.OpenNotepadTool(adapter), replace=True)
            with pytest.raises(PolicyLeaseBusyError):
                executor.policy = PermissionPolicy()
            assert executor.policy.level is PermissionLevel.AUTONOMOUS
        finally:
            release.set()
        assert future.result(timeout=5).ok
    executor.policy = PermissionPolicy()
    executor.registry.unregister(tool.name)
    assert calls == ["open_notepad"]


@pytest.mark.parametrize("status", ["desktop_unavailable", "unsupported_os", "service_unavailable",
                                   "launch_failed", "launch_unknown"])
def test_service_failure_is_truthful(connected, monkeypatch, status):
    adapter, _ = connected
    monkeypatch.setattr(adapter, "_request", lambda _: status)
    executor, _ = executor_for(adapter, level=PermissionLevel.AUTONOMOUS)
    result = executor.invoke("open_notepad", {}).result
    assert not result.ok and result.metadata["kind"] == status
    assert result.error == control.STATUS_TEXT[status]
    assert result.metadata["retryable"] is False


def test_unknown_launch_latches_against_automatic_retry(connected, monkeypatch):
    adapter, calls = connected
    def request(operation):
        calls.append(operation)
        return "launch_unknown"
    monkeypatch.setattr(adapter, "_request", request)
    executor, _ = executor_for(adapter, level=PermissionLevel.AUTONOMOUS)
    for _ in range(2):
        assert executor.invoke("open_notepad", {}).result.metadata["kind"] == "launch_unknown"
    assert calls == ["open_notepad"]


def test_private_transport_has_fixed_command_and_no_secrets(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setenv("MODEL_API_KEY", "secret-not-for-desktop")
    monkeypatch.setenv("PYTHONPATH", "untrusted-startup")
    def run(command, **kwargs):
        assert command[:2] == [sys.executable, "-I"]
        assert Path(command[2]) == Path(service.__file__).resolve()
        assert json.loads(kwargs["input"]) == {"v": 1, "operation": "open_notepad"}
        assert kwargs["shell"] is False and kwargs["close_fds"] is True
        assert kwargs["creationflags"] == 0x08000000 and kwargs["timeout"] == 10
        assert "MODEL_API_KEY" not in kwargs["env"] and "PYTHONPATH" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout=b'{"v":1,"status":"launch_accepted"}')
    monkeypatch.setattr(control, "_run_service", run)
    assert control.ComputerControlAdapter()._request("open_notepad") == "launch_accepted"


@pytest.mark.parametrize("response", [b"not-json", b"x" * 257, b'{"v":true,"status":"launch_accepted"}',
    b'{"v":1,"status":"ready"}', b'{"v":1,"status":[]}', b'{"v":1,"status":"launch_accepted","extra":1}'])
def test_invalid_launch_response_is_unknown(monkeypatch, response):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(control, "_run_service", lambda *a, **k: SimpleNamespace(returncode=0, stdout=response))
    assert control.ComputerControlAdapter()._request("open_notepad") == "launch_unknown"


@pytest.mark.parametrize("operation,expected", [("health", "service_unavailable"), ("open_notepad", "launch_unknown")])
def test_timeout_preserves_launch_uncertainty(monkeypatch, operation, expected):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("service", 10)
    monkeypatch.setattr(control, "_run_service", timeout)
    assert control.ComputerControlAdapter()._request(operation) == expected


def test_transport_oserror_after_possible_entry_is_unknown(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    def broken_pipe(*args, **kwargs):
        raise OSError("pipe lost after service launch")
    monkeypatch.setattr(control, "_run_service", broken_pipe)
    assert control.ComputerControlAdapter()._request("open_notepad") == "launch_unknown"


@pytest.mark.parametrize("payload", [{}, {"v": 1, "operation": "calc"}, {"v": True, "operation": "health"},
    {"v": 1, "operation": "open_notepad", "path": "evil"}, {"v": 1, "operation": []}, []])
def test_service_rejects_widened_protocol_before_native_access(monkeypatch, payload):
    monkeypatch.setattr(service, "availability", lambda: pytest.fail("invalid protocol reached native code"))
    assert service.handle_request(payload) == "invalid_request"


@pytest.mark.parametrize("status", ["unsupported_os", "desktop_unavailable", "service_unavailable"])
def test_unavailable_service_never_launches(monkeypatch, status):
    monkeypatch.setattr(service, "availability", lambda: status)
    monkeypatch.setattr(service, "_launch_notepad", lambda: pytest.fail("launch after refusal"))
    assert service.handle_request({"v": 1, "operation": "open_notepad"}) == status


@pytest.mark.parametrize("major,build,product,expected", [
    (10, 22000, 1, "ready"), (10, 26100, 1, "ready"),
    (10, 19045, 1, "unsupported_os"), (10, 26100, 3, "unsupported_os"),
    (6, 22000, 1, "unsupported_os"),
])
def test_windows_11_workstation_gate(monkeypatch, major, build, product, expected):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion", lambda: SimpleNamespace(
        platform_version=(major, 0, build), product_type=product), raising=False)
    monkeypatch.setattr(service, "_desktop_available", lambda: True)
    assert service.availability() == expected


def test_non_windows_has_no_native_or_process_access(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(control, "_run_service", lambda *a, **k: pytest.fail("process on unsupported OS"))
    assert service.availability() == "unsupported_os"
    assert control.ComputerControlAdapter({"enabled": True}).connect() is IntegrationStatus.UNAVAILABLE


def test_desktop_probe_failure_refuses(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion", lambda: SimpleNamespace(
        platform_version=(10, 0, 26100), product_type=1), raising=False)
    def denied():
        raise OSError("input desktop access denied")
    monkeypatch.setattr(service, "_desktop_available", denied)
    assert service.availability() == "desktop_unavailable"


@pytest.mark.parametrize("session,state,station,desktop,expected", [
    (0, 0, "WinSta0", "Default", False), (1, 4, "WinSta0", "Default", False),
    (1, 0, "Service", "Default", False), (1, 0, "WinSta0", "Winlogon", False),
    (1, 0, "WinSta0", "Default", True),
])
def test_active_unlocked_interactive_desktop_gate(monkeypatch, session, state, station, desktop, expected):
    closed = []
    handle = SimpleNamespace(CloseDesktop=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(UOI_NAME=2, DESKTOP_READOBJECTS=1))
    monkeypatch.setitem(sys.modules, "win32ts", SimpleNamespace(
        ProcessIdToSessionId=lambda _: session, WTS_CURRENT_SERVER_HANDLE=0,
        WTSConnectState=8, WTSActive=0, WTSQuerySessionInformation=lambda *a: state))
    monkeypatch.setitem(sys.modules, "win32service", SimpleNamespace(
        GetProcessWindowStation=lambda: "station", OpenInputDesktop=lambda *a: handle,
        GetUserObjectInformation=lambda obj, _: station if obj == "station" else desktop))
    assert service._desktop_available() is expected
    assert bool(closed) is (session != 0 and state == 0 and station == "WinSta0")


def test_real_isolated_service_health_and_invalid_input():
    for raw in (b'{"v":1,"operation":"health"}', b'{"v":1,"operation":"shell"}', b"x" * 257):
        result = subprocess.run([sys.executable, "-I", service.__file__], input=raw,
                                capture_output=True, timeout=10, check=True)
        payload = json.loads(result.stdout)
        assert payload["v"] == 1
        assert payload["status"] in {"ready", "unsupported_os", "desktop_unavailable", "service_unavailable", "invalid_request"}
        assert result.stderr == b""


def test_real_adapter_health_transport_never_launches():
    status = control.ComputerControlAdapter()._request("health")
    assert status in {"ready", "unsupported_os", "desktop_unavailable"}


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows bindings required; no application launched")
@pytest.mark.parametrize("waited,exit_code,expected", [(258, 259, "launch_accepted"),
                                                     (0, 0, "launch_accepted"), (0, 1, "launch_unknown")])
def test_native_fixed_notepad_launch_contract(monkeypatch, waited, exit_code, expected):
    import win32api
    import win32con
    import win32event
    import win32process
    closed = []
    process = SimpleNamespace(Close=lambda: closed.append("process"))
    thread = SimpleNamespace(Close=lambda: closed.append("thread"))
    def create(executable, command, proc_security, thread_security, inherit, flags, env, cwd, startup):
        assert Path(executable) == Path(win32api.GetSystemDirectory()) / "notepad.exe"
        assert command is proc_security is thread_security is None
        assert inherit is False and flags == win32con.CREATE_UNICODE_ENVIRONMENT
        assert cwd == win32api.GetSystemDirectory() and startup.lpDesktop == "winsta0\\default"
        assert "MODEL_API_KEY" not in env
        return process, thread, 123, 456
    monkeypatch.setenv("MODEL_API_KEY", "must-not-reach-notepad")
    monkeypatch.setattr(win32process, "CreateProcess", create)
    monkeypatch.setattr(win32event, "WaitForSingleObject", lambda *a: waited)
    monkeypatch.setattr(win32process, "GetExitCodeProcess", lambda _: exit_code)
    assert service._launch_notepad() == expected
    assert closed == ["thread", "process"]


def test_launch_error_is_not_success(monkeypatch):
    monkeypatch.setattr(service, "availability", lambda: "ready")
    def fail():
        raise OSError("private native error")
    monkeypatch.setattr(service, "_launch_notepad", fail)
    assert service.handle_request({"v": 1, "operation": "open_notepad"}) == "launch_failed"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows bindings required; no application launched")
def test_post_creation_error_retains_uncertainty_and_closes_handles(monkeypatch):
    import win32event
    import win32process
    closed = []
    process = SimpleNamespace(Close=lambda: closed.append("process"))
    thread = SimpleNamespace(Close=lambda: closed.append("thread"))
    monkeypatch.setattr(win32process, "CreateProcess", lambda *a: (process, thread, 1, 2))
    def fail(*args):
        raise OSError("post-creation failure")
    monkeypatch.setattr(win32event, "WaitForSingleObject", fail)
    assert service._launch_notepad() == "launch_unknown"
    assert closed == ["thread", "process"]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows bindings required; no application launched")
def test_native_launch_denial_is_reported(monkeypatch):
    import pywintypes
    import win32process
    def denied(*args):
        raise pywintypes.error(5, "CreateProcess", "Access denied")
    monkeypatch.setattr(win32process, "CreateProcess", denied)
    assert service._launch_notepad() == "launch_failed"


class ScriptedModel:
    def __init__(self):
        self.requests = []
    def generate(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ModelResponse(text="", tool_calls=(ToolCall("open", "open_notepad", {}),))
        return ModelResponse(text="Consult the desktop status below.")


def chat_config(tmp_path, *, enabled=True):
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{tmp_path.as_posix()}"\n'
                    '[permissions]\nlevel = "confirm"\n'
                    f'[conversation]\ntools_enabled = {str(enabled).lower()}\n'
                    '[logging]\nlevel = "CRITICAL"\n', encoding="utf-8")
    return ["--config", str(path)]


def test_ordinary_interactive_chat_opens_after_operator_authorization(connected, tmp_path, monkeypatch, capsys):
    _, calls = connected
    model = ScriptedModel()
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: model)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    inputs = iter(["Jarvis, open Notepad.", "open notepad", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    assert cli.main(chat_config(tmp_path) + ["chat", "--enable-desktop"]) == 0
    assert calls == ["health", "open_notepad"]
    assert any(tool["name"] == "open_notepad" for tool in model.requests[0].tools)
    assert "Windows accepted the Notepad launch" in capsys.readouterr().out


def test_json_cannot_implicitly_authorize(connected, tmp_path, monkeypatch, capsys):
    _, calls = connected
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: ScriptedModel())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("JSON prompted for approval"))
    assert cli.main(chat_config(tmp_path) + ["chat", "--enable-desktop", "--message", "Open Notepad", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["notices"] == ["[Desktop] Notepad was not launched by this request."]
    assert calls == ["health"]


def test_redirected_input_cannot_implicitly_authorize(connected, tmp_path, monkeypatch, capsys):
    _, calls = connected
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: ScriptedModel())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("redirected input prompted for approval"))
    assert cli.main(chat_config(tmp_path) + ["chat", "--enable-desktop", "--message", "Open Notepad"]) == 0
    assert "[Desktop] Notepad was not launched by this request." in capsys.readouterr().out
    assert calls == ["health"]


def test_ordinary_chat_does_not_connect_desktop(connected, tmp_path, monkeypatch, capsys):
    _, calls = connected
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: ScriptedModel())
    assert cli.main(chat_config(tmp_path) + ["chat", "--message", "Open Notepad"]) == 0
    assert calls == []


def test_desktop_requires_conversation_tool_opt_in(connected, tmp_path, capsys):
    _, calls = connected
    assert cli.main(chat_config(tmp_path, enabled=False) + ["chat", "--enable-desktop"]) == 2
    assert "tools_enabled = true" in capsys.readouterr().err
    assert not calls


def test_cli_unavailable_desktop_refuses_before_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(control.ComputerControlAdapter, "_request", lambda *a: "desktop_unavailable")
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: pytest.fail("model after service refusal"))
    assert cli.main(chat_config(tmp_path) + ["chat", "--enable-desktop", "--message", "Open Notepad"]) == 2
    assert "active, unlocked" in capsys.readouterr().err


def test_notepad_runner_does_not_approve_other_tools_or_replacement(connected):
    adapter, calls = connected
    executor, tool = executor_for(adapter)
    runner = NotepadConversationRunner(executor, executor.registry, notepad_tool=tool)
    executor.registry.register(control.OpenNotepadTool(adapter), replace=True)
    round_ = runner.run_calls([ToolCall("open", "open_notepad", {})])
    assert round_.needs_approval
    assert desktop_notices(SimpleNamespace(tool_outcomes=round_.outcomes)) == [
        "[Desktop] Notepad was not launched by this request."]
    assert not calls


def test_model_failure_after_launch_retains_authoritative_status(connected, tmp_path, monkeypatch, capsys):
    _, calls = connected
    class BrokenReply(ScriptedModel):
        def generate(self, request):
            if self.requests:
                raise RuntimeError("model failed after launch")
            return super().generate(request)
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: BrokenReply())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "open notepad")
    assert cli.main(chat_config(tmp_path) + ["chat", "--enable-desktop", "--message", "Open Notepad"]) == 1
    assert calls == ["health", "open_notepad"]
    assert "[Desktop] Windows accepted" in capsys.readouterr().out


def test_unknown_executor_failure_never_claims_non_application():
    outcome = SimpleNamespace(call=ToolCall("open", "open_notepad", {}), invocation=SimpleNamespace(
        result=SimpleNamespace(metadata={"kind": "unhandled_exception"})))
    assert "may have opened" in desktop_notices(SimpleNamespace(tool_outcomes=[outcome]))[0]


def test_missing_invocation_without_refusal_evidence_is_unknown():
    outcome = SimpleNamespace(call=ToolCall("open", "open_notepad", {}), invocation=None)
    assert "may have opened" in desktop_notices(SimpleNamespace(tool_outcomes=[outcome]))[0]


def test_cli_executor_result_loss_preserves_uncertain_notice(connected, tmp_path, monkeypatch, capsys):
    _, calls = connected
    invoke = ToolExecutor.invoke

    def lose_result(self, *args, **kwargs):
        invocation = invoke(self, *args, **kwargs)
        assert invocation.result.metadata["kind"] == "launch_accepted"
        raise RuntimeError("executor result lost")

    monkeypatch.setattr(ToolExecutor, "invoke", lose_result)
    monkeypatch.setattr(cli.Agent, "_build_model", lambda _: ScriptedModel())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "open notepad")
    assert cli.main(chat_config(tmp_path) + ["chat", "--enable-desktop", "--message", "Open Notepad"]) == 0
    output = capsys.readouterr().out
    assert "[Desktop] Notepad may have opened" in output
    assert "Notepad was not launched" not in output
    assert calls == ["health", "open_notepad"]


@pytest.mark.parametrize("lost_result", [True, False])
def test_result_loss_after_accepted_dispatch_blocks_chat_retries(connected, monkeypatch, lost_result):
    adapter, calls = connected
    executor, tool = executor_for(adapter, level=PermissionLevel.AUTONOMOUS)
    runner = NotepadConversationRunner(executor, executor.registry, notepad_tool=tool)
    invoke = executor.invoke
    invocations = []

    def lose_result(*args, **kwargs):
        invocations.append(True)
        invocation = invoke(*args, **kwargs)
        assert invocation.result.metadata["kind"] == "launch_accepted"
        if lost_result:
            raise RuntimeError("executor result lost after service acceptance")
        invocation.result = control.ToolResult.failure("result lost", kind="unhandled_exception")
        return invocation

    monkeypatch.setattr(executor, "invoke", lose_result)
    # Cover duplicates in one model response, a later round, and a later user turn.
    outcomes = runner.run_calls([ToolCall("first", "open_notepad", {}),
                                ToolCall("duplicate", "open_notepad", {})]).outcomes
    for identifier in ("next_round", "next_turn"):
        outcomes += runner.run_calls([ToolCall(identifier, "open_notepad", {})]).outcomes
    assert len(outcomes) == 4 and all(not outcome.ok for outcome in outcomes)
    notices = desktop_notices(SimpleNamespace(tool_outcomes=outcomes))
    assert all("may have opened" in notice and "Check the desktop" in notice for notice in notices)
    assert all("may have opened" in outcome.content for outcome in outcomes)
    assert calls == ["open_notepad"]
    assert invocations == [True]


def test_pre_dispatch_refusals_do_not_latch_notepad(connected):
    adapter, calls = connected
    executor, tool = executor_for(adapter, level=PermissionLevel.AUTONOMOUS)
    runner = NotepadConversationRunner(executor, executor.registry, notepad_tool=tool)
    executor.registry.unregister(tool.name)
    missing = runner.run_calls([ToolCall("missing", "open_notepad", {})]).outcomes[0]
    executor.registry.register(tool)
    invalid = runner.run_calls([ToolCall("invalid", "open_notepad", {"path": "other.exe"})]).outcomes[0]
    executor.policy = PermissionPolicy()
    denied = runner.run_calls([ToolCall("denied", "open_notepad", {})]).outcomes[0]
    assert desktop_notices(SimpleNamespace(tool_outcomes=[missing, invalid, denied])) == [
        "[Desktop] Notepad was not launched by this request."] * 3
    assert not calls
    executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    assert runner.run_calls([ToolCall("allowed", "open_notepad", {})]).outcomes[0].ok
    assert calls == ["open_notepad"]
