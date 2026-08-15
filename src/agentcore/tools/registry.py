"""Tool registry: name -> tool, filtered by configuration."""

from __future__ import annotations

from typing import Iterator

from agentcore.config.schema import ToolConfig
from agentcore.errors import ToolError, ToolNotFoundError
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolSpec


class ToolRegistry:
    """Holds the tools available to an agent instance.

    The registry enforces the config allowlist at *registration* time, so a
    disabled tool is never even visible to the reasoning layer.
    """

    def __init__(self, config: ToolConfig | None = None) -> None:
        self._config = config or ToolConfig()
        self._tools: dict[str, Tool] = {}
        self._log = get_logger("tools.registry")

    def register(self, tool: Tool, *, replace: bool = False) -> bool:
        """Register a tool. Returns False if config disallows it."""
        name = tool.spec.name
        if not name:
            raise ToolError("tool name must not be empty")
        if not replace and name in self._tools:
            raise ToolError("tool already registered", tool=name)
        if not self._config.is_allowed(name):
            self._log.info("tool.skipped", tool=name, reason="disabled by config")
            return False
        self._tools[name] = tool
        self._log.info(Events.TOOL_REGISTERED, tool=name,
                       capabilities=sorted(str(c) for c in tool.capabilities),
                       read_only=tool.spec.read_only)
        return True

    def register_all(self, tools: list[Tool], *, replace: bool = False) -> int:
        return sum(1 for t in tools if self.register(t, replace=replace))

    def unregister(self, name: str) -> None:
        if self._tools.pop(name, None) is None:
            raise ToolNotFoundError("tool is not registered", tool=name)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError("tool is not registered", tool=name,
                                    available=self.names()) from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[n].spec for n in self.names()]

    def describe(self) -> list[dict[str, object]]:
        """Machine-readable catalogue; the future model layer consumes this."""
        return [s.to_dict() for s in self.specs()]

    def by_capability(self, capability: Capability) -> list[Tool]:
        return [self._tools[n] for n in self.names()
                if capability in self._tools[n].capabilities]

    def read_only_tools(self) -> list[Tool]:
        return [self._tools[n] for n in self.names() if self._tools[n].spec.read_only]

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return (self._tools[n] for n in self.names())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools
