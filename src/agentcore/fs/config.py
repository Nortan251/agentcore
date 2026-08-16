"""Filesystem-safety configuration.

Lives in the ``fs`` package (like ``memory/config.py``, ``conversation/config.py``
and ``permissions/config.py``) so ``config.schema`` can re-export it without a
cycle: this module imports only :mod:`agentcore.errors`.

Every bound here has a stated reason. Arbitrary numbers are worse than no
numbers, because nobody knows later whether they may be changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentcore.errors import ConfigError

#: Reasoning for the defaults below is inline on each field. They are chosen to
#: be *generous for source code* and *hostile to bulk exfiltration*.


@dataclass(frozen=True)
class FilesystemConfig:
    """Bounds and policy for workspace-scoped filesystem access.

    No filesystem tool exists yet; this is the boundary a future ``read_file``
    or ``list_directory`` consumes so that limits are configuration, not
    constants buried in a tool.
    """

    #: Hard ceiling on bytes read from a single file. 1 MiB comfortably holds
    #: any plausible source file, config or log excerpt while making it
    #: impossible for one call to pull a multi-gigabyte blob into memory.
    #: Enforced *during* reading, chunk by chunk — never by reading then
    #: trimming.
    max_file_bytes: int = 1_048_576
    #: Bytes per read chunk. Small enough that the ceiling is honoured
    #: promptly, large enough to avoid syscall churn.
    read_chunk_bytes: int = 65_536
    #: Maximum directory entries a future listing may return. 1000 is far
    #: beyond a human-reviewable listing while bounding response size.
    max_directory_entries: int = 1_000
    #: Character ceiling on a rendered filesystem tool result. Deliberately
    #: larger than ``conversation.max_tool_result_chars`` (2000) so the
    #: conversational bound stays the authoritative one in chat, while
    #: autonomous runs still get a hard cap.
    max_result_chars: int = 10_000
    #: Longest accepted request path. 4096 matches the common POSIX PATH_MAX;
    #: anything longer is a probe, not a real file.
    max_path_length: int = 4_096
    #: Maximum path components. Bounds pathological deep-nesting requests.
    max_path_depth: int = 64
    #: Symlink posture. ``False`` (default) rejects *any* symlinked component
    #: in a requested path, even one that stays inside the workspace. This is
    #: the strictest safe default; escape is impossible by construction rather
    #: than by a resolution check that must be exactly right. Set ``True`` to
    #: allow symlinks whose resolved target is still contained.
    allow_internal_symlinks: bool = False
    #: Whether the sensitive-path denylist is enforced. Kept configurable for
    #: testing and deliberate operator override; on by default.
    deny_sensitive_paths: bool = True

    @property
    def effective_chunk_bytes(self) -> int:
        """Chunk size actually used, never larger than the byte ceiling.

        Clamped rather than rejected: *lowering* ``max_file_bytes`` is the
        safer direction, and an operator tightening the limit should not have
        to also remember to shrink the chunk size.
        """
        return min(self.read_chunk_bytes, self.max_file_bytes)

    def validate(self) -> None:
        for name in ("max_file_bytes", "read_chunk_bytes",
                     "max_directory_entries", "max_result_chars",
                     "max_path_length", "max_path_depth"):
            if getattr(self, name) < 1:
                raise ConfigError(f"filesystem.{name} must be at least 1",
                                  value=getattr(self, name))
