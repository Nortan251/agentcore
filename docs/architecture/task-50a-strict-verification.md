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

## Strict re-entry admission and terminal truth

Strict mode inspects existing task state *before* scheduling any new step.
Pre-existing `COMPLETED` steps with `SKIPPED`, `FAILED` or `UNVERIFIED`
verification are not dependencies eligible for release. The task becomes
`BLOCKED`, including when other, unrelated steps are ready, without
replaying the completed action or destroying its recorded effect. This
applies to restored `Task.from_dict` objects, not just live steps.

A task that is already terminal (`CANCELLED`, `FAILED` or `COMPLETED`)
is explicitly rejected from strict-runner re-entry. It is not transformed
to `BLOCKED`, nor is a misleading run status returned. Consistent with
`Agent.run_task`, terminal tasks need a separately authorized new run.
A cancelled control still takes precedence over a nonterminal task's
strict-verification preflight.

This guard checks the verification status recorded in the existing task
model; it is not authentication of arbitrary externally forged task state.
Task 50B must authenticate workflow continuity and independently ground
filesystem postconditions before relying on a historical PASSED verdict.
