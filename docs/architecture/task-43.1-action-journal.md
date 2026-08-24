# Task 43.1 — Durable action journal and effect reconciliation architecture

Status: **authoritative Stage 5 architecture**

Date: **2026-08-24**

Baseline: `784ab048cb9d68a0635d6beace3972f6e14de960`

This document freezes the contract for durable, external, privileged, or
meaningfully ambiguous actions before AgentCore gains email send, calendar
mutation, browser submission, desktop control, scheduled actions, or other
mutation-heavy capabilities. Reversible session-local UI/media control remains
on a low-latency non-journaled path. This is an architecture contract only;
Task 43.1 adds no production implementation.

This document supersedes only the *proposed, not implemented* side-effect
state names and `ActionBoundary` sketch in section F of
`task-42.1-realtime-turn-lifecycle.md`. The Stage 3 turn lifecycle and all
Stage 4 streaming contracts remain unchanged.

## 1. Scope, authority, and guarantee

The ActionJournal is a separate durable domain that records what AgentCore was
authorized to try, which meaningful effect attempts may have run, what
durable/external truth is known, and what must be reconciled. It is not a log
of every visible control, conversation history, run state, memory, or a
`TurnRecord` extension.

The target is deliberately narrower than distributed exactly-once delivery:

- **at-most-once** where AgentCore can prove it;
- **idempotent retry** where a documented external guarantee makes retry safe;
- **truthful ambiguity plus reconciliation** everywhere else.

AgentCore does not claim exactly-once effects across arbitrary external
systems. A local journal and a remote service do not share a transaction. No
state-machine label can remove that boundary.

Authority remains exactly:

```text
ToolRegistry.get
  -> Tool.validate_args
  -> PermissionPolicy.check / required user approval
  -> ToolExecutor-controlled action journal boundary, when the trusted
     semantic action kind is journal-eligible
  -> Tool.run
  -> normalization/logging
  -> ToolInvocation
```

`ToolExecutor` remains the sole tool-execution authority. ActionJournal records
authority evidence; it never grants permission, calls `Tool.run`, interprets
model text as approval, or becomes a shadow executor. A recovery coordinator
may select records that need attention, but all reconciliation or resumed
execution still enters a trusted `ToolExecutor` path.

AgentCore remains one user-facing JARVIS runtime. A recovery worker is an
internal runtime implementation detail, not an agent identity or a multi-agent
architecture.

The journal has a fully local durable implementation path. It requires no paid
model or hosted coordination service. Paid/cloud providers may supply an
effect or reconciliation API, but they are never journal authority.

## 2. Current-runtime findings and compatibility decisions

The baseline establishes these facts:

1. `ToolExecutor.invoke()` already owns registry lookup, argument validation,
   permission resolution, two trusted cancellation checkpoints, `Tool.run`,
   result normalization, and invocation logging.
2. `ToolInvocation` is created only after `Tool.run` returns. It has no durable
   invocation identity and cannot prove pre-effect intent.
3. `AgentState.record_tool_call()` also occurs after execution. It is bounded
   run history, not an effect journal.
4. `PendingAction` represents a proposal or approval wait. It can share the
   lexical `act_` prefix, but it is not an ActionJournal record and grants no
   authority.
5. `ToolSpec.read_only` currently provides only a read/mutation split. It does
   not distinguish ephemeral control from durable mutation and does not
   describe idempotency, commit boundaries, external identifiers, or
   reconciliation. A future trusted action-kind registration supplies that
   semantic classification; model arguments never do.
6. `TurnRecord.action_refs` is bounded and immutable under current CAS rules.
   Most conversation actions are discovered after turn admission, so the
   ActionJournal's `turn_id` link is authoritative. Task 43.1 does not change
   `TURN_SCHEMA_VERSION` or require a reciprocal late append to `TurnRecord`.
7. Turn cancellation and stale-result fencing are logical truth. They neither
   prove nor undo an external effect. `TurnBridge`, `TurnCoordinator`, and
   `StageLease` must remain unchanged.
8. Existing JSON state/session stores use separate domains and atomic replace;
   the realtime `TurnStore` adds strict validation, CAS, record fsync, and
   single-process ownership. ActionJournal needs its own stronger effect-aware
   store contract rather than reusing any of those records.

These are implementation gaps assigned to a later task, not contradictions
with the architecture. There is no unavoidable Stage 4 contradiction.

## 3. Terms

**Logical action** is one user- or workflow-intended mutation, such as sending
one particular message or moving one particular file. The durable `action_id`
names it across retries and restarts.

**Ephemeral control** is a reversible, session-local UI or media operation
whose loss/repetition after restart does not create meaningful durable,
external, account, security, privilege, or reconciliation truth. It remains a
normal tool invocation but is not an ActionJournal logical action.

**Invocation** is one caller's entrance to `ToolExecutor`. Duplicate callers
may have distinct invocation IDs while attaching to the same logical action.

**Attempt claim** is a durably reserved opportunity to call the mutation seam
once. Its `attempt_id` is written before the seam can be invoked.

**Mutation seam** is the trusted boundary after which a journal-eligible tool
may cause a durable, external, privileged, long-running, or meaningfully
ambiguous mutation. In the first safe integration, the entire `Tool.run` call
for a journal-eligible tool is treated as that seam. Capability-specific
adapters may later narrow the boundary, but may never move it ahead of durable
attempt persistence.

**Physical invocation** means the mutation seam was actually entered. An
attempt claim is necessary for it but, because the process can crash between a
local durable write and the next instruction, the claim alone is not proof
that it happened.

**Remote effect** is the external reality, which may have a provider-assigned
identifier or receipt. It is not the same as a local attempt or transport
response.

**Reconciliation** is a read-only, capability-specific probe that turns remote
or local evidence into a categorical verdict. It does not retry or compensate.

## A. Action identity

### A.1 Durable identities

| Identity | Meaning | Retry/restart lifetime |
|---|---|---|
| `action_id` | One logical mutating action. Uses the trusted `act_` namespace already reserved by realtime records. | Stable across safe retries, reconciliation, duplicate callers, and process restarts. |
| `invocation_id` | One entrance to `ToolExecutor` by a conversation, run, recovery path, or scheduler. | Fresh per caller entrance; persisted as correlation, never as action identity. |
| `attempt_id` | One durable claim permitting at most one invocation of the mutation seam. Use a namespace distinct from Stage 4 model transport attempts. | Fresh for every possible physical effect attempt; never reused. |
| `external_effect_id` | Provider/resource/receipt identity observed from the external system. | Provider-defined; stored only when safe and useful for verification. |
| `idempotency_key` | Stable key sent to an external API under a documented idempotency contract. | Stable for the entire `action_id`; never regenerated on retry. |
| `turn_id` | Originating realtime turn, when any. | Immutable provenance only. Turn terminalization does not terminalize action truth. |
| `operation_id` | Originating trusted turn operation/fence correlation, when any. | Correlation for that operation only; never a deduplication or authority key. |
| `run_id`, `task_id`, `subtask_id` | Autonomous-work provenance, when any. | Immutable links, not action identity or permission. |
| `schedule_occurrence_id` | Future scheduler's stable identity for one due occurrence. | Optional provenance and a source for a trusted deduplication key. The journal is not the scheduler. |

There is no separate `logical_action_id`. `action_id` is the logical identity.
Adding a second synonym would invite retry code to choose the wrong one.

For every actual physical invocation, exactly one durable `attempt_id` must
already exist. The converse is intentionally not claimed: cancellation or a
crash can occur after an attempt claim is durable and before the mutation seam
is entered. A live executor may then close that attempt as `NOT_INVOKED`; after
a crash it is conservatively `POSSIBLY_INVOKED`. `attempt_id` existence alone
never proves a remote effect.

Stage 4 model-stream `request_id` and `attempt_id` remain their own transport
domain. They are not action IDs or action-attempt IDs.

`idempotency_key` is stored separately from `action_id` because providers have
different length, character, and scoping rules. It may equal `action_id` only
when that is valid for the provider; otherwise trusted code derives a stable,
domain-separated provider-safe value. It never becomes a second logical action
identity.

### A.2 Kind, capability, and target

Every record has a stable, versioned `action_kind`, such as conceptual
`filesystem.move.v1` or `calendar.create.v1`, plus the registered tool name,
tool version, exact mutation capability set, and trusted journal-eligibility
class. A reconciler separately declares its minimal read/probe capability set.
Core code keys classification and reconciliation adapters by `action_kind` and
compatible tool version, never by model text or a model-selected argument.

The durable intent contains:

- a canonical intent digest;
- an opaque protected `intent_ref` when later execution or reconciliation
  needs sensitive values;
- a categorical target type;
- an optional installation-keyed target fingerprint;
- no raw target, recipient, path, body, or page content in the public record.

A plain hash is not safe for low-entropy values such as an email address. A
target fingerprint exposed to generic logs must be an installation-keyed
digest. If that facility is unavailable, omit the fingerprint and use safe IDs
and categories only.

### A.3 Deduplication identity

`action_id` is generated by trusted runtime code, never copied from a model or
provider. An optional unique pair
`(deduplication_scope, deduplication_key)` maps duplicate trusted callers to
the same action. The key is minted by trusted orchestration:

- a conversation may use turn ID plus trusted model-round and call ordinal;
- a durable workflow may use workflow-step identity;
- a scheduler may use schedule identity plus occurrence identity;
- an API may use an authenticated caller request ID.

A provider/model tool-call ID is untrusted correlation only. Arguments are not
automatically hashed into deduplication identity: two deliberate sends to the
same recipient with identical content can be two legitimate actions.

An automatic safe retry retains `action_id`, deduplication identity, and
idempotency key, while receiving a fresh `attempt_id` and `invocation_id`.

A deliberate retry after a terminal action is a new `action_id` linked by
`predecessor_action_id`. If a human authorizes a potentially duplicating retry
of an unresolved non-idempotent action, the original remains
`RECONCILE_REQUIRED`; the new record carries the predecessor link and a durable
duplicate-risk acknowledgement. History is never rewritten to pretend the
first action failed.

The existing `PendingAction.id` is a proposal/approval identifier despite its
legacy `act_` spelling. It must never be accepted as a journal action merely
because it matches the syntax. Future integration binds a distinct
`approval_request_id` to a journal action only after final approval.

## B. Closed durable state machine

The schema has exactly six action states.

| State | Meaning | Terminal? |
|---|---|---|
| `AUTHORIZED` | Validated canonical intent and final authority receipt are durable. No attempt currently owns execution. | No |
| `IN_FLIGHT` | An attempt claim is durable. The mutation seam may have been invoked, so effect truth must be established before unsafe retry. | No |
| `RECONCILE_REQUIRED` | Full outcome is unknown, partially known, or inherited from an interrupted `IN_FLIGHT` record. Physical execution is blocked while evidence is gathered or human input is required. | No; it may remain unresolved indefinitely |
| `SUCCEEDED` | Trusted evidence confirms the complete intended effect. | Yes |
| `FAILED` | Trusted evidence confirms the complete intended effect was not achieved. A fixed effect verdict distinguishes no effect from confirmed partial effect. | Yes |
| `CANCELLED` | Execution was prevented before the mutation seam. Every claimed attempt, if any, is conclusively `NOT_INVOKED`. | Yes |

There is no `PLANNED` state. A model plan is not authority and does not belong
in the journal. There is no separate `UNKNOWN` state: unknown effect truth is
represented by `RECONCILE_REQUIRED`. There is no `ACKNOWLEDGED` state: receipts
are evidence, not lifecycle authority.

### B.1 Legal transitions

```text
AUTHORIZED
  -> IN_FLIGHT
  -> CANCELLED
  -> FAILED                 # decisive local failure, no physical invocation

IN_FLIGHT
  -> SUCCEEDED              # complete effect confirmed
  -> FAILED                 # no effect or exact partial effect confirmed
  -> RECONCILE_REQUIRED     # outcome/extent ambiguous
  -> AUTHORIZED             # same-action retry is proved safe
  -> CANCELLED              # only if this owner proves seam was not invoked

RECONCILE_REQUIRED
  -> SUCCEEDED              # reconciliation confirms complete effect
  -> FAILED                 # reconciliation confirms no/full failure or exact partial effect
  -> AUTHORIZED             # reconciliation/guarantee proves same-action retry safe
  -> RECONCILE_REQUIRED     # bounded new probe evidence or human-required disposition
```

`SUCCEEDED`, `FAILED`, and `CANCELLED` have no outgoing transitions. A later
request is a new linked action.

`IN_FLIGHT -> AUTHORIZED` is allowed only when the record contains a checked
retry proof: a definitive no-effect verdict, or a still-valid external
idempotency guarantee using the same key. A transient exception or timeout is
not such proof.

`IN_FLIGHT -> CANCELLED` is deliberately narrow. It is valid only while the
same live owner holds the action execution guard and can affirm that its final
checkpoint stopped the mutation seam. After owner loss, `IN_FLIGHT` always
becomes `RECONCILE_REQUIRED`, never `CANCELLED`.

Every transition uses monotonic record versioning and append-only categorical
evidence. A terminal record cannot be rewritten by cancellation, turn
supersession, a model, a verifier, or a later caller.

### B.2 Effect verdict is separate from action state

Each completed attempt or reconciliation probe produces exactly one trusted
effect verdict:

- `CONFIRMED_APPLIED`: the full intended effect is verified;
- `CONFIRMED_NOT_APPLIED`: the intended effect did not occur;
- `CONFIRMED_PARTIAL`: a bounded, explicit subset occurred;
- `OUTCOME_UNKNOWN`: evidence cannot determine whether or how much occurred.

`CONFIRMED_PARTIAL` leads to `FAILED` only when the partial extent is itself
known; otherwise it leads to `RECONCILE_REQUIRED`. `FAILED` never means
“nothing happened” unless its effect verdict is `CONFIRMED_NOT_APPLIED`.

The generic `ToolResult.ok` flag, an HTTP status, exception type, timeout, or
model judgment is not by itself an effect verdict. A trusted capability adapter
must establish the verdict.

## C. Intent before effect

No journal-eligible durable, external, privileged, long-running, or
meaningfully ambiguous effect may occur before an `AUTHORIZED` record and an
`IN_FLIGHT` attempt claim are durably readable. Ephemeral controls use the
separate fast path frozen in section N and do not create these records.

The future `ToolExecutor` flow is frozen as:

```text
1. ToolRegistry.get
2. Tool.validate_args
3. pure/local capability preflight and canonical effect description
4. trusted cancellation/currentness checkpoint
5. PermissionPolicy.check and required user approval
6. trusted cancellation/currentness checkpoint
7. create-or-load AUTHORIZED ActionRecord
8. acquire the per-action execution guard
9. CAS: append fresh attempt claim and transition to IN_FLIGHT
10. durably flush, reload, and verify the exact claim
11. final cancellation/currentness checkpoint
12. invoke the mutation seam at most once
13. normalize a typed effect verdict and safe evidence
14. CAS the action result; never repeat step 12 to repair a journal write
15. only after durable result publication, return the ToolInvocation
```

Steps 8–14 may repeat for the same logical action only after a legal transition
back to `AUTHORIZED`. Each loop consumes current retry/deadline/authority
checks, allocates a fresh attempt claim, and retains the same action and
idempotency identities.

Steps 3 and 5 may inspect configuration or credentials through read-only local
services, but may not mutate external state. Credentials are resolved into
ephemeral execution context and never copied into an action descriptor.

If the durable create/claim operation reports an uncertain write outcome, the
executor must reload under store ownership and prove the exact record and
version. If it cannot, it does not invoke the mutation seam. A conservative
orphan `IN_FLIGHT` record is acceptable; an unjournaled effect is not.

The store's general mutex/CAS lock is released before step 12. Only the
per-action execution guard remains held across the physical call. No
coordinator, session, turn-store, journal-store, or admission lock may be held
while external I/O blocks or a cancellation close callback runs.

In the initial implementation, placing the boundary around the entire
`Tool.run` of every mutating tool is conservative and correct. A crash during
tool preflight may then require unnecessary reconciliation, but it cannot
cause an unrecorded effect. Narrower capability boundaries require a trusted
executor-created permit and may only improve precision.

One v1 action declares one logical commit boundary. A capability that performs
multiple independently visible mutations must split them into separate
actions when possible. If it cannot, it must report bounded partial evidence;
the journal does not pretend that a multi-step external operation is atomic.

### C.1 Crash windows

| Window | Durable truth and recovery |
|---|---|
| Before final permission allow | No ActionRecord and no effect. |
| After `AUTHORIZED`, before attempt claim | Intent exists; no attempt may have run. Resume only after current authority/owner policy permits it. |
| After `IN_FLIGHT` claim, before seam invocation | Live owner may prove `NOT_INVOKED`; after crash the record is ambiguous and must reconcile. |
| During physical invocation | `IN_FLIGHT`; cancellation may interrupt a resource but cannot assert rollback. |
| After remote success, before local result persistence | `IN_FLIGHT`/`RECONCILE_REQUIRED`; reconcile by external evidence, never blindly repeat. |
| After durable `SUCCEEDED` | Complete effect truth survives any turn cancellation or process crash. |

## D. Effect result and evidence

The transport/API response is ephemeral input to a trusted normalizer. The
journal persists a fixed result shape, not an arbitrary provider dictionary:

- effect verdict and categorical result/failure class;
- normalized, bounded result summary needed by the caller;
- safe external resource/receipt identifiers or an opaque protected evidence
  reference;
- provider request ID only when it is non-secret and useful for support;
- idempotency key use/guarantee version and retention horizon, when applicable;
- timing, counts, and attempt/action correlation;
- exact confirmed partial-effect categories, when any.

The journal does not persist credentials, OAuth tokens, cookies, authorization
headers, raw provider responses, raw exception text, arbitrary response
metadata, sensitive message bodies in the public record, arbitrary browser
page content, or unrestricted tool output.

Some capabilities require sensitive intent to resume safely—for example a
recipient and message body. That data lives behind an opaque `intent_ref` in a
separately protected, access-controlled journal payload owned by the action
domain. It never enters generic serialization, logs, metrics, prompts, or
observability. Credentials are excluded even from that payload and are
re-resolved at execution time. If protected storage is unavailable, corrupt,
or unreadable, the mutation fails closed.

AgentCore may tell the caller “done” only after `SUCCEEDED` is durably
published. If remote success is known in memory but publishing it fails, the
executor does not call the effect again and does not claim durable completion;
it surfaces an ambiguous/reconciliation outcome.

## E. Unknown or ambiguous outcomes

Ambiguity is first-class. These all produce `RECONCILE_REQUIRED` unless a
documented idempotency guarantee independently makes same-action retry safe:

- connection loss or timeout after the mutation seam was invoked;
- process death while `IN_FLIGHT`;
- provider response that does not establish whether the effect committed;
- cancellation while a remote request was in progress;
- a partial result whose exact external extent is unknown;
- loss of the local success write after a remote success;
- an unrecognized capability/tool version needed to interpret old evidence.

`RECONCILE_REQUIRED` blocks physical execution for that action. A model, generic
verifier, retry flag, or operator convenience cannot convert it to failure and
thereby make retry appear safe.

If automated reconciliation remains inconclusive, the record remains
`RECONCILE_REQUIRED` with a categorical disposition such as
`PROBE_LATER`, `HUMAN_CONFIRMATION_REQUIRED`, or `UNRESOLVABLE`. Those are
dispositions, not extra lifecycle states.

A trusted human may provide evidence that resolves the original action. A
human request to “try again anyway” does not resolve the original. It creates a
new linked action with explicit duplicate-risk acknowledgement and passes
normal validation, permission, and approval again.

## F. Idempotency and retry classes

Every mutating `action_kind` declares one of three effect classes. The class is
trusted registration metadata, not model input.

### F.1 Externally idempotent

The external system documents a key-based or resource-identity guarantee that
makes repeated requests equivalent within a known scope and retention window.

- Generate one idempotency key per `action_id` before the first attempt.
- Reuse that exact key for every physical retry.
- Use a fresh `attempt_id` for each actual retry.
- Persist the guarantee/version/scope on which retry relies.
- Retry only while the guarantee is still valid and current cancellation,
  deadline, permission, approval, and attempt budget permit it.
- If the guarantee expires or is weaker than the intended effect, stop and
  reconcile; do not assume safety.

Idempotency means duplicates collapse at the external boundary. It does not
grant authority, extend a deadline, or prove that the prior request succeeded.

### F.2 Locally verifiable or reversible

The effect can be inspected with authoritative local/domain state, such as
source/destination identity for a file move.

- Reconcile before retrying an ambiguous attempt.
- Retry only if evidence proves the intended effect is absent and retry is
  safe.
- If evidence proves the effect is present, record success rather than repeat.
- If state is partial or conflicting, remain blocked or record exact partial
  failure.
- “Reversible” never means cancellation automatically rolled it back.

A rollback/compensation is a new, separately validated, permitted, and
journaled action linked to the original. It is not a state transition that
erases the original effect.

### F.3 Non-idempotent or externally ambiguous

The system has no adequate idempotency guarantee and no authoritative proof of
absence after an ambiguous call.

- Never automatically retry after an `IN_FLIGHT` or unknown outcome.
- Use a capability-specific read-only probe if one exists.
- Otherwise require human confirmation and retain truthful ambiguity.
- A risk-accepted repeat is a new linked action, never reuse of the old
  `action_id` under a false “retry” label.

HTTP method names do not determine these classes. `POST` can be idempotent
under a provider key; `PUT` can still be unsafe if its resource or precondition
identity is unstable.

## G. Capability-specific reconciliation

The generic contract governs ownership and state transitions; a trusted
capability adapter interprets domain evidence. Conceptually:

```text
ActionReconciler.probe(
    canonical_intent_ref,
    immutable_attempt_evidence,
    trusted_operation_context,
) -> ReconciliationResult
```

`ReconciliationResult` is fixed and content-free at the generic layer:

- `CONFIRMED_APPLIED(normalized_result, evidence_refs)`;
- `CONFIRMED_NOT_APPLIED(evidence_refs, retry_safety)`;
- `CONFIRMED_PARTIAL(partial_summary, evidence_refs)`;
- `STILL_UNKNOWN(next_probe_policy | human_required)`.

The probe is read-only. If “verification” itself submits, repairs, deletes,
reverts, or compensates, that work is a new mutating action through
`ToolExecutor`.

The execution route is:

```text
ActionRecoveryCoordinator selects a claimed record
  -> ToolExecutor reconciliation entry point
  -> current registry/version lookup
  -> PermissionPolicy checks the reconciler's minimal current read/probe
     capabilities
  -> registered capability reconciler performs read-only probe
  -> ActionJournal CAS accepts the result for the same claim epoch
```

The coordinator never calls a provider, reconciler, or `Tool.run` directly.
The reconciliation entry point is part of `ToolExecutor`, not a second
executor. A future implementation may express a probe as a registered
read-only tool, but it must still use the same executor/policy route.
Because the probe is read-only, it does not need a second ActionRecord; its
invocation and accepted evidence are audit-linked to the action being
reconciled.

Reconciliation authority and mutation authority are deliberately separate:

- the historical mutation allow is evidence that the old attempt was
  authorized; it grants nothing now;
- every probe must pass current `PermissionPolicy` using only the minimal
  read/probe capabilities registered for that reconciler;
- a revoked mutation capability immediately forbids a retry but does not by
  itself forbid a separately allowed read-only probe;
- any retry or new physical attempt separately passes the current mutation
  capabilities and all current approval requirements;
- if probe permission is denied, credentials are unavailable, or the probe
  cannot be safely authorized, it does not run and the action remains
  `RECONCILE_REQUIRED`.

For example, after an ambiguous email send, current `SEND_EMAIL=DENY` and
`READ_EMAIL=ALLOW` may permit the registered reconciler to inspect Sent mail
and settle existing truth. It permits no resend. If current read permission is
also denied, the probe does not execute and ambiguity remains. There is no
special recovery bypass around `PermissionPolicy`.

Reconciliation results are accepted only for the exact `action_id`, record
version, and reconciliation claim epoch. A late probe from a stale worker is
discarded. Missing adapters, incompatible tool versions, inaccessible
credentials, probe-permission denial, or exhausted bounded probes leave the
action blocked; they never authorize a retry.

The generic layer does not treat “not found” as proof of absence. The adapter
must account for provider consistency, retention, and search semantics.

## H. Cancellation semantics

Cancellation is logical currentness plus best-effort physical interruption;
it is not effect truth.

### Before the mutation seam

A trusted cancellation/currentness checkpoint prevents the call. An
`AUTHORIZED` action transitions to `CANCELLED`. If an attempt claim was already
persisted, the same live owner may mark that attempt `NOT_INVOKED` and then
transition to `CANCELLED`.

### During the physical request

The executor may close an owned HTTP handle, terminate a child process it owns,
or request provider cancellation. The callback runs outside journal/store/
coordinator locks. The final action state follows effect evidence:

- confirmed complete effect -> `SUCCEEDED`;
- confirmed no effect -> `FAILED` with `CONFIRMED_NOT_APPLIED` and the logical
  cancellation recorded;
- exact partial effect -> `FAILED` with `CONFIRMED_PARTIAL`;
- uncertain effect -> `RECONCILE_REQUIRED`.

It does not become `CANCELLED` merely because the turn or token was cancelled
after physical invocation.

### After effect completion

Cancellation is appended as a bounded observation if useful, but cannot change
`SUCCEEDED`, erase an external identifier, remove partial evidence, or claim a
rollback. The durable effect result and the durable turn outcome may differ
without contradiction.

## I. Supersession and stale turns

`TurnCoordinator` and `TurnBridge` remain the authority for durable turn
supersession. Their cancellation token may interrupt a live resource, and the
turn fence continues to reject stale publication. ActionJournal answers a
different question: what happened outside AgentCore.

- Supersession before the mutation seam prevents the action and permits
  `CANCELLED`.
- Supersession after invocation never erases or relabels effect truth.
- A late trusted success is journaled even when its old turn can no longer
  publish tool text or an assistant response.
- A successor that resumes/carries forward a predecessor action must attach by
  trusted deduplication identity or `action_id`; it may not mint a fresh action
  as an automatic retry.
- If the existing action is `SUCCEEDED`, the successor observes the recorded
  result; if `IN_FLIGHT` or `RECONCILE_REQUIRED`, it reports verification in
  progress or requests human input; it does not execute a duplicate.
- A genuinely new user instruction may create a new action. The journal does
  not guess semantic identity from similar arguments.

The existing `StageLease` rule—that successor model/tool work waits for
predecessor terminalization—remains unchanged. This architecture neither
weakens the lease nor adds physical tool preemption.

## J. Permission and approval

ActionJournal is downstream of authority.

1. Arguments are validated first.
2. A pure canonical effect description may be prepared so approval is bound to
   the actual intent.
3. `PermissionPolicy` resolves all capabilities and required user approval.
4. Only a final `ALLOW` may be recorded as an `AUTHORIZED` action.
5. The executor rechecks trusted currentness before claiming an attempt.

Permission denial or unresolved approval creates no ActionRecord and no
physical attempt. Existing `PendingAction`/approval UI state remains a separate
domain. Approval must eventually be represented by a trusted durable receipt
bound to action kind, canonical intent digest, capability set, user/owner,
scope, and expiry or one-shot semantics. Changing arguments invalidates it.

The current boolean `ApprovalGate` cannot prove a restartable approval receipt.
Until a future implementation adds that receipt through the existing policy
authority, an approved action may not automatically reuse approval after
restart; it must be checked or requested again.

Recovery never inherits historical authority. A historical allow decision is
evidence that the old attempt was authorized, not permission for either a
probe or a new attempt. Reconciliation evaluates only the registered minimal
current read/probe capabilities. A retry evaluates the full current mutation
capability set and all current approval requirements. Revoking mutation
authority therefore prevents retries immediately without automatically
preventing an independently allowed read-only probe.

Model text, provider output, journal state, an idempotency key, external
success, and a recovery classification never grant authority.

## K. Crash and startup recovery

Before accepting new mutating execution for a workspace, startup must acquire
exclusive ActionStore ownership, validate the journal, and classify every
nonterminal record.

| Recovered state | Startup treatment |
|---|---|
| `AUTHORIZED` with no prior attempt | No effect may have occurred. Leave waiting until its durable owner resumes and current permission/approval is rechecked; do not execute merely because it exists. |
| `AUTHORIZED` after a checked safe retry decision | Preserve the retry proof, but still require current owner, authority, deadline, and attempt budget before a new claim. |
| `IN_FLIGHT` from a prior process epoch | Atomically move to `RECONCILE_REQUIRED`. Never mark failed, cancelled, or automatically repeat solely because the process died. |
| `RECONCILE_REQUIRED` | Claim a bounded read-only probe only when its minimal current read/probe capabilities pass policy; otherwise remain human-required/unresolved. Mutation permission is neither inherited nor required merely to inspect existing truth. |
| Terminal | Validate and leave unchanged. |

Externally idempotent actions may become `AUTHORIZED` for same-action retry
only while their exact key guarantee remains valid. Locally verifiable actions
require a conclusive probe first. Ambiguous non-idempotent actions never
automatically retry.

Startup turn reconciliation may mark an interrupted `TurnRecord` failed or
timed out; it must not make the corresponding action failed. The domains
recover independently and remain linkable by provenance.

An unavailable journal, unknown newer schema, invalid transition, broken
digest/reference, duplicate identity, or corrupt record fails the mutation
subsystem closed. The runtime quarantines the problem and permits harmless
read-only operation, but performs no mutation that might duplicate the corrupt
intent. It does not delete, coerce, or silently repair records.

## L. Concurrency and linearization

### L.1 Version 1 ownership domain

ActionJournal v1 is single-host, single-owning-process per workspace, matching
the current local AgentCore authority model. The durable store holds a
store-lifetime OS ownership lock. Another process cannot execute or recover
that workspace until the owner exits and the OS releases the lock.

This is not an active-active distributed lease. Cross-host concurrent action
execution is unsupported and fails closed. Supporting it later would require a
separately reviewed fencing/idempotency design at the external boundary.

### L.2 Per-action execution guard

Within the owner process:

- a host-wide map supplies one execution guard per `action_id`;
- the guard is held from attempt claim through durable outcome publication;
- duplicate callers do not start another attempt while the guard is held;
- the general store mutex is held only for bounded read/CAS/write operations,
  never for physical I/O;
- a stuck physical attempt blocks duplicate execution of that action rather
  than allowing a lease timeout to create overlap.

A time-based lease expiry alone can never authorize a second physical attempt:
the first worker might still be running. On process death, the OS ownership
lock and process-local guards disappear together, and recovery first treats
the durable `IN_FLIGHT` record as ambiguous.

### L.3 Durable duplicate and recovery fencing

The ActionStore atomically enforces unique `action_id` and unique non-null
`(deduplication_scope, deduplication_key)`. Create-if-absent linearizes
duplicate callers. CAS version plus a recovery claim epoch linearizes two
recovery threads: one wins; the loser reloads. Results from a stale epoch
cannot publish.

Separate conversation sessions share the same ActionStore. They interact only
when trusted deduplication scope intentionally makes them the same action.
Target similarity alone never joins them.

## M. Future ToolExecutor integration seam

No current production API changes in Task 43.1. A later implementation must add
typed contracts under `ToolExecutor`, conceptually:

- `EffectEligibility`: trusted, versioned action-kind metadata selecting the
  pure-read, ephemeral-control, or journal-required path;
- `MutationDescriptor`: pure canonical intent, `action_kind`, effect class,
  target category/ref, deduplication identity, and optional idempotency key;
- `ActionExecutionPermit`: executor-created, single-use, bound to exact
  action/attempt/tool/version and absent from model arguments;
- `MutationOutcome`: fixed effect verdict, normalized bounded result, safe
  evidence refs, and categorical error;
- `ActionReconciler`: read-only capability probe with an explicit minimal
  read/probe capability set, invoked only through the executor's
  reconciliation entry point.

The first implementation may conservatively treat entry to mutating
`Tool.run` as permit consumption. A future narrower adapter may consume the
permit immediately before its transport/file/UI seam. In both forms:

- `ToolExecutor` calls `Tool.run` at most once per claimed attempt, and every
  actual call has a pre-existing durable claim;
- no journal or recovery component calls `Tool.run` directly;
- the permit cannot be serialized, supplied by a model, reused, or transferred
  to another action;
- an unconsumed permit can close an attempt as `NOT_INVOKED` only in the same
  live owner;
- an exception after permit consumption defaults to `OUTCOME_UNKNOWN` unless
  a trusted adapter supplies stronger evidence.

When journal enforcement is enabled, each non-read action kind must have a
trusted eligibility registration. An action kind classified as journal-required
but missing its mutation descriptor/outcome/reconciler policy fails closed
before `Tool.run`. A generic desktop/browser action that cannot prove it is
ephemeral also fails closed; `read_only=False`, legacy `ToolResult`, arbitrary
metadata, model arguments, and exception types are not enough to infer or
downgrade effect truth.

`ToolInvocation` should later gain `invocation_id`, optional `action_id`,
`attempt_id`, and a safe action-outcome summary. It remains invocation history,
not the journal source of truth. Existing run/conversation recording may point
to the journal but must not copy protected payloads.

## N. Which operations require journaling

Journal eligibility is semantic action-kind metadata registered by trusted
code. A model, provider, tool argument, UI label, HTTP method, click, or key
sequence cannot select or downgrade it. The closed classification is:

| Semantic class | Meaning | Journal rule |
|---|---|---|
| `PURE_READ` | Reads existing state without changing durable, external, account, security, or meaningful user reality. | Fast path; no ActionRecord. |
| `EPHEMERAL_CONTROL` | Reversible/session-local UI or media control with no durable user-data mutation, durable external/account mutation, security/privilege mutation, meaningful restart ambiguity, or recovery requirement. | Fast path; no ActionRecord. |
| `DURABLE_LOCAL_MUTATION` | File create/write/move/rename/delete, repository/ref/config mutation, or meaningful durable application/user setting. | ActionJournal required. |
| `EXTERNAL_ACCOUNT_MUTATION` | Email/message send, calendar mutation, upload/post/publish, form submission, or remote repository/account mutation. | ActionJournal required. |
| `SECURITY_PRIVILEGE_SYSTEM_MUTATION` | Installation, privilege change, credential/security setting, or durable system configuration when later supported. | ActionJournal required and may require stronger approval. |
| `LONG_RUNNING_OR_AMBIGUOUS_EFFECT` | An effect whose restart/retry uncertainty could duplicate or lose meaningful user-visible reality, even if another label looks superficially local. | ActionJournal required; this class is an escalation override. |

Incidental process-local scratch with no durable or meaningful external/user
effect is not an action and creates no journal record.

The `EPHEMERAL_CONTROL` fast path includes, when the registered semantic action
kind meets every criterion above:

- open/focus/switch an application or window;
- switch a browser tab or open a normal browser page;
- play, pause, seek, or select next/previous media;
- adjust session/device volume.

These operations still follow the complete authority path:

```text
ToolRegistry.get
  -> Tool.validate_args
  -> trusted cancellation/currentness checkpoint
  -> PermissionPolicy.check / required approval
  -> trusted cancellation/currentness checkpoint
  -> Tool.run
  -> normalization/logging
  -> ToolInvocation
```

They may emit normal bounded tool observability, but no `ActionRecord`, attempt
claim, restart scan, or reconciliation item. The absence of journal I/O must
not remove or reorder validation, permission, approval, checkpoints, or stale
result fencing. Simple JARVIS controls therefore retain the low-latency path
without weakening authority.

Classification follows the effect's semantics, not its physical input
mechanism. “Click browser tab” may be `EPHEMERAL_CONTROL`; “click Send”,
“submit form”, “upload video”, “delete cloud file”, and “install application”
cross journal-required boundaries. Marking an email read or acknowledging a
remote notification is also an account mutation even if reversible.

A generic browser/desktop-control tool may not hide an arbitrary click or
keystroke behind one tool-wide `EPHEMERAL_CONTROL` label. Each allowed semantic
action kind must be registered and validated independently. If the trusted
adapter cannot determine before execution whether an invocation can cross a
durable/external/security boundary, registration or execution fails closed or
uses the journal-required class. The model cannot assert that a coordinate,
selector, shortcut, or button is harmless.

Pure reads and ephemeral controls remain lightweight. They are not journaled
merely for uniform observability. Compensation/rollback remains a separate
journal-required action whenever it mutates meaningful durable or external
reality.

## O. User-facing truth

The runtime maps durable truth to plain language:

| Journal truth | User-facing form |
|---|---|
| `SUCCEEDED` | “Done.” Include a safe result/reference when useful. |
| `FAILED` + `CONFIRMED_NOT_APPLIED` | “It failed; the change was not made.” |
| `FAILED` + `CONFIRMED_PARTIAL` | “It did not fully complete; these changes are confirmed.” Never claim rollback. |
| `CANCELLED` | “Cancelled before execution.” |
| `RECONCILE_REQUIRED` with an active probe | “The request may have completed; I am verifying it.” |
| unresolved ambiguity | “I cannot safely determine whether this happened.” |
| risky repeat available | “I need your confirmation before trying again because it could duplicate the action.” |

Low-level state names need not be shown. An initiating turn may be cancelled
while its action later becomes `SUCCEEDED`; the UI must present both facts
rather than forcing one to overwrite the other.

An ephemeral control returns through the ordinary tool result path and does
not surface “possibly switched tab” or “verifying media pause” recovery states.
If its semantic classification cannot be proven, it fails closed rather than
silently taking the fast path.

## P. Long-running workflows, schedulers, and zero-cost mode

Action records are independent of a live turn and can be referenced by future
workflow/scheduler domains. Those systems decide *when and why* work should
resume; the journal decides whether another physical attempt is safe.

Each scheduled occurrence receives a stable trusted occurrence identity and
therefore at most one logical `action_id`. A restarted scheduler submits the
same deduplication key and attaches to the existing action. It does not infer
completion from its own checkpoint.

The journal is not a scheduler, workflow state machine, queue, planner, or
proactive policy engine. Task 43.1 defines no Stage 6 runtime.

Local files, local models, and local protected storage can support the journal
without paid credentials. A missing cloud credential prevents that provider's
action before execution; it does not weaken local authority or journal truth.

## Q. Observability and privacy

Future structured events are categorical:

- `action.authorized`
- `action.duplicate_attached`
- `action.attempt_claimed`
- `action.effect_invoked`
- `action.succeeded`
- `action.failed`
- `action.cancelled`
- `action.reconcile_required`
- `action.reconciliation_started`
- `action.reconciliation_finished`
- `action.corrupt`

Safe fields are action/invocation/attempt IDs, action kind, tool name/version,
capability names, effect class, from/to state, record version, safe result
class, attempt/probe counts, durations, cancellation category, retry/reconcile
disposition, and an optional installation-keyed target fingerprint.

Never log or place in generic journal fields:

- credentials, API keys, OAuth tokens, cookies, or authorization headers;
- raw tool arguments or arbitrary metadata;
- sensitive email/message bodies or recipients by default;
- raw provider responses or provider exception messages;
- arbitrary browser page/form content;
- arbitrary file contents or unrestricted paths;
- user secrets or protected intent payloads.

`action.effect_invoked` is emitted only when the trusted permit is consumed. A
missing event is not recovery proof because the process may crash before the
log write. Metrics and logs are observability, never lifecycle authority.

Safe metrics include authorization-to-attempt latency, physical duration,
time-to-durable-result, reconciliation latency/count, terminal counts,
ambiguous-action age, duplicate attachments, and corruption/quarantine counts.
Ephemeral controls use existing bounded tool latency/outcome observability and
emit no `action.*` lifecycle events.

## R. Storage, representation, and versioning

Define an independent `ACTION_JOURNAL_SCHEMA_VERSION = 1` when implementation
begins. `TURN_SCHEMA_VERSION` remains `1`; ActionRecord must not reuse or embed
the `TurnRecord`, `AgentState`, `ConversationSession`, or `MemoryRecord` schema.

### R.1 Logical record shape

An `ActionRecord` has fixed, strictly validated fields:

- schema version, action ID, state, monotonic record version;
- action kind, tool name/version, trusted journal-eligibility class, mutation
  capabilities, reconciler's minimal read/probe capabilities, and effect
  class;
- canonical intent digest and protected intent reference;
- target category and optional safe fingerprint;
- deduplication scope/key and optional external idempotency key/contract;
- immutable provenance links and predecessor link;
- authority receipt and its scope/expiry;
- append-only attempt records and transition records;
- current bounded reconciliation disposition/claim epoch;
- normalized terminal result or failure/partial summary;
- safe external evidence references;
- created/updated timestamps from a trusted clock.

There is no arbitrary provider `raw`, arbitrary journal metadata bag, or
model-controlled transition note. Lists, strings, evidence refs, attempts, and
transitions have hard implementation bounds. On reaching a bound, execution
stops safely and requires operator handling; history is not truncated to make
room for a new physical attempt.

`ToolExecutor` owns creation, attempt claims, and result transitions. The
recovery coordinator may own a bounded reconciliation claim but asks the
executor to perform the probe. Tools, models, providers, presentation sinks,
and user interfaces receive neither a mutable `ActionRecord` nor a raw
`ActionStore` handle. User interfaces may read safe snapshots only.

Intent identity, kind, effect class, deduplication identity, idempotency key,
tool version, initial authority, provenance, and creation time are immutable.
CAS may append attempts/transitions/evidence and update only fields allowed by
the legal state transition.

### R.2 Store operations

The `ActionStore` contract must provide atomic:

- create-if-absent by `action_id` and deduplication identity;
- strict load;
- versioned compare-and-swap transition;
- append-attempt-and-enter-`IN_FLIGHT` as one durable operation;
- claim/release/update reconciliation by process epoch and claim epoch;
- enumerate nonterminal records;
- store/load protected intent/evidence references without exposing them in
  public record serialization;
- acquire/release single-owner workspace lifetime lock.

Record contents must be flushed before an effect permit is returned. Atomic
replacement prevents partial files; a supported platform must also document
its metadata durability boundary. A later SQLite backend is acceptable if it
implements the same contract. Backend choice does not change effect truth.

Do not hold an ActionStore mutex during network, filesystem mutation, browser,
desktop, subprocess, cancellation callback, or reconciliation I/O.

### R.3 Migration and corruption

Schema changes require explicit, tested migration with a backup/recovery plan.
Readers reject unknown newer versions and malformed/cross-record identities.
Migration never reclassifies ambiguous actions as failed or successful without
evidence and never deletes attempt history. Downgrades that cannot preserve
truth fail closed.

Protected payload corruption, missing evidence referenced by a nonterminal
record, duplicate deduplication ownership, illegal transition history, or an
invalid digest quarantines mutation execution for the workspace. Read-only
tools may continue if they do not depend on the corrupt domain.

## S. Capability examples

These examples demonstrate the generic contract. They do not authorize or
implement the named capabilities.

### S.1 Gmail send

The canonical intent includes protected recipient/body data and a stable
AgentCore-generated message marker. Sending is an external ambiguous mutation
unless the selected API documents a sufficient idempotency guarantee. A lost
response becomes `RECONCILE_REQUIRED`; a reconciler may search Sent mail by a
stable marker/message identity. Search absence under eventual consistency is
not automatically proof of no send. No blind resend occurs.

OAuth credentials remain outside the journal. Read, draft, send, modify, and
delete are separate permissions. Sending requires the configured user approval
policy.

### S.2 Calendar create or update

Where supported, a client-generated stable event ID and conditional update
precondition can make retries externally idempotent. Reconciliation fetches
that resource and compares safe canonical fields. A provider without stable
identity or reliable absence semantics remains ambiguous.

### S.3 Filesystem move

This is a locally verifiable/reversible mutation. Persist protected canonical
source/destination plus stable file identity/digest before the move. Recovery
inspects both locations:

- destination has the intended identity and source is absent -> succeeded;
- source has the intended identity and destination is absent -> safe to
  reauthorize a fresh attempt;
- both/neither/conflicting identity -> partial or reconciliation required.

Undo is a new move action. It does not rewrite the original as cancelled.

### S.4 Browser form submission

A domain-specific receipt, confirmation resource, or stable submission marker
may support reconciliation. Generic visual similarity or page text does not.
If submit may have occurred and no authoritative marker exists, remain
unresolved and request human confirmation before any new submission.

### S.5 Scheduled action

The future scheduler derives a stable occurrence key and calls
`ToolExecutor`. Restarting submits the same key, so duplicate scheduler workers
attach to one action. Due-time policy/approval is still required. The scheduler
does not own effect truth.

### S.6 Coding and Git mutation

Workspace file edits, branch changes, commits, pushes, and remote repository
mutations are journaled according to their actual boundary. Local Git/file
state can often be reconciled using repository identity, refs, tree IDs,
commits, and guarded paths. A generic shell command with unbounded effects is
ambiguous and must not be auto-replayed. A revert is a separate authorized
action.

## T. Explicit non-goals

Task 43.1 does not implement or authorize:

- ActionJournal production code or migration;
- Gmail, Calendar, contacts, browser, desktop, app-control, or file-mutation
  tools;
- scheduler, workflow engine, queue, proactive routine, or Stage 6 work;
- voice, audio, phone calling, remote access, or screen understanding;
- generic distributed transactions or exactly-once external delivery;
- active-active, cross-host, or multi-agent execution;
- a generic agent swarm or multiple user-facing agent identities;
- changes to Stage 4 stream events, `StreamAccumulator`, attempt identity,
  retry semantics, `TurnBridge`, `StageLease`, or conversation streaming;
- a second permission system, direct `Tool.run`, or a shadow executor;
- journaling every pure read;
- journaling proven ephemeral UI/media controls solely because they are
  user-visible;
- automatic rollback/compensation;
- any financial action authority.

Financial actions are completely out of scope. No action kind or capability
adapter may autonomously purchase products, spend money, manage cards or bank
accounts, transfer funds, trade, manage cryptocurrency, buy subscriptions, or
perform another financial transaction. Research and comparison may remain
read-only. ActionJournal durability never turns a blocked financial capability
into an allowed one.

## 4. Deterministic future implementation tests

Implementation tests use `Event`, `Barrier`, `Condition`, gated fakes,
subprocess crash points, and `FixedClock`. No correctness assertion uses
`time.sleep`, live network, probabilistic timing, or a real external account.

1. **Crash after intent, before effect.** Gate after durable `AUTHORIZED` and
   terminate a subprocess. Restart proves zero fake effects and no blind
   execution. A second variant terminates after `IN_FLIGHT` claim but before
   seam entry; despite the fake's zero call count, recovery conservatively
   enters `RECONCILE_REQUIRED`.
2. **Crash after remote success, before local success persistence.** A fake
   external ledger records one effect, then the subprocess is terminated at a
   gate before `SUCCEEDED`. Restart reconciles the ledger to `SUCCEEDED` and
   never calls the effect twice.
3. **Timeout with unknown outcome.** The fake crosses its commit gate and then
   raises a categorical timeout without a receipt. The action becomes
   `RECONCILE_REQUIRED`; no automatic attempt follows.
4. **Duplicate caller.** Two threads meet at a `Barrier` with the same trusted
   deduplication identity. Both observe one `action_id`; exactly one fake
   physical call runs, and the second attaches to its state/result.
5. **Cancellation before physical commit.** Gate immediately before final
   checkpoint, request trusted cancellation, and release. The action is
   `CANCELLED`, any claim says `NOT_INVOKED`, and the fake call count is zero.
6. **Cancellation during physical request.** Gate inside the owned fake
   request, request cancellation, and verify the close callback wakes it.
   Confirmed success stays success; otherwise the action is partial/unknown,
   never false-cancelled.
7. **Supersession after remote success.** Use real `ConversationRuntime`,
   `TurnBridge`, and `TurnCoordinator` with a gated mutating fake. After the
   effect ledger records success, admit a successor. The predecessor turn may
   cancel and publish no stale output, but the action becomes/remains
   `SUCCEEDED`; the successor attaches and causes no duplicate.
8. **Idempotent external retry.** The fake accepts one stable idempotency key,
   loses the first response, and returns the stored result on the fresh second
   attempt. Assert one logical action, two distinct attempt IDs, the same key,
   and one remote effect.
9. **Ambiguous non-idempotent action.** Lose the response after possible
   commit. Recovery and repeated caller attachment never increase physical
   call count. A deliberate risk-accepted repeat creates a new linked action.
10. **Restart reconciliation.** Seed `AUTHORIZED`, `IN_FLIGHT`,
    `RECONCILE_REQUIRED`, and terminal records. Restart applies the table in
    section K exactly and never resumes a prior physical call.
11. **Two recovery workers race.** Start both on a `Barrier`. CAS/claim epoch
    permits one probe; the loser reloads. A late stale result cannot overwrite
    the winner.
12. **Permission denial.** Deny the exact mutation capability. Assert no
    ActionRecord, no attempt ID, and zero physical calls. Pending approval has
    the same outcome until a final allow is resolved.
13. **Journal corruption.** Corrupt a state, digest, identity, transition, and
    protected reference in separate cases. Startup quarantines mutations and
    performs zero effects; it does not coerce/delete the record. Harmless reads
    remain available where independent.
14. **Read-only bypass.** Invoke a pure read through the real `ToolExecutor`.
    It validates and passes policy but creates no ActionRecord and returns its
    normal `ToolInvocation`.
15. **Secret sentinel.** Place distinct credential, authorization-header, raw
    provider, sensitive-body, and browser-content sentinels in the fake. The
    credential/header/raw-provider sentinels occur nowhere in public or
    protected journal storage; none occur in logs/events/errors. Required user
    content appears only in the protected intent payload, never the public
    record or observability.
16. **Result-persistence failure.** Let the remote fake succeed, then fail the
    journal CAS/flush. The executor never repeats the effect, never reports
    durable “done,” and restart reconciles the one effect.
17. **Retry guarantee expired.** Advance `FixedClock` past the provider's
    idempotency retention horizon. A previously safe retry becomes blocked and
    no new attempt starts.
18. **Confirmed partial effect.** A gated multi-step fake applies a known first
    step and fails before the second. The terminal result is `FAILED` with
    `CONFIRMED_PARTIAL`; cancellation and retry do not erase or duplicate it.
19. **Store lock discipline.** Gates prove the ActionStore/CAS mutex,
    coordinator lane, and session lock are free while the physical fake blocks
    and while its cancellation callback executes.
20. **Stage 4 regression.** The existing 42.5a/b/c suites remain unchanged and
    prove no stream event, accumulator, retry, StageLease, or TurnBridge
    contract moved.
21. **Ephemeral fast control.** Register a trusted `EPHEMERAL_CONTROL` fake and
    invoke it through the real `ToolExecutor`. Gates/counters prove argument
    validation, both trusted checkpoints, and current policy/approval occur in
    order; exactly one fake control operation runs and zero ActionRecords or
    action attempts exist.
22. **Desktop/browser semantic boundary.** Attempt to register or execute a
    generic desktop/browser “send/submit” action as `EPHEMERAL_CONTROL`.
    Trusted semantic registration rejects it or execution fails closed before
    `Tool.run`; zero send/submit effects occur. A model-supplied “ephemeral”
    flag, coordinate, selector, or button label cannot change the result.
23. **Reconciliation after mutation permission revocation.** Seed an ambiguous
    action, then deny its current mutation capability while allowing the
    reconciler's independent minimal read/probe capability. The real
    `ToolExecutor` permits exactly one gated read-only probe, authoritative
    evidence may settle the original action, and the physical mutation attempt
    count never increases.
24. **Reconciliation read permission revocation.** Seed the same ambiguous
    action but deny the registered read/probe capability. The probe body is
    never entered, the action remains `RECONCILE_REQUIRED`, and zero new
    mutation attempts occur.

## 5. Architecture self-audit

1. **Can a physical mutating effect occur before durable intent exists?** No.
   Both `AUTHORIZED` intent and the exact `IN_FLIGHT` attempt claim must be
   durably reloaded before the mutation seam.
2. **Can an ambiguous effect be automatically retried unsafely?** No.
   `RECONCILE_REQUIRED` blocks execution; only a documented live idempotency
   guarantee or conclusive absence proof permits same-action retry.
3. **Can cancellation erase the truth that an external effect happened?** No.
   Post-invocation state follows effect evidence, not cancellation labels.
4. **Can supersession erase or duplicate an effect?** No. It fences turn
   publication but cannot rewrite action history; carried-forward work attaches
   to the same trusted action identity.
5. **Can two workers physically execute the same logical action concurrently?**
   No in the supported v1 ownership domain. Store ownership, unique dedup, CAS,
   and a per-action execution guard prohibit it. Cross-host active-active is
   unsupported, not falsely claimed safe.
6. **Does ActionJournal grant authority?** No. It records a final authority
   receipt and enforces effect safety only.
7. **Does ToolExecutor remain sole execution authority?** Yes. Initial,
   retry, recovery, reconciliation, and compensation routes enter
   `ToolExecutor`; no direct `Tool.run` path exists.
8. **Are credentials/raw provider responses excluded?** Yes, including from
   protected intent payloads.
9. **Can restart recovery distinguish safe retry from reconciliation?** Yes,
   using durable state, attempt/effect class, idempotency contract, evidence,
   and current authority.
10. **Do we falsely claim exactly-once delivery?** No. The stated target is
    at-most-once where provable, provider-backed idempotent retry where
    supported, and truthful ambiguity otherwise.
11. **Are read-only operations kept lightweight?** Yes. Pure reads bypass the
    ActionJournal.
12. **Is the design usable by Gmail, Calendar, filesystem, and browser without
    baking one into core?** Yes. Core owns identities, state, CAS, and safety;
    registered capability adapters own effect and reconciliation semantics.
13. **Is financial authority still completely out of scope?** Yes. No payment,
    purchase, banking, transfer, trading, crypto, or subscription action is
    authorized by this design.
14. **Is Stage 4 untouched?** Yes. Neutral events, `StreamAccumulator`, model
    attempt identity, retry semantics, `TurnBridge`, `StageLease`, streaming
    consumption, and `TURN_SCHEMA_VERSION == 1` are unchanged.
15. **Do reversible session/UI controls require journal I/O merely because
    they are visible?** No. Trusted `EPHEMERAL_CONTROL` action kinds retain the
    full executor/policy/checkpoint path and create no ActionRecord.
16. **Can a generic click/keystroke hide a durable effect behind the ephemeral
    path?** No. Semantic action-kind registration is trusted and fail-closed;
    models and physical input mechanisms cannot choose eligibility.
17. **Does reconciliation inherit mutation authority or require it merely to
    inspect truth?** No. The probe passes its minimal current read/probe
    capabilities; any retry separately passes current mutation capability and
    approval.

## 6. Frozen conclusion

ActionJournal is durable evidence and reconciliation control around the one
existing tool authority. It provides no arbitrary exactly-once promise and no
new product capability. Its core rule is simple: persist authorized intent and
an exclusive attempt claim before every journal-eligible effect; after
ambiguity, establish truth or stop. Pure reads and proven ephemeral controls
stay on the normal low-latency ToolExecutor path without weakening authority.
