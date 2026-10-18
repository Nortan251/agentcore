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

## 2026-10-05 amendment — exact-head review returned two P1 findings

The preceding snapshot is preserved and superseded where it says review is pending or describes the admission rule as sufficient. Implementation head remains `cb60193466ecfb8ea0abe71b1c08ad2810bad978`; no implementation changes or test reruns followed this review.

DIRECTLY OBSERVED REVIEW FACTS: [review 5419036616](https://github.com/Nortan251/agentcore/pull/44#pullrequestreview-5419036616) was published against the exact full head SHA above. Two threads remain unresolved:

- [P1: reject idmapped mounts before trusting owner bits](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472896). Whitelisted filesystem magic does not distinguish an idmapped mount. The review identifies a different ordinary UID accessing another idmapped alias as owner of the same parent, bypassing the owner/mode admission assumptions and retaining staging-path substitution. This undermines the current production repair; no two-mount native reproduction is claimed observed locally.
- [P1: load the helper before dropping runtime access](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472904). The reviewer reports all four native worker cases failing with `ModuleNotFoundError: subprocess` when the interpreter/checkout is behind an invoking user's private ancestor. The worker drops UID before loading that runtime. These are reviewer-reported Linux failures, separate from the observed Windows counts; no native success is established.

DIAGNOSIS / INFERENCE: The filesystem-type allowlist and effective UID/mode check do not establish the promised safety across idmapped aliases. [Kernel idmapping documentation](https://cdn.kernel.org/doc/html/latest/filesystems/idmappings.html) describes per-mount ownership remapping for the same dentries. This is an additional merged-foundation namespace-authority P1, beyond the initially considered shared-write DAC configuration. Under the user's section 7 instruction to STOP on an additional distinct merged-foundation P0/P1, implementation stopped rather than silently expanding the mount-security model or publication architecture. The current repair is incomplete, not a validated fix.

REMAINING BLOCKERS: both P1s unresolved; native two-principal and two-mount proof missing; all six exact-head gates still billing-blocked before execution. Review has returned findings, so the zero-unresolved gate fails. A completed review with findings is not a clean review or a CI waiver.

NEXT REQUIRED ACTION: decide an auditable, bounded admission contract that excludes idmapped aliases (without the prohibited publication redesign), then repair that rule and the helper's runtime-loading setup; add the requested native two-mount/private-installation regressions. Any new implementation head needs appropriate focused/relevant/full validation, all six exact-head gates and fresh review. PR #43 remains unchanged and STOPPED until the independently reviewed foundation repair is merged.

CURRENT VERDICT: **STOPPED / OPEN / UNMERGED; INCOMPLETE SECURITY REPAIR; TWO UNRESOLVED P1s.** No merge, force push or thread resolution was performed.

## 2026-10-18 amendment — permitted engineering resumed; native worker imports repaired

The project lead authorized ordinary engineering work on the existing PR, preserving PR #43's separation and prohibiting merge. Earlier snapshots above remain historical. Their blanket engineering stop is superseded by this bounded follow-up; their production-security and native-validation blockers remain.

BASE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
HEAD SHA: `9f57a8edc52bbd810f9e07e63438d2eee3acda26`
PARENT SHA: `cb60193466ecfb8ea0abe71b1c08ad2810bad978`
BRANCH: `codex/fs-stage-publication-integrity-hotfix`
STATUS: OPEN / UNMERGED; ordinary follow-up complete; security repair incomplete

DIRECTLY VERIFIED PROVENANCE: [new implementation commit](https://github.com/Nortan251/agentcore/commit/9f57a8edc52bbd810f9e07e63438d2eee3acda26) is a single child of the prior audited head, with main as its ancestor. Local, remote and PR heads agree. Main remains the base above; PR #43 remains OPEN / UNMERGED at `a5f1c52447d69784847f9e75f7502e09a842c65c`. No merge, history rewrite, new PR or PR #43 implementation change.

COMPLETED IMPLEMENTATION: the worker loads Harness/AgentCore and lazy ctypes/platform/fcntl imports before changing UID. It resolves runtime paths before losing search access, while guard/journal/executor construction remains after the drop. The ordinary fresh-process regression mocks the transition and runs on Windows and Linux without credential changes. Four native private-ancestor variants copy the source/test dependencies beneath a 0700 ancestor, then require that ancestor to be inaccessible under the agent UID while retaining the original two-principal/effect assertions.

Exactly four implementation files changed: `tests/fs_stage_principal_worker.py`, `tests/test_fs_stage_parent.py`, `docs/architecture/filesystem-write-v1.md`, and the `_safe_stage_parent` docstring in `src/agentcore/fs/mutation.py` (80 insertions, 18 deletions). Executable production AST is identical to the parent after removing docstrings. The documentation now marks the idmapped-alias gap and different-principal writable-ancestor requirement explicitly. No runtime authority, schema, retry, Windows publication, Directory Create, defaults, dependencies or governance changes were made.

DIRECTLY OBSERVED LOCAL VALIDATION: Windows 11 build 26300 / AMD64 / Python 3.13.15, with the final committed tree and explicit checkout source path:

| Run | Passed | Failed | Skipped | Duration |
|---|---:|---:|---:|---|
| Intentional ordering regression before repair | 0 | 1 | 0 | 0.32s; 30 deselected |
| Ordering regression after repair | 1 | 0 | 0 | 0.61s; 30 deselected |
| Focused file-create / stage-parent | 80 | 0 | 33 | 8.92s |
| Relevant filesystem / Workspace / permissions / Task 43.2a-h | 680 | 0 | 51 | 69.61s |
| Full repository | 6429 | 0 | 293 | 467.20s; 89 warnings |
| Explicit pinned Chromium gate | 419 | 0 | 0 | 163.14s |

Focused command: `python -m pytest tests/test_fs_write.py tests/test_fs_stage_parent.py -ra`. Relevant suites additionally cover Directory Create, Workspace host/directory, permissions, and Task 43.2a-h. Full command: `python -m pytest -ra` (repository configuration supplies `-q`). All use separate short workspace temporary roots. The explicitly enabled Chromium run uses the exact twelve test modules from `browser-integration.yml`, with installed Playwright 1.62.0 / psutil 7.2.2. Compileall (`src tests`), staged/committed diff checks, changed-source review, allowed-file checks, clean status and commit/remote provenance passed. No implementation changes followed final validation.

NATIVE EVIDENCE NOT ESTABLISHED: only Python 3.13 is installed locally; WSL is not installed. All eight native worker cases, including the four new private-runtime cases, were skipped on Windows. The ordinary mocked-transition pass is not Linux, private-runtime end-to-end, different-UID, two-mount or ancestor-relocation proof. No manual privileged or mount experiments were performed. Those tests need an appropriately authorized Linux environment; Windows Python 3.11/3.12 and Linux Chromium also remain unavailable locally.

CURRENT-HEAD CI: all six automatic jobs concluded FAILURE with zero steps. Each annotation independently says the job was not started because recent account payments failed or the spending limit needs increasing. No assertions executed and no manual reruns or waiver occurred:

- Linux full: [run 39174693957 / job 117814846542](https://github.com/Nortan251/agentcore/actions/runs/39174693957/job/117814846542).
- Windows 3.11: [run 39174693912 / job 117814846559](https://github.com/Nortan251/agentcore/actions/runs/39174693912/job/117814846559).
- Windows 3.12: [run 39174693912 / job 117814846409](https://github.com/Nortan251/agentcore/actions/runs/39174693912/job/117814846409).
- Windows 3.13: [run 39174693912 / job 117814846641](https://github.com/Nortan251/agentcore/actions/runs/39174693912/job/117814846641).
- Chromium Linux: [run 39174694083 / job 117814847414](https://github.com/Nortan251/agentcore/actions/runs/39174694083/job/117814847414).
- Chromium Windows: [run 39174694083 / job 117814847082](https://github.com/Nortan251/agentcore/actions/runs/39174694083/job/117814847082).

REVIEW: [fresh exact-head Codex review requested](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6256248338); no new completed review observed at this snapshot. Both existing threads remain unresolved. The [worker finding](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472904) has a source repair and local ordering regression, but still needs native private-runtime evidence and independent review before resolution. The [idmapped-alias production P1](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472896) remains unfixed.

SOURCE REVIEW / INFERENCE: mount-local ownership/mode plus filesystem magic cannot establish global namespace exclusivity for the pathname used by `os.link`. An opened target parent also remains attached after relocation through a writable containing ancestor. No native hostile-alias or different-principal relocation reproduction was performed here. These remain production boundary requirements, not supported assumptions or additional trust exclusions. No unverified production heuristic was added.

NEXT REQUIRED ACTION: restore Actions account availability; obtain authorized native Linux execution for the existing/new two-UID private-runtime tests and the hostile-alias/ancestor requirements. Review a bounded object-bound staging/publication design with fail-closed unsupported semantics, without a pathname fallback or privileged production requirement. Complete independent review and all six gates on the eventual final head. Keep PR #43 separate until the foundation is independently approved and merged by the project lead.

CURRENT VERDICT: **ORDINARY FOLLOW-UP COMPLETE / OPEN / UNMERGED / SECURITY REPAIR INCOMPLETE / NOT MERGEABLE.** Two review threads, production security, native proof, exact-head CI and fresh review remain blockers. This ledger amendment changes documentation only and must never be merged into main.
