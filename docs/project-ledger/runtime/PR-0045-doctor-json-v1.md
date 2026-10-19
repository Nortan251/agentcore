# PR #45 — Doctor JSON diagnostics v1

AREA: runtime
TASK: Independent bounded machine-readable doctor diagnostics
STATUS: OPEN / UNMERGED — AWAITING FUTURE PROJECT-LEAD APPROVAL

BASE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
HEAD SHA: `522bbb56c6c51fe3f51de7490e00b3ec0ec3e0f0`
PARENT SHA: `c4a7f926b5717ed9e2a3111cd5b99dbe259bf35a`
BRANCH: `codex/doctor-json-v1`

[PR](https://github.com/Nortan251/agentcore/pull/45) · [Exact commit](https://github.com/Nortan251/agentcore/commit/522bbb56c6c51fe3f51de7490e00b3ec0ec3e0f0)

## Goal

Add one small independent milestone during the project-wide merge freeze. No PR may merge before November 2026; explicit project-lead approval remains required afterward. Auto-merge is disabled. Frozen PR #43 and #44 were not modified or advanced.

Live starting main, PR #43, PR #44 and ledger refs matched `547a2f00bcfa709d907c329fbea78730dd6731f6`, `a5f1c52447d69784847f9e75f7502e09a842c65c`, `9f57a8edc52bbd810f9e07e63438d2eee3acda26` and `332ad9f90da6640b34026887e0d85eddee1e3715`. The new branch contains the feature commit and one repairing child from main; neither frozen PR head is an ancestor.

## Production changes

The existing `doctor` already emits legacy JSON, but has no explicit `--json` flag or bounded versioned check contract. The new option emits `schema_version: 1`, boolean `ok`, and exactly five checks in fixed order: configuration, runtime, computer_control, model, providers. Each has exactly name/outcome/message. Messages are static, at most 64 characters; the whole document is below 2 KiB.

Exit codes: 0 for successful/not-configured checks, 1 for unavailable/error results, 2 for configuration-load failures. The unavailable computer-control stub produces 1 in the new mode. Legacy doctor output and exit behavior are preserved.

## Files changed

Four files, 410 insertions and 6 deletions: `src/agentcore/cli.py`, new `tests/test_cli_doctor_json.py`, new `docs/architecture/doctor-json-v1.md`, and `README.md`. No dependencies or forbidden subsystem/governance files changed.

## Architecture and security decisions

Reuse existing Agent construction, integration connection/health checks, model description and available-provider catalogue. Share integration wiring; use the existing methods for all business checks. Diagnostic projection uses fixed names, outcomes and messages rather than attempting to scrub arbitrary details. No credentials, credential variable names, paths, private configuration, integration-supplied names/details, provider/model names or raw exceptions are emitted. Configuration and constructor failures remain inside the JSON boundary. JSON mode disables logging throughout its lifecycle, including critical/custom levels, restores the prior disable threshold in finally, and never opens configured log files.

No objectives, model generation, tool actions, new network probes, retries, permission/approval changes, ActionJournal schema changes, tool-catalogue changes or generic diagnostics framework. Configured runtime initialization, including memory initialization, retains legacy doctor's behavior; no memory implementation changes. Read-only diagnostic rendering adds no state operations beyond that existing lifecycle.

## Bugs found

The requested standardized capability did not exist: legacy JSON was unversioned and included configuration-derived metadata. The missing improvement was a bounded sanitized contract with explicit exit semantics. No new finding in frozen filesystem work was investigated or repaired in this task.

## Bugs fixed

The [initial exact-head review](https://github.com/Nortan251/agentcore/pull/45#pullrequestreview-5565897672) found [P1 critical constructor log leakage](https://github.com/Nortan251/agentcore/pull/45#discussion_r4309482577) on `c4a7f926b5717ed9e2a3111cd5b99dbe259bf35a`. The source repair is `522bbb56c6c51fe3f51de7490e00b3ec0ec3e0f0`: disable all diagnostic-lifecycle logging and restore the previous threshold even on failure/interruption. The added regression failed before repair with private critical/custom-level records on stderr (1 failed, 30 deselected; stopped at first failure). Ten lifecycle success/failure cases and one interruption case now pass in the final suites.

The new format contains malformed/throwing integration and provider diagnostics and configuration/runtime/shutdown failures as categorical results. Tests verify private or very large values and exceptions cannot reach stdout/stderr. Legacy behavior is retained rather than silently replacing its schema.

## Known limitations

These are wiring/metadata checks, not authenticated provider connectivity or model-generation tests. No new network request was added. The currently unavailable computer-control adapter makes default JSON diagnostics return 1. Existing configured runtime initialization is retained; this feature is not a redesign of Agent startup. Only Windows Python 3.13 was available locally; Linux, Windows 3.11/3.12 and opt-in real Chromium gates were not executed locally for this milestone.

## Test results

Directly observed on Windows 11 build 26300 / AMD64 / Python 3.13.15 with this checkout explicitly selected through PYTHONPATH:

| Run | Passed | Failed | Skipped | Notes |
|---|---:|---:|---:|---|
| Pre-implementation regression | 0 | 1 | 0 | Intentional: old parser rejected --json |
| Final focused doctor / CLI | 51 | 0 | 0 | 0.56s |
| All CLI + config/logging/integrations/model factory/provider/integration | 995 | 0 | 0 | 75.99s |
| Full repository | 6468 | 0 | 264 | 89 warnings; 428.40s |

Focused command: `python -m pytest -o addopts='' -q tests/test_cli_doctor_json.py tests/test_cli.py -ra`. Surrounding tests use all `test_cli*.py` files plus `test_config.py`, `test_logging.py`, `test_integrations.py`, `test_models_factory.py`, `test_models_provider.py`, and `test_models_integration.py`. Full command: `python -m pytest -o addopts='' -q -ra`. Separate short workspace temporary directories were supplied to each run.

Compileall (`src tests`), unstaged/staged/committed whitespace checks, base-relative source review, four-file scope checks, exact parent/provenance checks and clean status passed. AST comparison verified only parser/main changed among pre-existing CLI functions, with three doctor-specific helpers added. Direct baseline execution confirmed legacy default stdout is byte-for-byte equal to verified main. A real CLI smoke run produced the documented five checks and expected exit 1. No implementation changes followed validation.

Tests cover deterministic keys/schema, successful, unavailable and failed checks, malformed results, bounded redaction, no credentials/private paths/raw exceptions, legacy compatibility, correct exit codes, shared check calls, and absence of new model/tool/network/durable-state actions. The full suite warnings were recorded, including existing Gmail fake-response cleanup warnings; no unrelated repair was bundled.

## CI

All six automatic jobs on the exact head concluded FAILURE before execution, with zero steps. Each annotation independently reports failed recent account payments or a spending limit requiring an increase. No assertions ran. No manual reruns or CI waiver occurred.

- Linux full: billing-blocked; [run 39183033764 / job 117839654736](https://github.com/Nortan251/agentcore/actions/runs/39183033764/job/117839654736).
- Windows 3.11: billing-blocked; [run 39183033757 / job 117839654926](https://github.com/Nortan251/agentcore/actions/runs/39183033757/job/117839654926).
- Windows 3.12: billing-blocked; [run 39183033757 / job 117839654784](https://github.com/Nortan251/agentcore/actions/runs/39183033757/job/117839654784).
- Windows 3.13: billing-blocked; [run 39183033757 / job 117839654655](https://github.com/Nortan251/agentcore/actions/runs/39183033757/job/117839654655).
- Chromium Linux: billing-blocked; [run 39183033743 / job 117839655103](https://github.com/Nortan251/agentcore/actions/runs/39183033743/job/117839655103).
- Chromium Windows: billing-blocked; [run 39183033743 / job 117839654868](https://github.com/Nortan251/agentcore/actions/runs/39183033743/job/117839654868).

## Codex review

[Fresh exact-head review requested](https://github.com/Nortan251/agentcore/pull/45#issuecomment-6257379332). The [Codex activity summary](https://github.com/Nortan251/agentcore/pull/45#issuecomment-6257233494) reports COMPLETED on `522bbb56c6c51fe3f51de7490e00b3ec0ec3e0f0` at 2026-10-19 01:01:35 UTC, with `has_any_findings=false` and `has_unresolved_findings=false`. This is the observed exact-head re-review result; the separate GitHub review submission record remains the initial review on the parent. No project-lead approval or CI success is implied.

## Unresolved review threads

The original P1 thread was resolved only after its repairing commit, deterministic failing/passing regression, final-head validation and completed no-findings re-review existed. A fresh GraphQL read confirmed zero unresolved PR45 threads. No outstanding P0/P1/P2 finding was observed on the final head.

## Blockers

Project-wide merge freeze and future explicit project-lead approval; unavailable required CI; Linux/Windows matrix and Chromium evidence. Local passes do not override missing CI or authorize merge.

## Next required action

Restore Actions account availability and obtain the required exact-head gates. The completed review and local passes do not authorize merge. Preserve the merge freeze even if technically ready. Do not start another milestone or resume frozen filesystem work automatically.

## Final verdict

**OPEN / UNMERGED — AWAITING FUTURE PROJECT-LEAD APPROVAL.** No merge before November 2026, and explicit project-lead approval is mandatory afterward. No auto-merge, force-push, history rewrite or frozen-PR changes. The ledger branch remains independent documentation-only history and must never merge into main.
