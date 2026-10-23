# Task 50 PR A — Opt-in strict TaskRunner verification gate

This change introduces `TaskRunner(strict_verification=True)`. The default
remains `False` and existing Agent, ConversationRuntime, WorkspaceAgent and
TaskRunner behavior is unchanged until a trusted caller explicitly opts in.

In a strict run, a successful tool invocation is recorded as physical execution
truth, then judged by the configured verifier. Only a `PASSED` verdict leaves
its subtask completed and dependency-eligible. `FAILED`, `UNVERIFIED` and
`SKIPPED` leave the step `BLOCKED`; the run stops before independent or
dependent steps. A raised verifier error also prevents dependency release and
propagates without converting the error into a successful result.

The existing verifier requires a `COMPLETED` subtask for its evidence context.
On non-PASSED verdict the strict runner uses existing legal transitions
`COMPLETED -> PENDING -> BLOCKED`, retaining its actual tool output and
verification record. No mutable external effect is undone by a status
transition, and strict-verification failure never triggers recovery/retry.

This PR deliberately does **not** add a supervised workspace host, plan
acceptance, approval prompts, cross-restart continuity, action prebinding,
deterministic filesystem content verification or any new tool. Those are
separate Task 50 PR B work and must retain the existing PermissionPolicy,
ToolExecutor, ActionJournal and native Windows publication boundaries.

This is a control-flow gate, not a claim that a model verifier can establish
real-world truth. Task 50 PR B must provide a trusted evidence-aware verifier,
and must not use model prose as proof that file creation succeeded.
