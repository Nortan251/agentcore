"""agentcore — foundation for a long-term autonomous AI computer agent.

Public surface is intentionally small; import submodules for the rest.
"""

from agentcore.config.loader import load_config
from agentcore.config.schema import AgentConfig
from agentcore.core.agent import Agent, AgentDependencies, build_agent
from agentcore.errors import AgentError
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.state.models import AgentPhase, AgentState
from agentcore.tasks.models import Subtask, Task, TaskStatus
from agentcore.tools.base import Tool, ToolResult, ToolSpec

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Agent",
    "AgentDependencies",
    "build_agent",
    "AgentConfig",
    "load_config",
    "AgentError",
    "Task",
    "Subtask",
    "TaskStatus",
    "AgentState",
    "AgentPhase",
    "Tool",
    "ToolSpec",
    "ToolResult",
    "PermissionLevel",
    "Capability",
]
