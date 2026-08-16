from __future__ import annotations

from typing import Any, Mapping

import pytest

from agentcore.config.schema import PermissionConfig, ToolConfig
from agentcore.errors import ToolError, ToolNotFoundError, ToolValidationError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.base import (
    FunctionTool,
    ParamType,
    Tool,
    ToolContext,
    ToolParameter,
    ToolResult,
    ToolSpec,
    tool_spec,
)
from agentcore.tools.builtin import EchoTool, WorkspaceInfoTool, default_tools
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry


class WritingTool(Tool):
    """Declares a mutating capability but performs no real side effect."""

    @property
    def spec(self) -> ToolSpec:
        return tool_spec("pretend_write", "Declares fs.write; does nothing.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("would have written")


class ExplodingTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return tool_spec("explode", "Raises an unexpected exception.")

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raise RuntimeError("boom")


# -- registry ---------------------------------------------------------------

def test_registry_register_and_lookup(registry):
    assert set(registry.names()) == {"echo", "workspace_info", "read_file",
                                     "list_directory"}
    assert registry.has("echo")
    assert "echo" in registry
    assert len(registry) == 4
    assert registry.get("echo").spec.name == "echo"


def test_registry_rejects_duplicates_unless_replacing(registry):
    with pytest.raises(ToolError):
        registry.register(EchoTool())
    assert registry.register(EchoTool(), replace=True)


def test_registry_unknown_tool_raises(registry):
    with pytest.raises(ToolNotFoundError):
        registry.get("nope")
    with pytest.raises(ToolNotFoundError):
        registry.unregister("nope")


def test_registry_honours_config_disable():
    reg = ToolRegistry(ToolConfig(disabled=("echo",)))
    reg.register_all(default_tools())
    assert reg.names() == ["list_directory", "read_file", "workspace_info"]


def test_registry_honours_config_allowlist():
    reg = ToolRegistry(ToolConfig(enabled=("echo",)))
    assert reg.register_all(default_tools()) == 1
    assert reg.names() == ["echo"]


def test_registry_queries(registry):
    assert [t.spec.name for t in registry.by_capability(Capability.FS_READ)] == [
        "list_directory", "read_file", "workspace_info"]
    assert len(registry.read_only_tools()) == 4
    catalogue = registry.describe()
    assert {c["name"] for c in catalogue} == {"echo", "workspace_info",
                                              "read_file", "list_directory"}
    assert catalogue[0]["parameters"][0]["name"] == "message"


# -- argument validation ----------------------------------------------------

def test_validate_args_applies_defaults():
    cleaned = EchoTool().validate_args({"message": "hi"})
    assert cleaned == {"message": "hi", "uppercase": False}


def test_validate_args_rejects_bad_input():
    tool = EchoTool()
    with pytest.raises(ToolValidationError):
        tool.validate_args({})                       # missing required
    with pytest.raises(ToolValidationError):
        tool.validate_args({"message": 5})           # wrong type
    with pytest.raises(ToolValidationError):
        tool.validate_args({"message": "a", "x": 1})  # unknown arg


def test_bool_does_not_satisfy_numeric_param():
    spec = tool_spec("n", "numeric",
                     parameters=[ToolParameter("count", ParamType.INTEGER)])
    tool = FunctionTool(spec, lambda a, c: a["count"])
    with pytest.raises(ToolValidationError):
        tool.validate_args({"count": True})
    assert tool.validate_args({"count": 3}) == {"count": 3}


def test_choices_are_enforced():
    spec = tool_spec("m", "mode", parameters=[
        ToolParameter("mode", ParamType.STRING, choices=("a", "b"))])
    tool = FunctionTool(spec, lambda a, c: a["mode"])
    assert tool.validate_args({"mode": "a"}) == {"mode": "a"}
    with pytest.raises(ToolValidationError):
        tool.validate_args({"mode": "z"})


# -- execution --------------------------------------------------------------

def test_executor_runs_tool(executor):
    inv = executor.invoke("echo", {"message": "hello"})
    assert inv.ok
    assert inv.result.output == "hello"
    assert inv.result.metadata["length"] == 5
    assert inv.duration_ms >= 0


def test_executor_applies_defaults_and_options(executor):
    inv = executor.invoke("echo", {"message": "hey", "uppercase": True})
    assert inv.result.output == "HEY"


def test_executor_returns_validation_failure_as_result(executor):
    inv = executor.invoke("echo", {})
    assert not inv.ok
    assert inv.result.metadata["kind"] == "validation"


def test_executor_raises_only_for_unknown_tool(executor):
    with pytest.raises(ToolNotFoundError):
        executor.invoke("does_not_exist")


def test_executor_catches_unhandled_exceptions(registry, tmp_path):
    registry.register(ExplodingTool())
    ex = ToolExecutor(registry,
                      PermissionPolicy(PermissionConfig(
                          level=PermissionLevel.AUTONOMOUS)),
                      workspace_dir=str(tmp_path))
    inv = ex.invoke("explode")
    assert not inv.ok
    assert "boom" in inv.result.error
    assert inv.result.metadata["kind"] == "unhandled_exception"


def test_executor_enforces_permissions(registry, tmp_path):
    registry.register(WritingTool())
    observe = ToolExecutor(registry,
                           PermissionPolicy(PermissionConfig(
                               level=PermissionLevel.OBSERVE)),
                           workspace_dir=str(tmp_path))
    inv = observe.invoke("pretend_write")
    assert not inv.ok
    assert inv.result.metadata["kind"] == "permission_denied"
    assert inv.decision is not None and not inv.decision.allowed

    autonomous = ToolExecutor(registry,
                              PermissionPolicy(PermissionConfig(
                                  level=PermissionLevel.AUTONOMOUS)),
                              workspace_dir=str(tmp_path))
    assert autonomous.invoke("pretend_write").ok


def test_executor_passes_context_settings(registry, tmp_path):
    seen: dict[str, Any] = {}

    def capture(args, context):
        seen["config"] = dict(context.config)
        seen["workspace"] = context.workspace_dir
        return "ok"

    registry.register(FunctionTool(tool_spec("capture", "capture ctx"), capture))
    ex = ToolExecutor(registry,
                      PermissionPolicy(PermissionConfig(
                          level=PermissionLevel.AUTONOMOUS)),
                      workspace_dir=str(tmp_path),
                      tool_settings={"capture": {"k": "v"}})
    assert ex.invoke("capture").ok
    assert seen["config"] == {"k": "v"}
    assert seen["workspace"] == str(tmp_path)


def test_workspace_info_tool_is_read_only(executor, tmp_path):
    inv = executor.invoke("workspace_info")
    assert inv.ok
    assert inv.result.output["workspace_dir"] == str(tmp_path)
    assert WorkspaceInfoTool().spec.read_only


def test_invocation_is_serialisable(executor):
    payload = executor.invoke("echo", {"message": "x"}).to_dict()
    assert payload["tool"] == "echo"
    assert payload["result"]["ok"] is True
