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

## 2026-10-19 bounded Windows compatibility amendment

This amendment supersedes the older Actions-availability statements for this repair. Earlier snapshots remain historical. The user authorized ordinary Windows repair of PR #43 only; the separate POSIX publication-integrity P1 remains a merge blocker.

- Starting PR head: `a5f1c52447d69784847f9e75f7502e09a842c65c`.
- Final head: `7bd7b4de98fe7cff63c577c0add2be16fb2d85dd`; parent: `ca138af8972484cc521ebe7c593e5cd97db95f18`.
- Divergence base: `547a2f00bcfa709d907c329fbea78730dd6731f6`; main independently verified at `cada2362beb43019f64dcb6395f98b136e0cd46c`.
- Standard Actions temporary merge commit: `dd109afdeb3642bd5d2614f33ec4e7b43b49c172`, verified parents main `cada2362...` and final head `7bd7b4d...`. No merge into the feature branch or main was performed.
- PR #44 remains OPEN at `144d878586837409f6141c5fdc3212220e76448f`; no changes or merge were made. Its production security P1 remains unresolved.

### Cause and bounded correction

Started with the [requested real CI evidence](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6270356517). The old Windows 3.11/3.12/3.13 rerun executed and failed revision reads; these were not billing refusals. A categorical same-handle [native diagnostic](https://github.com/Nortan251/agentcore/actions/runs/39287444393/job/118178469905) established that owner/group/DACL and SACL queries succeeded, the complete BACKUP `GetSecurityInfo` query returned WinError 5, and the complete BACKUP `GetKernelObjectSecurity` query succeeded on the unchanged handle. This identifies an API compatibility defect, not missing runner privilege.

The shared full-descriptor getter now uses `GetKernelObjectSecurity(handle, 0x10000)` under the same already-granted native handle rights. Production enables no privilege. Unavailable/disabled `SeSecurityPrivilege` still causes categorical refusal before a revision, action or stage exists. No partial descriptor is accepted.

Once reads ran, [native metadata diagnostics](https://github.com/Nortan251/agentcore/actions/runs/39293078436/job/118198469426) found the unconditional SACL setter changed an absent SACL into a present NULL SACL: expected control `0x8804`, observed `0x8014`, while attributes and owner/group/DACL/SACL contents otherwise matched. The setter now requests SACL restoration only when the complete source descriptor has `SE_SACL_PRESENT`; exact full metadata equality remains mandatory. An experimental setter API change at `3485b75...` failed native validation and was reverted at `0caccff...`; it is absent from the final diff.

Fresh review identified [an inherited-parent SACL P2](https://github.com/Nortan251/agentcore/pull/43#discussion_r4316850301). A new early stage check refuses an absent-source SACL whose presence/defaulting/inheritance/protection control differs after creation, before any content write or publication callback. The native regression sets a real inheritable parent audit SACL without propagating it to the old source; it verifies that the inherited stage is refused, no write occurs, cleanup succeeds, the old bytes/full metadata remain unchanged and the attempt is NOT_INVOKED rather than RECONCILE_REQUIRED. No parent auditing or access check is bypassed.

Three files differ from the supplied starting head:

- `src/agentcore/fs/_windows_mutation.py`: full getter compatibility, absent-SACL setter correction and early unsupported-stage refusal.
- `tests/test_fs_replace.py`: native read/stage regressions, unavailable/disabled-privilege refusal and inherited-parent regression. Existing privacy test now checks both error 32 on initial exclusive sharing and error 5 after the protected empty DACL is locked. The stale-change fixture writes after stage flush but before Windows pins the old target against other writers; original byte/stale/journal assertions remain.
- `tests/test_workspace_host.py`: explicit LF fixture creation, preserving existing exact byte assertions on Windows.

No dependencies, workflows, governance, POSIX implementation, authority, publication or recovery rules changed. Temporary detailed probes were removed; retained failure diagnostics expose only exception category, WinError and function/line locations.

### Exact-head validation

Local Windows Python 3.13 at the final head:

- Focused: **25 passed, 62 skipped**, 1.53 seconds.
- Surrounding filesystem/workspace/read/list suites: **621 passed, 203 skipped**, 75.04 seconds.
- Full repository: **6452 passed, 326 skipped, 89 warnings**, exit 0, 535.09 seconds. Existing Gmail fake-response warnings remain; no warning suppression was added.
- `python -m compileall -q src tests` and starting-head-relative `git diff --check` passed. Implementation checkout was clean; local, remote and PR head matched.

The local ordinary Windows token lacks assigned `SeSecurityPrivilege`: native positive replacement/SACL cases skip explicitly; the unavailable-permission refusal runs. Local results do not stand in for privileged native Windows or Linux evidence. A long scratch basetemp caused Windows path-length setup errors in an earlier diagnostic run; using a short task-local basetemp fixed setup without changing assertions.

All six gates ran for final PR head `7bd7b4de98fe7cff63c577c0add2be16fb2d85dd` on the standard temporary merge ref above (attempt 1). Workflow head provenance and checkout logs were independently checked; no workflow/assertion was weakened.

| Gate | Result | Exact job |
| --- | --- | --- |
| Linux full Python 3.13 | SUCCESS | [118284233689](https://github.com/Nortan251/agentcore/actions/runs/39317955305/job/118284233689) |
| Windows Python 3.11 | SUCCESS | [118284234012](https://github.com/Nortan251/agentcore/actions/runs/39317955212/job/118284234012) |
| Windows Python 3.12 | SUCCESS | [118284234501](https://github.com/Nortan251/agentcore/actions/runs/39317955212/job/118284234501) |
| Windows Python 3.13 | SUCCESS | [118284234278](https://github.com/Nortan251/agentcore/actions/runs/39317955212/job/118284234278) |
| Chromium Ubuntu | SUCCESS | [118284233101](https://github.com/Nortan251/agentcore/actions/runs/39317955144/job/118284233101) |
| Chromium Windows | SUCCESS | [118284232456](https://github.com/Nortan251/agentcore/actions/runs/39317955144/job/118284232456) |

Native Windows skip summaries were inspected: the revision/stage, privilege-denial, audit-SACL preservation and inherited-parent regressions executed; only the named POSIX/Linux cases in the replacement suite skipped. Full suite jobs passed; no CI total is inferred from quiet output.

### Review, blockers and next action

Fresh exact-head review was requested in [comment 6276388728](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6276388728). The [review receipt 6276435855](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6276435855) explicitly reviewed `7bd7b4de98` and reported no major issues. This is no-new-findings evidence, not an overall clean-PR claim. After the repairing commit, all three native Windows gates and exact-head review passed, only the inherited-SACL P2 thread was resolved. The final thread inventory contains four historically resolved feature P1s, the resolved Windows P2 and one unresolved POSIX P1.

The [existing POSIX publication-integrity P1](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179362899) remains unresolved. Green Windows tests cannot settle that finding. PR #44's separate production P1 is also unresolved; no repair of it is bundled here. The restricted security-work boundary was respected; no restricted-service access or alternate route around access controls was used.

FINAL VERDICT: the bounded Windows compatibility repair is complete and validated; PR #43 remains OPEN / UNMERGED and NOT READY TO MERGE because the separate POSIX publication-integrity P1 remains. No security-boundary expansion, separate adversarial native Linux repair/validation, PR #44 modification or merge was performed. Next work requires an explicitly scoped foundation security repair with suitable native Linux evidence, followed by fresh review and gates on any new implementation head. This ledger branch remains documentation-only and must never be merged into main.

## 2026-10-20 final integration and POSIX replacement containment amendment

This supersedes the earlier PR #43 foundation-blocked status. PR #44 is merged at main `de8432528042e5775a83d2091849c0a87cacf67c`, with reviewed implementation head `f5d7e3bd3a21f49ee64affda61dbc520382c3907`. PR #43 remains Draft / OPEN / UNMERGED; no merge is authorized.

BASE SHA (starting main): `de8432528042e5775a83d2091849c0a87cacf67c`.
STARTING PR HEAD: `7bd7b4de98fe7cff63c577c0add2be16fb2d85dd`.
HISTORICAL DIVERGENCE: `547a2f00bcfa709d907c329fbea78730dd6731f6`.
HEAD SHA: `64433667360f657fa64b62613bee3f4c95d97f36`.
PARENT SHA / NORMAL MERGE: `fc092daf973de45563b17f7678b39de8a75a80c3`; its parents are starting PR head and starting main, respectively.
BRANCH: `codex/file-replace-v1`.

### Goal, integration and production decision

A normal merge preserves audited history and integrates the merged PR #44 foundation. The sole textual conflict was the executor import block; both revision validation and foundation path rejection types are retained. The combined control flow preserves replacement authority checks and the foundation's pre-entry unsupported-publication reporting. The authoritative creation primitive, path rejection enum and all governance/workflow files match main; no removed POSIX staging/ownership mechanism was restored.

The original P1 checked a stage inode then resolved a mutable pathname in `os.replace`, allowing false confirmed success with a substituted object. The POSIX stage/rename/metadata implementation is removed. PR #44's native Windows guard now protects update snapshots, snapshot validation and replacement before traversal or staging. Unsupported update-read issues no revision; ordinary reads and Directory Create retain their existing contracts. No Issue #46 implementation or new POSIX publication experiment was added.

Milestone production files/functions: `fs/replacement.py` (`snapshot`, `check_snapshot`, `replace`, `observe`, Windows-only helpers); `tools/builtin/read_file_for_update.py` (`__init__`, `run` catalogue/refusal); `tools/builtin/replace_file.py` (`__init__` catalogue); `tools/executor.py` (merged imports and `_FilesystemPublicationUnsupported` summary); `workspace_host/composition.py` (platform prompt). Existing native Windows, revision-registry, execution-context, guard-delegation and expired-token-storage source remains unchanged from starting PR head. Tests: `test_fs_replace.py`, new `test_fs_replace_support.py`, `test_workspace_replace.py`. Docs: replacement and Workspace architecture.

Windows retains full descriptor/ACL/SACL and metadata equality, bounded complete reads, current-turn trusted revisions, stale checks, native owned-stage publication, private zero-sharing handles, target/ancestor protection, current permission/approval/claim leases and cleanup/recovery. Production enables no privilege; already-granted full-security access is still required. The Windows publication/cleanup body is AST-identical to starting PR head; native CI provides runtime proof, not that structural check alone.

### Durable truth and original finding

Unsupported update-read admits no action. An already-admitted unsupported replacement records NOT_INVOKED / CONFIRMED_NOT_APPLIED with `filesystem.unsupported_publication`, no stage and unchanged target. Possible physical entry is checked first: even a support-category post-entry exception remains uncertain. Legacy POSIX uncertain replacement remains OUTCOME_UNKNOWN / HUMAN_CONFIRMATION_REQUIRED for NEW, OLD and absent states, without retry or fabricated success. Protected schemas/evidence are unchanged.

The new unsupported-boundary regression failed at snapshot traversal on the integrated pre-repair merge, then passed after containment. Tests distinguish local platform-identity simulation from native Linux execution and synthetic historical journal attempts from kernel publication proof. Native Windows regressions add final owned-stage identity, live stage/ancestor rename denial and post-close handle release, permissions/approval, cancellation/timeout and post-restoration failed cleanup. Existing native security/privacy assertions remain.

### Validation and CI

- Local focused source checks: 58 passed / 69 skipped.
- Local surrounding filesystem/Workspace/permission/durable suites: 1018 passed / 196 skipped, 63.35 seconds.
- Local final-head full suite: **6529 passed, 357 skipped, 89 warnings**, exit 0 in 431.73 seconds.
- Compileall, committed `HEAD^ HEAD` whitespace, starting-head diff whitespace and clean implementation status: passed.
- Local ordinary Windows token lacks assigned SeSecurityPrivilege; positive privileged replacement cases skip explicitly and require native CI. Other local skips are POSIX/Linux/fork-specific.
- Native Linux: stage-parent 26 passed / 2 Windows-only skips, including 10/10 genuine distinct-UID foundation cases; surrounding 1031 passed / 183 skipped / 1 warning; full 6610 passed / 276 skipped / 98 warnings, 145.29 seconds. Windows-positive replacement tests skip explicitly on unsupported Linux; refusal tests execute.

All jobs report exact PR head above. The standard PR merge checkout is `2b1ba9dc0c743283e9e79bf7246434cffb8cbdb7`, parents verified main and exact PR head; Linux logs confirm checkout. No stale-head gate is counted. All three native Windows logs confirm the exact merge checkout and exactly 10 POSIX-only replacement skips; native positive snapshot/stage/ACL/SACL/identity/approval/cancellation/cleanup and Workspace replacement cases ran. Quiet output is not used to invent suite totals.

| Gate | Conclusion | Exact job |
| --- | --- | --- |
| Linux Python 3.13 | SUCCESS | [118488465856](https://github.com/Nortan251/agentcore/actions/runs/39380556980/job/118488465856) |
| Windows Python 3.11 | SUCCESS | [118488466376](https://github.com/Nortan251/agentcore/actions/runs/39380556790/job/118488466376) |
| Windows Python 3.12 | SUCCESS | [118488466191](https://github.com/Nortan251/agentcore/actions/runs/39380556790/job/118488466191) |
| Windows Python 3.13 | SUCCESS | [118488466576](https://github.com/Nortan251/agentcore/actions/runs/39380556790/job/118488466576) |
| Chromium Ubuntu | SUCCESS | [118488465457](https://github.com/Nortan251/agentcore/actions/runs/39380556794/job/118488465457) |
| Chromium Windows | SUCCESS | [118488465262](https://github.com/Nortan251/agentcore/actions/runs/39380556794/job/118488465262) |

### Review, limitations and next action

Fresh independent review was [requested with the full final SHA](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6284874474), explicitly including original P1 discussion_r4179362899. [Receipt](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6284905376) reports no major issues; the [machine-readable summary](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6272463554) identifies the complete final SHA, completed status and no new findings. This is not automatic original-P1 closure.

The original vulnerability's POSIX publication operation is absent and unsupported hosts stop before staging. The original vulnerability is unreachable under the documented supported scope: `snapshot` and `replace` call the shared platform guard before any traversal/staging, and no POSIX stage or pathname publication code remains. Native Linux refusal/legacy tests, all three native Windows suites and exact-head independent review now pass. Recommend closing the original P1 for this restricted design after project-lead assessment; its formal thread remains OPEN and was not auto-resolved. This is not certification of a future POSIX writer. Historical resolved threads were not reopened and no thread was automatically resolved. Next action is independent project-lead assessment of the contained original P1 and documented compatibility tradeoff; retain Draft and do not merge.

Limitations: no POSIX/macOS writer; no native macOS or hostile idmapped-mount execution claimed. Refusal does not depend on filesystem/mount ownership heuristics. Windows retains its documented external same-user target-close/native-rename race and full-security-access availability requirement; no stronger filesystem CAS or directory-fsync promise. Directory/read contracts are regression-tested, not newly certified against every namespace attack. No restricted-service workaround was attempted.

FINAL VERDICT: **READY FOR INDEPENDENT REVIEW** of the bounded Windows-supported/POSIX-refusal milestone; six exact-head gates and fresh exact-head code review passed. Original-P1 formal closure and any release/merge remain project-lead decisions. PR stays Draft and unmerged. The independent ledger branch remains documentation-only and must never be merged into main.

## 2026-10-20 amendment — controlled release BLOCKED by new exact-head P1

All earlier snapshots remain historical. The [project engineering release approval](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6289122793) and this release task authorized merging PR #43 only after live checks, with a stop condition for changed commits or a new P0/P1. No implementation repair, other PR merge or Issue #46 work was authorized.

STATUS: **BLOCKED / DRAFT / OPEN / UNMERGED**
EXACT REVIEWED PR HEAD: `64433667360f657fa64b62613bee3f4c95d97f36`
PR HEAD PARENT: `fc092daf973de45563b17f7678b39de8a75a80c3`
STARTING AND FINAL MAIN: `de8432528042e5775a83d2091849c0a87cacf67c`
PR #43 RELEASE MERGE SHA / PARENTS: none; no merge request was issued
PRE-MERGE CI INTEGRATION COMMIT: `2b1ba9dc0c743283e9e79bf7246434cffb8cbdb7`
INTEGRATION AND PR HEAD TREE: `95a847f0ce988ef958846bacef8799b9d1d716de`

### Live verification and stopped release

Before changing PR metadata, live GitHub confirmed the approved head and main were unchanged, PR #43 was Draft/open/unmerged and CLEAN, all six exact-head CI jobs succeeded, all six historical review threads were resolved, and the existing exact-head code review was completed without findings. PR #44 was already merged as starting main. The release approval was read and rechecked unchanged. AGENTS.md, current PR description, security threads, architecture, native runner logs and this independent ledger were read. No workflow or governance difference exists between current main and this PR.

All six PR job checkout logs identify the integration commit above. Its ordered parents are starting main and the reviewed PR head; its tree is identical to the reviewed head. Windows logs show the native replacement/Workspace cases were not skipped: the only replacement skips are 10 POSIX cases per Windows job. Native Linux logs show stage-parent 26 passed / 2 Windows-only skips, surrounding 1031 passed / 183 skipped / 1 warning, and full 6610 passed / 276 skipped / 98 warnings; all ten genuine distinct-UID foundation scenarios ran. Both Chromium jobs passed 419 tests. Windows junction/security/full-suite steps succeeded on all three versions; aggregate full-suite counts are suppressed by existing quiet flags and are not inferred.

At **2026-10-20 11:03:32 UTC**, the passing preflight allowed the authorized ready transition. That transition automatically started another code review on the same full head. The immediately repeated release gate detected the review was running and stopped before any merge API request. No pending review was treated as a pass.

At **2026-10-20 11:06:52 UTC**, [review 5584173610](https://github.com/Nortan251/agentcore/pull/43#pullrequestreview-5584173610) completed against the unchanged full head with a new **P1**, [discussion_r4324256953](https://github.com/Nortan251/agentcore/pull/43#discussion_r4324256953). The [machine-readable review summary](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6272463554) now reports completed with findings and unresolved findings. The release stopped, and PR #43 was restored to Draft at **11:07:51 UTC**. Final live GitHub reads confirm merged=false, merged_at=null, unchanged head/main, CLEAN mergeability, seven total review threads with the new P1 unresolved and the original six resolved. CLEAN and green CI do not override the security blocker.

The PR API's `merge_commit_sha` while unmerged is the synthetic integration commit above, not an actual release merge. No new main commit or PR #43 merge parents exist. Existing main remains the PR #44 merge, with ordered parents `cada2362beb43019f64dcb6395f98b136e0cd46c` and `f5d7e3bd3a21f49ee64affda61dbc520382c3907`, tree `a8b4af436ab5c0088cbcf21faafe791561a44396`.

### New blocking security finding

The reviewer found that `replacement.observe()` compares current workspace, ancestry, metadata, size and content digest without excluding the original target identity. When new content equals old bytes, an exception after the possible-publication callback but before native rename can leave the original target unchanged while reconciliation nevertheless reports success. This violates durable effect truth: current matching bytes do not prove that this uncertain replacement occurred. The reviewed predicate was reread and matches that concern. This task did not perform a new native exploit reproduction, implement a repair, add a regression, resolve the thread or weaken its priority.

The six earlier findings remain resolved, including the original POSIX mutable-stage finding under the approved Windows-supported/POSIX-refusal scope. That closure does not close this distinct new reconciliation P1. The prior release approval and earlier no-findings review cannot override the later exact-head result.

### Six CI conclusions and local checks

These are **pre-merge PR results**, attributed to the full reviewed head and verified integration checkout. There is no PR #43 post-merge run because PR #43 was not merged. Historical CI on unchanged main is not relabeled as post-merge release evidence.

| Required gate | PR conclusion / verified job | PR #43 post-merge conclusion |
|---|---|---|
| Linux full suite (py3.13) | SUCCESS — [118488465856](https://github.com/Nortan251/agentcore/actions/runs/39380556980/job/118488465856) | NOT RUN — release not merged |
| Windows fs verification (py3.11) | SUCCESS — [118488466376](https://github.com/Nortan251/agentcore/actions/runs/39380556790/job/118488466376) | NOT RUN — release not merged |
| Windows fs verification (py3.12) | SUCCESS — [118488466191](https://github.com/Nortan251/agentcore/actions/runs/39380556790/job/118488466191) | NOT RUN — release not merged |
| Windows fs verification (py3.13) | SUCCESS — [118488466576](https://github.com/Nortan251/agentcore/actions/runs/39380556790/job/118488466576) | NOT RUN — release not merged |
| Chromium adapter (ubuntu-latest) | SUCCESS — [118488465457](https://github.com/Nortan251/agentcore/actions/runs/39380556794/job/118488465457) | NOT RUN — release not merged |
| Chromium adapter (windows-latest) | SUCCESS — [118488465262](https://github.com/Nortan251/agentcore/actions/runs/39380556794/job/118488465262) | NOT RUN — release not merged |

Fresh local Windows Python 3.13 checks in the clean exact-head release checkout:

- Replacement/support/Workspace replacement/publication support smoke: **58 passed / 69 skipped**, 5.27 seconds.
- Surrounding filesystem/Workspace/permission/durable-execution suites, already running when the new review arrived: **1018 passed / 196 skipped**, 55.04 seconds.
- `python -m compileall -q src tests`, full-PR committed-diff whitespace check and implementation clean status: passed.
- Local token lacks assigned SeSecurityPrivilege; positive privileged replacement skips are explicit. Native Windows CI supplies positive-path evidence. Local Windows skips are not native Linux/macOS proof.
- Post-merge smoke: **NOT RUN**, because no PR #43 merge exists. No local full-suite rerun or separate security scan is claimed; exact-head native full CI was verified above.

### Capabilities, limitations and next action

Current main retains PR #44 native Windows create-only `write_file`, ordinary filesystem reads and Directory Create under their existing contracts. Unmerged PR #43 retains native Windows update-read/replacement with exact revision binding, complete security-descriptor/metadata eligibility and already-granted security access. It remains blocked from release by the new reconciliation P1. Ordinary tokens without full descriptor access refuse safely; production does not enable privileges. Linux/macOS/other unsupported platforms refuse file publication and, in the candidate, replacement/update-read before traversal/staging/publication. Ordinary reads and Directory Create retain their separate contracts; ordinary Agent defaults remain read-only.

No POSIX replacement support, native macOS validation, hostile two-idmapped-mount proof or stronger Windows filesystem CAS is claimed. The documented Windows same-user target-close/native-rename seam and durability/rights limits remain. [Issue #46](https://github.com/Nortan251/agentcore/issues/46) was not implemented. No production source, implementation branch, main, workflow, governance or other PR was changed. Only this existing record and its single INDEX row are updated; all historical record content is preserved. The independent documentation-only ledger must never merge into main.

Next required action is a separately scoped repair and deterministic regression for the identical-content/pre-rename uncertain-outcome case, followed by exact-head focused/relevant/full/native CI evidence, fresh independent review, security-thread closure and a new release-gate assessment. No such repair or further release action is performed in this milestone.

FINAL VERDICT: **BLOCKED** — new unresolved exact-head P1 4324256953; PR #43 restored to Draft and unmerged; main unchanged; no post-merge verification claimed.

## 2026-10-20 amendment — durable replacement reconciliation P1 repair

An interrupted replacement cannot be proven from current file contents, metadata or native identity. An untouched original can already contain the requested bytes; even a different matching host-owned file can have been moved into place by another principal with parent namespace rights. `replacement.observe()` now retains uncertainty because protected intent has no action-specific published-stage identity or completion receipt. It cannot promote ambiguous history to success or fabricate confirmed non-application/retry permission.

**PR #43 remains Draft / OPEN / UNMERGED. No ready transition, merge, auto-merge or main modification is authorized. Both [original P1 4324256953](https://github.com/Nortan251/agentcore/pull/43#discussion_r4324256953) and [follow-up P1 4327061408](https://github.com/Nortan251/agentcore/pull/43#discussion_r4327061408) remain formally open for explicit assessment.**

- Starting reviewed head: `64433667360f657fa64b62613bee3f4c95d97f36`.
- Final repair head: `4fb1b32c255ab07e37f1f6d8f4f1ce67eb4c6bdd`; direct parent: `ac93ee6ea67d23214b0bc17b6ee169ccae3eecda`.
- Unchanged main / merged PR #44: `de8432528042e5775a83d2091849c0a87cacf67c`.
- Standard PR integration checkout: `5f7457672e9e628dec88f3066b938c0b90e01574`, ordered parents unchanged main and final repair head. Its tree `956e89b49b2aef4f2f29e17c01a44a36b7af539c` equals the repair tree. All six job checkout logs were verified; this synthetic merge is not a release merge.

The first identity-exclusion candidate `ac93ee6` blocked unchanged originals but was insufficient. [Independent review](https://github.com/Nortan251/agentcore/pull/43#pullrequestreview-5587714301) correctly identified matching-sibling substitution by a different principal. The final correction removes optimistic recovery inference entirely, without rewriting that history or broadening concurrency exclusions.

Successful ordinary Windows replacement still uses the owned native stage/rename/flush path and confirms success, including identical-content replacement. A genuine post-publication interruption without durable success now remains OUTCOME_UNKNOWN / HUMAN_CONFIRMATION_REQUIRED. This is an explicit recovery availability tradeoff until authenticated action-specific publication evidence is designed separately. Current matching bytes, a changed identity and current-token ownership are insufficient historical proof; exclusive AgentCore host ownership does not serialize other principals' namespace operations. No stronger filesystem CAS or close-to-rename guarantee is claimed.

`observe` is the sole changed runtime function relative to the starting head. Native APIs, snapshot/revision binding, metadata/ACL/SACL/attributes, stage privacy/cleanup, executor/permission/approval checks, protected intent and journal schemas remain unchanged. Reads, file creation, Directory Create, POSIX fail-closed restrictions and legacy uncertainty retain their implementation. No Issue #46 or governance/workflow work is included.

Changed files: `src/agentcore/fs/replacement.py` (`observe`); `tests/test_fs_replace.py` (native execution/recovery and uncertain-publication regressions); `tests/test_fs_replace_support.py` (synthetic provenance/durable regression); `tests/test_workspace_replace.py` (startup recovery); `docs/architecture/filesystem-replace-v1.md` (recovery evidence contract).

Validation:

- Original reviewed runtime: **2 failed / 2 passed** in the synthetic predicate/durable regression, including false success for identical bytes on the original identity. The expanded provenance regression on `ac93ee6` also produced **2 failed / 2 passed**, exposing false success for changed identity. All four pass after the final correction. Synthetic facts are not native OS proof.
- Final local focused: **62 passed / 81 skipped**. Surrounding filesystem/Workspace/permissions/recovery/ActionJournal: **1105 passed / 208 skipped**.
- Full local Windows Python 3.13: **6533 passed / 369 skipped / 89 warnings**, exit 0 in 459.28 seconds.
- Compileall, committed `HEAD^ HEAD` and starting-head-relative whitespace, AST scope and clean-status checks pass.
- Ten native Windows tests cover identical/different bytes with marker-before-restoration failure, pre-rename failure, real matching-sibling substitution after failure, real rename followed by lost return, and normal success. They assert identity/metadata/content, exact HCR/unknown without terminal success/no-effect after uncertainty, normal confirmed success, cleanup and no second physical replacement. Sibling interference uses native filesystem I/O under controlled injection; it is not claimed execution as a distinct Windows user.
- Workspace startup blocks ambiguous old/new history, including identical bytes, before model calls or retry. Existing native metadata/ACL/SACL/privacy/stale/authority assertions remain. Positive recovery expectations were replaced with exact HCR/unknown assertions because current-state evidence does not identify this action's publication; ordinary successful execution assertions remain.
- Local privileged replacement skips are explicit because this ordinary token lacks assigned SeSecurityPrivilege. Native platform evidence is below. Initial surrounding path-length failures were corrected with a short test directory without source/assertion changes.

Commands: focused `python -m pytest tests/test_fs_replace.py tests/test_fs_replace_support.py tests/test_workspace_replace.py tests/test_fs_publication_support.py -ra -o addopts= --basetemp=../p2`; surrounding `python -m pytest` over filesystem, Workspace, permissions, recovery, ToolExecutor and Task 43 suites with `-ra -o addopts= --basetemp=../r2`; full `python -m pytest -ra -o addopts= --basetemp=../u2`; `python -m compileall -q src tests`; `git diff --check HEAD^ HEAD`; `git diff --check 64433667360f657fa64b62613bee3f4c95d97f36 HEAD`. Initial oversized-basetemp run was 62 failed / 1043 passed / 206 skipped; the complete short-path reruns pass without test weakening.

| Required final-head gate | Result | Exact job |
|---|---|---|
| Linux full suite (py3.13) | SUCCESS (attempt 2) | [job](https://github.com/Nortan251/agentcore/actions/runs/39452798963/job/118733387640) |
| Windows fs verification (py3.11) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39452798998/job/118731776360) |
| Windows fs verification (py3.12) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39452798998/job/118731776273) |
| Windows fs verification (py3.13) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39452798998/job/118731776270) |
| Chromium adapter (ubuntu-latest) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39452798954/job/118731769121) |
| Chromium adapter (windows-latest) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39452798954/job/118731768737) |


Native Linux attempt 2: stage-parent **26 passed / 2 Windows-only skips**, including **10/10 genuine distinct-UID scenarios**; surrounding **1035 passed / 195 skipped / 1 warning**; full **6614 passed / 288 skipped / 98 warnings**, 165.16 seconds. Compileall and committed whitespace passed. Attempt 1 failed an unchanged fork-test `waitpid(WNOHANG)` timing assertion after the child had sent the expected rejection and EOF (1 failed / 1034 passed / 195 skipped); no replacement assertion failed. The unchanged exact-head rerun passed; the failure is retained as an unrelated test-timing limitation, not hidden or repaired here.

All three native Windows jobs passed their security and full suites. Their replacement skip summaries contain only the existing 10 POSIX-only cases: the ten new native cases and privileged snapshot/stage/ACL/SACL/privacy/ownership/approval/cancellation/cleanup and Workspace replacement cases did not skip. Aggregate Windows full-suite totals are suppressed by existing quiet flags and are not invented. Both Chromium jobs passed **419 tests**. Linux evidence validates refusal/legacy uncertainty and the existing foundation, not supported POSIX replacement. No native macOS or distinct-principal Windows execution is claimed.

[Fresh independent review requested](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6295108020) on the full final SHA, explicitly asking to reassess both P1s. [Final-head receipt](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6295154862) reports no major issues; the [machine-readable summary](https://github.com/Nortan251/agentcore/pull/43#issuecomment-6272463554) identifies the full final SHA, completed status and no new findings. This is code-review evidence, not a separate security-scan claim or formal finding closure. Final thread inventory: **8 total, 6 earlier resolved, 2 P1 threads unresolved**; neither original P1 4324256953 nor provenance follow-up 4327061408 was resolved by this task.

No no-new-findings review automatically closes either P1. The earlier six resolved findings retain their resolutions. The independent ledger remains documentation-only and must never merge into main.

Remaining limits: ambiguous replacement recovery requires human confirmation; existing Windows close-to-rename, directory-flush/power-loss and full-descriptor-access limits remain. No POSIX/macOS writer, native macOS verification or stronger filesystem CAS is claimed. **READY FOR INDEPENDENT REVIEW** of this bounded repair; retain Draft/unmerged and the explicit release hold. Any finding closure or release decision remains separate.
