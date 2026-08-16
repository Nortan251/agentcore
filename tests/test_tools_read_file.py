"""Tests for the ``read_file`` builtin tool.

The tool deliberately owns almost no logic: every filesystem safety decision
belongs to :class:`~agentcore.fs.guard.WorkspaceGuard`. So these tests fall
into two groups:

* what the tool genuinely adds — UTF-8 decoding, truncation reporting, result
  shape, error mapping, descriptor hygiene;
* integrated proof that routing a request through the tool does not open a
  bypass around any guard protection.

The second group does not re-test the guard's internals (``test_fs_paths.py``
and ``test_fs_toctou.py`` do that). It proves the *tool* still refuses.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agentcore.config.schema import (
    AgentConfig,
    FilesystemConfig,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.fs.guard import WorkspaceGuard
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.tools.base import ToolContext
from agentcore.tools.builtin import default_tools
from agentcore.tools.builtin.read_file import ReadFileTool

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "src" / "utils").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "src" / "utils" / "helper.py").write_text("X = 1\n",
                                                      encoding="utf-8")
    (root / "notes.txt").write_text("plain notes\n", encoding="utf-8")
    (root / "empty.txt").write_bytes(b"")
    return root


@pytest.fixture
def outside(tmp_path):
    other = tmp_path / "outside"
    other.mkdir()
    (other / "secret.txt").write_text("TOP SECRET OUTSIDE\n", encoding="utf-8")
    return other


def make_context(workspace, *, config: FilesystemConfig | None = None,
                 logger: Any = None) -> ToolContext:
    """A ToolContext carrying a real guard, as the executor builds one."""
    return ToolContext(workspace=WorkspaceGuard(workspace, config=config),
                       workspace_dir=str(workspace), logger=logger)


def run_tool(workspace, path, *, config=None, logger=None):
    return ReadFileTool().run({"path": path},
                              make_context(workspace, config=config,
                                           logger=logger))


def open_fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:  # pragma: no cover - non-Linux
        return -1


# ===========================================================================
# Spec / registration
# ===========================================================================

def test_spec_is_read_only_and_declares_fs_read():
    spec = ReadFileTool().spec
    assert spec.name == "read_file"
    assert spec.read_only is True
    assert spec.capabilities == frozenset({Capability.FS_READ})


def test_spec_takes_exactly_one_required_string_argument():
    """Minimal surface: no line ranges, no encoding override, no flags."""
    params = ReadFileTool().spec.parameters
    assert len(params) == 1
    assert params[0].name == "path"
    assert params[0].required is True
    assert params[0].type.value == "string"


def test_tool_is_registered_by_default():
    assert "read_file" in {t.spec.name for t in default_tools()}


def test_unknown_arguments_are_rejected_by_validation():
    """Guards against a model inventing 'start_line'/'encoding' etc."""
    from agentcore.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        ReadFileTool().validate_args({"path": "a.txt", "start_line": 3})


def test_missing_path_is_rejected_by_validation():
    from agentcore.errors import ToolValidationError

    with pytest.raises(ToolValidationError):
        ReadFileTool().validate_args({})


# ===========================================================================
# Basic reading
# ===========================================================================

def test_reads_a_text_file(workspace):
    result = run_tool(workspace, "notes.txt")
    assert result.ok
    assert result.output == "plain notes\n"
    assert result.metadata["path"] == "notes.txt"
    assert result.metadata["truncated"] is False
    assert result.metadata["encoding"] == "utf-8"
    assert result.metadata["bytes_read"] == 12
    assert result.metadata["size_bytes"] == 12


def test_reads_a_nested_file(workspace):
    result = run_tool(workspace, "src/utils/helper.py")
    assert result.ok and result.output == "X = 1\n"
    assert result.metadata["path"] == "src/utils/helper.py"


def test_reads_a_dot_prefixed_relative_path(workspace):
    result = run_tool(workspace, "./src/app.py")
    assert result.ok and result.output == "print('hello')\n"
    # Normalised by the guard, reported without the './'.
    assert result.metadata["path"] == "src/app.py"


def test_empty_file_is_success_not_an_error(workspace):
    result = run_tool(workspace, "empty.txt")
    assert result.ok is True
    assert result.output == ""
    assert result.metadata["bytes_read"] == 0
    assert result.metadata["size_bytes"] == 0
    assert result.metadata["truncated"] is False


def test_unicode_text_round_trips(workspace):
    content = "héllo wörld ✓ 日本語 🎉 emoji\n"
    (workspace / "unicode.txt").write_text(content, encoding="utf-8")
    result = run_tool(workspace, "unicode.txt")
    assert result.ok and result.output == content


def test_crlf_and_tabs_are_preserved_verbatim(workspace):
    content = "line1\r\n\tindented\r\n"
    (workspace / "win.txt").write_bytes(content.encode("utf-8"))
    result = run_tool(workspace, "win.txt")
    assert result.ok and result.output == content


def test_result_output_is_json_serialisable(workspace):
    """Only serialisable data may reach the executor/conversation."""
    result = run_tool(workspace, "notes.txt")
    assert json.dumps(result.to_dict())


# ===========================================================================
# Binary / decoding policy
# ===========================================================================

def test_invalid_utf8_is_a_structured_failure(workspace):
    (workspace / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary\x80\x81")
    result = run_tool(workspace, "blob.bin")
    assert result.ok is False
    assert result.metadata["kind"] == "not_utf8_text"
    assert "utf-8" in result.error.lower()


def test_binary_content_is_never_lossily_decoded(workspace):
    """No U+FFFD substitution: the model must not receive fabricated text."""
    (workspace / "blob.bin").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xd8\xff")
    result = run_tool(workspace, "blob.bin")
    assert result.ok is False
    assert "\ufffd" not in str(result.output)
    assert "\ufffd" not in (result.error or "")


def test_decision_is_not_based_on_file_extension(workspace):
    """A .png holding UTF-8 text reads; a .py holding binary does not."""
    (workspace / "actually_text.png").write_text("just text\n",
                                                 encoding="utf-8")
    (workspace / "actually_binary.py").write_bytes(b"\xff\xfe\xfd\xfc")

    text_result = run_tool(workspace, "actually_text.png")
    assert text_result.ok and text_result.output == "just text\n"

    binary_result = run_tool(workspace, "actually_binary.py")
    assert binary_result.ok is False
    assert binary_result.metadata["kind"] == "not_utf8_text"


def test_nul_bytes_inside_valid_utf8_are_still_returned(workspace):
    """A NUL is legal UTF-8; the policy is about decodability, not taste."""
    (workspace / "nul.txt").write_bytes(b"a\x00b")
    result = run_tool(workspace, "nul.txt")
    assert result.ok and result.output == "a\x00b"


def test_failure_does_not_leak_binary_content(workspace):
    (workspace / "blob.bin").write_bytes(b"\xffSECRETBYTES\xfe")
    result = run_tool(workspace, "blob.bin")
    assert "SECRETBYTES" not in json.dumps(result.to_dict(), default=str)


# ===========================================================================
# Bounds and truncation
# ===========================================================================

def small_limit(n=1000, chunk=128) -> FilesystemConfig:
    return FilesystemConfig(max_file_bytes=n, read_chunk_bytes=chunk)


def grow_after_open(workspace, path, extra: bytes, monkeypatch):
    """A guard whose files grow *after* being opened and verified.

    The guard refuses a file that is already oversized, so this is the honest
    way to reach the truncation path: the file passes the size check at open
    time and only then exceeds the ceiling. It is also the real race the
    bounded reader exists to survive.
    """
    guard = WorkspaceGuard(workspace, config=small_limit())
    real_open = guard.open_file

    def open_then_grow(requested):
        handle = real_open(requested)
        with open(path, "ab") as fh:
            fh.write(extra)
        return handle

    monkeypatch.setattr(guard, "open_file", open_then_grow)
    return guard


def test_file_exactly_at_the_limit_is_complete(workspace):
    (workspace / "exact.txt").write_bytes(b"a" * 1000)
    result = run_tool(workspace, "exact.txt", config=small_limit())
    assert result.ok
    assert result.metadata["truncated"] is False
    assert result.metadata["bytes_read"] == 1000
    assert len(result.output) == 1000


def test_file_one_byte_over_the_limit_is_refused_by_the_guard(workspace):
    """The guard rejects oversized files outright before any read."""
    (workspace / "over.txt").write_bytes(b"a" * 1001)
    result = run_tool(workspace, "over.txt", config=small_limit())
    assert result.ok is False
    assert result.metadata["reason"] == "too_large"


def test_very_large_file_is_refused_not_loaded(workspace):
    (workspace / "huge.bin").write_bytes(b"x" * 5_000_000)
    result = run_tool(workspace, "huge.bin", config=small_limit())
    assert result.ok is False
    assert result.metadata["reason"] == "too_large"


def test_file_that_grows_after_opening_is_truncated_and_says_so(workspace,
                                                                monkeypatch):
    """Truncation is reachable when a file grows between open and read.

    The guard refuses a file already over the limit, so the honest way to
    exercise the truncation path is the race it exists for.
    """
    path = workspace / "grow.txt"
    path.write_bytes(b"a" * 500)
    guard = grow_after_open(workspace, path, b"b" * 100_000, monkeypatch)
    result = ReadFileTool().run({"path": "grow.txt"},
                                ToolContext(workspace=guard))
    assert result.ok
    assert result.metadata["truncated"] is True
    assert result.metadata["bytes_read"] == 1000


def test_truncation_is_stated_in_the_output_the_model_sees(workspace,
                                                           monkeypatch):
    """metadata never reaches the model, so the notice must be in the output."""
    path = workspace / "grow.txt"
    path.write_bytes(b"a" * 500)
    guard = grow_after_open(workspace, path, b"b" * 100_000, monkeypatch)
    result = ReadFileTool().run({"path": "grow.txt"},
                                ToolContext(workspace=guard))
    assert result.output.startswith("[truncated:")
    assert "truncated" in result.output[:80]


def test_complete_file_carries_no_truncation_notice(workspace):
    result = run_tool(workspace, "notes.txt")
    assert "truncated" not in result.output


def test_truncated_multibyte_character_is_dropped_not_called_binary(
        workspace, monkeypatch):
    """A cut through a multi-byte char is our artefact, not binary content."""
    path = workspace / "uni.txt"
    path.write_bytes("✓".encode() * 100)  # 300 bytes, under the limit
    # The ceiling is 1000, which is not a multiple of 3, so once the file has
    # grown the cut lands in the middle of a character.
    guard = grow_after_open(workspace, path, "✓".encode() * 400, monkeypatch)
    result = ReadFileTool().run({"path": "uni.txt"},
                                ToolContext(workspace=guard))
    assert result.ok, result.error
    assert result.metadata["truncated"] is True
    assert "\ufffd" not in result.output
    assert result.output.rstrip("\n").endswith("✓")


def test_reading_is_incremental_not_one_big_slurp(workspace):
    """Chunked reads keep peak memory bounded by the chunk size."""
    (workspace / "chunky.bin").write_bytes(b"z" * 4096)
    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_file_bytes=8192,
                                                   read_chunk_bytes=512))
    seen: list[int] = []
    real_iter = guard.iter_from

    def spy(handle):
        for chunk in real_iter(handle):
            seen.append(len(chunk))
            yield chunk

    guard.iter_from = spy  # type: ignore[method-assign]
    result = ReadFileTool().run({"path": "chunky.bin"},
                                ToolContext(workspace=guard))
    assert result.ok
    assert len(seen) > 1, "file was read in a single call"
    assert max(seen) <= 512


# ===========================================================================
# Security regression — the tool must not bypass the guard
# ===========================================================================

@pytest.mark.parametrize("path,reason", [
    ("../outside/secret.txt", "traversal"),
    ("../../etc/passwd", "traversal"),
    ("/etc/passwd", "absolute"),
    ("C:\\Windows\\System32\\config\\SAM", "drive_qualified"),
    ("\\\\server\\share\\x", "unc"),
    ("src\\app.py", "backslash"),
    ("", "empty"),
])
def test_malformed_and_escaping_paths_are_refused(workspace, path, reason):
    result = run_tool(workspace, path)
    assert result.ok is False
    assert result.metadata["reason"] == reason


def test_sibling_prefix_path_is_refused(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    sibling = tmp_path / "workspace-other"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("LOOT\n", encoding="utf-8")
    result = run_tool(root, "../workspace-other/loot.txt")
    assert result.ok is False
    assert "LOOT" not in str(result.output)


@POSIX_ONLY
def test_external_symlink_is_refused(workspace, outside):
    (workspace / "escape.txt").symlink_to(outside / "secret.txt")
    result = run_tool(workspace, "escape.txt")
    assert result.ok is False
    assert result.metadata["reason"] == "symlink"
    assert "TOP SECRET" not in json.dumps(result.to_dict(), default=str)


@POSIX_ONLY
def test_internal_symlink_refused_in_default_mode(workspace):
    (workspace / "alias.txt").symlink_to(workspace / "notes.txt")
    result = run_tool(workspace, "alias.txt")
    assert result.ok is False
    assert result.metadata["reason"] == "symlink"


@POSIX_ONLY
def test_sensitive_file_is_refused(workspace):
    (workspace / ".env").write_text("API_KEY=sk-must-never-leak\n",
                                    encoding="utf-8")
    result = run_tool(workspace, ".env")
    assert result.ok is False
    assert result.metadata["reason"] == "sensitive"
    assert "sk-must-never-leak" not in json.dumps(result.to_dict(),
                                                  default=str)


@POSIX_ONLY
def test_sensitive_target_behind_allowed_internal_symlink_is_refused(
        workspace):
    """The Task 33 finding, now exercised through the tool."""
    hidden = workspace / "hidden"
    hidden.mkdir()
    (hidden / ".env").write_text("API_KEY=sk-must-never-leak\n",
                                 encoding="utf-8")
    (workspace / "innocent.txt").symlink_to(hidden / ".env")
    result = run_tool(workspace, "innocent.txt",
                      config=FilesystemConfig(allow_internal_symlinks=True))
    assert result.ok is False
    assert result.metadata["reason"] == "sensitive_target"
    assert "sk-must-never-leak" not in json.dumps(result.to_dict(),
                                                  default=str)


@POSIX_ONLY
def test_hardlink_to_outside_file_is_refused(workspace, outside):
    try:
        os.link(outside / "secret.txt", workspace / "innocent.txt")
    except OSError as exc:  # pragma: no cover - filesystem dependent
        if exc.errno in {errno.EPERM, errno.EXDEV, errno.EOPNOTSUPP}:
            pytest.skip(f"cannot create hard links: {exc}")
        raise
    result = run_tool(workspace, "innocent.txt")
    assert result.ok is False
    assert result.metadata["reason"] == "multiple_hard_links"
    assert "TOP SECRET" not in json.dumps(result.to_dict(), default=str)


@POSIX_ONLY
def test_fifo_is_refused_and_does_not_hang(workspace):
    os.mkfifo(workspace / "pipe")
    result = run_tool(workspace, "pipe")
    assert result.ok is False
    assert result.metadata["reason"] == "not_a_regular_file"


def test_directory_is_refused(workspace):
    result = run_tool(workspace, "src")
    assert result.ok is False
    assert result.metadata["reason"] == "not_a_regular_file"


def test_missing_file_is_refused(workspace):
    result = run_tool(workspace, "nope.txt")
    assert result.ok is False
    assert result.metadata["reason"] == "not_found"


def test_tool_source_never_opens_files_itself():
    """AST proof that the tool has no independent filesystem access.

    A textual scan would match docstrings, so the tree is parsed and call
    targets inspected. This is what keeps ``WorkspaceGuard`` authoritative.
    """
    import ast
    from agentcore.tools.builtin import read_file as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    banned_names = {"open"}
    banned_attrs = {"open", "read_text", "read_bytes", "write_text",
                    "write_bytes", "unlink", "resolve", "lstat", "stat",
                    "iterdir", "glob", "rglob", "mkdir"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in banned_names, f"line {node.lineno}"
            if isinstance(func, ast.Attribute):
                assert func.attr not in banned_attrs, \
                    f"{func.attr} at line {node.lineno}"
        # No `import os` / `from pathlib import Path` either.
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = {a.name for a in node.names}
            assert mod not in {"os", "pathlib", "io", "shutil"}, mod
            assert not ({"os", "pathlib", "shutil"} & names)


def test_tool_uses_the_guard_entry_point():
    """It must call open_file, not a lower-level guard method."""
    import ast
    from agentcore.tools.builtin import read_file as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "open_file" in called
    # Not the pre-validated variant, which would let a caller pass an
    # unvalidated SafePath.
    assert "open_safe" not in called


def test_missing_workspace_is_a_failure_not_a_fallback(workspace):
    """With no guard the tool must refuse, never read by another route."""
    result = ReadFileTool().run({"path": "notes.txt"}, ToolContext())
    assert result.ok is False
    assert result.metadata["kind"] == "no_workspace"


# ===========================================================================
# Descriptor hygiene
# ===========================================================================

@POSIX_ONLY
def test_successful_reads_do_not_leak_descriptors(workspace):
    before = open_fd_count()
    for _ in range(100):
        assert run_tool(workspace, "notes.txt").ok
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_decode_failure_does_not_leak_a_descriptor(workspace):
    (workspace / "blob.bin").write_bytes(b"\xff\xfe" * 50)
    before = open_fd_count()
    for _ in range(100):
        assert run_tool(workspace, "blob.bin").ok is False
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_rejected_paths_do_not_leak_descriptors(workspace, outside):
    (workspace / ".env").write_text("k=v\n", encoding="utf-8")
    os.mkfifo(workspace / "pipe")
    before = open_fd_count()
    for name in (".env", "pipe", "missing.txt", "../outside/secret.txt",
                 "src"):
        for _ in range(20):
            assert run_tool(workspace, name).ok is False
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_read_failure_midway_still_closes(workspace, monkeypatch):
    guard = WorkspaceGuard(workspace)

    def boom(handle):
        raise OSError(errno.EIO, "simulated I/O error")

    monkeypatch.setattr(guard, "read_from", boom)
    before = open_fd_count()
    for _ in range(50):
        with pytest.raises(OSError):
            ReadFileTool().run({"path": "notes.txt"},
                               ToolContext(workspace=guard))
    assert open_fd_count() <= before + 1


def test_result_never_carries_an_open_file(workspace):
    from agentcore.fs.opened import OpenFile

    result = run_tool(workspace, "notes.txt")
    assert not isinstance(result.output, OpenFile)
    for value in result.metadata.values():
        assert not isinstance(value, OpenFile)
        assert not isinstance(value, int) or value < 10_000_000


# ===========================================================================
# Error safety
# ===========================================================================

def test_errors_do_not_reveal_the_absolute_host_path(workspace, outside):
    for name in ("nope.txt", "../outside/secret.txt", "src"):
        result = run_tool(workspace, name)
        blob = json.dumps(result.to_dict(), default=str)
        assert str(workspace) not in blob
        assert str(outside) not in blob


def test_errors_contain_no_stack_trace(workspace):
    result = run_tool(workspace, "nope.txt")
    blob = json.dumps(result.to_dict(), default=str)
    assert "Traceback" not in blob
    assert "File \"" not in blob


def test_rejection_reason_is_machine_readable(workspace):
    result = run_tool(workspace, "/etc/passwd")
    assert result.metadata["reason"] == "absolute"
    assert result.metadata["kind"] == "path_rejected"


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


def test_logging_never_includes_file_content_or_paths(workspace):
    (workspace / "secretish.txt").write_text("PASSWORD=hunter2\n",
                                             encoding="utf-8")
    logger = RecordingLogger()
    result = run_tool(workspace, "secretish.txt", logger=logger)
    assert result.ok
    blob = json.dumps(logger.events, default=str)
    assert "hunter2" not in blob
    assert "secretish" not in blob
    assert str(workspace) not in blob


def test_logging_records_safe_counters(workspace):
    logger = RecordingLogger()
    run_tool(workspace, "notes.txt", logger=logger)
    fields = [f for _, _, f in logger.events if "bytes_read" in f]
    assert fields and fields[0]["bytes_read"] == 12
    assert fields[0]["truncated"] is False


def test_rejection_logging_records_only_the_category(workspace):
    (workspace / ".env").write_text("API_KEY=sk-leak\n", encoding="utf-8")
    logger = RecordingLogger()
    run_tool(workspace, ".env", logger=logger)
    blob = json.dumps(logger.events, default=str)
    assert "sk-leak" not in blob
    assert "sensitive" in blob


def test_tool_works_without_a_logger(workspace):
    """context.logger may be None outside the executor."""
    assert ReadFileTool().run(
        {"path": "notes.txt"},
        ToolContext(workspace=WorkspaceGuard(workspace))).ok
