"""Guard-level tests for ``WorkspaceGuard.inspect_path`` (Task 41.2).

``inspect_path`` describes one workspace entry via ``lstat`` only: it never
opens a file, never reads a byte, never follows a final-component link, and
never reveals a resolved target. These tests pin that contract on POSIX; the
Windows half lives in ``tests/test_fs_windows.py``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import socket

import pytest

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import (
    LinkTarget,
    PathKind,
    PathRejectedError,
    RejectionReason,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    return other


def guard(root, **config):
    return WorkspaceGuard(root, config=FilesystemConfig(**config))


# ===========================================================================
# Regular / directory / root
# ===========================================================================

def test_regular_file_fields(ws):
    (ws / "notes.txt").write_bytes(b"hello world\n")
    r = guard(ws).inspect_path("notes.txt")
    assert r.kind is PathKind.REGULAR
    assert r.size == 12
    assert r.links == 1
    assert r.link_target is None
    assert r.modified_at is not None
    assert set(r.to_dict()) == {"path", "kind", "size", "links", "modified_at"}


def test_directory_fields(ws):
    (ws / "src").mkdir()
    r = guard(ws).inspect_path("src")
    assert r.kind is PathKind.DIRECTORY
    assert r.size is None and r.links is None and r.link_target is None
    assert r.modified_at is not None
    assert "size" not in r.to_dict() and "links" not in r.to_dict()


def test_root_spelling_maps_to_workspace_root(ws):
    g = guard(ws)
    for spelling in (".", "./", ""):
        r = g.inspect_path(spelling)
        assert r.relative == "."
        assert r.kind is PathKind.DIRECTORY
        assert r.modified_at is not None


def test_root_spelling_does_not_change_read_file_contract(ws):
    """``inspect(".")`` works without making ``read_file(".")`` legal."""
    g = guard(ws)
    assert g.inspect_path(".").kind is PathKind.DIRECTORY
    # check_relative_form still rejects "." for reads:
    with pytest.raises(PathRejectedError) as exc:
        g.resolve_relative(".")
    assert exc.value.reason is RejectionReason.EMPTY


# ===========================================================================
# Missing / unreadable
# ===========================================================================

def test_missing_path_is_not_found(ws):
    with pytest.raises(PathRejectedError) as exc:
        guard(ws).inspect_path("nope.txt")
    assert exc.value.reason is RejectionReason.NOT_FOUND


def test_parent_is_a_file_reports_not_a_directory(ws):
    (ws / "afile.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PathRejectedError) as exc:
        guard(ws).inspect_path("afile.txt/child")
    assert exc.value.reason is RejectionReason.NOT_A_DIRECTORY


@POSIX_ONLY
def test_unreadable_path_maps_to_unreadable(ws):
    """An unsearchable parent makes the final ``lstat`` fail with EACCES.

    ``lstat`` needs no permission on the file itself, so the deterministic
    POSIX "unreadable" condition for metadata inspection is a parent directory
    without search permission — the precise case the guard maps to
    ``UNREADABLE`` instead of folding into ``OTHER``.
    """
    if os.geteuid() == 0:
        pytest.skip("running as root; permission bits are not enforced")
    locked = ws / "locked"
    locked.mkdir()
    (locked / "inside.txt").write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        with pytest.raises(PathRejectedError) as exc:
            guard(ws).inspect_path("locked/inside.txt")
        assert exc.value.reason is RejectionReason.UNREADABLE
    finally:
        locked.chmod(0o755)


# ===========================================================================
# Sensitive names: no existence/metadata oracle
# ===========================================================================

def test_sensitive_names_rejected_before_observation(ws):
    g = guard(ws)
    (ws / ".env").write_text("k=v", encoding="utf-8")
    (ws / "ID_RSA").write_text("key", encoding="utf-8")
    for name in (".env", ".env.production", "ID_RSA", "secrets.json", ".ssh",
                 "absent.key"):
        with pytest.raises(PathRejectedError) as exc:
            g.inspect_path(name)
        assert exc.value.reason is RejectionReason.SENSITIVE
        # never an existence/flavour signal
        assert "no such" not in exc.value.message


def test_sensitive_names_are_not_an_existence_oracle(ws):
    """Existing and missing sensitive names are refused identically.

    Both an existing and a missing credential name produce the same categorical
    ``SENSITIVE`` rejection; neither message carries an existence signal
    ("no such file"), so inspection cannot answer "does a credential live
    here" by comparing outcomes.
    """
    g = guard(ws)
    (ws / "id_rsa").write_text("key", encoding="utf-8")
    with pytest.raises(PathRejectedError) as exc:
        g.inspect_path("id_rsa")
    with pytest.raises(PathRejectedError) as exc2:
        g.inspect_path("id_dsa")  # same denylist rule, does not exist
    assert exc.value.reason is exc2.value.reason is RejectionReason.SENSITIVE
    assert "no such" not in exc.value.message
    assert "no such" not in exc2.value.message


# ===========================================================================
# Links: categorical, never followed, never revealed
# ===========================================================================

@POSIX_ONLY
def test_internal_external_broken_symlinks_are_categorical(ws, outside):
    (ws / "real.txt").write_text("x", encoding="utf-8")
    (ws / "inside").symlink_to(ws / "real.txt")
    (ws / "escape").symlink_to(outside / "secret.txt")
    (ws / "broken").symlink_to(ws / "nope.txt")
    g = guard(ws)

    assert g.inspect_path("inside").link_target is LinkTarget.INTERNAL
    assert g.inspect_path("escape").link_target is LinkTarget.EXTERNAL
    assert g.inspect_path("broken").link_target is LinkTarget.BROKEN

    for name in ("inside", "escape", "broken"):
        r = g.inspect_path(name)
        assert r.kind is PathKind.SYMLINK
        assert r.size is None and r.links is None and r.modified_at is None

    blob = json.dumps([g.inspect_path(n).to_dict()
                       for n in ("inside", "escape", "broken")])
    assert str(outside) not in blob
    assert "secret.txt" not in blob


@POSIX_ONLY
def test_innocuous_symlink_to_sensitive_target(ws):
    hidden = ws / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("k=v", encoding="utf-8")
    (ws / "friendly").symlink_to(hidden / ".env")
    g = guard(ws)
    with pytest.raises(PathRejectedError) as exc:
        g.inspect_path("friendly")
    assert exc.value.reason is RejectionReason.SENSITIVE_TARGET
    blob = str(exc.value) + repr(exc.value.details)
    assert "hidden" not in blob
    assert ".env" not in blob
    assert "target" not in exc.value.details


@POSIX_ONLY
def test_parent_symlink_rejected(ws, outside):
    (ws / "sub").symlink_to(outside)
    with pytest.raises(PathRejectedError) as exc:
        guard(ws).inspect_path("sub/secret.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@POSIX_ONLY
def test_external_symlink_not_followed_even_when_internal_allowed(ws, outside):
    """allow_internal_symlinks relaxes traversal, not target disclosure."""
    (ws / "escape").symlink_to(outside / "secret.txt")
    g = guard(ws, allow_internal_symlinks=True)
    r = g.inspect_path("escape")
    assert r.kind is PathKind.SYMLINK
    assert r.link_target is LinkTarget.EXTERNAL
    assert str(outside) not in json.dumps(r.to_dict())


# ===========================================================================
# Special files / hardlinks / POSIX colon
# ===========================================================================

@POSIX_ONLY
def test_fifo_classified_without_blocking(ws):
    os.mkfifo(ws / "pipe")
    r = guard(ws).inspect_path("pipe")
    assert r.kind is PathKind.FIFO
    assert r.size is None and r.modified_at is None


@POSIX_ONLY
def test_socket_classified(ws):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(ws / "sock"))
        assert guard(ws).inspect_path("sock").kind is PathKind.SOCKET
    finally:
        srv.close()


@POSIX_ONLY
def test_hardlink_count_reported_not_refused(ws):
    (ws / "a.txt").write_text("x", encoding="utf-8")
    os.link(ws / "a.txt", ws / "b.txt")
    g = guard(ws)
    assert g.inspect_path("a.txt").links == 2
    assert g.inspect_path("b.txt").links == 2


@POSIX_ONLY
def test_colon_filename_is_ordinary_on_posix(ws):
    (ws / "notes:archive.txt").write_text("legit", encoding="utf-8")
    assert guard(ws).inspect_path("notes:archive.txt").kind is PathKind.REGULAR


# ===========================================================================
# Path forms / limits
# ===========================================================================

@pytest.mark.parametrize("requested,reason", [
    ("../x", RejectionReason.TRAVERSAL),
    ("/etc/passwd", RejectionReason.ABSOLUTE),
    ("C:/x", RejectionReason.DRIVE_QUALIFIED),
    ("C:\\Windows", RejectionReason.DRIVE_QUALIFIED),
    ("\\\\server\\share\\x", RejectionReason.UNC),
    ("src\\app.py", RejectionReason.BACKSLASH),
])
def test_hostile_path_forms_rejected(ws, requested, reason):
    with pytest.raises(PathRejectedError) as exc:
        guard(ws).inspect_path(requested)
    assert exc.value.reason is reason


def test_path_limits_rejected(ws):
    g = guard(ws)
    with pytest.raises(PathRejectedError) as exc:
        g.inspect_path("a" * 4097)
    assert exc.value.reason is RejectionReason.TOO_LONG
    with pytest.raises(PathRejectedError) as exc:
        g.inspect_path("/".join(["d"] * 65))
    assert exc.value.reason is RejectionReason.TOO_DEEP


# ===========================================================================
# Metadata contract
# ===========================================================================

def test_forbidden_fields_are_absent(ws):
    (ws / "notes.txt").write_text("hi", encoding="utf-8")
    r = guard(ws).inspect_path("notes.txt")
    d = r.to_dict()
    for forbidden in ("device", "inode", "atime", "ctime", "mode", "owner",
                      "group", "target", "absolute", "resolved", "content",
                      "mtime", "uid", "gid"):
        assert forbidden not in d
    for value in d.values():
        assert not (isinstance(value, str) and value.startswith("/"))


def test_modified_at_is_utc_iso_and_deterministic(ws):
    (ws / "t.txt").write_text("x", encoding="utf-8")
    r = guard(ws).inspect_path("t.txt")
    parsed = _dt.datetime.fromisoformat(r.modified_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == _dt.timedelta(0)
    assert r.modified_at.endswith("+00:00")
    assert r.modified_at == guard(ws).inspect_path("t.txt").modified_at


@POSIX_ONLY
def test_negative_mtime_renders_as_iso(ws):
    f = ws / "old.txt"
    f.write_text("x", encoding="utf-8")
    os.utime(f, (0, -3_600_000))  # pre-1970, deliberately negative
    r = guard(ws).inspect_path("old.txt")
    parsed = _dt.datetime.fromisoformat(r.modified_at)
    assert parsed.year < 1970
