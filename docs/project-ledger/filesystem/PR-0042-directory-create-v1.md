# PR #42 — Directory Create v1

AREA: filesystem
TASK: Journal-bound create-only mkdir and Workspace Agent integration
STATUS: Implementation complete; PR OPEN and UNMERGED; ready for project-lead review (2026-10-04)

BASE SHA: `d6acae41e95059289956a4a2acde33d170a537cf`
HEAD SHA: `f8d965e6b7053b80659d3d1adde54df4ad500aa0`
PARENT SHA: `d6acae41e95059289956a4a2acde33d170a537cf`
BRANCH: `codex/directory-create-v1`

[Implementation PR](https://github.com/Nortan251/agentcore/pull/42) · [Exact commit](https://github.com/Nortan251/agentcore/commit/f8d965e6b7053b80659d3d1adde54df4ad500aa0) · [Directory contract](https://github.com/Nortan251/agentcore/blob/f8d965e6b7053b80659d3d1adde54df4ad500aa0/docs/architecture/filesystem-directory-create-v1.md)

## Goal

Add exactly `create_directory(path)`: one missing workspace directory per call,
with existing parents. An authorized Workspace Agent can create `notes`, then
use the existing create-only `write_file` for a file inside it.

## Production changes

- Guard-owned descriptor/handle-relative directory create, with no recursion,
  replacement, deletion, shell, permission options or ownership markers.
- Exact `CreateDirectoryTool` and adapter use DURABLE_LOCAL_MUTATION and
  LOCALLY_VERIFIABLE_OR_REVERSIBLE; minimal protected intent pins normalized
  target, absent pre-state, workspace and parent identities, tool/mode/schema.
- Executor context generalized only for the two exact reviewed concrete
  mutation types. PR40's private context name/field remain compatibility aliases;
  exact tool/workspace/thread/PID and one-use binding prevent cross-consumption.
- Sealed Workspace catalogue is exactly inspect, list_directory, read_file,
  create_directory, write_file. Both mutations share action-capacity admission
  and sticky failure behavior. Ordinary Agent/default tools/chat remain read-only.

## Files changed

Paths refer to the exact implementation commit above; this ledger has no code.

- `src/agentcore/fs/mutation.py`, `guard.py`, `_windows_mutation.py`: verified
  native mkdir, trusted existing-target refusal, interrupted native-handle cleanup.
- `src/agentcore/tools/base.py`, `executor.py`: shared exact filesystem context,
  current authority/registry/health leases, conservative effect seam handling.
- `src/agentcore/tools/builtin/_filesystem_execution.py`, `create_directory.py`,
  `write_file.py`: one-use context, directory tool/adapter/reconciler, file alias.
- `src/agentcore/workspace_host/composition.py`: fifth tool, prompt and shared gates.
- `tests/test_fs_directory_create.py`, `test_workspace_directory_create.py`,
  `test_workspace_host.py`: real native effects, crash/authority seams, fixed
  catalogue, defaults/chat refusal, mixed capacity and startup history.
- `docs/architecture/filesystem-directory-create-v1.md`, `workspace-agent-v1.md`:
  exact contract, native guarantees, uncertainty and fifth-tool integration.

## Architecture and security decisions

Canonical execution remains registry -> validation -> current PermissionPolicy
-> ToolExecutor -> durable journal intent/attempt -> one-use context -> guard
-> native create. Direct run fails closed. Exact registration, policy generation,
claim and workspace are revalidated at the physical seam; leases cover native
I/O and cleanup without metadata mutexes over I/O. Preparation retains PR40's
read-authority requirement for pending CONFIRM; the host always requires
current explicit --enable-write plus AUTONOMOUS FS_READ and FS_WRITE.

POSIX uses `mkdir(final_name, 0700, dir_fd=verified_parent_fd)`, no-follow
directory open and identity/type checks, then created-directory/parent fsync.
Windows uses relative NtCreateFile FILE_CREATE, directory and reparse-open
semantics, returned-handle inspection and default trusted-parent ACL inheritance.
No pathname fallback or model-selected ACL exists. Existing targets of every
type, unsafe parents, sensitive/absolute/traversal/native device/ADS spellings
and detectable parent substitution are refused. DOS sensitive-parent aliases
retain the canonical-name checks.

Already-existing targets are refused before action admission with a categorical
confirmed-not-applied invocation result; an absent pre-state is never fabricated.
Native collision after preparation yields journaled CONFIRMED_NOT_APPLIED.
Live native success plus post-checks yields CONFIRMED_APPLIED. Proven pre-seam
interruption is NOT_INVOKED; possible native entry or failed post-check is
RECONCILE_REQUIRED. No cleanup deletes a possibly created directory.

Neither later presence nor absence proves historical mkdir ownership. Recovery
always retains OUTCOME_UNKNOWN/HUMAN_CONFIRMATION_REQUIRED for uncertain mkdir,
without a filesystem probe that could add no decisive evidence. Current FS_READ
and recovery/registration/policy/health leases still gate reconciliation. There
is no blind retry or marker/xattr/ADS dependency. Startup unresolved mkdir blocks
new model turns; AUTHORIZED history is never executed automatically.

Private stores, transcript capacity/temp recovery, ownership fences and public
categorical logging/JSON remain the PR41 contracts. No paths or host absolute
locations are published. Browser/Gmail/session domains and SYSTEM_PROMPT are unchanged.

## Bugs found

No P0/P1 issue in merged PR40/PR41 was discovered. A new synthetic Windows crash
hook initially retained an unreturned handle, and long fixture paths initially
hit this host's Windows pathname limit; neither result was treated as a green gate.

## Bugs fixed

The hook now closes crash-lost handles as process teardown would; native open
also closes a returned handle on interruption. Real success/post-check failure
handle counts are tested in a fresh subprocess. Shorter workspace-local pytest
scratch paths resolved the fixture path limit. Standing parent-limit prompt
wording was retained. No failed CI gate was rerun to obtain green.

## Known limitations

- Uncertain mkdir requires an operator even when its target currently exists.
- Windows atomic visibility has no supported POSIX-equivalent parent-directory
  fsync or power-loss durability guarantee. New directory ACLs inherit from the
  trusted workspace parent; this capability does not edit ACLs.
- Malicious same-user code and administrator/root namespace relocation remain
  excluded. POSIX descriptors do not freeze ancestry after hostile relocation.
- Protected JSON is authenticated plaintext, not encrypted at rest. Privacy
  relies on host filesystem access controls and endpoint trust.
- Existing 256-action and private-session capacity bounds remain. No editing,
  recursive mkdir, delete/rmdir, rename/move/copy, shell/process or chmod capability.

## Test results

Local Windows Python 3.13, unchanged final implementation source:

- Focused directory/write/Workspace suites: **217 passed, 12 skipped**.
- Affected filesystem, permissions, executor/ActionJournal/recovery, conversation,
  session/domain and CLI suites: **2261 passed, 144 skipped**.
- One full repository run after production stabilized: **6427 passed, 264 skipped**,
  exit 0. Configured double-quiet output suppresses the textual count; these are
  exact emitted progress counts, with no failed/error/xfail outcomes.
- `python -m compileall -q src tests` and base-relative `git diff --check`: passed.
  Final tracked/untracked implementation tree clean; local/remote/PR SHA equal.

Windows skips do not prove POSIX behavior; the matching exact-head Linux full
gate passed. The full local run retained Gmail fake-response destructor warnings;
Gmail production source was not modified. Native Windows cases passed locally
and on the three Windows gates; real Chromium gates supply optional browser coverage.

## CI

All six jobs SUCCESS on exact head `f8d965e6b7053b80659d3d1adde54df4ad500aa0`.
All three normal pull_request runs completed on run_attempt=1.

- Linux full: [run 37206763079 / job 111449442611](https://github.com/Nortan251/agentcore/actions/runs/37206763079/job/111449442611).
- Windows 3.11: [run 37206763057 / job 111449442486](https://github.com/Nortan251/agentcore/actions/runs/37206763057/job/111449442486).
- Windows 3.12: [run 37206763057 / job 111449442485](https://github.com/Nortan251/agentcore/actions/runs/37206763057/job/111449442485).
- Windows 3.13: [run 37206763057 / job 111449442367](https://github.com/Nortan251/agentcore/actions/runs/37206763057/job/111449442367).
- Chromium Linux: [run 37206762968 / job 111449442155](https://github.com/Nortan251/agentcore/actions/runs/37206762968/job/111449442155).
- Chromium Windows: [run 37206762968 / job 111449442324](https://github.com/Nortan251/agentcore/actions/runs/37206762968/job/111449442324).

## Codex review

Exact reviewed SHA: `f8d965e6b7053b80659d3d1adde54df4ad500aa0`.
[Fresh review request](https://github.com/Nortan251/agentcore/pull/42#issuecomment-5980656510)
covered native paths, authority/journal seams, uncertainty/retry, handle cleanup,
fixed catalogue, capacity, failure truth and host/session/Browser/Gmail boundaries.
[Codex result](https://github.com/Nortan251/agentcore/pull/42#issuecomment-5980705035):
no major issues; reviewed commit `f8d965e6b7`. No inline findings.

## Unresolved review threads

**0**; GraphQL reviewThreads has no next page. No unresolved P0/P1/P2 findings.

## Blockers

None for this implementation handoff. PR #42 remains open and unmerged.

## Next required action

Project lead independently reviews the exact implementation PR/head. No automatic
merge. Replacement/editing and operator lifecycle remain separate tasks.

## Final verdict

READY FOR PROJECT-LEAD DIRECTORY CREATE V1 REVIEW

## Amendment — 2026-10-04: project-lead review and merge

This amendment supersedes the earlier open/unmerged status while preserving the full pre-merge handoff above.

The project lead independently rechecked PR metadata, main-relative production changes, the generalized exact filesystem mutation context, POSIX/Windows native directory-create paths, journal-before-effect ordering, conservative uncertain-outcome handling, Workspace Agent five-tool integration, action-capacity/failure latching, exact-head review state, and all six GitHub Actions gates on `f8d965e6b7053b80659d3d1adde54df4ad500aa0`.

All three workflow runs completed successfully on `run_attempt=1`; unresolved review threads remained 0. No additional P0/P1/P2 blocker was found. A final project-lead approval comment was posted, then PR #42 was merged with an expected-head guard so any head movement would have stopped the merge.

Merge commit: `547a2f00bcfa709d907c329fbea78730dd6731f6`.

Authoritative `main` after merge: `547a2f00bcfa709d907c329fbea78730dd6731f6`.

Final status: **MERGED; Directory Create v1 complete.** Workspace Agent now exposes exactly five tools: inspect, list_directory, read_file, create_directory, write_file. Ordinary Agent/default chat remain read-only.
