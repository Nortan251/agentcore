"""Verified auto-merge policy for same-repository Codex pull requests.

This file is executed only from the trusted ``main`` branch by the
``codex-verified-automerge`` workflow.  Eligible PRs may not modify this file,
the merge workflow, the required CI workflows, or the root Codex policy.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Iterable, Mapping, Sequence

REPO = os.environ.get("GH_REPO", "")
CODEX_BOT_LOGINS = frozenset({
    "chatgpt-codex-connector",
    "chatgpt-codex-connector[bot]",
})
CLEAN_MARKER = "AGENTCORE_AUTOMERGE: CLEAN"
REQUIRED_WORKFLOWS = frozenset({
    "filesystem-linux",
    "filesystem-windows",
})
GOVERNANCE_PATHS = frozenset({
    "AGENTS.md",
})
GOVERNANCE_PREFIXES = (
    ".github/workflows/",
    ".github/automation/",
)
MAX_AUTO_MERGE_FILES = 100


def api(
    path: str,
    *,
    method: str = "GET",
    fields: Mapping[str, str] | None = None,
) -> Any:
    cmd = ["gh", "api"]
    if method != "GET":
        cmd += ["--method", method]
    cmd.append(path)
    if fields:
        for key, value in fields.items():
            cmd += ["-f", f"{key}={value}"]
    raw = subprocess.check_output(cmd, text=True)
    return json.loads(raw)


def current_main_sha() -> str:
    return api(f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]


def _matching_run_pr(
    run: Mapping[str, Any],
    pr_number: int,
) -> Mapping[str, Any] | None:
    return next(
        (
            item
            for item in run.get("pull_requests", [])
            if item.get("number") == pr_number
        ),
        None,
    )


def run_has_exact_provenance(
    run: Mapping[str, Any],
    *,
    pr_number: int,
    head_sha: str,
    main_sha: str,
) -> bool:
    """Return True only for a successful run of the exact head/base pair."""
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return False
    run_pr = _matching_run_pr(run, pr_number)
    if run_pr is None:
        return False
    return (
        run_pr.get("head", {}).get("sha") == head_sha
        and run_pr.get("base", {}).get("sha") == main_sha
    )


def latest_required_runs(
    pr_number: int,
    head_sha: str,
) -> dict[str, dict[str, Any]]:
    payload = api(
        f"repos/{REPO}/actions/runs"
        f"?event=pull_request&head_sha={head_sha}&per_page=100"
    )
    latest: dict[str, dict[str, Any]] = {}
    for run in payload.get("workflow_runs", []):
        name = run.get("name")
        if name not in REQUIRED_WORKFLOWS:
            continue
        if _matching_run_pr(run, pr_number) is None:
            continue
        previous = latest.get(name)
        order = (run.get("updated_at") or "", int(run.get("id", 0)))
        previous_order = (
            (previous.get("updated_at") or "", int(previous.get("id", 0)))
            if previous
            else ("", 0)
        )
        if order > previous_order:
            latest[name] = dict(run)
    return latest


def ci_is_green(
    pr_number: int,
    head_sha: str,
    main_sha: str,
) -> tuple[bool, str]:
    latest = latest_required_runs(pr_number, head_sha)
    missing = REQUIRED_WORKFLOWS.difference(latest)
    if missing:
        return False, f"missing CI: {sorted(missing)}"
    for name in sorted(REQUIRED_WORKFLOWS):
        run = latest[name]
        if not run_has_exact_provenance(
            run,
            pr_number=pr_number,
            head_sha=head_sha,
            main_sha=main_sha,
        ):
            run_pr = _matching_run_pr(run, pr_number)
            tested_head = (
                run_pr.get("head", {}).get("sha") if run_pr else None
            )
            tested_base = (
                run_pr.get("base", {}).get("sha") if run_pr else None
            )
            return False, (
                f"{name} is not a successful exact-head/current-main run "
                f"(head={tested_head}, base={tested_base}, "
                f"status={run.get('status')}, conclusion={run.get('conclusion')})"
            )
    return True, "required CI is green for current head and current main"


def _is_codex(user: Mapping[str, Any] | None) -> bool:
    return bool(user and user.get("login") in CODEX_BOT_LOGINS)


def review_body_is_exact_clean(body: str | None, head_sha: str) -> bool:
    """Bind clean-review authority to an explicit exact SHA in the review."""
    if not body or CLEAN_MARKER not in body:
        return False
    lowered = body.lower()
    return head_sha.lower() in lowered


def codex_review_state_from_payloads(
    *,
    reviews: Sequence[Mapping[str, Any]],
    comments: Sequence[Mapping[str, Any]],
    head_sha: str,
) -> tuple[bool, str]:
    """Require an exact-head structured CLEAN review and no exact-head findings."""
    exact_reviews = [
        review
        for review in reviews
        if _is_codex(review.get("user")) and review.get("commit_id") == head_sha
    ]
    exact_comments = [
        comment
        for comment in comments
        if _is_codex(comment.get("user")) and comment.get("commit_id") == head_sha
    ]

    if exact_comments:
        return False, "Codex posted inline findings for current head"

    if not exact_reviews:
        return False, "waiting for exact-head Codex review"

    clean = [
        review
        for review in exact_reviews
        if review_body_is_exact_clean(review.get("body"), head_sha)
    ]
    non_clean = [review for review in exact_reviews if review not in clean]
    if non_clean:
        return False, "Codex posted review findings for current head"
    if not clean:
        return False, "waiting for structured Codex CLEAN verdict"
    return True, "Codex CLEAN verdict is bound to current head"


def codex_review_is_clean(pr_number: int, head_sha: str) -> tuple[bool, str]:
    reviews = api(f"repos/{REPO}/pulls/{pr_number}/reviews?per_page=100")
    comments = api(f"repos/{REPO}/pulls/{pr_number}/comments?per_page=100")
    return codex_review_state_from_payloads(
        reviews=reviews,
        comments=comments,
        head_sha=head_sha,
    )


def path_is_governance(path: str) -> bool:
    return (
        path in GOVERNANCE_PATHS
        or any(path.startswith(prefix) for prefix in GOVERNANCE_PREFIXES)
    )


def changed_paths_are_auto_merge_safe(
    paths: Iterable[str],
) -> tuple[bool, str]:
    paths = tuple(paths)
    blocked = sorted(path for path in paths if path_is_governance(path))
    if blocked:
        return False, f"governance paths require manual merge: {blocked}"
    return True, "PR does not modify auto-merge governance"


def pr_changes_are_auto_merge_safe(pr: Mapping[str, Any]) -> tuple[bool, str]:
    changed_count = int(pr.get("changed_files") or 0)
    if changed_count > MAX_AUTO_MERGE_FILES:
        return False, (
            f"PR changes {changed_count} files; automatic merge limit is "
            f"{MAX_AUTO_MERGE_FILES}"
        )
    files = api(f"repos/{REPO}/pulls/{pr['number']}/files?per_page=100")
    return changed_paths_are_auto_merge_safe(
        str(item.get("filename", "")) for item in files
    )


def eligible_prs() -> list[dict[str, Any]]:
    pulls = api(f"repos/{REPO}/pulls?state=open&base=main&per_page=100")
    return [
        dict(pr)
        for pr in pulls
        if not pr.get("draft")
        and pr.get("head", {}).get("repo", {}).get("full_name") == REPO
        and str(pr.get("head", {}).get("ref", "")).startswith("codex/")
    ]


def try_merge_pr(pr: Mapping[str, Any], main_sha: str) -> str:
    number = int(pr["number"])
    head_sha = str(pr["head"]["sha"])
    base_sha = str(pr["base"]["sha"])
    print(f"PR #{number}: head={head_sha} base={base_sha}")

    if base_sha != main_sha:
        print(f"  skip: base is not current main {main_sha}")
        return main_sha

    changes_ok, changes_reason = pr_changes_are_auto_merge_safe(pr)
    print(f"  governance: {changes_reason}")
    if not changes_ok:
        return main_sha

    ci_ok, ci_reason = ci_is_green(number, head_sha, main_sha)
    print(f"  CI: {ci_reason}")
    if not ci_ok:
        return main_sha

    review_ok, review_reason = codex_review_is_clean(number, head_sha)
    print(f"  review: {review_reason}")
    if not review_ok:
        return main_sha

    # Re-read every mutable authority immediately before the merge. A new
    # head, a new main, or a new governance change invalidates this decision.
    fresh_pr = api(f"repos/{REPO}/pulls/{number}")
    fresh_main = current_main_sha()
    if fresh_pr.get("state") != "open" or fresh_pr.get("draft"):
        print("  skip: PR is no longer open/ready")
        return fresh_main
    if fresh_pr["head"]["sha"] != head_sha:
        print("  skip: PR head moved after verification")
        return fresh_main
    if fresh_pr["base"]["sha"] != fresh_main or fresh_main != main_sha:
        print("  skip: main moved after verification")
        return fresh_main

    fresh_changes_ok, fresh_changes_reason = pr_changes_are_auto_merge_safe(fresh_pr)
    if not fresh_changes_ok:
        print(f"  skip: {fresh_changes_reason}")
        return fresh_main

    if fresh_pr.get("mergeable") is not True:
        print(f"  skip: GitHub mergeable={fresh_pr.get('mergeable')}")
        return fresh_main

    result = api(
        f"repos/{REPO}/pulls/{number}/merge",
        method="PUT",
        fields={"sha": head_sha, "merge_method": "merge"},
    )
    if result.get("merged"):
        merged_sha = str(result["sha"])
        print(f"  merged PR #{number}: {merged_sha}")
        return merged_sha
    print(f"  merge declined by GitHub: {result.get('message')}")
    return fresh_main


def main() -> None:
    if not REPO:
        raise RuntimeError("GH_REPO is required")
    main_sha = current_main_sha()
    pulls = eligible_prs()
    if not pulls:
        print("No eligible open codex/** PRs.")
        return
    for pr in pulls:
        main_sha = try_merge_pr(pr, main_sha)


if __name__ == "__main__":
    main()
