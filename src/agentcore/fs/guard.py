"""``WorkspaceGuard`` — the single entry point future filesystem tools use.

A future ``read_file`` should need exactly this::

    with guard.open_file(args["path"]) as handle:     # steps 1-6 below
        data, truncated = guard.read_from(handle)     # step 7
        facts = handle.facts                          # from fstat(fd)

or, for the simple case, ``data, facts, truncated = guard.read_path(path)``.

Tools must not re-implement canonicalisation, containment, sensitive-file
checks, special-file checks or bounds.

The authoritative security ordering
-----------------------------------
Every filesystem read goes through these steps, in this order, in exactly
one place (:meth:`WorkspaceGuard.open_file`). A tool must not re-implement or
reorder any of them::

    1. validate relative form      cheap, lexical, no filesystem access
    2. resolve / canonicalise      symlinks collapsed
    3. containment                 component-aware, against the resolved target
    4. sensitive-path policy       on the requested name AND effective target
    5. safe open                   O_RDONLY | O_NOFOLLOW | O_NONBLOCK
    6. descriptor verification     type, link count and size from fstat(fd)
    7. bounded read                ceiling applied while reading

Steps 1–4 concern *pathnames*; steps 5–7 concern the *object actually opened*.
That split is the point. Anything decided in 1–4 can be invalidated by a
concurrent rename, so nothing from those steps is used as the final authority
on what is read — see :mod:`agentcore.fs.opened`.

TOCTOU — what this guarantees and what it does not
--------------------------------------------------
Type, size and link count are read from ``fstat`` on the **open descriptor**,
so they cannot be stale: they describe the object being read. An earlier design
compared ``(st_dev, st_ino)`` captured before opening; that was removed because
inode numbers are reused. On ext4 in this project's own sandbox, deleting a
validated file and recreating another at the same path **reused the inode on
the first attempt**, so the comparison silently accepted the replacement. The
same test on tmpfs never reuses an inode and would have reported success — a
good illustration of why the check was unsound.

TOCTOU is therefore **reduced, not eliminated**. What is still not guaranteed:

* *Which object the pathname resolved to* at the instant of the open. A
  concurrent attacker who can replace a **parent directory** between resolution
  and open can still redirect it. A complete defence requires ``openat``
  traversal from a directory descriptor, component by component, which is
  POSIX-specific; it is deliberately not built here.
* Consequently, a file legitimately replaced between steps 3 and 5 is read as
  whatever now exists at that path — but only ever a regular, single-linked,
  size-bounded file that the descriptor itself vouches for.

These limits are asserted by tests so they cannot be quietly overstated later.

Create-only mutation uses the separate guard-owned descriptor/handle-relative
boundary in ``fs.mutation``. Its explicit host trust boundary and durability
limits are documented in ``docs/architecture/filesystem-write-v1.md``; it does
not inherit this read path's pathname-only containment as its write authority.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.opened import OpenFile, descriptor_path, open_verified
from agentcore.fs.paths import (
    BoundedNameSelector,
    DirectoryEntry,
    DirectoryListing,
    FileFacts,
    Inspection,
    LinkTarget,
    PathKind,
    PathRejectedError,
    RejectionReason,
    canonical_workspace,
    check_relative_form,
    classify,
    contain,
    kind_from_lstat,
    symlinked_component,
    workspace_identity,
)
from agentcore.fs.sensitive import DEFAULT_SENSITIVE_POLICY, SensitivePathPolicy


def _mtime_iso(st_mtime: float) -> str:
    """Render an ``st_mtime`` float as a timezone-aware UTC ISO-8601 string.

    Uses the filesystem modification time, never the agent event clock, and
    returns a string so no raw epoch float reaches the model or state.
    """
    return datetime.fromtimestamp(st_mtime, tz=timezone.utc).isoformat()


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

    def prepare_create(self, requested: Any) -> dict[str, Any]:
        from agentcore.fs.mutation import prepare
        return prepare(self, requested)

    def create_file(self, intent: Any, data: bytes, validate: Any) -> str:
        from agentcore.fs.mutation import create
        return create(self, intent, data, validate)

    def observe_create(self, intent: Any, size: int) -> str | None:
        from agentcore.fs.mutation import observe
        return observe(self, intent, size)

    # -- containment -------------------------------------------------------
    def resolve_relative(self, requested: Any) -> SafePath:
        """Validate a requested path and return a contained absolute path.

        Steps 1–4 of the ordering in the module docstring. This settles
        *pathname* questions only; it deliberately makes no claim about the
        object that will eventually be opened.
        """
        relative = check_relative_form(
            requested,
            max_length=self._config.max_path_length,
            max_depth=self._config.max_path_depth,
        )

        # 4a. Sensitive policy on the requested name, before any filesystem
        #     access, so a denied credential file is not even stat'd.
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
                    "path crosses a symbolic link or reparse point, which is "
                    "not permitted",
                    path=link)

        # 2 + 3. Canonicalise, then containment against the resolved target.
        resolved = contain(self._root, relative)

        # 4b. Sensitive policy on the EFFECTIVE TARGET.
        #
        #     Only reachable when symlinks are permitted, and it is exactly the
        #     case a name-only check misses: ``notes.txt -> hidden/.env``
        #     resolves inside the workspace and passes containment, but the
        #     bytes it would yield are a credential file. Policy must follow
        #     the content, not the label.
        if self._config.deny_sensitive_paths:
            effective = resolved.relative_to(self._root).as_posix()
            if effective != relative.as_posix():
                reason = self._sensitive.reason(effective)
                if reason is not None:
                    # Deliberately no ``target=effective`` detail: the resolved
                    # location of a protected sensitive target is itself
                    # disclosure, and rejection details flow into logs and
                    # persisted run state (Task 39 F-04). The categorical
                    # reason plus the *requested* path are all a caller needs.
                    raise PathRejectedError(
                        RejectionReason.SENSITIVE_TARGET,
                        f"refused: path resolves to a sensitive file ({reason})",
                        path=relative.as_posix())

        return SafePath(absolute=resolved, relative=relative.as_posix())

    # -- opening (steps 5-6) -----------------------------------------------
    def open_file(self, requested: Any) -> OpenFile:
        """The authoritative entry point: request → open, verified descriptor.

        Runs the whole ordering from the module docstring and hands back an
        :class:`~agentcore.fs.opened.OpenFile` whose ``facts`` come from
        ``fstat`` on the descriptor. A future ``read_file`` should call this
        and nothing else.

        The result is a context manager and **must** be closed::

            with guard.open_file("src/app.py") as handle:
                data, truncated = guard.read_from(handle)
        """
        safe = self.resolve_relative(requested)
        return self.open_safe(safe)

    def open_safe(self, safe: SafePath) -> OpenFile:
        """Steps 5–6 for an already-validated :class:`SafePath`."""
        handle = open_verified(
            safe.absolute,
            relative=safe.relative,
            max_bytes=self._config.max_file_bytes,
            allow_multiple_links=self._config.allow_multiple_hard_links,
        )
        # Defence in depth: on Linux, confirm the descriptor really resolves
        # inside the workspace. This catches a parent-directory swap that
        # happened before the open, which pathname containment cannot see.
        # Absent /proc this returns None and containment rests on steps 2-3.
        try:
            actual = descriptor_path(handle.fd)
            if actual is not None and not actual.is_relative_to(self._root):
                raise PathRejectedError(
                    RejectionReason.ESCAPES_WORKSPACE,
                    "opened file lies outside the workspace",
                    path=safe.relative)
        except BaseException:
            handle.close()
            raise
        return handle

    def stat_path(self, safe: SafePath) -> FileFacts:
        """Advisory pathname inspection, for listing rather than reading.

        Explicitly *not* a security gate for reads: the answer may be stale by
        the time it is used. Reading must go through :meth:`open_file`, which
        judges the descriptor.
        """
        kind = classify(safe.absolute)
        if kind is PathKind.MISSING:
            raise PathRejectedError(RejectionReason.NOT_FOUND,
                                    "no such file in the workspace",
                                    path=safe.relative)
        st = safe.absolute.lstat()
        return FileFacts(relative=safe.relative, kind=kind, size=st.st_size,
                         device=st.st_dev, inode=st.st_ino,
                         links=st.st_nlink)

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
                         device=st.st_dev, inode=st.st_ino,
                         links=st.st_nlink)

    def root_path(self) -> SafePath:
        """The workspace root itself as a :class:`SafePath`."""
        return SafePath(absolute=self._root, relative=".")

    def resolve_directory(self, requested: Any) -> SafePath:
        """Validate a requested directory path, allowing the workspace root.

        ``resolve_relative`` refuses ``"."`` and ``""`` because a *file* read
        must name something. Listing the workspace root is the ordinary case,
        so it is accepted here and mapped onto :meth:`root_path`. Every other
        value goes through the normal validation unchanged.
        """
        if isinstance(requested, Path):
            requested = str(requested)
        if isinstance(requested, str) and requested.strip() in {
                ".", "./", "", "/"} and requested.strip() != "/":
            return self.root_path()
        return self.resolve_relative(requested)

    # -- inspection (metadata only, never traversal or content) ------------

    def inspect_path(self, requested: Any) -> Inspection:
        """Describe one workspace entry without reading it or following links.

        Metadata only. This never opens a file, never reads a byte, never
        executes anything, never descends into a directory and never follows a
        final-component link: a symlink or Windows reparse point is reported
        as ``kind=symlink`` with a categorical :class:`LinkTarget`, never as
        whatever it points at.

        The root spelling ``"."`` / ``"./"`` is accepted here and mapped onto
        the canonical workspace root, exactly as :meth:`resolve_directory`
        does; ``check_relative_form`` itself is unchanged, so ``read_file``
        keeps its existing ``"."`` rejection.

        Ordering:

        1. root spelling, or lexical validation via ``check_relative_form``;
        2. sensitive-name policy, before any filesystem observation, so a
           sensitive path is refused identically whether or not it exists
           (no existence/metadata oracle);
        3. parent components resolved through the normal guard machinery —
           a link in a parent, an escaping parent or a sensitive parent is
           refused before the final component is observed;
        4. final component classified with ``lstat`` (never followed); a
           reparse point counts as a symlink;
        5. a link whose effective target is sensitive is refused with
           ``SENSITIVE_TARGET`` and the target is never disclosed.

        TOCTOU contract: this is a metadata snapshot, not a read. A concurrent
        rename can make the snapshot describe a different object than existed
        earlier, but no content is exposed, no unsafe target is exposed, and a
        link swapped in is reported as a link, never followed. The hardlink
        *read* refusal does not apply here: nothing is read.
        """
        if isinstance(requested, Path):
            requested = requested.as_posix()
        if isinstance(requested, str) and requested.strip() in {
                ".", "./", ""}:
            return self._inspect_child(".", self._root)

        relative = check_relative_form(
            requested,
            max_length=self._config.max_path_length,
            max_depth=self._config.max_path_depth,
        )

        # Sensitive policy on the requested name, before any filesystem
        # access — mirroring resolve_relative, so inspection cannot answer
        # "does a credential file exist here".
        if self._config.deny_sensitive_paths:
            reason = self._sensitive.reason(relative)
            if reason is not None:
                raise PathRejectedError(RejectionReason.SENSITIVE,
                                        f"refused: {reason}",
                                        path=relative.as_posix())

        parts = relative.parts
        if len(parts) > 1:
            parent = self.resolve_directory(
                PurePosixPath(*parts[:-1]).as_posix())
            self.require_directory(parent)
        else:
            parent = self.root_path()
        child = parent.absolute / parts[-1]
        return self._inspect_child(relative.as_posix(), child)

    def _inspect_child(self, relative: str, child: Path) -> Inspection:
        """Classify one entry and build its :class:`Inspection`.

        ``lstat`` only: the object is never opened or followed. Precise error
        mapping replaces ``classify``'s coarse ``OTHER`` fold: a missing
        object is ``NOT_FOUND``, a permission failure is ``UNREADABLE`` and
        any other stat failure is ``UNREADABLE`` with its errno.
        """
        try:
            st = child.lstat()
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise PathRejectedError(RejectionReason.NOT_FOUND,
                                    "no such file in the workspace",
                                    path=relative) from exc
        except PermissionError as exc:
            raise PathRejectedError(RejectionReason.UNREADABLE,
                                    "path is not readable",
                                    path=relative) from exc
        except OSError as exc:
            raise PathRejectedError(RejectionReason.UNREADABLE,
                                    "path could not be inspected",
                                    path=relative, errno=exc.errno) from exc

        kind = kind_from_lstat(st)
        if kind is PathKind.SYMLINK:
            if self._config.deny_sensitive_paths and \
                    self._links_to_sensitive(child, kind):
                raise PathRejectedError(
                    RejectionReason.SENSITIVE_TARGET,
                    "refused: path resolves to a sensitive file",
                    path=relative)
            return Inspection(relative=relative, kind=kind,
                              link_target=self._link_target(child))

        size = st.st_size if kind is PathKind.REGULAR else None
        links = st.st_nlink if kind is PathKind.REGULAR else None
        modified_at = (_mtime_iso(st.st_mtime)
                       if kind in {PathKind.REGULAR, PathKind.DIRECTORY}
                       else None)
        return Inspection(relative=relative, kind=kind, size=size,
                          links=links, modified_at=modified_at)

    # -- listing (discovery, never traversal) ------------------------------
    def list_directory(self, safe: SafePath) -> DirectoryListing:
        """List the immediate children of a validated directory.

        Discovery only. This never opens a regular file, never follows a
        symlink to describe it, and never descends into a subdirectory.

        Each entry is classified with ``lstat`` (via
        :func:`~agentcore.fs.paths.classify`), so a symlink reports as a
        symlink rather than as whatever it points at. Sensitive entries keep
        their type but lose their name, because a credential file's *name* is
        itself disclosure.

        Memory is bounded, not merely the output. Names stream out of
        ``os.scandir`` into a :class:`~agentcore.fs.paths.BoundedNameSelector`
        holding at most ``max_directory_entries + 1`` of them, so a directory
        with millions of entries never materialises millions of strings. The
        selector returns exactly the prefix a full sort would have produced —
        see its docstring for why that is equivalent.

        ``truncated`` comes from the retained extra slot, so "exactly the cap"
        and "more than the cap" stay distinguishable; it is never inferred from
        ``len(entries) == limit``.

        Ordering is by byte value of the entry name, which is deterministic,
        locale-independent and case-sensitive on every platform. Sensitive
        entries are ordered by their real name before redaction, so a repeated
        listing of an unchanged directory is stable.
        """
        self.require_directory(safe)
        limit = self._config.max_directory_entries
        selector = BoundedNameSelector(limit)

        try:
            # scandir is lazy and the selector is bounded, so the whole
            # pipeline is streaming. Only names are touched here; nothing is
            # opened, stat'ed or followed at this stage.
            with os.scandir(safe.absolute) as scanner:
                for entry in scanner:
                    selector.add(entry.name)
        except PermissionError as exc:
            raise PathRejectedError(RejectionReason.UNREADABLE,
                                    "directory is not readable",
                                    path=safe.relative) from exc
        except OSError as exc:
            raise PathRejectedError(RejectionReason.UNREADABLE,
                                    "directory could not be listed",
                                    path=safe.relative,
                                    errno=exc.errno) from exc

        names, truncated = selector.result()
        base = "" if safe.relative == "." else f"{safe.relative}/"
        entries: list[DirectoryEntry] = []
        sensitive_count = 0

        for name in names:
            child = safe.absolute / name
            kind = classify(child)
            relative = f"{base}{name}"
            link_target = (self._link_target(child)
                           if kind is PathKind.SYMLINK else None)
            is_sensitive = (
                self._config.deny_sensitive_paths
                and (self._sensitive.is_sensitive(relative)
                     # A symlink whose *target* is credential material is
                     # withheld too. Its own name may look innocuous, and
                     # revealing "this link points at something you may not
                     # read" is itself a hint about where secrets live. Reads
                     # through it are already refused (SENSITIVE_TARGET); this
                     # keeps the listing consistent with that.
                     or self._links_to_sensitive(child, kind)))
            if is_sensitive:
                sensitive_count += 1
            entries.append(DirectoryEntry(
                name=None if is_sensitive else name,
                kind=kind,
                sensitive=is_sensitive,
                link_target=link_target,
            ))

        return DirectoryListing(relative=safe.relative,
                                entries=tuple(entries),
                                truncated=truncated, limit=limit,
                                sensitive_count=sensitive_count)

    def _links_to_sensitive(self, child: Path, kind: PathKind) -> bool:
        """Whether a symlink resolves to credential material inside the root.

        Mirrors the effective-target rule ``resolve_relative`` applies to
        reads, so a listing cannot hint at a secret that a read would refuse.
        Only the *workspace-relative* form of the target is tested; an
        external target is already reported as ``EXTERNAL`` without its path.
        """
        if kind is not PathKind.SYMLINK:
            return False
        try:
            resolved = child.resolve()
            if not resolved.is_relative_to(self._root):
                return False
            return self._sensitive.is_sensitive(
                resolved.relative_to(self._root).as_posix())
        except (OSError, RuntimeError, ValueError):
            return False

    def _link_target(self, child: Path) -> LinkTarget:
        """Classify where a symlink points, without disclosing the target.

        The link is resolved only to decide internal/external/broken. The
        resolved path is discarded immediately: it is never returned, so an
        external absolute path cannot leak through a listing. Nothing is
        opened and no directory is entered.
        """
        try:
            resolved = child.resolve()
        except (OSError, RuntimeError):
            # RuntimeError covers a symlink loop on some Python versions.
            return LinkTarget.UNKNOWN
        try:
            if not resolved.exists():
                return LinkTarget.BROKEN
        except OSError:
            return LinkTarget.UNKNOWN
        return (LinkTarget.INTERNAL if resolved.is_relative_to(self._root)
                else LinkTarget.EXTERNAL)

    # -- bounded reading (step 7) ------------------------------------------
    def iter_from(self, handle: OpenFile) -> Iterator[bytes]:
        """Yield bounded chunks from an already-opened, verified descriptor."""
        return handle.read_bounded(
            max_bytes=self._config.max_file_bytes,
            chunk_bytes=self._config.effective_chunk_bytes,
        )

    def read_from(self, handle: OpenFile) -> tuple[bytes, bool]:
        """Read an open descriptor, bounded. Returns ``(data, truncated)``.

        ``truncated`` says the ceiling stopped the read before the file ended,
        so a caller reports a prefix explicitly rather than silently.

        Truncation is decided by asking the **descriptor**, at the moment the
        read finished, whether anything remains — not by comparing against the
        size captured at open time. A file that grows while being read would
        otherwise fill the ceiling exactly and still be reported complete,
        which is the one thing a bounded reader must never do.
        """
        data = b"".join(self.iter_from(handle))
        if len(data) < self._config.max_file_bytes:
            # The read ended because the file ended.
            return data, False
        return data, handle.has_more()

    def read_path(self, requested: Any) -> tuple[bytes, FileFacts, bool]:
        """Convenience: validate, open, read and close in one call.

        Returns ``(data, facts, truncated)``. The descriptor is always closed,
        including on failure.
        """
        with self.open_file(requested) as handle:
            data, truncated = self.read_from(handle)
            return data, handle.facts, truncated

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
            "multiple_hard_links_allowed":
                self._config.allow_multiple_hard_links,
        }
