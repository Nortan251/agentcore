"""Tests for the ``list_directory`` builtin tool and its guard primitive.

The tool owns no security logic, so these tests split into:

* the guard's listing primitive — classification, sensitive redaction, symlink
  handling, bounds, ordering;
* the tool — rendering, result shape, error mapping;
* integrated proof that listing opens no bypass and reads no content.

Everything uses real filesystem fixtures.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import (
    REDACTED_NAME,
    LinkTarget,
    PathKind,
    PathRejectedError,
    RejectionReason,
)
from agentcore.permissions.levels import Capability
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from agentcore.tools.builtin.list_directory import ListDirectoryTool

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print(1)\n", encoding="utf-8")
    (root / "README.md").write_text("# docs\n", encoding="utf-8")
    (root / "notes.txt").write_text("notes\n", encoding="utf-8")
    return root


@pytest.fixture
def outside(tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")
    (other / "hoard").mkdir()
    (other / "hoard" / "loot.txt").write_text("LOOT\n", encoding="utf-8")
    return other


def listing(workspace, path=".", *, config=None):
    guard = WorkspaceGuard(workspace, config=config)
    return guard.list_directory(guard.resolve_directory(path))


def run_tool(workspace, path=".", *, config=None, logger=None):
    guard = WorkspaceGuard(workspace, config=config)
    return ListDirectoryTool().run({"path": path},
                                   ToolContext(workspace=guard,
                                               logger=logger))


def names_of(result_listing):
    return [e.display_name for e in result_listing.entries]


def open_fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:  # pragma: no cover - non-Linux
        return -1


# ===========================================================================
# Spec / registration
# ===========================================================================

def test_spec_is_read_only_with_fs_read():
    spec = ListDirectoryTool().spec
    assert spec.name == "list_directory"
    assert spec.read_only is True
    assert spec.capabilities == frozenset({Capability.FS_READ})


def test_spec_takes_exactly_one_required_argument():
    """No recursion, glob, sort or verbosity switches."""
    params = ListDirectoryTool().spec.parameters
    assert len(params) == 1
    assert params[0].name == "path"
    assert params[0].required is True


def test_registered_by_default():
    assert "list_directory" in {t.spec.name for t in default_tools()}


@pytest.mark.parametrize("extra", [
    {"recursive": True}, {"pattern": "*.py"}, {"depth": 2}, {"sort": "size"},
])
def test_unknown_arguments_are_rejected(extra):
    from agentcore.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        ListDirectoryTool().validate_args({"path": ".", **extra})


# ===========================================================================
# Basic listing
# ===========================================================================

def test_empty_directory(workspace):
    (workspace / "empty").mkdir()
    result = listing(workspace, "empty")
    assert result.entries == ()
    assert result.truncated is False


def test_empty_directory_renders_explicitly(workspace):
    (workspace / "empty").mkdir()
    assert "(empty directory)" in run_tool(workspace, "empty").output


def test_single_file(workspace):
    (workspace / "solo").mkdir()
    (workspace / "solo" / "only.txt").write_text("x", encoding="utf-8")
    assert names_of(listing(workspace, "solo")) == ["only.txt"]


def test_lists_workspace_root_with_dot(workspace):
    assert set(names_of(listing(workspace, "."))) == {
        "src", "README.md", "notes.txt"}


def test_root_listing_reports_relative_dot(workspace):
    assert listing(workspace, ".").relative == "."


def test_nested_directory(workspace):
    assert names_of(listing(workspace, "src")) == ["app.py"]


def test_only_immediate_children_are_listed(workspace):
    """One level: a nested file must not appear in the parent listing."""
    (workspace / "src" / "deep").mkdir()
    (workspace / "src" / "deep" / "buried.txt").write_text("x",
                                                           encoding="utf-8")
    assert set(names_of(listing(workspace, "src"))) == {"app.py", "deep"}
    assert "buried.txt" not in names_of(listing(workspace, "src"))


def test_directories_are_marked_in_the_rendering(workspace):
    output = run_tool(workspace, ".").output
    assert "src/" in output
    assert "(directory)" in output


# ===========================================================================
# Deterministic ordering
# ===========================================================================

def test_ordering_is_lexical_by_byte_value(workspace):
    target = workspace / "ordered"
    target.mkdir()
    for name in ("banana", "Apple", "cherry", "_under", "10", "2"):
        (target / name).write_text("x", encoding="utf-8")
    assert names_of(listing(workspace, "ordered")) == sorted(
        ["banana", "Apple", "cherry", "_under", "10", "2"])


def test_ordering_is_stable_across_calls(workspace):
    target = workspace / "many"
    target.mkdir()
    for i in range(40):
        (target / f"f{i}.txt").write_text("x", encoding="utf-8")
    first = names_of(listing(workspace, "many"))
    for _ in range(5):
        assert names_of(listing(workspace, "many")) == first


def test_ordering_is_case_sensitive_and_locale_independent(workspace):
    """Documents the rule: byte-value order, so 'Z' sorts before 'a'."""
    target = workspace / "case"
    target.mkdir()
    for name in ("Zebra", "apple", "Banana"):
        (target / name).write_text("x", encoding="utf-8")
    assert names_of(listing(workspace, "case")) == ["Banana", "Zebra", "apple"]


# ===========================================================================
# Entry classification
# ===========================================================================

def test_regular_files_and_directories_are_classified(workspace):
    kinds = {e.display_name: e.kind for e in listing(workspace, ".").entries}
    assert kinds["README.md"] is PathKind.REGULAR
    assert kinds["src"] is PathKind.DIRECTORY


@POSIX_ONLY
def test_fifo_is_classified_not_opened(workspace):
    os.mkfifo(workspace / "pipe")
    kinds = {e.display_name: e.kind for e in listing(workspace, ".").entries}
    assert kinds["pipe"] is PathKind.FIFO


@POSIX_ONLY
def test_socket_is_classified(workspace):
    import socket

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(workspace / "sock"))
        kinds = {e.display_name: e.kind
                 for e in listing(workspace, ".").entries}
        assert kinds["sock"] is PathKind.SOCKET
    finally:
        srv.close()


# ===========================================================================
# Symlinks — classified, never followed
# ===========================================================================

@POSIX_ONLY
def test_internal_symlink_is_reported_without_following(workspace):
    (workspace / "alias").symlink_to(workspace / "notes.txt")
    entry = {e.display_name: e for e in listing(workspace, ".").entries}["alias"]
    assert entry.kind is PathKind.SYMLINK
    assert entry.link_target is LinkTarget.INTERNAL


@POSIX_ONLY
def test_external_symlink_is_flagged_but_target_is_not_leaked(workspace,
                                                              outside):
    (workspace / "escape").symlink_to(outside / "secret.txt")
    result = listing(workspace, ".")
    entry = {e.display_name: e for e in result.entries}["escape"]
    assert entry.kind is PathKind.SYMLINK
    assert entry.link_target is LinkTarget.EXTERNAL
    blob = json.dumps(result.to_dict(), default=str)
    assert str(outside) not in blob
    assert "secret.txt" not in blob
    assert "TOP SECRET" not in blob


@POSIX_ONLY
def test_broken_symlink_is_reported_as_broken(workspace):
    (workspace / "dangling").symlink_to(workspace / "nothing_here")
    entry = {e.display_name: e
             for e in listing(workspace, ".").entries}["dangling"]
    assert entry.kind is PathKind.SYMLINK
    assert entry.link_target is LinkTarget.BROKEN


@POSIX_ONLY
def test_symlinked_directory_is_not_recursively_enumerated(workspace,
                                                           outside):
    (workspace / "portal").symlink_to(outside / "hoard",
                                      target_is_directory=True)
    result = listing(workspace, ".")
    entry = {e.display_name: e for e in result.entries}["portal"]
    assert entry.kind is PathKind.SYMLINK
    # The linked directory's contents must not appear anywhere.
    blob = json.dumps(result.to_dict(), default=str)
    assert "loot.txt" not in blob
    assert "LOOT" not in blob


@POSIX_ONLY
def test_internal_symlinked_directory_is_not_entered(workspace):
    (workspace / "inner").symlink_to(workspace / "src", target_is_directory=True)
    result = listing(workspace, ".")
    blob = json.dumps(result.to_dict(), default=str)
    # 'app.py' lives inside src/; listing the root must not reveal it.
    assert "app.py" not in blob


@POSIX_ONLY
def test_listing_a_symlinked_directory_is_refused(workspace, outside):
    """The guard refuses to treat a symlink as a listable directory."""
    (workspace / "portal").symlink_to(outside, target_is_directory=True)
    result = run_tool(workspace, "portal")
    assert result.ok is False
    assert result.metadata["reason"] == "symlink"


# ===========================================================================
# Sensitive entries
# ===========================================================================

SENSITIVE_NAMES = [".env", ".env.production", ".git-credentials", ".netrc",
                   "id_rsa", "id_ed25519", "server.pem", "private.key"]


@POSIX_ONLY
@pytest.mark.parametrize("name", SENSITIVE_NAMES)
def test_sensitive_entry_name_is_withheld(workspace, name):
    (workspace / name).write_text("credential material\n", encoding="utf-8")
    result = listing(workspace, ".")
    shown = names_of(result)
    assert name not in shown, f"{name} leaked into the listing"
    assert REDACTED_NAME in shown
    assert result.sensitive_count == 1


@POSIX_ONLY
def test_sensitive_entry_still_reports_its_type(workspace):
    """The model learns something exists, without learning its name."""
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    entry = [e for e in listing(workspace, ".").entries if e.sensitive][0]
    assert entry.name is None
    assert entry.display_name == REDACTED_NAME
    assert entry.kind is PathKind.REGULAR
    assert entry.to_dict()["sensitive"] is True


@POSIX_ONLY
def test_case_variation_is_still_withheld(workspace):
    (workspace / "ID_RSA").write_text("key\n", encoding="utf-8")
    assert "ID_RSA" not in names_of(listing(workspace, "."))


@POSIX_ONLY
def test_nested_sensitive_entry_is_withheld(workspace):
    (workspace / "src" / ".env").write_text("k=v\n", encoding="utf-8")
    result = listing(workspace, "src")
    assert ".env" not in names_of(result)
    assert result.sensitive_count == 1


@POSIX_ONLY
def test_symlink_named_sensitively_is_withheld(workspace):
    (workspace / "id_rsa").symlink_to(workspace / "notes.txt")
    result = listing(workspace, ".")
    assert "id_rsa" not in names_of(result)
    entry = [e for e in result.entries if e.sensitive][0]
    assert entry.kind is PathKind.SYMLINK


@POSIX_ONLY
def test_ordinary_names_are_not_over_classified(workspace):
    """Files that merely mention credentials stay fully visible."""
    ordinary = ["token_handling.py", "authentication.md", "id_rsa.pub",
                "secrets_tutorial.md", "password_policy.rst",
                "key_bindings.json", "client-passwords.txt"]
    for name in ordinary:
        (workspace / name).write_text("x", encoding="utf-8")
    shown = names_of(listing(workspace, "."))
    for name in ordinary:
        assert name in shown, f"{name} was wrongly withheld"


@POSIX_ONLY
def test_sensitive_directory_entry_is_withheld(workspace):
    (workspace / ".ssh").mkdir()
    result = listing(workspace, ".")
    assert ".ssh" not in names_of(result)
    entry = [e for e in result.entries if e.sensitive][0]
    assert entry.kind is PathKind.DIRECTORY


@POSIX_ONLY
def test_listing_a_sensitive_directory_is_refused(workspace):
    (workspace / ".ssh").mkdir()
    (workspace / ".ssh" / "id_rsa").write_text("key\n", encoding="utf-8")
    result = run_tool(workspace, ".ssh")
    assert result.ok is False
    assert result.metadata["reason"] == "sensitive"


@POSIX_ONLY
def test_sensitive_notice_appears_in_the_rendered_output(workspace):
    """metadata never reaches the model, so the notice must be in output."""
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    output = run_tool(workspace, ".").output
    assert "withheld" in output
    assert ".env" not in output


@POSIX_ONLY
def test_redaction_can_be_disabled_by_configuration(workspace):
    """Consistent with the rest of the layer: the policy is configurable."""
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    result = listing(workspace, ".",
                     config=FilesystemConfig(deny_sensitive_paths=False))
    assert ".env" in names_of(result)


# ===========================================================================
# Bounds and truncation
# ===========================================================================

def populate(workspace, count, name="dir"):
    target = workspace / name
    target.mkdir()
    for i in range(count):
        (target / f"file{i:04d}.txt").write_text("x", encoding="utf-8")
    return target


def test_exactly_at_the_entry_limit_is_not_truncated(workspace):
    populate(workspace, 10)
    result = listing(workspace, "dir",
                     config=FilesystemConfig(max_directory_entries=10))
    assert len(result.entries) == 10
    assert result.truncated is False


def test_one_over_the_limit_is_truncated(workspace):
    populate(workspace, 11)
    result = listing(workspace, "dir",
                     config=FilesystemConfig(max_directory_entries=10))
    assert len(result.entries) == 10
    assert result.truncated is True
    assert result.limit == 10


def test_large_directory_is_bounded(workspace):
    populate(workspace, 500)
    result = listing(workspace, "dir",
                     config=FilesystemConfig(max_directory_entries=25))
    assert len(result.entries) == 25
    assert result.truncated is True


def test_truncation_is_stated_in_the_output(workspace):
    populate(workspace, 30)
    output = run_tool(workspace, "dir",
                      config=FilesystemConfig(max_directory_entries=5)).output
    assert "truncated" in output.lower()


def test_truncated_listing_keeps_deterministic_prefix(workspace):
    populate(workspace, 50)
    config = FilesystemConfig(max_directory_entries=7)
    first = names_of(listing(workspace, "dir", config=config))
    assert first == sorted(first)
    assert names_of(listing(workspace, "dir", config=config)) == first


# ===========================================================================
# Path security — delegated entirely to the guard
# ===========================================================================

@pytest.mark.parametrize("path,reason", [
    ("../outside", "traversal"),
    ("../../etc", "traversal"),
    ("/etc", "absolute"),
    ("C:\\Windows", "drive_qualified"),
    ("\\\\server\\share", "unc"),
    ("src\\deep", "backslash"),
])
def test_unsafe_paths_are_refused(workspace, path, reason):
    result = run_tool(workspace, path)
    assert result.ok is False
    assert result.metadata["reason"] == reason


def test_sibling_prefix_directory_is_refused(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("LOOT\n", encoding="utf-8")
    result = run_tool(root, "../workspace-other")
    assert result.ok is False
    assert "LOOT" not in str(result.output)


def test_missing_directory_is_refused(workspace):
    result = run_tool(workspace, "nowhere")
    assert result.ok is False
    assert result.metadata["reason"] == "not_found"


def test_file_path_is_refused_as_a_directory(workspace):
    result = run_tool(workspace, "notes.txt")
    assert result.ok is False
    assert result.metadata["reason"] == "not_a_directory"


@POSIX_ONLY
def test_unreadable_directory_is_refused_cleanly(workspace):
    if os.geteuid() == 0:
        pytest.skip("running as root; permission bits are not enforced")
    locked = workspace / "locked"
    locked.mkdir()
    (locked / "inside.txt").write_text("x", encoding="utf-8")
    locked.chmod(0o000)
    try:
        result = run_tool(workspace, "locked")
        assert result.ok is False
        assert result.metadata["reason"] == "unreadable"
    finally:
        locked.chmod(0o755)


def test_missing_workspace_is_a_failure(workspace):
    result = ListDirectoryTool().run({"path": "."}, ToolContext())
    assert result.ok is False
    assert result.metadata["kind"] == "no_workspace"


# ===========================================================================
# No content access
# ===========================================================================

def test_tool_source_never_touches_the_filesystem():
    """AST proof: the tool has no independent filesystem access at all."""
    from agentcore.tools.builtin import list_directory as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    banned_attrs = {"open", "read_text", "read_bytes", "write_text",
                    "write_bytes", "iterdir", "scandir", "walk", "glob",
                    "rglob", "listdir", "resolve", "lstat", "stat", "unlink"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id != "open", f"line {node.lineno}"
            if isinstance(func, ast.Attribute):
                assert func.attr not in banned_attrs, \
                    f"{func.attr} at line {node.lineno}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = {a.name for a in node.names}
            assert mod not in {"os", "pathlib", "io", "shutil", "subprocess"}
            assert not ({"os", "pathlib", "shutil", "subprocess"} & names)


def test_tool_uses_the_guard_listing_primitive():
    from agentcore.tools.builtin import list_directory as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "list_directory" in called
    assert "resolve_directory" in called
    # It must never open anything.
    assert "open_file" not in called
    assert "read_from" not in called
    assert "read_path" not in called


@POSIX_ONLY
def test_listing_never_reveals_file_contents(workspace):
    (workspace / "loud.txt").write_text("SENTINEL-CONTENT-XYZ\n",
                                        encoding="utf-8")
    result = run_tool(workspace, ".")
    assert "SENTINEL-CONTENT-XYZ" not in json.dumps(result.to_dict(),
                                                    default=str)


@POSIX_ONLY
def test_listing_a_directory_of_fifos_does_not_hang(workspace):
    """Opening a FIFO would block forever; classification must not open."""
    target = workspace / "pipes"
    target.mkdir()
    for i in range(3):
        os.mkfifo(target / f"pipe{i}")
    result = listing(workspace, "pipes")
    assert len(result.entries) == 3
    assert all(e.kind is PathKind.FIFO for e in result.entries)


# ===========================================================================
# Resource safety
# ===========================================================================

@POSIX_ONLY
def test_repeated_listings_do_not_leak_descriptors(workspace, outside):
    (workspace / "escape").symlink_to(outside / "secret.txt")
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    os.mkfifo(workspace / "pipe")
    before = open_fd_count()
    for _ in range(100):
        assert run_tool(workspace, ".").ok
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_rejected_listings_do_not_leak_descriptors(workspace):
    before = open_fd_count()
    for path in ("nowhere", "notes.txt", "../outside", "/etc"):
        for _ in range(25):
            assert run_tool(workspace, path).ok is False
    assert open_fd_count() <= before + 1


def test_repeated_calls_are_stable(workspace):
    first = run_tool(workspace, ".").output
    for _ in range(5):
        assert run_tool(workspace, ".").output == first


# ===========================================================================
# Result shape and errors
# ===========================================================================

def test_result_metadata_shape(workspace):
    result = run_tool(workspace, ".")
    assert result.ok
    assert result.metadata["path"] == "."
    assert result.metadata["count"] == 3
    assert result.metadata["truncated"] is False
    assert result.metadata["entry_limit"] == 1000
    assert isinstance(result.metadata["entries"], list)


def test_result_is_json_serialisable(workspace):
    assert json.dumps(run_tool(workspace, ".").to_dict())


def test_errors_never_expose_absolute_paths(workspace, outside):
    for path in ("nowhere", "../outside", "notes.txt"):
        blob = json.dumps(run_tool(workspace, path).to_dict(), default=str)
        assert str(workspace) not in blob
        assert str(outside) not in blob


def test_errors_contain_no_traceback(workspace):
    blob = json.dumps(run_tool(workspace, "nowhere").to_dict(), default=str)
    assert "Traceback" not in blob


def test_successful_result_never_contains_absolute_paths(workspace):
    blob = json.dumps(run_tool(workspace, ".").to_dict(), default=str)
    assert str(workspace) not in blob


# ===========================================================================
# Observability
# ===========================================================================

class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level, event, **fields):
        self.events.append((level, event, fields))

    def info(self, event, **f): self._record("info", event, **f)
    def warning(self, event, **f): self._record("warning", event, **f)
    def debug(self, event, **f): self._record("debug", event, **f)
    def error(self, event, **f): self._record("error", event, **f)
    def exception(self, event, **f): self._record("error", event, **f)
    def bind(self, **f): return self


@POSIX_ONLY
def test_logging_records_counters_not_names(workspace):
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    logger = RecordingLogger()
    run_tool(workspace, ".", logger=logger)
    blob = json.dumps(logger.events, default=str)
    assert "README.md" not in blob
    assert ".env" not in blob
    assert str(workspace) not in blob
    assert any("entries" in f for _, _, f in logger.events)


def test_tool_works_without_a_logger(workspace):
    assert run_tool(workspace, ".").ok


@POSIX_ONLY
def test_innocuous_symlink_to_sensitive_target_is_withheld(workspace):
    """A listing must not hint at secrets a read would refuse.

    ``sensitive_link -> .env`` has a harmless name and resolves inside the
    workspace, so a name-only check would show it. Reading through it is
    already refused with SENSITIVE_TARGET (Task 33), and the listing is now
    consistent with that.
    """
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    (workspace / "sensitive_link").symlink_to(workspace / ".env")
    result = listing(workspace, ".")
    shown = names_of(result)
    assert "sensitive_link" not in shown
    assert ".env" not in shown
    assert result.sensitive_count == 2


@POSIX_ONLY
def test_ordinary_internal_symlink_stays_visible(workspace):
    """Only links to credential material are withheld, not all links."""
    (workspace / "alias").symlink_to(workspace / "notes.txt")
    result = listing(workspace, ".")
    assert "alias" in names_of(result)
    assert result.sensitive_count == 0


@POSIX_ONLY
def test_external_symlink_is_not_withheld_merely_for_being_external(
        workspace, outside):
    """External links are flagged, not redacted: the name is the user's own."""
    (workspace / "escape").symlink_to(outside / "secret.txt")
    result = listing(workspace, ".")
    assert "escape" in names_of(result)
    entry = {e.display_name: e for e in result.entries}["escape"]
    assert entry.link_target is LinkTarget.EXTERNAL
