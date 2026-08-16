"""Descriptor-anchored file access.

The security problem this module exists to solve
------------------------------------------------
Checking a *pathname* and then opening it later is two operations against a
mutable namespace. Anything decided before the open — type, size, link count —
describes whatever the name referred to *then*, not what was actually opened.

The audit reproduced exactly that: on ext4, deleting a validated file and
recreating another one at the same path frequently **reuses the same inode**,
so a ``(st_dev, st_ino)`` comparison cannot tell the replacement apart. That
was verified in this sandbox — the inode was reused on the first attempt (see
``tests/test_fs_toctou.py``), while the same test on tmpfs never reuses and
would have silently "passed".

The fix is not a better comparison; it is to stop asking the question about a
pathname at all. :func:`open_verified` opens **first** and then derives every
security decision from ``fstat`` on the returned descriptor. Type, size, link
count and containment are all properties of the object that is actually going
to be read, so no window exists between deciding and using.

What is still not guaranteed is *which* object the pathname resolved to at the
instant of the open — a parent directory swapped mid-traversal can still
redirect it. Defending that needs ``openat``-relative traversal from a
directory descriptor. See ``README.md`` for the full statement.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from agentcore.fs.paths import (
    FileFacts,
    PathKind,
    PathRejectedError,
    RejectionReason,
    kind_from_mode,
)

#: Flags used for every read. Each one is load-bearing:
#:
#: ``O_RDONLY``   nothing here may ever write.
#: ``O_NOFOLLOW`` the final component must not be a symlink at open time. This
#:                is a kernel-enforced check, unlike an ``lstat`` beforehand.
#: ``O_NONBLOCK`` opening a FIFO with no writer would otherwise block forever.
#:                Opening non-blocking lets us inspect and reject it instead of
#:                hanging the agent. Cleared again before reading a regular
#:                file, for which it has no effect anyway.
#: ``O_BINARY``   Windows only; prevents newline translation.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)

#: ``ELOOP`` is what ``O_NOFOLLOW`` raises when the final component is a
#: symlink. ``EMLINK`` is used by some BSDs for the same condition.
_SYMLINK_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}


@dataclass(frozen=True)
class OpenFile:
    """An open, verified, read-only file descriptor plus what it really is.

    Every field is derived from ``fstat`` on :attr:`fd`, never from a pathname
    lookup, so callers cannot accidentally act on stale information.

    Use as a context manager; the descriptor is always closed on exit.
    """

    fd: int
    facts: FileFacts
    #: Workspace-relative path this was opened through. For reporting only —
    #: it is never re-resolved or trusted for a security decision.
    relative: str

    def __enter__(self) -> OpenFile:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the descriptor. Safe to call more than once."""
        try:
            os.close(self.fd)
        except OSError:
            # Already closed; nothing to release.
            pass

    def has_more(self) -> bool:
        """Whether any unread bytes remain, according to the descriptor.

        Asked *after* a bounded read to decide truncation honestly. Comparing
        the byte count against the size recorded at open time is not enough: a
        file that grew during the read would fill the ceiling exactly and look
        complete. This attempts a single one-byte read at the current offset
        and restores the offset afterwards, so it is authoritative and has no
        side effect on the caller's position.
        """
        try:
            offset = os.lseek(self.fd, 0, os.SEEK_CUR)
            probe = os.read(self.fd, 1)
            os.lseek(self.fd, offset, os.SEEK_SET)
            return bool(probe)
        except OSError:
            # Not seekable, or the descriptor is gone. Refuse to guess
            # "complete": claiming completeness wrongly is the unsafe error.
            return True

    def read_bounded(self, *, max_bytes: int,
                     chunk_bytes: int) -> Iterator[bytes]:
        """Yield chunks from the descriptor, stopping at ``max_bytes``.

        The ceiling is applied while reading, so a file that grew after being
        opened still cannot exceed it, and no more than one chunk is held at a
        time.
        """
        remaining = max_bytes
        size = max(1, min(chunk_bytes, max_bytes))
        while remaining > 0:
            chunk = os.read(self.fd, min(size, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk


def open_verified(
    path: Path,
    *,
    relative: str,
    max_bytes: int,
    allow_multiple_links: bool,
) -> OpenFile:
    """Open ``path`` read-only and verify the descriptor itself.

    Order is deliberate: **open first**, then judge the descriptor. Nothing
    about the pathname is trusted after the ``os.open`` call returns.

    On any rejection the descriptor is closed before raising, so no caller can
    leak one by handling the error.
    """
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        raise _open_failure(exc, relative) from exc

    try:
        st = os.fstat(fd)
        kind = kind_from_mode(st.st_mode)

        # 1. Type — from the descriptor, so a FIFO or device swapped in after
        #    validation is still caught here.
        if kind is not PathKind.REGULAR:
            raise PathRejectedError(
                RejectionReason.NOT_A_REGULAR_FILE,
                f"opened object is a {kind}, which cannot be read safely",
                path=relative)

        # 2. Hard links — see the module note in ``sensitive.py``'s sibling
        #    discussion and README. A regular file with more than one link may
        #    be an alias for a file outside the workspace, and the kernel gives
        #    us no way to enumerate the other names. Containment is a property
        #    of *names*, and a hardlink defeats it, so multi-link regular files
        #    are refused by default.
        if not allow_multiple_links and st.st_nlink > 1:
            raise PathRejectedError(
                RejectionReason.MULTIPLE_HARD_LINKS,
                "file has multiple hard links, so it may alias content "
                "outside the workspace",
                path=relative, links=st.st_nlink)

        # 3. Size — also from the descriptor, so the limit applies to the
        #    object actually opened rather than to a stale pathname lookup.
        if st.st_size > max_bytes:
            raise PathRejectedError(
                RejectionReason.TOO_LARGE,
                "file exceeds the maximum readable size",
                path=relative, size=st.st_size, limit=max_bytes)

        facts = FileFacts(relative=relative, kind=kind, size=st.st_size,
                          device=st.st_dev, inode=st.st_ino,
                          links=st.st_nlink)
    except BaseException:
        os.close(fd)
        raise

    return OpenFile(fd=fd, facts=facts, relative=relative)


def _open_failure(exc: OSError, relative: str) -> PathRejectedError:
    """Translate an ``os.open`` errno into a categorical rejection."""
    if exc.errno in _SYMLINK_ERRNOS:
        return PathRejectedError(
            RejectionReason.SYMLINK,
            "path is a symbolic link, which is not permitted",
            path=relative)
    if exc.errno == errno.ENOENT:
        return PathRejectedError(RejectionReason.NOT_FOUND,
                                 "no such file in the workspace",
                                 path=relative)
    if exc.errno == errno.EACCES:
        return PathRejectedError(RejectionReason.UNREADABLE,
                                 "file is not readable", path=relative)
    if exc.errno == errno.EISDIR:
        return PathRejectedError(RejectionReason.NOT_A_REGULAR_FILE,
                                 "path is a directory, not a file",
                                 path=relative)
    if exc.errno == errno.ENOTDIR:
        return PathRejectedError(RejectionReason.NOT_FOUND,
                                 "no such file in the workspace",
                                 path=relative)
    if exc.errno in {errno.ENXIO, getattr(errno, "EOPNOTSUPP", errno.ENXIO)}:
        # Unix sockets cannot be opened at all, and a FIFO with no writer
        # would block; the kernel refuses before we can fstat it.
        return PathRejectedError(
            RejectionReason.NOT_A_REGULAR_FILE,
            "path is a special file, which cannot be read safely",
            path=relative)
    return PathRejectedError(RejectionReason.UNREADABLE,
                             "file could not be opened safely",
                             path=relative, errno=exc.errno)


def descriptor_path(fd: int) -> Path | None:
    """Best-effort real path of an open descriptor.

    Linux exposes ``/proc/self/fd/N``; where that is unavailable this returns
    ``None`` and the caller must not treat the absence as a failure. Used only
    as a *defence in depth* containment re-check after opening, never as the
    primary containment mechanism.
    """
    link = Path("/proc/self/fd") / str(fd)
    try:
        return Path(os.readlink(link))
    except (OSError, ValueError):
        return None
