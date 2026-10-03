"""WorkspaceGuard-owned create-only primitives; no tool/permission authority.

Descriptor/handle-relative access protects against unsafe path objects and
detectable substitutions. Hostile same-user/admin ancestor relocation is outside
the host boundary. See docs/architecture/filesystem-write-v1.md.
"""

from contextlib import contextmanager, ExitStack
import hashlib
import os
import secrets
import stat

from agentcore.fs.paths import PathRejectedError, RejectionReason, check_relative_form


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
            _reject()
    return {"path": relative.as_posix(), "directories": identities,
            "workspace": hashlib.sha256(guard.identity.encode("utf-8")).hexdigest()}


def _check(guard, intent, relative, identities):
    if intent["workspace"] != hashlib.sha256(guard.identity.encode("utf-8")).hexdigest():
        _reject()
    with parent(guard, relative) as (_, current):
        if current != identities or current != intent["directories"]:
            _reject()


def create(guard, intent, data, validate):
    """Return 'applied' / 'not_applied'; exceptions preserve uncertainty.

    validate is executor-owned current authority, never model arguments. Native
    publication refuses an existing name without a check/overwrite window.
    """
    if type(data) is not bytes or len(data) > guard.config.max_write_bytes:
        _reject(RejectionReason.TOO_LARGE)
    relative = relative_path(guard, intent["path"])
    with parent(guard, relative) as (handle, identities):
        _check(guard, intent, relative, identities)
        if _exists(handle, relative.name):
            return "not_applied"
        stage = ".agentcore-stage-" + secrets.token_hex(16)
        if stage.casefold() == relative.name.casefold():
            _reject()
        validate()
        published = False
        if os.name == "nt":
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
        else:
            owned = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=handle)
            try:
                offset = 0
                while offset < len(data):
                    count = os.write(owned, memoryview(data)[offset:])
                    if count <= 0:
                        raise OSError("short staging write")
                    offset += count
                os.fsync(owned)
                _check(guard, intent, relative, identities)
                validate()
                staged = os.fstat(owned)
                named = os.stat(stage, dir_fd=handle, follow_symlinks=False)
                if staged.st_nlink != 1 or (named.st_dev, named.st_ino) != (staged.st_dev, staged.st_ino):
                    _reject()
                try:
                    os.link(stage, relative.name, src_dir_fd=handle, dst_dir_fd=handle,
                            follow_symlinks=False)
                except FileExistsError:
                    return "not_applied"
                published = True
            finally:
                try:
                    named, staged = os.stat(stage, dir_fd=handle, follow_symlinks=False), os.fstat(owned)
                    if (named.st_dev, named.st_ino) == (staged.st_dev, staged.st_ino):
                        os.unlink(stage, dir_fd=handle)
                    else:
                        _reject()
                finally:
                    os.close(owned)
            os.fsync(handle)
        return "applied"


def observe(guard, intent, size):
    """Read only the final safe object; staging names never count as effects."""
    relative = relative_path(guard, intent["path"])
    with parent(guard, relative) as (handle, identities):
        _check(guard, intent, relative, identities)
        if os.name == "nt":
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
        else:
            try:
                opened = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=handle)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(opened)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != size:
                    _reject()
                chunks, remaining = [], size + 1
                while remaining:
                    chunk = os.read(opened, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
                after = os.fstat(opened)
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink) != (
                        info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink):
                    _reject()
                named = os.stat(relative.name, dir_fd=handle, follow_symlinks=False)
                if (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino):
                    _reject()
            finally:
                os.close(opened)
        _check(guard, intent, relative, identities)
        if len(data) != size:
            _reject()
        return hashlib.sha256(data).hexdigest()
