"""``WorkspaceGuard`` — the single entry point future filesystem tools use.

A future ``read_file`` or ``list_directory`` should need exactly this::

    safe = guard.resolve_relative(args["path"])       # containment + policy
    facts = guard.require_readable_file(safe)         # kind + size checks
    data, truncated = guard.read_bytes(safe)          # bounded during reading

and must not re-implement canonicalisation, containment, sensitive-file checks
or special-file checks.

TOCTOU — what this guarantees and what it does not
--------------------------------------------------
Validation and use are two operations, so a sufficiently privileged local
attacker who can rename or relink paths *between* them can in principle make
the opened object differ from the validated one. ``pathlib.resolve()`` does not
prevent this and this module never claims it does.

What :meth:`WorkspaceGuard.open_regular` actually does:

* opens with ``O_NOFOLLOW`` (where the platform provides it), so the final
  component cannot be a symlink at the moment of opening;
* ``fstat``s the **open descriptor** and compares device/inode against the
  values observed during validation, so a swap between validate and open is
  *detected* and raises :class:`~agentcore.fs.paths.PathRejectedError` with
  reason ``CHANGED_UNDERFOOT``;
* re-checks the kind from that same descriptor, so the thing being read is a
  regular file according to the descriptor itself, not an earlier lookup.

What remains unguaranteed: a **parent directory** component could be swapped
between the walk and the open (a full defence needs ``openat``-relative
traversal from a directory descriptor, which is POSIX-specific and out of scope
here), and the check assumes the attacker cannot recreate a file with an
identical device/inode pair. Both are documented in tests rather than papered
over.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.paths import (
    FileFacts,
    O_NOFOLLOW,
    NON_READABLE_KINDS,
    PathKind,
    PathRejectedError,
    RejectionReason,
    canonical_workspace,
    check_relative_form,
    classify,
    contain,
    symlinked_component,
    workspace_identity,
)
from agentcore.fs.sensitive import DEFAULT_SENSITIVE_POLICY, SensitivePathPolicy


@dataclass(frozen=True)
class SafePath:
    """A path proven to be inside the workspace at validation time.

    Only the guard constructs these. A future tool that accepts a ``SafePath``
    rather than a string cannot be handed an unvalidated path by mistake.
    """

    #: Absolute, resolved location on disk.
    absolute: Path
    #: Workspace-relative POSIX form — the only form that is safe to log or
    #: show a model, since it reveals nothing about the host layout.
    relative: str

    def __str__(self) -> str:
        return self.relative


class WorkspaceGuard:
    """Workspace identity plus every filesystem safety check, in one object.

    Constructed once (by :class:`~agentcore.core.agent.Agent`) from the
    canonical workspace root, then handed to tools through
    :class:`~agentcore.tools.base.ToolContext`.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        config: FilesystemConfig | None = None,
        sensitive_policy: SensitivePathPolicy | None = None,
    ) -> None:
        self._root = canonical_workspace(root)
        self._config = config or FilesystemConfig()
        self._config.validate()
        self._sensitive = sensitive_policy or DEFAULT_SENSITIVE_POLICY

    # -- identity ----------------------------------------------------------
    @property
    def root(self) -> Path:
        """The canonical workspace root. Read-only by design."""
        return self._root

    @property
    def config(self) -> FilesystemConfig:
        return self._config

    @property
    def sensitive_policy(self) -> SensitivePathPolicy:
        return self._sensitive

    @property
    def identity(self) -> str:
        """Stable identity string used to pin a run to this workspace."""
        return workspace_identity(self._root)

    def matches(self, identity: str | None) -> bool:
        """Whether ``identity`` denotes this same workspace.

        ``None`` means "legacy run, recorded before workspace pinning existed"
        and is treated as a match so old state keeps loading.
        """
        return identity is None or identity == self.identity

    # -- containment -------------------------------------------------------
    def resolve_relative(self, requested: Any) -> SafePath:
        """Validate a requested path and return a contained absolute path.

        Order matters and is deliberate:

        1. lexical form (cheap, no filesystem access, cross-platform);
        2. sensitive-path policy (before *any* filesystem access, so a denied
           credential file is not even stat'd);
        3. symlink policy (walked component by component, never followed);
        4. resolved containment (the actual guarantee).
        """
        relative = check_relative_form(
            requested,
            max_length=self._config.max_path_length,
            max_depth=self._config.max_path_depth,
        )

        if self._config.deny_sensitive_paths:
            reason = self._sensitive.reason(relative)
            if reason is not None:
                raise PathRejectedError(RejectionReason.SENSITIVE,
                                        f"refused: {reason}",
                                        path=relative.as_posix())

        if not self._config.allow_internal_symlinks:
            link = symlinked_component(self._root, relative)
            if link is not None:
                raise PathRejectedError(
                    RejectionReason.SYMLINK,
                    "path crosses a symbolic link, which is not permitted",
                    path=link)

        resolved = contain(self._root, relative)
        return SafePath(absolute=resolved, relative=relative.as_posix())

    # -- kind / readability ------------------------------------------------
    def require_readable_file(self, safe: SafePath) -> FileFacts:
        """Assert ``safe`` is an existing, regular, readable, in-bounds file.

        Returns :class:`FileFacts` carrying the device/inode identity that
        :meth:`open_regular` later verifies.
        """
        kind = classify(safe.absolute)
        if kind is PathKind.MISSING:
            raise PathRejectedError(RejectionReason.NOT_FOUND,
                                    "no such file in the workspace",
                                    path=safe.relative)
        if kind is PathKind.SYMLINK:
            raise PathRejectedError(
                RejectionReason.SYMLINK,
                "path is a symbolic link, which is not permitted",
                path=safe.relative)
        if kind is PathKind.DIRECTORY:
            raise PathRejectedError(RejectionReason.NOT_A_REGULAR_FILE,
                                    "path is a directory, not a file",
                                    path=safe.relative)
        if kind in NON_READABLE_KINDS:
            raise PathRejectedError(
                RejectionReason.NOT_A_REGULAR_FILE,
                f"path is a {kind}, which cannot be read safely",
                path=safe.relative)

        st = safe.absolute.lstat()
        if st.st_size > self._config.max_file_bytes:
            raise PathRejectedError(
                RejectionReason.TOO_LARGE,
                "file exceeds the maximum readable size",
                path=safe.relative, size=st.st_size,
                limit=self._config.max_file_bytes)
        if not os.access(safe.absolute, os.R_OK):
            raise PathRejectedError(RejectionReason.UNREADABLE,
                                    "file is not readable",
                                    path=safe.relative)
        return FileFacts(relative=safe.relative, kind=kind, size=st.st_size,
                         device=st.st_dev, inode=st.st_ino)

    def require_directory(self, safe: SafePath) -> FileFacts:
        """Assert ``safe`` is an existing directory inside the workspace."""
        kind = classify(safe.absolute)
        if kind is PathKind.MISSING:
            raise PathRejectedError(RejectionReason.NOT_FOUND,
                                    "no such directory in the workspace",
                                    path=safe.relative)
        if kind is PathKind.SYMLINK:
            raise PathRejectedError(
                RejectionReason.SYMLINK,
                "path is a symbolic link, which is not permitted",
                path=safe.relative)
        if kind is not PathKind.DIRECTORY:
            raise PathRejectedError(RejectionReason.NOT_A_DIRECTORY,
                                    "path is not a directory",
                                    path=safe.relative)
        st = safe.absolute.lstat()
        return FileFacts(relative=safe.relative, kind=kind, size=0,
                         device=st.st_dev, inode=st.st_ino)

    def root_path(self) -> SafePath:
        """The workspace root itself as a :class:`SafePath`."""
        return SafePath(absolute=self._root, relative=".")

    # -- bounded reading ---------------------------------------------------
    def open_regular(self, safe: SafePath, facts: FileFacts):
        """Open a validated regular file, verifying it did not change.

        Returns a binary file object. See the module docstring for the exact
        TOCTOU guarantee.
        """
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | O_NOFOLLOW
        try:
            fd = os.open(safe.absolute, flags)
        except OSError as exc:
            # ELOOP here means the final component became a symlink.
            raise PathRejectedError(RejectionReason.CHANGED_UNDERFOOT,
                                    "file could not be opened safely",
                                    path=safe.relative,
                                    errno=exc.errno) from exc
        try:
            st = os.fstat(fd)
            import stat as stat_module
            if not stat_module.S_ISREG(st.st_mode):
                raise PathRejectedError(
                    RejectionReason.NOT_A_REGULAR_FILE,
                    "opened object is not a regular file",
                    path=safe.relative)
            if (st.st_dev, st.st_ino) != (facts.device, facts.inode):
                raise PathRejectedError(
                    RejectionReason.CHANGED_UNDERFOOT,
                    "file changed between validation and opening",
                    path=safe.relative)
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb")

    def iter_bounded(self, safe: SafePath,
                     facts: FileFacts) -> Iterator[bytes]:
        """Yield chunks, stopping at the configured byte ceiling.

        The ceiling is enforced **while** reading: at no point is more than
        ``max_file_bytes`` (plus one chunk) held, and a file that grew after
        validation still cannot exceed the cap.
        """
        remaining = self._config.max_file_bytes
        chunk_size = self._config.effective_chunk_bytes
        with self.open_regular(safe, facts) as handle:
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

    def read_bytes(self, safe: SafePath,
                   facts: FileFacts) -> tuple[bytes, bool]:
        """Read a validated file, bounded. Returns ``(data, truncated)``.

        ``truncated`` is ``True`` when the ceiling stopped the read before the
        file ended, so a caller can say so explicitly rather than silently
        returning a prefix.
        """
        parts = list(self.iter_bounded(safe, facts))
        data = b"".join(parts)
        truncated = len(data) >= self._config.max_file_bytes
        if truncated:
            # Distinguish "exactly at the cap and complete" from "cut short".
            truncated = facts.size > self._config.max_file_bytes
        return data, truncated

    # -- observability -----------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """Non-sensitive description for diagnostics.

        Includes the root because operators need to know which directory the
        agent is pinned to; excludes anything derived from user file contents.
        """
        return {
            "workspace": self.identity,
            "exists": self._root.is_dir(),
            "max_file_bytes": self._config.max_file_bytes,
            "max_directory_entries": self._config.max_directory_entries,
            "symlinks_allowed": self._config.allow_internal_symlinks,
            "sensitive_paths_denied": self._config.deny_sensitive_paths,
        }
