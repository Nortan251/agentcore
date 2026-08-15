from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest

from agentcore.integrations.base import (
    Integration,
    IntegrationRegistry,
    IntegrationStatus,
)
from agentcore.integrations.computer_control import (
    PLANNED_CAPABILITIES,
    ComputerControlAdapter,
)
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolContext, ToolResult, ToolSpec, tool_spec
from agentcore.tools.registry import ToolRegistry


class PingTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return tool_spec("ping", "Return pong.")

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success("pong")


class FakeIntegration(Integration):
    def __init__(self, available: bool = True) -> None:
        super().__init__("fake")
        self.available = available

    def connect(self) -> IntegrationStatus:
        self.status = (IntegrationStatus.CONNECTED if self.available
                       else IntegrationStatus.UNAVAILABLE)
        return self.status

    def provided_tools(self) -> Sequence[Tool]:
        return (PingTool(),) if self.status is IntegrationStatus.CONNECTED else ()


def test_connected_integration_publishes_its_tools():
    registry = ToolRegistry()
    integrations = IntegrationRegistry()
    integrations.add(FakeIntegration())
    infos = integrations.connect_all(registry)

    assert registry.names() == ["ping"]
    assert infos[0].status is IntegrationStatus.CONNECTED
    assert infos[0].tools == ["ping"]


def test_unavailable_integration_registers_nothing():
    registry = ToolRegistry()
    integrations = IntegrationRegistry()
    integrations.add(FakeIntegration(available=False))
    integrations.connect_all(registry)
    assert registry.names() == []


def test_duplicate_integration_rejected():
    integrations = IntegrationRegistry()
    integrations.add(FakeIntegration())
    with pytest.raises(ValueError):
        integrations.add(FakeIntegration())


def test_computer_control_is_declared_but_unimplemented():
    """Scope boundary: the adapter must expose no automation capability."""
    adapter = ComputerControlAdapter()
    assert adapter.connect() is IntegrationStatus.UNAVAILABLE
    assert adapter.provided_tools() == ()

    registry = ToolRegistry()
    assert adapter.register_into(registry) == 0
    assert registry.names() == []

    # the contract is documented for the future implementation
    assert Capability.INPUT_CONTROL in PLANNED_CAPABILITIES
    assert Capability.SCREEN_OBSERVE in PLANNED_CAPABILITIES


def test_health_report_is_serialisable():
    integrations = IntegrationRegistry()
    integrations.add(ComputerControlAdapter())
    health = integrations.health()
    assert health[0]["name"] == "computer_control"
    assert health[0]["tools"] == []
