from agentcore.tools.base import Tool
from agentcore.tools.builtin.echo import EchoTool, WorkspaceInfoTool
from agentcore.tools.builtin.read_file import ReadFileTool


def default_tools() -> list[Tool]:
    """Tools registered by default: read-only capabilities only.

    ``read_file`` is the first tool here that touches real user data. It is
    still read-only and still gated by ``FS_READ`` through the normal
    executor/permission pipeline, and every filesystem safety decision it
    makes belongs to :class:`~agentcore.fs.guard.WorkspaceGuard`.
    """
    return [EchoTool(), WorkspaceInfoTool(), ReadFileTool()]


__all__ = ["EchoTool", "WorkspaceInfoTool", "ReadFileTool", "default_tools"]
