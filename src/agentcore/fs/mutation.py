"""WorkspaceGuard-owned create-only primitives; no tool/permission authority.

File publication requires Windows handle semantics; POSIX file creation is
refused before physical entry. Directory creation retains its separate contract.
See docs/architecture/filesystem-write-v1.md.
"""

from contextlib import contextmanager, ExitStack
import hashlib
import os
import secrets
import stat
import sys

from agentcore.fs.paths import PathRejectedError, RejectionReason, check_relative_form


class DestinationExistsError(PathRejectedError):
    """Trusted preflight proves that no create attempt should be admitted."""

    def __init__(self):
        super().__init__(RejectionReason.CHANGED_UNDERFOOT, "existing destination refused")


def _reject(reason=RejectionReason.CHANGED_UNDERFOOT):
    raise PathRejectedError(reason, "filesystem create refused")


def content_bytes(content, limit):
    # Bound characters before allocating their encoded representation.
    if type(content) is not str or len(content) > limit:
        _reject(RejectionReason.TOO_LARGE)
    try:
        data = content.encode("utf-8", "strict")
    except UnicodeError:
        _reject()
    if len(data) > limit:
        _reject(RejectionReason.TOO_LARGE)
    return data


def relative_path(guard, requested):
    relative = check_relative_form(requested, max_length=min(4096, guard.config.max_path_length),
                                   max_depth=min(64, guard.config.max_path_depth))
    # Mutation always enforces the host sensitive policy and no-follow posture,
    # even if an operator relaxed the read-only filesystem configuration.
    if guard.sensitive_policy.reason(relative) is not None:
        _reject(RejectionReason.SENSITIVE)
    if os.name == "nt":
        reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"}
        if any(part != part.rstrip(" .") or any(char in part for char in '<>:"|?*')
               or part.split(".")[0].upper() in reserved for part in relative.parts):
            _reject()
    return relative


@contextmanager
def parent(guard, relative):
    with ExitStack() as stack:
        identities = []
        if os.name == "nt":
            from agentcore.fs import _windows_mutation as win
            handle = win.root(guard.root)
            stack.callback(win.close, handle)
            canonical_parts = []
            for name in (None, *relative.parts[:-1]):
                if name is not None:
                    handle = win.open_child(handle, name, directory=True)
                    stack.callback(win.close, handle)
                attrs, identity, _, _ = win.facts(handle)
                if attrs & 0x400 or not attrs & 0x10:
                    _reject(RejectionReason.SYMLINK)
                if name is not None:
                    # NT also resolves DOS short-name aliases. Check the actual
                    # opened components, not only the model's lexical spelling.
                    canonical_parts.append(win.final_name(handle))
                    relative_path(guard, "/".join((*canonical_parts, relative.name)))
                identities.append(list(identity))
        elif os.name == "posix":
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            handle = os.open(guard.root, flags)
            stack.callback(os.close, handle)
            for name in (None, *relative.parts[:-1]):
                if name is not None:
                    handle = os.open(name, flags, dir_fd=handle)
                    stack.callback(os.close, handle)
                info = os.fstat(handle)
                if not stat.S_ISDIR(info.st_mode):
                    _reject(RejectionReason.NOT_A_DIRECTORY)
                identities.append([info.st_dev, info.st_ino])
        else:
            _reject()
        yield handle, identities


def _exists(handle, name):
    try:
        if os.name == "nt":
            from agentcore.fs import _windows_mutation as win
            child = win.open_child(handle, name)
            win.close(child)
        else:
            os.stat(name, dir_fd=handle, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def prepare(guard, requested):
    relative = relative_path(guard, requested)
    with parent(guard, relative) as (handle, identities):
        if _exists(handle, relative.name):
            raise DestinationExistsError()
    return {"path": relative.as_posix(), "directories": identities,
            "workspace": hashlib.sha256(guard.identity.encode("utf-8")).hexdigest()}


def _check(guard, intent, relative, identities):
    if intent["workspace"] != hashlib.sha256(guard.identity.encode("utf-8")).hexdigest():
        _reject()
    with parent(guard, relative) as (_, current):
        if current != identities or current != intent["directories"]:
            _reject()


def _require_file_publication():
    # Unnamed Linux stages bind the source inode, but cannot pin its ancestry.
    if os.name != "nt" or sys.platform != "win32":
        _reject(RejectionReason.UNSUPPORTED_PUBLICATION)


def create(guard, intent, data, validate):
    """Return 'applied' / 'not_applied'; exceptions preserve uncertainty.

    validate is executor-owned current authority, never model arguments. Native
    publication refuses an existing name without a check/overwrite window.
    """
    if type(data) is not bytes or len(data) > guard.config.max_write_bytes:
        _reject(RejectionReason.TOO_LARGE)
    relative = relative_path(guard, intent["path"])
    _require_file_publication()
    with parent(guard, relative) as (handle, identities):
        _check(guard, intent, relative, identities)
        if _exists(handle, relative.name):
            return "not_applied"
        stage = ".agentcore-stage-" + secrets.token_hex(16)
        if stage.casefold() == relative.name.casefold():
            _reject()
        validate()
        published = False
        from agentcore.fs import _windows_mutation as win
        owned = win.open_child(handle, stage, create=True)
        publication_started = False
        try:
            win.write(owned, data)
            win.flush(owned)
            _check(guard, intent, relative, identities)
            validate()
            publication_started = True
            try:
                win.publish(owned, handle, relative.name)
            except FileExistsError:
                publication_started = False
                return "not_applied"
            published = True
            win.flush(owned)
        finally:
            try:
                if not published:
                    # Never delete a final file after an ambiguous rename.
                    # If the kernel still identifies this handle as our
                    # staging object, cleanup can safely discard that object.
                    if not publication_started or win.has_staging_name(owned, stage):
                        win.discard(owned)
            finally:
                win.close(owned)
        return "applied"


def create_directory(guard, intent, validate):
    """Create one final directory relative to a verified parent, without markers.

    validate marks possible physical entry immediately before native create.
    Exceptions after it retain uncertainty, including post-check/fsync failures.
    No cleanup removes a possibly created final directory.
    """
    relative = relative_path(guard, intent["path"])
    with parent(guard, relative) as (handle, identities):
        _check(guard, intent, relative, identities)
        if _exists(handle, relative.name):
            return "not_applied"
        validate()
        if os.name == "nt":
            from agentcore.fs import _windows_mutation as win
            try:
                owned = win.open_child(handle, relative.name, directory=True, create=True)
            except FileExistsError:
                return "not_applied"
            try:
                attrs, identity, _, _ = win.facts(owned)
                if attrs & 0x400 or not attrs & 0x10:
                    _reject()
                # Inspect the returned create handle, then prove that the final
                # name still denotes that same directory under the parent.
                named = win.open_child(handle, relative.name, directory=True)
                try:
                    current_attrs, current_identity, _, _ = win.facts(named)
                    if current_attrs & 0x400 or not current_attrs & 0x10 or identity != current_identity:
                        _reject()
                finally:
                    win.close(named)
                _check(guard, intent, relative, identities)
            finally:
                win.close(owned)
            # Windows directory handles have no supported parent-fsync promise.
        else:
            try:
                os.mkdir(relative.name, mode=0o700, dir_fd=handle)
            except FileExistsError:
                return "not_applied"
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            owned = os.open(relative.name, flags, dir_fd=handle)
            try:
                info = os.fstat(owned)
                named = os.stat(relative.name, dir_fd=handle, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(named.st_mode) or (
                        info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                    _reject()
                _check(guard, intent, relative, identities)
                os.fsync(owned)
                os.fsync(handle)
            finally:
                os.close(owned)
        return "applied"


def observe(guard, intent, size):
    """Read only the final safe object; staging names never count as effects."""
    relative = relative_path(guard, intent["path"])
    # Legacy POSIX attempts lack verified publication identity/ancestry too.
    _require_file_publication()
    with parent(guard, relative) as (handle, identities):
        _check(guard, intent, relative, identities)
        from agentcore.fs import _windows_mutation as win
        try:
            opened = win.open_child(handle, relative.name, read=True)
        except FileNotFoundError:
            return None
        try:
            attrs, _, actual_size, links = win.facts(opened)
            if attrs & (0x400 | 0x10) or links != 1 or actual_size != size or not win.regular(opened):
                _reject()
            data = win.read(opened, size + 1)
            if win.facts(opened)[2:] != (actual_size, links):
                _reject()
        finally:
            win.close(opened)
        _check(guard, intent, relative, identities)
        if len(data) != size:
            _reject()
        return hashlib.sha256(data).hexdigest()
