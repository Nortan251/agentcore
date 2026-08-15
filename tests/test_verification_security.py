"""Security audit for the verification layer, over a real HTTP boundary.

Verification is the one layer that deliberately puts *execution context* —
including tool arguments — into a model prompt, so secret handling is tested
here against a real localhost server rather than a mock, to confirm what
actually leaves the process.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.observability.logging import configure_logging
from agentcore.permissions.levels import PermissionLevel
from agentcore.tasks.models import Subtask, Task, VerificationStatus
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-verification-secret-value"
PROVIDER_KEY = "sk-provider-credential"
ENV_VAR = "VERIFY_AUDIT_KEY"


class _State:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.verdict = {"verdict": "uncertain", "confidence": 0.2,
                        "reason": "cannot confirm the credential was stored"}


STATE = _State()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        STATE.requests.append({"body": body, "auth": self.headers.get(
            "Authorization")})
        content = json.dumps(STATE.verdict)
        out = json.dumps({
            "model": "stub",
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


class SecretTool(Tool):
    """Takes a credential-shaped argument, so redaction is exercised."""

    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec(
            "secret_tool", "Record a credential under a label.",
            parameters=[ToolParameter("api_key", ParamType.STRING),
                        ToolParameter("label", ParamType.STRING)])

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("recorded")


@pytest.fixture
def agent(server, tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_VAR, PROVIDER_KEY)
    STATE.requests.clear()
    log_path = tmp_path / "agent.log"
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(provider="openai_compatible", name="stub",
                          api_key_env=ENV_VAR, base_url=server,
                          max_retries=1),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="DEBUG", file_path=str(log_path)),
        features=FeatureFlags(verification=True),
    ).validate()
    tool = SecretTool()
    built = Agent(config, AgentDependencies(tools=[tool]))
    yield built, tool, log_path
    configure_logging(LoggingConfig(level="CRITICAL"))


def _task_with_secret(expected: str, requires: bool = True) -> Task:
    task = Task(objective="store a credential")
    sub = task.add_subtask(Subtask(
        description="record the credential", tool_name="secret_tool",
        tool_args={"api_key": SECRET, "label": "prod"}))
    task.metadata["plan"] = {"annotations": {
        sub.id: {"expected_result": expected,
                 "requires_verification": requires}}}
    return task


def test_secret_never_reaches_the_model(agent):
    """Tool args go into the verification prompt; secrets must be redacted."""
    built, tool, _ = agent
    built.run_task(_task_with_secret(
        "the credential was recorded in the secure vault"))

    assert STATE.requests, "the model should have been consulted"
    wire = json.dumps([r["body"] for r in STATE.requests])
    assert SECRET not in wire
    assert "***" in wire            # redaction actually applied
    assert "prod" in wire           # non-sensitive context preserved


def test_verification_prompt_actually_sent(agent):
    built, _, _ = agent
    built.run_task(_task_with_secret("the credential was recorded in the secure vault"))
    wire = json.dumps([r["body"] for r in STATE.requests])
    assert "EXPECTED RESULT" in wire
    assert "verification component" in wire


def test_provider_credential_not_in_prompt_body(agent):
    built, _, _ = agent
    built.run_task(_task_with_secret("the credential was recorded in the secure vault"))
    bodies = json.dumps([r["body"] for r in STATE.requests])
    assert PROVIDER_KEY not in bodies
    # it belongs in the auth header only
    assert STATE.requests[0]["auth"] == f"Bearer {PROVIDER_KEY}"


def test_no_secret_in_logs(agent):
    built, _, log_path = agent
    built.run_task(_task_with_secret("the credential was recorded in the secure vault"))
    built.shutdown()
    for handler in logging.getLogger("agentcore").handlers:
        handler.flush()

    contents = log_path.read_text(encoding="utf-8")
    assert SECRET not in contents
    assert PROVIDER_KEY not in contents
    assert "verification.completed" in contents


def test_uncertain_model_verdict_does_not_pass(agent):
    built, _, _ = agent
    task = _task_with_secret("the credential was recorded in the secure vault")
    built.run_task(task)
    assert task.subtasks[0].verification.status is VerificationStatus.UNVERIFIED


def test_verification_does_not_rerun_the_tool(agent):
    built, tool, _ = agent
    built.run_task(_task_with_secret("the credential was recorded in the secure vault"))
    assert tool.runs == 1


def test_model_claiming_success_still_requires_confidence(agent):
    """A confident-sounding but low-confidence pass is not accepted."""
    built, _, _ = agent
    STATE.verdict = {"verdict": "pass", "confidence": 0.1,
                     "reason": "it definitely worked"}
    task = _task_with_secret("the credential was recorded in the secure vault")
    built.run_task(task)
    assert task.subtasks[0].verification.status is VerificationStatus.UNVERIFIED
    STATE.verdict = {"verdict": "uncertain", "confidence": 0.2,
                     "reason": "cannot confirm the credential was stored"}


def test_malformed_model_reply_cannot_forge_success(agent, monkeypatch):
    built, _, _ = agent
    STATE.verdict = {"totally": "wrong shape"}
    task = _task_with_secret("the credential was recorded in the secure vault")
    built.run_task(task)
    assert task.subtasks[0].verification.status is not VerificationStatus.PASSED
    STATE.verdict = {"verdict": "uncertain", "confidence": 0.2,
                     "reason": "cannot confirm the credential was stored"}
