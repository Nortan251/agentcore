"""Tool registry: name -> tool, filtered by configuration."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

from agentcore.config.schema import ToolConfig
from agentcore.errors import ToolError, ToolNotFoundError
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolSpec


class ToolRegistryLeaseError(ToolError):
    """A current tool binding could not be leased or mutated safely."""


class ToolRegistry:
    """Holds the tools available to an agent instance.

    The registry enforces the config allowlist at *registration* time, so a
    disabled tool is never even visible to the reasoning layer.

    Binding changes and execution leases linearize under a short metadata
    mutex.  A lease verifies one exact name/object binding and reference-counts
    it, then releases the mutex before yielding to tool or provider I/O.
    Replacement or removal of that name fails until every lease is released;
    unrelated names remain independently mutable.  Registered tool objects
    and their specs are trusted immutable configuration.  Dynamic changes must
    use ``register(..., replace=True)`` or ``unregister()`` so the lease fence
    can order them against active invocations.
    """

    def __init__(self, config: ToolConfig | None = None) -> None:
        self._owner_pid = os.getpid()
        self._config = config or ToolConfig()
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()
        self._execution_leases: dict[str, int] = {}
        self._log = get_logger("tools.registry")

    def _require_owner_process(self) -> None:
        # Check before touching an inherited mutex. A multi-threaded fork can
        # otherwise strand the child behind a lock held by a vanished thread.
        if self._owner_pid != os.getpid():
            raise ToolRegistryLeaseError(
                "tool registry belongs to another process",
                reason="ownership_mismatch",
            )

    def register(self, tool: Tool, *, replace: bool = False) -> bool:
        """Register a tool. Returns False if config disallows it."""
        self._require_owner_process()
        name = tool.spec.name
        if not name:
            raise ToolError("tool name must not be empty")
        if not self._config.is_allowed(name):
            self._log.info("tool.skipped", tool=name, reason="disabled by config")
            return False
        with self._lock:
            if not replace and name in self._tools:
                raise ToolError("tool already registered", tool=name)
            if replace and self._execution_leases.get(name, 0):
                raise ToolRegistryLeaseError(
                    "tool binding has active execution leases",
                    tool=name,
                    reason="execution_lease_active",
                )
            self._tools[name] = tool
        self._log.info(Events.TOOL_REGISTERED, tool=name,
                       capabilities=sorted(str(c) for c in tool.capabilities),
                       read_only=tool.spec.read_only)
        return True

    def register_all(self, tools: list[Tool], *, replace: bool = False) -> int:
        self._require_owner_process()
        return sum(1 for t in tools if self.register(t, replace=replace))

    def unregister(self, name: str) -> None:
        self._require_owner_process()
        with self._lock:
            if name not in self._tools:
                raise ToolNotFoundError("tool is not registered", tool=name)
            if self._execution_leases.get(name, 0):
                raise ToolRegistryLeaseError(
                    "tool binding has active execution leases",
                    tool=name,
                    reason="execution_lease_active",
                )
            del self._tools[name]

    def get(self, name: str) -> Tool:
        self._require_owner_process()
        with self._lock:
            try:
                return self._tools[name]
            except KeyError as exc:
                raise ToolNotFoundError(
                    "tool is not registered",
                    tool=name,
                    available=sorted(self._tools),
                ) from exc

    @contextmanager
    def execution_lease(
        self,
        name: str,
        expected_tool: Tool,
    ) -> Iterator[Tool]:
        """Fence one exact binding across a physical invocation.

        The metadata mutex is held only while verifying and updating the lease
        reference count.  It is deliberately released before control is
        yielded to permission, provider, filesystem, or other tool I/O.
        """
        self._require_owner_process()
        with self._lock:
            current = self._tools.get(name)
            if current is None:
                raise ToolRegistryLeaseError(
                    "tool is not registered for execution",
                    tool=name,
                    reason="registration_missing",
                )
            if current is not expected_tool:
                raise ToolRegistryLeaseError(
                    "tool binding changed before execution",
                    tool=name,
                    reason="ownership_mismatch",
                )
            self._execution_leases[name] = (
                self._execution_leases.get(name, 0) + 1
            )
        try:
            yield current
        finally:
            self._require_owner_process()
            with self._lock:
                remaining = self._execution_leases[name] - 1
                if remaining:
                    self._execution_leases[name] = remaining
                else:
                    del self._execution_leases[name]

    def has(self, name: str) -> bool:
        self._require_owner_process()
        with self._lock:
            return name in self._tools

    def names(self) -> list[str]:
        self._require_owner_process()
        with self._lock:
            return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        self._require_owner_process()
        with self._lock:
            tools = tuple(self._tools[name] for name in sorted(self._tools))
        return [tool.spec for tool in tools]

    def describe(self) -> list[dict[str, object]]:
        """Machine-readable catalogue; the future model layer consumes this."""
        return [s.to_dict() for s in self.specs()]

    def by_capability(self, capability: Capability) -> list[Tool]:
        self._require_owner_process()
        with self._lock:
            tools = tuple(self._tools[name] for name in sorted(self._tools))
        return [tool for tool in tools if capability in tool.capabilities]

    def read_only_tools(self) -> list[Tool]:
        self._require_owner_process()
        with self._lock:
            tools = tuple(self._tools[name] for name in sorted(self._tools))
        return [tool for tool in tools if tool.spec.read_only]

    def __len__(self) -> int:
        self._require_owner_process()
        with self._lock:
            return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        self._require_owner_process()
        with self._lock:
            tools = tuple(self._tools[name] for name in sorted(self._tools))
        return iter(tools)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        self._require_owner_process()
        with self._lock:
            return name in self._tools
