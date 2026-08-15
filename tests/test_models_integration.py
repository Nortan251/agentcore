"""Integration: the model layer reached through the existing Agent DI path.

Confirms Task 2 plugs into the Task 1 architecture without altering it.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

import pytest

from agentcore.config.schema import (
    AgentConfig,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies, build_agent
from agentcore.core.interfaces import ModelProvider
from agentcore.models.messages import ModelRequest, ModelResponse
from agentcore.models.providers.echo import EchoProvider
from agentcore.models.transport import HttpResponse
from agentcore.permissions.levels import PermissionLevel
from agentcore.tasks.models import TaskStatus

API_KEY_ENV = "INTEGRATION_MODEL_KEY"
SECRET = "sk-integration-secret-value"


def cfg(tmp_path, **model_kwargs: Any) -> AgentConfig:
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path),
        model=ModelConfig(**model_kwargs),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
    ).validate()


class RecordingTransport:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.requests: list[dict[str, Any]] = []

    def post_json(self, url: str, payload: Mapping[str, Any],
                  headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.requests.append({"url": url, "payload": dict(payload),
                              "headers": dict(headers)})
        return HttpResponse(status=200, body=json.dumps(self.body).encode())


# -- injection --------------------------------------------------------------

def test_injected_model_is_used(tmp_path):
    provider = EchoProvider(ModelConfig(provider="echo", name="m"))
    agent = Agent(cfg(tmp_path), AgentDependencies(model=provider))
    assert agent.model is provider
    assert agent.model.complete("hi") == "echo: hi"


def test_model_built_from_config_when_not_injected(tmp_path):
    agent = Agent(cfg(tmp_path, provider="echo", name="m"))
    assert isinstance(agent.model, EchoProvider)
    assert isinstance(agent.model, ModelProvider)


def test_injected_model_overrides_configuration(tmp_path):
    injected = EchoProvider(ModelConfig(provider="echo", name="injected"),
                            prefix="INJECTED: ")
    agent = Agent(cfg(tmp_path, provider="openai", name="gpt-4o"),
                  AgentDependencies(model=injected))
    assert agent.model is injected


def test_no_model_configured_leaves_seam_empty(tmp_path):
    """Default behaviour from Task 1 must be preserved exactly."""
    agent = Agent(cfg(tmp_path))
    assert agent.model is None
    assert agent.model_info() is None


def test_misconfigured_provider_does_not_prevent_startup(tmp_path):
    """A bad model config degrades to None rather than crashing the agent."""
    agent = Agent(cfg(tmp_path, provider="openai_compatible", name="none"))
    assert agent.model is None
    assert agent.status()["tools"]  # agent is otherwise fully functional


def test_unknown_provider_degrades_gracefully(tmp_path):
    agent = Agent(cfg(tmp_path, provider="not_a_real_provider", name="m"))
    assert agent.model is None


def test_build_agent_factory_accepts_model(tmp_path):
    provider = EchoProvider(ModelConfig(provider="echo", name="m"))
    agent = build_agent(cfg(tmp_path), model=provider)
    assert agent.model is provider


# -- status / reporting -----------------------------------------------------

def test_status_reports_model_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    agent = Agent(cfg(tmp_path, provider="openai", name="gpt-4o-mini",
                      api_key_env=API_KEY_ENV))
    status = agent.status()
    assert status["model"]["provider"] == "openai_compatible"
    assert status["model"]["model"] == "gpt-4o-mini"
    assert status["model"]["credential_configured"] is True
    assert SECRET not in json.dumps(status)


def test_status_shape_unchanged_for_existing_keys(tmp_path):
    status = Agent(cfg(tmp_path)).status()
    for key in ("run_id", "phase", "permission_level", "tools"):
        assert key in status


# -- end-to-end through a fake HTTP boundary --------------------------------

def test_end_to_end_call_through_agent(tmp_path, monkeypatch):
    """Config -> factory -> provider -> transport, with no real network."""
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    transport = RecordingTransport({
        "model": "gpt-4o-mini",
        "choices": [{"message": {"role": "assistant", "content": "42"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
    })
    from agentcore.models.factory import create_provider

    config = cfg(tmp_path, provider="openai", name="gpt-4o-mini",
                 api_key_env=API_KEY_ENV, temperature=0.5,
                 max_output_tokens=256)
    provider = create_provider(config.model, transport=transport)
    agent = Agent(config, AgentDependencies(model=provider))

    answer = agent.model.complete("ultimate question", system="be brief")
    assert answer == "42"

    sent = transport.requests[0]
    assert sent["url"] == "https://api.openai.com/v1/chat/completions"
    assert sent["payload"]["model"] == "gpt-4o-mini"
    assert sent["payload"]["temperature"] == 0.5
    assert sent["payload"]["max_tokens"] == 256
    assert sent["payload"]["messages"][0]["role"] == "system"


def test_agent_task_execution_unaffected_by_model(tmp_path):
    """Regression: the runner/tool path is untouched by this task."""
    from agentcore.tasks.models import Subtask, Task

    agent = Agent(cfg(tmp_path, provider="echo", name="m"))
    task = Task(objective="say hi")
    task.add_subtask(Subtask(description="echo", tool_name="echo",
                             tool_args={"message": "hi"}))
    report = agent.run_task(task)
    assert report.status is TaskStatus.COMPLETED
    assert task.subtasks[0].result == "hi"


def test_planner_still_passthrough(tmp_path):
    """Scope boundary: no planner behaviour was added in this task."""
    from agentcore.core.defaults import PassthroughPlanner

    agent = Agent(cfg(tmp_path, provider="echo", name="m"))
    assert isinstance(agent.planner, PassthroughPlanner)
    assert agent.execute("some objective").status is TaskStatus.BLOCKED


# -- logging safety ---------------------------------------------------------

def test_no_secret_in_agent_logs(tmp_path, monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    log_path = tmp_path / "agent.log"
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(provider="openai", name="gpt-4o-mini",
                          api_key_env=API_KEY_ENV),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="DEBUG", file_path=str(log_path)),
    ).validate()

    transport = RecordingTransport({
        "model": "gpt-4o-mini",
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
    })
    from agentcore.models.factory import create_provider

    agent = Agent(config, AgentDependencies(
        model=create_provider(config.model, transport=transport)))
    agent.model.complete("hello")
    agent.shutdown()

    import logging
    for handler in logging.getLogger("agentcore").handlers:
        handler.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert SECRET not in contents
    assert "model.request" in contents
    assert "model.response" in contents

    from agentcore.observability.logging import configure_logging
    configure_logging(LoggingConfig(level="CRITICAL"))
