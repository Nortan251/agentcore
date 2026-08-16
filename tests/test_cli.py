from __future__ import annotations

import json

import pytest

from agentcore.cli import main


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_status_command(capsys):
    assert main(["status"]) == 0
    payload = _json_out(capsys)
    assert payload["permission_level"] == "observe"
    assert "echo" in payload["tools"]


def test_tools_command(capsys):
    assert main(["tools"]) == 0
    names = {t["name"] for t in _json_out(capsys)}
    assert names == {"echo", "workspace_info", "read_file", "list_directory"}


def test_doctor_reports_computer_control_unavailable(capsys):
    assert main(["doctor"]) == 0
    payload = _json_out(capsys)
    assert payload["features_enabled"] == []
    cc = next(i for i in payload["integrations"]
              if i["name"] == "computer_control")
    assert cc["status"] == "unavailable"


def test_run_command_blocks_without_a_tool(capsys):
    """`run` now prints a human summary by default (JSON via --json)."""
    assert main(["run", "do a thing"]) == 0
    out = capsys.readouterr().out
    assert "Status:     blocked" in out
    assert "FINAL:" in out


def test_run_command_json_blocks_without_a_tool(capsys):
    assert main(["run", "do a thing", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["report"]["status"] == "blocked"
    assert payload["state"]["pending_actions"] == 1
    assert payload["resumed"] is False


def test_doctor_lists_available_model_providers(capsys):
    assert main(["doctor"]) == 0
    payload = _json_out(capsys)
    assert "echo" in payload["available_providers"]
    assert "openai_compatible" in payload["available_providers"]
    # no provider configured by default
    assert payload["model"] is None


def test_status_includes_model_field(capsys):
    assert main(["status"]) == 0
    assert _json_out(capsys)["model"] is None


def test_doctor_reports_configured_model_without_secret(tmp_path, capsys,
                                                        monkeypatch):
    monkeypatch.setenv("CLI_TEST_KEY", "sk-cli-secret")
    path = tmp_path / "agent.toml"
    path.write_text(
        '[model]\nprovider = "openai"\nname = "gpt-4o-mini"\n'
        'api_key_env = "CLI_TEST_KEY"\n', encoding="utf-8")
    assert main(["--config", str(path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "gpt-4o-mini" in out
    assert "sk-cli-secret" not in out


def test_bad_config_returns_error_code(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text('environment = "nonsense"\n', encoding="utf-8")
    assert main(["--config", str(bad), "status"]) == 2


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
