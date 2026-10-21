"""Offline PowerShell receipt checks; the CLI is substituted, no desktop is used."""

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
    assert ("PASS for this single" in completed.stdout) is passed
    assert receipt["commit"] == "42fae5e72cd6ba014982bd6da0c77d3cbd88b996"
    sent = json.loads(invocation.read_text(encoding="utf-8-sig"))
    assert sent == ["-m", "agentcore.cli", "--config", str(config), "chat", "--enable-desktop",
                    "--message", "Jarvis, what application am I using right now?", "--json"]
