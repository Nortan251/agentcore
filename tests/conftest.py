from __future__ import annotations

import pytest

from agentcore.config.schema import (
    AgentConfig,
    LoggingConfig,
    PermissionConfig,
    ToolConfig,
)
from agentcore.permissions.levels import PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.builtin import default_tools
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry


@pytest.fixture
def autonomous_config(tmp_path) -> AgentConfig:
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        tools=ToolConfig(),
    ).validate()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(default_tools())
    return reg


@pytest.fixture
def executor(registry, tmp_path) -> ToolExecutor:
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    return ToolExecutor(registry, policy, workspace_dir=str(tmp_path))
