# Credential, identity, and capability foundation

Implementation baseline: `9d37123c5a7ca56ddfedf34ec436fa1fcabc2123`.

This first slice supplies a host-injected credential boundary for authenticated
**pure reads**. It adds no OAuth flow, Google API, default external tool,
credential UI, production persistence, paid service, or mutation permission.
Stage 5 and its frozen ActionJournal/approval/recovery schemas are unchanged.

## Authority and integration points

The execution chain remains:

```text
ToolRegistry lookup
  -> AuthenticatedReadTool argument validation
  -> exact ToolRegistry execution lease
  -> capture trusted account/provider binding (metadata only)
  -> checkpoint -> PermissionPolicy.check(authenticated_read=True) -> checkpoint
  -> executor-created CredentialExecution in ToolContext
  -> Tool.run -> consume the one-use permit
  -> checkpoint + current registry/spec/PermissionPolicy checks
  -> current credential state, exact reference/identity, expiry and scope checks
  -> short-lived CredentialLease
  -> checkpoint + current permission/registry checks after lookup
  -> lease the exact evaluated policy generation
  -> final provider identity + credential current/expiry checks
  -> registered provider.read
  -> bounded, detached, secret-checked result -> ToolInvocation
```

`CredentialRuntime` supplies credentials beneath the executor. It does not
decide whether an operation is permitted, call `Tool.run`, grant capabilities,
resolve approvals, schedule a retry, or establish external-effect truth.
`CredentialInfo` is inert metadata; knowledge of a reference is insufficient to
lease a secret. The vault accepts only its attached runtime's private authority,
and that runtime accepts only its attached executor's authority. Neither is
serializable or derived from model arguments, snapshots, history, or approvals.

Permission, expiry and provider identity are checked after vault lookup because
even a local backend can wait while the host revokes access. A lease is not
permission to dispatch. Its metadata mutex is already released when this final
host check runs, and the provider has not received or revealed the material.
Identity and expiry are checked again after that host callback returns, so a
wait or state change in the callback cannot admit a changed or expired binding.

`AgentDependencies.credential_runtime` is explicit and defaults to `None`.
ConversationToolRunner and TaskRunner retain the same ToolExecutor instance.
There is no configuration loader, environment fallback, hidden default vault,
or restoration of runtime authority from generic state. Existing model-provider
environment-key lookup is a separate, unchanged facility.

An `AuthenticatedReadTool` holds only its public ToolSpec. Its provider, account,
credential reference, and required provider scopes are registered out of band.
The tool must require **both `NET_READ` and `CREDENTIAL_ACCESS`**, have no other
capabilities outside `READ_ONLY_CAPABILITIES`, and have `read_only=True`. Token
scopes restrict credential use; they never add or override policy capabilities.

## Credential capability decision (PR #22 authority audit)

`CREDENTIAL_ACCESS` covers **any live secret use**, including a trusted bound
provider calling `CredentialLease.reveal()`. The vocabulary and master handoff
did not establish a narrower export-only meaning. Authentication must therefore
declare this existing capability rather than hide it behind `NET_READ`.

`DEFAULT_BLOCKED_CAPABILITIES` is unchanged. `evaluate(CREDENTIAL_ACCESS)` and
ordinary `check` requests for it still deny at every permission level, including
AUTONOMOUS and with an approving gate. `CREDENTIAL_ACCESS` is not added to the
global `READ_ONLY_CAPABILITIES` set. `EMAIL_SEND`, `PAYMENT`, `SYSTEM_ADMIN`, and
other mutation gates have no new path or relaxed default.

The sole exception is the reviewed bound pure-read context:

1. The host explicitly constructs `PermissionPolicy(...,
   allow_authenticated_reads=True)` or supplies
   `AgentDependencies(allow_authenticated_reads=True, credential_runtime=...)`.
   The opt-in defaults to false. It is not a `PermissionConfig`/config-loader
   field, a tool argument, a token scope, or restored session/journal data.
2. ToolExecutor supplies `authenticated_read=True` to `PermissionPolicy.check`
   only for the exact, validated final `AuthenticatedReadTool` class. A generic
   tool claiming the same capability names or read-only flag cannot opt in.
   This classification parameter supplies no authority by itself.
3. Policy requires the host opt-in, both declared capabilities, and no mutating
   capability. Explicit blocks still win for credential use and network reads.
   A capability override can further restrict the credential read's level but
   cannot create its opt-in; approval cannot substitute for it. Host-authorized
   pure reads are otherwise available at OBSERVE.
4. All declared read capabilities pass their ordinary current policy checks.
   The executor and credential runtime additionally enforce the exact current
   tool, provider, account, connection, generation, scope, and expiry binding.

This context permits no raw credential export/management tool, authenticated
mutation, ephemeral-control escape, or reconciliation probe. ActionJournal's
adapter and reconciliation-capability rules are unchanged. Authenticated reads
remain non-journaled by their validated effect classification. The host opt-in
does not change Stage 5 mutation policy fingerprints or historical approval
semantics, because it cannot authorize any such action.

## Identity and lifecycle

- `AccountIdentity` is the exact `(provider, issuer, subject)` established by
  trusted host authentication. Issuer includes any relevant tenant namespace.
  There is no email/display-name lookup, case folding, or default account.
- `acc_...` identifies that account within this vault. `con_...` identifies one
  explicit connection. `crd_...` identifies one credential generation. All are
  generated independently with 128 bits of randomness, not derived from a
  secret or provider identifier.
- The in-memory backend retains account identity across an explicit disconnect
  and reconnect within its lifetime. Reconnect creates a new connection and
  reference. It provides no durable identity/import promise across restart;
  the future production backend must define that contract explicitly.
- `register` refuses a second current connection for the exact identity.
  `describe` returns immutable, secret-free metadata and never resolves tokens.
- `rotate` compares the exact current reference, requires the same identity,
  retains account/connection IDs, increments generation, creates a new reference,
  and discards its own old material. Scopes may stay the same or shrink, never
  silently expand. An expired current credential may rotate; a revoked or
  replaced credential cannot be revived.
- Runtime rebinding after rotation is explicit. Reusing the same tool requires
  the same connection, account, provider object, ToolSpec and required scopes.
  A different account requires a new concrete tool object and explicit registry
  replacement. A permission wait that captured an older binding cannot adopt
  the replacement silently.
- `revoke` is idempotent for that exact revoked reference. Revoking a replaced
  reference fails stale rather than silently revoking a newer generation.
- `close` releases owned material. Runtime and vault lifetimes are host-owned
  and closed separately; one runtime attaches to one vault and one executor.

The backend bounds records, including tombstones, at 4,096; the runtime bounds
tool bindings at 256. Exhaustion fails closed. Durable archival and compaction
are future backend concerns, not silent deletion/reuse of identities.

## Concurrency, revocation, and process ownership

Concurrent **whole-policy replacement** through `executor.policy = new_policy`
is supported. Existing tests already treat replacement from another thread as
current revocation; the audit therefore classified the post-check dispatch race
as **P1**, not a documentation-only P2.

PermissionPolicy takes a detached immutable configuration snapshot and exposes
a read-only host opt-in. Mutating a published policy's authority in place is not
a supported update operation. Caller-owned override dictionaries are copied,
and the published mapping/configuration cannot be edited through the public
surface. Custom trusted policies must retain these snapshot semantics rather
than mutate their authority or depend on uncoordinated external state.

The executor owns a monotonically increasing policy generation and a short
metadata mutex. Evaluation and checkpoints run outside that mutex. It then
atomically verifies that the evaluated policy object **and generation** are
still current and acquires a dispatch lease. Replacing a policy and replacing
it back (ABA) does not validate the earlier decision. A completed replacement
before this admission fails the call closed. The preliminary permission lease
is released before vault lookup, so revocation while lookup waits still works.

The final lease spans provider-identity/current-expiry checks, provider I/O, and
result scrubbing. Until it exits, whole-policy replacement fails categorically
with `credential_busy` and changes nothing; it is not a successful revocation
or queued update. The host may explicitly retry after dispatch returns. This
applies to any replacement on that executor while a credential dispatch is
leased. Ordinary policy evaluations, public reads, unrelated registry/vault
metadata, and other executors remain available. No permission mutex is held
across callbacks or network I/O. PID checks precede the new mutex and its lease
cleanup; cleanup also verifies the creating thread. This does not introduce a
new concurrency promise for existing Stage 5 mutation/approval paths.

One credential generation/connection permits at most one active synchronous
provider call. Concurrent use fails busy. Successful revocation, rotation, or
vault close cannot overlap that lease. A busy administrative request **has not
completed** and must not be reported to the user as a successful revocation.
It can be retried explicitly after the call returns. This avoids claiming that
a token was revoked while a provider is still permitted to use it.

The binding registry likewise prevents replacement/close while its execution
lease is active. Runtime and vault metadata locks are released before any tool,
provider, or network I/O. Unrelated registrations and lookups can proceed while
a provider blocks. There is no timed lease expiry that admits duplicate work,
blocking wait for revocation, remote call under a metadata lock, or automatic
retry/refresh. A hung call leaves that connection busy; forced interruption and
draining revocation are explicitly not implemented.

PID ownership checks precede all inherited locks, injected-clock use, and
exceptional cleanup. Provider leases and execution permits also require their
creating thread. Neither can be transferred or replayed, including by calling
the private dispatch seam directly. Child processes must create fresh vaults
and runtimes. This is one owning process, not a cross-process credential service.

## Privacy boundary and trusted provider contract

`SecretMaterial`, vaults, runtimes, provider leases, and execution permits are
not dataclasses and reject pickle, copy, and deepcopy. Their string/repr output
contains no material. Generic dataclass serialization of a ToolContext carrying
a live permit fails rather than traversing it. ToolContext never carries raw
tokens, and the provider is not handed its logger, config, history, or journal.

Only a live, unexpired `CredentialLease.reveal()` exposes plaintext to the
registered provider's immediate synchronous request. Expiry during a lookup
blocks dispatch; expiry during an admitted call still permits internal result
scrubbing. The lease is invalidated on return.
Authenticated tool arguments are detached/validated before permission, and their
values and untrusted correlation fields are excluded from executor logs and
ToolInvocation. Credential/account/scope selectors are not ordinary parameters.
Incoming model data cannot replace the trusted binding.

The active account, connection, and credential-reference IDs are also withheld
if a provider echoes them in a result, including ordinary `id` fields and common
transport encodings. These IDs remain inert metadata, not secrets or permission.
Trusted host metadata APIs may use them; provider output is not an account
selection or binding-metadata export channel in this first slice.

Provider output must be a ToolResult with bounded plain JSON. No arbitrary
object repr, serializer, iterator, non-finite number, or unbounded tree is used.
Results are detached, credential-shaped fields are rejected, and literal/common
JSON, URL and base64 encodings of the leased token are checked in both strings
and the complete serialized result (including numeric values). Unsafe output
is withheld. Numeric credential text also contributes canonical integer/finite
float forms, so leading zeros, signs, exponent formatting, or float rounding
cannot bypass this check through ordinary decimal casts. Valid metadata stays
in its detached dictionary instead of being expanded into constructor keywords.
Provider failure bodies, exception messages, chains and notes are
discarded. KeyboardInterrupt/SystemExit retain their control-flow class with
empty content after safe cleanup. Logs contain categorical failures only.
Only cancellation originating from the trusted host checkpoint propagates;
a backend cancellation exception is sanitized like any other backend failure.

This is not a sandbox for malicious Python, arbitrary data-loss prevention, an
encrypted heap, or a guarantee of zeroization. Provider/backend implementations
are trusted code. They must not cache plaintext, log wire headers or bodies,
encode credentials into outputs, silently refresh/switch identity, redirect
credentials to another recipient, mutate under a read label, or retry behind
the executor. Their configuration must remain immutable while bound, and their
behavior across distinct connections must be thread-safe. Python caller-owned
strings, debuggers reading frame locals, host compromise, and process memory
dumps are outside the accidental-disclosure boundary.

No production secret belongs in the in-memory development backend or a
repository/config/plaintext JSON file. OAuth refresh tokens are not implemented
or exposed to read providers by this slice. A later host authentication service
must keep refresh material separate and supply verified, current access material
through an appropriate protected backend. Merely passing an identity alongside
a token does not prove the token's issuer, subject, audience, or scopes; verifying
those is the future authentication adapter's responsibility.

## ActionJournal, approval, recovery, and next work

Authenticated mutations are deliberately rejected at registration/validation.
No credential permit is added to action descriptors, protected payloads,
ApprovalBinding/receipts, ActionRecord, or ActionReconciliationContext. Existing
read-only credential operations produce no ActionRecord. Existing Stage 5
effects continue through their unchanged semantic adapters and effect evidence.
Historical approvals, completed effects, restored sessions, and stale provider
state cannot supply missing live credential authority.

Before authenticated mutations/reconciliation are enabled, the next design must
bind the stable connection/account identity into canonical intent and approval
identity, define how an existing journaled action resolves that exact account
after restart, and separately authorize a probe's minimal read scopes. Token
rotation must not change logical action/account identity, and missing credentials
must preserve unresolved effect truth. This cannot be inferred safely from a
tool name or a prior successful connection. No Stage 5 redesign is required here.

Recommended next task: implement and review the host credential backend and
Google OAuth connection lifecycle, beginning with verified account identity and
read scopes only. Cover OS/encrypted storage, least-privilege consent, refresh
token isolation, issuer/subject/audience validation, expiry, reconnect/revocation,
and atomic refresh publication. Do not add Gmail sending or authenticated
mutations until their account-bound intent/approval/recovery contract is reviewed.

## Verification

`tests/test_credentials.py` covers registration, identity ambiguity, missing and
forged references, revocation/reconnect, exact-reference rotation, scope changes,
expiry, serialization, bounded mutable-store behavior, concurrent rotation,
thread ownership, and simulated/real fork fencing.

`tests/test_credential_execution.py` exercises the real executor path, current
permission, binding changes, provider identity, missing services, argument
injection, result/exception leak attempts, one-use/escaped permits, concurrent
metadata mutation during provider I/O, and process ownership. Real Agent
conversation/autonomous tests serialize JSON state/sessions and inspect a
nonempty ActionJournal plus a consumed approval record, including the rejected
secret-output path and the default-denied host opt-in. Additional deterministic
Events pause after policy evaluation and at the final provider-identity and
credential-expiry boundaries. They prove stale-generation/ABA rejection, busy
replacement during dispatch, exception/cancellation cleanup, and policy metadata
progress during provider I/O. Poisoned-lock and real-fork tests cover inherited
policy access and cleanup alongside the existing runtime ownership fences.

`tests/test_permissions.py` covers the bounded context at every level, unchanged
default blocks, immutable policy snapshots, explicit block/override precedence,
and rejection of generic or widened credential authority. POSIX tests skip
explicitly on Windows and require Linux CI.
