# PR #49 — Conversation desktop evidence and provider compatibility

AREA: computer-control
TASK: Supervised natural-language desktop conversation evidence and provider compatibility
STATUS: MERGED (2026-10-23)

BASE SHA: `42fae5e72cd6ba014982bd6da0c77d3cbd88b996`
HEAD SHA: `1a081f0e2a82eac30c7936800c81965492144079`
PARENT SHA: `d4a964625ae9b9ac5f248f0e1bdb24a24da1dc79`
BRANCH: `codex/task-49-conversation-desktop-e2e`

[PR #49](https://github.com/Nortan251/agentcore/pull/49) · [final implementation commit](https://github.com/Nortan251/agentcore/commit/1a081f0e2a82eac30c7936800c81965492144079) · [normal merge commit](https://github.com/Nortan251/agentcore/commit/042e6bc5bcacd2872222d5029a5df607fd733255)

## Goal
Verify a bounded natural-language desktop conversation through the existing ToolRegistry → validation → PermissionPolicy → ToolExecutor → isolated Windows-native path, without new native authority. Keep model text, actual execution, approval and human visual confirmation distinct.

## Production changes
- Synchronous OpenAI-compatible requests explicitly use `stream=false`; optional strictly typed thinking configuration remains opt-in, with streaming behavior distinct.
- Constrained interactive Windows foreground observation, Notepad denial and approved one-shot launch collect factual receipts. Operator confirmation is separate from native acceptance.
- Launcher authenticates attributed commit/tree/source blobs and constrains executable Python source and artifacts; refuses unsuitable source conditions rather than attributing mutable execution to a commit.
- Native receipt enrichment uses sibling temporary serialization and atomic replacement. CLI receipt write, flush and close failures yield uncertainty/no-automatic-retry warnings rather than falsely successful reports.

## Files changed
Final cumulative PR scope: **15 changed files**. Provider and configuration compatibility; desktop CLI/evidence and prompts; PowerShell launcher; operational documentation and focused tests. Exact diff: [PR files](https://github.com/Nortan251/agentcore/pull/49/files). Governance (`AGENTS.md`, CI workflows), central PermissionPolicy, ToolExecutor, registered native adapter and native service were not modified by Task 49.

## Architecture and security decisions
No arbitrary desktop control or new provider credential is introduced. Model output is never authority or real-world proof. Tracked and ignored source/executable-artifact provenance guards, current approval, one-action bounds, durable authority boundaries and receipt-preserving failures are retained. The launcher is not an immutable machine sandbox.

## Bugs found
Three historical automated P1 findings: dirty working-tree commit attribution; ignored `.pyc`/native `.pyd` executed despite cache precautions; and destructive non-atomic receipt update. Later independent review identified further object/working-tree provenance weaknesses, then one lower-severity P3 CLI receipt-close uncertainty-reporting defect. All underwent scoped repairs.

## Bugs fixed
Exact source provenance is tied to authenticated commit/tree/blob snapshots and isolated execution; executable artifacts are refused. Enrichment uses atomic sibling replacement with CLR null backup correctly bound on PowerShell 5.1 and 7. Close/write/flush evidence failures preserve categorical uncertainty and no automatic replay. Three historical P1 review threads were individually reconciled and formally resolved before merge.

## Known limitations
Four operator-confirmed live desktop passes are historical receipts on ancestor heads, not current-head hardware testing or signed immutable-source proof. Other limits: initial-receipt crash gap, trusted host and dependencies, power-loss/durability boundaries and unexplained GLM provider timeout. No subsequent live hardware/provider tests are claimed.

## Test results
Reported local final-head suite: **7,075 passed, 391 skipped, 89 warnings**. Narrow last repair: **11 regressions passed**, focused **474 passed**, surrounding **478 passed / 2 skipped**. These numbers originate in the engineering handoff; this ledger amendment did not rerun them. Merge candidate PR CI and following main post-merge CI were separately checked on GitHub.

## CI
Exact final implementation head `1a081f0e2a82eac30c7936800c81965492144079`: all six jobs SUCCESS.
- Linux full: [run 39929328240](https://github.com/Nortan251/agentcore/actions/runs/39929328240).
- Windows 3.11, 3.12, 3.13: [run 39929328239](https://github.com/Nortan251/agentcore/actions/runs/39929328239); one unchanged-job 3.12 timeout required rerun and cause remains unproven.
- Chromium Ubuntu/Windows: [run 39929328248](https://github.com/Nortan251/agentcore/actions/runs/39929328248).

Post-merge commit `042e6bc5bcacd2872222d5029a5df607fd733255`: all six also SUCCESS.
- Linux full: [run 39997756290](https://github.com/Nortan251/agentcore/actions/runs/39997756290).
- Windows 3.11, 3.12, 3.13: [run 39997756399](https://github.com/Nortan251/agentcore/actions/runs/39997756399).
- Chromium Ubuntu/Windows: [run 39997756237](https://github.com/Nortan251/agentcore/actions/runs/39997756237).

## Codex review
The previous automated GitHub review remained marked Running on older commit `62090d0f31c33b6856ee719578d0cad3b2e26ad9`; no completed current-head automated verdict was represented. The owner explicitly accepted [a full-diff independent review and final-delta assessment](https://github.com/Nortan251/agentcore/pull/49#pullrequestreview-5632228684) on the exact candidate in lieu of waiting for that bot, without weakening repository governance.

## Unresolved review threads
**0** at final merge: all three historical P1 threads marked resolved on GitHub after individual repair evidence review.

## Blockers
None for the completed merge. Historical automated-review backend uncertainty and documented runtime/platform limitations remain separately tracked; they do not reverse or imply a fresh automated approval.

## Next required action
Task 50, a supervised bounded workflow, remains a separately scoped milestone; plan acceptance and per-effect current permission/approval must remain distinct. Do not infer Task 50 implementation or a PR from this handoff.

## Final verdict
**MERGED AND VERIFIED**. Normal merge SHA `042e6bc5bcacd2872222d5029a5df607fd733255`, parents unchanged main `42fae5e72cd6ba014982bd6da0c77d3cbd88b996` and exact candidate `1a081f0e2a82eac30c7936800c81965492144079`; matching tree verified in engineering completion report.
