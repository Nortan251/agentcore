"""Workspace identity and path containment primitives.

This module is the *only* place in the project that decides whether a
requested path is inside the agent's workspace. Future filesystem tools must
consume :class:`~agentcore.fs.guard.WorkspaceGuard` rather than re-deriving any
of this.

Two separate ideas live here:

``canonical_workspace(value)``
    Turns configuration (possibly relative, possibly ``~``-prefixed, possibly
    a symlink) into one absolute, symlink-free :class:`pathlib.Path`. Called
    once during agent construction.

``contain(root, requested)``
    Maps *workspace root + requested relative path* to a safe absolute path or
    a categorised rejection.

Containment is decided with :meth:`pathlib.PurePath.is_relative_to`, which is
path-component aware. A string prefix test is wrong and this module never uses
one: ``/workspace-other`` starts with ``/workspace`` but is not inside it.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from agentcore.errors import AgentError


class RejectionReason(str, Enum):
    """Why a requested path was refused.

    Categorical rather than free text so callers, logs and tests can branch on
    a stable value without parsing a message.
    """

    NOT_A_STRING = "not_a_string"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    TOO_DEEP = "too_deep"
    NUL_BYTE = "nul_byte"
    ABSOLUTE = "absolute"
    DRIVE_QUALIFIED = "drive_qualified"
    UNC = "unc"
    BACKSLASH = "backslash"
    TRAVERSAL = "traversal"
    ESCAPES_WORKSPACE = "escapes_workspace"
    SYMLINK = "symlink"
    SENSITIVE = "sensitive"
    NOT_FOUND = "not_found"
    NOT_A_REGULAR_FILE = "not_a_regular_file"
    NOT_A_DIRECTORY = "not_a_directory"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    CHANGED_UNDERFOOT = "changed_underfoot"

    def __str__(self) -> str:
        return self.value


class PathRejectedError(AgentError):
    """A requested path was refused by the workspace safety layer.

    Carries a :class:`RejectionReason` so callers can react to the *category*.
    The message never embeds the absolute resolved path: a rejection reason is
    safe to surface, a filesystem layout is not.
    """

    def __init__(self, reason: RejectionReason, message: str,
                 **details: Any) -> None:
        super().__init__(message, reason=str(reason), **details)
        self.reason = reason


class WorkspaceMismatchError(AgentError):
    """A persisted run belongs to a different workspace than the current one."""


def canonical_workspace(value: Any) -> Path:
    """Resolve configuration into the one canonical workspace root.

    Accepts a ``str`` or :class:`~pathlib.Path`. Expands ``~``, makes the path
    absolute against the current working directory, and resolves symlinks so
    that every later containment check compares like with like.

    The directory need not exist yet — the default workspace
    (``./.agent_workspace``) is created lazily by other layers. But if the path
    exists and is not a directory, that is a configuration error worth failing
    loudly on.
    """
    if isinstance(value, Path):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        raise PathRejectedError(RejectionReason.NOT_A_STRING,
                                "workspace_dir must be a string or Path",
                                got=type(value).__name__)
    if not raw.strip():
        raise PathRejectedError(RejectionReason.EMPTY,
                                "workspace_dir must not be empty")
    if "\x00" in raw:
        raise PathRejectedError(RejectionReason.NUL_BYTE,
                                "workspace_dir contains a NUL byte")

    root = Path(raw).expanduser()
    # resolve() is non-strict: a not-yet-created workspace is fine.
    root = root.resolve()
    if not root.is_absolute():  # pragma: no cover - resolve() guarantees this
        raise PathRejectedError(RejectionReason.ABSOLUTE,
                                "workspace_dir did not resolve to an "
                                "absolute path")
    if root.exists() and not root.is_dir():
        raise PathRejectedError(
            RejectionReason.NOT_A_DIRECTORY,
            "workspace_dir exists but is not a directory")
    return root


def workspace_identity(root: Path) -> str:
    """Stable string identity for a canonical workspace root.

    Used to pin a run to the workspace it started in. It is deliberately the
    canonical path string: human-readable in state files and directly
    comparable, with no hashing that would obscure a mismatch during debugging.
    """
    return str(root)


# ---------------------------------------------------------------------------
# Request-path validation
# ---------------------------------------------------------------------------

def check_relative_form(requested: Any, *, max_length: int,
                        max_depth: int) -> PurePosixPath:
    """Validate the *lexical* form of a requested path.

    Purely syntactic: no filesystem access happens here, so it is cheap, fully
    deterministic and testable on any platform. Windows-shaped inputs are
    rejected on POSIX too, which is what makes the rule cross-platform.

    Returns the normalised relative path (``.`` segments removed).
    """
    if isinstance(requested, Path):
        requested = str(requested)
    if not isinstance(requested, str):
        raise PathRejectedError(RejectionReason.NOT_A_STRING,
                                "path must be a string",
                                got=type(requested).__name__)
    if "\x00" in requested:
        raise PathRejectedError(RejectionReason.NUL_BYTE,
                                "path contains a NUL byte")
    if not requested.strip():
        raise PathRejectedError(RejectionReason.EMPTY,
                                "path must not be empty")
    if len(requested) > max_length:
        raise PathRejectedError(RejectionReason.TOO_LONG,
                                "path is too long", limit=max_length,
                                length=len(requested))

    # Backslashes are rejected outright. On Windows they are separators; on
    # POSIX they are legal filename characters, so accepting them would make
    # the same request mean two different things on two platforms. Predictable
    # beats permissive for a security primitive.
    if "\\" in requested:
        win = PureWindowsPath(requested)
        if win.drive.startswith("\\\\"):
            raise PathRejectedError(RejectionReason.UNC,
                                    "UNC paths are not accepted")
        if win.drive:
            raise PathRejectedError(RejectionReason.DRIVE_QUALIFIED,
                                    "drive-qualified paths are not accepted")
        raise PathRejectedError(RejectionReason.BACKSLASH,
                                "backslashes are not accepted in paths")

    # Drive-qualified forms without backslashes, e.g. "C:/x" or bare "C:".
    win = PureWindowsPath(requested)
    if win.drive:
        if win.drive.startswith("//"):
            raise PathRejectedError(RejectionReason.UNC,
                                    "UNC paths are not accepted")
        raise PathRejectedError(RejectionReason.DRIVE_QUALIFIED,
                                "drive-qualified paths are not accepted")

    pure = PurePosixPath(requested)
    if pure.is_absolute():
        raise PathRejectedError(RejectionReason.ABSOLUTE,
                                "absolute paths are not accepted")

    parts = pure.parts
    if ".." in parts:
        # Rejected lexically so the caller gets a precise reason. The resolved
        # containment check below is still performed and is the real guarantee.
        raise PathRejectedError(RejectionReason.TRAVERSAL,
                                "path traversal ('..') is not accepted")
    if not parts:
        # "." or "./" normalises to nothing.
        raise PathRejectedError(RejectionReason.EMPTY,
                                "path must name something inside the workspace")
    if len(parts) > max_depth:
        raise PathRejectedError(RejectionReason.TOO_DEEP,
                                "path has too many components",
                                limit=max_depth, depth=len(parts))
    return PurePosixPath(*parts)


def symlinked_component(root: Path, relative: PurePosixPath) -> str | None:
    """Return the first component of ``relative`` that is a symlink, if any.

    Walks from the workspace root outward using :meth:`Path.is_symlink`, which
    does *not* follow links, so a symlinked parent directory is detected before
    it can be traversed. Returns the workspace-relative component path so the
    caller can report it without leaking the absolute layout.
    """
    current = root
    walked: list[str] = []
    for part in relative.parts:
        current = current / part
        walked.append(part)
        if current.is_symlink():
            return "/".join(walked)
    return None


def contain(root: Path, relative: PurePosixPath) -> Path:
    """Map a validated relative path onto an absolute path inside ``root``.

    The containment test runs against the **resolved** candidate, not the
    lexical string, and uses component-aware
    :meth:`~pathlib.PurePath.is_relative_to`.
    """
    candidate = (root / Path(*relative.parts))
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise PathRejectedError(
            RejectionReason.ESCAPES_WORKSPACE,
            "path resolves outside the workspace")
    return resolved


# ---------------------------------------------------------------------------
# Special-file classification
# ---------------------------------------------------------------------------

class PathKind(str, Enum):
    """What a path actually *is* on disk."""

    REGULAR = "regular"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    FIFO = "fifo"
    SOCKET = "socket"
    CHAR_DEVICE = "char_device"
    BLOCK_DEVICE = "block_device"
    OTHER = "other"
    MISSING = "missing"

    def __str__(self) -> str:
        return self.value


#: Kinds a read tool must never open. Opening a FIFO can block forever;
#: opening a device can hang or return unbounded data.
NON_READABLE_KINDS = frozenset({
    PathKind.FIFO, PathKind.SOCKET, PathKind.CHAR_DEVICE,
    PathKind.BLOCK_DEVICE, PathKind.OTHER,
})


def classify(path: Path) -> PathKind:
    """Classify a path without following symlinks.

    Uses ``lstat`` so a symlink reports as :attr:`PathKind.SYMLINK` rather than
    silently reporting whatever it points at.
    """
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return PathKind.MISSING
    except OSError:
        return PathKind.OTHER
    mode = st.st_mode
    if stat_module.S_ISLNK(mode):
        return PathKind.SYMLINK
    if stat_module.S_ISREG(mode):
        return PathKind.REGULAR
    if stat_module.S_ISDIR(mode):
        return PathKind.DIRECTORY
    if stat_module.S_ISFIFO(mode):
        return PathKind.FIFO
    if stat_module.S_ISSOCK(mode):
        return PathKind.SOCKET
    if stat_module.S_ISCHR(mode):
        return PathKind.CHAR_DEVICE
    if stat_module.S_ISBLK(mode):
        return PathKind.BLOCK_DEVICE
    return PathKind.OTHER


@dataclass(frozen=True)
class FileFacts:
    """Identity of a file as observed at validation time.

    ``device``/``inode`` let a later open verify — via ``fstat`` on the open
    descriptor — that it got the same object that was validated. This is the
    project's TOCTOU mitigation; see :mod:`agentcore.fs.guard`.
    """

    relative: str
    kind: PathKind
    size: int
    device: int
    inode: int

    def to_dict(self) -> dict[str, Any]:
        # Deliberately no absolute path: safe to log.
        return {"path": self.relative, "kind": str(self.kind),
                "size": self.size}


#: ``O_NOFOLLOW`` refuses to open a final-component symlink at the syscall
#: level. Absent on some platforms (notably Windows), hence the fallback.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
