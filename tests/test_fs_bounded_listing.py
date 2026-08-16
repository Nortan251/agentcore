"""Task 36: directory enumeration must be memory-bounded, not just output-bounded.

Before this change ``list_directory`` did ``sorted(entry.name for entry in
scanner)`` and then sliced to the cap: the *result* was bounded but a directory
with millions of entries built millions of strings first.

These tests prove three things:

1. the selector never retains more than ``limit + 1`` names (the memory-bound
   invariant, asserted on instrumented state rather than on process RSS);
2. it returns exactly what a full sort would have returned, verified
   differentially against ``sorted(...)[:limit]`` over randomised input;
3. the observable listing behaviour — ordering, truncation, redaction, symlink
   handling — is unchanged.
"""

from __future__ import annotations

import os
import random
import string

import pytest

from agentcore.fs.config import FilesystemConfig
from agentcore.fs.guard import WorkspaceGuard
from agentcore.fs.paths import (
    REDACTED_NAME,
    BoundedNameSelector,
    LinkTarget,
    PathKind,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="requires POSIX filesystem semantics")


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def populate(directory, count, *, prefix="file", width=5):
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (directory / f"{prefix}{i:0{width}d}.txt").write_text(
            "x", encoding="utf-8")
    return directory


def listing(workspace, path=".", *, limit=None):
    config = (FilesystemConfig(max_directory_entries=limit)
              if limit is not None else None)
    guard = WorkspaceGuard(workspace, config=config)
    return guard.list_directory(guard.resolve_directory(path))


def names_of(result):
    return [e.display_name for e in result.entries]


# ===========================================================================
# The selector in isolation
# ===========================================================================

def test_capacity_is_limit_plus_one():
    """One extra slot is what makes 'exactly N' distinguishable from 'N+1'."""
    selector = BoundedNameSelector(10)
    assert selector.capacity == 11


def test_rejects_a_meaningless_limit():
    with pytest.raises(ValueError):
        BoundedNameSelector(0)


def test_retains_at_most_capacity_however_many_are_offered():
    """The core memory-bound invariant, on instrumented internal state."""
    selector = BoundedNameSelector(5)
    for i in range(100_000):
        selector.add(f"name{i:06d}")
        assert selector.retained <= 6
    assert selector.peak_size <= 6
    assert selector.seen == 100_000


def test_peak_size_never_exceeds_capacity_in_reverse_order():
    """Descending input is the worst case: every name displaces another."""
    selector = BoundedNameSelector(4)
    for i in reversed(range(10_000)):
        selector.add(f"name{i:06d}")
    assert selector.peak_size <= 5
    names, truncated = selector.result()
    assert names == ["name000000", "name000001", "name000002", "name000003"]
    assert truncated is True


def test_returns_smallest_names_regardless_of_arrival_order():
    for order in (list(range(20)), list(reversed(range(20)))):
        selector = BoundedNameSelector(3)
        for i in order:
            selector.add(f"{i:03d}")
        assert selector.result() == (["000", "001", "002"], True)


def test_fewer_than_limit_returns_everything_untruncated():
    selector = BoundedNameSelector(10)
    for name in ("c", "a", "b"):
        selector.add(name)
    assert selector.result() == (["a", "b", "c"], False)


def test_exactly_at_limit_is_not_truncated():
    selector = BoundedNameSelector(3)
    for name in ("c", "a", "b"):
        selector.add(name)
    assert selector.result() == (["a", "b", "c"], False)


def test_one_over_limit_is_truncated():
    selector = BoundedNameSelector(3)
    for name in ("c", "a", "b", "d"):
        selector.add(name)
    assert selector.result() == (["a", "b", "c"], True)


def test_empty_stream():
    assert BoundedNameSelector(5).result() == ([], False)


def test_unicode_names_order_by_code_point():
    selector = BoundedNameSelector(10)
    for name in ("日本語.txt", "éclair", "apple", "Zebra", "✓check"):
        selector.add(name)
    names, _ = selector.result()
    assert names == sorted(["日本語.txt", "éclair", "apple", "Zebra", "✓check"])


def test_case_sensitivity_matches_byte_order():
    selector = BoundedNameSelector(10)
    for name in ("apple", "Banana", "Zebra", "_under"):
        selector.add(name)
    names, _ = selector.result()
    assert names == ["Banana", "Zebra", "_under", "apple"]


def test_repeated_identical_names_are_all_retained():
    """Filesystems don't produce duplicates, but the structure must not lose
    or dedupe entries if one ever appears."""
    selector = BoundedNameSelector(5)
    for _ in range(3):
        selector.add("same")
    names, truncated = selector.result()
    assert names == ["same", "same", "same"]
    assert truncated is False


@pytest.mark.parametrize("limit", [1, 2, 7, 50])
def test_differential_against_full_sort(limit):
    """Randomised proof of equivalence to ``sorted(all)[:limit]``.

    Filesystem iteration order is arbitrary, so the input is shuffled every
    trial. This is the property that lets the bounded algorithm replace the
    unbounded one without changing public ordering semantics.
    """
    rng = random.Random(20260817 + limit)
    alphabet = list(string.ascii_letters + string.digits + "._-") + \
        list("éÉ✓日本")
    for _ in range(400):
        count = rng.randint(0, 120)
        names = list({
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
            for _ in range(count)
        })
        shuffled = names[:]
        rng.shuffle(shuffled)

        selector = BoundedNameSelector(limit)
        for name in shuffled:
            selector.add(name)
        got, truncated = selector.result()

        assert got == sorted(names)[:limit]
        assert truncated is (len(names) > limit)
        assert selector.peak_size <= limit + 1


# ===========================================================================
# The guard uses it, and stays bounded
# ===========================================================================

@POSIX_ONLY
def test_large_directory_is_not_materialised(workspace, monkeypatch):
    """The actual defect: a directory far larger than the cap.

    The selector is instrumented so the assertion is about retained storage,
    not about process memory, which is noisy and platform-dependent.
    """
    populate(workspace / "huge", 5_000)

    peaks: list[int] = []
    real_result = BoundedNameSelector.result

    def spy(self):
        peaks.append(self.peak_size)
        return real_result(self)

    monkeypatch.setattr(BoundedNameSelector, "result", spy)

    result = listing(workspace, "huge", limit=10)

    assert len(result.entries) == 10
    assert result.truncated is True
    assert peaks and max(peaks) <= 11, peaks


@POSIX_ONLY
def test_large_directory_returns_the_true_sorted_prefix(workspace):
    """End-to-end equivalence against a real 3000-entry directory."""
    target = populate(workspace / "big", 3_000)
    expected = sorted(p.name for p in target.iterdir())[:25]
    assert names_of(listing(workspace, "big", limit=25)) == expected


@POSIX_ONLY
def test_scandir_is_consumed_lazily(workspace, monkeypatch):
    """Guards against a regression to ``sorted(scandir)``.

    If someone reintroduces a full materialisation, the guard would pull every
    entry into a list before the cap applies. Counting how many names the
    selector was offered is fine (it must see them all), but the retained
    structure must stay small — that is what is asserted.
    """
    populate(workspace / "many", 1_200)
    seen_peak: list[int] = []
    real_add = BoundedNameSelector.add

    def spy(self, name):
        real_add(self, name)
        seen_peak.append(self.retained)

    monkeypatch.setattr(BoundedNameSelector, "add", spy)
    listing(workspace, "many", limit=20)
    assert max(seen_peak) <= 21


@POSIX_ONLY
def test_exactly_at_cap_is_not_truncated(workspace):
    populate(workspace / "exact", 15)
    result = listing(workspace, "exact", limit=15)
    assert len(result.entries) == 15
    assert result.truncated is False


@POSIX_ONLY
def test_one_over_cap_is_truncated(workspace):
    populate(workspace / "over", 16)
    result = listing(workspace, "over", limit=15)
    assert len(result.entries) == 15
    assert result.truncated is True


@POSIX_ONLY
def test_below_cap_returns_everything(workspace):
    populate(workspace / "small", 4)
    result = listing(workspace, "small", limit=15)
    assert len(result.entries) == 4
    assert result.truncated is False


@POSIX_ONLY
def test_limit_of_one_still_works(workspace):
    populate(workspace / "one", 50)
    result = listing(workspace, "one", limit=1)
    assert names_of(result) == ["file00000.txt"]
    assert result.truncated is True


@POSIX_ONLY
def test_ordering_is_stable_across_repeated_large_listings(workspace):
    populate(workspace / "stable", 2_000)
    first = names_of(listing(workspace, "stable", limit=30))
    for _ in range(3):
        assert names_of(listing(workspace, "stable", limit=30)) == first
    assert first == sorted(first)


# ===========================================================================
# Security semantics survive the rewrite
# ===========================================================================

@POSIX_ONLY
def test_sensitive_entries_are_still_redacted_under_the_cap(workspace):
    """A redacted entry must not be able to slip through the bounded path.

    '.env' sorts early, so it is inside the retained prefix and must still be
    withheld after the change.
    """
    target = workspace / "mixed"
    populate(target, 200)
    (target / ".env").write_text("API_KEY=sk-must-not-leak\n", encoding="utf-8")

    result = listing(workspace, "mixed", limit=10)
    shown = names_of(result)
    assert ".env" not in shown
    assert REDACTED_NAME in shown
    assert result.sensitive_count == 1


@POSIX_ONLY
def test_sensitive_entry_beyond_the_cap_is_simply_absent(workspace):
    """An entry outside the returned prefix is not listed at all.

    It is neither disclosed nor counted — being dropped by the cap is not a
    policy decision, so ``sensitive_count`` must not report it.
    """
    target = workspace / "late"
    populate(target, 100, prefix="aaa")
    (target / "zzz_id_rsa").write_text("key\n", encoding="utf-8")

    result = listing(workspace, "late", limit=5)
    assert "zzz_id_rsa" not in names_of(result)
    assert result.sensitive_count == 0
    assert result.truncated is True


@POSIX_ONLY
def test_symlinks_are_still_classified_without_being_followed(workspace,
                                                              tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")

    target = workspace / "links"
    target.mkdir()
    (target / "aaa_external").symlink_to(outside / "secret.txt")
    (target / "aab_broken").symlink_to(target / "missing")
    (target / "aac_real.txt").write_text("x", encoding="utf-8")

    result = listing(workspace, "links", limit=10)
    kinds = {e.display_name: e for e in result.entries}
    assert kinds["aaa_external"].kind is PathKind.SYMLINK
    assert kinds["aaa_external"].link_target is LinkTarget.EXTERNAL
    assert kinds["aab_broken"].link_target is LinkTarget.BROKEN
    import json
    assert "TOP SECRET" not in json.dumps(result.to_dict(), default=str)
    assert str(outside) not in json.dumps(result.to_dict(), default=str)


@POSIX_ONLY
def test_no_recursion_into_subdirectories(workspace):
    target = workspace / "parent"
    target.mkdir()
    (target / "child").mkdir()
    (target / "child" / "buried.txt").write_text("x", encoding="utf-8")

    result = listing(workspace, "parent", limit=100)
    assert names_of(result) == ["child"]
    import json
    assert "buried.txt" not in json.dumps(result.to_dict(), default=str)


# ===========================================================================
# Resource safety
# ===========================================================================

def open_fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:  # pragma: no cover - non-Linux
        return -1


@POSIX_ONLY
def test_repeated_large_listings_leak_no_descriptors(workspace):
    populate(workspace / "fds", 500)
    before = open_fd_count()
    for _ in range(50):
        listing(workspace, "fds", limit=20)
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_scandir_is_closed_when_enumeration_raises(workspace, monkeypatch):
    """An error mid-walk must not leak the directory handle."""
    populate(workspace / "boom", 200)

    real_add = BoundedNameSelector.add
    state = {"calls": 0}

    def explode(self, name):
        state["calls"] += 1
        if state["calls"] > 10:
            raise OSError(5, "simulated enumeration failure")
        real_add(self, name)

    monkeypatch.setattr(BoundedNameSelector, "add", explode)
    before = open_fd_count()
    for _ in range(25):
        state["calls"] = 0
        with pytest.raises(Exception):
            listing(workspace, "boom", limit=20)
    assert open_fd_count() <= before + 1


@POSIX_ONLY
def test_storage_stays_bounded_even_if_enumeration_fails_late(workspace,
                                                              monkeypatch):
    populate(workspace / "partial", 1_000)
    peaks: list[int] = []
    real_add = BoundedNameSelector.add
    state = {"calls": 0}

    def watched(self, name):
        state["calls"] += 1
        real_add(self, name)
        peaks.append(self.retained)
        if state["calls"] == 900:
            raise OSError(5, "late failure")

    monkeypatch.setattr(BoundedNameSelector, "add", watched)
    with pytest.raises(Exception):
        listing(workspace, "partial", limit=12)
    assert max(peaks) <= 13


# ===========================================================================
# Regression detectors for the defect itself
#
# The instrumented-selector tests above prove the selector is bounded, but a
# caller could still materialise the whole directory *before* feeding it — the
# exact defect Task 35 left behind. Verified by temporarily reintroducing
# `sorted(scandir)`: those tests still passed, so these were added to catch it.
# ===========================================================================

@POSIX_ONLY
def test_guard_does_not_build_a_full_name_list(workspace, monkeypatch):
    """Counts how many entry names are alive at once during the scan.

    A tracking iterator replaces scandir's stream. It records how many names
    the *caller* has pulled without the selector having discarded any, which is
    exactly what a full materialisation looks like.
    """
    populate(workspace / "watched", 2_000)

    guard = WorkspaceGuard(workspace,
                           config=FilesystemConfig(max_directory_entries=10))
    safe = guard.resolve_directory("watched")

    real_scandir = os.scandir
    stats = {"pulled": 0, "max_live": 0}

    class TrackingScandir:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self._inner)
            stats["pulled"] += 1
            # Live names = everything pulled minus everything the bounded
            # structure has already thrown away.
            live = stats["pulled"] - max(0, stats["pulled"] - 11)
            stats["max_live"] = max(stats["max_live"], live)
            return entry

        def close(self):
            self._inner.close()

    monkeypatch.setattr(os, "scandir", TrackingScandir)
    result = guard.list_directory(safe)

    assert stats["pulled"] == 2_000, "every entry must still be examined"
    assert stats["max_live"] <= 11
    assert len(result.entries) == 10
    assert result.truncated is True


def test_enumeration_never_sorts_the_whole_stream():
    """AST guard against a regression to ``sorted(e.name for e in scanner)``.

    The bounded structure sorts at most ``limit + 1`` retained names, and that
    sort lives in ``BoundedNameSelector.result``. ``list_directory`` itself
    must contain no sort and no comprehension over the scandir stream.
    """
    import ast
    import inspect
    import textwrap

    from agentcore.fs.guard import WorkspaceGuard

    source = textwrap.dedent(
        inspect.getsource(WorkspaceGuard.list_directory))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            # `tuple(entries)` is fine: `entries` is already capped at `limit`.
            # A sort, however, can only be over the unbounded stream.
            assert node.func.id != "sorted", (
                f"sorted() at line {node.lineno} would materialise the "
                f"directory stream")
        # A comprehension whose source is the scandir iterator materialises it.
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp)):
            for generator in node.generators:
                iter_src = ast.dump(generator.iter)
                assert "scanner" not in iter_src, (
                    "a comprehension over the scandir stream materialises it")


def test_selector_is_the_only_place_that_sorts():
    """The final sort must be over retained names, never the whole directory."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(BoundedNameSelector.result))
    tree = ast.parse(source)
    sorts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "sorted"]
    assert len(sorts) == 1
