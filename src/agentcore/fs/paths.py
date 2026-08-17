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

import heapq
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
    #: A regular file with more than one hard link. Containment is a property
    #: of pathnames, and a hardlink gives one inode several names — possibly
    #: including one outside the workspace.
    MULTIPLE_HARD_LINKS = "multiple_hard_links"
    #: The sensitive-path policy matched the link's *effective target* rather
    #: than the requested name.
    SENSITIVE_TARGET = "sensitive_target"

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


#: ``FILE_ATTRIBUTE_REPARSE_POINT``. On Windows this bit is set for symlinks,
#: directory junctions and every other reparse point — the mechanism by which
#: a name can redirect elsewhere. POSIX ``stat`` results have no such
#: attribute, so the link test falls back to the symlink bit alone there.
_REPARSE_POINT = 0x400


def _is_windows_reparse(st: Any) -> bool:
    """Whether a ``stat_result`` carries the Windows reparse-point attribute.

    Always ``False`` off Windows. On Windows this is the one signal that is
    present for junctions (and other reparse points) even though their
    ``st_mode`` says "directory" and ``Path.is_symlink()`` reports ``False``
    for them on current CPython.
    """
    if os.name != "nt":
        return False
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_link(path: Path) -> bool:
    """Whether ``path`` is a redirecting link.

    True for a symlink on any platform, and on Windows for any reparse point
    (which is how a directory junction is represented). ``Path.is_symlink()``
    alone is not enough on Windows: a junction is a reparse point whose mode
    is a directory, so the attribute check is what closes that gap.
    """
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        st = path.lstat()
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def symlinked_component(root: Path, relative: PurePosixPath) -> str | None:
    """Return the first component of ``relative`` that is a link, if any.

    Walks from the workspace root outward using :func:`_is_link`, which does
    *not* follow links, so a symlinked — or, on Windows, junctioned — parent
    directory is detected before it can be traversed. Returns the
    workspace-relative component path so the caller can report it without
    leaking the absolute layout.
    """
    current = root
    walked: list[str] = []
    for part in relative.parts:
        current = current / part
        walked.append(part)
        if _is_link(current):
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


def kind_from_mode(mode: int) -> PathKind:
    """Classify a ``st_mode`` value.

    Split out from :func:`classify` so the same logic serves both a pathname
    ``lstat`` and an ``fstat`` on an open descriptor — the descriptor path must
    not re-implement type detection.
    """
    return _kind_from_mode(mode)


def classify(path: Path) -> PathKind:
    """Classify a path without following symlinks.

    Uses ``lstat`` so a symlink reports as :attr:`PathKind.SYMLINK` rather than
    silently reporting whatever it points at. On Windows a reparse point — a
    junction, or any other reparse tag — is also reported as
    :attr:`PathKind.SYMLINK`, because it redirects exactly like a symlink even
    though its ``st_mode`` says "directory".

    This is a *pathname* check and is therefore advisory: by the time the
    result is used the name may refer to something else. Security decisions
    about a file being read must come from :mod:`agentcore.fs.opened` instead.
    """
    try:
        st = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return PathKind.MISSING
    except OSError:
        return PathKind.OTHER
    if _is_windows_reparse(st):
        return PathKind.SYMLINK
    return _kind_from_mode(st.st_mode)


def _kind_from_mode(mode: int) -> PathKind:
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


class LinkTarget(str, Enum):
    """Where a symlink points, without disclosing the target path itself.

    Determined by resolving the link *for classification only*. The resolved
    path is never returned, logged or shown to a model: knowing that a link
    escapes the workspace is useful, knowing the host layout it escapes to is
    not.
    """

    #: Resolves to something inside the workspace.
    INTERNAL = "internal"
    #: Resolves outside the workspace. Reading it would be refused.
    EXTERNAL = "external"
    #: Points at something that does not exist.
    BROKEN = "broken"
    #: Could not be determined (permission error, resolution failure).
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class DirectoryEntry:
    """One immediate child of a listed directory.

    Describes the entry *itself*. Nothing here requires opening a file,
    following a link or descending into a subdirectory.

    ``name`` is ``None`` for a sensitive entry: the filename is withheld
    because a credential file's *name* can disclose as much as its contents
    (``id_rsa`` says a private key is present; ``.env.production`` names an
    environment). ``sensitive`` still reports that something is there.
    """

    #: The entry's own name, or ``None`` when withheld as sensitive.
    name: str | None
    kind: PathKind
    sensitive: bool = False
    #: Only meaningful when ``kind`` is :attr:`PathKind.SYMLINK`.
    link_target: LinkTarget | None = None

    @property
    def display_name(self) -> str:
        return self.name if self.name is not None else REDACTED_NAME

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.display_name,
                                   "type": str(self.kind)}
        if self.sensitive:
            payload["sensitive"] = True
        if self.link_target is not None:
            payload["link"] = str(self.link_target)
        return payload


#: Stand-in shown instead of a sensitive entry's real name.
REDACTED_NAME = "<redacted>"


class _Descending:
    """Wraps a name so a min-heap behaves as a max-heap.

    ``heapq`` provides only a min-heap, but retaining the *smallest* names
    requires cheap access to the largest one held so far. Inverting the
    comparison is the standard way to get that without a second structure.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_Descending") -> bool:
        return self.value > other.value


class BoundedNameSelector:
    """Selects the lexicographically smallest names from a stream, in bounded
    memory.

    Why this exists
    ---------------
    Listing a directory previously did ``sorted(entry.name for entry in
    scanner)`` and then sliced to the cap. The *output* was bounded but memory
    was not: a directory with millions of entries built a list of millions of
    strings before discarding nearly all of them.

    This consumes the same stream while holding at most ``capacity`` names,
    where ``capacity == limit + 1``.

    Why the result is identical to a full sort
    ------------------------------------------
    The selector keeps the ``limit + 1`` smallest names seen so far. The
    ``limit`` smallest names of the whole directory are necessarily a subset of
    the ``limit + 1`` smallest, so sorting the retained set and slicing to
    ``limit`` yields exactly the prefix ``sorted(all_names)[:limit]`` would
    have produced. The final sort runs over at most ``limit + 1`` items, never
    the whole directory.

    Why one extra slot
    ------------------
    Truncation must distinguish "exactly ``limit`` entries" from "more than
    ``limit``". Retaining one name beyond the cap makes that a fact about
    stored data rather than an inference from ``len(result) == limit``, which
    cannot tell the two cases apart.
    """

    __slots__ = ("_limit", "_capacity", "_heap", "peak_size", "seen")

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        #: One more than the cap, so "more exist" is observable.
        self._capacity = limit + 1
        self._heap: list[_Descending] = []
        #: High-water mark of retained names. Asserted by tests as the
        #: memory-bound invariant; never used by the algorithm itself.
        self.peak_size = 0
        #: Total names offered. A counter, not storage.
        self.seen = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def retained(self) -> int:
        """How many names are held right now."""
        return len(self._heap)

    def add(self, name: str) -> None:
        """Offer one name. O(log capacity) time, O(capacity) space."""
        self.seen += 1
        heap = self._heap
        if len(heap) < self._capacity:
            heapq.heappush(heap, _Descending(name))
        elif name < heap[0].value:
            # Full: swap out the largest retained name for this smaller one.
            heapq.heappushpop(heap, _Descending(name))
        # Otherwise the name is larger than everything retained and cannot
        # belong to the smallest `limit`; it is dropped immediately.
        if len(heap) > self.peak_size:
            self.peak_size = len(heap)

    def result(self) -> tuple[list[str], bool]:
        """Return ``(names, truncated)``.

        ``names`` holds at most ``limit`` entries in ascending byte order;
        ``truncated`` is ``True`` when more eligible entries existed.
        """
        ordered = sorted(item.value for item in self._heap)
        truncated = len(ordered) > self._limit
        return ordered[:self._limit], truncated


@dataclass(frozen=True)
class DirectoryListing:
    """The bounded, classified result of listing one directory."""

    #: Workspace-relative path of the directory that was listed.
    relative: str
    entries: tuple[DirectoryEntry, ...]
    #: True when the entry cap stopped the walk before the directory ended.
    truncated: bool
    #: The cap that was applied, so a caller can report it.
    limit: int
    #: Entries withheld because the sensitive policy matched their names.
    sensitive_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative,
            "entries": [e.to_dict() for e in self.entries],
            "count": len(self.entries),
            "truncated": self.truncated,
            "entry_limit": self.limit,
            "sensitive_entries": self.sensitive_count,
        }


@dataclass(frozen=True)
class FileFacts:
    """Identity of a file as observed at validation time.

    These values are read from ``fstat`` on the **open descriptor**, so they
    describe the object actually being read rather than whatever a pathname
    referred to earlier. ``device``/``inode`` are retained for diagnostics and
    logging; they are deliberately *not* used as a TOCTOU defence, because
    inode numbers are reused (reproduced on ext4 — see
    :mod:`agentcore.fs.opened`).
    """

    relative: str
    kind: PathKind
    size: int
    device: int
    inode: int
    #: ``st_nlink`` of the opened object. >1 on a regular file means the inode
    #: has other names, which may lie outside the workspace.
    links: int = 1

    def to_dict(self) -> dict[str, Any]:
        # Deliberately no absolute path: safe to log.
        return {"path": self.relative, "kind": str(self.kind),
                "size": self.size, "links": self.links}


#: ``O_NOFOLLOW`` refuses to open a final-component symlink at the syscall
#: level. Absent on some platforms (notably Windows), hence the fallback.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
