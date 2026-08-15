"""Tool executor: the one path through which tools are invoked.

Pipeline per invocation:

    lookup -> validate args -> permission check -> run -> normalise result

Centralising this means permission gating cannot be bypassed by a new tool, and
every invocation produces the same structured log events.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from agentcore.errors import (
    AgentError,
    PermissionDeniedError,
    ToolValidationError,
)
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.permissions.policy import Decision, PermissionPolicy
from agentcore.tools.base import ToolContext, ToolResult
from agentcore.tools.registry import ToolRegistry


@dataclass
class ToolInvocation:
    """Full record of one tool call, suitable for history and replay."""

    tool: str
    args: dict[str, Any]
    result: ToolResult
    duration_ms: float
    decision: Decision | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": self.args,
            "result": self.result.to_dict(),
            "duration_ms": round(self.duration_ms, 3),
            "decision": self.decision.to_dict() if self.decision else None,
        }


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        *,
        workspace_dir: str = ".",
        tool_settings: Mapping[str, Mapping[str, Any]] | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.workspace_dir = workspace_dir
        self._tool_settings = dict(tool_settings or {})
        self._log = logger or get_logger("tools.executor")

    def _context(self, tool_name: str) -> ToolContext:
        return ToolContext(
            config=dict(self._tool_settings.get(tool_name, {})),
            logger=self._log.bind(tool=tool_name),
            workspace_dir=self.workspace_dir,
        )

    def invoke(self, tool_name: str, args: Mapping[str, Any] | None = None,
               **context_meta: Any) -> ToolInvocation:
        """Run a tool and always return a record — never raises for tool failure.

        Raises only :class:`ToolNotFoundError` (programmer error) so that a
        mistyped tool name is loud, while runtime failures stay data.
        """
        args = dict(args or {})
        log = self._log.bind(tool=tool_name, **context_meta)
        tool = self.registry.get(tool_name)  # raises ToolNotFoundError
        log.info(Events.TOOL_INVOKED, args=args)
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        # 1. argument validation
        try:
            cleaned = tool.validate_args(args)
        except ToolValidationError as exc:
            log.warning(Events.TOOL_FAILED, reason="validation", error=exc.message,
                        details=exc.details)
            return ToolInvocation(tool_name, args,
                                  ToolResult.failure(exc.message,
                                                     kind="validation",
                                                     **exc.details),
                                  elapsed())

        # 2. permission gate
        decision = self.policy.check(tool.capabilities,
                                     description=tool.spec.description)
        log.debug(Events.PERMISSION_CHECKED, **decision.to_dict())
        if not decision.allowed:
            log.warning(Events.TOOL_DENIED, **decision.to_dict())
            denial = PermissionDeniedError(decision.reason,
                                           tool=tool_name,
                                           capability=str(decision.capability)
                                           if decision.capability else None)
            return ToolInvocation(tool_name, cleaned,
                                  ToolResult.failure(denial.message,
                                                     kind="permission_denied",
                                                     **denial.details),
                                  elapsed(), decision)

        # 3. execution
        try:
            result = tool.run(cleaned, self._context(tool_name))
            if not isinstance(result, ToolResult):
                result = ToolResult.success(result)
        except AgentError as exc:
            log.warning(Events.TOOL_FAILED, reason="agent_error",
                        error=exc.message, details=exc.details)
            result = ToolResult.failure(exc.message, kind=type(exc).__name__,
                                        retryable=exc.retryable, **exc.details)
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the agent
            log.exception(Events.TOOL_FAILED, reason="unhandled_exception",
                          error=str(exc))
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}",
                                        kind="unhandled_exception")

        duration = elapsed()
        if result.ok:
            log.info(Events.TOOL_SUCCEEDED, duration_ms=round(duration, 3))
        else:
            log.warning(Events.TOOL_FAILED, error=result.error,
                        duration_ms=round(duration, 3))
        return ToolInvocation(tool_name, cleaned, result, duration, decision)
