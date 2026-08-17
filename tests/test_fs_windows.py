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

import subprocess
import sys

import pytest

from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import (
    LinkTarget,
    PathKind,
    PathRejectedError,
    RejectionReason,
)
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin.list_directory import ListDirectoryTool
from agentcore.tools.builtin.read_file import ReadFileTool

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32",
    reason="requires a real Windows filesystem (junctions/reparse points)")


def make_junction(link, target):
    """Create a directory junction, asserting every premise along the way.

    A silent ``mklink`` failure would leave no junction behind, and a missing
    path is *not* a symlink rejection, so the setup is verified before any
    security assertion is made.
    """
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, errors="replace",
    )
    assert result.returncode == 0, (
        f"junction creation failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}")
    assert link.is_symlink(), (
        "junction not reported as a symlink on this Python build; "
        "the documented rejection path cannot apply")


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
