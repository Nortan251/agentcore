"""Regression tests for the Task 33 hardening pass.

Three audit findings drive this file:

A. regular-file TOCTOU / inode reuse
B. hardlink escape
C. internal symlink pointing at a sensitive file inside the workspace

plus descriptor hygiene, because moving the security decisions onto an open
file descriptor is only an improvement if descriptors are always released.

Everything here uses real filesystem objects. Critically, the inode-reuse
tests do **not** assume inode numbers change — that assumption is exactly what
made the previous design look correct on tmpfs while being unsound on ext4.
"""

from __future__ import annotations

import errno
import os
import resource
import subprocess
import tempfile
from pathlib import Path

import pytest

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.opened import OpenFile, open_verified
from agentcore.fs.paths import PathKind, PathRejectedError, RejectionReason

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


def _fstype(path: Path) -> str:
    try:
        return subprocess.run(["stat", "-f", "-c", "%T", str(path)],
                              capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:  # pragma: no cover - diagnostics only
        return "unknown"


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "hidden").mkdir(parents=True)
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    return root


@pytest.fixture
def outside(tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    return other


def open_fd_count() -> int:
    """Number of descriptors this process currently holds."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:  # pragma: no cover - non-Linux
        return -1


# ===========================================================================
# FINDING A — regular-file TOCTOU / inode reuse
# ===========================================================================

@POSIX_ONLY
def test_inode_reuse_is_real_on_this_filesystem(tmp_path):
    """Documents *why* device+inode comparison was abandoned.

    This test never asserts that inodes ARE reused — that is filesystem
    dependent (tmpfs allocates monotonically, ext4 recycles aggressively). It
    asserts the weaker, always-true fact that inode identity is not a reliable
    signal, and records what this filesystem actually did.
    """
    victim = tmp_path / "victim"
    victim.write_text("original", encoding="utf-8")
    first = victim.lstat().st_ino

    reused = False
    for _ in range(50):
        victim.unlink()
        victim.write_text("replacement", encoding="utf-8")
        if victim.lstat().st_ino == first:
            reused = True
            break

    # Either outcome is acceptable; the point is that we must not depend on it.
    assert isinstance(reused, bool)
    print(f"\n[fs={_fstype(tmp_path)}] inode reuse observed: {reused}")


@POSIX_ONLY
def test_security_facts_come_from_the_descriptor(workspace):
    """Type, size and link count must be fstat results, not stale lstat ones."""
    target = workspace / "file.txt"
    target.write_text("12345", encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    with guard.open_file("file.txt") as handle:
        assert handle.facts.size == 5
        assert handle.facts.kind is PathKind.REGULAR
        assert handle.facts.links == 1
        # Confirm they really match the descriptor, not the path.
        st = os.fstat(handle.fd)
        assert (handle.facts.size, handle.facts.inode) == (st.st_size,
                                                           st.st_ino)


@POSIX_ONLY
def test_stale_size_cannot_bypass_the_ceiling(workspace):
    """The core exploit the old design allowed.

    Validate a tiny file, replace it with a huge one (reusing the inode on
    ext4), and the old code would have trusted the 5-byte size captured
    earlier. Size now comes from the descriptor, so the limit still applies.
    """
    target = workspace / "small.txt"
    target.write_text("small", encoding="utf-8")
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=128))
    safe = guard.resolve_relative("small.txt")

    target.unlink()
    target.write_bytes(b"X" * 100_000)

    with pytest.raises(PathRejectedError) as exc:
        guard.open_safe(safe)
    assert exc.value.reason is RejectionReason.TOO_LARGE


@POSIX_ONLY
def test_repeated_replacement_attempts_never_leak_outside_content(workspace,
                                                                  outside):
    """Hammer the race: no iteration may ever return outside content."""
    target = workspace / "race.txt"
    target.write_text("legit", encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    safe = guard.resolve_relative("race.txt")

    leaked = []
    for i in range(60):
        target.unlink()
        if i % 3 == 0:
            target.symlink_to(outside / "secret.txt")
        elif i % 3 == 1:
            os.link(outside / "secret.txt", target)
        else:
            target.write_text(f"replacement-{i}", encoding="utf-8")
        try:
            with guard.open_safe(safe) as handle:
                data, _ = guard.read_from(handle)
            if b"TOP SECRET" in data:
                leaked.append(i)
        except PathRejectedError:
            pass
        target.unlink()
        target.write_text("legit", encoding="utf-8")

    assert leaked == [], f"outside content leaked on iterations {leaked}"


@POSIX_ONLY
def test_growth_after_open_is_still_bounded(workspace):
    """A file that grows while being read cannot exceed the ceiling."""
    target = workspace / "grow.bin"
    target.write_bytes(b"a" * 500)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=64))
    with guard.open_file("grow.bin") as handle:
        with open(target, "ab") as fh:
            fh.write(b"b" * 100_000)
        data, _ = guard.read_from(handle)
    assert len(data) == 1000


def test_toctou_limitation_is_documented_not_overclaimed():
    """The docs must not claim TOCTOU is eliminated."""
    from agentcore.fs import guard as guard_module
    from agentcore.fs import opened as opened_module

    combined = (guard_module.__doc__ or "") + (opened_module.__doc__ or "")
    lowered = combined.lower()
    assert "parent directory" in lowered
    assert "not guaranteed" in lowered or "not eliminated" in lowered
    assert "eliminates toctou" not in lowered
    assert "toctou is impossible" not in lowered


# ===========================================================================
# FINDING B — hardlink escape
# ===========================================================================

@POSIX_ONLY
def test_hardlink_to_outside_file_is_refused(workspace, outside):
    """The reproduction from the audit."""
    try:
        os.link(outside / "secret.txt", workspace / "innocent.txt")
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        if exc.errno in {errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP}:
            pytest.skip(f"filesystem cannot create hard links: {exc}")
        raise

    guard = WorkspaceGuard(workspace)
    with pytest.raises(PathRejectedError) as exc_info:
        guard.read_path("innocent.txt")
    assert exc_info.value.reason is RejectionReason.MULTIPLE_HARD_LINKS
    assert exc_info.value.details["links"] == 2
    assert "TOP SECRET" not in str(exc_info.value)


@POSIX_ONLY
def test_hardlink_wholly_inside_workspace_is_also_refused(workspace):
    """v1 policy is conservative: link count is the only available signal.

    The kernel gives no way to enumerate an inode's other names, so a link
    that happens to stay inside cannot be distinguished from one that escapes.
    Refusing both is the honest conservative choice, and this test pins that
    tradeoff so it is a decision rather than an accident.
    """
    original = workspace / "original.txt"
    original.write_text("content", encoding="utf-8")
    os.link(original, workspace / "alias.txt")

    guard = WorkspaceGuard(workspace)
    for name in ("original.txt", "alias.txt"):
        with pytest.raises(PathRejectedError) as exc:
            guard.read_path(name)
        assert exc.value.reason is RejectionReason.MULTIPLE_HARD_LINKS


@POSIX_ONLY
def test_ordinary_single_link_file_is_unaffected(workspace):
    """The policy must not break normal files: they have st_nlink == 1."""
    data, facts, _ = WorkspaceGuard(workspace).read_path("notes.txt")
    assert data == b"plain notes\n"
    assert facts.links == 1


@POSIX_ONLY
def test_hardlink_policy_can_be_opted_out(workspace, outside):
    """Documented escape hatch for deduplicated trees and build caches."""
    os.link(outside / "secret.txt", workspace / "linked.txt")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_multiple_hard_links=True))
    data, facts, _ = guard.read_path("linked.txt")
    assert facts.links == 2
    # Opting in genuinely does expose the outside content — that is the
    # documented cost of the setting, asserted so nobody enables it blindly.
    assert b"TOP SECRET" in data


def test_directories_are_not_caught_by_the_link_policy(workspace):
    """Directories always have st_nlink >= 2; the policy is for regular files."""
    guard = WorkspaceGuard(workspace)
    facts = guard.require_directory(guard.resolve_relative("hidden"))
    assert facts.kind is PathKind.DIRECTORY
    assert facts.links >= 2  # '.' plus the parent entry


# ===========================================================================
# FINDING C — internal symlink to a sensitive target
# ===========================================================================

@pytest.fixture
def sensitive_link(workspace):
    """An innocuously-named symlink to a credential file inside the workspace."""
    env = workspace / "hidden" / ".env"
    env.write_text("API_KEY=sk-must-never-leak\n", encoding="utf-8")
    (workspace / "public-link.pem").symlink_to(env)
    (workspace / "notes-alias.txt").symlink_to(env)
    return env


@POSIX_ONLY
def test_direct_access_to_sensitive_file_is_denied(workspace, sensitive_link):
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("hidden/.env")
    assert exc.value.reason is RejectionReason.SENSITIVE


@POSIX_ONLY
def test_innocuous_symlink_to_sensitive_target_denied_when_symlinks_allowed(
        workspace, sensitive_link):
    """The exact gap the audit identified.

    ``notes-alias.txt`` matches no denylist entry and resolves inside the
    workspace, so a name-only policy allows it. Policy now evaluates the
    effective target, so it is refused.
    """
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("notes-alias.txt")
    assert exc.value.reason is RejectionReason.SENSITIVE_TARGET
    assert exc.value.details["target"] == "hidden/.env"


@POSIX_ONLY
def test_sensitively_named_link_is_caught_by_its_own_name_first(
        workspace, sensitive_link):
    """Defence in depth: ``public-link.pem`` matches ``*.pem`` at step 4a.

    It never reaches the target check, which is the correct (earliest, least
    informative) rejection.
    """
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("public-link.pem")
    assert exc.value.reason is RejectionReason.SENSITIVE


@POSIX_ONLY
@pytest.mark.parametrize("link_name", ["public-link.pem", "notes-alias.txt"])
def test_symlink_to_sensitive_target_denied_when_symlinks_forbidden(
        workspace, sensitive_link, link_name):
    """Default mode refuses either way — on the name or on the symlink rule."""
    guard = WorkspaceGuard(workspace)
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative(link_name)
    assert exc.value.reason in {RejectionReason.SYMLINK,
                                RejectionReason.SENSITIVE}


@POSIX_ONLY
def test_no_credential_content_appears_in_the_rejection(workspace,
                                                        sensitive_link):
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    for name in ("public-link.pem", "notes-alias.txt"):
        with pytest.raises(PathRejectedError) as exc:
            guard.resolve_relative(name)
        blob = str(exc.value) + repr(exc.value.details)
        assert "sk-must-never-leak" not in blob


@POSIX_ONLY
def test_benign_internal_symlink_still_works_when_allowed(workspace):
    """Hardening must not break the legitimate case the option exists for."""
    (workspace / "real.txt").write_text("harmless", encoding="utf-8")
    (workspace / "alias.txt").symlink_to(workspace / "real.txt")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    data, _, _ = guard.read_path("alias.txt")
    assert data == b"harmless"


@POSIX_ONLY
def test_symlink_into_a_sensitive_directory_is_denied(workspace):
    ssh = workspace / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    (workspace / "docs.txt").symlink_to(ssh / "id_rsa")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("docs.txt")
    assert exc.value.reason is RejectionReason.SENSITIVE_TARGET


@POSIX_ONLY
def test_chained_symlink_to_sensitive_target_is_denied(workspace,
                                                       sensitive_link):
    """link -> link -> .env must be caught: resolution collapses the chain."""
    (workspace / "hop1.txt").symlink_to(workspace / "public-link.pem")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("hop1.txt")
    assert exc.value.reason is RejectionReason.SENSITIVE_TARGET


@POSIX_ONLY
def test_symlink_via_sensitive_dir_component_is_denied(workspace):
    """A link whose target sits under .aws/ is refused on the directory rule."""
    aws = workspace / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\n", encoding="utf-8")
    (workspace / "cfg.txt").symlink_to(aws / "credentials")
    guard = WorkspaceGuard(
        workspace, config=FilesystemConfig(allow_internal_symlinks=True))
    with pytest.raises(PathRejectedError) as exc:
        guard.resolve_relative("cfg.txt")
    assert exc.value.reason is RejectionReason.SENSITIVE_TARGET


# ===========================================================================
# Descriptor hygiene
# ===========================================================================

@POSIX_ONLY
def test_successful_read_closes_its_descriptor(workspace):
    guard = WorkspaceGuard(workspace)
    before = open_fd_count()
    for _ in range(50):
        guard.read_path("notes.txt")
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_rejected_open_leaks_no_descriptor(workspace, outside):
    """Every rejection path must close the fd it may have opened."""
    os.link(outside / "secret.txt", workspace / "linked.txt")
    (workspace / "big.bin").write_bytes(b"x" * 5000)
    os.mkfifo(workspace / "pipe")
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=128))
    before = open_fd_count()
    for name in ("linked.txt", "big.bin", "pipe", "missing.txt"):
        for _ in range(25):
            with pytest.raises(PathRejectedError):
                guard.read_path(name)
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_exception_during_read_still_closes(workspace):
    guard = WorkspaceGuard(workspace)
    before = open_fd_count()
    for _ in range(25):
        with pytest.raises(RuntimeError):
            with guard.open_file("notes.txt"):
                raise RuntimeError("boom")
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_close_is_idempotent(workspace):
    handle = WorkspaceGuard(workspace).open_file("notes.txt")
    handle.close()
    handle.close()  # must not raise


@POSIX_ONLY
def test_descriptor_is_not_retained_in_guard_state(workspace):
    """No long-lived object may hold an fd."""
    guard = WorkspaceGuard(workspace)
    guard.read_path("notes.txt")
    for value in vars(guard).values():
        assert not isinstance(value, OpenFile)
        assert not isinstance(value, int) or value > 10_000 or value < 0


def test_open_file_is_not_serialisable_into_state():
    """An OpenFile must never end up in persisted run state."""
    import json
    from agentcore.fs.paths import FileFacts

    facts = FileFacts(relative="a.txt", kind=PathKind.REGULAR, size=1,
                      device=1, inode=2, links=1)
    handle = OpenFile(fd=3, facts=facts, relative="a.txt")
    with pytest.raises(TypeError):
        json.dumps(handle)
    # The facts, however, are plain data and safe to record.
    assert json.dumps(facts.to_dict())


@POSIX_ONLY
def test_many_sequential_reads_do_not_exhaust_descriptors(workspace):
    """Guards against a slow leak that only shows up under load."""
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    guard = WorkspaceGuard(workspace)
    for _ in range(min(500, soft // 2)):
        data, _, _ = guard.read_path("notes.txt")
        assert data == b"plain notes\n"


# ===========================================================================
# Ordering
# ===========================================================================

@POSIX_ONLY
def test_sensitive_name_beats_existence(workspace):
    """Policy runs before any filesystem access, so absence is not revealed."""
    guard = WorkspaceGuard(workspace)
    assert not (workspace / ".env").exists()
    with pytest.raises(PathRejectedError) as exc:
        guard.open_file(".env")
    assert exc.value.reason is RejectionReason.SENSITIVE


@POSIX_ONLY
def test_traversal_beats_sensitive_target(workspace, outside):
    """Lexical checks run first: cheapest and least informative rejection."""
    guard = WorkspaceGuard(workspace)
    with pytest.raises(PathRejectedError) as exc:
        guard.open_file("../outside/secret.txt")
    assert exc.value.reason is RejectionReason.TRAVERSAL


@POSIX_ONLY
def test_containment_beats_hardlink_policy(workspace, outside):
    """An escaping path is refused before anything is opened."""
    guard = WorkspaceGuard(workspace)
    with pytest.raises(PathRejectedError) as exc:
        guard.open_file("../outside/secret.txt")
    assert exc.value.reason is not RejectionReason.MULTIPLE_HARD_LINKS


def test_single_authoritative_entry_point_exists():
    """read_file must have exactly one door to walk through."""
    assert hasattr(WorkspaceGuard, "open_file")
    assert hasattr(WorkspaceGuard, "read_path")
    # The unsound pre-open API is gone.
    assert not hasattr(WorkspaceGuard, "require_readable_file")
    assert not hasattr(WorkspaceGuard, "open_regular")


def test_open_verified_requires_explicit_bounds():
    """The primitive cannot be called without stating its limits."""
    import inspect

    sig = inspect.signature(open_verified)
    for name in ("max_bytes", "allow_multiple_links", "relative"):
        assert sig.parameters[name].default is inspect.Parameter.empty


# ===========================================================================
# Truncation honesty (defect found while building read_file, Task 34)
# ===========================================================================

@POSIX_ONLY
def test_file_growing_during_read_is_reported_truncated(workspace):
    """A file that grows mid-read must never look complete.

    ``read_from`` previously compared the byte count against the size recorded
    at open time. A file that grew during the read filled the ceiling exactly
    and was reported complete — silently presenting a prefix as the whole
    file. Truncation is now decided by probing the descriptor.
    """
    path = workspace / "grow.bin"
    path.write_bytes(b"a" * 300)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=128))
    with guard.open_file("grow.bin") as handle:
        assert handle.facts.size == 300
        with open(path, "ab") as fh:
            fh.write(b"b" * 100_000)
        data, truncated = guard.read_from(handle)
    assert len(data) == 1000
    assert truncated is True


@POSIX_ONLY
def test_complete_file_is_not_reported_truncated(workspace):
    path = workspace / "exact.bin"
    path.write_bytes(b"a" * 1000)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=1000,
                                                   read_chunk_bytes=128))
    with guard.open_file("exact.bin") as handle:
        data, truncated = guard.read_from(handle)
    assert len(data) == 1000
    assert truncated is False


@POSIX_ONLY
def test_has_more_does_not_disturb_the_read_offset(workspace):
    (workspace / "probe.txt").write_text("abcdefghij", encoding="utf-8")
    guard = WorkspaceGuard(workspace)
    with guard.open_file("probe.txt") as handle:
        first = os.read(handle.fd, 4)
        assert handle.has_more() is True
        rest = os.read(handle.fd, 100)
    assert first == b"abcd"
    assert rest == b"efghij"
