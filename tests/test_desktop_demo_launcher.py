"""Offline PowerShell receipt checks, including native Python console input."""

import json
import os
from pathlib import Path
import py_compile
from importlib.util import cache_from_source
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
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{ return '42fae5e72cd6ba014982bd6da0c77d3cbd88b996' }}
    if ($args -contains 'status' -or $args -contains 'ls-files') {{ return }}
    throw 'Unexpected Git provenance command.'
}}
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
    assert sent[:2] == ["-B", "-X"]
    assert sent[2].startswith("pycache_prefix=")
    assert not Path(sent[2].split("=", 1)[1]).exists()
    assert sent[3:] == ["-m", "agentcore.cli", "--config", str(config), "chat", "--enable-desktop",
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
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{ return '55c6ca468dd7981c3bdc2c6489c31b9b2ab7a68b' }}
    if ($args -contains 'status' -or $args -contains 'ls-files') {{ return }}
    throw 'Unexpected Git provenance command.'
}}
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
    assert sent[:2] == ["-B", "-X"]
    assert sent[2].startswith("pycache_prefix=")
    assert not Path(sent[2].split("=", 1)[1]).exists()
    assert sent[3:] == ["-m", "agentcore.cli", "--config", str(config), "chat", "--enable-desktop",
                    "--message", "Jarvis, open Notepad.", "--desktop-evidence", str(records[0])]


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("approval_mode", [False, True])
@pytest.mark.parametrize("case", ["completed", "cli_failure", "refused", "missing_receipt"])
def test_notepad_launcher_with_native_python_console(tmp_path, shell_name, case, approval_mode):
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
assert gate.request_many((Capability.APP_CONTROL, Capability.PROCESS_SPAWN), 'offline fixture') is {approval_mode!r}
assert gate.request_count == 1 and gate.decision == {'approved' if approval_mode else 'denied'!r}
assert gate.explicit_denial is {not approval_mode!r}
payload = {{'conversation_ok': True, 'model_reply': 'Request denied; Notepad was not opened.',
    'error_category': None,
    'desktop_tool_calls': [{{'name': 'open_notepad', 'arguments': {{}}, 'status': 'denied',
        'executor_returned': True, 'executed': False}}],
    'approval': {{'request_count': gate.request_count, 'decision': gate.decision, 'explicit_denial': gate.explicit_denial}},
    'launch_results': [{{'arguments_validated': True, 'executor_category': 'permission_denied',
        'launch_assessment': 'not_launched', 'native_action_invoked': False, 'native_outcome': None}}]}}
if {approval_mode!r}:
    payload['model_reply'] = 'Windows accepted the launch; a visible window is not verified.'
    payload['desktop_tool_calls'][0].update(status='ok', executed=True)
    payload['launch_results'][0].update(executor_category='launch_accepted', launch_assessment='launch_accepted',
        native_action_invoked=True, native_outcome='launch_accepted')
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
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{ return '59182ddce411a73a96b4c333aeeab06fbff9b50a' }}
    if ($args -contains 'status' -or $args -contains 'ls-files') {{ return }}
    throw 'Unexpected Git provenance command.'
}}
& {ps_quote(launcher)} -Config {ps_quote(config)} -{'NotepadApproval' if approval_mode else 'NotepadDenial'} -EvidenceDirectory {ps_quote(results)}
Write-Host 'OFFLINE_LAUNCHER_RETURNED'
""", encoding="utf-8-sig")
    code, output = run_console([shell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver)],
                               ready="OFFLINE_STDIN_TTY",
                               answers=("open notepad" if approval_mode else "no") + "\ryes\ryes\r")
    assert code == 0, output
    assert "OFFLINE_LAUNCHER_RETURNED" in output and "NativeCommandError" not in output
    assert "Type 'open notepad' to allow:" in output
    assert (results / "attempts.txt").read_text() == "one attempt\n"
    verdict = "LIVE NOTEPAD APPROVAL PASS" if approval_mode else "LIVE NOTEPAD DENIAL PASS"
    pass_field = "approval_test_pass" if approval_mode else "denial_test_pass"
    visual_field = "operator_confirmed_new_window" if approval_mode else "operator_confirmed_no_new_window"
    records = list(results.glob("notepad-approval-*.json" if approval_mode else "notepad-denial-*.json"))
    if case == "missing_receipt":
        assert not records and "No complete structured receipt" in output
        assert verdict not in output
        return
    assert len(records) == 1
    receipt = json.loads(records[0].read_text(encoding="utf-8-sig"))
    assert receipt["approval"] == {"request_count": 1, "decision": "approved" if approval_mode else "denied",
                                   "explicit_denial": not approval_mode}
    assert receipt["launch_results"][0]["native_action_invoked"] is approval_mode
    if case == "refused":
        assert pass_field not in receipt and "Interactive turn refused" in output
    else:
        assert receipt[pass_field] is (case == "completed")
        assert receipt[visual_field] is (case == "completed")
        assert receipt["operator_confirmed_reply"] is (case == "completed")
    assert (verdict in output) is (case == "completed")
    assert ("Did you see the real approval prompt" in output) is (case == "completed")
    assert ("Does the final MODEL REPLY" in output) is (case == "completed")
    if case == "cli_failure":
        assert "OFFLINE_CLI_FAILURE" in output


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("case,answers,passed", [
    ("accepted", ["yes", "yes"], True), ("no_window", ["no", "yes"], False),
    ("bad_reply", ["yes", "no"], False), ("denied", [], False), ("no_call", [], False),
    ("malformed", [], False), ("not_validated", [], False), ("no_prompt", [], False),
    ("no_approval", [], False), ("uncertain", [], False), ("unknown", [], False),
    ("native_failed", [], False), ("model_failed", [], False), ("duplicate", [], False),
    ("cli_failed", [], False), ("refused", [], False),
])
def test_approval_launcher_requires_native_acceptance_and_two_human_confirmations(
        tmp_path, shell_name, case, answers, passed):
    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell
    payload = {"conversation_ok": True, "model_reply": "Windows accepted the launch; visibility is not verified.",
        "error_category": None,
        "desktop_tool_calls": [{**CALL, "name": "open_notepad"}],
        "approval": {"request_count": 1, "decision": "approved", "explicit_denial": False},
        "launch_results": [{"arguments_validated": True, "executor_category": "launch_accepted",
            "launch_assessment": "launch_accepted", "native_action_invoked": True, "native_outcome": "launch_accepted"}]}
    if case == "bad_reply": payload["model_reply"] = "I visually verified a new Notepad window."
    if case == "denied":
        payload["approval"].update(decision="denied", explicit_denial=True)
        payload["desktop_tool_calls"][0].update(status="denied", executed=False)
        payload["launch_results"][0].update(executor_category="permission_denied", launch_assessment="not_launched",
                                            native_action_invoked=False, native_outcome=None)
    if case == "no_call": payload["desktop_tool_calls"] = payload["launch_results"] = []
    if case == "malformed": payload["desktop_tool_calls"][0]["arguments"] = None
    if case == "not_validated": payload["launch_results"][0]["arguments_validated"] = False
    if case in ("no_call", "no_prompt"): payload["approval"]["request_count"] = 0
    if case == "no_approval": payload["approval"]["decision"] = None
    if case in ("uncertain", "unknown", "native_failed"):
        payload["desktop_tool_calls"][0].update(status="failed", executed=False, executor_returned=case != "uncertain")
        kind = None if case == "uncertain" else "launch_failed" if case == "native_failed" else "launch_unknown"
        payload["launch_results"][0].update(executor_category=kind,
            launch_assessment=kind or "launch_unknown", native_action_invoked=None, native_outcome=kind)
    if case == "model_failed": payload.update(conversation_ok=False, model_reply=None, error_category="model_timeout")
    if case == "duplicate": payload["desktop_tool_calls"] *= 2
    fixture = tmp_path / "cli.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    config = tmp_path / "agent.local.toml"
    config.write_text("# offline fixture, no credential\n", encoding="utf-8")
    results = tmp_path / "receipts"
    command = f"""$global:task49Answers = [System.Collections.Generic.Queue[string]]::new()
foreach ($answer in @({', '.join(ps_quote(answer) for answer in answers)})) {{ $global:task49Answers.Enqueue($answer) }}
$global:task49Attempts = 0
function Read-Host {{
    param([string]$Prompt)
    if ($global:task49Answers.Count -eq 0) {{ throw 'Unexpected operator question.' }}
    return $global:task49Answers.Dequeue()
}}
function Start-Sleep {{ throw 'No foreground delay in the Notepad approval test.' }}
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{ return '8bde554a898fa90bd162ad5e0e93dd436b8a0717' }}
    if ($args -contains 'status' -or $args -contains 'ls-files') {{ return }}
    throw 'Unexpected Git provenance command.'
}}
function python {{
    $global:task49Attempts++
    if (@($args) -contains '--json' -or $args[-3] -ne 'Jarvis, open Notepad.') {{ throw 'Wrong interactive request.' }}
    $taskReceipt = $args[[array]::IndexOf($args, '--desktop-evidence') + 1]
    Get-Content -LiteralPath {ps_quote(fixture)} -Raw | Set-Content -LiteralPath $taskReceipt -Encoding utf8
    $global:LASTEXITCODE = {2 if case == 'refused' else 1 if case in ('cli_failed', 'model_failed') else 0}
}}
& {ps_quote(LAUNCHER)} -Config {ps_quote(config)} -NotepadApproval -EvidenceDirectory {ps_quote(results)}
if ($global:task49Answers.Count -ne 0 -or $global:task49Attempts -ne 1) {{ throw 'Missing question or repeated invocation.' }}
"""
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = list(results.glob("notepad-approval-*.json"))
    assert len(records) == 1 and not list(results.glob("notepad-denial-*.json"))
    receipt = json.loads(records[0].read_text(encoding="utf-8-sig"))
    for field in ("approval", "launch_results", "desktop_tool_calls", "model_reply"):
        assert receipt[field] == payload[field]
    if case == "refused":
        assert receipt == payload and "LIVE NOTEPAD APPROVAL PASS" not in completed.stdout
        return
    assert receipt["approval_test_pass"] is passed
    assert receipt["mode"] == "NotepadApproval" and receipt["commit"] == "8bde554a898fa90bd162ad5e0e93dd436b8a0717"
    assert "denial_test_pass" not in receipt and "operator_confirmed_no_new_window" not in receipt
    assert receipt["operator_confirmed_new_window"] is bool(answers and answers[0] == "yes")
    assert receipt["operator_confirmed_reply"] is bool(answers and answers[1] == "yes")
    assert ("LIVE NOTEPAD APPROVAL PASS" in completed.stdout) is passed


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
def test_notepad_modes_are_mutually_exclusive_before_any_cli_turn(tmp_path, shell_name):
    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell
    command = f"""$ErrorActionPreference = 'Stop'
function python {{ throw 'CLI must not be invoked.' }}
& {ps_quote(LAUNCHER)} -Config {ps_quote(tmp_path / 'unused.toml')} -NotepadDenial -NotepadApproval
"""
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode != 0 and "Select only one Notepad test" in completed.stderr
    assert "CLI must not be invoked" not in completed.stderr


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("mode", ["foreground", "approval"])
@pytest.mark.parametrize("case", [
    "dirty_before", "staged_before", "untracked_before", "dirty_after",
    "head_after", "status_error",
])
def test_launcher_refuses_unverifiable_checkout_without_commit_attribution(
        tmp_path, shell_name, mode, case):
    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell
    fixture = tmp_path / "cli.json"
    payload = ({"ok": True, "reply": "You are using Notepad.",
                "desktop_tool_calls": [CALL], "desktop_observations": [OBSERVED]}
               if mode == "foreground" else
               {"conversation_ok": True, "model_reply": "Windows accepted the launch.",
                "error_category": None, "desktop_tool_calls": [{**CALL, "name": "open_notepad"}],
                "approval": {"request_count": 1, "decision": "approved", "explicit_denial": False},
                "launch_results": [{"arguments_validated": True, "executor_category": "launch_accepted",
                                    "launch_assessment": "launch_accepted", "native_action_invoked": True,
                                    "native_outcome": "launch_accepted"}]})
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    config = tmp_path / "agent.local.toml"
    config.write_text("# test only\n", encoding="utf-8")
    results = tmp_path / "receipts"
    command = f"""$ErrorActionPreference = 'Stop'
$global:task49Checks = 0
$global:task49Invocations = 0
function Read-Host {{ throw 'An unverified run must not solicit a positive verdict.' }}
function Start-Sleep {{ param([int]$Seconds) }}
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{
        if ('{case}' -eq 'head_after' -and $global:task49Checks -gt 0) {{
            return '0000000000000000000000000000000000000000'
        }}
        return 'b6ca6176af7e42aeb709912eb3a1b4fa75c4686e'
    }}
    if ($args -contains 'status') {{
        $global:task49Checks++
        if ('{case}' -eq 'status_error') {{ $global:LASTEXITCODE = 1; return }}
        if ('{case}' -eq 'dirty_before') {{ return ' M tools/Start-Desktop-Demo.ps1' }}
        if ('{case}' -eq 'staged_before') {{ return 'M  tools/Start-Desktop-Demo.ps1' }}
        if ('{case}' -eq 'dirty_after' -and $global:task49Checks -gt 1) {{
            return ' M src/agentcore/cli.py'
        }}
        return
    }}
    if ($args -contains 'ls-files') {{
        if ('{case}' -eq 'untracked_before') {{ return 'src/agentcore/shadow.py' }}
        return
    }}
    throw 'Unexpected git invocation.'
}}
function python {{
    $global:task49Invocations++
    if ('{mode}' -eq 'foreground') {{
        Get-Content -LiteralPath {ps_quote(fixture)}
    }} else {{
        $record = $args[[array]::IndexOf($args, '--desktop-evidence') + 1]
        Get-Content -LiteralPath {ps_quote(fixture)} -Raw | Set-Content -LiteralPath $record -Encoding utf8
    }}
    $global:LASTEXITCODE = 0
}}
try {{
    & {ps_quote(LAUNCHER)} -Config {ps_quote(config)} -EvidenceDirectory {ps_quote(results)} {'-NotepadApproval' if mode == 'approval' else ''}
    throw 'The launcher accepted an unverifiable checkout.'
}} catch {{
    if ($_.Exception.Message -notmatch 'Demo provenance unavailable') {{ throw }}
    Write-Host 'EXPECTED_PROVENANCE_REFUSAL'
}}
Write-Host "INVOCATIONS=$global:task49Invocations"
"""
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "EXPECTED_PROVENANCE_REFUSAL" in completed.stdout
    expected_calls = 1 if case in ("dirty_after", "head_after") else 0
    assert f"INVOCATIONS={expected_calls}" in completed.stdout
    if mode == "foreground":
        assert not list(results.glob("conversation-*.json"))
    elif expected_calls:
        receipts = list(results.glob("notepad-approval-*.json"))
        assert len(receipts) == 1
        # Original CLI receipt survives without SHA or PASS backfill.
        assert json.loads(receipts[0].read_text(encoding="utf-8-sig")) == payload
    else:
        assert not results.exists()


def test_isolated_bytecode_prefix_prevents_ignored_cache_execution(tmp_path):
    """A loadable malicious checkout cache must not override committed .py source."""
    source = tmp_path / "src"
    source.mkdir()
    module = source / "cacheprobe.py"
    evil = 'RESULT = "EVIL"\n'
    safe = 'RESULT = "SAFE"\n'
    assert len(evil) == len(safe)
    module.write_text(evil, encoding="utf-8")
    py_compile.compile(str(module), cfile=cache_from_source(str(module)),
                       invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
                       doraise=True)
    stat = module.stat()
    module.write_text(safe, encoding="utf-8")
    os.utime(module, (stat.st_atime, stat.st_mtime))

    def execute(*options):
        process = subprocess.run([sys.executable, *options, "-c",
                                  "import cacheprobe; print(cacheprobe.RESULT)"],
                                 cwd=source, capture_output=True, text=True,
                                 timeout=20, check=True)
        return process.stdout.strip()

    # -B alone prevents writes but does not prevent loading forged existing bytecode.
    assert execute("-B") == "EVIL"
    fresh = tmp_path / "fresh-bytecode-cache"
    assert execute("-B", "-X", f"pycache_prefix={fresh}") == "SAFE"
    assert not list(fresh.rglob("*.pyc"))


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
def test_receipt_enrichment_partial_write_failure_keeps_cli_evidence(tmp_path, shell_name):
    shell = shutil.which(shell_name)
    if shell_name == "pwsh" and not shell:
        pytest.skip("Optional PowerShell 7 is not installed")
    assert shell
    config = tmp_path / "agent.local.toml"
    config.write_text("# offline fixture\n", encoding="utf-8")
    receipt_dir = tmp_path / "receipts"
    payload = {
        "conversation_ok": True, "model_reply": "Windows accepted the launch.",
        "error_category": None,
        "desktop_tool_calls": [{**CALL, "name": "open_notepad"}],
        "approval": {"request_count": 1, "decision": "approved", "explicit_denial": False},
        "launch_results": [{"arguments_validated": True, "executor_category": "launch_accepted",
                            "launch_assessment": "launch_accepted", "native_action_invoked": True,
                            "native_outcome": "launch_accepted"}],
    }
    fixture = tmp_path / "cli.json"
    fixture.write_text(json.dumps(payload), encoding="utf-8")
    command = f"""$ErrorActionPreference = 'Stop'
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{ return '6bdf479f4ddaa609f47c47a5642b23566c480938' }}
    if ($args -contains 'status' -or $args -contains 'ls-files') {{ return }}
    throw 'Unexpected git command.'
}}
$global:task49Confirmations = 0
function Read-Host {{
    param([string]$Prompt)
    $global:task49Confirmations++
    return 'yes'
}}
function python {{
    $record = $args[[array]::IndexOf($args, '--desktop-evidence') + 1]
    Microsoft.PowerShell.Management\\Set-Content -LiteralPath $record -Value (Get-Content -LiteralPath {ps_quote(fixture)} -Raw) -Encoding utf8
    $global:LASTEXITCODE = 0
}}
function Set-Content {{
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline = $true)] $InputObject,
          [string]$LiteralPath, [string]$Encoding)
    process {{
        if ($LiteralPath -like '*.tmp') {{
            [IO.File]::WriteAllText($LiteralPath, '{{"partial":')
            throw 'INJECTED_PARTIAL_WRITE_FAILURE'
        }}
        Microsoft.PowerShell.Management\Set-Content -LiteralPath $LiteralPath -Value $InputObject -Encoding $Encoding
    }}
}}
try {{
    & {ps_quote(LAUNCHER)} -Config {ps_quote(config)} -NotepadApproval -EvidenceDirectory {ps_quote(receipt_dir)}
    throw 'Launcher should not succeed after interrupted enrichment.'
}} catch {{
    if ($_.Exception.Message -notmatch 'INJECTED_PARTIAL_WRITE_FAILURE') {{ throw }}
    Write-Host 'EXPECTED_ENRICHMENT_FAILURE'
}}
Write-Host "CONFIRMATIONS=$global:task49Confirmations"
"""
    completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "EXPECTED_ENRICHMENT_FAILURE" in completed.stdout
    assert "CONFIRMATIONS=2" in completed.stdout
    records = list(receipt_dir.glob("notepad-approval-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8-sig")) == payload
    assert not list(receipt_dir.glob("*.tmp"))
    assert "LIVE NOTEPAD APPROVAL PASS" not in completed.stdout
