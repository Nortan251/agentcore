# Task 50B — first complete supervised workspace workflow

This composition coordinates existing LLMPlanner, TaskRunner (strict mode),
ToolRegistry, ToolExecutor, WorkspaceGuard, DurableApprovalAuthority and
ActionRecoveryCoordinator. It adds no new permission level, effect adapter,
planner, journal, mutation primitive or retry path. Ordinary Agent/chat and
WorkspaceAgent v1 retain their existing defaults and AUTONOMOUS admission.

## Use

Configure an existing workspace and `permissions.level = "CONFIRM"`. Run from
an interactive terminal; the review is private stderr, public JSON is stdout:

```text
agentcore --config agent.local.toml workflow --enable-write --message "Summarize the declared input" --input notes.txt --output summary.txt --json
agentcore --config agent.local.toml workflow --resume task_<12 hex digits> --json
```

The first challenge accepts a host-validated plan. The second separately approves
the full canonical-workspace-relative destination and exact UTF-8 draft. ASCII
JSON displays every character, escaping controls rather than executing terminal
sequences. It includes byte count and SHA-256; nothing is silently truncated.
Noninteractive stdin/stderr, EOF or a mismatched challenge grants no authority.
Neither prompt accepts model text as permission. Public results omit paths,
bodies, objective/model prose and approval details. Private storage is
authenticated plaintext protected by host ACLs, not encrypted at rest.

## Bounds and execution

The operator declares up to five input paths and one output. The planner proposes
a linear chain of three to eight tool calls: observations of `.` or declared
inputs, one penultimate `write_file`, then a separate `read_file` of that output.
Every declared input must be read. Only inspect/list_directory/read_file/write_file
are registered. Extra tools/arguments/paths, branches and additional writes are
rejected. Existing filesystem traversal/sensitive-path/no-follow/single-link
checks remain authoritative. Listing discovers no additional read authority.

The write's planned content must be empty: this is a deferred host draft slot,
not authorization to write an empty file. After accepted observations, one
tool-free model completion drafts the proposed body. The host enforces a maximum
4096 UTF-8 output bytes, 65536 aggregate observed input bytes and a cooperative
120-second execution budget (operator may lower bytes or set 1–600 seconds).
Incomplete/truncated observations stop dependency release. No automatic replan.
Provider calls and human input are cooperative, not physically preempted; a late
response is discarded at a checkpoint before mutation admission/approval.

TaskRunner receives content-free task arguments/results. A private invocation
bridge resolves the immutable accepted arguments and delegates to the canonical
ToolExecutor. Current CONFIRM, FS_READ and explicit write opt-in are required
for creation. FS_WRITE capability overrides cannot remove the executor's
intrinsic CONFIRM approval requirement. Its existing leases and exact tool
registration fences cover preflight through native publication. Existing
destinations/absent parents are refused. File publication remains Windows-only;
POSIX is explicitly refused without fallback.

## Durable truth and verification

Acceptance stores an immutable plan in the existing protected payload store
and authenticated continuity in a fixed-name ProtectedFile under the locked
workspace slot. Stable task/run/step IDs and a dispatch latch are saved before
canonical write admission. Scope binds workspace, accepted plan and create step.
The existing approval authority owns request authentication, expiry, decision,
one-shot consumption and receipt; the host only supplies a typed human gate and
persists its correlation. Raw draft exists transiently and in protected action
intent, never generic state or public journal. At most 256 retained workflows;
no pruning or history replacement. An unverified workflow retains the active
slot and blocks admission of another objective for operator investigation.

ActionJournal alone determines physical effect truth. A SUCCEEDED /
CONFIRMED_APPLIED mutation receipt passes the create-step verifier and releases
the final read. It does **not** complete the objective. The separate read must
be successful, strict UTF-8, untruncated, have consistent path/size/byte metadata,
and match the approved byte count and SHA-256 without normalization. Only then
is a timestamped verification receipt linked to workflow/read/action/intent
durably saved. Strict TaskRunner blocks on any other verdict or verifier error.

Completion proves that observation time, not immutable future contents or
semantic quality. Human review owns the draft's suitability. Inspection does
not claim an atomic workspace snapshot.

## Restart and failures

The existing slot lock excludes concurrent callers and PID/thread fences precede
store access. Startup audits protected objects, authenticated plans/approvals,
workflow/action links and terminal verification receipts. Missing/corrupt
essential evidence, unsafe storage, unknown temp residue and capacity exhaustion
fail closed. Separate atomic stores are not represented as one transaction.

Resume never invokes write_file, including before dispatch, after consumed
approval, after an action claim, or after a lost executor return. Interrupted
IN_FLIGHT actions pass through the existing recovery coordinator and canonical
read-only reconciliation. Unknown effects block dependent reads and new
objectives; absence is not proof of historical non-application. A confirmed
create permits one explicitly accepted, currently authorized verification read.
FS_WRITE may be revoked and write opt-in omitted for this read-only path; FS_READ
must remain allowed. Historical completion is explicitly labelled, with its
original observation time. A current still-matches claim requires a new read.

Denial, cancellation, partial completion, confirmed creation with unfinished
verification, unknown/reconciliation-required effects and storage/evidence
failure are distinct public outcomes. A post-create failure never rolls back or
erases the journal's known result. No automatic repeat, compensation, reset,
background scheduling or duplicate-risk successor UI exists in this milestone.

## Evidence

`test_task_50b_workflow.py` uses synthetic model and typed human UI decisions with
real canonical executor, durable stores, native Windows file publication and
independent reads. Subprocess tests kill real processes before dispatch, after
authorization/claim/publication/success/read/receipt; restart must not recreate.
Fault injection, synthetic approval and native filesystem evidence are distinct;
these tests are not a live operator/provider walkthrough. Existing filesystem,
approval, recovery, WorkspaceAgent and credential/Gmail suites cover reused
boundaries. Linux tests prove POSIX refusal, not supported publication.
