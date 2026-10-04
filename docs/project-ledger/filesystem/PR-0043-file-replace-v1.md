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

## 2026-10-04 amendment — P1 repairs and final-head verification

The preceding initial snapshot is preserved. The initial exact-head Codex
[review](https://github.com/Nortan251/agentcore/pull/43#pullrequestreview-5407935802)
identified two P1 defects in the new feature, not merged foundations:
[update-read argument logging](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179126041)
and [unpreserved inode flags](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179126045).

FINAL IMPLEMENTATION HEAD: `40b6024f04db899aecbbfdcd8921afb2e833ca8a`
PARENT: `09ff3643a90b0b7ceab007862bd9bdbd17be1604`
AUTHORITATIVE BASE: `547a2f00bcfa709d907c329fbea78730dd6731f6`

[Repair commit](https://github.com/Nortan251/agentcore/commit/40b6024f04db899aecbbfdcd8921afb2e833ca8a)
adds the executor's existing private-argument tag, a generic logging regression,
Linux descriptor inode/extended policy queries, refusal of unsupported policy
flags/project IDs/extent hints, stage flag comparison and a real nodump
refusal regression. Unsupported querying/filesystems/ABIs and non-Linux POSIX
fail closed. A local control reproduced the old raw-argument log and confirmed
the repaired spec removed it. Native Linux proof remains unavailable locally.
Both original threads were resolved after the repair was pushed.

Final-source local Windows Python 3.13 validation:

- Focused replacement/path/private-argument suites: **194 passed, 75 skipped**.
- Full repository suite after production stabilized: **6450 passed, 319 skipped,
  89 warnings**, exit 0, 491.72 seconds. This includes all named affected suites.
- Compileall and base-relative diff checks passed. Implementation tree clean;
  local/remote/PR SHA match. No new production changes followed validation.
- Earlier surrounding counts above remain historical, not substitute final-head
  evidence. Ordinary-token Windows and POSIX/native platform gaps remain explicit.

All six new normal jobs on this final head were again refused **before start**
for the same independently verified GitHub payment/spending-limit annotation.
The user requested recording blocked gates for now. No manual reruns occurred.

- Linux full: [run 37231833132 / job 111523044220](https://github.com/Nortan251/agentcore/actions/runs/37231833132/job/111523044220).
- Windows 3.11: [run 37231833085 / job 111523044238](https://github.com/Nortan251/agentcore/actions/runs/37231833085/job/111523044238).
- Windows 3.12: [run 37231833085 / job 111523044502](https://github.com/Nortan251/agentcore/actions/runs/37231833085/job/111523044502).
- Windows 3.13: [run 37231833085 / job 111523044544](https://github.com/Nortan251/agentcore/actions/runs/37231833085/job/111523044544).
- Chromium Ubuntu: [run 37231833128 / job 111523044096](https://github.com/Nortan251/agentcore/actions/runs/37231833128/job/111523044096).
- Chromium Windows: [run 37231833128 / job 111523044481](https://github.com/Nortan251/agentcore/actions/runs/37231833128/job/111523044481).

Fresh final-head review: [request](https://github.com/Nortan251/agentcore/pull/43#issuecomment-5984028403), acknowledged; result pending. No clean final-head review claim. Original two threads resolved; no other threads observed at last query.

CURRENT VERDICT: STOPPED at required platform verification and fresh review.
PR remains OPEN / UNMERGED. Restore Actions availability and validate this exact
head, then append native/platform/review evidence; do not merge the ledger into main.

## 2026-10-04 amendment — private staging and final residue repair

The preceding snapshots are historical and preserved. Subsequent exact-head
Codex reviews found two further P1s in the new feature:
[early partial-stage disclosure](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179197558)
at `40b6024f04db899aecbbfdcd8921afb2e833ca8a`, and
[failed Windows stage cleanup disclosure](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179290371)
at `4d3ea0278071d03e0417201609fc30810280be49`.

FINAL IMPLEMENTATION HEAD: `a5f1c52447d69784847f9e75f7502e09a842c65c`
PARENT: `4d3ea0278071d03e0417201609fc30810280be49`
AUTHORITATIVE BASE: `547a2f00bcfa709d907c329fbea78730dd6731f6`

[Private-stage repair](https://github.com/Nortan251/agentcore/commit/4d3ea0278071d03e0417201609fc30810280be49)
keeps POSIX staging at owner-private 0600 while writing, atomically publishes
complete bytes, then restores/verifies the old mode through the owned descriptor.
Restoration failure remains uncertain; unsafe metadata cannot settle as applied.
Windows replacement stages gained exclusive handles.

[Residue repair](https://github.com/Nortan251/agentcore/commit/a5f1c52447d69784847f9e75f7502e09a842c65c)
creates Windows stages with a protected current-user DACL, then sets a protected
empty DACL before any content exists. Owned-handle rights survive; later data-read
opens fail even after failed discard/close. Full original descriptor restoration
occurs after complete write/flush/final comparison with the old target pinned.
The uncertainty boundary starts before that restoration because it can disclose
complete bytes. Post-restoration failures are UNKNOWN, even before native rename.
The target handle still closes immediately before rename; the external same-user
race remains excluded rather than claimed as perfect CAS. Existing create
primitives, Gmail/Browser, governance and global budgets are unchanged.

All four historical P1 threads were resolved after their repairs were pushed.
No confirmed P0/P1 merged-foundation defect was identified.

Final exact-head Windows Python 3.13 evidence, reused without ledger test reruns:

- Focused replacement/path/private-argument suites: **195 passed, 79 skipped**
  in 2.90 seconds; 46 unavailable full-descriptor Windows cases and 33 POSIX/Linux
  cases. Native protected-empty-DACL regression passes on this ordinary token.
- Full repository suite: **6451 passed, 323 skipped, 89 warnings**, exit 0,
  529.62 seconds. Includes all named affected suites. Compileall and base-relative
  diff check passed; implementation worktree clean, local/remote/PR head match.
- An earlier full run at the staging repair had one unchanged Gmail claim-acquisition
  assertion failure. Its isolated reconciliation suite passed 38 tests, and a
  diagnostic full run passed 6450 tests with 322 skips. The final-head full run
  above also passed. The cause was not established; source/assertions were not
  changed. Scratch diagnostics trace exception categories only, without changing
  behavior. Existing Gmail fake-response warnings remain.
- Full native Windows replacement/descriptor success, Linux and Chromium platform
  proof remain unavailable; local successes do not replace these gates.

All six normal jobs at the final head were refused **before runner start** with
the independently checked annotation that account payments failed or the spending
limit needs increasing. No tests ran. The user requested recording blocked gates
for now, and no manual reruns occurred.

- Linux: [job 111532676802](https://github.com/Nortan251/agentcore/actions/runs/37235155186/job/111532676802).
- Windows 3.11: [job 111532677216](https://github.com/Nortan251/agentcore/actions/runs/37235155254/job/111532677216).
- Windows 3.12: [job 111532677185](https://github.com/Nortan251/agentcore/actions/runs/37235155254/job/111532677185).
- Windows 3.13: [job 111532677093](https://github.com/Nortan251/agentcore/actions/runs/37235155254/job/111532677093).
- Chromium Ubuntu: [job 111532677011](https://github.com/Nortan251/agentcore/actions/runs/37235155160/job/111532677011).
- Chromium Windows: [job 111532676821](https://github.com/Nortan251/agentcore/actions/runs/37235155160/job/111532676821).

Fresh exact-head [Codex review 5408261737](https://github.com/Nortan251/agentcore/pull/43#pullrequestreview-5408261737) reviewed `a5f1c52447d69784847f9e75f7502e09a842c65c` and found a fifth P1: [stage pathname substitution](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179362899). That thread remains unresolved. This supersedes the earlier no-foundation-defect observation.

CURRENT VERDICT: STOPPED at required platform verification. PR remains OPEN /
UNMERGED. Restore Actions availability and validate this exact head, then append
platform evidence; never merge ledger into main or force-push it.

## 2026-10-04 stopped-state amendment — merged foundation P1 exposed

STOPPED under the user's explicit rule to report merged-foundation P0/P1 defects
before hiding a repair inside this feature. No production edits followed the
latest review. Implementation head and parent above remain final for this stop.

The replacement finding exposes the same flaw in unchanged merged create-only
foundation code at authoritative base `547a2f00bcfa709d907c329fbea78730dd6731f6`:
[parent traversal](https://github.com/Nortan251/agentcore/blob/547a2f00bcfa709d907c329fbea78730dd6731f6/src/agentcore/fs/mutation.py#L58)
checks directory type/identity but does not exclude other-principal namespace
write authority; [publication](https://github.com/Nortan251/agentcore/blob/547a2f00bcfa709d907c329fbea78730dd6731f6/src/agentcore/fs/mutation.py#L189)
links the stage pathname after comparing its inode, and cleanup compares only
the restored stage name. A different principal with search/write access to a
non-sticky parent can rename the stage despite its private file mode, as specified
by [POSIX rename permissions](https://pubs.opengroup.org/onlinepubs/9799919799/functions/rename.html).
This is outside neither the same-user nor administrator/root host exclusion.

Deterministic sequence: move verified stage aside immediately before link;
create a substitute at its name; publish that substitute; remove its stage name
and restore the original before cleanup. Cleanup then passes and the primitive
returns applied with substitute final bytes. The unchanged
[write adapter](https://github.com/Nortan251/agentcore/blob/547a2f00bcfa709d907c329fbea78730dd6731f6/src/agentcore/tools/builtin/write_file.py#L123)
maps applied to CONFIRMED_APPLIED. An ordinary attacker-owned substitute can
allow host read/write access to avoid Linux protected-hardlink restrictions.

An exact-base control-flow reproduction confirmed both normal publication and
substitution: the normal case returned applied with intended bytes; substitution
returned applied with a different synthetic digest, a single final link and zero
recognized stage residue. Foundation source was verified unchanged. This used
actual Windows file writes/identities/hardlinks/renames/unlinks with directory-FD
resolution/fsync adaptation and delete sharing to model POSIX open-file renaming.
It is **not native POSIX or a second-principal integration test**. Linux native
proof remains unavailable; no platform success is invented.

FINAL REVIEW STATUS: five historical feature P1 findings; four repaired/resolved,
one unresolved staging-integrity P1 at final head. A merged-foundation integrity
P1 is exposed and remains unpatched. No clean final-head review or readiness claim.

FINAL VERDICT: STOPPED at the explicit merged-foundation security boundary, plus
six exact-head platform jobs blocked by Actions billing. PR remains OPEN /
UNMERGED. Required next work is a separately scoped foundation repair and native
Linux regression before resuming File Replace, then final-head local tests,
fresh review and six platform gates. Restore Actions availability for those gates.
Preserve this stop and earlier snapshots; never merge ledger into main.
