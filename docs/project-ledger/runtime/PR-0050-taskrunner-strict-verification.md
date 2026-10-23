# PR #50 — Task 50A opt-in strict TaskRunner verification

AREA: runtime
TASK: Prerequisite strict verification/dependency gating for Task 50 supervised workflows
STATUS: DRAFT / OPEN / UNMERGED — TESTS AND REVIEW PENDING

BASE SHA: `042e6bc5bcacd2872222d5029a5df607fd733255`
HEAD SHA: `29f7ac82bba54d86f84023496dcd5ce505647803`
PARENT SHA: `89591c1ed82af9c818e9cc6df11d16388f525ba2`
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

## 2026-10-23 amendment — all six exact-head CI jobs successful

Authenticated GitHub read confirms PR #50 remains **DRAFT / OPEN / UNMERGED** at `89591c1ed82af9c818e9cc6df11d16388f525ba2`; main/base remains `042e6bc5bcacd2872222d5029a5df607fd733255`. Required CI workflow results are now complete and successful:
- Linux Python 3.13: [run 40011130495 / job 120611487055](https://github.com/Nortan251/agentcore/actions/runs/40011130495/job/120611487055) — SUCCESS.
- Windows Python 3.11: [run 40011130430 / job 120611487144](https://github.com/Nortan251/agentcore/actions/runs/40011130430/job/120611487144) — SUCCESS.
- Windows Python 3.12: [run 40011130430 / job 120611487074](https://github.com/Nortan251/agentcore/actions/runs/40011130430/job/120611487074) — SUCCESS.
- Windows Python 3.13: [run 40011130430 / job 120611486717](https://github.com/Nortan251/agentcore/actions/runs/40011130430/job/120611486717) — SUCCESS.
- Chromium Ubuntu: [run 40011130636 / job 120611489067](https://github.com/Nortan251/agentcore/actions/runs/40011130636/job/120611489067) — SUCCESS.
- Chromium Windows: [run 40011130636 / job 120611488446](https://github.com/Nortan251/agentcore/actions/runs/40011130636/job/120611488446) — SUCCESS.

No independent exact-head code review, local full-suite rerun or separate merge authorization is claimed. Current next action: arrange independent source/security review of the exact head and address findings before moving PR out of Draft. Task 50B remains unimplemented.

## 2026-10-23 amendment — independent P1/P2 review and scoped repair

Independent review on `89591c1ed82af9c818e9cc6df11d16388f525ba2` reported **NO-GO**, P1:1 and P2:1, with eight failing characterization probes and three positive controls. The P1 was strict admission of existing `COMPLETED` steps whose verification was not PASSED, allowing pending dependent or independent work in live and serialized tasks. P2 was a misleading `BLOCKED` run report on re-entry of an already `CANCELLED` parent. Previously green exact-head CI did not detect these cases.

Scoped candidate repair commit: [`29f7ac82bba54d86f84023496dcd5ce505647803`](https://github.com/Nortan251/agentcore/commit/29f7ac82bba54d86f84023496dcd5ce505647803), parent `89591c1ed82af9c818e9cc6df11d16388f525ba2`. Changed files remain the original three: runner, its dedicated regression tests, and strict-verification architecture note. Strict preflight now inspects existing completed verification status before dispatch; terminal tasks are explicitly rejected without changing their terminal state. No broad authority, recovery, executor or workflow host change.

GitHub PR comment documenting both findings and candidate repair: https://github.com/Nortan251/agentcore/pull/50#issuecomment-6375648333.

**CI on the new head and independent re-review pending.** Earlier CI/test results remain historical for the parent, not evidence of success on this new repair. Keep PR #50 DRAFT and unmerged; no Task 50B implementation authorized.

## 2026-10-23 UTC amendment — final independent GO, Ready review and authorized merge

This dated amendment supersedes the earlier pending/NO-GO snapshots above without rewriting them. Final status: **MERGED; Task 50A released**. Approved implementation head: [`29f7ac82bba54d86f84023496dcd5ce505647803`](https://github.com/Nortan251/agentcore/commit/29f7ac82bba54d86f84023496dcd5ce505647803), parent `89591c1ed82af9c818e9cc6df11d16388f525ba2`. No implementation source changed during release.

Independent full-diff re-review found original **P1 FIXED**, original **P2 FIXED**, and **no new actionable P0/P1/P2**. Strict admission blocks all three non-PASSED completed verdicts before dependent or independent dispatch in live/restored tasks, including repeated invocations. PASSED continuation preserves completed execution evidence. Strict terminal-task reentry rejects CANCELLED, FAILED and COMPLETED tasks without altering terminal truth or dispatching tools. Cancellation precedence and exception/evidence handling were checked. The [independent engineering assessment](https://github.com/Nortan251/agentcore/pull/50#issuecomment-6376438653) is a conversation comment, **not a native GitHub APPROVED review**.

Previously executed, exact-head local evidence was verified for this release rather than rerun:
- Original failure/control probes: **10 passed**; the original terminal-report probe was replaced by checks of the repaired StateError contract.
- Additional independent re-review checks: **27 passed**.
- Compatibility suites: **257 passed**; authority/recovery suites: **332 passed, 5 skipped**. Focused counts overlap.
- First full local invocation: **7,043 passed, 391 skipped, 62 failed, 89 warnings**. The 62 failures were existing WorkspaceAgent tests hitting a generated 267-character Windows temporary lock path, not represented as successful execution.
- Unchanged-source affected-suite reruns under a shorter temporary root: **86 passed, 16 skipped**. JUnit case identity comparison confirmed all 62 previously failed cases passed.
- Consolidated case coverage: **7,105 passed, 391 skipped across the original invocation and targeted reruns**, not one uninterrupted green full-suite execution.
- Compilation, base-to-head whitespace validation and all **387** archived-source file comparisons passed in the independent re-review.

All six final-head PR CI jobs were independently rechecked as **SUCCESS** before Ready and immediately before merge:
- Linux full: [run 40030983159 / job 120678874740](https://github.com/Nortan251/agentcore/actions/runs/40030983159/job/120678874740) — **6,979 passed, 517 skipped, 98 warnings**.
- Windows 3.11: [run 40030983161 / job 120678875172](https://github.com/Nortan251/agentcore/actions/runs/40030983161/job/120678875172) — SUCCESS.
- Windows 3.12: [run 40030983161 / job 120678875017](https://github.com/Nortan251/agentcore/actions/runs/40030983161/job/120678875017) — SUCCESS.
- Windows 3.13: [run 40030983161 / job 120678875281](https://github.com/Nortan251/agentcore/actions/runs/40030983161/job/120678875281) — SUCCESS.
- Chromium Linux: [run 40030983166 / job 120678875362](https://github.com/Nortan251/agentcore/actions/runs/40030983166/job/120678875362) — SUCCESS.
- Chromium Windows: [run 40030983166 / job 120678875117](https://github.com/Nortan251/agentcore/actions/runs/40030983166/job/120678875117) — SUCCESS.

PR #50 was marked Ready at **23:01:30 UTC** after explicit owner authorization for this exact head. The automatically triggered [Codex review summary](https://github.com/Nortan251/agentcore/pull/50#issuecomment-6376442860) reports **completed at 23:03:27 UTC**, exact commit `29f7ac82bba54d86f84023496dcd5ce505647803`, `has_any_findings=false` and `has_unresolved_findings=false`. No duplicate review was requested. Native GitHub review submissions: **0**; inline review threads: **0**, hence no unresolved threads. No separate automated security-review completion is claimed.

Final governance inspection reported unprotected main, **no active applied branch rules**, and an existing **disabled** repository ruleset. No governance setting changed and no protection bypass or forced merge was used. There was no native approval requirement to substitute for. Both remote head and main remained the owner-approved values immediately before merge; no actionable release-blocking finding remained.

GitHub confirmed a normal merge at **2026-10-23T23:04:39Z**: [`f09f670dd7a6abccdb2889c6e5685537db6dda63`](https://github.com/Nortan251/agentcore/commit/f09f670dd7a6abccdb2889c6e5685537db6dda63). The merge used the exact expected-head SHA guard. Verified ordered parents:
1. Unchanged main/base `042e6bc5bcacd2872222d5029a5df607fd733255`.
2. Approved candidate `29f7ac82bba54d86f84023496dcd5ce505647803`.

The actual merge tree is `628573a8a444995bd875a57edce02ddfd30b07e1`, identical to the candidate and the PR CI synthetic merge `9e66702ec01d2351021ab274db53a20361d34c2e`. Direct candidate-to-merge source diff is empty. PR state is closed/merged, not merely merge-requested.

Post-merge push CI on `f09f670dd7a6abccdb2889c6e5685537db6dda63`, observed at **23:07 UTC**: Linux completed successfully; the other five jobs remain **IN PROGRESS**, not passed:
- Linux full: [run 40037554337 / job 120700636153](https://github.com/Nortan251/agentcore/actions/runs/40037554337/job/120700636153) — SUCCESS.
- Windows 3.11: [run 40037554332 / job 120700636607](https://github.com/Nortan251/agentcore/actions/runs/40037554332/job/120700636607).
- Windows 3.12: [run 40037554332 / job 120700636351](https://github.com/Nortan251/agentcore/actions/runs/40037554332/job/120700636351).
- Windows 3.13: [run 40037554332 / job 120700636484](https://github.com/Nortan251/agentcore/actions/runs/40037554332/job/120700636484).
- Chromium Linux: [run 40037554432 / job 120700636688](https://github.com/Nortan251/agentcore/actions/runs/40037554432/job/120700636688).
- Chromium Windows: [run 40037554432 / job 120700636961](https://github.com/Nortan251/agentcore/actions/runs/40037554432/job/120700636961).

Task 50A's approved implementation and release are complete; post-merge CI observation remains pending at this snapshot. Remaining limits: strict verification trusts supplied task evidence; it does not provide authenticated workflow continuity, current supervised file-write authority, independent filesystem postconditions or restart reconciliation. Existing non-strict defaults are retained. The Windows short-temporary-path testing constraint remains documented.

Recommended first Task 50B implementation milestone, after separate authorization: a bounded host using the existing planner/TaskRunner, an accepted fixed plan and durable workflow/step identity, current permission plus the canonical CONFIRM approval path for one create-only file action, independent filesystem read verification, and journal-backed restart reconciliation that blocks continuation on uncertainty. Resolve how WorkspaceAgent's AUTONOMOUS-plus-enable-write composition participates in that canonical approval path before adding the write. No Task 50B implementation was started. Keep this ledger branch independent and never merge it into main.
