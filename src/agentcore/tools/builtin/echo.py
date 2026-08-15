"""Reference tools.

These exist to exercise and demonstrate the abstraction end to end; they are
deliberately trivial and side-effect free. Real capability tools land in later
tasks.
"""

from __future__ import annotations

from typing import Any, Mapping

from agentcore.permissions.levels import Capability
from agentcore.tools.base import (
    ParamType,
    Tool,
    ToolContext,
    ToolParameter,
    ToolResult,
    ToolSpec,
    tool_spec,
)


class EchoTool(Tool):
    """Returns its input. The canonical read-only smoke-test tool."""

    @property
    def spec(self) -> ToolSpec:
        return tool_spec(
            name="echo",
            description="Return the supplied message unchanged.",
            parameters=[
                ToolParameter("message", ParamType.STRING,
                              "Text to echo back."),
                ToolParameter("uppercase", ParamType.BOOLEAN,
                              "Uppercase the output.", required=False,
                              default=False),
            ],
            capabilities=[],
            read_only=True,
            tags=("diagnostic",),
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        message = str(args["message"])
        if args.get("uppercase"):
            message = message.upper()
        return ToolResult.success(message, length=len(message))


class WorkspaceInfoTool(Tool):
    """Reports the configured workspace directory.

    Declares ``FS_READ`` so the permission path is genuinely exercised by a
    capability-bearing tool, while remaining strictly read-only.
    """

    @property
    def spec(self) -> ToolSpec:
        return tool_spec(
            name="workspace_info",
            description="Report the agent's configured workspace directory.",
            parameters=[],
            capabilities=[Capability.FS_READ],
            read_only=True,
            tags=("diagnostic", "filesystem"),
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        from pathlib import Path

        path = Path(context.workspace_dir)
        return ToolResult.success(
            {"workspace_dir": str(path), "exists": path.exists()},
        )
