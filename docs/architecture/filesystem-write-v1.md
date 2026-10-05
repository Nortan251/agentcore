# Filesystem Write v1

`write_file(path, content)` creates one missing regular UTF-8 file. Parents must
already exist. Existing destinations are refused at preparation and by atomic
publication itself. There is no replacement, append, truncation, directory
creation, deletion, shell, browser, or Gmail capability in this composition.
`default_tools()` and ordinary Agent construction remain read-only.

## Guarantees

The authority path is `ToolRegistry -> argument validation -> PermissionPolicy
-> ToolExecutor -> durable protected intent / ActionJournal attempt claim ->
Tool.run -> WorkspaceGuard-owned mutation -> normalized effect truth`.
The exact production tool requires a one-use, thread/process-local executor
context; a direct `run` has no mutation authority. The journal is evidence,
never permission. Current registration, claim, workspace, health, policy and
one-shot approval are checked before staging and again before publication.
An immutable policy-generation lease and registry execution lease span staging,
publication and cleanup, without holding their metadata mutexes across I/O.
The same leases cover destination preparation and read-only reconciliation,
including publication of reconciled journal truth. Revocation/removal fails
busy while an observation is leased and succeeds after it ends; a revocation
completed before leasing prevents filesystem access.
An unavailable claim or filesystem reconciliation binding returns immediately;
a later re-registration must start a fresh probe and acquire its own leases.
An immutable health lease also spans preparation, staging/publication/cleanup,
and reconciliation through truth publication. Quarantine immediately denies
new admissions, then waits without holding metadata mutexes for previously
leased filesystem work to finish. That work precedes completed quarantine;
its owning thread retains the healthy snapshot until exit. A same-thread
quarantine request raises busy instead of waiting for itself, but is latched
and becomes sticky quarantine when the final lease exits. Startup validation
cannot clear a pending or completed quarantine. Ownership is checked before
accessing health mutexes inherited across a process fork.
OBSERVE/PROPOSE refuse writes; CONFIRM requires the existing durable approval
path, including when a host capability override requests a lower level.
AUTONOMOUS still respects current capability denial.
Denied writes are checked before destination preparation, so they cannot probe
target existence or parent structure, including when resuming an action.
When write permission requires CONFIRM approval, destination preparation needs
independently allowed current FS_READ. Pending approval grants no observation
authority; a host configuration blocking FS_READ therefore refuses this
preflight uniformly before inspecting any destination.

Lexical path validation and the host sensitive policy remain in `fs`. Writes
enforce the sensitive/no-follow/single-link posture even if read-only filesystem
settings were relaxed. Absolute paths, traversal, Windows ADS/device spellings,
symlink/reparse components, directories and special destinations are refused.
Existing hard-link destinations are refused without modifying either alias.
Directory object identities, canonical workspace identity and the normalized
relative target are bound into protected intent. Detectable substitutions fail
closed; the model cannot select identities, roots, journal classes, temporary
names or recovery policy.

On POSIX, traversal opens components relative to directory descriptors with
`O_DIRECTORY | O_NOFOLLOW`. Staged file creation requires the **opened target
parent** to be owned by the effective AgentCore UID and to have neither group
nor world write permission. The descriptor is checked during execution before
the callback that marks possible physical entry and before staging can begin;
the shared `prepare_create` / Directory Create path is not restricted by this
file-specific rule. An unsafe parent produces no stage, staged bytes, final
target, or physical mutation. Parent safety is host policy, never a model option.

User-owned `0700`, `0750`, `0755`, etc. remain admitted. `0770`, `0775`, `0777`
and sticky shared writable parents such as `01777` are deliberately refused.
Parents owned by another UID are refused even with restrictive mode bits.
This is a conservative compatibility restriction, not a sticky-bit policy.

The supported POSIX permission model in this version is local Linux ext2/3/4,
XFS, Btrfs or tmpfs on the 64-bit x86-64 / AArch64 libc ABI. `fstatfs` identifies
the filesystem using that same open directory descriptor. Other POSIX systems,
unknown ABIs, native inspection errors, NFS, CIFS, FUSE, overlay and other
unproven permission models fail closed. Under Linux POSIX ACL semantics the
reported group permission bits bound named-user/group access via the ACL mask;
with no group write bit, an ACL cannot silently grant another ordinary UID
directory-entry write authority. Default ACLs do not grant access to the parent.
See [Linux acl(5)](https://man7.org/linux/man-pages/man5/acl.5.html) and
[fstatfs(2)](https://man7.org/linux/man-pages/man2/statfs.2.html).
This hotfix does not add general ACL evaluation or alternate publication APIs.

A host-generated exclusive 0600 staging file is
written exactly, fsynced, and published via descriptor-relative `link` with no
replacement semantics. The owned staging name is then unlinked and the parent
directory fsynced. Unsupported publication fails closed. This does not require
`openat2` or Linux 5.6. The briefly double-linked final file is not reported as
success until staging cleanup and directory flush complete.
Publication still uses the staging pathname: admission prevents a different
ordinary principal from substituting that name in supported parents, rather
than attempting another check-then-use pathname comparison. The object
published must be the staging object AgentCore wrote and verified. Directory
Create uses a direct final-name mkdir, with no staging-file publication window.

On Windows, the trusted root is opened with `FILE_FLAG_OPEN_REPARSE_POINT` and
directory semantics. Every requested child is opened with `NtCreateFile`
relative to an inspected directory handle and `FILE_OPEN_REPARSE_POINT`.
Reparse attributes are rejected, not followed. Data-read directory handles
also supply canonical component names for sensitive-policy checks, so DOS
short-name aliases cannot disguise sensitive parents. These directory handles
exclude delete sharing; attribute-only handles do not establish that exclusion.
Staging is exclusive and flushed with `FlushFileBuffers`.
`NtSetInformationFile(FileRenameInformation)` publishes relative to the final
parent handle with `ReplaceIfExists=False`. Owned staging cleanup uses handle
disposition, not a re-resolved path. No unsafe pathname fallback is provided.
The final file is flushed after rename. Windows exposes no equivalent supported
stdlib/Win32 parent-directory fsync guarantee here; file flushing and atomic
visibility must not be described as identical to POSIX directory durability.
Uncertain publication never deletes the possibly published final file.

## Bounds and privacy

The separate host-owned `filesystem.max_write_bytes` defaults to and is capped
at 128 KiB. UTF-8 characters are bounded before encoding, then exact encoded
bytes are bounded. Unpaired surrogates and binary inputs are refused.
The readable-file ceiling does not determine mutation size.

The existing protected record ceiling is 1 MiB. Its actual serializer uses
ASCII JSON escaping. Worst-case content consumes at most six serialized bytes
per UTF-8 byte: `6 * 131072 + 6 * 4096 + 8192 = 819200 < 1048576`.
The path is capped at 4096 characters and 64 components; the final reserve covers bounded native
directory identities, schema/digest fields and the authenticated record
envelope. Both exact N and N+1 and worst-case escaping are tested with real
protected storage. The intent contains one content copy required for exact
execution and crash recovery, plus minimal path/identity/digest metadata.

Raw content is absent from public ActionRecord, invocation args, approval text,
logs and generic state. The protected JSON backend authenticates and separates
content but is **not encrypted at rest**; confidentiality relies on host
filesystem access control. Public intent digests are installation-key-bound by
the existing protected store, not model-selected. No redundant content/effect
evidence object is persisted.

## Effect truth and recovery

The adapter uses `DURABLE_LOCAL_MUTATION` and
`LOCALLY_VERIFIABLE_OR_REVERSIBLE`; reconciliation requires only current
`FS_READ`. Staging cannot begin before durable intent and attempt truth.
The live executor distinguishes a proven pre-staging interruption (NOT_INVOKED)
from exceptions/cancellation/interruptions once staging may have started
(RECONCILE_REQUIRED). An exception never implies no final effect.

The read-only reconciler uses the same verified filesystem boundary:

- Safe single-linked regular final file with exact size and content digest:
  CONFIRMED_APPLIED.
- Final target absent after an uncertain attempt:
  OUTCOME_UNKNOWN / HUMAN_CONFIRMATION_REQUIRED. Absence cannot distinguish
  no publication from a published file subsequently removed by a cooperating
  process. This preserves historical effect uncertainty and authorizes no retry.
- Unsafe/different/unverifiable final target or substituted ancestry:
  OUTCOME_UNKNOWN / HUMAN_CONFIRMATION_REQUIRED. No blind retry.

Live refusal of an existing target proves CONFIRMED_NOT_APPLIED for that
attempt; a proven interruption before staging remains NOT_INVOKED. Neither
classification is inferred merely from absence during crash recovery.
Staging alone is not application. No automatic write retry is requested by the
reconciler. Existing targets cannot be overwritten on a resumed action, and
ActionJournal guards/attempt truth fence duplicate execution. Crash residue is
bounded to one random `.agentcore-stage-<32 hex>` object per physical attempt;
there is no startup cleanup subsystem. On POSIX an interruption between link
publication and staging unlink can leave two links; reconciliation remains
unknown until an operator resolves that residue. Protected-store orphan objects
retain the existing operator-managed lifecycle.

## Host trust assumptions / non-guarantees

The configured workspace is trusted host configuration.

AgentCore protects the mutation from untrusted model/page/file content and
unsafe workspace path objects.

AgentCore does not defend the workspace namespace against a separate malicious
process already running with the same user's filesystem authority or an
administrator/root (or equivalent privileged authority) concurrently relocating
authorized directories or changing their permission model. Different ordinary
principals are within the boundary: supported staged-create parents deny them
the namespace authority needed for staging-path substitution.

In particular, POSIX directory descriptors remain attached to directory objects
after hostile relocation outside the workspace. Component no-follow traversal
does not freeze namespace ancestry. Tests deterministically characterize this
excluded interleave instead of claiming to prevent it. Ordinary detectable
parent replacements are separately tested as refusals. These are host-boundary
limits, not promises of race-proof or sandbox-proof operation.

## Explicit host composition

Construct `FilesystemWriteFoundation(guard)`, then call its
`register(tool_registry, action_adapters)`. Supply that exact guard, registries,
an ActionStore, ProtectedActionPayloadStore, current PermissionPolicy and (for
CONFIRM) existing DurableApprovalAuthority to `ToolExecutor`. Run
`ActionRecoveryCoordinator(store, executor).recover_startup()` before invoking.
Missing/corrupt storage or pending/quarantined startup remains fail closed.
There is no convenient alternate execution API and no automatic host enablement.
