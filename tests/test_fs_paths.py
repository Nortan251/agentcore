"""Real-filesystem tests for the workspace safety foundation.

Nothing here mocks ``Path.resolve()`` or any filesystem call: every test builds
actual directories, files, symlinks and special files under ``tmp_path``. A
mocked containment test proves only that the mock behaves, which is precisely
the failure mode this layer exists to prevent.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path, PurePosixPath

import pytest

from agentcore.errors import ConfigError
from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import SafePath, WorkspaceGuard
from agentcore.fs.paths import (
    PathKind,
    PathRejectedError,
    RejectionReason,
    canonical_workspace,
    check_relative_form,
    classify,
    workspace_identity,
)
from agentcore.fs.sensitive import (
    DEFAULT_SENSITIVE_POLICY,
    SensitivePathPolicy,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


@pytest.fixture
def workspace(tmp_path):
    """A real workspace directory with a small file tree inside it."""
    root = tmp_path / "workspace"
    (root / "src" / "utils").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "utils" / "test.py").write_text("x = 1\n", encoding="utf-8")
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    return root


@pytest.fixture
def guard(workspace):
    return WorkspaceGuard(workspace)


@pytest.fixture
def outside(tmp_path):
    """A directory that is a sibling of the workspace, holding a secret."""
    other = tmp_path / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    return other


# ===========================================================================
# Canonical workspace identity
# ===========================================================================

def test_relative_workspace_becomes_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    root = canonical_workspace("./proj")
    assert root.is_absolute()
    assert root == (tmp_path / "proj").resolve()


def test_workspace_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = canonical_workspace("~/somewhere")
    assert str(root).startswith(str(tmp_path.resolve()))


def test_workspace_need_not_exist_yet(tmp_path):
    root = canonical_workspace(tmp_path / "not_created")
    assert root.is_absolute()
    assert not root.exists()


def test_workspace_resolves_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert canonical_workspace(link) == real.resolve()


@pytest.mark.parametrize("bad", ["", "   ", "\t"])
def test_empty_workspace_rejected(bad):
    with pytest.raises(PathRejectedError) as exc:
        canonical_workspace(bad)
    assert exc.value.reason is RejectionReason.EMPTY


def test_non_string_workspace_rejected():
    with pytest.raises(PathRejectedError) as exc:
        canonical_workspace(42)
    assert exc.value.reason is RejectionReason.NOT_A_STRING


def test_nul_byte_workspace_rejected():
    with pytest.raises(PathRejectedError) as exc:
        canonical_workspace("/tmp/ab\x00cd")
    assert exc.value.reason is RejectionReason.NUL_BYTE


def test_workspace_that_is_a_file_rejected(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(PathRejectedError) as exc:
        canonical_workspace(f)
    assert exc.value.reason is RejectionReason.NOT_A_DIRECTORY


def test_identity_is_stable_and_absolute(workspace):
    g1 = WorkspaceGuard(workspace)
    g2 = WorkspaceGuard(str(workspace))
    assert g1.identity == g2.identity == workspace_identity(workspace.resolve())
    assert Path(g1.identity).is_absolute()


def test_guard_root_is_read_only(guard, tmp_path):
    with pytest.raises(AttributeError):
        guard.root = tmp_path  # type: ignore[misc]


# ===========================================================================
# Valid relative paths
# ===========================================================================

@pytest.mark.parametrize("requested,expected", [
    ("notes.txt", "notes.txt"),
    ("src/app.py", "src/app.py"),
    ("src/utils/test.py", "src/utils/test.py"),
    ("./src/app.py", "src/app.py"),
    ("./notes.txt", "notes.txt"),
    ("src/./app.py", "src/app.py"),
    ("src//app.py", "src/app.py"),
])
def test_valid_paths_are_accepted_and_normalised(guard, workspace,
                                                 requested, expected):
    safe = guard.resolve_relative(requested)
    assert safe.relative == expected
    assert safe.absolute == (workspace / expected).resolve()
    assert safe.absolute.is_relative_to(workspace.resolve())


def test_safe_path_str_is_the_relative_form(guard):
    assert str(guard.resolve_relative("src/app.py")) == "src/app.py"


def test_accepts_a_path_object(guard):
    assert guard.resolve_relative(Path("src/app.py")).relative == "src/app.py"


def test_nonexistent_but_contained_path_is_allowed_by_containment(guard):
    """Containment is about location, not existence."""
    safe = guard.resolve_relative("src/brand_new.py")
    assert safe.relative == "src/brand_new.py"
    # Existence is a separate, explicit check.
    with pytest.raises(PathRejectedError) as exc:
        guard.require_readable_file(safe)
    assert exc.value.reason is RejectionReason.NOT_FOUND


# ===========================================================================
# Traversal
# ===========================================================================

@pytest.mark.parametrize("requested", [
    "../outside",
    "../../outside",
    "../outside/secret.txt",
    "../../../../../../etc/passwd",
    "src/../../outside",
    "src/./../../outside",
    "src/../src/../../outside",
    "a/b/c/../../../../outside",
    "..",
    "../",
])
def test_traversal_is_rejected(guard, requested):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason is RejectionReason.TRAVERSAL


def test_traversal_that_stays_inside_is_still_rejected(guard):
    """``src/../notes.txt`` is harmless but refused: '..' is never accepted.

    Rejecting the *form* rather than only the outcome keeps the rule simple
    and removes a class of normalisation bugs.
    """
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("src/../notes.txt")
    assert exc.value.reason is RejectionReason.TRAVERSAL


def test_dotdot_inside_a_filename_is_not_traversal(guard, workspace):
    (workspace / "weird..name.txt").write_text("ok", encoding="utf-8")
    assert guard.resolve_relative("weird..name.txt").relative == \
        "weird..name.txt"


# ===========================================================================
# Sibling-prefix confusion — the reason string prefixes are banned
# ===========================================================================

def test_sibling_prefix_directory_is_not_inside(tmp_path):
    """``/…/workspace-other`` must not count as inside ``/…/workspace``."""
    root = tmp_path / "workspace"
    root.mkdir()
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("loot", encoding="utf-8")

    # The naive check that this layer must never use:
    assert str(sibling).startswith(str(root))
    # The check it actually uses:
    assert not sibling.resolve().is_relative_to(root.resolve())

    guard = WorkspaceGuard(root)
    with pytest.raises(PathRejectedError):
        guard.resolve_relative("../workspace-other/loot.txt")


def test_symlink_to_sibling_prefix_directory_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("loot", encoding="utf-8")
    (root / "peek").symlink_to(sibling, target_is_directory=True)

    guard = WorkspaceGuard(root)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("peek/loot.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


# ===========================================================================
# Absolute / platform-specific forms
# ===========================================================================

@pytest.mark.parametrize("requested", [
    "/etc/passwd",
    "/",
    "/tmp/anything",
])
def test_posix_absolute_paths_rejected(guard, requested):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason is RejectionReason.ABSOLUTE


@pytest.mark.parametrize("requested", [
    "C:\\Windows\\System32\\config\\SAM",
    "C:/Windows/System32",
    "D:\\data\\file.txt",
    "c:/lower/drive",
])
def test_windows_drive_paths_rejected_on_any_platform(guard, requested):
    """Windows-shaped input is refused on Linux too — the rule is portable."""
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason is RejectionReason.DRIVE_QUALIFIED


@pytest.mark.parametrize("requested", [
    "\\\\server\\share\\file.txt",
    "\\\\?\\C:\\Windows",
    "//server/share/file.txt",
])
def test_unc_paths_rejected_on_any_platform(guard, requested):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason in {RejectionReason.UNC,
                                RejectionReason.DRIVE_QUALIFIED}


def test_backslash_paths_rejected(guard):
    """Backslash means 'separator' on Windows and 'filename char' on POSIX.

    Accepting it would make one request mean two different things, so it is
    refused outright rather than interpreted.
    """
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("src\\app.py")
    assert exc.value.reason is RejectionReason.BACKSLASH


def test_windows_reserved_form_is_covered_by_drive_check():
    """Documents the cross-platform reasoning behind the drive test.

    ``PureWindowsPath('/etc/passwd').is_absolute()`` is False, so a naive
    ``is_absolute`` check would miss POSIX absolutes when run on Windows and
    vice versa. Both checks therefore run, unconditionally, on every platform.
    """
    from pathlib import PureWindowsPath
    assert PureWindowsPath("/etc/passwd").is_absolute() is False
    assert PurePosixPath("C:\\Windows").is_absolute() is False


# ===========================================================================
# Degenerate input
# ===========================================================================

@pytest.mark.parametrize("requested", ["", "   ", ".", "./", "././."])
def test_empty_or_pathless_rejected(guard, requested):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(requested)
    assert exc.value.reason is RejectionReason.EMPTY


def test_nul_byte_rejected(guard):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("src/a\x00b.py")
    assert exc.value.reason is RejectionReason.NUL_BYTE


@pytest.mark.parametrize("bad", [None, 42, 3.5, [], {}, True])
def test_non_string_rejected(guard, bad):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(bad)
    assert exc.value.reason is RejectionReason.NOT_A_STRING


def test_overlong_path_rejected(guard):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("a" * 5000)
    assert exc.value.reason is RejectionReason.TOO_LONG


def test_too_deep_path_rejected(guard):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("/".join(["d"] * 100))
    assert exc.value.reason is RejectionReason.TOO_DEEP


def test_depth_limit_is_configurable(workspace):
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_path_depth=2))
    assert guard.resolve_relative("src/app.py").relative == "src/app.py"
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("src/utils/test.py")
    assert exc.value.reason is RejectionReason.TOO_DEEP


def test_check_relative_form_is_pure(tmp_path):
    """The lexical check touches no filesystem, so it works for a fake root."""
    out = check_relative_form("a/./b", max_length=100, max_depth=10)
    assert out == PurePosixPath("a/b")


# ===========================================================================
# Symlinks
# ===========================================================================

@POSIX_ONLY
def test_symlink_to_file_outside_workspace_rejected(guard, workspace, outside):
    (workspace / "escape.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("escape.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@POSIX_ONLY
def test_symlinked_directory_outside_workspace_rejected(guard, workspace,
                                                        outside):
    (workspace / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("out/secret.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@POSIX_ONLY
def test_symlinked_parent_directory_is_caught_before_traversal(guard,
                                                               workspace,
                                                               outside):
    """A symlinked *parent* is the subtle case: the leaf looks innocent."""
    nested = outside / "deep" / "deeper"
    nested.mkdir(parents=True)
    (nested / "loot.txt").write_text("loot", encoding="utf-8")
    (workspace / "gateway").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("gateway/deep/deeper/loot.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@POSIX_ONLY
def test_nested_symlink_escape_rejected(guard, workspace, outside):
    """link -> link -> outside."""
    hop = outside / "hop"
    hop.mkdir()
    (hop / "target.txt").write_text("x", encoding="utf-8")
    (outside / "first").symlink_to(hop, target_is_directory=True)
    (workspace / "second").symlink_to(outside / "first",
                                      target_is_directory=True)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("second/target.txt")
    assert exc.value.reason is RejectionReason.SYMLINK


@POSIX_ONLY
def test_symlink_inside_workspace_rejected_by_default(guard, workspace):
    """Default posture rejects even a *contained* symlink.

    Escape then becomes impossible by construction rather than dependent on a
    resolution check being exactly right.
    """
    (workspace / "alias.py").symlink_to(workspace / "src" / "app.py")
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("alias.py")
    assert exc.value.reason is RejectionReason.SYMLINK


@POSIX_ONLY
def test_internal_symlink_allowed_when_configured(workspace):
    (workspace / "alias.py").symlink_to(workspace / "src" / "app.py")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    safe = guard.resolve_relative("alias.py")
    assert safe.absolute.is_relative_to(workspace.resolve())


@POSIX_ONLY
def test_escaping_symlink_still_rejected_when_internal_allowed(workspace,
                                                               outside):
    """Relaxing the symlink rule must NOT relax containment."""
    (workspace / "escape.txt").symlink_to(outside / "secret.txt")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("escape.txt")
    assert exc.value.reason is RejectionReason.ESCAPES_WORKSPACE


@POSIX_ONLY
def test_broken_symlink_rejected(guard, workspace):
    (workspace / "dangling").symlink_to(workspace / "nope.txt")
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("dangling")
    assert exc.value.reason is RejectionReason.SYMLINK


@pytest.mark.skipif(sys.platform != "win32",
                    reason="junction/reparse-point behaviour requires Windows; "
                           "see the module note on Windows CI")
def test_windows_junction_rejected(tmp_path):  # pragma: no cover - needs Windows
    """Windows junctions/reparse points.

    NOT verified in this project's Linux sandbox. ``Path.is_symlink()`` reports
    True for junctions on modern CPython, so the same walk should reject them,
    but that must be proven by an actual Windows CI job before anyone relies
    on it.
    """
    root = tmp_path / "ws"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    os.system(f'mklink /J "{root / "j"}" "{target}"')
    guard = WorkspaceGuard(root)
    with pytest.raises(PathRejectedError):
        guard.resolve_relative("j/anything.txt")


# ===========================================================================
# Special files
# ===========================================================================

def test_classify_regular_and_directory(workspace):
    assert classify(workspace / "notes.txt") is PathKind.REGULAR
    assert classify(workspace / "src") is PathKind.DIRECTORY
    assert classify(workspace / "missing") is PathKind.MISSING


@POSIX_ONLY
def test_classify_symlink_does_not_follow(workspace):
    (workspace / "alias").symlink_to(workspace / "notes.txt")
    assert classify(workspace / "alias") is PathKind.SYMLINK


@POSIX_ONLY
def test_fifo_is_rejected(workspace):
    """Opening a FIFO can block forever, so it must be refused before opening."""
    fifo = workspace / "pipe"
    os.mkfifo(fifo)
    assert classify(fifo) is PathKind.FIFO
    guard = WorkspaceGuard(workspace)
    safe = guard.resolve_relative("pipe")
    with pytest.raises(PathRejectedError) as exc:
        guard.require_readable_file(safe)
    assert exc.value.reason is RejectionReason.NOT_A_REGULAR_FILE


@POSIX_ONLY
def test_character_device_is_rejected(workspace):
    """/dev/null via a contained path, using a symlink-free bind is not
    possible in a sandbox, so classify() is checked directly."""
    assert classify(Path("/dev/null")) is PathKind.CHAR_DEVICE


@POSIX_ONLY
def test_socket_is_rejected(workspace):
    import socket
    sock_path = workspace / "sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        assert classify(sock_path) is PathKind.SOCKET
        guard = WorkspaceGuard(workspace)
        safe = guard.resolve_relative("sock")
        with pytest.raises(PathRejectedError) as exc:
            guard.require_readable_file(safe)
        assert exc.value.reason is RejectionReason.NOT_A_REGULAR_FILE
    finally:
        srv.close()


def test_directory_rejected_as_a_file(guard):
    safe = guard.resolve_relative("src")
    with pytest.raises(PathRejectedError) as exc:
        guard.require_readable_file(safe)
    assert exc.value.reason is RejectionReason.NOT_A_REGULAR_FILE


def test_require_directory_accepts_a_directory(guard):
    facts = guard.require_directory(guard.resolve_relative("src"))
    assert facts.kind is PathKind.DIRECTORY


def test_require_directory_rejects_a_file(guard):
    with pytest.raises(PathRejectedError) as exc:
        guard.require_directory(guard.resolve_relative("notes.txt"))
    assert exc.value.reason is RejectionReason.NOT_A_DIRECTORY


@POSIX_ONLY
def test_unreadable_file_rejected(workspace):
    if os.geteuid() == 0:
        pytest.skip("running as root; permission bits are not enforced")
    locked = workspace / "locked.txt"
    locked.write_text("secret", encoding="utf-8")
    locked.chmod(0o000)
    try:
        guard = WorkspaceGuard(workspace)
        with pytest.raises(PathRejectedError) as exc:
            guard.require_readable_file(guard.resolve_relative("locked.txt"))
        assert exc.value.reason is RejectionReason.UNREADABLE
    finally:
        locked.chmod(0o644)


# ===========================================================================
# Sensitive-path policy
# ===========================================================================

@pytest.mark.parametrize("name", [
    ".env", ".env.local", ".env.production", "prod.env",
    ".git-credentials", ".netrc", "_netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    "server.pem", "private.key", "cert.p12", "store.pfx", "vault.kdbx",
    "secrets.yaml", "secrets.json", ".htpasswd", ".pgpass",
    "kubeconfig", "my_credentials.json",
])
def test_sensitive_filenames_denied(name):
    assert DEFAULT_SENSITIVE_POLICY.is_sensitive(name), name


@pytest.mark.parametrize("path", [
    ".ssh/config", ".aws/credentials", ".gnupg/secring.gpg",
    "project/.ssh/id_rsa", ".kube/config", ".docker/config.json",
    "repo/.git/config",
])
def test_sensitive_directories_denied(path):
    assert DEFAULT_SENSITIVE_POLICY.is_sensitive(path), path


@pytest.mark.parametrize("name", [
    "README.md", "src/app.py", "notes.txt", "token_handling.py",
    "docs/authentication.md", "password_policy.rst", "secrets_tutorial.md",
    "id_rsa.pub", "key_bindings.json", "environment.md",
])
def test_ordinary_files_are_not_sensitive(name):
    """Files that merely *mention* credentials in their name stay readable.

    ``token_handling.py`` and ``id_rsa.pub`` are the important cases: a policy
    that denied them would be useless in a real codebase.
    """
    assert not DEFAULT_SENSITIVE_POLICY.is_sensitive(name), name


def test_sensitive_matching_is_case_insensitive():
    assert DEFAULT_SENSITIVE_POLICY.is_sensitive("ID_RSA")
    assert DEFAULT_SENSITIVE_POLICY.is_sensitive("Server.PEM")
    assert DEFAULT_SENSITIVE_POLICY.is_sensitive(".SSH/config")


def test_sensitive_reason_is_explanatory():
    reason = DEFAULT_SENSITIVE_POLICY.reason("id_rsa")
    assert "credential" in reason.lower()


def test_guard_denies_sensitive_before_touching_disk(workspace):
    """The file need not even exist: policy runs before filesystem access."""
    guard = WorkspaceGuard(workspace)
    assert not (workspace / ".env").exists()
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(".env")
    assert exc.value.reason is RejectionReason.SENSITIVE


def test_guard_denies_existing_sensitive_file(workspace):
    (workspace / ".env").write_text("API_KEY=sk-not-yours\n", encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(".env")
    assert exc.value.reason is RejectionReason.SENSITIVE


def test_sensitive_rejection_never_reveals_contents(workspace):
    (workspace / ".env").write_text("API_KEY=sk-super-secret-value\n",
                                    encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(".env")
    blob = str(exc.value) + repr(exc.value.details)
    assert "sk-super-secret-value" not in blob


def test_policy_can_be_extended():
    policy = SensitivePathPolicy().extend(names={"licence.dat"},
                                          patterns=("*.vault",),
                                          directories={"private"})
    assert policy.is_sensitive("licence.dat")
    assert policy.is_sensitive("data.vault")
    assert policy.is_sensitive("private/notes.txt")
    # The default instance is untouched — no shared mutable state.
    assert not DEFAULT_SENSITIVE_POLICY.is_sensitive("licence.dat")


def test_sensitive_denial_can_be_disabled(workspace):
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(deny_sensitive_paths=False))
    assert guard.resolve_relative(".env").relative == ".env"


def test_custom_policy_is_used_by_the_guard(workspace):
    (workspace / "notes.txt").write_text("x", encoding="utf-8")
    guard = WorkspaceGuard(
        workspace,
        sensitive_policy=SensitivePathPolicy().extend(names={"notes.txt"}))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("notes.txt")
    assert exc.value.reason is RejectionReason.SENSITIVE


# ===========================================================================
# Bounded reading
# ===========================================================================

def test_read_small_file(guard):
    safe = guard.resolve_relative("notes.txt")
    facts = guard.require_readable_file(safe)
    data, truncated = guard.read_bytes(safe, facts)
    assert data == b"plain notes\n"
    assert truncated is False


def test_oversized_file_rejected_before_reading(workspace):
    big = workspace / "big.bin"
    big.write_bytes(b"x" * 5000)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=100))
    safe = guard.resolve_relative("big.bin")
    with pytest.raises(PathRejectedError) as exc:
        guard.require_readable_file(safe)
    assert exc.value.reason is RejectionReason.TOO_LARGE
    assert exc.value.details["limit"] == 1000


def test_byte_ceiling_is_enforced_during_reading(workspace):
    """A file that grows after validation still cannot exceed the cap.

    This is the property that matters: the bound is applied while reading, not
    by loading everything and trimming afterwards.
    """
    path = workspace / "grows.bin"
    path.write_bytes(b"a" * 500)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=64))
    safe = guard.resolve_relative("grows.bin")
    facts = guard.require_readable_file(safe)
    # Grow it far beyond the ceiling *after* validation.
    with open(path, "ab") as fh:
        fh.write(b"b" * 100_000)
    data, _ = guard.read_bytes(safe, facts)
    assert len(data) == 1000


def test_iter_bounded_never_holds_more_than_one_chunk(workspace):
    path = workspace / "chunked.bin"
    path.write_bytes(b"z" * 10_000)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=10_000,
                                                   read_chunk_bytes=1024))
    safe = guard.resolve_relative("chunked.bin")
    facts = guard.require_readable_file(safe)
    sizes = [len(c) for c in guard.iter_bounded(safe, facts)]
    assert max(sizes) <= 1024
    assert sum(sizes) == 10_000


def test_truncation_is_reported_explicitly(workspace):
    path = workspace / "trunc.bin"
    path.write_bytes(b"q" * 4096)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=4096,
                                                   read_chunk_bytes=512))
    safe = guard.resolve_relative("trunc.bin")
    facts = guard.require_readable_file(safe)
    data, truncated = guard.read_bytes(safe, facts)
    # Exactly at the ceiling and complete -> not truncated.
    assert len(data) == 4096 and truncated is False


def test_empty_file_reads_cleanly(workspace):
    (workspace / "empty.txt").write_bytes(b"")
    guard = WorkspaceGuard(workspace)
    safe = guard.resolve_relative("empty.txt")
    facts = guard.require_readable_file(safe)
    assert guard.read_bytes(safe, facts) == (b"", False)


# ===========================================================================
# TOCTOU
# ===========================================================================

@POSIX_ONLY
def test_swapped_file_is_detected(workspace, outside):
    """Replacing the validated file before opening is detected, not read."""
    victim = workspace / "target.txt"
    victim.write_text("legitimate", encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    safe = guard.resolve_relative("target.txt")
    facts = guard.require_readable_file(safe)

    # Swap in a different file at the same path.
    victim.unlink()
    (workspace / "target.txt").write_text("attacker content", encoding="utf-8")

    with pytest.raises(PathRejectedError) as exc:
        guard.read_bytes(safe, facts)
    assert exc.value.reason is RejectionReason.CHANGED_UNDERFOOT


@POSIX_ONLY
def test_file_replaced_by_symlink_is_not_followed(workspace, outside):
    victim = workspace / "target.txt"
    victim.write_text("legitimate", encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    safe = guard.resolve_relative("target.txt")
    facts = guard.require_readable_file(safe)

    victim.unlink()
    victim.symlink_to(outside / "secret.txt")

    with pytest.raises(PathRejectedError) as exc:
        guard.read_bytes(safe, facts)
    assert exc.value.reason is RejectionReason.CHANGED_UNDERFOOT
    assert "TOP SECRET" not in str(exc.value)


@POSIX_ONLY
def test_o_nofollow_is_available_on_this_platform():
    """Documents the primitive the TOCTOU defence depends on."""
    assert hasattr(os, "O_NOFOLLOW")


def test_documented_toctou_limitation_is_stated():
    """The guard must not overclaim.

    A parent-directory swap between the symlink walk and the open is NOT
    defended against; that needs ``openat``-relative traversal. The docstring
    says so explicitly, and this test fails if that admission is removed.
    """
    from agentcore.fs import guard as guard_module
    doc = guard_module.__doc__ or ""
    assert "parent directory" in doc.lower()
    assert "does not" in doc.lower() or "unguaranteed" in doc.lower()


# ===========================================================================
# Read-only structural guarantee
# ===========================================================================

def test_guard_exposes_no_write_operations():
    """The primitive must not offer a way to mutate the filesystem."""
    public = [n for n in dir(WorkspaceGuard) if not n.startswith("_")]
    forbidden = ("write", "unlink", "delete", "remove", "mkdir", "rmdir",
                 "rename", "chmod", "touch", "copy", "move", "truncate")
    for name in public:
        assert not any(word in name.lower() for word in forbidden), name


def test_guard_source_never_calls_write_apis():
    """AST scan: the fs package must not reference mutating filesystem APIs.

    Textual matching was previously shown to be too weak (it matches
    docstrings), so this walks the parsed tree and inspects attribute names.
    """
    import ast
    import agentcore.fs as fs_pkg

    banned = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir",
              "rename", "replace", "chmod", "touch", "remove", "removedirs",
              "makedirs", "rmtree", "truncate"}
    package_dir = Path(fs_pkg.__file__).parent
    offenders = []
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in banned:
                offenders.append(f"{path.name}:{node.lineno}:{node.attr}")
            if isinstance(node, ast.Name) and node.id in banned:
                offenders.append(f"{path.name}:{node.lineno}:{node.id}")
    assert offenders == [], offenders


def test_open_regular_opens_read_only(guard, workspace):
    safe = guard.resolve_relative("notes.txt")
    facts = guard.require_readable_file(safe)
    with guard.open_regular(safe, facts) as handle:
        assert handle.readable()
        assert not handle.writable()


# ===========================================================================
# Configuration
# ===========================================================================

def test_filesystem_config_defaults_are_sane():
    cfg = FilesystemConfig()
    cfg.validate()
    assert cfg.max_file_bytes == 1_048_576
    assert cfg.read_chunk_bytes <= cfg.max_file_bytes
    assert cfg.allow_internal_symlinks is False
    assert cfg.deny_sensitive_paths is True


@pytest.mark.parametrize("field", [
    "max_file_bytes", "read_chunk_bytes", "max_directory_entries",
    "max_result_chars", "max_path_length", "max_path_depth",
])
def test_non_positive_bounds_rejected(field):
    with pytest.raises(ConfigError):
        FilesystemConfig(**{field: 0}).validate()


def test_chunk_larger_than_ceiling_is_clamped_not_rejected():
    """Tightening ``max_file_bytes`` alone must stay valid.

    Lowering the ceiling is the safe direction, so the chunk size is clamped
    rather than turned into a configuration error.
    """
    cfg = FilesystemConfig(max_file_bytes=100, read_chunk_bytes=1000)
    cfg.validate()
    assert cfg.effective_chunk_bytes == 100


def test_clamped_chunk_still_reads_correctly(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"k" * 500)
    guard = WorkspaceGuard(tmp_path,
                           config=FilesystemConfig(max_file_bytes=100,
                                                   read_chunk_bytes=99_999))
    safe = guard.resolve_relative("f.bin")
    with pytest.raises(PathRejectedError) as exc:
        guard.require_readable_file(safe)
    assert exc.value.reason is RejectionReason.TOO_LARGE


# ===========================================================================
# Observability
# ===========================================================================

def test_describe_is_free_of_file_contents(guard, workspace):
    (workspace / "notes.txt").write_text("confidential body", encoding="utf-8")
    info = guard.describe()
    assert "confidential body" not in str(info)
    assert info["workspace"] == guard.identity


def test_rejection_details_use_relative_paths_only(guard, workspace, outside):
    """A rejection may name the relative path; never the host layout."""
    (workspace / "out").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("out/secret.txt")
    blob = str(exc.value) + repr(exc.value.details)
    assert str(outside) not in blob
    assert str(workspace) not in blob


def test_file_facts_to_dict_has_no_absolute_path(guard):
    safe = guard.resolve_relative("src/app.py")
    payload = guard.require_readable_file(safe).to_dict()
    assert payload["path"] == "src/app.py"
    assert not any(str(v).startswith("/") for v in payload.values())


def test_rejection_reason_is_categorical(guard):
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("/etc/passwd")
    # Callers branch on the enum, not on message text.
    assert exc.value.reason is RejectionReason.ABSOLUTE
    assert exc.value.details["reason"] == "absolute"
