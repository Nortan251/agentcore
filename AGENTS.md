# AgentCore Codex Working Rules

These rules apply to repository work performed or reviewed by Codex.

## Branch and merge workflow

- Never push implementation work directly to `main`.
- Work on a task branch and open a pull request into `main`.
- Prefer branch names under `codex/` for work intended to use the repository's verified auto-merge path.
- Do not bypass, disable, weaken, or mark required CI as non-gating to obtain a merge.
- Ordinary Codex/task work must never force-push, squash, rebase, or amend an already-audited repair unless the task explicitly requires history rewriting.
- The trusted `.github/automation` auto-merge gate has one narrow exception: it may use `--force-with-lease=refs/heads/main:<verified-main-sha>` only as an atomic compare-and-swap that refuses a stale `main`. Before that push it must construct and verify a merge commit whose first parent is the exact CI-verified `main` SHA and whose second parent is the exact reviewed PR head SHA. This exception is not permission to rewrite `main` or bypass review/CI.
- Do not merge a PR yourself. The repository auto-merge gate is responsible for merging eligible `codex/**` PRs after the exact current head has passed review and CI.
- Changes to `AGENTS.md`, `.github/workflows/**`, or `.github/automation/**` are governance changes and intentionally require a manually reviewed merge; they are never eligible for the automatic merge path.

## Required verification before handoff

Run the smallest focused tests that prove the change, then the relevant surrounding suites. For substantial runtime changes, also run the full test suite when practical.

At minimum before declaring a branch ready:

- `python -m pytest -q` for changes that can affect runtime behavior.
- `python -m compileall -q src tests`.
- `git diff --check`.
- Report the exact final commit SHA and its parent.
- Report changed files and the exact test results.

GitHub remains the merge authority. A local test pass does not replace the repository's Linux and Windows CI runs on the PR.

## Code review priorities

Treat the following as merge-blocking P0/P1 concerns when reviewing AgentCore:

1. Any path that bypasses the canonical tool execution authority chain:
   `ToolRegistry -> validation -> PermissionPolicy -> ToolExecutor -> Tool.run`.
2. Any mutation that can reach a physical effect without current permission/approval and exact trusted registration checks.
3. Any loss of uncertainty around external effects, retries, timeouts, cancellation, recovery, or reconciliation.
4. Concurrency, TOCTOU, lock-order, process/fork, ownership, lease, or cancellation races that can execute stale or duplicate work.
5. Credentials, secrets, protected payloads, or sensitive evidence entering prompts, generic model-visible state, logs, or the ActionJournal.
6. Historical approval, journal state, cached provider state, or recovery metadata being treated as current authority.
7. Schema or lifecycle-state changes that silently violate the frozen ActionJournal model or compatibility assumptions.
8. Provider-idempotency logic that infers `not applied` from a generic exception instead of trusted provider evidence.
9. Tests that pass by weakening assertions instead of fixing the underlying invariant.
10. CI/platform-specific behavior that is skipped or hidden rather than handled safely.
11. Any PR-controlled change that can weaken the CI or review policy used as merge authority for that same PR.

When a P0/P1 is found, identify the exact invariant violated and require a focused regression test that fails before the repair and passes after it.

## Review behavior

- Review the exact current PR head, not an earlier commit.
- Re-review after every repair commit that changes behavior relevant to a finding.
- Never treat a reaction timestamp as proof that a particular SHA was reviewed.
- If the exact current head has any P0/P1 finding, do **not** emit the clean marker below.
- If the exact current head is clean with no P0/P1 findings, submit a GitHub review whose body contains these two lines, using the full 40-character commit SHA:

  `AGENTCORE_AUTOMERGE: CLEAN`

  `Reviewed commit: <full-current-head-sha>`

  This structured verdict is required because a generic 👍 reaction cannot be bound safely to a specific commit.
- Do not approve or recommend merging when required Linux or Windows CI is failing, missing, stale, still running, or was tested against an older `main`.
