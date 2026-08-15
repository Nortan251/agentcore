from agentcore.tools.base import Tool
from agentcore.tools.builtin.echo import EchoTool, WorkspaceInfoTool


def default_tools() -> list[Tool]:
    """Tools registered by default: read-only diagnostics only."""
    return [EchoTool(), WorkspaceInfoTool()]


__all__ = ["EchoTool", "WorkspaceInfoTool", "default_tools"]
