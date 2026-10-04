# PR #40 — Filesystem Write v1

AREA: filesystem
TASK: Durable, explicitly composed create-only workspace file writes
STATUS: Implementation complete; PR OPEN and UNMERGED; ready for project-lead review (2026-10-04)

BASE SHA: `72a0f55be620565344b3e57667371e2a695adb0e`
HEAD SHA: `e32a9c6a66537c4da15522e0262ee4b415fbf934`
PARENT SHA: `ba3382b0d9c2607f68b3d2c0656a65df0c41bf72`
BRANCH: `codex/filesystem-write-v1`

[Implementation PR](https://github.com/Nortan251/agentcore/pull/40) · [Exact implementation commit](https://github.com/Nortan251/agentcore/commit/e32a9c6a66537c4da15522e0262ee4b415fbf934)

## Goal

Create one missing bounded UTF-8 regular file through the real ToolExecutor and ActionJournal. Refuse existing destinations without replacement. Keep default Agent construction read-only and provide explicit trusted host composition.

## Production changes

- WorkspaceGuard owns POSIX descriptor-relative and Windows inspected handle-relative staging/publication/reconciliation. Native publication refuses an existing final name atomically; normal cleanup removes only owned staging objects.
- FilesystemWriteFoundation binds the exact write tool, protected intent, durable mutation classification and read-only reconciler. Content is persisted once in protected intent; public journal, invocation, approval and logging surfaces remain redacted.
- Executor-only one-use context requires durable intent/attempt truth and current authority. Policy, exact registration and health leases cover preparation, physical I/O and reconciliation through journal truth publication, without holding metadata mutexes across I/O.
- Host write limit is separately capped at 128 KiB; conservative JSON serialization bound is 819,200 bytes below the existing 1 MiB protected-record ceiling.

## Files changed

Eight production files, two test files, one architecture document; no governance or browser/Gmail implementation files changed. Paths refer to the exact implementation commit above.

| Path | Purpose |
|---|---|
| `src/agentcore/config/loader.py` | Load host-owned write bound |
| `src/agentcore/fs/config.py` | Separate capped write-byte limit |
| `src/agentcore/fs/guard.py` | Guard-owned create/probe entry points and boundary documentation |
| `src/agentcore/fs/mutation.py` | Verified traversal, exclusive staging, atomic missing-only publication and safe final observation |
| `src/agentcore/fs/_windows_mutation.py` | Narrow native handle/reparse/no-replace boundary and canonical parent names |
| `src/agentcore/tools/base.py` | Non-public live filesystem execution context |
| `src/agentcore/tools/builtin/write_file.py` | Fixed create-only tool, protected semantic adapter and reconciler |
| `src/agentcore/tools/executor.py` | Durable/current authority, leases, interruption classification and quarantine ordering |
| `tests/test_fs_paths.py` | Preserve the read-path structural prohibition while excluding exactly the new mutation modules |
| `tests/test_fs_write.py` | Real filesystem, journal, permission, recovery, concurrency and resource regressions |
| `docs/architecture/filesystem-write-v1.md` | Guarantees, bounds, host exclusions, durability and explicit composition |

## Architecture and security decisions

`FS_WRITE`, `read_only=False`, `DURABLE_LOCAL_MUTATION`, `LOCALLY_VERIFIABLE_OR_REVERSIBLE`. Public arguments are only workspace-relative path and text content; host composition controls roots, action identity, pre-state, staging names and effect/recovery classification.

POSIX uses component-wise directory descriptors with no-follow traversal, exclusive 0600 same-parent staging, exact writes/fsync, no-replace hard-link publication, owned staging unlink and parent fsync. Windows rejects reparse components, reapplies sensitive policy to canonical opened parent names (including DOS short-name aliases), excludes directory delete sharing, flushes staging and publishes via relative native rename with replacement disabled.

OBSERVE/PROPOSE cannot write. CONFIRM requires existing durable approval; its pre-approval destination observation independently requires allowed FS_READ. AUTONOMOUS still obeys current FS_WRITE denial. Failed initial claim/registration snapshots cannot fall through to an unleased later lookup.

Safe single-linked exact final content reconciles APPLIED. Unsafe/different/unverifiable final content remains UNKNOWN/HUMAN. **After project-lead clarification, absence following an uncertain attempt also remains UNKNOWN/HUMAN:** current absence cannot disprove prior publication followed by cooperating deletion. Known live no-replace refusal can prove NOT_APPLIED; proven pre-staging interruption is NOT_INVOKED. Recovery authorizes no automatic write retry.

Quarantine denies new admissions immediately and completes after already leased filesystem work exits. The owning thread retains its admitted healthy snapshot. A reentrant request fails busy but is latched for sticky quarantine at final lease exit; startup proof cannot clear it. Read-only reconciliation leases remain active through truth publication.

## Bugs found

Codex reported eight P1 findings across successive exact-head reviews:

1. [Denied destination existence oracle](https://github.com/Nortan251/agentcore/pull/40#discussion_r4174961095).
2. [False historical absence after uncertain publication](https://github.com/Nortan251/agentcore/pull/40#discussion_r4174961100).
3. [Sensitive Windows parent short-name alias](https://github.com/Nortan251/agentcore/pull/40#discussion_r4174961101).
4. [Policy revocation during preparation](https://github.com/Nortan251/agentcore/pull/40#discussion_r4175395075).
5. [Read permission/registration revocation during reconciliation](https://github.com/Nortan251/agentcore/pull/40#discussion_r4175395078).
6. [Pre-approval observation while FS_READ is blocked](https://github.com/Nortan251/agentcore/pull/40#discussion_r4175422392).
7. [Unavailable registration followed by unleased re-registration](https://github.com/Nortan251/agentcore/pull/40#discussion_r4175445002).
8. [Quarantine completing between validation and publication](https://github.com/Nortan251/agentcore/pull/40#discussion_r4175476655).

Local validation also caught pre-staging cancellation classification, a POSIX publication-hook test setup interaction, a sibling unavailable-claim fallback, and process-wide handle-count contamination (a decrease from 251 to 247 during a broader run).

## Bugs fixed

All eight P1 findings have code repairs and resolved replies with focused before/after evidence. Nineteen reviewed/sibling regression cases fail on their respective prior heads and pass after repair. The handle test now runs in a fresh process while preserving exact equality assertions for real writes and staging failure; no leak assertion or CI gate was weakened. Pre-staging cancellation stays inert; the POSIX hook delegates unrelated hard-link fixture setup.

## Known limitations

- Configured workspace and protected stores are trusted host configuration. Malicious same-user processes, administrators/root and hostile equivalent-authority ancestor relocation are excluded explicitly. The original blocker investigation was retained; real POSIX characterization demonstrates the excluded relocation rather than claiming to prevent it.
- Parents must exist; no overwrite, append, replacement, deletion or directory creation. Normal Agent/default tools remain read-only until a later host-composition task.
- Protected JSON authenticates/separates content but is not encrypted at rest; confidentiality requires host filesystem access control.
- Windows file flush/atomic visibility does not establish a POSIX-equivalent parent-directory durability guarantee. Unsupported native publication fails closed.
- Crash residue is at most one owned random staging object per physical attempt. POSIX interrupted link/unlink can leave two links and require operator cleanup before reconciliation can prove a safe final file. No cleanup daemon or blind retry was added.
- Quarantine completion can wait for earlier native filesystem I/O. A same-thread request reports busy while retaining the pending quarantine; future admission stays denied.

## Test results

Reported local Windows Python 3.13 results on the final implementation:

- Focused write suite: **78 passed, 4 POSIX-only skips**.
- Filesystem, ActionJournal, executor and permission suites: **818 passed, 95 skipped**.
- Full repository: **6,288 passed, 256 skipped, 89 warnings**, 450.20 seconds. Warnings include unchanged Gmail fake-response destructor warnings.
- `compileall -q src tests` and `git diff --check`: passed; implementation worktree clean.

The local skipped POSIX FIFO, detectable substitution, excluded hostile relocation and staging-alias residue cases are covered by the final-head Linux full-suite gate. Native Windows no-replace/reparse/short-name/resource tests run in all three Windows full-suite jobs. Chromium integration evidence comes from the two real browser CI gates below.

## CI

All six jobs **SUCCESS**; each workflow run's API `headSha` equals `e32a9c6a66537c4da15522e0262ee4b415fbf934`.

- Linux full: [run 37164736602 / job 111325233481](https://github.com/Nortan251/agentcore/actions/runs/37164736602/job/111325233481).
- Windows 3.11: [run 37164736647 / job 111325233825](https://github.com/Nortan251/agentcore/actions/runs/37164736647/job/111325233825).
- Windows 3.12: [run 37164736647 / job 111325233698](https://github.com/Nortan251/agentcore/actions/runs/37164736647/job/111325233698).
- Windows 3.13: [run 37164736647 / job 111325233807](https://github.com/Nortan251/agentcore/actions/runs/37164736647/job/111325233807).
- Chromium Linux (Ubuntu): [run 37164736617 / job 111325233636](https://github.com/Nortan251/agentcore/actions/runs/37164736617/job/111325233636).
- Chromium Windows: [run 37164736617 / job 111325233542](https://github.com/Nortan251/agentcore/actions/runs/37164736617/job/111325233542).

## Codex review

Exact reviewed SHA: `e32a9c6a66537c4da15522e0262ee4b415fbf934`.

[Final scoped review request](https://github.com/Nortan251/agentcore/pull/40#issuecomment-5974955036). [Codex final result](https://github.com/Nortan251/agentcore/pull/40#issuecomment-5974985574): no major issues; reviewed commit `e32a9c6a66`; no new inline findings on this head. Review covered overwrite/path/link/alias escapes, journal truth before mutation, staging, interruption/retry, leakage, permissions/approval, handle leaks, quarantine ordering and documented host exclusions.

## Unresolved review threads

**0**, verified through GitHub reviewThreads on the exact final head. All eight reported P1 threads are resolved; no unresolved P0/P1/P2 findings remain.

## Blockers

None for this implementation handoff. PR remains open and unmerged.

## Next required action

Project lead reviews the exact PR/head and explicitly decides the next phase. Normal JARVIS mutation enablement and replacement semantics remain separate future tasks. No automatic merge.

## Final verdict

READY FOR PROJECT-LEAD FILESYSTEM WRITE V1 REVIEW
