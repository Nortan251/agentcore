# PR #44 — POSIX staged-create publication integrity hotfix

AREA: filesystem
TASK: Separate merged-foundation P1 repair discovered during File Replace review
STATUS: STOPPED / OPEN / UNMERGED (2026-10-05); native Linux, CI and review required

BASE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
HEAD SHA: `cb60193466ecfb8ea0abe71b1c08ad2810bad978`
PARENT SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
BRANCH: `codex/fs-stage-publication-integrity-hotfix`

[PR](https://github.com/Nortan251/agentcore/pull/44) · [Exact commit](https://github.com/Nortan251/agentcore/commit/cb60193466ecfb8ea0abe71b1c08ad2810bad978) · [PR #43 P1 finding](https://github.com/Nortan251/agentcore/pull/43#discussion_r4179362899)

## Goal

Prevent a different ordinary principal from substituting a verified staging pathname before merged POSIX create-only publication. This is independent of PR #43, which remains unchanged, OPEN / UNMERGED and STOPPED until this foundation repair is independently merged.

## Production changes

`fs/mutation.py` checks the already-opened publication parent during execution before the physical-entry callback and any stage creation: owner UID must equal effective AgentCore UID; group/world write bits are refused. Sticky shared writable and other-UID parents are conservatively refused. Descriptor-based fstatfs limits permission semantics to local Linux ext2/3/4, XFS, Btrfs and tmpfs on 64-bit x86-64/AArch64 libc; unsupported systems, ABIs, filesystems and native inspection errors fail closed.

## Files changed

Exactly four files (448 insertions, 2 deletions): `src/agentcore/fs/mutation.py`, `tests/test_fs_stage_parent.py`, `tests/fs_stage_principal_worker.py`, `docs/architecture/filesystem-write-v1.md`. No PR #43 source, shared preparation, Windows implementation, Directory Create implementation, permission levels, journal schema, ceilings, host catalogue, Browser/Gmail or governance changes.

## Architecture and security decisions

Registry -> validation -> current PermissionPolicy -> executor -> protected intent / ActionJournal -> guard -> effect remains canonical. An unsafe parent is rejected before `execution.entered` becomes true, so it creates no stage, staged bytes or target and retains NOT_INVOKED / CONFIRMED_NOT_APPLIED. Existing post-entry uncertainty, cleanup and read-only reconciliation remain unchanged. Parent admission is host policy with no model argument or bypass.

Linux POSIX ACL named-user/group grants are bounded by the group-class mode mask; default ACLs do not authorize mutation of the parent. Unproven permission models (including NFS/CIFS/FUSE/overlay) are refused. See [acl(5)](https://man7.org/linux/man-pages/man5/acl.5.html) and [fstatfs(2)](https://man7.org/linux/man-pages/man2/statfs.2.html). Windows remains handle-bound for publication and cleanup. Direct Directory Create has no equivalent staged-file pathname publication. Same-user malicious code and administrator/root/equivalent privilege remain outside the host boundary; different ordinary principals cannot obtain the stage substitution window in supported parents. No global race-proof claim or new inode-publication architecture.

## Bugs found

The merged stage-name publication risk was reported by PR #43 review. Initial local focused setup hit a missing temporary parent; initial surrounding tests hit native Windows path limits (59 failed, 620 passed, 44 skipped). These were test-environment failures; shortening the workspace scratch path fixed the surrounding run without changing production or assertions. No separately demonstrated additional merged-foundation P0/P1 was found in the narrow Directory Create audit.

## Bugs fixed

The physical create path now refuses unsafe parent namespace authority before staging. Tests retain an adversarial publication interleave and explicitly give substituted objects read/write mode so protected_hardlinks cannot mask the vulnerable control. No destination-path check-then-use workaround is introduced.

## Known limitations

- Native Linux validation remains missing on this Windows host; WSL was rechecked and is not installed. No Linux second-principal pass or native vulnerable-base reproduction is claimed.
- The native helper is implemented for a root/noninteractive-sudo test environment: privilege only constructs two unused nonzero UIDs. Both AgentCore and the attacker drop real/effective UID/GID, supplementary groups and effective capabilities before operating. Shared-parent namespace authority is demonstrated; safe and masked-ACL parents deny substitution at the os.link seam; restrictive other-UID ownership is refused. Missing setup capability produces an explicit skip.
- POSIX support is deliberately restricted to the stated local Linux permission model. Sticky/shared write directories and unproven systems are compatibility refusals.
- Same-user/privileged permission changes and hostile ancestor relocation remain host-boundary limits. Existing no-blind-retry and crash-residue semantics remain.

## Test results

Reported local Windows 11 build 26300 / AMD64 / Python 3.13.15, final production source and final test files:

- Focused file-create/stage-parent suites: **79 passed, 29 skipped**, exit 0 (39.15s). New Linux tests account for 25 skips; the native Windows publication regression passes.
- Relevant file/directory mutation, Workspace Host/directory, permissions and Task 43.2a-h journal/executor/recovery/protected storage/approval/idempotency/corruption suites: **679 passed, 47 skipped**, exit 0 (108.83s). A later refinement touched only the locally skipped native helper; the final full suite includes all these tests.
- Final complete repository suite: **6428 passed, 289 skipped, 89 warnings**, exit 0 (565.74s). Warnings include existing Gmail fake-response destructor warnings.
- `python -m compileall -q src tests`, staged/base-relative `git diff --check`, allowed-file inspection and implementation cleanliness passed. Local, remote and PR heads match the exact commit above. No changes followed final validation.
- Native safe-parent / genuinely different-principal proof is **NOT PASSED LOCALLY**. Local Windows results do not replace it.

No implementation tests were rerun for this ledger publication.

## CI

All six normal jobs on this exact head concluded FAILURE **before execution**, with no steps. Each check annotation independently states that recent account payments failed or the Actions spending limit must be increased. No assertions ran; no manual rerun or CI waiver occurred.

- Chromium adapter (windows-latest): billing-blocked FAILURE, zero steps; [run 37356828872 / job 111921248025](https://github.com/Nortan251/agentcore/actions/runs/37356828872/job/111921248025).
- Chromium adapter (ubuntu-latest): billing-blocked FAILURE, zero steps; [run 37356828872 / job 111921248383](https://github.com/Nortan251/agentcore/actions/runs/37356828872/job/111921248383).
- Linux full suite (py3.13): billing-blocked FAILURE, zero steps; [run 37356829031 / job 111921248728](https://github.com/Nortan251/agentcore/actions/runs/37356829031/job/111921248728).
- Windows fs verification (py3.11): billing-blocked FAILURE, zero steps; [run 37356829223 / job 111921251407](https://github.com/Nortan251/agentcore/actions/runs/37356829223/job/111921251407).
- Windows fs verification (py3.12): billing-blocked FAILURE, zero steps; [run 37356829223 / job 111921251696](https://github.com/Nortan251/agentcore/actions/runs/37356829223/job/111921251696).
- Windows fs verification (py3.13): billing-blocked FAILURE, zero steps; [run 37356829223 / job 111921251858](https://github.com/Nortan251/agentcore/actions/runs/37356829223/job/111921251858).

## Codex review

Fresh independent exact-head review requested in [comment 6000686529](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6000686529), covering namespace/ACL assumptions, descriptor checks, staging order, effect truth, uncertainty/reconciliation, cleanup, native test validity, Windows and Directory Create. Review result is pending at this snapshot. An acknowledgement or request is not completed review; no clean-review or completed Codex-run claim.

## Unresolved review threads

No threads observed on PR #44 at the evidence snapshot, but review has not completed, so the zero-unresolved-P0/P1/P2 merge gate is not established. PR #43's existing staging-substitution P1 remains unresolved; this task does not resolve it or advance File Replace.

## Blockers

Native Linux safe-parent / different-ordinary-principal execution remains unverified. All six exact-head Actions gates are billing-blocked. Fresh exact-head independent Codex review is pending. No merge consideration until all required evidence exists and zero P0/P1/P2 remain.

## Next required action

Restore Actions account availability, then run all six exact-head gates and verify the native two-UID tests actually execute without skips. Complete independent review, repair any findings on a new head with appropriate fresh validation/review, and append evidence here. Keep PR #43 stopped until this repair is independently merged. Never merge this ledger branch into main or force-push it.

## Final verdict

**STOPPED / OPEN / UNMERGED.** Implementation and available local validation are complete; native security proof, CI and review gates prevent a mergeable or fully validated claim.
