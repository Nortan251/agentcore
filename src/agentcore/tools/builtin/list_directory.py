"""``list_directory`` — workspace-scoped directory discovery.

Like :mod:`~agentcore.tools.builtin.read_file`, this tool owns **no** security
logic. Validation, containment, symlink policy, sensitive-name policy and the
bounded walk all belong to :meth:`WorkspaceGuard.list_directory`, which this
tool calls and nothing else. It never opens a file, never follows a link and
never descends into a subdirectory.

What the tool adds is presentation: turning a
:class:`~agentcore.fs.paths.DirectoryListing` into text the model can act on,
and mapping guard rejections onto the project's normal ``ToolResult`` failures.

Why the listing is rendered into ``output`` rather than left in ``metadata``:
the conversational renderer sends only ``result.output`` to the model, so
anything the model must know — the entries, that some were withheld, that the
list was cut short — has to be in the text.
"""

from __future__ import annotations

from typing import Any, Mapping

from agentcore.fs.paths import PathRejectedError, RejectionReason
from agentcore.observability.events import Events
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

#: Shown when the entry cap stopped the walk.
TRUNCATION_NOTICE = (
    "[truncated: only the first {shown} entries are listed; this directory "
    "contains more]"
)

#: Shown once when any entry was withheld, so the model understands that a
#: name it cannot see is a policy decision rather than an omission.
SENSITIVE_NOTICE = (
    "[{count} entr{suffix} withheld: the name itself is treated as "
    "credential material]"
)

#: Same shape as read_file's mapping: the guard's message is already safe, so
#: these only add the context needed to choose a different action.
_REASON_HINTS: dict[RejectionReason, str] = {
    RejectionReason.NOT_FOUND: "no such directory in the workspace",
    RejectionReason.NOT_A_DIRECTORY:
        "this path is a file, not a directory",
    RejectionReason.SENSITIVE:
        "this directory is treated as credential material and cannot be "
        "listed",
    RejectionReason.SENSITIVE_TARGET:
        "this path resolves to credential material and cannot be listed",
    RejectionReason.SYMLINK: "symbolic links are not permitted",
    RejectionReason.ESCAPES_WORKSPACE:
        "this path lies outside the workspace",
    RejectionReason.TRAVERSAL: "paths containing '..' are not accepted",
    RejectionReason.ABSOLUTE:
        "give a path relative to the workspace, not an absolute one",
    RejectionReason.UNREADABLE: "this directory could not be read",
}


class ListDirectoryTool(Tool):
    """List the immediate entries of a workspace-relative directory.

    Discovery, not traversal: one level only, no recursion, no file access.
    """

    @property
    def spec(self) -> ToolSpec:
        return tool_spec(
            name="list_directory",
            description=(
                "List the immediate entries of a directory in the workspace. "
                "Use '.' for the workspace root, or a relative path such as "
                "'src'. Lists one level only — it does not recurse, open "
                "files or follow symbolic links. Entries whose names are "
                "credential material are reported without their names."
            ),
            parameters=[
                ToolParameter(
                    "path", ParamType.STRING,
                    "Workspace-relative directory to list, e.g. 'src'. "
                    "Use '.' for the workspace root.",
                ),
            ],
            capabilities=[Capability.FS_READ],
            read_only=True,
            tags=("filesystem",),
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        guard = context.workspace
        if guard is None:
            return ToolResult.failure(
                "no workspace is configured for this agent",
                kind="no_workspace")

        log = context.logger
        try:
            # THE security boundary: validation, containment, sensitive-name
            # policy, classification and bounding all happen in the guard.
            safe = guard.resolve_directory(args["path"])
            listing = guard.list_directory(safe)
        except PathRejectedError as exc:
            return self._rejected(exc, log)

        self._log(log, count=len(listing.entries),
                  truncated=listing.truncated)
        return ToolResult.success(
            self._render(listing),
            **listing.to_dict(),
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _render(listing: Any) -> str:
        """Plain-text rendering — this is what the model actually receives."""
        lines: list[str] = []
        for entry in listing.entries:
            suffix = "/" if str(entry.kind) == "directory" else ""
            line = f"{entry.display_name}{suffix}  ({entry.kind}"
            if entry.link_target is not None:
                line += f" -> {entry.link_target}"
            line += ")"
            lines.append(line)

        if not lines:
            lines.append("(empty directory)")
        if listing.sensitive_count:
            lines.append(SENSITIVE_NOTICE.format(
                count=listing.sensitive_count,
                suffix="y" if listing.sensitive_count == 1 else "ies"))
        if listing.truncated:
            lines.append(TRUNCATION_NOTICE.format(shown=len(listing.entries)))
        return "\n".join(lines)

    @staticmethod
    def _rejected(exc: PathRejectedError, log: Any) -> ToolResult:
        hint = _REASON_HINTS.get(exc.reason)
        message = f"{exc.message}: {hint}" if hint else exc.message
        if log is not None:
            log.warning(Events.TOOL_FAILED, reason=str(exc.reason))
        return ToolResult.failure(message, kind="path_rejected",
                                  **exc.details)

    @staticmethod
    def _log(log: Any, *, count: int, truncated: bool) -> None:
        """Counters only — never entry names, never the path."""
        if log is None:
            return
        log.info(Events.TOOL_SUCCEEDED, entries=count, truncated=truncated)
