"""``inspect`` — bounded, read-only description of one workspace entry.

Like ``read_file`` and ``list_directory``, this tool owns **no** security
logic. Validation, containment, the sensitive-name policy, parent-link
rejection, final-component classification and the categorical link target all
belong to :meth:`~agentcore.fs.guard.WorkspaceGuard.inspect_path`, which this
tool calls and nothing else.

What the tool adds is presentation: rendering an
:class:`~agentcore.fs.paths.Inspection` into one bounded line of text for the
model, and mapping guard rejections onto the project's normal ``ToolResult``
failures.

The tool never opens a file, never reads a byte, never follows a link and
never descends into a directory — it describes the entry the pathname refers
to, exactly as ``list_directory`` describes entries, one path at a time.
"""

from __future__ import annotations

from typing import Any, Mapping

from agentcore.fs.paths import Inspection, PathRejectedError, RejectionReason
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

#: Human-readable explanation per guard rejection category. The guard's own
#: message is already safe (it never embeds an absolute path); these add the
#: context a model needs to choose a different action instead of retrying.
_REASON_HINTS: dict[RejectionReason, str] = {
    RejectionReason.NOT_FOUND: "no such file in the workspace",
    RejectionReason.SENSITIVE:
        "this path is treated as credential material and cannot be inspected",
    RejectionReason.SENSITIVE_TARGET:
        "this path resolves to credential material and cannot be inspected",
    RejectionReason.SYMLINK:
        "symbolic links are not permitted in path components",
    RejectionReason.ESCAPES_WORKSPACE:
        "this path lies outside the workspace",
    RejectionReason.TRAVERSAL:
        "paths containing '..' are not accepted",
    RejectionReason.ABSOLUTE:
        "give a path relative to the workspace, not an absolute one",
    RejectionReason.ADS:
        "alternate data streams are not supported",
    RejectionReason.NOT_A_DIRECTORY:
        "a parent component is not a directory",
    RejectionReason.UNREADABLE:
        "this path could not be inspected",
    RejectionReason.TOO_LONG:
        "this path is too long",
    RejectionReason.TOO_DEEP:
        "this path has too many components",
}


class InspectTool(Tool):
    """Describe a workspace entry without reading its contents.

    Strictly read-only: it classifies through the guard's ``lstat``-based
    inspection primitive and never touches a write, create, rename or delete
    API, and never opens the object for reading.
    """

    @property
    def spec(self) -> ToolSpec:
        return tool_spec(
            name="inspect",
            description=(
                "Describe a filesystem entry in the workspace without reading "
                "its contents. Give a path relative to the workspace root, or "
                "'.' for the workspace root itself. Returns the entry type "
                "and, where applicable, its size and modification time. "
                "Symbolic links and Windows junctions/reparse points are "
                "described categorically (internal/external/broken) and are "
                "never followed. No file contents are returned."
            ),
            parameters=[
                ToolParameter(
                    "path", ParamType.STRING,
                    "Workspace-relative path to inspect, e.g. 'src/app.py' "
                    "or '.'. Describes type, size and modification time; "
                    "links are reported categorically, never followed.",
                ),
            ],
            capabilities=[Capability.FS_READ],
            read_only=True,
            tags=("filesystem",),
        )

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        guard = context.workspace
        if guard is None:
            # No workspace was wired up. A failure, never a fallback to some
            # other way of inspecting the path.
            return ToolResult.failure(
                "no workspace is configured for this agent",
                kind="no_workspace")

        requested = args["path"]
        log = context.logger

        try:
            # THE security boundary. Lexical validation, sensitive-name
            # policy, parent-link rejection, final-component classification
            # and the categorical link target all happen inside this one call.
            inspection: Inspection = guard.inspect_path(requested)
        except PathRejectedError as exc:
            return self._rejected(exc, log)

        self._log(log, ok=True, kind=str(inspection.kind))
        return ToolResult.success(self._render(inspection),
                                  **inspection.to_dict())

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _render(inspection: Inspection) -> str:
        """One bounded line of text for the model.

        Only safe, categorical fields; never a resolved path, identifier or
        raw timestamp.
        """
        line = f"{inspection.relative}  ({inspection.kind}"
        if inspection.link_target is not None:
            line += f" -> {inspection.link_target}"
        line += ")"
        extras: list[str] = []
        if inspection.size is not None:
            extras.append(f"{inspection.size} bytes")
        if inspection.modified_at is not None:
            extras.append(f"modified {inspection.modified_at}")
        if extras:
            line += "  " + "  ".join(extras)
        return line

    @staticmethod
    def _rejected(exc: PathRejectedError, log: Any) -> ToolResult:
        """Turn a guard rejection into a safe, categorical tool failure.

        The guard's message and details are already log-safe — they carry the
        workspace-relative path and a reason, never the host layout or a
        resolved target — so they are reused rather than reformatted.
        """
        hint = _REASON_HINTS.get(exc.reason)
        message = f"{exc.message}: {hint}" if hint else exc.message
        if log is not None:
            log.warning(Events.TOOL_FAILED, reason=str(exc.reason))
        return ToolResult.failure(message, kind="path_rejected",
                                  **exc.details)

    @staticmethod
    def _log(log: Any, *, ok: bool, kind: str) -> None:
        """Record only the entry type — never the path, size or content.

        A path can be sensitive, and inspection may be called on anything the
        model names, so even the workspace-relative path is withheld here.
        """
        if log is None:
            return
        if ok:
            log.info(Events.TOOL_SUCCEEDED, kind=kind)
        else:
            log.warning(Events.TOOL_FAILED, kind=kind)
