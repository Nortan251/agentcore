"""Windows-only filesystem security verification (Task 38).

These tests run only on a real Windows filesystem (``sys.platform == "win32"``)
and are skipped everywhere else. They close the verification gap Task 37
deliberately left open: until now, junction/reparse-point behaviour was never
executed because the project's only environment was Linux.

Every test uses the real :class:`~agentcore.fs.guard.WorkspaceGuard` and the
real ``read_file`` / ``list_directory`` tools — no mocks — and would fail if
the containment or symlink policy were weakened. Junctions (created with
``mklink /J``, which needs no elevation) are the Windows reparse-point type
the security walk must refuse.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import (
    LinkTarget,
    PathKind,
    PathRejectedError,
    RejectionReason,
)
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin.inspect import InspectTool
from agentcore.tools.builtin.list_directory import ListDirectoryTool
from agentcore.tools.builtin.read_file import ReadFileTool

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires a real Windows filesystem (junctions/reparse points)")


def make_junction(link, target):
    """Create a directory junction, asserting every premise along the way.

    A silent ``mklink`` failure would leave no junction behind, and a missing
    path is *not* a link rejection, so the setup is verified before any
    security assertion is made. The premise is the reparse-point attribute:
    ``Path.is_symlink()`` reports ``False`` for junctions on current CPython.
    """
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, errors="replace",
    )
    assert result.returncode == 0, (
        f"junction creation failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}")
    assert link.lstat().st_file_attributes & 0x400, (
        "junction did not materialise as a reparse point; "
        "the documented rejection path cannot apply")


def make_directory_symlink(link, target):
    """Create a *true* NTFS symbolic link with ``mklink /D``.

    Distinct from :func:`make_junction`: ``/D`` creates a real symbolic link
    (reparse tag ``IO_REPARSE_TAG_SYMLINK``), while ``/J`` creates a junction
    (``IO_REPARSE_TAG_MOUNT_POINT``). Both are asserted to exist, so a silent
    failure cannot masquerade as a pass.
    """
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/D", str(link), str(target)],
        capture_output=True, text=True, errors="replace",
    )
    assert result.returncode == 0, (
        f"directory symlink creation failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}")
    assert link.is_symlink(), "mklink /D did not create a symlink"


def make_file_symlink(link, target):
    """Create a *true* NTFS file symbolic link with ``mklink`` (no flag)."""
    result = subprocess.run(
        ["cmd", "/c", "mklink", str(link), str(target)],
        capture_output=True, text=True, errors="replace",
    )
    assert result.returncode == 0, (
        f"file symlink creation failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}")
    assert link.is_symlink(), "mklink did not create a file symlink"


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET OUTSIDE", encoding="utf-8")
    return other


# ===========================================================================
# read_file integration (Task 38 §5)
# ===========================================================================

@WINDOWS_ONLY
def test_read_file_reads_ordinary_file_in_workspace(ws):
    """The ordinary in-workspace read still works on Windows."""
    (ws / "notes.txt").write_text("hello from windows", encoding="utf-8")
    guard = WorkspaceGuard(ws)
    result = ReadFileTool().run({"path": "notes.txt"},
                                ToolContext(workspace=guard))
    assert result.ok is True
    assert "hello from windows" in result.output
    assert result.metadata["path"] == "notes.txt"


@WINDOWS_ONLY
def test_read_file_refuses_escaping_junction_through_guard(ws, outside):
    """Scenario B: a junction pointing outside the workspace is refused."""
    make_junction(ws / "j", outside)
    guard = WorkspaceGuard(ws)
    with pytest.raises(PathRejectedError) as exc:
        guard.open_file("j/secret.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@WINDOWS_ONLY
def test_read_file_refuses_escaping_junction_through_tool(ws, outside):
    """The tool path adds no bypass around the guard."""
    make_junction(ws / "j", outside)
    guard = WorkspaceGuard(ws)
    result = ReadFileTool().run({"path": "j/secret.txt"},
                                ToolContext(workspace=guard))
    assert result.ok is False
    assert result.metadata["kind"] == "path_rejected"
    assert result.metadata["reason"] == "symlink"
    # Neither the outside content nor its absolute location may leak.
    assert "TOP SECRET" not in str(result.output)
    assert str(outside) not in str(result.output)


@WINDOWS_ONLY
def test_read_file_refuses_nested_junction_escape(ws, outside):
    """Scenario C: the junction is an intermediate parent component."""
    (ws / "a").mkdir()
    make_junction(ws / "a" / "b", outside)
    guard = WorkspaceGuard(ws)
    with pytest.raises(PathRejectedError) as exc:
        guard.open_file("a/b/secret.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


# ===========================================================================
# list_directory integration (Task 38 §6)
# ===========================================================================

@WINDOWS_ONLY
def test_list_directory_does_not_follow_junction(ws, outside):
    """Listing reports the junction, never the target's contents."""
    make_junction(ws / "j", outside)
    (ws / "real.txt").write_text("x", encoding="utf-8")
    guard = WorkspaceGuard(ws)
    listing = guard.list_directory(guard.resolve_directory("."))

    kinds = {e.display_name: e.kind for e in listing.entries}
    assert kinds["j"] is PathKind.SYMLINK

    targets = {e.display_name: e.link_target for e in listing.entries}
    assert targets["j"] is LinkTarget.EXTERNAL

    payload = str(listing.to_dict())
    # The external target's absolute path and its entries must not appear.
    assert str(outside) not in payload
    assert "secret.txt" not in payload


@WINDOWS_ONLY
def test_list_directory_tool_does_not_follow_junction(ws, outside):
    """The list_directory tool adds no bypass around the guard."""
    make_junction(ws / "j", outside)
    guard = WorkspaceGuard(ws)
    result = ListDirectoryTool().run({"path": "."},
                                     ToolContext(workspace=guard))
    assert result.ok is True
    assert "secret.txt" not in str(result.output)
    assert str(outside) not in str(result.output)
    # The junction itself is still visible, as a symlink, not flattened.
    assert "j" in str(result.output)


# ===========================================================================
# Windows path forms (Task 38 §7)
# ===========================================================================

@WINDOWS_ONLY
@pytest.mark.parametrize("requested,reason", [
    ("C:/Windows/System32", RejectionReason.DRIVE_QUALIFIED),
    ("C:\\Windows\\System32", RejectionReason.DRIVE_QUALIFIED),
    ("\\\\server\\share\\x", RejectionReason.UNC),
    ("..\\..\\x", RejectionReason.BACKSLASH),
    ("/etc/passwd", RejectionReason.ABSOLUTE),
])
def test_windows_path_forms_rejected_on_real_windows(ws, requested, reason):
    """Drive, UNC, backslash and absolute forms are refused on Windows too."""
    guard = WorkspaceGuard(ws)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason is reason


# ===========================================================================
# Alternate data streams (Task 39 F-01)
# ===========================================================================

@WINDOWS_ONLY
@pytest.mark.parametrize("requested", [
    "id_rsa::$DATA",
    "id_rsa:hidden",
    "config.json:hidden",
    "notes.txt:hidden",
    "ordinary:stream",
])
def test_ads_requests_are_rejected_outright(ws, requested):
    """Any stream-qualified request fails closed as ``ads``, never normalised.

    The guard must refuse the qualified name before any resolution, so
    ``id_rsa::$DATA`` cannot silently become ``id_rsa`` and open.
    """
    guard = WorkspaceGuard(ws)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason is RejectionReason.ADS


@WINDOWS_ONLY
def test_ads_sensitive_names_cannot_bypass_the_sensitive_policy(ws):
    """The exact audit finding: a stream qualifier must not dodge the
    sensitive-name policy, and the base file must not be reached."""
    (ws / "id_rsa").write_text("PRIVATE KEY BODY", encoding="utf-8")
    (ws / "config.json").write_text('{"secret": "value"}', encoding="utf-8")
    guard = WorkspaceGuard(ws)
    for name in ("id_rsa::$DATA", "id_rsa:hidden", "config.json:hidden"):
        result = ReadFileTool().run({"path": name},
                                    ToolContext(workspace=guard))
        assert result.ok is False
        assert result.metadata["reason"] == "ads"
        # Neither the base file's bytes nor the (absent) stream leak.
        assert "PRIVATE KEY BODY" not in str(result.output)
        assert "value" not in str(result.output)


@WINDOWS_ONLY
def test_ads_read_is_rejected_while_ordinary_name_still_reads(ws):
    """``notes.txt`` succeeds; ``notes.txt:hidden`` is refused.

    The stream is created if the volume supports it, to prove the rejection
    applies to a *real* alternate data stream and not merely to the string.
    """
    (ws / "notes.txt").write_text("visible body", encoding="utf-8")
    try:
        # Create the stream if the volume supports it; the rejection must
        # hold either way.
        with open(ws / "notes.txt:hidden", "w", encoding="utf-8") as fh:
            fh.write("SECRET-STREAM-BODY")
    except OSError:
        pass

    guard = WorkspaceGuard(ws)

    ordinary = ReadFileTool().run({"path": "notes.txt"},
                                  ToolContext(workspace=guard))
    assert ordinary.ok is True
    assert "visible body" in ordinary.output

    denied = ReadFileTool().run({"path": "notes.txt:hidden"},
                                ToolContext(workspace=guard))
    assert denied.ok is False
    assert denied.metadata["reason"] == "ads"
    assert "SECRET-STREAM-BODY" not in str(denied.output)
    assert "visible body" not in str(denied.output)

    # Also refused by list_directory, which shares the same validation path.
    listed = ListDirectoryTool().run({"path": "notes.txt:hidden"},
                                     ToolContext(workspace=guard))
    assert listed.ok is False
    assert listed.metadata["reason"] == "ads"


@WINDOWS_ONLY
def test_ads_rejection_has_no_path_detail_bypass(ws):
    """The rejection is the same category no matter the qualifier shape."""
    guard = WorkspaceGuard(ws)
    for name in ("file.txt::$DATA", "file.txt:anything"):
        with pytest.raises(PathRejectedError) as exc:
            guard.resolve_relative(name)
        assert exc.value.reason is RejectionReason.ADS


# ===========================================================================
# True NTFS symbolic links (Task 40 §5/§6)
# ===========================================================================

@WINDOWS_ONLY
def test_true_symlink_escaping_is_refused(ws, outside):
    """Scenario A: a real symlink to an outside directory is refused.

    Default policy; the link walk sees the symlink before any resolution, so
    the outside content and its location must never surface.
    """
    make_directory_symlink(ws / "link", outside)
    guard = WorkspaceGuard(ws)
    result = ReadFileTool().run({"path": "link/secret.txt"},
                                ToolContext(workspace=guard))
    assert result.ok is False
    assert result.metadata["reason"] == "symlink"
    assert "TOP SECRET" not in str(result.output)
    assert str(outside) not in str(result.output)


@WINDOWS_ONLY
def test_true_symlink_internal_default_policy_rejected(ws):
    """Scenario B (default): an internal true symlink is still a link."""
    (ws / "inner").mkdir()
    (ws / "inner" / "ok.txt").write_text("inner ok", encoding="utf-8")
    make_directory_symlink(ws / "alias", ws / "inner")
    guard = WorkspaceGuard(ws)
    with pytest.raises(PathRejectedError) as exc:
        guard.open_file("alias/ok.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@WINDOWS_ONLY
def test_true_symlink_internal_allowed_when_configured(ws):
    """Scenario B: with allow_internal_symlinks, a contained target is read."""
    (ws / "inner").mkdir()
    (ws / "inner" / "ok.txt").write_text("inner ok", encoding="utf-8")
    make_directory_symlink(ws / "alias", ws / "inner")
    guard = WorkspaceGuard(
        ws, config=FilesystemConfig(allow_internal_symlinks=True))
    data, _, _ = guard.read_path("alias/ok.txt")
    assert data == b"inner ok"


@WINDOWS_ONLY
def test_true_symlink_sensitive_target_rejected_when_allowed(ws):
    """Scenario B: an innocuously-named link to a credential file is refused.

    This is the case that depends on link-following resolution: the requested
    name is harmless, so only the *effective target* can trigger the sensitive
    policy. Must be refused on every supported Python version.
    """
    hidden = ws / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("API_KEY=sk-true-symlink", encoding="utf-8")
    make_file_symlink(ws / "innocent.txt", hidden / ".env")
    guard = WorkspaceGuard(
        ws, config=FilesystemConfig(allow_internal_symlinks=True))
    result = ReadFileTool().run({"path": "innocent.txt"},
                                ToolContext(workspace=guard))
    assert result.ok is False
    assert result.metadata["reason"] == "sensitive_target"
    assert "sk-true-symlink" not in str(result.output)


@WINDOWS_ONLY
def test_true_symlink_escaping_still_rejected_when_internal_allowed(ws,
                                                                    outside):
    """Scenario B: escape stays refused even with internal symlinks allowed.

    With the link walk skipped, containment itself must detect the escape —
    which requires link-following resolution on every supported version.
    """
    make_directory_symlink(ws / "link", outside)
    guard = WorkspaceGuard(
        ws, config=FilesystemConfig(allow_internal_symlinks=True))
    result = ReadFileTool().run({"path": "link/secret.txt"},
                                ToolContext(workspace=guard))
    assert result.ok is False
    assert "TOP SECRET" not in str(result.output)
    assert str(outside) not in str(result.output)


@WINDOWS_ONLY
def test_true_symlink_nested_intermediate_parent(ws, outside):
    """Scenario C: a symlink in an intermediate parent obeys the same rules."""
    (ws / "a").mkdir()
    make_directory_symlink(ws / "a" / "b", outside)
    guard = WorkspaceGuard(ws)
    result = ReadFileTool().run({"path": "a/b/secret.txt"},
                                ToolContext(workspace=guard))
    assert result.ok is False
    assert result.metadata["reason"] == "symlink"
    assert "TOP SECRET" not in str(result.output)
    assert str(outside) not in str(result.output)


@WINDOWS_ONLY
def test_list_directory_true_symlink_not_followed(ws, outside):
    """Scenario D: listing classifies a true symlink, never follows it."""
    make_directory_symlink(ws / "link", outside)
    (ws / "real.txt").write_text("x", encoding="utf-8")
    guard = WorkspaceGuard(ws)
    listing = guard.list_directory(guard.resolve_directory("."))

    kinds = {e.display_name: e.kind for e in listing.entries}
    assert kinds["link"] is PathKind.SYMLINK

    targets = {e.display_name: e.link_target for e in listing.entries}
    assert targets["link"] is LinkTarget.EXTERNAL

    payload = str(listing.to_dict())
    assert str(outside) not in payload
    assert "secret.txt" not in payload


@WINDOWS_ONLY
def test_list_directory_withholds_innocuous_link_to_sensitive(ws):
    """An innocuously-named true symlink to a credential file is withheld."""
    (ws / ".env").write_text("k=v", encoding="utf-8")
    make_file_symlink(ws / "alias.txt", ws / ".env")
    guard = WorkspaceGuard(ws)
    listing = guard.list_directory(guard.resolve_directory("."))
    names = {e.display_name for e in listing.entries}
    assert "alias.txt" not in names
    assert listing.sensitive_count == 2  # .env itself, plus the link


# ===========================================================================
# Case sensitivity of the sensitive-name policy (Task 40 §12)
# ===========================================================================

@WINDOWS_ONLY
@pytest.mark.parametrize("name", [
    ".env", ".ENV", ".Env", "id_rsa", "ID_RSA", "prod.env", "cert.pem",
])
def test_case_variations_of_sensitive_names_are_protected(ws, name):
    """Every case/pattern variant stays protected through the real guard."""
    (ws / name).write_text("credential-ish body", encoding="utf-8")
    guard = WorkspaceGuard(ws)
    result = ReadFileTool().run({"path": name}, ToolContext(workspace=guard))
    assert result.ok is False
    assert result.metadata["reason"] == "sensitive"
    assert "credential-ish body" not in str(result.output)


# ===========================================================================
# Reserved device names (Task 40 §13)
# ===========================================================================

@WINDOWS_ONLY
@pytest.mark.parametrize("name", ["NUL", "CON"])
def test_reserved_device_names_fail_closed(ws, name):
    """NUL/CON keep failing closed; the exact reason is not pinned."""
    guard = WorkspaceGuard(ws)
    with pytest.raises(PathRejectedError):
        guard.open_file(name)


# ===========================================================================
# inspect (Task 41.2)
# ===========================================================================

def _inspect(ws, path):
    return InspectTool().run({"path": path},
                             ToolContext(workspace=WorkspaceGuard(ws)))


@WINDOWS_ONLY
def test_inspect_root_regular_directory_and_mtime(ws):
    (ws / "notes.txt").write_text("x", encoding="utf-8")
    (ws / "d").mkdir()
    root = _inspect(ws, ".")
    assert root.ok and root.metadata["kind"] == "directory"
    regular = _inspect(ws, "notes.txt")
    assert regular.ok and regular.metadata["kind"] == "regular"
    assert regular.metadata["size"] == 1
    assert regular.metadata["modified_at"] is not None
    directory = _inspect(ws, "d")
    assert directory.ok and directory.metadata["kind"] == "directory"


@WINDOWS_ONLY
def test_inspect_junction_categorical_external(ws, outside):
    """A junction is reported categorically, never followed."""
    make_junction(ws / "j", outside)
    r = _inspect(ws, "j")
    assert r.ok is True
    assert r.metadata["kind"] == "symlink"
    assert r.metadata["link_target"] == "external"
    assert "secret.txt" not in json.dumps(r.to_dict())
    assert str(outside) not in json.dumps(r.to_dict())


@WINDOWS_ONLY
def test_inspect_true_symlink_categorical_external(ws, outside):
    make_directory_symlink(ws / "s", outside)
    r = _inspect(ws, "s")
    assert r.ok is True
    assert r.metadata["kind"] == "symlink"
    assert r.metadata["link_target"] == "external"
    assert "secret.txt" not in json.dumps(r.to_dict())
    assert str(outside) not in json.dumps(r.to_dict())


@WINDOWS_ONLY
def test_inspect_internal_junction_categorical(ws):
    (ws / "inner").mkdir()
    (ws / "inner" / "ok.txt").write_text("x", encoding="utf-8")
    make_junction(ws / "j", ws / "inner")
    r = _inspect(ws, "j")
    assert r.ok is True
    assert r.metadata["kind"] == "symlink"
    assert r.metadata["link_target"] == "internal"


@WINDOWS_ONLY
def test_inspect_nested_junction_parent_rejected(ws, outside):
    (ws / "a").mkdir()
    make_junction(ws / "a" / "b", outside)
    r = _inspect(ws, "a/b/secret.txt")
    assert r.ok is False
    assert r.metadata["reason"] == "symlink"
    assert str(outside) not in json.dumps(r.to_dict())


@WINDOWS_ONLY
def test_inspect_innocuous_link_to_sensitive_target(ws):
    hidden = ws / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("k=v", encoding="utf-8")
    make_file_symlink(ws / "friendly", hidden / ".env")
    r = _inspect(ws, "friendly")
    assert r.ok is False
    assert r.metadata["reason"] == "sensitive_target"
    blob = json.dumps(r.to_dict())
    assert "hidden" not in blob
    assert ".env" not in blob


@WINDOWS_ONLY
@pytest.mark.parametrize("name", [
    "notes.txt:hidden", "id_rsa::$DATA", "config.json:hidden",
])
def test_inspect_ads_rejected(ws, name):
    r = _inspect(ws, name)
    assert r.ok is False
    assert r.metadata["reason"] == "ads"


@WINDOWS_ONLY
@pytest.mark.parametrize("requested,reason", [
    ("C:/Windows/System32", "drive_qualified"),
    ("C:\\Windows\\System32", "drive_qualified"),
    ("\\\\server\\share\\x", "unc"),
    ("..\\..\\x", "backslash"),
    ("/etc/passwd", "absolute"),
])
def test_inspect_hostile_path_forms_rejected(ws, requested, reason):
    r = _inspect(ws, requested)
    assert r.ok is False
    assert r.metadata["reason"] == reason


@WINDOWS_ONLY
@pytest.mark.parametrize("name", [
    ".env", ".ENV", "id_rsa", "ID_RSA", "prod.env", "cert.pem",
])
def test_inspect_sensitive_names_rejected(ws, name):
    (ws / name).write_text("x", encoding="utf-8")
    r = _inspect(ws, name)
    assert r.ok is False
    assert r.metadata["reason"] == "sensitive"


@WINDOWS_ONLY
@pytest.mark.parametrize("name", ["NUL", "CON"])
def test_inspect_reserved_names_never_open_or_hang(ws, name):
    """Reserved device names must fail closed without opening anything.

    Either ``lstat`` fails (categorical rejection) or the entry is classified
    as a special/other kind — never as a regular file with a readable size.
    """
    guard = WorkspaceGuard(ws)
    try:
        r = guard.inspect_path(name)
    except PathRejectedError:
        return
    assert r.kind in (PathKind.CHAR_DEVICE, PathKind.BLOCK_DEVICE,
                      PathKind.OTHER)
    assert r.size is None and r.links is None
