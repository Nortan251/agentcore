# PR #43 — File Replace v1

AREA: filesystem
TASK: Revision-bound full-file atomic replacement with stale-read protection
STATUS: OPEN / UNMERGED; verification blocked (2026-10-04)

BASE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
HEAD SHA: `09ff3643a90b0b7ceab007862bd9bdbd17be1604`
PARENT SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
BRANCH: `codex/file-replace-v1`

[PR](https://github.com/Nortan251/agentcore/pull/43) · [Exact implementation commit](https://github.com/Nortan251/agentcore/commit/09ff3643a90b0b7ceab007862bd9bdbd17be1604) · [Architecture](https://github.com/Nortan251/agentcore/blob/09ff3643a90b0b7ceab007862bd9bdbd17be1604/docs/architecture/filesystem-replace-v1.md)

## Goal

Add only complete bounded update-read and existing-file full replacement to the explicit Workspace Host. Preserve merged PR40/41/42 authority, privacy and recovery invariants.

## Production changes

Two tools, `read_file_for_update` and `replace_file`, use a bounded ephemeral host-owned revision registry and guard-owned snapshots/publication. Workspace catalogue is exactly seven tools; ordinary Agent/defaults/chat remain read-only. Saved Workspace update-read TOOL messages contain an expired revision projection without changing current model context.

## Files changed

The implementation commit changes 17 files. New production modules are `fs/replacement.py`, `fs/revisions.py`, and the two builtin tools. Narrow integration changes cover guard, native Windows helpers, exact filesystem executor context, Workspace composition/storage. Regression tests cover replacement/path checks and existing catalogue assertions. Only the new replacement architecture and necessary Workspace architecture paragraphs changed. Governance, historical filesystem docs, Browser/Gmail and global ceilings are unchanged.

## Architecture and security decisions

Registry -> validation -> current PermissionPolicy -> executor -> protected intent/action/attempt -> exact one-use context -> strict guard -> native publication. Current explicit write opt-in plus AUTONOMOUS FS_READ/FS_WRITE remain independently required. Tokens bind PID/thread/host/workspace/path and are consumed once before preparation observations; metadata/digests, never the old raw body, form the snapshot.

Complete escaped JSON plus the conversation frame must fit the current result ceiling before token issuance. Preparation and final post-staging checks compare full trusted snapshots. Durable intent contains new content once and bounded old metadata. Worst-case protected envelope is 884736 bytes below the unchanged 1 MiB ceiling.

POSIX uses exclusive same-parent stages, preserves ordinary owner/group/mode, refuses xattrs/unusual modes and fsyncs parent after rename. Windows uses parent-relative native handles and exact bounded full owner/DACL/SACL preservation. Production never enables privileges; targets that cannot be proven safe with current rights are refused. Ordinary Windows tokens lacking full descriptor access cannot use this workflow.

Only exact NEW bytes plus safe ancestry/metadata reconcile as applied. OLD, absent, different or unsafe states remain UNKNOWN/HUMAN with no retry. Shared capacity/failure latch and startup recovery are retained; authorized history is not automatically executed.

## Bugs found

The initial full-suite run exposed the structural read-only AST test's missing recognition of the new reviewed mutation module/guard dispatch. No merged-foundation P0/P1 defect was identified. Initial long-path pytest fixtures exceeded Windows path limits; shorter workspace-local scratch roots were used.

## Bugs fixed

The AST check explicitly recognizes only the new replacement mutation boundary while still scanning every ordinary guard read method. Added deterministic historical-authorized replacement refusal and handle-cleanup regressions. Production source was stable before the final full run.

## Known limitations

- Local Windows token lacks assigned SeSecurityPrivilege; replacement success/stale/native metadata workflows are explicitly skipped locally, not claimed verified. Privileged Windows CI tests exercise assigned privileges only in synthetic test fixtures and fail if implementation behavior is unavailable despite those rights.
- POSIX tests and optional real Chromium coverage require their missing exact-head CI gates.
- External noncooperating same-user changes after final comparison/during native rename, including removal, remain outside host ownership. Neither rename primitive is a perfect CAS or intrinsically replace-only syscall. Windows closes inspected target handles at that seam and provides no POSIX parent fsync guarantee.
- A crash may leave bounded inert random staging residue. Protected payloads are authenticated private plaintext, not encrypted at rest.
- No patches, append/delete/rename/copy/chmod/shell, binary/large-file editing, or Browser/Gmail mutation.

## Test results

Reported local Windows Python 3.13 results against unchanged final production source:

- Focused replacement/update-read/path suites: 163 passed, 74 skipped.
- Existing create-file/directory and Workspace Host suites: 217 passed, 12 skipped.
- Final full suite: 6449 passed, 318 skipped, 89 warnings, exit 0 (494.48 seconds). Warnings include existing Gmail fake-response destructor warnings; Gmail production was not changed.
- Compileall and staged/base-relative diff checks passed. Implementation working tree clean; local, remote and PR head match.

These counts do not substitute for missing platform/native-success proof. No tests or research were rerun for this ledger.

## CI

All six exact-head normal jobs concluded FAILURE because GitHub **did not start them**: account payment failure or Actions spending limit. No test assertion failed in these jobs; no blind rerun was attempted.

- Linux full: [run 37230515319 / job 111519000394](https://github.com/Nortan251/agentcore/actions/runs/37230515319/job/111519000394).
- Windows 3.11: [run 37230515194 / job 111519000151](https://github.com/Nortan251/agentcore/actions/runs/37230515194/job/111519000151).
- Windows 3.12: [run 37230515194 / job 111519000051](https://github.com/Nortan251/agentcore/actions/runs/37230515194/job/111519000051).
- Windows 3.13: [run 37230515194 / job 111519000195](https://github.com/Nortan251/agentcore/actions/runs/37230515194/job/111519000195).
- Chromium Linux: [run 37230515338 / job 111519000585](https://github.com/Nortan251/agentcore/actions/runs/37230515338/job/111519000585).
- Chromium Windows: [run 37230515338 / job 111519000387](https://github.com/Nortan251/agentcore/actions/runs/37230515338/job/111519000387).

## Codex review

Fresh review requested specifically for the exact head above: [request](https://github.com/Nortan251/agentcore/pull/43#issuecomment-5983862902). Connector acknowledged with an eyes reaction. Review result pending; no clean-review claim.

## Unresolved review threads

Pending final review query; absence of findings yet is not review completion.

## Blockers

GitHub Actions account billing/spending limit prevents all six required platform gates, leaving native Windows success/metadata preservation, Linux full and Chromium proof unavailable. Fresh review result remains pending.

## Next required action

Restore Actions availability, then execute the diagnosed blocked runs on this exact head. Inspect native Windows and POSIX results, repair any failures without weakening gates, rerun required final-head validation after production changes, and resolve every P0/P1/P2 review finding. Append evidence amendments here; never merge ledger into main.

## Final verdict

STOPPED at required verification. Implementation PR exists and remains open/unmerged. Not ready for project-lead approval until exact-head platform gates and review are complete.
