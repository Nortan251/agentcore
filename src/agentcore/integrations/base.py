"""Integration adapters.

An *integration* is an external system the agent talks to (a local
computer-control service, a browser driver, a SaaS API). Each adapter owns its
connection lifecycle and publishes the tools it provides; the agent registers
those tools and otherwise knows nothing about the transport.

This is the seam the local Windows control layer will use: it runs as a
separate process on the user's machine, and a ``ComputerControlAdapter``
implemented in a later task will connect to it and expose its tools here. No
part of the core agent changes when that happens.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from agentcore.observability.logging import get_logger
from agentcore.tools.base import Tool
from agentcore.tools.registry import ToolRegistry


class IntegrationStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    UNAVAILABLE = "unavailable"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


@dataclass
class IntegrationInfo:
    name: str
    status: IntegrationStatus = IntegrationStatus.DISCONNECTED
    detail: str = ""
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value,
                "detail": self.detail, "tools": list(self.tools)}


class Integration(ABC):
    """Base class for external-system adapters."""

    def __init__(self, name: str, settings: dict[str, Any] | None = None) -> None:
        self.name = name
        self.settings = dict(settings or {})
        self.status = IntegrationStatus.DISCONNECTED
        self._log = get_logger("integrations", integration=name)

    @abstractmethod
    def connect(self) -> IntegrationStatus:
        """Establish the connection. Must not raise for expected unavailability."""

    def disconnect(self) -> None:
        self.status = IntegrationStatus.DISCONNECTED

    @abstractmethod
    def provided_tools(self) -> Sequence[Tool]:
        """Tools this integration exposes; empty when not connected."""

    def health(self) -> IntegrationInfo:
        return IntegrationInfo(
            name=self.name, status=self.status,
            tools=[t.spec.name for t in self.provided_tools()],
        )

    def register_into(self, registry: ToolRegistry, *, replace: bool = False) -> int:
        count = registry.register_all(list(self.provided_tools()), replace=replace)
        self._log.info("integration.registered", tools=count,
                       status=str(self.status))
        return count


class IntegrationRegistry:
    """Keeps track of adapters and wires their tools into a tool registry."""

    def __init__(self) -> None:
        self._items: dict[str, Integration] = {}
        self._log = get_logger("integrations.registry")

    def add(self, integration: Integration) -> None:
        if integration.name in self._items:
            raise ValueError(f"integration already added: {integration.name}")
        self._items[integration.name] = integration

    def get(self, name: str) -> Integration:
        return self._items[name]

    def names(self) -> list[str]:
        return sorted(self._items)

    def connect_all(self, registry: ToolRegistry) -> list[IntegrationInfo]:
        """Connect every adapter and register the tools of those that succeed."""
        results: list[IntegrationInfo] = []
        for name in self.names():
            integration = self._items[name]
            status = integration.connect()
            if status is IntegrationStatus.CONNECTED:
                integration.register_into(registry)
            else:
                self._log.warning("integration.unavailable", integration=name,
                                  status=str(status))
            results.append(integration.health())
        return results

    def health(self) -> list[dict[str, Any]]:
        return [self._items[n].health().to_dict() for n in self.names()]
