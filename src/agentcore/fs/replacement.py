"""Native Windows snapshot/replacement boundary, without tool authority.

Unsupported hosts refuse before traversal, staging or publication.

Final comparison plus native rename is not a filesystem CAS. Noncooperating
same-user writers racing that final native interval are outside host ownership.
"""

from contextlib import contextmanager
import hashlib
import secrets

from agentcore.fs.mutation import parent, relative_path, _check, _require_file_publication


class RevisionRejectedError(ValueError):
    def __init__(self, category="stale_revision"):
        self.category = category
        super().__init__(category)


@contextmanager
def target(guard, handle, relative):
    from agentcore.fs import _windows_mutation as win
    opened = win.open_update_target(handle, relative.name)
    try:
        yield opened
    finally:
        win.close(opened)


def facts(guard, opened, relative):
    from agentcore.fs import _windows_mutation as win
    relative_path(guard, str(relative.parent / win.final_name(opened)))
    return win.update_facts(opened)


def read_opened(guard, opened, relative, limit):
    from agentcore.fs import _windows_mutation as win
    before = facts(guard, opened, relative)
    size = before["size"]
    if not 0 <= size <= limit:
        raise RevisionRejectedError("update_read_too_large")
    chunks, remaining = [], size + 1
    while remaining:
        count = min(guard.config.read_chunk_bytes, remaining)
        chunk = win.read(opened, count)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != size or facts(guard, opened, relative) != before:
        raise RevisionRejectedError()
    return data, before


def snapshot(guard, requested, limit):
    _require_file_publication()
    relative = relative_path(guard, requested)
    with parent(guard, relative) as (handle, identities):
        with target(guard, handle, relative) as opened:
            data, state = read_opened(guard, opened, relative, limit)
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
    _require_file_publication()
    try:
        _, current = snapshot(guard, expected["path"], guard.config.max_write_bytes)
        if current != expected:
            raise RevisionRejectedError()
    except Exception:
        raise RevisionRejectedError() from None


def replace(guard, intent, data, validate, publication):
    """Durable attempt precedes staging. publication marks possible final effect.

    Both callbacks are executor-owned. A stale comparison before native rename
    is definite no-effect; exceptions after publication() remain uncertain.
    """
    _require_file_publication()
    old = intent["old"]
    relative = relative_path(guard, intent["path"])
    with parent(guard, relative) as (handle, identities):
        _check(guard, old, relative, identities)
        check_snapshot(guard, old)
        validate()
        stage = ".agentcore-stage-" + secrets.token_hex(16)
        if stage.casefold() == relative.name.casefold():
            raise RevisionRejectedError()
        from agentcore.fs import _windows_mutation as win
        owned = win.create_update_stage(handle, stage, old["metadata"])
        started = False
        try:
            win.write(owned, data)
            win.flush(owned)
            # Keep the old target pinned through the final authority checks.
            with target(guard, handle, relative):
                check_snapshot(guard, old)
                _check(guard, old, relative, identities)
                validate()
                publication()
            started = True
            win.publish_update(owned, handle, relative.name)
            # Restore only after a successful rename, through the same handle.
            win.preserve_update_metadata(owned, old["metadata"])
            win.flush(owned)
        finally:
            try:
                if not started or win.has_staging_name(owned, stage):
                    win.discard(owned)
            finally:
                win.close(owned)
    return "applied"


def observe(guard, intent):
    _require_file_publication()
    # Intent contains no action-specific published-stage identity or receipt.
    return False
