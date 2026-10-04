"""Strict guard-owned snapshot/replacement boundary, without tool authority.

Final comparison plus native rename is not a filesystem CAS. Noncooperating
same-user writers racing that final native interval are outside host ownership.
"""

from contextlib import contextmanager
import hashlib
import os
import secrets
import stat

from agentcore.fs.mutation import parent, relative_path, _check


class RevisionRejectedError(ValueError):
    def __init__(self, category="stale_revision"):
        self.category = category
        super().__init__(category)


@contextmanager
def target(guard, handle, relative):
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        opened = win.open_update_target(handle, relative.name)
        close = win.close
    elif os.name == "posix":
        opened = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, "O_CLOEXEC", 0), dir_fd=handle)
        close = os.close
    else:
        raise RevisionRejectedError("unsupported_file_metadata")
    try:
        yield opened
    finally:
        close(opened)


def facts(guard, opened, relative):
    if os.name == "nt":
        from agentcore.fs import _windows_mutation as win
        relative_path(guard, str(relative.parent / win.final_name(opened)))
        return win.update_facts(opened)
    info = os.fstat(opened)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RevisionRejectedError()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != os.geteuid() or mode & ~0o777:
        raise RevisionRejectedError("unsupported_file_metadata")
    # Linux ACL/capability/security xattrs are refused, not silently discarded.
    # Platforms without a descriptor xattr query cannot establish eligibility.
    if not hasattr(os, "listxattr") or os.listxattr(opened):
        raise RevisionRejectedError("unsupported_file_metadata")
    return {"identity": [info.st_dev, info.st_ino], "size": info.st_size,
            "metadata": {"mode": mode, "uid": info.st_uid, "gid": info.st_gid},
            "change": [info.st_mtime_ns, info.st_ctime_ns]}


def read_opened(guard, opened, relative, limit):
    before = facts(guard, opened, relative)
    size = before["size"]
    if not 0 <= size <= limit:
        raise RevisionRejectedError("update_read_too_large")
    chunks, remaining = [], size + 1
    while remaining:
        count = min(guard.config.read_chunk_bytes, remaining)
        if os.name == "nt":
            from agentcore.fs import _windows_mutation as win
            chunk = win.read(opened, count)
        else:
            chunk = os.read(opened, count)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != size or facts(guard, opened, relative) != before:
        raise RevisionRejectedError()
    return data, before


def snapshot(guard, requested, limit):
    relative = relative_path(guard, requested)
    with parent(guard, relative) as (handle, identities):
        with target(guard, handle, relative) as opened:
            data, state = read_opened(guard, opened, relative, limit)
            if os.name != "nt":
                named = os.stat(relative.name, dir_fd=handle, follow_symlinks=False)
                if [named.st_dev, named.st_ino] != state["identity"]:
                    raise RevisionRejectedError()
            state.update({"path": relative.as_posix(), "directories": identities,
                          "workspace": hashlib.sha256(guard.identity.encode()).hexdigest(),
                          "digest": hashlib.sha256(data).hexdigest(), "revision_schema": 1,
                          "read_tool": "read_file_for_update", "replace_tool": "replace_file",
                          "tool_version": "1.0.0"})
            _check(guard, state, relative, identities)
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeError:
        raise RevisionRejectedError("not_utf8_text") from None
    return text, state


def check_snapshot(guard, expected):
    try:
        _, current = snapshot(guard, expected["path"], guard.config.max_write_bytes)
        if current != expected:
            raise RevisionRejectedError()
    except Exception:
        raise RevisionRejectedError() from None


def _same_stage(handle, stage, owned):
    named, opened = os.stat(stage, dir_fd=handle, follow_symlinks=False), os.fstat(owned)
    if not stat.S_ISREG(named.st_mode) or opened.st_nlink != 1 or (
            named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise RevisionRejectedError()


def replace(guard, intent, data, validate, publication):
    """Durable attempt precedes staging. publication marks possible final effect.

    Both callbacks are executor-owned. A stale comparison before native rename
    is definite no-effect; exceptions after publication() remain uncertain.
    """
    old = intent["old"]
    relative = relative_path(guard, intent["path"])
    with parent(guard, relative) as (handle, identities):
        _check(guard, old, relative, identities)
        check_snapshot(guard, old)
        validate()
        stage = ".agentcore-stage-" + secrets.token_hex(16)
        if stage.casefold() == relative.name.casefold():
            raise RevisionRejectedError()
        if os.name == "nt":
            from agentcore.fs import _windows_mutation as win
            owned = win.create_update_stage(handle, stage, old["metadata"])
            started = False
            try:
                win.write(owned, data)
                win.preserve_update_metadata(owned, old["metadata"])
                win.flush(owned)
                check_snapshot(guard, old)
                _check(guard, old, relative, identities)
                validate()
                publication()
                started = True
                win.publish_update(owned, handle, relative.name)
                win.flush(owned)
            finally:
                try:
                    if not started or win.has_staging_name(owned, stage):
                        win.discard(owned)
                finally:
                    win.close(owned)
        else:
            owned = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=handle)
            try:
                metadata = old["metadata"]
                os.fchown(owned, metadata["uid"], metadata["gid"])
                os.fchmod(owned, metadata["mode"])
                if os.listxattr(owned):
                    raise RevisionRejectedError("unsupported_file_metadata")
                offset = 0
                while offset < len(data):
                    count = os.write(owned, memoryview(data)[offset:])
                    if count <= 0:
                        raise OSError("staging write failed")
                    offset += count
                os.fsync(owned)
                _same_stage(handle, stage, owned)
                check_snapshot(guard, old)
                _check(guard, old, relative, identities)
                validate()
                publication()
                os.replace(stage, relative.name, src_dir_fd=handle, dst_dir_fd=handle)
                os.fsync(handle)
            finally:
                try:
                    try:
                        _same_stage(handle, stage, owned)
                    except FileNotFoundError:
                        pass
                    else:
                        os.unlink(stage, dir_fd=handle)
                finally:
                    os.close(owned)
    return "applied"


def observe(guard, intent):
    _, observed = snapshot(guard, intent["path"], guard.config.max_write_bytes)
    old = intent["old"]
    return (observed["workspace"] == old["workspace"]
            and observed["directories"] == old["directories"]
            and observed["metadata"] == old["metadata"]
            and observed["size"] == intent["size"]
            and observed["digest"] == intent["content_digest"])
