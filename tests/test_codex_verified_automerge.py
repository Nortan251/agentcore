from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "automation"
    / "codex_verified_automerge.py"
)
SPEC = importlib.util.spec_from_file_location("codex_verified_automerge", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


def _run(*, pr_number=7, head="head", base="main", status="completed", conclusion="success"):
    return {
        "status": status,
        "conclusion": conclusion,
        "pull_requests": [
            {
                "number": pr_number,
                "head": {"sha": head},
                "base": {"sha": base},
            }
        ],
    }


def _review(*, head: str, body: str, login="chatgpt-codex-connector[bot]"):
    return {
        "user": {"login": login},
        "commit_id": head,
        "body": body,
    }


def _comment(*, head: str, login="chatgpt-codex-connector[bot]"):
    return {
        "user": {"login": login},
        "commit_id": head,
        "body": "finding",
    }


def test_ci_requires_exact_current_head_and_base_pair():
    assert POLICY.run_has_exact_provenance(
        _run(head="h1", base="m1"),
        pr_number=7,
        head_sha="h1",
        main_sha="m1",
    )

    assert not POLICY.run_has_exact_provenance(
        _run(head="h1", base="old-main"),
        pr_number=7,
        head_sha="h1",
        main_sha="new-main",
    )
    assert not POLICY.run_has_exact_provenance(
        _run(head="old-head", base="m1"),
        pr_number=7,
        head_sha="new-head",
        main_sha="m1",
    )


def test_ci_rejects_failed_or_incomplete_runs_even_with_matching_provenance():
    assert not POLICY.run_has_exact_provenance(
        _run(status="in_progress", conclusion=None),
        pr_number=7,
        head_sha="head",
        main_sha="main",
    )
    assert not POLICY.run_has_exact_provenance(
        _run(conclusion="failure"),
        pr_number=7,
        head_sha="head",
        main_sha="main",
    )


def test_clean_codex_review_is_bound_to_exact_head_sha():
    new_head = "2" * 40
    old_head = "1" * 40
    old_clean = _review(
        head=old_head,
        body=f"{POLICY.CLEAN_MARKER}\nReviewed commit: {old_head}",
    )

    ok, reason = POLICY.codex_review_state_from_payloads(
        reviews=[old_clean],
        comments=[],
        head_sha=new_head,
    )
    assert not ok
    assert reason == "waiting for exact-head Codex review"


def test_exact_head_clean_codex_review_passes():
    head = "a" * 40
    clean = _review(
        head=head,
        body=f"{POLICY.CLEAN_MARKER}\nReviewed commit: {head}",
    )

    ok, reason = POLICY.codex_review_state_from_payloads(
        reviews=[clean],
        comments=[],
        head_sha=head,
    )
    assert ok
    assert "current head" in reason


def test_exact_head_codex_findings_block_even_if_clean_marker_also_exists():
    head = "b" * 40
    clean = _review(
        head=head,
        body=f"{POLICY.CLEAN_MARKER}\nReviewed commit: {head}",
    )

    ok, reason = POLICY.codex_review_state_from_payloads(
        reviews=[clean],
        comments=[_comment(head=head)],
        head_sha=head,
    )
    assert not ok
    assert reason == "Codex posted inline findings for current head"


def test_unstructured_codex_review_is_not_clean_authority():
    head = "c" * 40
    review = _review(
        head=head,
        body=f"Reviewed commit: {head}\nNo blocking findings mentioned.",
    )

    ok, reason = POLICY.codex_review_state_from_payloads(
        reviews=[review],
        comments=[],
        head_sha=head,
    )
    assert not ok
    assert reason == "Codex posted review findings for current head"


def test_paginated_later_codex_finding_is_not_omitted():
    head = "d" * 40
    clean = _review(
        head=head,
        body=f"{POLICY.CLEAN_MARKER}\nReviewed commit: {head}",
    )
    review_pages = [[clean], []]
    comment_pages = [[], [_comment(head=head)]]

    reviews = POLICY.flatten_pages(review_pages)
    comments = POLICY.flatten_pages(comment_pages)
    ok, reason = POLICY.codex_review_state_from_payloads(
        reviews=reviews,
        comments=comments,
        head_sha=head,
    )
    assert not ok
    assert reason == "Codex posted inline findings for current head"


def test_governance_paths_cannot_use_automatic_merge():
    blocked_cases = [
        ["src/agentcore/runtime.py", ".github/workflows/filesystem-linux.yml"],
        [".github/workflows/filesystem-windows.yml"],
        [".github/workflows/codex-verified-automerge.yml"],
        [".github/automation/codex_verified_automerge.py"],
        ["AGENTS.md"],
    ]
    for paths in blocked_cases:
        ok, reason = POLICY.changed_paths_are_auto_merge_safe(paths)
        assert not ok, paths
        assert "manual merge" in reason


def test_renaming_governance_file_out_of_protected_path_is_blocked():
    files = [
        {
            "status": "renamed",
            "previous_filename": ".github/workflows/filesystem-linux.yml",
            "filename": "docs/not-a-workflow.yml",
        }
    ]
    evidence = POLICY.file_evidence_paths(files)
    assert ".github/workflows/filesystem-linux.yml" in evidence
    assert "docs/not-a-workflow.yml" in evidence

    ok, reason = POLICY.changed_paths_are_auto_merge_safe(evidence)
    assert not ok
    assert "manual merge" in reason


def test_normal_runtime_and_test_changes_remain_auto_merge_eligible():
    ok, reason = POLICY.changed_paths_are_auto_merge_safe(
        [
            "src/agentcore/tools/executor.py",
            "tests/test_tools.py",
            "docs/architecture/example.md",
        ]
    )
    assert ok
    assert "does not modify" in reason


def test_atomic_push_rejects_if_main_moves_after_verification(monkeypatch):
    calls = []

    def fake_run_git(*args, check=True):
        calls.append((args, check))
        # Models the remote rejecting the push because main changed after the
        # verified base was observed but before the final update.
        return subprocess.CompletedProcess(["git", *args], 1, "", "stale lease")

    monkeypatch.setattr(POLICY, "run_git", fake_run_git)
    expected_main = "1" * 40
    merge_sha = "2" * 40

    assert not POLICY.push_merge_with_expected_main(merge_sha, expected_main)
    assert len(calls) == 1
    args, check = calls[0]
    assert args[0] == "push"
    assert f"--force-with-lease=refs/heads/main:{expected_main}" in args
    assert f"{merge_sha}:refs/heads/main" in args
    assert check is False


def test_atomic_push_uses_exact_main_lease_when_remote_is_unchanged(monkeypatch):
    calls = []

    def fake_run_git(*args, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(POLICY, "run_git", fake_run_git)
    expected_main = "3" * 40
    merge_sha = "4" * 40

    assert POLICY.push_merge_with_expected_main(merge_sha, expected_main)
    args, _ = calls[0]
    assert f"--force-with-lease=refs/heads/main:{expected_main}" in args
