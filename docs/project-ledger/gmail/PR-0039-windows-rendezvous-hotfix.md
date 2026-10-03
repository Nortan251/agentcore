# PR #39 — Windows Gmail authority-test rendezvous hotfix

AREA: gmail
TASK: Replace timing-only Gmail mutation/send authority test rendezvous with bounded, outcome-aware synchronization and guaranteed cleanup.
STATUS: OPEN, UNMERGED; implementation, testing, exact-head CI and Codex review complete; project-lead review required.

BASE SHA: `dfedf634e3a2bbbf4f77e5fc03630446bb982069`
HEAD SHA: `d90eeab49b1de149a480351f28756b6291ee9f40`
PARENT SHA: `dfedf634e3a2bbbf4f77e5fc03630446bb982069`
BRANCH: `codex/gmail-windows-approval-rendezvous-hotfix`

Evidence: [PR #39](https://github.com/Nortan251/agentcore/pull/39), [exact head](https://github.com/Nortan251/agentcore/commit/d90eeab49b1de149a480351f28756b6291ee9f40), [base/parent](https://github.com/Nortan251/agentcore/commit/dfedf634e3a2bbbf4f77e5fc03630446bb982069), and [review](https://github.com/Nortan251/agentcore/pull/39#issuecomment-5972568957).

Final evidence verified on 2026-10-03 at 19:17 UTC, after all required implementation verification gates passed.

Git/GitHub metadata, the complete four-file PR diff, workflow head SHAs, job outcomes, review and thread state were independently verified for this ledger handoff. Local test results and the isolated call-path investigation are attributed to the PR author; the ledger task did not rerun implementation tests. Diagnosis and unresolved uncertainty are identified below.

## Goal

Repair the Windows-sensitive test harness exposed by the historical PR #38 Windows 3.11 `approval_expiry` failure at `entered.wait(3)`. Preserve production Gmail authority semantics and deterministic no-POST checks. Keep this hotfix separate from Browser Read capacity changes.

## Production changes

None. The verified PR diff contains only tests/helpers; no changes under `src/`, workflows, governance, dependencies, or PR #38.

## Files changed

- `tests/gmail_send_fakes.py`: shared `WorkerRendezvous` and bounded watchdog.
- `tests/test_gmail_mutation_authority.py`: outcome-aware profile/post/probe and POSIX lock-holder rendezvous, with cleanup.
- `tests/test_gmail_send_authority.py`: analogous policy/vault/final-lease and POSIX lock-holder rendezvous; shared ownership-worker join budget.
- `tests/test_gmail_worker_rendezvous.py`: eleven deterministic characterization cases.

## Architecture and security decisions

One `threading.Condition` waits for boundary entry **or** captured worker completion. Early captured exceptions are raised; early normal returns report their actual result instead of resembling a generic unset Event. A context manager releases and joins the worker on host failure as well as success. Callback-watchdog failure is checked separately during cleanup so production exception scrubbing cannot disguise a barrier timeout as an authority rejection.

Entry, callback release and join each have a finite 30-second test watchdog, chosen as three times the existing ten-second Gmail socket bound to allow local writes, TLS setup and loaded Windows scheduling. This is a test diagnostic budget, not a production total-operation deadline. The diff adds no sleeps, retries, xfails or new platform skips.

Existing approval/credential expiry, cancellation, claim-change, lease-exclusion, metadata-mutex and no-POST assertions remain. Production `ToolRegistry -> validation -> PermissionPolicy -> ToolExecutor -> Tool.run` and Gmail dispatch checks are untouched. POSIX child-rejection checks remain five-second bounded checks.

## Bugs found

- Verified harness defect: the old three-second entry wait ignored already captured worker outcomes, conflating valid delayed progress, early exceptions and legitimate fail-closed returns.
- Short callback/join windows introduced similar timing assumptions; POSIX lock-holder entry could fail before reaching its cleanup block.
- A callback barrier timeout could be scrubbed into a failed operation result and resemble the authority rejection under test.
- Historical evidence: [failed Windows 3.11 job `111259652394`, run `37142495175`](https://github.com/Nortan251/agentcore/actions/runs/37142495175/job/111259652394). Its missing worker outcome/stack prevents recovery of the precise historical trigger.

## Bugs fixed

Outcome-aware notifications expose early exceptions/returns immediately, finite watchdogs distinguish missing progress, and cleanup releases/joins workers on host failure. A separate callback-timeout flag prevents scrubbed barrier failure from passing an authority-rejection test. The shared repair covers the corresponding mutation/send authority sites and POSIX lock holders.

Deterministic tests cover entry, RuntimeError/SystemExit, early successful return, bounded timeout, host-failure cleanup, notification of an already waiting host, scrubbed callback failure, and actual LiveHarness preparation/pending-approval/TLS-delay paths. Event-gated TLS delay proves a valid worker can miss a deadline and later reach profile/POST; it does not reproduce or identify the historical Windows scheduler event.

## Known limitations

The original failed job's exact trigger remains unknown. Scheduling/load and local disk/TLS delay are supported possibilities, not proven causes of that specific failure. No production authority defect was established.

Local Python 3.11 was unavailable; local testing used Windows Python 3.13. Existing POSIX skips on Windows are reported separately; Linux full CI supplies platform coverage. An uncooperative/deadlocked Python thread cannot be forcibly stopped: the bounded join fails and the daemon remains a process-exit safeguard.

Pre-existing fake HTTPResponse interruption/destructor warnings remain. Unrelated read/consent/composition rendezvous were inspected but left outside this mutation/send hotfix. PR-author investigation reports no Browser module loaded in the isolated Gmail call path; broader-suite resource load can still affect scheduling.

## Test results

PR-author reported, with final-source counts distinguished from earlier selections:

- Exact-base mutation authority characterization: **9 passed, 1 existing POSIX skip**.
- Final helper/mutation/send authority: **33 passed, 2 existing POSIX skips**.
- Focused plus directly related tests before the final callback-watchdog guard: **305 passed, 2 existing POSIX skips**.
- Final-source broader Gmail selection: **954 passed, 4 existing POSIX skips, 5,384 deselected**.
- Final-source `python -m compileall -q src tests` and `git diff --check`: passed.
- Independently verified Chromium job logs: **360 passed** on Linux and **360 passed** on Windows.
- No local full-repository run; exact-head full-suite CI is recorded below. Implementation tests were not rerun by the documentation-only ledger task.

## CI

All six jobs succeeded with workflow `head_sha` equal to `d90eeab49b1de149a480351f28756b6291ee9f40`:

- Linux full: **SUCCESS**, Python 3.13; run `37146744736`, [job `111272157746`](https://github.com/Nortan251/agentcore/actions/runs/37146744736/job/111272157746).
- Windows 3.11: **SUCCESS**; run `37146744737`, [job `111272157819`](https://github.com/Nortan251/agentcore/actions/runs/37146744737/job/111272157819).
- Windows 3.12: **SUCCESS**; run `37146744737`, [job `111272157855`](https://github.com/Nortan251/agentcore/actions/runs/37146744737/job/111272157855).
- Windows 3.13: **SUCCESS**; run `37146744737`, [job `111272157726`](https://github.com/Nortan251/agentcore/actions/runs/37146744737/job/111272157726).
- Chromium Linux: **SUCCESS**, 360 passed; run `37146744748`, [job `111272157934`](https://github.com/Nortan251/agentcore/actions/runs/37146744748/job/111272157934).
- Chromium Windows: **SUCCESS**, 360 passed; run `37146744748`, [job `111272157739`](https://github.com/Nortan251/agentcore/actions/runs/37146744748/job/111272157739).

Full-suite counts are not available from the quiet CI output used for this record; successful jobs are not converted into invented pass/skip totals. No failed-job rerun or waived gate is reported.

## Codex review

[Codex comment `5972568957`](https://github.com/Nortan251/agentcore/pull/39#issuecomment-5972568957) reports no major issues on exact head `d90eeab49b1de149a480351f28756b6291ee9f40` (displayed as `d90eeab49b`; the [review request](https://github.com/Nortan251/agentcore/pull/39#issuecomment-5972546891) names the full SHA). The review covers scope, authority assertions, outcome diagnostics, bounded cleanup, rendezvous inventory, deadlocks and cross-test leaks.

## Unresolved review threads

GitHub GraphQL returned **0 inline review threads**, with no remaining page. No unresolved inline finding was present at the exact-head check.

## Blockers

No outstanding implementation/test/CI/Codex-review blocker at the recorded head. Project-lead review and explicit merge authorization remain required. PR #38 still needs hotfix integration and its own final-head validation.

## Next required action

Project lead independently reviews this exact head and decides whether to authorize merge. If merged, integrate main into PR #38, rerun six CI gates and fresh Codex review on that final Browser head, then append dated amendments to the corresponding ledger records. Do not merge automatically.

## Final verdict

Hotfix implementation and verification are complete and ready for independent project-lead review. PR #39 is open and unmerged. This ledger publication grants no merge authority and does not complete PR #38.

## Amendment — 2026-10-03: project-lead review and merge

This amendment supersedes the earlier open/unmerged status while preserving the historical snapshot above.

The project lead independently verified exact head `d90eeab49b1de149a480351f28756b6291ee9f40`, the four test/helper-only changed files, six successful exact-head CI jobs, fresh Codex review, and zero unresolved review threads. PR #39 was merged with an expected-head guard as `7d9d34453ac384dc2a423600dba6a900731002c0`.

PR #38 subsequently integrated that new main, passed its own fresh exact-head gates, and merged as `72a0f55be620565344b3e57667371e2a695adb0e`. The Gmail hotfix remains test-only in the integrated tree.

Final status: **MERGED; Gmail Windows authority-test rendezvous hotfix complete.**
