"""Offline PowerShell receipt checks, including native Python console input."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher")
LAUNCHER = Path(__file__).resolve().parents[1] / "tools" / "Start-Desktop-Demo.ps1"
CALL = {"name": "get_foreground_app", "arguments": {}, "status": "ok",
        "executor_returned": True, "executed": True}
OBSERVED = {"status": "observed", "app": "Notepad"}


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell"])
@pytest.mark.parametrize("changes,answers,expected_app,passed", [
    ({}, ["yes", "yes"], "Notepad", True),
    ({}, ["no", "yes"], "Notepad", False),
    ({}, ["yes", "no"], "Notepad", False),
    ({"desktop_tool_calls": [], "desktop_observations": [],
      "reply": "I used get_foreground_app and saw Notepad."}, [], "Notepad", False),
    ({"ok": False, "error_category": "model_timeout", "reply": None}, ["yes"], "Notepad", False),
    ({"desktop_observations": [{"status": "unknown", "app": None}],
      "reply": "The foreground application is unknown."}, ["yes", "yes"], "unknown", True),
    ({"desktop_observations": [{"status": "unknown", "app": None, "reason": "shared_application_host"}],
      "reply": "I don't have access to your current application context."}, ["yes", "no"], "unknown", False),
    ({"desktop_observations": [{"status": "observed", "app": "Google Chrome"}]}, ["yes"], "Notepad", False),
    ({"desktop_tool_calls": [{**CALL, "arguments": None}]}, ["yes"], "Notepad", False),
    ({"desktop_tool_calls": [CALL, CALL]}, ["yes"], "Notepad", False),
    ({"desktop_tool_calls": [{**CALL, "executor_returned": False, "executed": False}]}, ["yes"], "Notepad", False),
    ({"reply": ""}, ["yes"], "Notepad", False),
])
def test_launcher_requires_separate_call_native_reply_and_human_evidence(
        tmp_path, changes, answers, expected_app, passed, shell_name):
    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell, "Windows verification requires the built-in PowerShell"
    payload = {"ok": True, "reply": "You are using Notepad.", "desktop_tool_calls": [CALL],
               "desktop_observations": [OBSERVED], **changes}
    fixture = tmp_path / "cli.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    config = tmp_path / "agent.local.toml"
    config.write_text("# no credential\n", encoding="utf-8")
    results = tmp_path / "receipts"
    invocation = tmp_path / "invocation.json"
    command = f"""$global:task49Answers = [System.Collections.Generic.Queue[string]]::new()
foreach ($answer in @({', '.join(ps_quote(answer) for answer in answers)})) {{ $global:task49Answers.Enqueue($answer) }}
function Read-Host {{
    param([string]$Prompt)
    if ($global:task49Answers.Count -eq 0) {{ throw 'Unexpected operator question.' }}
    return $global:task49Answers.Dequeue()
}}
function Start-Sleep {{ param([int]$Seconds) if ($Seconds -ne 8) {{ throw 'Wrong focus delay.' }} }}
function git {{ $global:LASTEXITCODE = 0; return '42fae5e72cd6ba014982bd6da0c77d3cbd88b996' }}
function python {{
    ConvertTo-Json -InputObject @($args) | Set-Content -LiteralPath {ps_quote(invocation)} -Encoding utf8
    $global:LASTEXITCODE = {0 if payload['ok'] else 1}
    Get-Content -LiteralPath {ps_quote(fixture)}
}}
& {ps_quote(LAUNCHER)} -Config {ps_quote(config)} -ExpectedApp {ps_quote(expected_app)} -EvidenceDirectory {ps_quote(results)}
if ($global:task49Answers.Count -ne 0) {{ throw 'Missing operator question.' }}
"""
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = list(results.glob("conversation-*.json"))
    assert len(records) == 1
    receipt = json.loads(records[0].read_text(encoding="utf-8-sig"))
    assert receipt["live_conversation_pass"] is passed
    assert receipt["desktop_tool_calls"] == payload["desktop_tool_calls"]
    assert receipt["desktop_observations"] == payload["desktop_observations"]
    assert receipt["model_reply"] == payload["reply"]
    assert receipt["operator_confirmed_native"] is bool(answers and answers[0] == "yes")
    assert receipt["operator_confirmed_reply"] is bool(len(answers) > 1 and answers[1] == "yes")
    assert ("PASS for this single" in completed.stdout) is passed
    assert receipt["commit"] == "42fae5e72cd6ba014982bd6da0c77d3cbd88b996"
    sent = json.loads(invocation.read_text(encoding="utf-8-sig"))
    assert sent == ["-m", "agentcore.cli", "--config", str(config), "chat", "--enable-desktop",
                    "--message", "Jarvis, what application am I using right now?", "--json"]


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell"])
@pytest.mark.parametrize("case,answers,passed", [
    ("denied", ["yes", "yes"], True), ("bad_reply", ["yes", "no"], False),
    ("new_window", ["no", "yes"], False), ("no_call", [], False), ("no_gate", [], False),
    ("malformed", [], False), ("uncertain", [], False), ("approved", [], False),
    ("model_failed", [], False), ("refused", [], False),
])
def test_denial_launcher_requires_real_gate_executor_and_operator_evidence(tmp_path, shell_name, case, answers, passed):
    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell
    payload = {"conversation_ok": True, "model_reply": "Your request was denied; Notepad was not opened.",
        "error_category": None,
        "desktop_tool_calls": [{**CALL, "name": "open_notepad", "status": "denied", "executed": False}],
        "approval": {"request_count": 1, "decision": "denied", "explicit_denial": True},
        "launch_results": [{"arguments_validated": True, "executor_category": "permission_denied",
            "launch_assessment": "not_launched", "native_action_invoked": False, "native_outcome": None}]}
    if case == "bad_reply": payload["model_reply"] = "Notepad was opened."
    if case == "no_call": payload["desktop_tool_calls"] = payload["launch_results"] = []
    if case in ("no_call", "no_gate"):
        payload["approval"] = {"request_count": 0, "decision": None, "explicit_denial": False}
    if case == "malformed": payload["desktop_tool_calls"][0]["arguments"] = None
    if case == "uncertain":
        payload["desktop_tool_calls"][0].update(status="failed", executor_returned=False)
        payload["launch_results"][0].update(launch_assessment="launch_unknown", native_action_invoked=None)
    if case == "approved":
        payload["approval"].update(decision="approved", explicit_denial=False)
        payload["launch_results"][0].update(native_action_invoked=True, native_outcome="launch_accepted")
    if case == "model_failed": payload.update(conversation_ok=False, model_reply=None, error_category="model_timeout")
    fixture = tmp_path / "cli.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    config = tmp_path / "agent.local.toml"
    config.write_text("# no credential\n", encoding="utf-8")
    invocation = tmp_path / "invocation.json"
    results = tmp_path / "receipts"
    command = f"""$global:task49Answers = [System.Collections.Generic.Queue[string]]::new()
foreach ($answer in @({', '.join(ps_quote(answer) for answer in answers)})) {{ $global:task49Answers.Enqueue($answer) }}
function Read-Host {{
    param([string]$Prompt)
    if ($global:task49Answers.Count -eq 0) {{ throw 'Unexpected operator question.' }}
    return $global:task49Answers.Dequeue()
}}
function Start-Sleep {{ throw 'Denial test must not use the foreground delay.' }}
function git {{ $global:LASTEXITCODE = 0; return '55c6ca468dd7981c3bdc2c6489c31b9b2ab7a68b' }}
function python {{
    ConvertTo-Json -InputObject @($args) | Set-Content -LiteralPath {ps_quote(invocation)} -Encoding utf8
    $taskReceipt = $args[[array]::IndexOf($args, '--desktop-evidence') + 1]
    Get-Content -LiteralPath {ps_quote(fixture)} -Raw | Set-Content -LiteralPath $taskReceipt -Encoding utf8
    $global:LASTEXITCODE = {2 if case == 'refused' else 0 if payload['conversation_ok'] else 1}
}}
& {ps_quote(LAUNCHER)} -Config {ps_quote(config)} -NotepadDenial -EvidenceDirectory {ps_quote(results)}
if ($global:task49Answers.Count -ne 0) {{ throw 'Missing operator question.' }}
"""
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = list(results.glob("notepad-denial-*.json"))
    assert len(records) == 1
    receipt = json.loads(records[0].read_text(encoding="utf-8-sig"))
    if case == "refused":
        assert receipt == payload and "LIVE NOTEPAD DENIAL PASS" not in completed.stdout
        return
    assert receipt["denial_test_pass"] is passed
    assert receipt["mode"] == "NotepadDenial" and receipt["commit"] == "55c6ca468dd7981c3bdc2c6489c31b9b2ab7a68b"
    assert receipt["approval"] == payload["approval"] and receipt["launch_results"] == payload["launch_results"]
    assert receipt["model_reply"] == payload["model_reply"]
    assert receipt["operator_confirmed_no_new_window"] is bool(answers and answers[0] == "yes")
    assert receipt["operator_confirmed_reply"] is bool(len(answers) > 1 and answers[1] == "yes")
    assert ("LIVE NOTEPAD DENIAL PASS" in completed.stdout) is passed
    sent = json.loads(invocation.read_text(encoding="utf-8-sig"))
    assert sent == ["-m", "agentcore.cli", "--config", str(config), "chat", "--enable-desktop",
                    "--message", "Jarvis, open Notepad.", "--desktop-evidence", str(records[0])]


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("case", ["denied", "cli_failure", "refused", "missing_receipt"])
def test_denial_launcher_with_native_python_console(tmp_path, shell_name, case):
    from _windows_console import run_console

    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell
    # Copy only the launcher; substitute model/native work, never Python or input().
    tools = tmp_path / "tools"
    tools.mkdir()
    launcher = tools / LAUNCHER.name
    shutil.copyfile(LAUNCHER, launcher)
    package = tmp_path / "src" / "agentcore"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"__path__.append({str(LAUNCHER.parents[1] / 'src' / 'agentcore')!r})\n", encoding="utf-8")
    (package / "cli.py").write_text(f"""import json, sys
from pathlib import Path
from agentcore.desktop.chat import ConsoleNotepadApproval
from agentcore.permissions.levels import Capability
assert sys.stdin.isatty() and sys.stdout.isatty()
print('OFFLINE_STDIN_TTY', flush=True)
gate = ConsoleNotepadApproval()
assert gate.request_many((Capability.APP_CONTROL, Capability.PROCESS_SPAWN), 'offline fixture') is False
assert gate.request_count == 1 and gate.decision == 'denied' and gate.explicit_denial
payload = {{'conversation_ok': True, 'model_reply': 'Request denied; Notepad was not opened.',
    'error_category': None,
    'desktop_tool_calls': [{{'name': 'open_notepad', 'arguments': {{}}, 'status': 'denied',
        'executor_returned': True, 'executed': False}}],
    'approval': {{'request_count': gate.request_count, 'decision': gate.decision, 'explicit_denial': gate.explicit_denial}},
    'launch_results': [{{'arguments_validated': True, 'executor_category': 'permission_denied',
        'launch_assessment': 'not_launched', 'native_action_invoked': False, 'native_outcome': None}}]}}
record = Path(sys.argv[sys.argv.index('--desktop-evidence') + 1])
with (record.parent / 'attempts.txt').open('a') as attempts:
    attempts.write('one attempt\\n')
if {case != 'missing_receipt'!r}:
    record.write_text(json.dumps(payload), encoding='utf-8')
if {case == 'cli_failure'!r}:
    print('OFFLINE_CLI_FAILURE', file=sys.stderr, flush=True)
sys.exit({1 if case == 'cli_failure' else 2 if case == 'refused' else 0})
""", encoding="utf-8")
    config = tmp_path / "agent.local.toml"
    config.write_text("# offline fixture, no credential\n", encoding="utf-8")
    results = tmp_path / "receipts"
    driver = tmp_path / "driver.ps1"
    driver.write_text(f"""$ErrorActionPreference = 'Stop'
Set-Alias python {ps_quote(sys.executable)}
function git {{ $global:LASTEXITCODE = 0; return '59182ddce411a73a96b4c333aeeab06fbff9b50a' }}
& {ps_quote(launcher)} -Config {ps_quote(config)} -NotepadDenial -EvidenceDirectory {ps_quote(results)}
Write-Host 'OFFLINE_LAUNCHER_RETURNED'
""", encoding="utf-8-sig")
    code, output = run_console([shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                               ready="OFFLINE_STDIN_TTY", answers="no\ryes\ryes\r")
    assert code == 0, output
    assert "OFFLINE_LAUNCHER_RETURNED" in output and "NativeCommandError" not in output
    assert "Type 'open notepad' to allow:" in output
    assert (results / "attempts.txt").read_text() == "one attempt\n"
    records = list(results.glob("notepad-denial-*.json"))
    if case == "missing_receipt":
        assert not records and "No complete structured receipt" in output
        assert "LIVE NOTEPAD DENIAL PASS" not in output
        return
    assert len(records) == 1
    receipt = json.loads(records[0].read_text(encoding="utf-8-sig"))
    assert receipt["approval"] == {"request_count": 1, "decision": "denied", "explicit_denial": True}
    assert receipt["launch_results"][0]["native_action_invoked"] is False
    if case == "refused":
        assert "denial_test_pass" not in receipt and "Interactive turn refused" in output
    else:
        assert receipt["denial_test_pass"] is (case == "denied")
        assert receipt["operator_confirmed_no_new_window"] is (case == "denied")
        assert receipt["operator_confirmed_reply"] is (case == "denied")
    assert ("LIVE NOTEPAD DENIAL PASS" in output) is (case == "denied")
    assert ("Did you see the real approval prompt" in output) is (case == "denied")
    assert ("Does the final MODEL REPLY" in output) is (case == "denied")
    if case == "cli_failure":
        assert "OFFLINE_CLI_FAILURE" in output
