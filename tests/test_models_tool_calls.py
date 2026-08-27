"""Provider-side tool-call parsing, and the real path from HTTP to execution.

The provider is a *normalisation* layer: it turns the vendor's nested
``{"function": {...}}`` wire shape into the neutral ``ToolCall``, and does
nothing else. The properties under test are mostly negative — it must not
guess, not fabricate, and not execute.
"""

from __future__ import annotations

import ast
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import journal_for_tools

from agentcore.config.schema import (
    AgentConfig,
    ConversationConfig,
    FeatureFlags,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.models.errors import ModelResponseError
from agentcore.models.messages import FinishReason, ModelRequest, Role, ToolCall
from agentcore.models.providers.openai_compatible import OpenAICompatibleProvider
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.tools.base import (
    ParamType,
    Tool,
    ToolParameter,
    ToolResult,
    tool_spec,
)

PROVIDER_SOURCE = pathlib.Path(
    "src/agentcore/models/providers/openai_compatible.py")


def provider(**config):
    settings = {"name": "test-model", "base_url": "http://localhost:1",
                "api_key_env": "TEST_KEY"}
    settings.update(config)
    return OpenAICompatibleProvider(ModelConfig(**settings))


def response(message, *, finish="tool_calls", model="test-model"):
    return {"model": model,
            "choices": [{"message": message, "finish_reason": finish}]}


def function_call(name="echo_test", arguments='{"text": "hello"}',
                  call_id="call_123", include_id=True):
    entry = {"type": "function",
             "function": {"name": name, "arguments": arguments}}
    if include_id:
        entry["id"] = call_id
    return entry


# ===========================================================================
# The standard shape
# ===========================================================================

def test_single_tool_call_is_normalised():
    parsed = provider().parse_response(
        response({"role": "assistant", "content": None,
                  "tool_calls": [function_call()]}))
    assert parsed.tool_calls == [
        ToolCall(id="call_123", name="echo_test",
                 arguments={"text": "hello"})]


def test_supplied_call_id_is_preserved_exactly():
    parsed = provider().parse_response(
        response({"content": None,
                  "tool_calls": [function_call(call_id="call_ABC-999")]}))
    assert parsed.tool_calls[0].id == "call_ABC-999"


def test_finish_reason_is_mapped():
    parsed = provider().parse_response(
        response({"content": None, "tool_calls": [function_call()]}))
    assert parsed.finish_reason is FinishReason.TOOL_CALLS


def test_arguments_may_arrive_as_an_object():
    """Some compatible backends send a mapping rather than JSON text."""
    parsed = provider().parse_response(
        response({"content": None,
                  "tool_calls": [function_call(arguments={"text": "hi"})]}))
    assert parsed.tool_calls[0].arguments == {"text": "hi"}


def test_arguments_mapping_is_copied():
    payload = {"text": "hi"}
    parsed = provider().parse_response(
        response({"content": None,
                  "tool_calls": [function_call(arguments=payload)]}))
    parsed.tool_calls[0].arguments["text"] = "mutated"
    assert payload == {"text": "hi"}


# ===========================================================================
# Multiple calls
# ===========================================================================

def test_multiple_tool_calls_preserve_order_ids_and_arguments():
    parsed = provider().parse_response(response({
        "content": None,
        "tool_calls": [
            function_call(name="first", arguments='{"n": 1}', call_id="a"),
            function_call(name="second", arguments='{"n": 2}', call_id="b"),
            function_call(name="third", arguments='{"n": 3}', call_id="c"),
        ]}))
    assert [c.id for c in parsed.tool_calls] == ["a", "b", "c"]
    assert [c.name for c in parsed.tool_calls] == ["first", "second", "third"]
    assert [c.arguments["n"] for c in parsed.tool_calls] == [1, 2, 3]


def test_duplicate_tool_names_stay_distinct():
    parsed = provider().parse_response(response({
        "content": None,
        "tool_calls": [function_call(call_id="a", arguments='{"text": "x"}'),
                       function_call(call_id="b", arguments='{"text": "y"}')]}))
    assert [c.id for c in parsed.tool_calls] == ["a", "b"]


# ===========================================================================
# Text and tool calls
# ===========================================================================

def test_text_only_response_is_unchanged():
    parsed = provider().parse_response(
        response({"role": "assistant", "content": "Just talking."},
                 finish="stop"))
    assert parsed.text == "Just talking."
    assert parsed.tool_calls == []


def test_tool_calls_with_null_content():
    parsed = provider().parse_response(
        response({"content": None, "tool_calls": [function_call()]}))
    assert parsed.text == ""
    assert len(parsed.tool_calls) == 1


def test_tool_calls_with_absent_content():
    parsed = provider().parse_response(
        response({"tool_calls": [function_call()]}))
    assert parsed.text == ""
    assert len(parsed.tool_calls) == 1


def test_text_and_tool_calls_both_survive():
    parsed = provider().parse_response(response({
        "content": "Let me check that for you.",
        "tool_calls": [function_call()]}))
    assert parsed.text == "Let me check that for you."
    assert len(parsed.tool_calls) == 1


def test_empty_content_without_tool_calls_still_fails():
    """Pre-existing behaviour: nothing usable came back."""
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({"content": ""}, finish="stop"))
    assert "empty content" in exc.value.message


# ===========================================================================
# Missing call id
# ===========================================================================

def test_missing_id_is_generated():
    parsed = provider().parse_response(
        response({"content": None,
                  "tool_calls": [function_call(include_id=False)]}))
    generated = parsed.tool_calls[0].id
    assert generated and generated.startswith("call_")


def test_generated_ids_are_unique_across_responses():
    payload = response({"content": None,
                        "tool_calls": [function_call(include_id=False)]})
    first = provider().parse_response(payload).tool_calls[0].id
    second = provider().parse_response(payload).tool_calls[0].id
    assert first != second


def test_generated_ids_are_unique_within_one_response():
    parsed = provider().parse_response(response({
        "content": None,
        "tool_calls": [function_call(include_id=False),
                       function_call(include_id=False)]}))
    assert parsed.tool_calls[0].id != parsed.tool_calls[1].id


def test_generated_id_is_not_derived_from_the_call():
    """Two identical calls must not collapse onto one id."""
    parsed = provider().parse_response(response({
        "content": None,
        "tool_calls": [function_call(include_id=False, arguments='{"t": 1}'),
                       function_call(include_id=False, arguments='{"t": 1}')]}))
    first, second = parsed.tool_calls
    assert first.id != second.id
    assert "echo_test" not in first.id


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_ids_are_replaced(blank):
    entry = function_call()
    entry["id"] = blank
    parsed = provider().parse_response(
        response({"content": None, "tool_calls": [entry]}))
    assert parsed.tool_calls[0].id.startswith("call_")


def test_generated_id_uses_the_project_utility(monkeypatch):
    import agentcore.models.providers.openai_compatible as module

    seen = []

    def fake_new_id(prefix=""):
        seen.append(prefix)
        return f"{prefix}_stub"

    monkeypatch.setattr(module, "new_id", fake_new_id)
    parsed = provider().parse_response(
        response({"content": None,
                  "tool_calls": [function_call(include_id=False)]}))
    assert seen == ["call"]
    assert parsed.tool_calls[0].id == "call_stub"


# ===========================================================================
# Arguments
# ===========================================================================

@pytest.mark.parametrize("arguments", ["{}", "", None])
def test_absent_or_empty_arguments_become_an_empty_mapping(arguments):
    parsed = provider().parse_response(response({
        "content": None,
        "tool_calls": [function_call(arguments=arguments)]}))
    assert parsed.tool_calls[0].arguments == {}


def test_invalid_json_arguments_raise():
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({
            "content": None,
            "tool_calls": [function_call(arguments="{not json")]}))
    assert "not valid JSON" in exc.value.message
    assert exc.value.details["tool"] == "echo_test"


def test_invalid_json_never_produces_a_tool_call():
    """No guessed, partial or placeholder arguments — the whole response
    fails, so the conversation layer cannot receive a fabricated call."""
    with pytest.raises(ModelResponseError):
        provider().parse_response(response({
            "content": "here you go",
            "tool_calls": [function_call(),
                           function_call(call_id="c2",
                                         arguments="{broken")]}))


def test_json_scalar_arguments_raise():
    for arguments in ('"a string"', "42", "[1, 2]", "null", "true"):
        with pytest.raises(ModelResponseError) as exc:
            provider().parse_response(response({
                "content": None,
                "tool_calls": [function_call(arguments=arguments)]}))
        assert "not a JSON object" in exc.value.message


def test_unexpected_argument_type_raises():
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({
            "content": None, "tool_calls": [function_call(arguments=42)]}))
    assert "unexpected type" in exc.value.message


def test_argument_errors_never_echo_the_raw_text():
    """Arguments are model-generated and could contain anything."""
    secret = "sk-provider-parse-must-not-leak"
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({
            "content": None,
            "tool_calls": [function_call(arguments=f'{{"k": "{secret}"')]}))
    blob = json.dumps(exc.value.to_dict())
    assert secret not in blob


# ===========================================================================
# Malformed shapes
# ===========================================================================

def test_tool_calls_not_a_list_raises():
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({"content": None,
                                            "tool_calls": {"id": "c"}}))
    assert "not a list" in exc.value.message


def test_tool_calls_none_is_treated_as_absent():
    parsed = provider().parse_response(
        response({"content": "hi", "tool_calls": None}, finish="stop"))
    assert parsed.tool_calls == []


def test_empty_tool_calls_list_is_not_a_tool_response():
    parsed = provider().parse_response(
        response({"content": "hi", "tool_calls": []}, finish="stop"))
    assert parsed.tool_calls == []


@pytest.mark.parametrize("entry,fragment", [
    ("garbage", "not an object"),
    (["nested"], "not an object"),
    ({"id": "c"}, "no function object"),
    ({"id": "c", "function": "text"}, "no function object"),
    ({"id": "c", "function": {}}, "no usable function name"),
    ({"id": "c", "function": {"name": ""}}, "no usable function name"),
    ({"id": "c", "function": {"name": "   "}}, "no usable function name"),
    ({"id": "c", "function": {"name": 42}}, "no usable function name"),
])
def test_malformed_entries_fail_loudly(entry, fragment):
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({"content": None,
                                            "tool_calls": [entry]}))
    assert fragment in exc.value.message


def test_errors_carry_the_offending_index():
    with pytest.raises(ModelResponseError) as exc:
        provider().parse_response(response({
            "content": None,
            "tool_calls": [function_call(), {"id": "c", "function": {}}]}))
    assert exc.value.details["index"] == 1


def test_existing_malformed_response_handling_is_unchanged():
    for payload, fragment in [
            ("not a dict", "not a JSON object"),
            ({}, "no choices"),
            ({"choices": []}, "no choices"),
            ({"choices": ["x"]}, "not an object"),
            ({"choices": [{}]}, "no message"),
    ]:
        with pytest.raises(ModelResponseError) as exc:
            provider().parse_response(payload)
        assert fragment in exc.value.message


# ===========================================================================
# The provider executes nothing
# ===========================================================================

def test_provider_never_imports_the_tool_layer():
    tree = ast.parse(PROVIDER_SOURCE.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("agentcore.tools.registry", "agentcore.tools.executor",
                      "agentcore.tools.base", "agentcore.permissions.policy"):
        assert forbidden not in imported


def test_provider_calls_no_execution_methods():
    tree = ast.parse(PROVIDER_SOURCE.read_text())
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    for forbidden in ("run", "invoke", "check", "evaluate",
                      "add_pending_action"):
        assert forbidden not in called


def test_provider_uses_no_eval():
    tree = ast.parse(PROVIDER_SOURCE.read_text())
    names = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "eval" not in names and "exec" not in names
    assert "literal_eval" not in PROVIDER_SOURCE.read_text()


def test_parsing_a_dangerous_looking_call_executes_nothing():
    """Parsing is pure data handling, whatever the tool claims to be."""
    parsed = provider().parse_response(response({
        "content": None,
        "tool_calls": [function_call(name="delete_everything",
                                     arguments='{"path": "/"}')]}))
    assert parsed.tool_calls[0].name == "delete_everything"
    assert parsed.tool_calls[0].arguments == {"path": "/"}


# ===========================================================================
# Localhost end-to-end: real HTTP, real provider, real executor
# ===========================================================================

class EchoTest(Tool):
    """The acceptance tool: `echo_test(text) -> "ECHO: <text>"`."""

    def __init__(self) -> None:
        self.runs: list[dict] = []

    @property
    def spec(self):
        return tool_spec(
            "echo_test", "Echo the given text back.",
            parameters=[ToolParameter("text", ParamType.STRING,
                                      required=True)],
            capabilities=[Capability.FS_READ], read_only=True)

    def run(self, args, context):
        self.runs.append(dict(args))
        return ToolResult.success(f"ECHO: {args['text']}")


class MutatingTool(Tool):
    def __init__(self) -> None:
        self.runs = 0

    @property
    def spec(self):
        return tool_spec("danger_write", "Mutating.", parameters=[],
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.runs += 1
        return ToolResult.success("wrote")


class StubServer:
    """A localhost OpenAI-compatible endpoint returning scripted bodies."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - required name
                length = int(self.headers.get("Content-Length", 0))
                stub.requests.append(
                    json.loads(self.rfile.read(length) or b"{}"))
                body = (stub.bodies.pop(0) if len(stub.bodies) > 1
                        else stub.bodies[0])
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the default stderr log
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port

    def __enter__(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"


def agent_for(tmp_path, stub, tools, *, level=PermissionLevel.AUTONOMOUS,
              monkeypatch=None):
    config = AgentConfig(
        workspace_dir=str(tmp_path),
        model=ModelConfig(provider="openai_compatible", name="test-model",
                          base_url=stub.base_url, api_key_env="TEST_KEY",
                          max_retries=1),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        memory=MemoryConfig(),
        conversation=ConversationConfig(tools_enabled=True),
        features=FeatureFlags(memory=True)).validate()
    tools = list(tools)
    return Agent(config, AgentDependencies(
        tools=tools, **journal_for_tools(tools)))


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "test-secret-value")


def tool_body(name="echo_test", arguments='{"text": "hello"}',
              call_id="call_123"):
    return {"model": "test-model", "choices": [{
        "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": call_id, "type": "function",
                                    "function": {"name": name,
                                                 "arguments": arguments}}]},
        "finish_reason": "tool_calls"}]}


def text_body(text):
    return {"model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


def test_end_to_end_real_provider_drives_the_tool_loop(tmp_path):
    """HTTP -> parse -> validate -> permit -> execute -> result -> answer."""
    echo = EchoTest()
    with StubServer([tool_body(),
                     text_body("The tool returned hello.")]) as stub:
        agent = agent_for(tmp_path, stub, [echo])
        result = agent.conversation().send("Please echo hello")

    # 1-2. the provider parsed a real HTTP tool call. More than two requests
    # is legitimate: memory capture also consults the model. Chat turns are
    # the ones carrying the tool catalogue.
    chat_requests = [r for r in stub.requests if r.get("tools")]
    assert chat_requests, "the catalogue was sent"
    assert len(stub.requests) >= 2
    # 3-5. exactly one execution of the right tool with the right arguments
    assert echo.runs == [{"text": "hello"}]
    outcome = result.tool_outcomes[0]
    assert outcome.status == "ok" and outcome.executed
    # the provider-supplied call id survived end to end
    assert outcome.call.id == "call_123"
    # 6-7. the Role.TOOL message went back through the provider
    tool_messages = [m for m in result.session.messages
                     if m.role is Role.TOOL]
    assert len(tool_messages) == 1
    assert "ECHO: hello" in tool_messages[0].content
    assert any("ECHO: hello" in json.dumps(r) for r in stub.requests), \
        "the tool result was sent back to the provider"
    # 8. the final assistant message
    assert result.reply.content == "The tool returned hello."
    assert result.session.messages[-1].role is Role.ASSISTANT
    # 9. nothing durable was written
    assert agent.memory.count(include_expired=True) == 0


def test_end_to_end_malformed_arguments_fail_safely(tmp_path):
    echo = EchoTest()
    with StubServer([tool_body(arguments="{not json")]) as stub:
        agent = agent_for(tmp_path, stub, [echo])
        result = agent.conversation().send("echo something")

    # The provider refused the response, so the turn failed honestly.
    assert result.ok is False
    assert result.reply is None
    assert echo.runs == []


def test_end_to_end_unknown_tool_never_executes(tmp_path):
    echo = EchoTest()
    with StubServer([tool_body(name="not_registered"),
                     text_body("I could not do that.")]) as stub:
        agent = agent_for(tmp_path, stub, [echo])
        result = agent.conversation().send("do something")

    assert echo.runs == []
    outcome = result.tool_outcomes[0]
    assert outcome.status == "failed" and outcome.executed is False
    assert "no tool named 'not_registered'" in outcome.content
    assert result.reply.content == "I could not do that."


def test_end_to_end_denied_tool_never_executes(tmp_path):
    mutating = MutatingTool()
    with StubServer([tool_body(name="danger_write", arguments="{}"),
                     text_body("That is not permitted.")]) as stub:
        agent = agent_for(tmp_path, stub, [mutating],
                          level=PermissionLevel.OBSERVE)
        result = agent.conversation().send("write something")

    assert mutating.runs == 0
    assert result.tool_outcomes[0].status == "denied"


def test_end_to_end_approval_required_escalates(tmp_path):
    mutating = MutatingTool()
    with StubServer([tool_body(name="danger_write", arguments="{}"),
                     text_body("I need your approval.")]) as stub:
        agent = agent_for(tmp_path, stub, [mutating],
                          level=PermissionLevel.CONFIRM)
        result = agent.conversation().send("write something")

    assert mutating.runs == 0
    assert result.tool_outcomes[0].status == "approval_required"
    assert len(agent.state.pending_actions) == 1


def test_end_to_end_text_only_still_works(tmp_path):
    with StubServer([text_body("Just an answer.")]) as stub:
        agent = agent_for(tmp_path, stub, [EchoTest()])
        result = agent.conversation().send("hello")
    assert result.reply.content == "Just an answer."
    assert result.tool_rounds == []


def test_end_to_end_credentials_never_leave_the_transport(tmp_path, capsys):
    echo = EchoTest()
    with StubServer([tool_body(),
                     text_body("done")]) as stub:
        agent = agent_for(tmp_path, stub, [echo])
        result = agent.conversation().send("echo hello")

    blob = json.dumps([m.to_dict() for m in result.session.messages])
    assert "test-secret-value" not in blob
    assert "test-secret-value" not in capsys.readouterr().out
