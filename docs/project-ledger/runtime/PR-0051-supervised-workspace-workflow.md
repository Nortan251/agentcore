# PR #51 — Task 50B complete supervised workspace workflow

AREA: runtime
TASK: Inspect declared workspace inputs, separately authorize one create, independently verify exact bytes
STATUS: DRAFT / OPEN / UNMERGED — VALIDATED; GO FOR INDEPENDENT REVIEW

BASE SHA: `f09f670dd7a6abccdb2889c6e5685537db6dda63`
HEAD SHA: `c51cf13e944d2bc6ade2f1231d5ca48686632768`
PARENT SHA: `b2f9cc64e227714b73603c8eb9c70013c7b68dbf`
BRANCH: `codex/task-50b-supervised-workflow`

[PR #51](https://github.com/Nortan251/agentcore/pull/51) · [exact head](https://github.com/Nortan251/agentcore/commit/c51cf13e944d2bc6ade2f1231d5ca48686632768) · [architecture contract](https://github.com/Nortan251/agentcore/blob/c51cf13e944d2bc6ade2f1231d5ca48686632768/docs/architecture/task-50b-supervised-workflow.md)

## Goal
Deliver a complete bounded supervised objective, not a file-writing demonstration: accepted plan, declared inspection, exact human file approval, one create-only mutation and independent complete read-back verification. Task 50A's six post-merge jobs passed before branching. The owner authorized implementation and a Draft PR, not Ready or merge.

## Production changes
`agentcore workflow` composes existing LLMPlanner, strict TaskRunner, sealed ToolRegistry, ToolExecutor, PermissionPolicy, DurableApprovalAuthority, filesystem tools, protected storage, ActionJournal and recovery coordinator. The host validates a linear 3–8-step plan against up to five operator-declared inputs and one output. It persists acceptance and stable run/task/step identity before tools, then creates one bounded draft from accepted observations. Separate private terminal challenges accept the plan and approve the full exact UTF-8 body/destination. The existing executor owns expiring one-shot approval and publication. A distinct read must match untruncated byte length/SHA-256 before a linked durable verification receipt permits success.

## Files changed
Exactly eight cumulative files:
- `src/agentcore/cli.py`: isolated workflow command/arguments and bounded public logging surface.
- `src/agentcore/credentials/_protected_file.py`: one fixed workflow filename, retaining the pinned atomic private-file boundary.
- `src/agentcore/workspace_host/workflow.py`: accepted profile, canonical invocation bridge, strict verifiers, current authority and read-only resume.
- `src/agentcore/workspace_host/workflow_storage.py`: authenticated inert continuity and existing approval/protected-plan storage.
- `src/agentcore/workspace_host/workflow_cli.py`: private interactive human decisions and categorical reporting.
- `tests/test_task_50b_workflow.py`: deterministic workflow/authority/privacy/restart regressions.
- `tests/fixtures/task50b_crash_worker.py`: finite native process-death fault worker.
- `docs/architecture/task-50b-supervised-workflow.md`: usage, contracts, limits and evidence.

No governance, native publication, permission hierarchy, generic runner or WorkspaceAgent v1 behavior was changed. No new dependency, planner, journal or automation framework.

## Architecture and security decisions
Creation requires current CONFIRM plus explicit write opt-in; plan acceptance is not mutation approval. Immutable arguments are resolved privately at the existing executor boundary. Model prose cannot authorize or mark completion. A confirmed action receipt verifies the mutating prerequisite, releasing the separate read without circular verification. Raw observations/drafts stay outside generic Task/AgentState and public output. Stable dedup keys and a durably saved dispatch latch prohibit create replay after interruption. ActionJournal remains authoritative for physical effects; continuity records only plan/progress/postcondition correlation.

Restart authenticates plans/approvals, validates workflow/action links and terminal receipts, and uses the existing coordinator for bounded canonical FS_READ probes. Revoked FS_WRITE does not block independently allowed reconciliation or an explicitly accepted final verification read. Unknown effects never release dependents. Orphaned/missing/corrupt essential evidence fails closed, rather than admitting another objective. No retry/reset/compensation path is exposed.

## Bugs found
The self-audit identified an ignored provider token option, stale planner identity reuse that could overwrite completed continuity, and default terminal output that omitted the historical verification label/time. Linux CI additionally found an approval owner lock created with ordinary POSIX file defaults, preventing safe reopen. Each issue has a focused reproducer. The draft's independent UTF-8 byte ceiling prevented oversized publication even before the provider-option repair.

## Bugs fixed
Provider repair `4c1b277278ebf84f26677bac31ab41a0ec578d2b` passes `max_output_tokens` through BaseModelProvider, with a failing-before/passing-after request-contract regression. POSIX repair `314e23006a22b7b4678a2e289bf072b1cb991800` pre-creates the approval lock with the existing private-lock helper, preserves refusal of unsafe existing locks without chmod, and verifies restart. The POSIX test asserts canonical durable NOT_INVOKED / CONFIRMED_NOT_APPLIED refusal and no file effect, rather than incorrectly expecting an empty journal. Identity repair `b2f9cc64e227714b73603c8eb9c70013c7b68dbf` rejects retained task IDs before acceptance/dispatch. Final head `c51cf13e944d2bc6ade2f1231d5ca48686632768` labels and timestamps historical terminal output. The self-audit also added fail-closed orphan/missing evidence admission and preserved confirmed action truth on verification-save failure. No known actionable P0/P1/P2 remains in the implementation self-assessment; independent review is still required.

## Known limitations
Native Windows publication only; Linux/macOS refuse. Protected storage authenticates plaintext under host ACLs, not encryption or protection from malicious same-user/admin code. At most 256 retained workflows; an unverified workflow keeps the active slot for operator investigation, with no automatic reset/retry. A confirmed create alone is incomplete. Verification proves bytes at its observation time, not future immutability, semantic quality or an atomic workspace snapshot. Provider/human waits are cooperative and late results are discarded before dispatch. Existing filesystem recovery and Windows durability limits are inherited. Deterministic tests use synthetic model/UI decisions; no live human/provider walkthrough is claimed.

## Test results
Fresh native Windows focused final-head run: **69 passed, 2 skipped**. Native file creation and independent exact read-back passed for UTF-8, empty, control-containing and byte-limit cases. Actual public approval data/body is not published; tests assert durable consumed approval correlation and linked verification, using synthetic typed human decisions.

Seven real subprocess death boundaries passed: pre-dispatch, action authorization, claim, native publication, success return, final read and durable receipt. Restart makes zero create calls; confirmed cases finish only via read, uncertain/unattempted cases remain stopped. Tests also cover denial/expiry/revocation, lower capability overrides, existing targets, unsafe inputs/paths, changed/deleted/truncated files, lost executor returns, duplicate owners, missing/corrupt evidence and confidential public/generic state.

Surrounding runtime/approval/recovery/filesystem/WorkspaceAgent/credential/Gmail suites returned **633 passed, 31 skipped, 6 warnings** before the subsequent repairs; these counts overlap focused cases and are not a final-head full-suite claim. Earlier partial full invocations were interrupted for reproduced repairs and are not counted as successful.

Final-head uninterrupted local full suite: **7,174 passed, 393 skipped, 89 warnings in 686.76 seconds**. JUnit confirms 7,567 cases, zero failures and zero errors. Compilation, full base-to-head committed whitespace and unchanged source checks passed after the run. The final run includes all focused/compatibility/security cases; no tests/governance were weakened. The warnings include known Gmail test-double unraisable diagnostics, not hidden failures.

## CI
Final head `c51cf13e944d2bc6ade2f1231d5ca48686632768`:
- Linux full: [run 40049824771 / job 120739811261](https://github.com/Nortan251/agentcore/actions/runs/40049824771/job/120739811261) — SUCCESS; **7,014 passed, 553 skipped, 98 warnings**, compile/whitespace passed.
- Windows 3.11: [run 40049824671 / job 120739810797](https://github.com/Nortan251/agentcore/actions/runs/40049824671/job/120739810797) — SUCCESS.
- Windows 3.12: [run 40049824671 / job 120739810900](https://github.com/Nortan251/agentcore/actions/runs/40049824671/job/120739810900) — SUCCESS.
- Windows 3.13: [run 40049824671 / job 120739810975](https://github.com/Nortan251/agentcore/actions/runs/40049824671/job/120739810975) — SUCCESS.
- Chromium Linux: [run 40049824675 / job 120739810701](https://github.com/Nortan251/agentcore/actions/runs/40049824675/job/120739810701) — SUCCESS.
- Chromium Windows: [run 40049824675 / job 120739810819](https://github.com/Nortan251/agentcore/actions/runs/40049824675/job/120739810819) — SUCCESS.

All six fetched job logs identify the same synthetic merge `8fc94cee86518b64190fbfbec5c69591ec90434b`, parents unchanged base `f09f670dd7a6abccdb2889c6e5685537db6dda63` and candidate `c51cf13e944d2bc6ade2f1231d5ca48686632768`. Its tree `07972614fcb93b844ee72bb4b8e145093cf68922` exactly equals the candidate; direct diff is empty. Linux behavior is native CI evidence; no local Linux/WSL execution was possible or claimed.

## Codex review
Critical implementation self-audit performed; this is not an independent review or native GitHub approval. No duplicate automated review requested. Draft retained for a separate exact-head independent assessment.

## Unresolved review threads
**0** inline review threads, **0** review submissions and no review comments at the final inventory check. No thread resolution was performed; this is not an independent review clearance.

## Blockers
No known implementation/validation P0/P1/P2 blocker remains in the self-audit. Independent exact-head review and separate owner authorization remain required before Ready/merge. No merge operation is authorized in Task 50B.

## Next required action
Independently review this exact head against the named base. Keep Draft and unmerged; obtain separate owner merge authorization after review. No additional implementation or automatic review loop is needed for this handoff.

## Final verdict
**TECHNICAL GO FOR INDEPENDENT REVIEW.** Complete bounded capability delivered and exact-head local/native CI validation passed. PR #51 remains Draft/open/unmerged, with no native GitHub approval or independent review claimed. The ledger remains documentation-only and separate from main.
