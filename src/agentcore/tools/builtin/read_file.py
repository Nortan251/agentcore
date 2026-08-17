"""``read_file`` — the first real filesystem capability.

Deliberately small. Every security decision belongs to
:class:`~agentcore.fs.guard.WorkspaceGuard`, which this tool calls through its
single authoritative entry point, :meth:`WorkspaceGuard.open_file`. This module
contains **no** path handling, containment, symlink, sensitive-file, hardlink
or special-file logic — duplicating any of it would create a second, weaker
security boundary that could drift out of step with the first.

What this tool actually adds on top of the guard is only:

* turning a validated, bounded byte stream into UTF-8 text, refusing anything
  that is not valid UTF-8 rather than mangling it;
* reporting truncation explicitly, in the output the model sees;
* mapping guard rejections onto the project's normal ``ToolResult`` failures.
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

#: Prefix placed on the returned text when the byte ceiling stopped the read.
#:
#: ``ToolResult.metadata`` does not reach the model — the conversational
#: renderer sends only ``output`` — so truncation has to be stated in the
#: output itself. Otherwise the model would have to infer completeness from
#: character count, which is exactly what the task forbids.
TRUNCATION_NOTICE = (
    "[truncated: only the first {read} bytes of this {total}-byte file are "
    "shown]\n"
)

#: Human-readable explanation per guard rejection category. The guard's own
#: message is already safe (it never embeds an absolute path); these add the
#: bit of context a model needs to choose a different action instead of
#: retrying blindly.
_REASON_HINTS: dict[RejectionReason, str] = {
    RejectionReason.NOT_FOUND: "no such file in the workspace",
    RejectionReason.SENSITIVE:
        "this file is treated as credential material and cannot be read",
    RejectionReason.SENSITIVE_TARGET:
        "this path resolves to credential material and cannot be read",
    RejectionReason.MULTIPLE_HARD_LINKS:
        "this file has multiple hard links and may alias content outside the "
        "workspace",
    RejectionReason.SYMLINK:
        "symbolic links are not permitted",
    RejectionReason.ESCAPES_WORKSPACE:
        "this path lies outside the workspace",
    RejectionReason.TRAVERSAL:
        "paths containing '..' are not accepted",
    RejectionReason.ABSOLUTE:
        "give a path relative to the workspace, not an absolute one",
    RejectionReason.ADS:
        "alternate data streams are not supported",
    RejectionReason.NOT_A_REGULAR_FILE:
        "only regular files can be read",
    RejectionReason.TOO_LARGE:
        "this file is larger than the configured read limit",
}


class ReadFileTool(Tool):
    """Read a UTF-8 text file from inside the agent's workspace.

    Strictly read-only: it opens through the guard, which uses ``O_RDONLY``,
    and never touches a write, create, rename or delete API.
    """

    @property
    def spec(self) -> ToolSpec:
        return tool_spec(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the workspace. Give a path "
                "relative to the workspace root, such as 'src/app.py'. "
                "Absolute paths, '..' and credential files are refused. "
                "Large files are truncated and the result says so."
            ),
            parameters=[
                ToolParameter(
                    "path", ParamType.STRING,
                    "Workspace-relative path of the file to read, "
                    "e.g. 'src/app.py'.",
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
            # other way of reading the file.
            return ToolResult.failure(
                "no workspace is configured for this agent",
                kind="no_workspace")

        requested = args["path"]
        log = context.logger

        try:
            # THE security boundary. Validation, containment, symlink policy,
            # sensitive-path policy, hardlink policy, special-file rejection
            # and the secure open all happen inside this one call.
            with guard.open_file(requested) as handle:
                data, truncated = guard.read_from(handle)
                facts = handle.facts
        except PathRejectedError as exc:
            return self._rejected(exc, log)

        text = self._decode(data, truncated=truncated)
        if text is None:
            self._log(log, ok=False, reason="not_utf8", bytes_read=len(data))
            return ToolResult.failure(
                "file is not valid UTF-8 text and cannot be read as text",
                kind="not_utf8_text",
                path=facts.relative,
                bytes_read=len(data),
            )

        body = text
        if truncated:
            body = TRUNCATION_NOTICE.format(read=len(data),
                                            total=facts.size) + text

        self._log(log, ok=True, bytes_read=len(data), truncated=truncated)
        return ToolResult.success(
            body,
            path=facts.relative,
            bytes_read=len(data),
            size_bytes=facts.size,
            truncated=truncated,
            encoding="utf-8",
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _decode(data: bytes, *, truncated: bool) -> str | None:
        """Strict UTF-8 decode, returning ``None`` when the bytes are not text.

        No lossy codec and no ``errors="replace"``: silently substituting
        U+FFFD would hand the model plausible-looking text that is not what the
        file contains.

        The one subtlety is truncation. Stopping at a byte ceiling can cut a
        multi-byte character in half, which raises ``UnexpectedEndOfData`` —
        that is an artefact of *our* cut, not evidence of binary content. In
        that case, and only for a truncated read, the trailing partial
        character is dropped and the rest is returned.
        """
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if truncated and exc.end == len(data) and exc.start > 0:
                try:
                    return data[:exc.start].decode("utf-8")
                except UnicodeDecodeError:
                    return None
            return None

    @staticmethod
    def _rejected(exc: PathRejectedError, log: Any) -> ToolResult:
        """Turn a guard rejection into a safe, categorical tool failure.

        The guard's message and details are already log-safe — they carry the
        workspace-relative path and a reason, never the host layout — so they
        are reused rather than reformatted.
        """
        hint = _REASON_HINTS.get(exc.reason)
        message = f"{exc.message}: {hint}" if hint else exc.message
        if log is not None:
            log.warning(Events.TOOL_FAILED, reason=str(exc.reason))
        return ToolResult.failure(message, kind="path_rejected",
                                  **exc.details)

    @staticmethod
    def _log(log: Any, *, ok: bool, bytes_read: int,
             truncated: bool = False, reason: str | None = None) -> None:
        """Record only counters and status — never content, never the path.

        Even the workspace-relative path is withheld: file *names* can be
        sensitive, and the byte counts are what actually aid debugging.
        """
        if log is None:
            return
        fields: dict[str, Any] = {"bytes_read": bytes_read,
                                  "truncated": truncated}
        if reason:
            fields["reason"] = reason
        if ok:
            log.info(Events.TOOL_SUCCEEDED, **fields)
        else:
            log.warning(Events.TOOL_FAILED, **fields)
