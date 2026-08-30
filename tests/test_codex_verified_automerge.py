from __future__ import annotations

import importlib.util
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
        body=(
            f"{POLICY.CLEAN_MARKER}\n"
            f"Reviewed commit: {old_head}"
        ),
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
        body=(
            f"{POLICY.CLEAN_MARKER}\n"
            f"Reviewed commit: {head}"
        ),
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
        body=(
            f"{POLICY.CLEAN_MARKER}\n"
            f"Reviewed commit: {head}"
        ),
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
