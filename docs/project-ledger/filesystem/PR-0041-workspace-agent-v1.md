# PR #41 — Workspace Agent v1

AREA: filesystem
TASK: Explicit one-shot model-driven workspace reads and create-only file writes
STATUS: Implementation complete; PR OPEN and UNMERGED; ready for project-lead review (2026-10-04)

BASE SHA: `67e6069e30979fea68aefd50164123a7348ada3e`
HEAD SHA: `9c4d0123df9c3b161f81b3c1923cf35c80bd1dc2`
PARENT SHA: `ff80ea121a6beae5cbfaee3de157f1a055ab6440`
BRANCH: `codex/workspace-agent-v1`

[Implementation PR](https://github.com/Nortan251/agentcore/pull/41) · [Exact final commit](https://github.com/Nortan251/agentcore/commit/9c4d0123df9c3b161f81b3c1923cf35c80bd1dc2) · [Architecture contract](https://github.com/Nortan251/agentcore/blob/9c4d0123df9c3b161f81b3c1923cf35c80bd1dc2/docs/architecture/workspace-agent-v1.md)

## Goal

Run one real model conversation turn with exactly inspect, list_directory, read_file and PR40's existing create-only write_file. Require current explicit host opt-in and current AUTONOMOUS FS_READ plus FS_WRITE; retain default Agent/chat read-only behavior.

## Production changes

- `agentcore workspace --enable-write --message "..." [--session conv_<12 hex>] [--json]` is a dedicated one-shot host, using the standard provider factory. Absent opt-in refuses even config loading; policy denial precedes provider, workspace and mutation-service construction. No conversational approvals or permission elevation.
- The composition owns sealed exact tool and action-adapter registries. All admitted calls use the canonical executor; write_file, native filesystem mutation, canonical executor, Gmail, credentials and governance source remain unchanged. Both required capabilities and AUTONOMOUS remain checked under the existing policy-generation lease through physical I/O.
- Private storage outside the workspace owns the installation key, journal, protected intents and transcript store. Exclusive slot ownership covers bootstrap, recovery, one turn, save and close. PID/thread fencing rejects retained/inherited authority before lock or I/O access.
- Startup authenticates stored protected objects, validates journal recovery before model calls, and performs only existing read-only reconciliation. Remaining unresolved actions block with bounded opaque IDs. No automatic create retry or AUTHORIZED-action resume.
- CLI publishes categorical errors and durable action states even after model, save or cleanup failure. Failed/uncertain writes disable further calls; model prose cannot reverse their truth. Reply text stays in the private transcript; public JSON has `reply: null` and `reply_generated`.

## Files changed

Paths refer to the exact final commit above. No production code is placed on the ledger branch.

| Path | Purpose |
|---|---|
| `src/agentcore/cli.py` | Dedicated workspace parser and early host dispatch |
| `src/agentcore/conversation/runtime.py` | Accept the dedicated session domain |
| `src/agentcore/conversation/session.py` | Isolated marker and reserved-directory cross-domain refusal |
| `src/agentcore/workspace_host/__init__.py` | Dedicated composition export |
| `src/agentcore/workspace_host/errors.py` | Content-free categories and current authority checks |
| `src/agentcore/workspace_host/privacy.py` | Event/numeric-only logging |
| `src/agentcore/workspace_host/storage.py` | Native private storage, explicit key lifecycle, lifetime locking and inert transcript-temp recovery |
| `src/agentcore/workspace_host/composition.py` | Fixed tools, scoped one-turn conversation, canonical execution, startup recovery and action truth |
| `src/agentcore/workspace_host/cli.py` | One-shot provider flow and bounded public result |
| `tests/test_workspace_host.py` | Real stores/files, synthetic provider, native ACL/mode/locks, Events, process kills and fork regressions |
| `docs/architecture/workspace-agent-v1.md` | Host contract, privacy, ownership, recovery and limits |

## Architecture and security decisions

Windows uses OS-known Local AppData under `AgentCore/WorkspaceFiles`; POSIX uses the current UID's passwd home under `.local/share/agentcore/workspace-files`. Neither is controlled by model/config/environment path strings. Trusted Python deployment/test injection remains host-only. The canonical workspace identity selects a domain-separated SHA-256 slot; host root and workspace cannot overlap in either direction.

POSIX directories/files require current ownership and private modes; unsafe writable ancestors, links and special files refuse without repair. Windows new directories reuse only the reviewed private DACL installer. Existing directories require current-user ownership and protected inheritable current-user/SYSTEM ACLs. Regular files also accept the token's default owner group on elevated hosts. Pinned directory identities and native handles/locks protect the slot lifetime; close releases stores before slot ownership and invalidates admission first.

Session domain `workspace_files` uses `workspace_files_isolated_v1` and private `.workspace_sessions`. Ordinary, Browser and workspace domains cannot replay each other's sessions or use each other's reserved stores. Missing/corrupt resumes refuse before the model; history never restores opt-in, authority, capabilities or tools. No generic AgentState, memory, capture, Browser, Gmail, shell, process, credentials, background loop or resolution UI is composed.

The inherited create-only primitive refuses existing destinations, absent parents, sensitive/escaping paths and unsafe link/reparse cases. Host reads enforce conservative sensitive/link posture. Raw structured write arguments and protocol IDs are not serialized into transcripts. User messages, reads and final prose can contain content and intentionally persist privately. Public journal/result/log surfaces remain content-free; protected payloads authenticate plaintext and are **not encrypted at rest**.

Capacity is admitted before effects: a 257th action is refused before canonical preparation/publication under exclusive ownership, retaining earlier effect truth. A 4,097th durable transcript is refused before model/new-file admission. Existing sessions can still update at capacity. Startup reserves one additional bounded scan slot for an interrupted atomic transcript save, validates every entry before removing only owner-private regular `tmp<8 lowercase/digit/underscore chars>.tmp` files in the pinned private transcript directory, and never reads/replays/promotes their content. Saved transcripts, journal and protected payloads are preserved; unknown overflow/unsafe objects fail closed and action/payload temp handling is unchanged.

## Bugs found

Three Codex findings across the implementation and repair heads:

1. P1: [journal capacity could hide a physical create](https://github.com/Nortan251/agentcore/pull/41#discussion_r4177530032).
2. P2: [new sessions could exceed the directory bound](https://github.com/Nortan251/agentcore/pull/41#discussion_r4177530036).
3. P1: [killed full-session update could hide action/recovery truth behind temp overflow](https://github.com/Nortan251/agentcore/pull/41#discussion_r4177589124).

## Bugs fixed

All three were repaired in host code, with resolved evidence replies. Three real-boundary regressions failed on `21fe5f7517b3a40e08c6c34a3ada33b11b37fd90` and pass after capacity admission repair: 255-to-256-to-257 actions in one turn, an already-full 256-action slot, and 4,095-to-4,096 transcripts followed by new-session refusal and successful resume.

Two actual process-kill cases failed on `ff80ea121a6beae5cbfaee3de157f1a055ab6440` and pass after crash-temp repair: kill just after mkstemp, and after writing but before os.replace, with 4,096 transcripts and a real preceding successful create. Reopen preserves confirmed-applied journal truth and executes a real read. Two additional regressions retain unknown overflow and refuse all cleanup if any entry has unsafe native ACL/mode. No sleep-based concurrency oracle or weakened CI gate was added.

## Known limitations

- Protected storage is authenticated plaintext; confidentiality depends on host filesystem permissions and endpoint security. Malicious same-user code, administrators/root and equivalent-authority namespace relocation remain outside the host boundary.
- PR40's Windows power-loss/parent-directory flush limitations remain unchanged. Missing parents must be created separately; there is no overwrite, append, delete, move, copy, mkdir, shell or process tool.
- Bounded retention remains 256 journal actions and 4,096 durable private-directory entries. History is not silently pruned; new records/sessions at capacity require future operator lifecycle work. Startup corruption/unknown overflow fails closed.
- Uncertain effects require existing reconciliation or human intervention; absent final files after uncertainty never justify blind retry. AUTHORIZED historical records are preserved without automatic execution.
- Model reply prose is private by design. This is one turn with no streaming, GUI, background autonomy or conversational/operator-resolution flow. Model/service/session failures never roll back a confirmed create.

## Test results

Local Windows Python 3.13 evidence on unchanged final-head source:

- Focused workspace host: **62 passed, 6 POSIX-only skipped**, 44.40 seconds.
- Surrounding filesystem write/read/list/inspect, conversation/session, ActionJournal/recovery, CLI, Browser session and Gmail host suites: **2,187 passed, 148 skipped, 9 warnings**, 178.03 seconds.
- Final full repository after production stabilized: **6,350 passed, 262 skipped, 89 warnings**, 456.73 seconds.
- `python -m compileall -q src tests` and main-relative `git diff --check`: passed. Implementation tracked tree clean, local/remote/PR head equal.

Warnings include fake Gmail response destructor warnings in unchanged code. Local Windows skips do not establish POSIX execution; the exact-head Linux full gate provides matching platform coverage. Native Windows ownership/ACL/reparse tests are also covered by the three Windows full-suite gates. Earlier-head suite results and the interrupted `ff80ea1` full run are not substituted for final evidence.

## CI

All six jobs **SUCCESS**. All three normal `pull_request` workflow runs completed at `head_sha=9c4d0123df9c3b161f81b3c1923cf35c80bd1dc2`, `run_attempt=1`. No red job was rerun merely to obtain green.

- Linux full: [run 37203100510 / job 111438627806](https://github.com/Nortan251/agentcore/actions/runs/37203100510/job/111438627806).
- Windows 3.11: [run 37203100502 / job 111438627852](https://github.com/Nortan251/agentcore/actions/runs/37203100502/job/111438627852).
- Windows 3.12: [run 37203100502 / job 111438627762](https://github.com/Nortan251/agentcore/actions/runs/37203100502/job/111438627762).
- Windows 3.13: [run 37203100502 / job 111438627830](https://github.com/Nortan251/agentcore/actions/runs/37203100502/job/111438627830).
- Chromium Linux (Ubuntu): [run 37203100525 / job 111438628233](https://github.com/Nortan251/agentcore/actions/runs/37203100525/job/111438628233).
- Chromium Windows: [run 37203100525 / job 111438628550](https://github.com/Nortan251/agentcore/actions/runs/37203100525/job/111438628550).

## Codex review

Exact reviewed SHA: `9c4d0123df9c3b161f81b3c1923cf35c80bd1dc2`.

[Final exact-head re-review request](https://github.com/Nortan251/agentcore/pull/41#issuecomment-5980054478). [Codex result](https://github.com/Nortan251/agentcore/pull/41#issuecomment-5980083864): no major issues, reviewed commit `9c4d0123df`. No new inline findings on this head. The initial fresh review and required repair re-reviews covered current authority, fixed tools, cross-domain privacy, recovery ordering/truth, ownership, native permissions and capacity/crash seams.

## Unresolved review threads

**0**, verified with GitHub reviewThreads (no next page). Both P1 threads and the P2 thread are resolved; no unresolved P0/P1/P2 findings remain.

## Blockers

None for this implementation handoff. PR #41 is open, non-draft and unmerged; main remains the requested base.

## Next required action

Project lead independently reviews the exact PR/head and chooses the next phase. No automatic merge. Replacement semantics, operator lifecycle/resolution and broader compositions remain separate work.

## Final verdict

READY FOR PROJECT-LEAD WORKSPACE AGENT V1 REVIEW
