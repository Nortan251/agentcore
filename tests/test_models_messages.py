"""Tests for the vendor-neutral request/response abstraction."""

from __future__ import annotations

import pytest

from agentcore.models.messages import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)


def test_message_helpers_and_serialisation():
    assert Message.system("s").role is Role.SYSTEM
    assert Message.user("u").role is Role.USER
    assert Message.assistant("a").role is Role.ASSISTANT
    assert Message.user("hi").to_dict() == {"role": "user", "content": "hi"}
    assert Message(Role.TOOL, "x", name="t").to_dict()["name"] == "t"


def test_request_requires_messages():
    with pytest.raises(ValueError):
        ModelRequest(messages=[])


def test_from_prompt_builds_system_then_user():
    req = ModelRequest.from_prompt("do it", system="be terse")
    assert [m.role for m in req.messages] == [Role.SYSTEM, Role.USER]
    assert req.messages[0].content == "be terse"
    assert req.messages[1].content == "do it"


def test_from_prompt_without_system_has_only_user():
    req = ModelRequest.from_prompt("just this")
    assert [m.role for m in req.messages] == [Role.USER]


def test_from_prompt_maps_known_options():
    req = ModelRequest.from_prompt("p", model="m1", temperature=0.9,
                                   max_output_tokens=64, stop=("END",))
    assert req.model == "m1"
    assert req.temperature == 0.9
    assert req.max_output_tokens == 64
    assert req.stop == ("END",)


def test_from_prompt_preserves_unknown_options_in_metadata():
    """Unrecognised kwargs must not be silently dropped."""
    req = ModelRequest.from_prompt("p", seed=42)
    assert req.metadata["seed"] == 42


def test_usage_from_payload_is_defensive():
    assert Usage.from_payload(None) == Usage()
    assert Usage.from_payload("nonsense") == Usage()
    assert Usage.from_payload({"prompt_tokens": 3, "completion_tokens": 4}
                              ).total_tokens == 7
    assert Usage.from_payload({"prompt_tokens": -5}).prompt_tokens == 0
    assert Usage.from_payload({"total_tokens": 9}).total_tokens == 9


def test_response_excludes_raw_by_default():
    resp = ModelResponse(text="hi", raw={"big": "payload"})
    assert "raw" not in resp.to_dict()
    assert resp.to_dict(include_raw=True)["raw"] == {"big": "payload"}


def test_response_serialisation_shape():
    resp = ModelResponse(text="hi", model="m", provider="p",
                         finish_reason=FinishReason.STOP,
                         usage=Usage(1, 2, 3),
                         tool_calls=[ToolCall("id1", "t", {"a": 1})])
    payload = resp.to_dict()
    assert payload["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] == 3
    assert payload["tool_calls"] == [{"id": "id1", "name": "t",
                                      "arguments": {"a": 1}}]


def test_request_carries_future_planner_fields():
    """Shapes a future Planner needs must already exist (no logic yet)."""
    req = ModelRequest.from_prompt("p", tools=[{"name": "echo"}],
                                   response_format={"type": "json_object"})
    assert req.tools == [{"name": "echo"}]
    assert req.response_format == {"type": "json_object"}
    assert "tools" in req.to_dict()
