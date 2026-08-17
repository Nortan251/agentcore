from agentcore.tools.base import Tool
from agentcore.tools.builtin.echo import EchoTool, WorkspaceInfoTool
from agentcore.tools.builtin.inspect import InspectTool
from agentcore.tools.builtin.list_directory import ListDirectoryTool
from agentcore.tools.builtin.read_file import ReadFileTool


def default_tools() -> list[Tool]:
    """Tools registered by default: read-only capabilities only.

    ``read_file``, ``list_directory`` and ``inspect`` touch real user data.
    All three are read-only, all are gated by ``FS_READ`` through the normal
    executor/permission pipeline, and every filesystem safety decision they
    make belongs to :class:`~agentcore.fs.guard.WorkspaceGuard`.
    """
    return [EchoTool(), WorkspaceInfoTool(), ReadFileTool(),
            ListDirectoryTool(), InspectTool()]


__all__ = ["EchoTool", "WorkspaceInfoTool", "ReadFileTool",
           "ListDirectoryTool", "InspectTool", "default_tools"]
