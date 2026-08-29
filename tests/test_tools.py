from __future__ import annotations

import os
import threading
from typing import Any, Mapping

import pytest

from agentcore.action_journal import (
    ActionAdapterRegistry,
    ActionAdapterSpec,
    ActionEffectClass,
    ActionJournalEligibility,
    EffectVerdict,
    InMemoryActionStore,
    NormalizedActionEffect,
    PreparedActionIntent,
)
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
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError


class WritingTool(Tool):
    """Declares a mutating capability but performs no real side effect."""

    @property
    def spec(self) -> ToolSpec:
        return tool_spec("pretend_write", "Declares fs.write; does nothing.",
                         capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("would have written")


class WritingAdapter:
    spec = ActionAdapterSpec(
        tool_name="pretend_write",
        tool_version="1.0.0",
        action_kind="test.pretend_write.v1",
        eligibility=ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
        effect_class=ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
    )

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="a" * 64,
            target_category="test.target",
        )

    def normalize_effect(self, result):
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_APPLIED,
            result_class="test.applied",
            summary="The test effect was confirmed.",
        )


class ExplodingTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return tool_spec("explode", "Raises an unexpected exception.")

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        raise RuntimeError("boom")


# -- registry ---------------------------------------------------------------

def test_registry_register_and_lookup(registry):
    assert set(registry.names()) == {"echo", "workspace_info", "read_file",
                                     "list_directory", "inspect"}
    assert registry.has("echo")
    assert "echo" in registry
    assert len(registry) == 5
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
    assert reg.names() == ["inspect", "list_directory", "read_file",
                           "workspace_info"]


def test_registry_honours_config_allowlist():
    reg = ToolRegistry(ToolConfig(enabled=("echo",)))
    assert reg.register_all(default_tools()) == 1
    assert reg.names() == ["echo"]


def test_registry_queries(registry):
    assert [t.spec.name for t in registry.by_capability(Capability.FS_READ)] == [
        "inspect", "list_directory", "read_file", "workspace_info"]
    assert len(registry.read_only_tools()) == 5
    catalogue = registry.describe()
    assert {c["name"] for c in catalogue} == {"echo", "workspace_info",
                                              "read_file", "list_directory",
                                              "inspect"}
    assert catalogue[0]["parameters"][0]["name"] == "message"


def test_registry_execution_lease_is_per_name_and_reference_counted():
    registry = ToolRegistry()
    original = EchoTool()
    replacement = EchoTool()
    unrelated = WorkspaceInfoTool()
    registry.register(original)
    unrelated_registered = threading.Event()
    unrelated_errors = []

    def register_unrelated():
        try:
            registry.register(unrelated)
        except BaseException as exc:  # pragma: no cover - asserted below
            unrelated_errors.append(exc)
        finally:
            unrelated_registered.set()

    with registry.execution_lease("echo", original):
        worker = threading.Thread(target=register_unrelated)
        worker.start()
        assert unrelated_registered.wait(1), (
            "registry metadata mutex was held while the lease was yielded")
        worker.join(1)
        assert unrelated_errors == []
        assert registry.get("workspace_info") is unrelated

        with registry.execution_lease("echo", original):
            with pytest.raises(ToolRegistryLeaseError) as replaced:
                registry.register(replacement, replace=True)
            with pytest.raises(ToolRegistryLeaseError) as removed:
                registry.unregister("echo")
            assert replaced.value.details["reason"] == "execution_lease_active"
            assert removed.value.details["reason"] == "execution_lease_active"
        with pytest.raises(ToolRegistryLeaseError):
            registry.register(replacement, replace=True)

    assert registry.register(replacement, replace=True)
    assert registry.get("echo") is replacement


def test_registry_execution_lease_rejects_missing_or_changed_binding():
    registry = ToolRegistry()
    original = EchoTool()
    replacement = EchoTool()
    registry.register(original)

    with pytest.raises(ToolRegistryLeaseError) as changed:
        with registry.execution_lease("echo", replacement):
            raise AssertionError("mismatched lease body ran")
    assert changed.value.details["reason"] == "ownership_mismatch"

    registry.unregister("echo")
    with pytest.raises(ToolRegistryLeaseError) as missing:
        with registry.execution_lease("echo", original):
            raise AssertionError("missing lease body ran")
    assert missing.value.details["reason"] == "registration_missing"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_inherited_registry_fails_before_touching_metadata_mutex():
    registry = ToolRegistry()
    registry.register(EchoTool())
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            registry.get("echo")
        except ToolRegistryLeaseError as exc:
            payload = exc.details.get("reason", "missing").encode("ascii")
        else:
            payload = b"unexpected_success"
        os.write(write_fd, payload)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 64) == b"ownership_mismatch"
        waited_pid, status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(read_fd)


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


def test_fast_path_releases_registry_mutex_but_fences_its_binding(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    invocations = []

    def blocking_read(args, context):
        entered.set()
        assert release.wait(5), "fast-path test tool was not released"
        return "original"

    original = FunctionTool(tool_spec("leased_read", "leased read"),
                            blocking_read)
    replacement = FunctionTool(
        tool_spec("leased_read", "leased read"),
        lambda args, context: "replacement",
    )
    unrelated = FunctionTool(
        tool_spec("unrelated_read", "unrelated read"),
        lambda args, context: "unrelated",
    )
    registry = ToolRegistry()
    registry.register(original)
    executor = ToolExecutor(
        registry,
        PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS)),
        workspace_dir=str(tmp_path),
    )
    worker = threading.Thread(
        target=lambda: invocations.append(executor.invoke("leased_read")))
    worker.start()
    assert entered.wait(5), "fast-path physical seam was not entered"
    try:
        # This succeeds from another thread of control while run() is blocked,
        # proving the short metadata mutex is not held across physical I/O.
        assert registry.register(unrelated)
        with pytest.raises(ToolRegistryLeaseError) as blocked:
            registry.register(replacement, replace=True)
        assert blocked.value.details["reason"] == "execution_lease_active"
        assert registry.get("leased_read") is original
    finally:
        release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert len(invocations) == 1 and invocations[0].result.output == "original"
    assert registry.register(replacement, replace=True)
    assert executor.invoke("leased_read").result.output == "replacement"


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
    tool = WritingTool()
    registry.register(tool)
    adapters = ActionAdapterRegistry()
    adapters.register(tool, WritingAdapter())
    store = InMemoryActionStore()
    observe = ToolExecutor(registry,
                           PermissionPolicy(PermissionConfig(
                               level=PermissionLevel.OBSERVE)),
                           workspace_dir=str(tmp_path),
                           action_store=store,
                           action_adapters=adapters)
    assert observe._mark_action_startup_healthy(store)
    inv = observe.invoke("pretend_write")
    assert not inv.ok
    assert inv.result.metadata["kind"] == "permission_denied"
    assert inv.decision is not None and not inv.decision.allowed

    autonomous = ToolExecutor(registry,
                              PermissionPolicy(PermissionConfig(
                                  level=PermissionLevel.AUTONOMOUS)),
                              workspace_dir=str(tmp_path),
                              action_store=store,
                              action_adapters=adapters)
    assert autonomous._mark_action_startup_healthy(store)
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
