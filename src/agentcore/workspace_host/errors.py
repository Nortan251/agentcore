"""Content-free host failures, safe for CLI publication."""

from agentcore.action_journal.models import require_action_id


class WorkspaceHostError(Exception):
    def __init__(self, category, *, action_ids=()):
        self.category = category
        self.action_ids = tuple(require_action_id(value) for value in action_ids)[:256]
        super().__init__(category)


def require_authority(enable_write, policy):
    from agentcore.permissions.levels import Capability, PermissionLevel

    if enable_write is not True:
        raise WorkspaceHostError("workspace_write_opt_in_required")
    if policy.level is not PermissionLevel.AUTONOMOUS or not all(
            policy.evaluate(capability).allowed
            for capability in (Capability.FS_READ, Capability.FS_WRITE)):
        raise WorkspaceHostError("workspace_authority_denied")
