# PR #50 — Task 50A opt-in strict TaskRunner verification

AREA: runtime
TASK: Prerequisite strict verification/dependency gating for Task 50 supervised workflows
STATUS: DRAFT / OPEN / UNMERGED — TESTS AND REVIEW PENDING

BASE SHA: `042e6bc5bcacd2872222d5029a5df607fd733255`
HEAD SHA: `89591c1ed82af9c818e9cc6df11d16388f525ba2`
PARENT SHA: `216a640fe3da84289f503dd03d5ae421bb3b02c5`
BRANCH: `codex/task-50a-strict-verification`

[PR #50](https://github.com/Nortan251/agentcore/pull/50) · [head](https://github.com/Nortan251/agentcore/commit/9d68812dfe76616bdb18f2ae9435b9b4a55e94cd) · [architecture note](https://github.com/Nortan251/agentcore/blob/9d68812dfe76616bdb18f2ae9435b9b4a55e94cd/docs/architecture/task-50a-strict-verification.md)

## Goal
A strict, explicitly opt-in verification gate before releasing TaskRunner dependencies in future supervised workflows. Existing Agent defaults remain compatible; Task 50B's host/planning/approval features are out of scope.

## Production changes
`TaskRunner(strict_verification=True)` prevents an executed step whose verification is SKIPPED, UNVERIFIED or FAILED from being considered dependency-eligible. It blocks further steps and avoids calling recovery or replaying the tool for this condition. Verifier exceptions also prevent dependency release while retaining the actual invocation/result; the error propagates. `False` retains legacy behavior.

## Files changed
- `src/agentcore/core/runner.py`: opt-in gate and blocked transitions.
- `tests/test_task_50a_strict_verification.py`: positive, negative, retry, resume and default-compatibility cases.
- `docs/architecture/task-50a-strict-verification.md`: scope, semantics and limitations.

## Architecture and security decisions
Uses existing legal TaskStatus transitions `COMPLETED -> PENDING -> BLOCKED` after a non-PASSED verdict because verifier evidence construction requires completion context. Physical execution remains recorded independently. Does not add another authority chain, approvals, file effects, model provenance or workflow host. Strict verdict requires a trusted evidence-aware verifier; Task 50B must implement actual postcondition verification.

## Bugs found
An existing default runner can treat a step as completed even if its verifier returns SKIPPED or an adverse verdict; this is intentional legacy behavior but insufficient for supervised dependency release.

## Bugs fixed
Candidate opt-in gating only. Awaiting exact-head test validation and independent review; **no verified-fix claim yet**.

## Known limitations
No plan acceptance, native file verification, durable workflow continuity, pre-dispatch action binding, restart reconciliation or new human approval. This is not a complete Task 50 workflow; POSIX file writes remain refused.

## Test results
Pending. No local tests or compileall were executed in the editing environment; exact-head CI was queued/running upon opening PR. Do not present source inspection as executed test evidence.

## CI
- Linux full: [workflow](https://github.com/Nortan251/agentcore/actions/runs/40010534011) — running at first observation.
- Windows 3.11: [workflow](https://github.com/Nortan251/agentcore/actions/runs/40010533996) — queued at first observation.
- Windows 3.12: same Windows workflow — queued.
- Windows 3.13: same Windows workflow — queued.
- Chromium Linux: [workflow](https://github.com/Nortan251/agentcore/actions/runs/40010533969) — queued at first observation.
- Chromium Windows: same Chromium workflow — queued.

## Codex review
No completed independent exact-head review claimed. Draft PR is not ready for merge.

## Unresolved review threads
No formal clearance has been performed. Check current GitHub thread inventory before assessment.

## Blockers
Required local/full verification, six current-head CI jobs, independent exact-head review and explicit owner merge authorization.

## Next required action
Validate or repair the narrow gate with failing-before/passing-after regressions; obtain all required exact-head checks and review. Keep draft and unmerged until separately authorized. Do not start Task 50B based solely on this candidate.

## Final verdict
**DRAFT / NOT CLEARED FOR MERGE.**

## 2026-10-23 amendment — fail-closed reentry and cancellation hardening

The original candidate `9d68812dfe76616bdb18f2ae9435b9b4a55e94cd` was refined through `216a640fe3da84289f503dd03d5ae421bb3b02c5` to final currently proposed head `89591c1ed82af9c818e9cc6df11d16388f525ba2`. Source and focused tests were amended to refuse independently ready subtasks on reentry while any strict verification remains blocked, clear active subtask attribution on verifier exceptions, and preserve cancellation precedence over that fail-closed restart check. The branch retains exactly three cumulative changed files. No Task 50B implementation or new tool authority exists.

All exact-head CI jobs and independent review remain pending at this amendment, and no local full-suite/compileall pass is claimed. Final merge status remains **DRAFT / OPEN / UNMERGED**. No work on `main`.
