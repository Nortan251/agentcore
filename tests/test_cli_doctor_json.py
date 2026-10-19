"""Bounded doctor reports reuse the existing checks without running actions."""

import json
import socket

import pytest

from agentcore import cli
from agentcore.config.schema import AgentConfig, LoggingConfig, ModelConfig
from agentcore.integrations.base import IntegrationInfo, IntegrationStatus
from agentcore.models import factory


def _read(capsys):
    captured = capsys.readouterr()
    assert not captured.err
    assert len(captured.out.encode("utf-8")) < 2048
    payload = json.loads(captured.out)
    assert list(payload) == ["schema_version", "ok", "checks"]
    assert payload["schema_version"] == 1 and type(payload["ok"]) is bool
    assert len(payload["checks"]) == 5
    for check in payload["checks"]:
        assert list(check) == ["name", "outcome", "message"]
        assert type(check["message"]) is str and 0 < len(check["message"]) <= 64
    return payload


def _outcomes(payload):
    return {check["name"]: check["outcome"] for check in payload["checks"]}


def test_doctor_json_success_has_stable_schema(monkeypatch, capsys):
    def connected(self):
        self.status = IntegrationStatus.CONNECTED
        return self.status
    monkeypatch.setattr(cli.ComputerControlAdapter, "connect", connected)
    assert cli.main(["doctor", "--json"]) == 0
    first = capsys.readouterr()
    assert not first.err
    payload = json.loads(first.out)
    assert list(payload) == ["schema_version", "ok", "checks"]
    assert payload["schema_version"] == 1 and payload["ok"] is True
    assert [check["name"] for check in payload["checks"]] == [
        "configuration", "runtime", "computer_control", "model", "providers"]
    assert [check["outcome"] for check in payload["checks"]] == [
        "ok", "ok", "ok", "not_configured", "ok"]
    for check in payload["checks"]:
        assert list(check) == ["name", "outcome", "message"]
        assert type(check["message"]) is str and 0 < len(check["message"]) <= 64
    assert cli.main(["doctor", "--json"]) == 0
    assert capsys.readouterr().out == first.out


def test_unavailable_integration_is_safe_failure(capsys):
    assert cli.main(["doctor", "--json"]) == 1
    payload = _read(capsys)
    assert payload["ok"] is False
    assert _outcomes(payload) == {
        "configuration": "ok", "runtime": "ok", "computer_control": "unavailable",
        "model": "not_configured", "providers": "ok"}


@pytest.mark.parametrize("stage", ["configuration", "runtime", "connect", "health", "model", "providers", "shutdown"])
def test_failures_are_categorical_and_do_not_stringify_exceptions(stage, monkeypatch, capsys):
    class PrivateError(Exception):
        def __str__(self):
            pytest.fail("doctor attempted to render an exception")
    def failed(*args, **kwargs):
        raise PrivateError("secret-token /private/config " * 10000)
    targets = {
        "configuration": (cli, "load_config"), "runtime": (cli, "Agent"),
        "connect": (cli.ComputerControlAdapter, "connect"),
        "health": (cli.ComputerControlAdapter, "health"),
        "model": (cli.Agent, "model_info"), "providers": (factory, "available_providers"),
        "shutdown": (cli.Agent, "shutdown"),
    }
    monkeypatch.setattr(*targets[stage], failed)
    assert cli.main(["doctor", "--json"]) == (2 if stage == "configuration" else 1)
    payload = _read(capsys)
    name = {"connect": "computer_control", "health": "computer_control", "shutdown": "runtime"}.get(stage, stage)
    assert _outcomes(payload)[name] == "error"
    if stage in {"configuration", "runtime"}:
        assert _outcomes(payload)["providers"] == "not_checked"
    else:
        assert _outcomes(payload)["runtime"] == ("error" if stage == "shutdown" else "ok")
    assert "secret-token" not in json.dumps(payload)
    assert "/private" not in json.dumps(payload)


@pytest.mark.parametrize("health", [None, [], [None], [{"name": "private-name", "status": "connected"}],
    [{"name": "computer_control", "status": []}], [{"name": "computer_control", "status": "private-status"}]])
def test_malformed_integration_results_fail_safely(health, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_doctor_integrations", lambda agent: health)
    assert cli.main(["doctor", "--json"]) == 1
    payload = _read(capsys)
    assert _outcomes(payload)["computer_control"] == "error"
    assert "private-" not in json.dumps(payload)


@pytest.mark.parametrize("model", [[], {}, {"provider": "secret", "model": []}])
def test_malformed_model_description_fails_safely(model, monkeypatch, capsys):
    monkeypatch.setattr(cli.Agent, "model_info", lambda agent: model)
    assert cli.main(["doctor", "--json"]) == 1
    assert _outcomes(_read(capsys))["model"] == "error"


@pytest.mark.parametrize("providers", [None, [], (), [{"private": "value"}]])
def test_malformed_provider_catalogue_fails_safely(providers, monkeypatch, capsys):
    monkeypatch.setattr(factory, "available_providers", lambda: providers)
    assert cli.main(["doctor", "--json"]) == 1
    assert _outcomes(_read(capsys))["providers"] == "error"


@pytest.mark.parametrize("provider", ["echo", "unknown-private-provider"])
def test_private_configuration_and_details_never_enter_output(provider, tmp_path, monkeypatch, capsys):
    secret = "sk-private-value"
    private_name = "private-model-name" * 10000
    env_name = "PRIVATE_DOCTOR_KEY"
    monkeypatch.setenv(env_name, secret)
    log_path = tmp_path / "private-logs" / "diagnostic.log"
    config = AgentConfig(workspace_dir=str(tmp_path), agent_name="private-agent",
        model=ModelConfig(provider=provider, name=private_name, api_key_env=env_name,
                          base_url="https://private-endpoint.invalid/private"),
        logging=LoggingConfig(level="DEBUG", file_path=str(log_path)))
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.ComputerControlAdapter, "health", lambda self: IntegrationInfo(
        name="computer_control", status=IntegrationStatus.UNAVAILABLE,
        detail=secret * 10000, tools=[str(tmp_path)]))
    assert cli.main(["doctor", "--json"]) == 1
    captured = capsys.readouterr()
    assert not captured.err and len(captured.out.encode()) < 2048
    for value in (secret, private_name, env_name, str(tmp_path), "private-agent", "private-endpoint", provider):
        assert value not in captured.out
    payload = json.loads(captured.out)
    assert _outcomes(payload)["model"] == ("ok" if provider == "echo" else "unavailable")
    assert not log_path.parent.exists()


@pytest.mark.parametrize("content", [None, 'environment = "private-invalid-value"', 'model = ['])
def test_real_configuration_failures_have_safe_json_and_exit_two(content, tmp_path, capsys):
    path = tmp_path / "private-config.toml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert cli.main(["--config", str(path), "doctor", "--json"]) == 2
    payload = _read(capsys)
    assert _outcomes(payload)["configuration"] == "error"
    assert "private-" not in json.dumps(payload)


def test_legacy_report_is_unchanged_and_checks_are_reused(monkeypatch, capsys):
    calls = []
    for owner, name in ((cli, "_doctor_integrations"), (cli.Agent, "model_info"), (factory, "available_providers")):
        original = getattr(owner, name)
        def record(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(owner, name, record)
    assert cli.main(["doctor"]) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert list(legacy) == ["version", "environment", "permission_level", "tools",
                            "features_enabled", "integrations", "model", "available_providers"]
    assert legacy["permission_level"] == "observe" and legacy["model"] is None
    legacy_calls = calls[:]
    calls.clear()
    assert cli.main(["doctor", "--json"]) == 1
    _read(capsys)
    assert calls == legacy_calls


def test_legacy_connects_integrations_before_collecting_tool_names(monkeypatch, capsys):
    from types import SimpleNamespace
    connected = []
    def integrations(agent):
        connected.append(True)
        return []
    def names():
        assert connected
        return ["integration_tool"]
    monkeypatch.setattr(cli, "_doctor_integrations", integrations)
    monkeypatch.setattr(cli, "Agent", lambda config: SimpleNamespace(
        registry=SimpleNamespace(names=names), policy=SimpleNamespace(level="observe"),
        model_info=lambda: None, shutdown=lambda: None))
    assert cli.main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["tools"] == ["integration_tool"]


def test_json_diagnostics_do_not_execute_actions_or_add_io(monkeypatch, tmp_path, capsys):
    from agentcore.models.providers.echo import EchoProvider
    from agentcore.state.store import JsonFileStateStore
    from agentcore.tools.executor import ToolExecutor
    def forbidden(*args, **kwargs):
        pytest.fail("diagnostics attempted execution, network or durable state creation")
    monkeypatch.setattr(cli, "load_config", lambda path: AgentConfig(
        workspace_dir=str(tmp_path), model=ModelConfig(provider="echo")))
    for owner, name in ((cli.Agent, "execute"), (ToolExecutor, "invoke"),
                        (EchoProvider, "generate"), (socket, "create_connection"),
                        (JsonFileStateStore, "__init__")):
        monkeypatch.setattr(owner, name, forbidden)
    before = list(tmp_path.rglob("*"))
    assert cli.main(["doctor", "--json"]) == 1
    assert _outcomes(_read(capsys))["model"] == "ok"
    assert list(tmp_path.rglob("*")) == before
