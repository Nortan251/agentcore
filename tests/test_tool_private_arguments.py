"""Generic privacy marker is immutable metadata, never capability authority."""

from dataclasses import FrozenInstanceError

import pytest
from conftest import journal_for_tools

from agentcore.config.schema import AgentConfig, FeatureFlags, LoggingConfig, PermissionConfig
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.errors import ToolValidationError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.state.models import AgentState
from agentcore.tasks.models import Task, Subtask, TaskStatus, VerificationResult, VerificationStatus
from agentcore.tools.base import FunctionTool, ToolParameter, ToolResult, ToolSpec, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError


def test_tags_are_frozen_detached_host_metadata():
    tags = ["private_arguments"]
    spec = ToolSpec("read", "read", tags=tags)
    tags.clear()
    assert spec.tags == ("private_arguments",)
    with pytest.raises(FrozenInstanceError):
        spec.tags = ()


@pytest.mark.parametrize("argument", ["tags", "private_arguments", "capabilities", "read_only"])
def test_arguments_cannot_change_private_metadata_or_authority(argument):
    tool = FunctionTool(tool_spec("read", "read", parameters=(ToolParameter("text"),),
                                 tags=("private_arguments",)), lambda *_: pytest.fail("bad args must not run"))
    with pytest.raises(ToolValidationError):
        tool.validate_args({"text": "value", argument: False})
    assert tool.spec.tags == ("private_arguments",)


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("level", list(PermissionLevel))
def test_read_permission_result_truth_and_no_journal_unchanged(private, level):
    received = []
    tool = FunctionTool(tool_spec("read", "read", parameters=(ToolParameter("text"),),
        capabilities=(Capability.NET_READ,), tags=("private_arguments",) if private else ()),
        lambda args, ctx: received.append(dict(args)) or ToolResult.success({"value": args["text"]}))
    registry = ToolRegistry()
    registry.register(tool)
    journal = journal_for_tools([tool])
    executor = ToolExecutor(registry, PermissionPolicy(PermissionConfig(level=level)), **journal)
    result = executor.invoke("read", {"text": "SYNTHETIC_INPUT"})
    assert result.ok and result.result.output == {"value": "SYNTHETIC_INPUT"}
    assert received == [{"text": "SYNTHETIC_INPUT"}]
    assert journal["action_store"].list_actions() == []
    state = AgentState()
    state.record_tool_call(result)
    assert state.tool_calls[0]["result"]["output"] == result.result.output
    assert result.args["text"] == ("***" if private else "SYNTHETIC_INPUT")


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("level,allowed", [(PermissionLevel.OBSERVE, False), (PermissionLevel.PROPOSE, False),
                                          (PermissionLevel.CONFIRM, False), (PermissionLevel.AUTONOMOUS, True)])
def test_tag_does_not_change_write_permission_approval_or_journal(private, level, allowed):
    runs = []
    approvals = []
    class Gate:
        def request(self, *args):
            approvals.append(args)
            return False
    tool = FunctionTool(tool_spec("write", "synthetic write", capabilities=(Capability.FS_WRITE,), read_only=False,
                                 tags=("private_arguments",) if private else ()),
                        lambda *_: runs.append(1) or ToolResult.success("done"))
    registry = ToolRegistry()
    registry.register(tool)
    journal = journal_for_tools([tool])
    executor = ToolExecutor(registry, PermissionPolicy(PermissionConfig(level=level), approval_gate=Gate()), **journal)
    assert executor._mark_action_startup_healthy(journal["action_store"])
    result = executor.invoke("write", {})
    assert result.ok is allowed
    assert bool(runs) is allowed
    # Journaled writes require DurableApprovalAuthority, never the legacy
    # boolean gate. A privacy tag cannot substitute for that authority.
    assert approvals == []
    if level is PermissionLevel.CONFIRM:
        assert executor.policy.evaluate(Capability.FS_WRITE).outcome.value == "require_approval"
        assert result.result.metadata["kind"] == "approval_unavailable"
    assert tool.spec.read_only is False and tool.spec.capabilities == frozenset({Capability.FS_WRITE})
    assert result.action_id is not None if allowed else not runs


def test_private_marker_never_bypasses_registration_lease(monkeypatch):
    runs = []
    tool = FunctionTool(tool_spec("read", "read", tags=("private_arguments",)), lambda *_: runs.append("original"))
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(registry, PermissionPolicy())
    def checkpoint():
        with pytest.raises(ToolRegistryLeaseError):
            registry.register(FunctionTool(tool.spec, lambda *_: runs.append("replacement")), replace=True)
    result = executor.invoke("read", {}, checkpoint=checkpoint)
    assert result.ok and runs == ["original"]


def test_verifier_recovery_and_state_use_original_task_args_and_live_result(tmp_path):
    seen = []
    def function(args, context):
        seen.append(args["text"])
        if len(seen) == 1:
            return ToolResult.failure("transient", retryable=True)
        return ToolResult.success("verified result")
    tool = FunctionTool(tool_spec("read", "read", parameters=(ToolParameter("text"),), tags=("private_arguments",)), function)
    class Verifier:
        def verify(self, subtask, state):
            assert subtask.tool_args == {"text": "ORIGINAL_INPUT"}
            assert subtask.result == "verified result"
            return VerificationResult(status=VerificationStatus.PASSED, checked_by="test")
    config = AgentConfig(workspace_dir=str(tmp_path), logging=LoggingConfig(level="CRITICAL"),
                         features=FeatureFlags(recovery=True))
    agent = Agent(config, AgentDependencies(tools=[tool], verifier=Verifier()))
    task = Task(objective="Read synthetic input")
    task.add_subtask(Subtask(description="read", tool_name="read", tool_args={"text": "ORIGINAL_INPUT"}))
    report = agent.run_task(task)
    assert report.status is TaskStatus.COMPLETED
    assert report.retried == 1 and seen == ["ORIGINAL_INPUT", "ORIGINAL_INPUT"]
    assert all(item["args"] == {"text": "***"} for item in agent.state.tool_calls)
    assert task.subtasks[0].verification.status is VerificationStatus.PASSED
