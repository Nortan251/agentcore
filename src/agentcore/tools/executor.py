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
from agentcore.fs.sensitive import DEFAULT_SENSITIVE_POLICY
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.permissions.policy import Decision, PermissionPolicy
from agentcore.tools.base import ToolContext, ToolResult
from agentcore.tools.registry import ToolRegistry


#: Placeholder used when a value is withheld from a log line.
_REDACTED = "***"

#: Rejection categories whose *message* names a sensitive file. The guard's
#: reason string embeds the filename (``"'.env' is a known credential
#: file"``), so the raw error must not reach the structured log; the
#: categorical reason is enough for operations.
_SENSITIVE_FAILURE_REASONS = frozenset({"sensitive", "sensitive_target"})


def _safe_args_for_log(args: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of tool args with sensitive-path-shaped values redacted.

    The executor is the single point where an invocation is logged, so this is
    where a model-supplied path such as ``.env`` or ``id_rsa`` must be kept out
    of the structured log. It reuses the authoritative sensitive-path policy
    (the same one the filesystem guard enforces) rather than a second,
    divergent list, so the log can never disclose a path that a read would
    refuse. Ordinary paths (``notes.txt``, ``src/app.py``) are left intact.
    """
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and DEFAULT_SENSITIVE_POLICY.is_sensitive(value):
            safe[key] = _REDACTED
        else:
            safe[key] = value
    return safe


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
        workspace: Any = None,
        tool_settings: Mapping[str, Mapping[str, Any]] | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        # A guard, when supplied, is authoritative for the workspace root: it
        # has already canonicalised it. This keeps one definition of "the
        # workspace" rather than letting the executor hold a second opinion.
        self.workspace = workspace
        self.workspace_dir = (str(workspace.root) if workspace is not None
                              else workspace_dir)
        self._tool_settings = dict(tool_settings or {})
        self._log = logger or get_logger("tools.executor")

    def _context(self, tool_name: str) -> ToolContext:
        return ToolContext(
            config=dict(self._tool_settings.get(tool_name, {})),
            logger=self._log.bind(tool=tool_name),
            workspace_dir=self.workspace_dir,
            workspace=self.workspace,
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
        # Raw args are logged, but a sensitive filesystem path supplied as an
        # argument (e.g. ``{"path": ".env"}``) must not reach the log.
        log.info(Events.TOOL_INVOKED, args=_safe_args_for_log(args))
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
            # A sensitive rejection's error message names the protected file;
            # log only the categorical reason so the filename never reaches
            # the structured log (Task 39 F-03). Everything else keeps the
            # full message for debugging.
            reason = result.metadata.get("reason") if result.metadata else None
            if reason in _SENSITIVE_FAILURE_REASONS:
                log.warning(Events.TOOL_FAILED, reason=reason,
                            duration_ms=round(duration, 3))
            else:
                log.warning(Events.TOOL_FAILED, error=result.error,
                            duration_ms=round(duration, 3))
        return ToolInvocation(tool_name, cleaned, result, duration, decision)
