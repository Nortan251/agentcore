# Filesystem Write v1

`write_file(path, content)` creates one missing regular UTF-8 file. Parents must
already exist. Existing destinations are refused at preparation and by atomic
publication itself. There is no replacement, append, truncation, directory
creation, deletion, shell, browser, or Gmail capability in this composition.
`default_tools()` and ordinary Agent construction remain read-only.

## Guarantees

**File creation now requires native Windows handle publication. POSIX file
creation is unsupported and fails closed, including private local Linux
directories.** This is an intentional compatibility restriction. The former
owner/mode/filesystem allowlist and pathname publication have been removed.
The [historical production P1](https://github.com/Nortan251/agentcore/pull/44#discussion_r4187472896)
requires independent review of this refusal contract; passing CI alone does not
resolve it. Directory creation retains its separate implementation and contract.

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

The shared parent preparation still validates target absence and binds the
directory identities for both mutation tools. File execution then validates the
bounded content and lexical path and checks publication support **before opening
the mutation parent, staging, or calling the physical-entry callback**. Any host
other than native Windows is refused with `unsupported_publication`. A claimed
durable attempt records NOT_INVOKED / CONFIRMED_NOT_APPLIED; no stage or final
file is created. Existing permission and approval checks still precede this
execution boundary. The policy is host-owned, unconditional, and has no model,
configuration, environment-variable or pathname fallback override.

On POSIX this refusal is independent of UID, ACL, mode, filesystem, ABI, mount
mapping or ancestor layout. No local mount inspection is treated as proof that
another alias of the backing directory is harmless. Native tests exercise
ordinary distinct UIDs, masked ACLs, foreign ownership, shared/private parents,
and a writable containing ancestor; these demonstrate refusal, not supported
Linux publication. A hostile two-idmapped-mount experiment is not required by
the implementation and is not claimed as tested.

### Why Linux unnamed staging alone is insufficient

`O_TMPFILE` without `O_EXCL` plus `linkat` can bind publication to an unnamed
source inode. `AT_EMPTY_PATH` requires CAP_DAC_READ_SEARCH; a procfs descriptor
link with `AT_SYMLINK_FOLLOW` can avoid that privilege, but depends on verified
procfs/descriptor semantics and filesystem support. Neither method freezes the
destination directory's namespace ancestry. An opened parent remains attached
to the directory after a different principal relocates a containing ancestor;
another final pathname check leaves a further check/use interval. `openat2`
resolution restrictions govern lookup, not a later independent publication.
Idmapped aliases also prevent mount-local owner bits from proving global
namespace exclusivity. Refusing unknown filesystem magic or idmapped status on
only the observed mount would not close that authority gap.

Consequently this repair does not add an unproven native binding, a privileged
production requirement, a cooperative lock, or an exclusion of other ordinary
principals from the threat model. Restoring POSIX file creation requires a
separate enforceable source-identity **and namespace-authority** contract with
native adversarial evidence. More random names, mode checks and post-effect
digest comparisons cannot replace it.

Platform references: [open(2)](https://man7.org/linux/man-pages/man2/open.2.html),
[linkat(2)](https://man7.org/linux/man-pages/man2/link.2.html),
[openat2(2)](https://man7.org/linux/man-pages/man2/openat2.2.html),
and [kernel idmapping documentation](https://docs.kernel.org/filesystems/idmappings.html).

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

On Windows the read-only reconciler uses the same verified filesystem boundary:

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
ActionJournal guards/attempt truth fence duplicate execution. Windows crash
residue remains bounded to one random `.agentcore-stage-<32 hex>` object per
physical attempt; there is no startup cleanup subsystem. Protected-store orphan
objects retain the existing operator-managed lifecycle.

For legacy uncertain file attempts reconciled on POSIX, observation refuses
before target traversal. Even an exact matching content digest does not restore
the missing publication guarantee. The adapter records OUTCOME_UNKNOWN /
HUMAN_CONFIRMATION_REQUIRED, keeps the original attempt, and never retries it.
Previously terminal journal records are not rewritten. Operators must review
historical POSIX effects and existing residue separately; this repair neither
deletes nor reclassifies their files. Schemas, action identity, protected intent,
approval decisions and persisted authority semantics are unchanged.

## Host trust assumptions / non-guarantees

The configured workspace is trusted host configuration.

AgentCore protects the mutation from untrusted model/page/file content and
unsafe workspace path objects.

File publication's supported Windows boundary uses the existing pinned,
no-delete-sharing directory and staging handles and native no-replace rename.
The successfully published object is the handle-owned object that received the
verified content. This does not promise immutable contents after handles close,
protection from administrator/kernel compromise, or POSIX-like directory fsync
durability on Windows. Native/filesystem errors after physical entry preserve
uncertainty; they never authorize a blind retry.

Other ordinary principals, hostile idmapped aliases and writable-ancestor
relocation are not newly excluded from the POSIX threat model. POSIX file
publication is refused because this implementation cannot enforce that model.
The shared POSIX traversal remains available for Directory Create under its
separate documented contract; this repair does not certify its namespace
behavior or repair File Replace.

## File Replace dependency

PR #43 must not reuse the removed POSIX staging or owner/mode helpers or bypass
the file-publication support boundary. Its separate `os.replace(stage, ...)`
source-substitution finding is not repaired by this create-only change.
Replacement must independently bind its verified source and admitted target,
preserve metadata/confidentiality and revision checks, define fail-closed
platform support and uncertainty boundaries, and obtain its own regressions,
exact-head review and all required CI. No PR #43 code is changed here.

## Explicit host composition

Construct `FilesystemWriteFoundation(guard)`, then call its
`register(tool_registry, action_adapters)`. Supply that exact guard, registries,
an ActionStore, ProtectedActionPayloadStore, current PermissionPolicy and (for
CONFIRM) existing DurableApprovalAuthority to `ToolExecutor`. Run
`ActionRecoveryCoordinator(store, executor).recover_startup()` before invoking.
Missing/corrupt storage or pending/quarantined startup remains fail closed.
There is no convenient alternate execution API and no automatic host enablement.
