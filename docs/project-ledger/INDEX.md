# AgentCore project handoff ledger

Canonical entry point for engineering handoffs in [Nortan251/agentcore](https://github.com/Nortan251/agentcore).

**Permanent branch: `agentcore/project-ledger`. NEVER merge this branch into `main`. Never force-push it.**

This branch has an independent documentation-only root commit. Its tracked tree contains handoff records only, without production source or implementation history. Implementation PRs remain separate.

| PR | Area | Task | Exact head | Status | Handoff |
|----|------|------|------------|--------|---------|
| [#45](https://github.com/Nortan251/agentcore/pull/45) | runtime | Doctor JSON diagnostics v1 | `522bbb56c6c51fe3f51de7490e00b3ec0ec3e0f0` | OPEN / UNMERGED — AWAITING FUTURE PROJECT-LEAD APPROVAL; merge freeze; six gates billing-blocked; logging P1 repaired/resolved; exact-head Codex review completed without findings | [PR-0045-doctor-json-v1.md](runtime/PR-0045-doctor-json-v1.md) |
| [#44](https://github.com/Nortan251/agentcore/pull/44) | filesystem | POSIX staged-create publication integrity hotfix | `9f57a8edc52bbd810f9e07e63438d2eee3acda26` | OPEN / UNMERGED; worker import repair implemented; two threads unresolved pending evidence; production P1 and native proof outstanding; six new-head gates billing-blocked; fresh review requested | [PR-0044-fs-stage-publication-integrity-hotfix.md](filesystem/PR-0044-fs-stage-publication-integrity-hotfix.md) |
| [#43](https://github.com/Nortan251/agentcore/pull/43) | filesystem | Revision-bound File Replace v1 | `a5f1c52447d69784847f9e75f7502e09a842c65c` | OPEN / UNMERGED; STOPPED merged-foundation P1; one review P1 unresolved; six gates billing-blocked | [PR-0043-file-replace-v1.md](filesystem/PR-0043-file-replace-v1.md) |
| [#42](https://github.com/Nortan251/agentcore/pull/42) | filesystem | Journal-bound create-only Directory Create v1 | `f8d965e6b7053b80659d3d1adde54df4ad500aa0` | MERGED as `547a2f00bcfa709d907c329fbea78730dd6731f6`; project-lead review and exact-head gates complete | [PR-0042-directory-create-v1.md](filesystem/PR-0042-directory-create-v1.md) |
| [#41](https://github.com/Nortan251/agentcore/pull/41) | filesystem | Explicit one-shot Workspace Agent v1 | `9c4d0123df9c3b161f81b3c1923cf35c80bd1dc2` | MERGED as `d6acae41e95059289956a4a2acde33d170a537cf`; project-lead review and exact-head gates complete | [PR-0041-workspace-agent-v1.md](filesystem/PR-0041-workspace-agent-v1.md) |
| [#40](https://github.com/Nortan251/agentcore/pull/40) | filesystem | Durable create-only Filesystem Write v1 | `e32a9c6a66537c4da15522e0262ee4b415fbf934` | MERGED as `67e6069e30979fea68aefd50164123a7348ada3e`; final exact-head CI/review and project-lead review complete | [PR-0040-filesystem-write-v1.md](filesystem/PR-0040-filesystem-write-v1.md) |
| [#39](https://github.com/Nortan251/agentcore/pull/39) | gmail | Windows Gmail authority-test rendezvous hotfix | `d90eeab49b1de149a480351f28756b6291ee9f40` | MERGED as `7d9d34453ac384dc2a423600dba6a900731002c0`; superseded by Browser integration on main | [PR-0039-windows-rendezvous-hotfix.md](gmail/PR-0039-windows-rendezvous-hotfix.md) |
| [#38](https://github.com/Nortan251/agentcore/pull/38) | browser | Browser Read v1.1 static capacity | `a404120a739d4dd0960f687ebe94cf34adebb939` | MERGED as `72a0f55be620565344b3e57667371e2a695adb0e`; final tests/CI/review recorded; handoff complete | [PR-0038-static-capacity.md](browser/PR-0038-static-capacity.md) |

## Organization

Areas: `browser`, `gmail`, `runtime`, `realtime`, `action-journal`, `memory`, `computer-control`, `integrations`, `misc`.

Create area directories only when adding handoffs; do not add empty-directory placeholders. Name each handoff `<area>/PR-XXXX-short-task-slug.md`, with a zero-padded actual PR number. Never invent a PR number or publish a final handoff before the PR exists.

Every handoff has exactly one table entry, ordered newest PR first. Amendments stay in that PR's existing handoff; update its single index row to describe the latest recorded head and status.

## Update procedure for every AgentCore task that opens or updates a PR

1. Finish implementation and testing first, then push the implementation branch.
2. Obtain the actual PR number and exact remote implementation head SHA.
3. In a separate ledger checkout, run `git fetch origin` and synchronize with `origin/agentcore/project-ledger` before editing. Use a fast-forward when possible; reconcile diverging ledger records without overwriting another update.
4. Create or update the classified handoff and its exactly one INDEX entry. Use the format below and authoritative GitHub evidence.
5. Verify the staged changes contain only ledger documentation, check links/schema/index consistency, and run `git diff --check`.
6. Commit and push `agentcore/project-ledger` without force. If the push races another update, fetch, reconcile, revalidate, and push again.
7. Report the implementation PR/head and the ledger handoff path/ledger commit SHA. The ledger update must not alter the implementation PR.

Never erase old handoffs. Preserve the original snapshot and append dated, clearly marked amendments for superseding evidence; do not silently rewrite historical facts. Keep each record concise but sufficient for independent project-lead review.

Do not store secrets, credentials, tokens, private email contents, or raw sensitive tool arguments. Do not use local filesystem paths as authoritative references. Link to GitHub PRs, exact commits, review comments, workflows and jobs. Distinguish verified facts, reported results, diagnosis/inference, and unresolved uncertainty. A PR's existence does not establish that its Codex task is complete.

## Required handoff format

```markdown
# <PR number> — <task>

AREA:
TASK:
STATUS:

BASE SHA:
HEAD SHA:
PARENT SHA:
BRANCH:

## Goal
## Production changes
## Files changed
## Architecture and security decisions
## Bugs found
## Bugs fixed
## Known limitations
## Test results
## CI
- Linux full:
- Windows 3.11:
- Windows 3.12:
- Windows 3.13:
- Chromium Linux:
- Chromium Windows:
## Codex review
## Unresolved review threads
## Blockers
## Next required action
## Final verdict
```

Include workflow/job IDs when available and the exact reviewed SHA. Mark missing evidence explicitly; do not substitute older-head evidence for current-head validation.

## Initial backfill

PR #38 and PR #39 retain their original pre-merge snapshots plus dated amendments. PR #39 merged as `7d9d34453ac384dc2a423600dba6a900731002c0`; PR #38 then integrated that main, passed fresh exact-head gates at `a404120a739d4dd0960f687ebe94cf34adebb939`, and merged as `72a0f55be620565344b3e57667371e2a695adb0e`. Do not reconstruct older tasks from memory. Backfill older important PRs from GitHub evidence only when requested.
