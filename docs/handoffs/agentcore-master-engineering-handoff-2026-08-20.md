# AgentCore master engineering handoff - 2026-08-20

Status: **Task 42.2 IMPLEMENTATION AND CI GREEN — INDEPENDENT FINAL
CONFIRMATION STILL PENDING**

This document is the durable transfer point for a new engineering agent. It is
descriptive, not executable. The code-under-review baseline is
`797e2f5c8087423d72a2e972adaa350b47b4393f`. The documentation commit is the
commit containing this file with subject `docs: add post-task-42.2 engineering
handoff`; resolve it exactly with:

```text
git log -1 --format=%H -- docs/handoffs/agentcore-master-engineering-handoff-2026-08-20.md
```

A Git commit cannot embed its own final SHA without changing that SHA. The
command above is therefore the authoritative self-reference; the project-lead
report accompanying the commit records the resolved value.

## 1. Repository identity and exact status

| Item | Verified value |
|---|---|
| Repository | AgentCore (`agentcore` package), private GitHub repository |
| Branch | `main` |
| Code-under-review SHA | `797e2f5c8087423d72a2e972adaa350b47b4393f` |
| Handoff documentation SHA | Containing commit identified by the command above |
| Pre-handoff tree | Clean; local `main`, `origin/main`, and remote `main` all matched the code SHA |
| Package | `agentcore` 0.1.0; setuptools build backend |
| Runtime | Python standard-library-first library and CLI (`agentcore = agentcore.cli:main`) |
| Python requirement | `>=3.11`; Windows CI covers 3.11, 3.12, and 3.13; Linux CI uses 3.13 |
| Files at code baseline | 96 Python source files under `src`; 80 Python test files under `tests` |
| Dependencies | No runtime dependencies; development extra is `pytest>=8` |

Two gating workflows exist. `.github/workflows/filesystem-linux.yml` runs the
complete suite on `ubuntu-latest` with Python 3.13 and `contents: read` only.
`.github/workflows/filesystem-windows.yml` runs a non-fail-fast Python
3.11/3.12/3.13 matrix on `windows-latest`, also with `contents: read`. Its
gates include a reparse-point diagnostic, a junction-focused test, the Windows
filesystem security module, and the full suite. Security steps have no
`continue-on-error`. Both install the documented `.[dev]` extra.

The code baseline and the later documentation commit must not be conflated.
All executable verification in section 11 belongs to `797e2f5...`; this handoff
changes only Markdown and provides no new executable evidence.

## 2. Mission and product direction

AgentCore is intended to become a production-grade JARVIS-style personal
computer agent, not merely a chatbot or a loose tool collection. The target is
a low-latency interaction layer over a controlled agent runtime: text first,
then provider-neutral streaming, voice, turn detection and barge-in; bounded
context plus useful durable memory; autonomous multi-step execution with
verification and recovery; and eventually Gmail, calendar and other APIs,
browser/computer control, and background or scheduled work.

The desired interaction is natural and concise. A future user should be able to
speak, receive an early streamed response, interrupt immediately, delegate
multi-step work, and obtain truthful completion or uncertainty reports. That
experience depends on explicit turn ownership, cancellation, deadlines,
stale-result rejection, durable action evidence, resource cleanup, and
measured time-to-first-token/audio rather than presentation tricks.

The personality invariant is absolute: personality, voice, initiative, and
presentation sit above runtime and security authority. They may shape wording
and interaction but can never grant permission, choose or widen a capability,
bypass `ToolExecutor`, alter trusted state transitions, fabricate success,
override failed/unknown evidence, or convert model/tool/email/web data into
instructions. A convincing JARVIS persona is not an authorization mechanism.

## 3. Priority hierarchy and model allocation

The standing project priority is:

`Correctness -> security -> deterministic behavior -> recoverability -> concurrency safety -> performance/latency -> maintainability -> token efficiency -> development speed`

Codex is the primary implementation engineer for difficult repository work:
debugging, concurrency, cancellation, permissions, process/tool execution,
browser/PC control, cross-platform behavior, and multi-file security changes.
It must flag material architectural or security uncertainty rather than make a
silent choice.

During Codex cooldown, Arena/Claude may implement work whose architecture and
acceptance contract are already approved. They are also appropriate for
huge-context audits, adversarial review, test expansion, documentation, and
well-specified implementation. They must not improvise foundational decisions
about security, concurrency ownership, durable-state semantics, permissions,
process isolation, or computer control. Those decisions require an approved
architecture or escalation to the project lead.

Before a Codex availability window closes, preserve exact baselines, pending
audit status, known risks, test evidence, and the next bounded prompt. When
Codex returns, it must inspect every interim Arena commit and its complete diff
before reusing assumptions. Use the lowest reasoning level that preserves
quality: Low for narrow read-only/status work, Medium for isolated deterministic
changes or documentation, High for serious multi-file implementation and CI,
and Max for state ownership, concurrency, cancellation, process isolation,
destructive actions, browser/PC authority, or similarly foundational work.
Never downgrade genuinely difficult work to save tokens.

## 4. Current architecture and ownership boundaries

### Composition and public execution

`src/agentcore/core/agent.py` is the composition root.
`Agent.__init__` validates `AgentConfig`, constructs one `PermissionPolicy`, one
`ToolRegistry`, one canonical `WorkspaceGuard`, and one `ToolExecutor`, then
injects that executor into `TaskRunner`. It resolves model, planner, verifier,
recovery, memory/capture/resolver/conflict stores, and state-store dependencies.
`build_agent()` is the convenience factory. `Agent.execute()` calls
`create_task()` then `run_task()`; `run_task()`, `_execute_once()`, `_replan()`,
`checkpoint()`, and `resume()` own the synchronous autonomous lifecycle.
`resume()` checks the persisted workspace identity before adopting state.

The autonomous call graph is:

```text
build_agent / Agent
  -> Planner.plan or replan
  -> Task / Subtask
  -> Agent.run_task / _execute_once
  -> TaskRunner.run / _run_subtask
  -> ToolExecutor.invoke
  -> AgentState.record_tool_call
  -> Verifier.verify
  -> bounded RecoveryPolicy.decide when adverse
  -> checkpoint / JsonFileStateStore
```

`src/agentcore/core/runner.py:TaskRunner` executes dependency-ready subtasks
sequentially. Missing tool assignment becomes a pending human action, not fake
execution. A returned `ToolInvocation` is recorded in `AgentState`; only an
`ok` invocation supplies execution evidence. Verification follows execution.
`FAILED` and `UNVERIFIED` verdicts are adverse and may enter bounded recovery.
Recovery can request retry, replan, escalation, or stop; the outer `Agent`
owns replanning, preserving one executor and one set of gates.

### Conversation

`Agent.conversation()` constructs
`src/agentcore/conversation/runtime.py:ConversationRuntime` from the agent's
existing model, memory components, `AgentState`, configuration, session store,
and optional tool runner. `Agent._build_tool_runner()` gives
`ConversationToolRunner` the *same* executor, registry, and policy used by the
autonomous runner. `ConversationRuntime.send()` appends the user message,
retrieves bounded memory, performs a bounded synchronous model/tool/model loop,
captures only through the explicit memory policy, and saves the session.

```text
ConversationRuntime.send
  -> ConversationRuntime._generate / BaseModelProvider.generate
  -> model ToolCall(s)
  -> ConversationToolRunner.run_calls (sequential)
  -> ToolExecutor.invoke (the shared instance)
  -> Role.TOOL message framed as UNTRUSTED DATA
  -> next model request / final assistant Message
  -> ConversationSession / JsonFileSessionStore
```

`ConversationToolRunner` offers only enabled registry specifications, performs
a non-executing approval precheck when needed, bounds model-visible result
text, and routes valid calls into `ToolExecutor`. It does not implement a
second permission or filesystem policy. `conversation.max_tool_rounds` bounds
the loop. Tool output is a data-role message and cannot become a system
message. Current providers are still synchronous.

### One authoritative tool path

The sole execution authority is `src/agentcore/tools/executor.py:ToolExecutor`:

```text
ToolRegistry.get
  -> Tool.validate_args
  -> PermissionPolicy.check(required capabilities)
  -> Tool.run(ToolContext)
  -> ToolResult normalization
  -> structured logging
  -> ToolInvocation
```

`ToolRegistry` owns registration, enable/disable filtering, lookup, catalogue,
and capability queries. `Tool`, `ToolSpec`, `ToolParameter`, `ToolContext`, and
`ToolResult` in `tools/base.py` define validation and result contracts.
Capabilities come from the trusted tool specification; model-supplied
arguments cannot choose the workspace, permission level, capability, or
approval result. `PermissionPolicy.evaluate()` can describe an approval
requirement without action; `check()` produces the terminal allow/deny decision
and uses an `ApprovalGate` only where policy requires it.

The five default tools are `echo`, `workspace_info`, `read_file`,
`list_directory`, and `inspect`. The three filesystem tools require `FS_READ`
and delegate filesystem authority to one `WorkspaceGuard` created from the
canonical configured workspace. There is no write/delete/move/search/shell
tool.

### Planning, verification, and recovery

`src/agentcore/planning/planner.py:LLMPlanner` turns an objective into a
schema-validated `Plan`/`Task`, using the live tool catalogue and bounded,
untrusted memory context. It does not execute or authorize. Planning errors are
categorical. Replan signatures and ledgers reject repeated ineffective plans.

`src/agentcore/verification/verifier.py:Verifier` builds a
`VerificationContext`, prefers deterministic evidence, and invokes model
assistance only when configured and appropriate. It has no executor and never
grants permission. Malformed, uncertain, unreachable, or low-confidence model
judgment cannot forge a pass. `UNKNOWN`/`UNVERIFIED` is not success.

`src/agentcore/recovery/policy.py:RecoveryPolicy` deterministically classifies
evidence and chooses bounded `RETRY`, `REPLAN`, `ESCALATE`, or `STOP` according
to `RecoveryConfig` and `RecoveryHistory`. Permission denial is never retried
around. Retry safety is derived from real tool contracts; recovery cannot
approve, execute, erase errors, or relabel unknown work as success.

### Four separate state domains

| Domain | Owner and durable representation | Purpose |
|---|---|---|
| Autonomous run state | `AgentState`; `InMemoryStateStore` / `JsonFileStateStore` | One run's phase, task/subtask, bounded tool evidence, errors, verification, pending actions, resume point, history |
| Conversation session | `ConversationSession`; `InMemorySessionStore` / `JsonFileSessionStore` | Bounded recent user/assistant/tool protocol history for dialogue continuity |
| Durable memory | `MemoryRecord`; in-memory/JSON memory and conflict stores | Intentionally retained facts/preferences/workflows with scope, trust, provenance, correction and conflict handling |
| Realtime turn coordination | `TurnRecord`; `InMemoryTurnStore` / `JsonFileTurnStore` | Admission generation, lifecycle, cancellation outcome, stale-operation fence and restart reconciliation |

These stores link by opaque IDs where necessary but do not contain one
another. Conversation transcript does not silently become durable memory;
memory does not become run state; `AgentState` phases are not overloaded as
voice/interaction phases; realtime records contain no transcript or tool
payload. A future action journal will be a fifth separate evidence domain.

### Models and transport

`models/messages.py` defines provider-neutral `Message`, `Role`, `ToolCall`,
`Usage`, `ModelRequest`, and `ModelResponse`. `BaseModelProvider.generate()` is
the normalized blocking boundary; `complete()` is its string adapter.
`EchoProvider` and `OpenAICompatibleProvider` are current implementations.
`HttpClient`/`UrllibTransport` own synchronous HTTP, sanitized errors, per-I/O
timeout, retry policy and backoff. There is no stream API or cancellation
propagation. Model credentials are resolved from a named environment variable
at use time, never stored as values.

### Observability, filesystem, and privacy

`StructuredLogger`, `StructuredFormatter`, and `Events` centralize structured
events and recursive redaction. Logs should contain categorical outcomes and
safe identifiers, not secrets, raw tool content, or protected paths.

`WorkspaceGuard` in `fs/guard.py` owns path syntax, containment, sensitive-name
policy, link/reparse behavior, hardlink policy, descriptor-anchored file reads,
bounded directory listing, and metadata inspection. `InspectTool` delegates to
`WorkspaceGuard.inspect_path()`. Its safe schema is `path`, `kind`, `size`,
`links`, UTC `modified_at`, and categorical `link_target`; it excludes absolute
and resolved targets, inode/device, owner/ACL/mode, atime/ctime, bytes, and file
content. Workspace identity is deliberately persisted in `AgentState` and is
not itself a forbidden resolved-target leak.

### Realtime package status

`src/agentcore/realtime/` is an internal foundation added by Task 42.2. Its
`__init__.py` explicitly says it is not wired into runtime. A repository-wide
import trace finds no use from `Agent`, conversation, `TaskRunner`,
`ToolExecutor`, providers, permissions, filesystem tools, CLI, configuration,
or observability. It changes no live behavior yet.

## 5. Security and correctness invariants

1. `ToolExecutor` is the only tool execution authority. Every conversation and
   autonomous action must pass registry lookup, real argument validation,
   trusted capability evaluation, permission check, `Tool.run`, normalization,
   logging, and `ToolInvocation` in that order.
2. Validation precedes tool code. Capability, workspace, permission level,
   deadline/cancellation context, approval, and future action-journal context
   are trusted runtime inputs, never model arguments.
3. Permission checks cannot be bypassed or duplicated in runner-specific code.
   Approval is bound to the proposed action and does not grant a general
   capability. Blocked capabilities remain blocked.
4. Verification consumes evidence only. Recovery is deterministic/bounded and
   cannot execute, approve, rewrite failure, or convert `UNKNOWN` to success.
5. Sensitive direct paths and innocent links to sensitive targets reveal only
   stable categorical failure. No existence/size/time/type oracle, absolute or
   resolved target, host path, raw OS error, traceback, content, inode/device,
   owner/ACL/mode, or forbidden timestamp is exposed to model, state, sessions,
   logs, or recovery context.
6. The canonical workspace is pinned at composition and in autonomous state.
   Model arguments and ambient current-directory changes cannot replace it.
7. Transcript, tool/result messages, model output, and inspection metadata do
   not automatically become durable memory. Capture is explicit, bounded, and
   user-statement-oriented.
8. Durable realtime schema is exactly version 1. Records are immutable,
   bounded, UTC-aware, strictly parsed, and fail closed on malformed/corrupt or
   future-schema data. Arbitrary user/model content is not a durable note.
9. Realtime mutations use optimistic CAS: expected version plus an operation
   ID. A successful logical mutation increments version once, replays exactly,
   rejects operation-ID reuse with different intent, and never overwrites a
   newer record.
10. `(session_id, turn_id, generation, active_operation_id, expected_phase)`
    fences stale callbacks. Cancellation, expired deadline, terminal state,
    missing/corrupt store data, or a mismatched lease rejects acceptance.
11. Terminal turn state is immutable. Same terminal transition replay is
    idempotent; incompatible terminal changes fail. A terminal callback is
    never accepted for further work.
12. Restart never silently resumes a nonterminal realtime turn. Reconciliation
    terminalizes it truthfully. In particular, interrupted `FINALIZING` work
    that had requested `COMPLETED` becomes `FAILED`, not manufactured success;
    opaque action references remain evidence only.
13. One active logical turn per session is the target. At most one successor
    may queue. Coordinator-lock acquisition order—not timestamps—is “newest.”
14. `read_only` is an effect classification, not proof of thread safety;
    version 1 assumes no concurrent reads/tools merely because that flag is set.
15. One owning process controls a JSON turn-store domain. Atomic replacement is
    record durability, not multi-process exclusion. Inherited stores,
    coordinators, coordination locks, and lanes must fail before touching
    potentially poisoned post-fork synchronization.
16. No Task 42 live integration exists. Do not claim cancellation, turn
    admission, deadlines, or stale fencing protect present conversation/tool
    calls until a later approved stage connects them.

## 6. Completed task history

The older history below combines the prior durable handoff with functionality
still present in the current tree. Several pre-Task-38 commit objects are not
available in this checkout, so their historical short IDs are retained and are
explicitly not expanded or embellished.

| Task | Purpose and verified result | Commit record | Security/test significance |
|---|---|---|---|
| 31 | Established the baseline runtime architecture around explicit state, tasks, tools, permissions, configuration, logging and extension seams. | Historical baseline near `839efa3` | Composition and honest no-op seams precede advanced capabilities. |
| 32 | Built the workspace-relative filesystem security foundation. | Historical `5c74951` | Centralized lexical/containment/sensitive/link rules in `WorkspaceGuard`; security tests established. |
| 33 | Hardened descriptor/TOCTOU handling, hardlink defense and sensitive-target refusal. | Historical `3e2f7a7` | Read from a verified descriptor, reject unsafe link aliases, preserve categorical failures. |
| 34 | Added secure bounded UTF-8 `read_file`. | Historical `890b9d6` | `FS_READ`, strict decoding, special-file refusal, explicit truncation; executor-routed tests. |
| 35 | Added secure one-level `list_directory`. | Historical `40785ff` | Non-recursive, no link follow/open, sensitive names withheld, bounded output. |
| 36 | Made directory enumeration memory-bounded. | Historical `f957155` | Selection remains O(limit), protecting large-directory behavior. |
| 37 | Hardened the Windows junction test. | Historical `3ad3d6b` | Ensured reparse/junction behavior was exercised on a real Windows filesystem. |
| 38 | Added real Windows CI and reparse/portability corrections. | `05edc6e` plus follow-up portability commits in reachable history | Windows security behavior became gating rather than inferred. |
| 39 | Closed Windows ADS bypasses and hardened logging/state path privacy. | `7feb717` | ADS paths fail closed; sensitive tool arguments/log failures are redacted/categorical. |
| 40 | Expanded Windows 3.11/3.12/3.13 and true-symlink coverage; fixed monotonic event ordering. | Culminating baseline `f1466dcb7dd9d13d1fa7fec740043357ace4fab8`; clock fix `63be21b` | `utc_now()` is strictly increasing within process; cross-version Windows suite gates filesystem security. |
| 41.1 | Produced the architecture/contract for bounded metadata-only `inspect`. | Architecture report only; no separate code commit identified | Fixed safe fields, sensitive/link/root semantics before implementation. |
| 41.2 | Implemented `InspectTool`, `Inspection`, and `WorkspaceGuard.inspect_path()`. | `3bdb5adfc51e072a8f35f6b539b365b08a31bd92` | Metadata only, `FS_READ`, root supported without relaxing `read_file`, no resolved-target disclosure. |
| 41.3 | Verified inspect through real conversation/autonomous paths; added Linux gating and corrected one cross-platform state assertion. | Final `d425ebe0e143d57ab6319583cab7f21ba24e5a29` | Proved shared executor, validation/permission/privacy/persistence/memory/recovery boundaries; no production change. |
| 42.1 | Defined realtime turn, concurrency, cancellation, stream, deadline, action and migration architecture. | `13e525460624f601242ae768edd90694594751e4`, refined by `b308c82e04d0a38bb05954d4f2a57cb9cbb449a5` | Documentation only; approved ownership and safety contracts before executable work. |

Task 42.2 is intentionally not in the completed table. Its implementation and
CI are green at `797e2f5...`, but independent final confirmation remains the
acceptance gate.

## 7. Task 41.3 final state

Task 41.3 added `tests/test_inspect_agent_e2e.py`, then the Linux full-suite
workflow. It exercised the highest production composition paths with scripted
providers at the model boundary while retaining the real registry, executor,
argument validation, permission policy, `InspectTool`, `WorkspaceGuard`, and
serializers.

Conversation coverage followed user message -> `ConversationRuntime` -> model
`ToolCall("inspect")` -> `ConversationToolRunner` -> shared `ToolExecutor` ->
real `FS_READ` policy -> guard -> untrusted tool data -> second model request ->
final persisted assistant reply. Cases covered regular file, directory without
recursion, workspace root while preserving `read_file(".")` rejection,
external and broken links, missing/traversal failure, direct sensitive path,
innocent link to sensitive target, malformed/injected arguments, disabled
tool, allowed/blocked permission paths, exact untrusted-data role/framing, and
the tool-round bound.

Autonomous coverage entered through `Agent`/`TaskRunner`, proved the same
executor object and `FS_READ` policy, recorded truthful invocation evidence in
`AgentState`, verified without re-execution, and exercised bounded negative
verification/recovery. Actual `JsonFileSessionStore` and `JsonFileStateStore`
save/reload were checked. Memory was enabled non-vacuously and did not capture
tool metadata, `Role.TOOL`, or model output. Recursive privacy checks excluded
resolved/sensitive targets and forbidden metadata while allowing the explicitly
configured `AgentState.workspace` identity.

The Linux-only initial failure was a flawed test assertion: it broadly forbade
the pinned workspace path in serialized `AgentState`, contradicting the
established resume security contract. Commit `d425ebe...` narrowed only that
assertion and kept all resolved-target, sensitive-target, model-visible,
invocation, session, error, log, and metadata checks. No production file was
changed in Task 41.3. Linux and all Windows matrix jobs were green before Task
42 began.

## 8. Approved Task 42.1 architecture

The normative design is
`docs/architecture/task-42.1-realtime-turn-lifecycle.md`; this section is a map,
not a redesign.

- A `Turn` is one admitted user interaction, separate from autonomous
  `AgentState`. It has immutable `turn_<12 lowercase hex>` identity and a
  per-session generation. It may link to run/task/subtask/action IDs but does
  not contain those objects.
- Lifecycle phases are `CREATED`, `QUEUED`, `GENERATING`, `EXECUTING_TOOL`,
  `WAITING_APPROVAL`, `VERIFYING`, `RECOVERING`, `CANCELLING`, `FINALIZING`, and
  terminal `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`. Cancellation enters
  cleanup/finalization rather than pretending to be physically instantaneous.
- Version 1 supports one active logical turn per session, at most one queued
  successor, model serialization per `Agent`, action/tool serialization per
  `Agent`, and one autonomous run per `Agent`. Different sessions can be
  admitted independently. No read concurrency is inferred from `read_only`.
- Each callback/event carries session, turn, generation and operation identity;
  provider request/attempt/sequence fields further fence streaming. Stale work
  can be recorded as evidence where necessary but cannot mutate current
  session/turn outcomes.
- Cancellation is hierarchical: agent shutdown root -> turn -> model/tool/
  verification/recovery child. First reason wins atomically; listeners fire
  once outside locks; later reasons are bounded audit facts. Existing Python
  work is cooperatively cancelled; preemption is permitted only for owned,
  cleanly terminable resources.
- Streaming is provider-neutral, synchronous and pull-based initially. Common
  envelope fields are `turn_id`, `session_id`, `generation`, `operation_id`,
  `request_id`, `attempt_id`, and monotonically increasing `sequence`.
  Provisional text/tool fragments are bounded and never persisted as final
  messages. Complete strict JSON tool calls enter the existing executor only
  after a successful enclosing response.
- `Deadline` uses monotonic time for decisions and UTC only for durable audit.
  Child deadlines can shorten, never extend. Turn, model-request, tool, and
  cleanup budgets form one nested model. Current tool timeout settings are not
  enforced; migration must thread trusted contexts and remaining budgets before
  claiming preemption.
- A future action journal records validated-and-permitted `PREPARED` intent,
  `STARTED` immediately before first external effect, authoritative
  acknowledgement/receipt, and truthful terminal or `UNKNOWN` outcome. Only
  safe/read-only reconciliation may be separately admitted through
  `ToolExecutor`. An unknown external effect is never retried or declared
  successful without evidence.
- One process owns a turn/session store domain. Admission uses a short
  per-session lane and durable optimistic CAS. File replacement is atomic
  persistence, not writer exclusion. Store I/O and callbacks occur outside
  coordinator/session/model/action locks.
- Successor ordering is coordinator-lock acquisition order, never UI or wall
  timestamps. A new input cancels active work as `SUPERSEDED`; it replaces an
  existing successor, which terminates as `SUPERSEDED_BEFORE_START`. Every
  accepted input retains a durable terminal record.
- Future Gmail/external integration requires least-privilege OAuth, separate
  capabilities, resolved recipients, explicit send confirmation, action IDs/
  idempotency, redacted durable evidence, and untrusted incoming content.

The approved stages are: (1) internal foundation; (2) admission and one-shot
conversation integration; (3) deadlines and autonomous correlation; (4)
streaming core and first provider; (5) interruptible tools and action journal
under separate security review. Task 42.2 implements only stage 1 plus the
internal coordinator needed to prove admission semantics. It does not activate
stage 2.

## 9. Task 42.2 implementation at the code baseline

### Package and schemas

`src/agentcore/realtime/errors.py` defines categorical `RealtimeError`
subclasses for validation, transitions, operation conflict, store/not-found/
already-exists/version/ownership, coordinator failure, and cancellation.

`turn.py` defines `TURN_SCHEMA_VERSION = 1`, immutable `TurnIdentity`,
`TurnCounters`, `TransitionRecord`, `TurnSnapshot`, and `TurnRecord`. Durable
fields are identity/link IDs, generation, phase/version, transition ledger,
created/updated UTC timestamps, optional UTC deadline, cancellation/requested/
final outcomes, bounded counters, trusted action references, and active
operation ID. It strictly validates exact keys, enum types, ID formats, bounds,
timestamp order, transition contiguity, unique operation IDs, terminal
consistency, and schema equality. Durable notes are restricted to trusted
categorical values. Records do not contain prompt text, assistant output, raw
tool data, cancellation detail, locks, listeners, callbacks, monotonic values,
or provider objects.

`TurnController.transition()` loads current state, authenticates exact replay,
requires expected phase/version, clamps audit time against `updated_at`, builds
one legal immutable next record, and CASes it. A CAS loser reloads only to
recognize the same accepted operation; it does not replay a different intent
against new state. `accepts(OperationContext, expected_phase=...)` is a
fail-closed stale callback fence. Transition capacity reserves space for
cancellation/finalization so bounded history cannot strand nonterminal work.

`Deadline` stores monotonic and UTC expiries, computes expiry from the injected
`Clock`, and creates child deadlines using the minimum parent/requested budget.
`OperationContext` binds session, turn, generation, operation ID,
`CancellationToken`, and `Deadline` as trusted context. This context is not yet
propagated through any live provider/tool path.

### Cancellation

`cancellation.py` implements `CancellationReason` including
`USER_INTERRUPT`, `SUPERSEDED`, `SUPERSEDED_BEFORE_START`, `OPERATOR_CANCEL`,
`DEADLINE_EXCEEDED`, `AGENT_SHUTDOWN`, and `INTERNAL_FAILURE`.
`CancellationSource` owns a token, atomic first-winner reason, bounded ephemeral
detail, ordered registrations, secondary categorical reasons, and categorical
callback-error type names. `cancel()` claims the callback batch under one lock
then invokes outside it, isolating exceptions and continuing later callbacks.
Late registration observes cancellation once. `child()` propagates parent to
child only; close detaches registrations and is idempotent. The implementation
creates no worker thread and never force-kills work.

### Stores, CAS, and process ownership

`store.py:TurnStore` exposes `create`, `load`, `compare_and_swap`, `list_turns`,
shared `admission_coordination`, and close/context-manager lifecycle.
`InMemoryTurnStore` holds immutable records under a lock and enforces the same
fresh-record and CAS candidate contracts as JSON.

`JsonFileTurnStore` resolves and pins its directory at construction, so later
CWD changes cannot redirect records, temporary files, locking, or reopen
behavior. It shares a canonical-directory `_StoreDomain` within one process:
an OS ownership handle, reentrant store mutex, owner PID, reference count, and
one `_AdmissionCoordination`. The OS lock is nonblocking (`flock` on POSIX,
`msvcrt.locking` on Windows); another process fails categorically. Fork hooks
discard inherited domain caches in the child, and instance operations reject
PID mismatch before inherited mutex acquisition. Same-process store instances
share the domain and release the OS lock only at the last close.

JSON writes use strict deterministic serialization to a same-directory
temporary file, flush and fsync record contents, atomically `os.replace`, clean
temporary files, and on POSIX fsync the directory. If replacement succeeded but
directory fsync reports failure, an exact retry is idempotent. Windows retains
CAS/concurrent-writer integrity but does not claim the same sudden-power-loss
directory-entry guarantee. Corrupt JSON, invalid UTF-8, malformed/future schema,
or forged record relationships fail closed.

`reconcile_nonterminal_turns()` is explicit, not hidden in constructor/load.
Terminal records remain unchanged. Cancellation/deadline/internal-failure
evidence determines truthful terminal state. Other interrupted phases are
driven through cancellation/finalization to failure. A pre-crash
`FINALIZING` request for `COMPLETED` is reconciled to `FAILED` with internal
failure rather than manufacturing completion. Reconciliation uses deterministic
operation IDs and CAS, handles clocks behind persisted timestamps, preserves
action references/counters/evidence, and is itself crash-replay safe.

### Coordinator

`coordinator.py:TurnCoordinator` is the internal admission authority.
`TurnAdmission` returns immutable snapshots of admitted/current/queued and any
superseded records. Per-session `_AdmissionLane` instances live in the store
domain's shared `_AdmissionCoordination`, so multiple coordinators over the same
in-memory or JSON domain linearize together; different store domains remain
independent. FIFO admission lanes hold no store I/O lock.

Admission reads and validates the durable session, assigns the next generation,
creates a `CREATED` record before work, supersedes current active work, cancels
and terminalizes an older queued successor, and transitions the new record to
`QUEUED`. Stage-transition CAS races are reloaded and reclassified up to a
bounded eight conflicts; on exhaustion cleanup terminalizes the prepared turn
without overwriting newer state. ID collisions never authorize cleanup of an
unrelated record. Ambiguous post-write failure probes exact immutable identity
before cleanup.

The coordinator captures immutable `(coordinator owner PID, coordination owner
PID)`. `admit`, `_lane`, `_release_lane`, `_enter_lane`, and `_leave_lane` all
perform a lock-free owner-PID check before touching cached coordination or lane
synchronization. An inherited coordinator never recreates state or attempts
child-side cleanup of parent-owned coordination. A newly constructed child-side
store/coordinator may acquire the released OS domain normally.

Nothing in this section is connected to live `Agent`, conversation,
`TaskRunner`, `ToolExecutor`, model transport, verification, recovery, CLI,
permissions, filesystem, configuration, or capabilities.

## 10. Task 42.2 defect and hardening history

### Initial foundation - `f09164076e9c10b2c17f70909d0a6316f95b63bc`

The initial implementation added the six realtime modules and four focused
test modules. Independent audit then tested assumptions rather than accepting
test reports.

### Coordination hardening - `5c55fbe10019737ebb6eda3485d7798335f8d7a3`

1. **Coordinator-instance-local lanes.** Two coordinators over one store could
   concurrently observe the same maximum generation and create duplicate live
   admissions. The fix moved admission lanes into a store-domain shared
   `_AdmissionCoordination`. Tests run concurrent coordinators against memory
   and JSON, assert unique generations/one active-plus-successor, verify
   independent domains, and exercise race-safe empty-lane cleanup. Lesson:
   concurrency ownership follows the durable domain, not a façade instance.
2. **Admission versus stage transition.** Supersession could use a record that
   advanced after classification, lose the admission, or overwrite newer
   state. `_supersede_active()` now reloads, CASes, reclassifies conflicts, and
   fails after eight without lying. Tests inject `CANCELLING`, `FINALIZING`,
   and terminal transitions inside the active-record CAS boundary and verify
   newer state plus newly admitted record are preserved. Lesson: CAS conflicts
   require semantic reclassification, not blind retry.
3. **ID-collision cleanup.** A generated turn-ID collision could cause failure
   cleanup to mutate the unrelated existing record. `TurnAlreadyExistsError`
   now authorizes no cleanup; ambiguous creation cleanup requires exact created
   identity and immutable links. Collision and post-write-failure tests protect
   both cases. Lesson: cleanup authority must be proven, not inferred from an
   attempted identifier.
4. **Relative JSON directory CWD drift.** A relative store path could change
   meaning after CWD changed, separating lock/record/temp/reopen locations. The
   constructor now resolves one absolute internal directory. Tests change CWD
   across create/load/CAS/temp/reopen. Lesson: persist and lock against one
   canonical storage identity.
5. **Windows directory fsync limitation.** The implementation previously risked
   overstating durability. Documentation and code now promise record-content
   fsync plus atomic replacement everywhere and directory fsync on POSIX only.
   Tests cover replace-before-fsync idempotence and temp cleanup. Lesson:
   platform durability claims must match available primitives.

Fault-injection tests now fail immediately after each real JSON admission and
supersession persistence boundary, reopen, reconcile, and prove no duplicate
live turn, incorrectly live successor, or fabricated success.

### Inherited coordinator fencing - `6ea3e9efdb76e5e9047cf4dbb9022ffe505e0d62`

A forked child could inherit a coordinator whose shared coordination lock was
held by a vanished parent thread, then hang in `_lane()` before the store's PID
check. The fix added immutable coordination/coordinator owner PIDs and a
lock-free check before cached coordination-lock paths. A bounded POSIX fork test
holds the relevant lock across fork and requires categorical ownership failure.
Lesson: post-fork validation must precede every inherited userspace lock; a
later store fence is too late.

### Inherited lane cleanup fencing - `797e2f5c8087423d72a2e972adaa350b47b4393f`

The remaining reproduced defect occurred after initial PID validation: an
injected ID factory forked after the child entered admission control; store
access raised ownership error, but `finally: lane.leave()` could acquire an
inherited `Condition` held by a vanished parent thread and hang before guarded
`_release_lane()`. The fix routes every direct lane lock acquisition through
coordinator-owned `_enter_lane()` and `_leave_lane()`, each checking owner PID
before touching the condition. PID mismatch raises the original categorical
ownership error without child cleanup.

The POSIX-only regression begins a real admission, enters its lane, forks from
the ID factory while another parent thread holds `lane.condition`, and uses a
bounded pipe/select protocol. The child must report `TurnStoreOwnershipError`;
the vulnerable implementation times out rather than hanging the suite. Focused
helper tests prove enter/leave guards reject without calling underlying lane
methods, and parent lane accounting/cleanup remains valid. Lesson: an
ownership fence must cover cleanup and exceptional paths, not only entry.

## 11. Latest verification evidence for `797e2f5...`

| Scope | Result |
|---|---|
| Realtime focused suite | 384 passed, 3 skipped, 0 failed |
| Related state/clock/ID tests | 70 passed, 0 failed |
| Local full suite | 3,210 passed, 134 skipped, 0 failed |
| Linux Python 3.13 | 3,285 passed, 59 skipped, 0 failed |
| Windows Python 3.11 | 3,210 passed, 134 skipped, 0 failed |
| Windows Python 3.12 | 3,210 passed, 134 skipped, 0 failed |
| Windows Python 3.13 | 3,210 passed, 134 skipped, 0 failed |
| Static checks | `python -m compileall -q src tests` and `git diff --check` passed |
| Tree | Clean; local and remote `main` matched the code SHA |

CI evidence:

- [Linux Python 3.13 run](https://github.com/Nortan251/agentcore/actions/runs/32321278187)
- [Windows Python 3.11/3.12/3.13 matrix](https://github.com/Nortan251/agentcore/actions/runs/32321277682)

Linux executed the new POSIX inherited-lane regression; Windows skips only
POSIX-specific fork tests. These counts are evidence for the code baseline,
not for the documentation-only containing commit.

## 12. Independent audit status and final confirmation scope

Earlier independent audits found and reproduced the store-domain coordination,
stage-race, cleanup-authority, CWD, inherited coordination-lock, and inherited
lane-cleanup issues. The first two hardening audits accepted their fixes only
after additional adversarial race/crash tests. Commit `797e2f5...` addresses the
last reported inherited-lane cleanup defect, and Linux executed its regression
successfully.

One final independent read-only confirmation of exactly
`797e2f5c8087423d72a2e972adaa350b47b4393f` remains pending. Task 42.2 must not
be declared complete until the verdict is exactly:

`FINAL CONFIRMATION ACCEPTED — TASK 42.2 COMPLETE`

The audit is narrow: compare `797e2f5...` with parent `6ea3e9e...`; inspect
`realtime/coordinator.py` and `test_realtime_coordinator.py`; prove every direct
`_AdmissionLane` condition/lock acquisition, including exceptional cleanup, is
preceded by lock-free owner-PID validation; prove no inherited path can hang or
mask ownership failure; verify the regression truly holds the lane condition
across fork and is bounded; confirm normal parent/shared-domain/lane cleanup and
new child-side construction remain supported; and confirm no race, lock leak,
test-only production hook, runtime integration, public API change, or Task 42.3
scope entered. It is a read-only code/test audit, not another implementation
pass.

## 13. Accepted limitations and deferred risks

These are accepted scope limits, not claims that the features work:

- No live realtime admission, generation fencing, or turn persistence is wired
  to conversation or autonomous execution.
- No provider streaming, token/tool-call delta accumulator, backpressure, or
  partial-output persistence contract is implemented in code.
- No cancellation token reaches current model, transport retry, tool,
  verifier, recovery, approval, or `TaskRunner` boundaries.
- `ToolConfig.default_timeout_seconds` and tool retry settings are not enforced
  by `ToolExecutor`; current calls are synchronous and cannot be force-stopped.
- No voice input/output, VAD, wake word, turn detection, TTS, audio barge-in, or
  audio-device recovery exists.
- No Gmail, calendar, or external API integration; no OAuth/token store.
- No action journal, effect classes, idempotency key plumbing, acknowledgement
  boundary, or mutation reconciliation.
- No mutation filesystem tools; no browser, PC control, shell/process
  execution, or network capability implementation. Declared capability enum
  values are vocabulary, not implemented authority.
- No scheduler, background execution, or multi-run worker.
- No database, ORM, Redis, vector store, or embedding backend. Persistence is
  in-memory/JSON and deterministic retrieval.
- Version 1 supports one owning process per JSON turn-store domain. It is not a
  distributed lock or multi-process service.
- Windows cannot make the same directory-entry power-loss durability claim as
  POSIX directory fsync. Atomic/CAS concurrency integrity remains claimed.
- Existing filesystem parent-directory replacement TOCTOU is not completely
  eliminated on every platform; descriptor verification and POSIX
  `/proc/self/fd` are defense-in-depth, not universal transactional identity.
- Filesystem inspection is a metadata snapshot; concurrent rename may change
  which object it describes. Sensitive-path policy is filename/path policy,
  not content DLP. Hardlink rejection may create conservative false positives.
- Reparse coverage includes junctions and true symlinks but not every possible
  Windows reparse tag or cloud-placeholder behavior.
- Existing model transport has per-attempt timeout/retries, not one shared turn
  deadline. A provider that ignores future cancellation will require lane
  quarantine and stale fencing.
- Current state/session/memory JSON stores predate realtime strict CAS and do
  not thereby gain multi-process safety. Do not copy weaker legacy store
  semantics into realtime work.
- Git history older than the reachable Task 38-era chain is incomplete in this
  checkout; historical short IDs above come from the durable prior handoff and
  current functionality, not newly reconstructed commit objects.

Any defect found by final confirmation is a bug and must be fixed before Task
42.3. The limits above should instead be assigned to the approved future stage
that owns them.

## 14. External-reference lessons routed to future stages

Public-repository reviews supplied patterns, not code to copy:

| Lesson | Required future owner |
|---|---|
| Resume must not re-execute already acknowledged work; recovery starts from durable evidence. | Task 42.5 action journal and later autonomous checkpoint integration |
| Cancellation must propagate through coordinator, provider, retry sleep, executor, tool, verifier/recovery and session commit boundaries. | Task 42.3 admission first; Task 42.4 streaming; Task 42.5 tools/actions |
| Providers may emit late/buffered events after cancellation; generation/request/attempt/sequence fences must reject them. | Task 42.4 streaming provider adapter and consumer |
| Text and tool-call fragments can interleave; accumulators need per-index/call identity, bounds, ordering and explicit completion. | Task 42.4 stream schema/accumulator |
| Approval must bind to an exact action ID and canonical content hash, not a vague capability or natural-language summary. | Task 42.5 action journal/approval integration |
| External mutations need idempotency and must preserve `UNKNOWN` when acknowledgement is ambiguous. | Task 42.5 and each later external capability |
| Incoming email/API/page content is untrusted data, not authority or system instruction. | Gmail/API stage and browser stage |

None of these lessons authorizes adding the feature during the pending audit or
Task 42.3. Each must enter through the existing executor/permission boundary in
its assigned reviewed stage.

## 15. Future Gmail and external API contract

Gmail is not implemented in Task 42.1, Task 42.2, or the next integration
slice. A later approved design must satisfy all of the following:

1. Use OAuth with least-privilege, user-granted scopes. Passwords, access/
   refresh tokens and secret values never appear in source, normal config,
   prompts, logs, session/run/turn state, memory, or action records. A secure
   credential facility is a separate reviewed dependency.
2. Model separate capabilities for read, draft, send, modify, and delete.
   `EMAIL_SEND` remains blocked by default until an explicit security change.
3. Resolve recipient identities before action. Sending requires explicit
   confirmation by default, bound to the exact action ID plus canonical
   recipient/subject/body hash. Any material change invalidates approval.
4. Use the same action ID/idempotency key for retry where the provider supports
   it, and persist provider message identifiers/receipts as authoritative
   evidence to prevent duplicate sends.
5. Persist only durable redacted intent/result records. Never store raw secrets;
   bound message content according to separately approved privacy policy.
6. Distinguish “not submitted” from “submission outcome unknown.” Cancellation,
   timeout, crash, or transport return after `STARTED` without decisive provider
   acknowledgement is `UNKNOWN`, not failure-safe-to-retry and not success.
7. Treat incoming email/API text, attachments, quoted instructions and links as
   untrusted data. They cannot grant permission, change system rules, choose a
   capability, or authorize follow-on actions.
8. Route every external action through `ToolRegistry`/`ToolExecutor`, real
   validation, capability permission, future action journal, verification, and
   bounded recovery. No provider-specific shortcut may execute beside this
   path.

## 16. Exact next step

The first action for Arena/Claude is the final independent, read-only
confirmation audit of code commit
`797e2f5c8087423d72a2e972adaa350b47b4393f`, using the scope in section 12. If
accepted, stop and report the exact verdict to the project lead. Do not begin
Task 42.3 without a separate approved implementation prompt.

After acceptance, the expected engineering stage is Task 42.3: admission and
one-shot conversation integration from the approved staged plan. Its boundary
is to compose `TurnCoordinator` at `Agent.conversation`, create/admit before a
send, serialize session mutation, fence one-shot model results, persist final
turn state, and integration-test simultaneous input/supersession/session reload
while retaining the synchronous provider compatibility path. It does not
authorize streaming, voice, general tool interruption, action journal,
mutation capabilities, browser/PC control, or a redesign of Task 42.2.

## 17. Zero-placeholder Arena/Claude continuation prompt

```text
You are performing the final independent, read-only confirmation of AgentCore
Task 42.2.

Repository: private AgentCore repository, branch main.
Code target: 797e2f5c8087423d72a2e972adaa350b47b4393f
Compare against parent: 6ea3e9efdb76e5e9047cf4dbb9022ffe505e0d62
Handoff: docs/handoffs/agentcore-master-engineering-handoff-2026-08-20.md
Architecture: docs/architecture/task-42.1-realtime-turn-lifecycle.md

Do not edit files, run formatting, commit, push, or begin Task 42.3. Verify the
repository is clean and HEAD is the target. Read the handoff as orientation but
trust actual code and tests over its claims.

Inspect the complete target diff, especially:
- src/agentcore/realtime/coordinator.py
- src/agentcore/realtime/store.py where ownership coordination is defined
- tests/test_realtime_coordinator.py

Confirm:
1. Every coordinator path that can directly acquire an _AdmissionLane lock or
   condition, including lane enter and exceptional lane cleanup/leave, performs
   a lock-free immutable owner-PID check first.
2. No inherited-coordinator path can touch a poisoned coordination/lane lock,
   hang before ownership rejection, attempt child cleanup of parent state, or
   mask TurnStoreOwnershipError.
3. The fence preserves parent behavior, shared same-process store-domain
   coordination, lane user counts/cleanup, and newly constructed supported
   child-side store/coordinator behavior.
4. The POSIX regression truly starts admission, enters the relevant lane,
   forks from the injected ID factory while another parent thread holds the
   lane condition, and uses bounded pipe/select synchronization that would time
   out against the prior implementation. Keep Windows exclusion narrow.
5. No new race, lock leak, stale-PID issue, swallowed ownership error, state
   corruption, test-only production hook, public API change, or scope expansion
   was introduced.
6. No Agent, conversation, TaskRunner, ToolExecutor, provider, permission,
   filesystem, workflow, dependency, configuration, capability, or Task 42.3
   behavior changed.

Try to disprove the fix; do not assume green CI proves correctness. Report only
concrete findings with severity, exact symbol, failure sequence, impact, and
smallest safe correction. If there are no blocking findings, end exactly:
FINAL CONFIRMATION ACCEPTED — TASK 42.2 COMPLETE
Otherwise end:
FINAL CONFIRMATION FIXES REQUIRED

Stop after the audit and report to the project lead. If a later prompt
authorizes Task 42.3 implementation, follow the approved Task 42.1 staged
architecture and the accepted Task 42.2 contracts; do not redesign the
foundation or broaden scope without project-lead approval.
```

## 18. Codex-reset return procedure

1. Establish baselines: code-under-review
   `797e2f5c8087423d72a2e972adaa350b47b4393f`; handoff is this file's containing
   commit resolved with the command in section 1. Verify local/remote branch
   identity and clean user-owned changes before doing anything.
2. Read the final confirmation verdict. Task 42.2 is complete only if it says
   `FINAL CONFIRMATION ACCEPTED — TASK 42.2 COMPLETE`. Otherwise reproduce and
   resolve each actionable finding under a separately bounded Task 42.2 fix.
3. Inspect every Arena/Claude commit after the handoff: ordered log, stats,
   complete patches, affected instruction files, security boundaries and
   working-tree state. Do not reuse an old assumption simply because a task
   number matches.
4. Rerun the realtime focused suite, related state/clock/ID tests, compileall,
   diff/privacy checks, full local suite, Linux Python 3.13, and Windows Python
   3.11/3.12/3.13 for any executable interim change. Documentation-only commits
   need documentation checks, not performative full-suite reruns.
5. Reconcile audit findings against the approved architecture. Verify no drift
   in ToolExecutor authority, permissions, state separation, filesystem
   privacy, CAS/idempotency, terminal/unknown semantics, process ownership, or
   fork fencing.
6. Only after acceptance and a separate project-lead prompt, assign Codex the
   highest-leverage difficult next task. The expected next stage is the narrow
   Task 42.3 one-shot conversation admission integration; streaming, voice,
   tool interruption and actions remain later stages.

This handoff adds no runtime behavior, test, workflow, configuration,
dependency, permission, capability, or Task 42.3 implementation.
