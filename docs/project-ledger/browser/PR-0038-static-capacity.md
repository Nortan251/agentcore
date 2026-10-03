# PR #38 — Browser Read v1.1 static capacity

AREA: browser
TASK: Increase bounded complete static-response input capacity and correct the host header-line ceiling.
STATUS: OPEN, UNMERGED; Browser ceiling repair ready for Gmail-hotfix integration; final integrated-head handoff incomplete.

BASE SHA: `dfedf634e3a2bbbf4f77e5fc03630446bb982069`
HEAD SHA: `be7f1a8340b27b8ad41d411673b77806f9142585`
PARENT SHA: `2e38dfc335f70cd497a5ab473b9a200b9bd7cd96`
BRANCH: `codex/browser-read-v1-1-static-capacity`

Snapshot verified on 2026-10-03 at 19:07 UTC. Evidence: [PR #38](https://github.com/Nortan251/agentcore/pull/38), [exact head](https://github.com/Nortan251/agentcore/commit/be7f1a8340b27b8ad41d411673b77806f9142585), [base](https://github.com/Nortan251/agentcore/commit/dfedf634e3a2bbbf4f77e5fc03630446bb982069), [parent](https://github.com/Nortan251/agentcore/commit/2e38dfc335f70cd497a5ab473b9a200b9bd7cd96), and [exact-head capacity document](https://github.com/Nortan251/agentcore/blob/be7f1a8340b27b8ad41d411673b77806f9142585/docs/architecture/browser-read-v1.1-static-capacity.md).

Git/GitHub metadata, selected implementation diffs, and CI outcomes below were independently checked for this ledger snapshot. Local test counts are attributed to the PR author and were not rerun by the ledger task. Diagnosis and uncertainty are labeled explicitly.

## Goal

Accept the characterized Wikipedia-like static HTTP 200 fixture, whose 20,000-byte CSP line and 1,782,579-byte complete body exceeded the prior 8,192-byte line and 262,144-byte body limits. Keep finite host-owned limits and existing security boundaries.

## Production changes

- `BrowserConfig` now defaults and caps individual response header/status lines at 24,576 bytes, including CRLF, and complete bodies at 2,097,152 bytes.
- The appended header-line setting reaches only response-header/status parsing. Other `_line` callers retain the existing 8,192-byte default or their narrower budget.
- The existing derived source formula, `max_body_bytes * 6 + 16`, now yields 12,582,928 bytes/characters.
- This PR changes no Gmail production code or Gmail tests.

## Files changed

Verified PR file list, relative to the base:

- `docs/architecture/browser-read-v1.1-compatibility-baseline.md`
- `docs/architecture/browser-read-v1.1-static-capacity.md`
- `src/agentcore/browser/config.py`
- `src/agentcore/browser/fetch.py`
- `tests/test_browser_fetch.py`
- `tests/test_browser_native_visibility.py`
- `tests/test_browser_render_source_bound.py`
- `tests/test_browser_response_compatibility.py`
- `tests/test_browser_session.py`
- `tests/test_browser_tool_boundary.py`

## Architecture and security decisions

The independent aggregate response-header budget remains 32,768 bytes with at most 100 fields. Body framing remains complete-or-reject for Content-Length, chunked, and EOF responses. There is no partial HTML success or CSP exception. Host-configured lower bounds remain enforceable; model arguments do not configure these limits.

The exact-head capacity document records unchanged SafeFetch numeric pinning, DNS/peer/TLS/redirect validation, per-turn host navigation authority, tool isolation and `{NET_READ}`. Chromium remains offline in fresh nonpersistent contexts, with JavaScript disabled and resource activity blocked. DOM/output/time limits and categorical public errors remain unchanged. These are documented contracts and inspected scope, not a new independent security audit by this ledger task.

## Bugs found

- Verified historical project-lead finding: the prior head incorrectly permitted host header-line settings up to 32,768 instead of the approved 24,576 ceiling.
- Historical Windows 3.11 job `111259652394` failed the unchanged Gmail `approval_expiry` case at `entered.wait(3)`. The [project-lead comment](https://github.com/Nortan251/agentcore/pull/38#issuecomment-5972257774) requires a separate hotfix; the [failed job](https://github.com/Nortan251/agentcore/actions/runs/37142495175/job/111259652394) is historical evidence.
- Diagnosis/inference: a fixed three-second rendezvous can fail under scheduler delay. The exact trigger of that historical failure remains unresolved; this snapshot does not assert a production Gmail authority defect.

## Bugs fixed

The current repair head changes the header-line validation ceiling, corresponding tests, and capacity documentation to 24,576. The independently checked config diff enforces that ceiling. The PR author reports acceptance at 24,575/24,576 and rejection at 24,577. The Gmail reliability issue is not fixed by this PR.

## Known limitations

Static HTTP 200 input only; non-200 responses including 202, compression, malformed/conflicting framing, unsupported types/charsets, oversized input, and excessive DOM counts remain rejected. Dynamic JavaScript content, clicks, forms, login, and interactive navigation remain outside scope. Larger input does not enlarge model-visible text/link projections.

Current green Windows CI does not resolve the historical rendezvous defect. A separate Gmail hotfix and subsequent integration are still required. No exact-current-head Codex review was present at snapshot time.

## Test results

- PR-author reported on current head: BrowserConfig boundaries **26 passed, 27 deselected**; focused session/fetch/compatibility/network/tool-boundary selection **448 passed**; compileall and diff-check passed.
- Independently verified current-head Chromium job logs: **419 passed** on Linux and **419 passed** on Windows.
- Independently verified Windows job logs on each Python version: junction/reparse selection **1 passed, 160 deselected**; filesystem security selection **58 passed**; the full-suite steps and jobs succeeded. Full-suite counts are not printed in the retrieved quiet logs.
- Historical parent-head results reported in the PR body: **720** complete Browser passes with Chromium; local full suite **6,197 passed, 252 skipped**. These do not establish validation of the current repair head.
- No implementation tests were executed by this documentation-only ledger task.

## CI

All six jobs succeeded with workflow `head_sha` equal to `be7f1a8340b27b8ad41d411673b77806f9142585`:

- Linux full: **SUCCESS**, Python 3.13; run `37145410446`, [job `111268195400`](https://github.com/Nortan251/agentcore/actions/runs/37145410446/job/111268195400).
- Windows 3.11: **SUCCESS**; run `37145410439`, [job `111268195350`](https://github.com/Nortan251/agentcore/actions/runs/37145410439/job/111268195350).
- Windows 3.12: **SUCCESS**; run `37145410439`, [job `111268195538`](https://github.com/Nortan251/agentcore/actions/runs/37145410439/job/111268195538).
- Windows 3.13: **SUCCESS**; run `37145410439`, [job `111268195548`](https://github.com/Nortan251/agentcore/actions/runs/37145410439/job/111268195548).
- Chromium Linux: **SUCCESS**, 419 passed; run `37145410424`, [job `111268195339`](https://github.com/Nortan251/agentcore/actions/runs/37145410424/job/111268195339).
- Chromium Windows: **SUCCESS**, 419 passed; run `37145410424`, [job `111268195258`](https://github.com/Nortan251/agentcore/actions/runs/37145410424/job/111268195258).

These runs predate Gmail-hotfix integration and cannot substitute for final integrated-head CI.

## Codex review

[Codex review comment `5971942367`](https://github.com/Nortan251/agentcore/pull/38#issuecomment-5971942367) reported no major issues on parent `2e38dfc335f70cd497a5ab473b9a200b9bd7cd96` (comment displays abbreviated `2e38dfc335`; the preceding review request names the full SHA). Historical review only. Current head `be7f1a8340b27b8ad41d411673b77806f9142585` has no verified exact-head Codex review in this snapshot.

## Unresolved review threads

GitHub GraphQL returned **0 inline review threads**, with no remaining page. The project-lead's top-level [DO NOT MERGE comment](https://github.com/Nortan251/agentcore/pull/38#issuecomment-5972257774) remains actionable despite the empty inline-thread list.

## Blockers

Separate Gmail hotfix completion/merge, integration into PR #38, six green final integrated-head jobs, and fresh exact-head Codex review. No merge is authorized by this ledger record.

## Next required action

Finish the separately scoped Gmail reliability hotfix and record its actual PR/head/evidence in the `gmail` area. After project-lead-authorized hotfix merge, update PR #38 from main, obtain fresh exact-head CI/review, and append a dated amendment here while updating its single INDEX entry. Preserve this snapshot.

## Final verdict

Browser capacity repair is documented and ready for Gmail-hotfix integration. PR #38 remains open and unmerged; the overall task is **not complete or ready for merge** at this snapshot. Six current-head green jobs do not remove the outstanding integration and review requirements.

## Amendment — 2026-10-03, 19:17 UTC: separate Gmail hotfix verified

Supersedes only the original snapshot's statement that Gmail-hotfix implementation/verification remains unfinished. [PR #39](https://github.com/Nortan251/agentcore/pull/39) now has six successful exact-head CI jobs, a clean exact-head Codex review and zero inline review threads at `d90eeab49b1de149a480351f28756b6291ee9f40`. See its [classified handoff](../gmail/PR-0039-windows-rendezvous-hotfix.md) for authoritative job/review links and diagnosis limits.

PR #39 is still open and unmerged. PR #38 remains open at `be7f1a8340b27b8ad41d411673b77806f9142585`; this ledger task changed neither implementation PR. Hotfix merge authorization, integration into PR #38, and six green jobs plus a fresh review on the final integrated Browser head remain required. The original failed job's exact trigger remains unknown; green hotfix CI does not retrospectively prove it.
