"""Verified auto-merge policy for same-repository Codex pull requests.

This file is executed only from the trusted ``main`` branch by the
``codex-verified-automerge`` workflow. Eligible PRs may not modify this file,
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
GOVERNANCE_PATHS = frozenset({"AGENTS.md"})
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


def flatten_pages(pages: Sequence[Any]) -> list[Any]:
    """Flatten list-valued GitHub pagination pages and reject odd payloads."""
    flattened: list[Any] = []
    for page in pages:
        if not isinstance(page, list):
            raise TypeError("paginated GitHub response page is not a list")
        flattened.extend(page)
    return flattened


def api_paginated(path: str) -> list[Any]:
    raw = subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", path],
        text=True,
    )
    pages = json.loads(raw)
    if not isinstance(pages, list):
        raise TypeError("paginated GitHub response is not a list")
    return flatten_pages(pages)


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=check,
        text=True,
        capture_output=True,
    )


def git_output(*args: str) -> str:
    return run_git(*args).stdout.strip()


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
            tested_head = run_pr.get("head", {}).get("sha") if run_pr else None
            tested_base = run_pr.get("base", {}).get("sha") if run_pr else None
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
    return bool(
        body
        and CLEAN_MARKER in body
        and head_sha.lower() in body.lower()
    )


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
    reviews = api_paginated(
        f"repos/{REPO}/pulls/{pr_number}/reviews?per_page=100"
    )
    comments = api_paginated(
        f"repos/{REPO}/pulls/{pr_number}/comments?per_page=100"
    )
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


def file_evidence_paths(files: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Include both sides of renames so governance files cannot be renamed away."""
    paths: list[str] = []
    for item in files:
        filename = item.get("filename")
        previous = item.get("previous_filename")
        if filename:
            paths.append(str(filename))
        if previous:
            paths.append(str(previous))
    return tuple(paths)


def pr_changes_are_auto_merge_safe(pr: Mapping[str, Any]) -> tuple[bool, str]:
    changed_count = int(pr.get("changed_files") or 0)
    if changed_count > MAX_AUTO_MERGE_FILES:
        return False, (
            f"PR changes {changed_count} files; automatic merge limit is "
            f"{MAX_AUTO_MERGE_FILES}"
        )
    files = api_paginated(f"repos/{REPO}/pulls/{pr['number']}/files?per_page=100")
    return changed_paths_are_auto_merge_safe(file_evidence_paths(files))


def eligible_prs() -> list[dict[str, Any]]:
    pulls = api(f"repos/{REPO}/pulls?state=open&base=main&per_page=100")
    return [
        dict(pr)
        for pr in pulls
        if not pr.get("draft")
        and pr.get("head", {}).get("repo", {}).get("full_name") == REPO
        and str(pr.get("head", {}).get("ref", "")).startswith("codex/")
    ]


def push_merge_with_expected_main(merge_sha: str, main_sha: str) -> bool:
    """Atomically update main only if the remote ref is still exactly main_sha."""
    lease = f"--force-with-lease=refs/heads/main:{main_sha}"
    result = run_git(
        "push",
        lease,
        "origin",
        f"{merge_sha}:refs/heads/main",
        check=False,
    )
    return result.returncode == 0


def create_and_push_merge_commit(
    *,
    pr_number: int,
    head_ref: str,
    head_sha: str,
    main_sha: str,
) -> str | None:
    """Create (main_sha, head_sha) merge and CAS-push it to main."""
    run_git("config", "user.name", "github-actions[bot]")
    run_git(
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )

    run_git("fetch", "--no-tags", "origin", "refs/heads/main")
    fetched_main = git_output("rev-parse", "FETCH_HEAD")
    if fetched_main != main_sha:
        print(f"  skip: remote main moved to {fetched_main} before merge creation")
        return None

    run_git("fetch", "--no-tags", "origin", f"refs/heads/{head_ref}")
    fetched_head = git_output("rev-parse", "FETCH_HEAD")
    if fetched_head != head_sha:
        print(f"  skip: remote PR head moved to {fetched_head}")
        return None

    run_git("switch", "--detach", main_sha)
    merge_message = (
        f"Merge pull request #{pr_number} from {head_ref}\n\n"
        "Verified Codex auto-merge"
    )
    merged = run_git(
        "merge",
        "--no-ff",
        "--no-edit",
        "-m",
        merge_message,
        head_sha,
        check=False,
    )
    if merged.returncode != 0:
        print("  skip: local merge did not complete cleanly")
        run_git("merge", "--abort", check=False)
        return None

    merge_sha = git_output("rev-parse", "HEAD")
    first_parent = git_output("rev-parse", "HEAD^1")
    second_parent = git_output("rev-parse", "HEAD^2")
    if first_parent != main_sha or second_parent != head_sha:
        raise RuntimeError(
            "constructed merge commit parents do not match verified refs"
        )

    if not push_merge_with_expected_main(merge_sha, main_sha):
        print("  skip: atomic main lease failed; main changed before push")
        return None
    if current_main_sha() != merge_sha:
        raise RuntimeError("main did not resolve to the pushed merge commit")
    return merge_sha


def try_merge_pr(pr: Mapping[str, Any], main_sha: str) -> str:
    number = int(pr["number"])
    head_sha = str(pr["head"]["sha"])
    head_ref = str(pr["head"]["ref"])
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

    merge_sha = create_and_push_merge_commit(
        pr_number=number,
        head_ref=head_ref,
        head_sha=head_sha,
        main_sha=main_sha,
    )
    if merge_sha is None:
        return current_main_sha()
    print(f"  merged PR #{number}: {merge_sha}")
    return merge_sha


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
