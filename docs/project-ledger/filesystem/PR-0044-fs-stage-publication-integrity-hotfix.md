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

## 2026-10-19 amendment — native foreign-directory fixture repaired and validated

Earlier snapshots are preserved. Their missing/billing-blocked native evidence is superseded by the executed CI below; their production publication-integrity warning remains in force. This is a bounded test-harness repair, not a production security repair or merge authorization.

BASE / DIVERGENCE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
LIVE MAIN SHA: `cada2362beb43019f64dcb6395f98b136e0cd46c` (PR #45 merged)
HEAD SHA: `144d878586837409f6141c5fdc3212220e76448f`
PARENT SHA: `1fa53ee9a79f3828372ab142afc43e82e650f5e0`
FIXTURE REPAIR SHA: `1fa53ee9a79f3828372ab142afc43e82e650f5e0`, parent `9f57a8edc52bbd810f9e07e63438d2eee3acda26`
BRANCH: `codex/fs-stage-publication-integrity-hotfix`
STATUS: OPEN / UNMERGED / BOUNDED HARNESS REPAIR COMPLETE / PRODUCTION P1 UNRESOLVED

DIRECTLY VERIFIED FAILURE BEFORE REPAIR: the public Actions rerun [39174693957, attempt 2 / job 118130111973](https://github.com/Nortan251/agentcore/actions/runs/39174693957/job/118130111973), described in [comment 6270320916](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6270320916), executed Linux tests and failed both `foreign-checkout` and `foreign-private-runtime`. At worker line 109, the already-dropped agent attempted to rename an attacker-owned 0755 directory into the workspace and received `PermissionError`. The attacker rendezvous then closed. These were fixture-preparation failures, not successful security assertions or production-exploit evidence. The other five rerun jobs succeeded; the old Linux failure is preserved as historical evidence.

COMPLETED REPAIR: privileged preparation now creates agent-owned `host/workspace` ancestors and puts the attacker-owned 0755 `foreign` directory directly at its final workspace path before forking/dropping credentials. The post-drop rename is removed, and a post-drop assertion confirms the foreign owner remains the distinct attacker UID. No Harness, guard, journal, executor or tool execution is moved before `drop(uid)`. Real/effective UID/GID checks, supplementary-group clearing, zero effective capabilities, private-runtime access denial, socket publication seam, byte/entry assertions and ActionJournal effect-truth assertions all remain. Setup exceptions still cause nonzero worker/test exit; no failure is converted into a skip or success.

Exactly two files differ from the supplied starting head: `tests/fs_stage_principal_worker.py` and `.github/workflows/filesystem-linux.yml`. The existing Linux job now runs focused native regressions, the surrounding filesystem/Workspace/permissions/Task 43.2 suites, the unchanged full-suite gate, compileall and a committed-diff whitespace check. Full-suite summary counts are made visible by overriding duplicate quiet options. CI permissions/triggers and production source are unchanged. The workflow governance changes were explicitly included in both fresh review requests. No new dependency, PR, history rewrite, PR #43 modification, publication primitive, mutation authority or assertion weakening was introduced.

REVIEW FOLLOW-UP: [review 5573716207](https://github.com/Nortan251/agentcore/pull/44#pullrequestreview-5573716207) on intermediate head `1fa53ee9a79f3828372ab142afc43e82e650f5e0` found [P2: compare committed revisions in the whitespace check](https://github.com/Nortan251/agentcore/pull/44#discussion_r4315526400). A clean Actions checkout makes revision-less `git diff --check` ineffective. Final commit `144d878586837409f6141c5fdc3212220e76448f` fetches depth 2 and uses `git diff --check HEAD^ HEAD`: PR synthetic merge versus its main first parent, or pushed main tip versus its first parent. An isolated deterministic Git fixture proved that committed trailing whitespace passes the old command but fails the new command for both linear and PR-merge shapes. The new P2 thread was resolved only after the repairing commit, successful final-head Linux step and fresh exact-head review existed. Both original P1 threads remain unresolved.

DIRECTLY OBSERVED TEST RESULTS:

| Environment / run | Passed | Failed | Skipped | Warnings / duration |
|---|---:|---:|---:|---|
| Final-head native Linux stage-parent focused | 30 | 0 | 1 | 5.22s; only native-Windows publication skipped |
| Final-head native Linux surrounding suite | 1024 | 0 | 67 | 1 warning; 30.94s |
| Final-head native Linux full repository | 6603 | 0 | 160 | 98 warnings; 177.87s |
| Local Windows 11 / Python 3.13.15 focused | 80 | 0 | 33 | 8.52s |
| Local Windows surrounding suite | 959 | 0 | 132 | 60.40s |
| Local Windows full repository | 6429 | 0 | 293 | 89 warnings; 482.65s |

Native focused command: `python -m pytest -o addopts= tests/test_fs_stage_parent.py -v -rs`. Surrounding command: `python -m pytest -o addopts= -q -ra tests/test_fs_*.py tests/test_workspace*.py tests/test_permissions*.py tests/test_task_43_2*.py`. Full: `python -m pytest -o addopts= -q -ra`. Compileall and `git diff --check HEAD^ HEAD` passed in the final Linux job. Local compileall and committed bounded-repair/full-PR diff checks also passed; local/remote implementation trees are clean.

All **eight real distinct-ordinary-UID cases PASSED with zero native setup skips**, covering shared, safe, masked ACL and foreign ownership in both checkout and unreadable-private-runtime variants. Both previously failing foreign cases reached their post-drop tool/result assertions. Native full-suite skips consist of 74 Windows-specific cases and 86 explicit Chromium-gate cases; their separate jobs are recorded below. No Windows pass or mocked credential transition is presented as native proof.

Local tests used this checkout's explicit source path and separate workspace temporary roots. An initial local invocation specified a nonexistent basetemp parent and produced 79 setup errors, 1 pass and 33 skips; correcting the invocation yielded the focused result above without changing tests. The local full run started on `1fa53ee9a79f3828372ab142afc43e82e650f5e0`; final-head `src/` and `tests/` are byte-identical, verified by Git. Local Windows does not include PR #45 integration; final-head Actions does.

FINAL-HEAD CI: GitHub records these pull-request runs against `144d878586837409f6141c5fdc3212220e76448f`. The Linux checkout log and fetched Git parents independently establish synthetic merge `ea82253f66cadefac387c896e30e27406a5fdaab`, parents current main `cada2362beb43019f64dcb6395f98b136e0cd46c` and final PR head. This is post-PR-#45 integration evidence, despite the PR API's older base snapshot field. Intermediate-head run 39275884993 also passed native focused/surrounding/full, but is historical, not substituted for final-head CI.

| Gate | Final result | Run / job |
|---|---|---|
| Linux full / focused / surrounding / compile / committed whitespace | SUCCESS | [39276803695 / 118143105962](https://github.com/Nortan251/agentcore/actions/runs/39276803695/job/118143105962) |
| Windows Python 3.11 | SUCCESS | [39276803707 / 118143106469](https://github.com/Nortan251/agentcore/actions/runs/39276803707/job/118143106469) |
| Windows Python 3.12 | SUCCESS | [39276803707 / 118143106124](https://github.com/Nortan251/agentcore/actions/runs/39276803707/job/118143106124) |
| Windows Python 3.13 | SUCCESS | [39276803707 / 118143106410](https://github.com/Nortan251/agentcore/actions/runs/39276803707/job/118143106410) |
| Chromium Linux | SUCCESS | [39276803794 / 118143105836](https://github.com/Nortan251/agentcore/actions/runs/39276803794/job/118143105836) |
| Chromium Windows | SUCCESS | [39276803794 / 118143106566](https://github.com/Nortan251/agentcore/actions/runs/39276803794/job/118143106566) |

All six final-head gates completed successfully. No billing-blocked, pending, stale-head or skipped native setup result is counted as success. Windows job-detail requests intermittently returned HTTP 502; final successful conclusions were independently verified through the PR checks and workflow/job endpoints. No unobserved Windows full-suite pass/skip count is invented here.

FRESH EXACT-HEAD REVIEW: requested in [comment 6270710921](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6270710921); [receipt 6270751629](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6270751629) reports no major issues on `144d878586`. The review summary independently records the full final SHA and completed status. This does not close or override existing production findings.

REMAINING BLOCKERS / LIMITS: the [idmapped-alias production P1](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472896) remains unfixed: production still checks a staging inode then publishes a mutable pathname. Different-ordinary-principal writable-ancestor relocation also remains an outstanding boundary requirement. Neither hostile-mount nor ancestor-relocation proof was performed in this task. The [original worker-import P1 thread](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472904) is deliberately preserved unresolved; all four private-runtime cases now have successful native evidence. A fresh review finding no new major issues and passing CI do not establish repaired publication integrity or merge readiness.

NEXT REQUIRED ACTION: obtain separate explicit authorization for the production publication-integrity repair and its hostile-alias/ancestor regressions. Keep PR #44 OPEN / UNMERGED and PR #43 untouched in this task. No merge or security-access circumvention was performed. This independent documentation-only ledger branch must never be merged into main.

CURRENT VERDICT: **BOUNDED NATIVE HARNESS REPAIR COMPLETE / OPEN / UNMERGED / PRODUCTION SECURITY REPAIR STILL INCOMPLETE / NOT MERGEABLE.**


## 2026-10-19 amendment — production file-publication fail-closed candidate

AREA: filesystem
TASK: production staged-file publication integrity repair in existing PR #44
STATUS: OPEN / DRAFT / UNMERGED; READY FOR INDEPENDENT REVIEW; POSIX file creation deliberately unsupported; original P1 closure pending project lead

BASE / DIVERGENCE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
LIVE MAIN / CI INTEGRATION BASE: `cada2362beb43019f64dcb6395f98b136e0cd46c`
STARTING HEAD: `144d878586837409f6141c5fdc3212220e76448f`
PRODUCTION REPAIR: `e8bc88284934acbd99943083be5e8cc4d0497480` (parent `144d878586837409f6141c5fdc3212220e76448f`)
HEAD SHA: `8d9a5340441789c732935316883706bdc0aaf1f7`
PARENT SHA: `e8bc88284934acbd99943083be5e8cc4d0497480`
SYNTHETIC CI MERGE: `085647913ecfa3248e7576ee5f7a0f5095abd769` (parents `cada2362beb43019f64dcb6395f98b136e0cd46c` and `8d9a5340441789c732935316883706bdc0aaf1f7`)
BRANCH: `codex/fs-stage-publication-integrity-hotfix`

Earlier snapshots above remain historical. The previous vulnerable POSIX publication is now removed, rather than retained behind another ownership heuristic. No finding was formally resolved in this task.

### Goal, production changes and security decision

The old code compared a verified staging descriptor with a pathname and then passed that pathname to `os.link`. Hostile idmapped aliases could change its binding despite observed private owner/mode bits. Open directory descriptors also follow directories relocated through writable containing ancestors. Mount-local checks did not establish the claimed authority.

`_require_file_publication()` now refuses every non-native-Windows host with `unsupported_publication`. `create()` invokes it after bounded-content/lexical validation but before opening the mutation parent or calling the physical-entry callback. The POSIX inode-check/pathname-link implementation and `_safe_stage_parent` / `_linux_parent_filesystem` heuristics are removed. There is no fallback, model/configuration override, new dependency or privileged production requirement. Native Windows retains the original owned-handle no-replace publication and cleanup in the same shared foundation.

`observe()` applies the same support restriction. Old uncertain POSIX file attempts cannot be promoted merely because final bytes match: they remain OUTCOME_UNKNOWN / HUMAN_CONFIRMATION_REQUIRED with no retry. A new refused durable attempt is NOT_INVOKED / CONFIRMED_NOT_APPLIED. Existing authority, permission/approval, protected intent, action identity and journal schema are unchanged. Historical terminal records and residue are not rewritten/deleted. POSIX read tools and Directory Create have separate unchanged contracts; this repair does not certify directory namespace behavior.

Linux O_TMPFILE/linkat can bind the source inode, but cannot freeze destination ancestry; openat2 constrains lookup, not a later publication operation. Inspecting one mount cannot exclude hostile aliases. The implementation therefore withdraws POSIX file creation, including private ext4/XFS/Btrfs/tmpfs parents, instead of inventing an ordinary-principal trust exclusion. Windows's existing native semantics remain the supported scope; post-close immutability, privileged compromise resistance and parent-directory fsync durability are not promised.

### Files changed and regression coverage

Eleven files differ from the starting head: `src/agentcore/fs/mutation.py`, `src/agentcore/fs/paths.py`, `tests/test_fs_publication_support.py`, `tests/test_fs_stage_parent.py`, `tests/fs_stage_principal_worker.py`, `tests/test_fs_write.py`, `tests/test_fs_directory_create.py`, `tests/test_workspace_host.py`, `tests/test_workspace_directory_create.py`, `docs/architecture/filesystem-write-v1.md`, and `docs/architecture/workspace-agent-v1.md`. No workflow/governance, Browser, Gmail, model-execution, default-composition or PR #43 source changed.

New policy/ordering tests had 9 failures and 1 pass against the starting production mutation module, then 10 passes against the repair. These controlled Windows platform-selector checks are not native Linux exploit proof. A test-draft field-access mistake was corrected before that comparison. Existing supported Windows success, authority, race, failure, uncertainty and reconciliation assertions are retained. Linux now asserts the intentionally withdrawn capability's refusal; success-path tests are explicitly Windows-specific, without a fake native implementation or globally skipping the suite.

The native helper keeps real UID/GID/group/capability assertions and post-drop host construction. It adds an ancestor-relocation capability probe by the distinct ordinary attacker, restores that controlled directory, then proves that creation never reaches the retained publication seam. The committed Windows regression proves denied staging-name substitution and unchanged file identity through native publication. An additional local final-head assert check denied relocation of target parent, workspace root and containing ancestor during publication, then verified all three rename successfully after handle release. That supplementary same-UID Windows check is local evidence, not Linux or matrix-wide proof.

### Directly observed local results

Windows Python 3.13.15 focused: 90 passed / 27 skipped. Surrounding filesystem/workspace/permission/Task 43.2 suites: 969 passed / 127 skipped. Final changed-test subset: 13 passed. Final corrected policy-lease test: 1 passed. Full repository: **6439 passed, 288 skipped, 89 warnings in 492.16s (0:08:12)**.

The local full suite began at `e8bc88284934acbd99943083be5e8cc4d0497480`; final commit changes only the host test's turn/tool-result assertions, which passed separately. Runtime source and every other test are identical. Final-head CI independently exercised the entire final tree plus current main. Compileall, working/committed/starting-head/full-PR whitespace checks and final clean status passed. Local Python 3.11/3.12 and native Linux are unavailable; platform evidence is the CI below.

### Final-head CI

| Gate | Conclusion | Final-head job |
|---|---|---|
| Chromium adapter (ubuntu-latest) | SUCCESS | [job 118303886128](https://github.com/Nortan251/agentcore/actions/runs/39323673649/job/118303886128) |
| Chromium adapter (windows-latest) | SUCCESS | [job 118303885965](https://github.com/Nortan251/agentcore/actions/runs/39323673649/job/118303885965) |
| Linux full suite (py3.13) | SUCCESS | [job 118303885669](https://github.com/Nortan251/agentcore/actions/runs/39323673657/job/118303885669) |
| Windows fs verification (py3.11) | SUCCESS | [job 118303887099](https://github.com/Nortan251/agentcore/actions/runs/39323673757/job/118303887099) |
| Windows fs verification (py3.12) | SUCCESS | [job 118303887602](https://github.com/Nortan251/agentcore/actions/runs/39323673757/job/118303887602) |
| Windows fs verification (py3.13) | SUCCESS | [job 118303887511](https://github.com/Nortan251/agentcore/actions/runs/39323673757/job/118303887511) |

Final-head Linux: native stage-parent **26 passed, 1 skipped (Windows-only), 5.75s**; surrounding **979 passed, 117 skipped, 1 warning, 17.08s**; full **6558 passed, 210 skipped, 98 warnings, 154.19s**. All ten genuine distinct-ordinary-UID cases ran successfully, including all five private-runtime variants and both ancestor-relocation probes; there were zero native setup skips. The attacker demonstrated actual shared-entry substitution and ancestor-relocation authority in controlled probes; subsequent AgentCore file creation was refused without staging or confirmed application. This is native refusal evidence, not evidence of supported Linux publication.

Each Windows matrix version passed its real junction/reparse gate (1 passed, 160 deselected), its filesystem-security gate (58 passed), and the entire full-suite step. The existing full-suite command suppresses the aggregate summary through duplicate quiet flags; no unobserved Windows CI pass count is inferred. Both Chromium gates reported **419 passed**, Ubuntu in 113.59s and Windows in 163.52s. Linux compileall and committed whitespace checks passed. All six jobs checked out the synthetic merge listed above, whose parents are current main and the final PR head.

Intermediate Linux [run 39323436871](https://github.com/Nortan251/agentcore/actions/runs/39323436871) passed native tests but failed surrounding (1 failed, 978 passed, 117 skipped, 1 warning) because this task incorrectly equated TurnResult.ok with tool success. Final commit preserves turn completion and separately checks the exact invocation outcome; it changes no production source. The failed run is historical and is not substituted for final-head validation.

### Review, unresolved findings and limits

[Final-head review request](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6277174262) explicitly requested assessment of the production P1 and support restriction. [Completed receipt](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6277212416) identifies `8d9a534044` and reports no major issues. The full requested SHA is the final head above and has not changed. No new review finding was returned. This is independent automated review, not project-lead acceptance or original-P1 closure.

Production [P1 4187472896](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472896) has an implemented unconditional-refusal candidate; its thread remains open for independent closure and compatibility acceptance. Historical worker [P1 4187472904](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472904) was repaired before this task and its five native private-runtime cases pass; its thread also remains open. Historical whitespace [P2 4315526400](https://github.com/Nortan251/agentcore/pull/44#discussion_r4315526400) was previously repaired/resolved. GraphQL confirms exactly those two original unresolved threads; no new issue was returned by final-head review. A clean review and green CI are not treated as original-P1 closure.

No hostile two-idmapped-mount experiment was attempted or claimed; no Linux publication success guarantee is offered. No manual privilege/mount workaround or unavailable security service was used. Restoring POSIX file creation requires a separately enforceable source-identity and namespace-authority design with native evidence.

PR #43 remains OPEN / DRAFT / UNMERGED at `7bd7b4de98fe7cff63c577c0add2be16fb2d85dd`, unchanged. Its pathname-based `os.replace` finding is not repaired automatically. It must independently preserve source/target/revision identity, metadata/confidentiality, supported-platform refusal and uncertainty, with its own correction, regressions, reviews and CI. No feature work resumed here.

### Next required action and final verdict

**READY FOR INDEPENDENT REVIEW of the fail-closed candidate; no merge recommendation.** Production source no longer admits the vulnerable POSIX publication, native refusal and supported Windows identity checks passed, all six final-head gates passed, and the exact-head independent automated review completed without new findings. The project lead must assess the deliberate POSIX compatibility withdrawal and decide whether the original production P1 has sufficient closure evidence. Both original P1 threads remain open. PR #44 remains draft and unmerged; stop at this bounded milestone.

This append-only amendment and the single PR #44 INDEX row are the only ledger changes. Preserve this independent documentation-only branch; never merge it into main.

## 2026-10-19 amendment — platform-specific support declaration and Windows preservation

Earlier snapshots remain historical. The project lead's current task explicitly permits unsupported-platform refusal while requiring preservation of every working Windows feature. Live verification found PR #44 already advanced from supplied `144d878586837409f6141c5fdc3212220e76448f` through production containment `e8bc88284934acbd99943083be5e8cc4d0497480` and host-test correction `8d9a5340441789c732935316883706bdc0aaf1f7`. Their implementation and evidence were investigated before this follow-up. No historical result is relabeled as current.

BASE / DIVERGENCE SHA: `547a2f00bcfa709d907c329fbea78730dd6731f6`
LIVE MAIN / CI INTEGRATION BASE: `cada2362beb43019f64dcb6395f98b136e0cd46c` (PR #45 merged)
HEAD SHA: `f5d7e3bd3a21f49ee64affda61dbc520382c3907`
PARENT / VERIFIED STARTING SHA: `8d9a5340441789c732935316883706bdc0aaf1f7`
CI SYNTHETIC MERGE: `a644f7b5255f0ad37b837c874b90299e502e9de9`, parents current main and final head
BRANCH: `codex/fs-stage-publication-integrity-hotfix`
STATUS: OPEN / DRAFT / UNMERGED; READY FOR INDEPENDENT REVIEW of supported Windows publication / POSIX refusal

### Root cause, production scope and platform tradeoff

The old POSIX code checked an owned stage then used its mutable pathname for publication. Final-parent owner/mode/filesystem checks did not exclude idmapped aliases; open parent descriptors did not prevent relocation through writable containing ancestors. The intervening production repair removes this publication path and refuses POSIX legacy uncertain observation. The new commit retains that containment and the working Windows owned-handle writer.

Linux O_TMPFILE/linkat can bind source identity, but source identity alone does not establish admitted destination ancestry at publication. openat2 constrains its lookup, not a later independent link. Apple documents pathname link and advisory locking; these do not establish the combined invariant either. This is an engineering assessment of examined mechanisms, not proof that all future POSIX designs are impossible. No new ordinary-principal trust exclusion, privileged production requirement, mount heuristic, configuration override or pathname fallback is introduced. No native macOS environment is available, and macOS publication support is not claimed. References: [Linux open](https://man7.org/linux/man-pages/man2/open.2.html), [linkat](https://man7.org/linux/man-pages/man2/link.2.html), [openat2](https://man7.org/linux/man-pages/man2/openat2.2.html), [kernel idmappings](https://docs.kernel.org/filesystems/idmappings.html), [Apple link](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/link.2.html), [Apple advisory locks](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html).

Windows continues native owned-handle no-replace rename, stage writer/deleter exclusion, pinned directory handles, flushes, cleanup and uncertainty. The published object is the handle-owned verified object; this is a live publication guarantee, not immutable contents after handles close, historical authorship under arbitrary later changes, administrator/kernel protection or a Windows parent-directory fsync promise. The existing Windows final-content reconciliation contract is retained. `_windows_mutation.py` is byte-identical to the supplied head; AST comparison also confirms the retained Windows publication body and complete Directory Create function are unchanged.

POSIX refuses only file publication; read tools and Directory Create retain their separate contracts. `write_file` is not removed globally or from the fixed registry. Its catalogue now explicitly declares native Windows support and Linux/macOS unavailability. The new safe error is `filesystem_publication_unsupported`, with durable `filesystem.unsupported_publication`. Only an unsupported category before possible physical entry gets NOT_INVOKED / CONFIRMED_NOT_APPLIED. Once entry may have happened, the same category remains OUTCOME_UNKNOWN. Raw exception diagnostics do not escape. POSIX legacy uncertainty remains HUMAN_CONFIRMATION_REQUIRED without retry; historical terminal records and residue remain unchanged.

The exact registry/argument/policy/approval/executor/protected-intent/durable-attempt/guard route is preserved. Denial still precedes destination observation. Existing policy, registry and health leases, exact claim/workspace bindings, protected payload schema and action identity remain intact. Unrelated credential branches keep the same result classifications. No Browser, Gmail, model, governance/workflow, dependency or PR #43 change occurred.

### Changed files and deterministic regressions

This follow-up changes exactly five files: `src/agentcore/tools/builtin/write_file.py` (WriteFileTool catalogue), `src/agentcore/tools/executor.py` (filesystem pre-entry marker/classification/reporting), `tests/test_fs_publication_support.py` (catalogue, categorical durable refusal, entry/uncertainty split), `tests/test_fs_stage_parent.py` (native Windows ancestor-relocation/release control), and `docs/architecture/filesystem-write-v1.md` (platform reporting contract). The supplied-head-to-final candidate includes 13 files after the two intervening repair commits; no second filesystem implementation was added.

Controlled reporting regression on the verified parent: **5 failed / 6 deselected**. The repaired checks pass. The new injected-error test exercises both sides of the physical-entry checkpoint, preserving uncertainty after possible entry and excluding raw diagnostics. This is executor regression coverage, not native POSIX exploit proof.

The new native Windows test denies actual renames of final parent, workspace root and containing ancestor during owned-handle publication, verifies intended bytes, then successfully renames all three after handle release. This control rules out fixture permissions as the cause of denial. Existing owned-stage identity, denied stage substitution, collision, bounds, approval, permission, protected intent and recovery assertions remain.

### Exact final-tree validation

| Environment / scope | Passed | Skipped | Warnings / duration |
|---|---:|---:|---|
| Local Windows 3.13 before this follow-up, focused | 90 | 27 | 30.24s |
| Local Windows 3.13 final focused | 94 | 27 | 8.52s |
| Local Windows 3.13 final surrounding | 973 | 127 | 77.24s |
| Local Windows 3.13 final full | 6443 | 288 | 89 warnings; 557.44s |
| Final-head native Linux stage-parent | 26 | 2 | 6.17s; Windows-only skips |
| Final-head native Linux surrounding | 982 | 118 | 1 warning; 19.73s |
| Final-head native Linux full | 6561 | 211 | 98 warnings; 167.56s |
| Final-head Chromium Ubuntu | 419 | 0 | 82.83s |
| Final-head Chromium Windows | 419 | 0 | 143.64s |

Local commands use `python -m pytest -o addopts= -q -ra` with focused modules or the filesystem/workspace/permissions/Task-43.2 suites, then the full repository. Explicit source PYTHONPATH and separate short workspace temp roots were used. Compileall and both `git diff --check HEAD^ HEAD` and full-PR committed-diff checks passed. Windows matrix 3.11/3.12/3.13 each passed the real junction gate (1 passed / 160 deselected), filesystem security gate (58 passed), and full-suite step. Aggregate Windows CI full counts are suppressed by existing quiet options and are not inferred. All ten Linux distinct-UID cases passed, including five private-runtime variants and two ancestor probes. These establish safe refusal, not supported Linux file publication. No native setup skip is counted as a pass.

The initial surrounding invocation recorded 59 failures / 914 passes / 127 skips from Windows path-length errors in the deep temp path. The unchanged tests/source passed with a short workspace temp root. Full-suite warnings and real platform skips remain reported; no unrelated warning cleanup was attempted.

Native Linux all ten genuinely different ordinary-UID cases passed with zero native setup skips: shared, private/safe, masked ACL, foreign ownership and writable-ancestor scenarios, each in checkout and unreadable-private-runtime variants. Both real namespace-capability probes ran; AgentCore then refused file creation with no stage, no final file and no confirmed application. This is native safe-refusal evidence, not working Linux publication or two-idmapped-mount proof. The two focused skips are native Windows tests.

A first local surrounding invocation failed **59 / 914 passed / 127 skipped** because its deeply nested temp root exceeded Windows path limits while creating private storage lock files. The unchanged source/tests passed after using a short workspace temp root. This failure is retained as invocation evidence, not hidden or counted as security validation. Local Linux is unavailable (WSL not installed); only Python 3.13 is installed locally. Native Linux and Windows 3.11/3.12 evidence comes from the actual existing Actions jobs. No restricted-service or privilege workaround was sought.

| Gate | Conclusion | Exact final-head job |
|---|---|---|
| Chromium adapter (ubuntu-latest) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39333673728/job/118337737628) |
| Linux full suite (py3.13) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39333673733/job/118337737846) |
| Windows fs verification (py3.11) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39333673717/job/118337737939) |
| Chromium adapter (windows-latest) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39333673728/job/118337738164) |
| Windows fs verification (py3.12) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39333673717/job/118337738125) |
| Windows fs verification (py3.13) | SUCCESS | [job](https://github.com/Nortan251/agentcore/actions/runs/39333673717/job/118337737618) |

All six runs are attributed by GitHub to the exact final head; checkout logs establish the synthetic merge above with current main. Existing workflows and gates are unchanged. Windows full-suite commands suppress the aggregate count through duplicate quiet flags; only directly observed step conclusions are reported, not invented counts. Linux compileall/committed whitespace passed; local compileall, final-commit and full-PR diff checks passed.

### Review, original finding status and next milestone

Fresh independent [review request](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6278573410) names the full final SHA and original production finding. [Completed receipt](https://github.com/Nortan251/agentcore/pull/44#issuecomment-6278616986) identifies `f5d7e3bd3a`; the review summary independently records the full SHA, completed status and no new findings. This is automated code review, not separate project-lead acceptance or automatic closure of original findings.

Original production [P1 4187472896](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472896) has the implemented refusal candidate, but remains formally unresolved for independent closure assessment. Historical worker-import [P1 4187472904](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472904) was repaired before this follow-up, with all five private-runtime variants now passing; its thread remains open. Historical whitespace [P2 4315526400](https://github.com/Nortan251/agentcore/pull/44#discussion_r4315526400) remains repaired/resolved. No new final-head review issue was returned, and no review thread was resolved in this task.

Linux and macOS file creation remain unavailable, including ordinary private directories. No native macOS job exists in these workflows and none is represented as passed. No hostile two-idmapped-mount experiment was attempted or claimed. The candidate repairs the vulnerable admitted POSIX path by refusing it, without claiming supported POSIX publication. Directory Create/read namespace limits are separate, not newly certified. Main remains unchanged and therefore is not repaired by an unmerged Draft candidate.

PR #43 remains OPEN / DRAFT / UNMERGED at `7bd7b4de98fe7cff63c577c0add2be16fb2d85dd`, unchanged. It must not restore removed POSIX helpers or bypass publication admission. Its distinct replacement P1 still needs object-bound source/target/revision integrity, metadata/confidentiality preservation, platform-specific support/refusal, durable uncertainty, its own correction/regressions, fresh review and all required CI. No PR #43 feature work is performed or implied.

**READY FOR INDEPENDENT REVIEW of this Windows-supported / POSIX-refusal candidate.** Windows functionality is preserved with native before/after evidence, unsupported publication is categorically and durably refused, all six exact-head gates pass, and fresh exact-head automated review has no new finding. Original security closure, compatibility acceptance and merge remain project-lead decisions. PR #44 stays Draft and unmerged. No second PR, main push, history rewrite or merge occurred. Stop after this bounded milestone.

Only this existing PR #44 record receives the append-only amendment and its single INDEX row is updated. Preserve all earlier snapshots and unrelated records. This independent documentation-only branch must never be merged into main.
