# Filesystem Directory Create v1

`create_directory(path)` creates exactly one missing workspace directory. Its
only argument is a bounded workspace-relative path. Parents must already exist;
every existing destination type is refused. The tool declares `{FS_WRITE}` and
`read_only=False`. No model-selected recursive, replacement, permission, owner,
mode, action identity or recovery option exists. Successful creation leaves an
empty directory, without markers, sidecars, xattrs or ADS.

## Authority and protected intent

The canonical route is fixed Workspace registry -> argument validation ->
current PermissionPolicy -> ToolExecutor -> ActionJournal -> one-use filesystem
mutation context -> WorkspaceGuard -> native create. Direct `run()` fails
closed. The executor recognizes only the exact reviewed `WriteFileTool` and
`CreateDirectoryTool` concrete types; arbitrary tools/subclasses/tags do not
receive the context. It remains one-use, exact-tool/workspace, PID/thread-bound,
non-serializable and inactive after execution. `filesystem_write_execution` and
the private `_WriteExecution` name remain compatibility aliases for PR40;
directory execution receives only `filesystem_mutation_execution`. Contexts
cannot cross the exact tool object binding.

The adapter uses `DURABLE_LOCAL_MUTATION` and
`LOCALLY_VERIFIABLE_OR_REVERSIBLE`, with current `FS_READ` for reconciliation.
Preparation has the accepted filesystem policy/registration/health leases.
Denied writes never inspect destination existence. Pending CONFIRM approval
does not provide read authority: preparation also needs independently allowed
FS_READ in that posture. The dedicated Workspace Host always requires FS_READ
and FS_WRITE, AUTONOMOUS and current explicit `--enable-write`.

Protected intent contains schema, exact tool name/version, mode
`create_directory`, a canonical workspace fingerprint, normalized relative
target, verified parent identity chain and observed absent pre-state. Public
records contain only categorical information, authenticated digests and opaque
references. Paths stay protected even though no file content is involved. The
existing JSON protected backend authenticates plaintext; it is not encrypted.

An already-existing target is refused during trusted preparation, before an
action or attempt is admitted. The safe invocation result reports
`confirmed_not_applied`; no absent pre-state is fabricated. A destination that
appears after preparation is refused with journaled CONFIRMED_NOT_APPLIED.
Durable intent and IN_FLIGHT attempt truth precede physical mkdir.

Registry, current policy generation, exact claim and workspace bindings are
revalidated at admission and immediately before native create. Leases span
physical I/O and handle cleanup. No registry/policy/health metadata mutex spans
I/O. Quarantine refuses new admissions and waits for existing leased effects.
Historical actions, sessions and model/file/tool output never grant authority.

## Native path boundary

The guard reuses PR40's component-by-component verified parent traversal,
sensitive policy and parent identity checks. Absolute paths, traversal,
symlink parents and Windows reparse parents are refused. Windows reserved
devices, ADS, trailing-dot/space and unsafe native component spellings are
refused. Canonical opened names also prevent sensitive-parent DOS short-name
aliases. Target symlinks/reparse points and existing files/directories are
refused; no pathname fallback exists. Detectable parent substitutions fail
closed. The final parent must already exist.

On POSIX, `mkdir(final_name, mode=0700, dir_fd=verified_parent_fd)` atomically
fails on any existing name. The host mode can only become more restrictive
through umask. The new directory is opened relative to that parent using
`O_DIRECTORY | O_NOFOLLOW`; descriptor and named-object identities/types and
parent identities are checked. Both the created directory and parent are
fsynced before live success. Unsupported opens/fsyncs fail closed with unknown
effect once mkdir may have happened. No hidden staging directory is created.

On Windows, `NtCreateFile` uses an inspected parent handle as RootDirectory,
one final component, FILE_CREATE (fail if exists), FILE_DIRECTORY_FILE,
FILE_OPEN_REPARSE_POINT and synchronous semantics. The requested directory
access is read/attribute access, without file-staging rename/delete rights.
Directory handles omit delete sharing. Native default ACL inheritance comes
from the host-configured workspace parent; this tool neither chooses nor
changes ACLs. The returned handle must describe a non-reparse directory and
the final name must still identify the same object. Handles close on success,
post-check failure and interrupted native open. There is no supported Windows
directory/parent fsync guarantee here; atomic visibility is not a POSIX
directory durability or power-loss promise.

## Crash seams and reconciliation

Normal live native success plus post-checks yields CONFIRMED_APPLIED. Existing
destination refusal yields CONFIRMED_NOT_APPLIED. A proven interruption before
the final create seam yields NOT_INVOKED under the existing Stage-5 contract.
Once native create may have begun, any exception/interruption/post-check or
durability failure yields RECONCILE_REQUIRED. A possibly created final
directory is never deleted as cleanup.

Mkdir has no durable pre-publication evidence proving historical ownership.
A later existing directory may have been created by another process; a later
absent target may have been created and removed. Both remain OUTCOME_UNKNOWN /
HUMAN_CONFIRMATION_REQUIRED. The reconciler therefore performs no unnecessary
filesystem observation: neither observation would change that decision. It
still runs only under current FS_READ, exact registration, recovery claim and
policy/health leases. No matching-name/type inference settles mkdir success.
No automated retry occurs for the same uncertain action. A requested empty
directory stays free of ownership markers, including after live success.

## Workspace integration and trust exclusions

The sealed Workspace Agent catalogue is exactly `inspect`, `list_directory`,
`read_file`, `create_directory`, `write_file`. Ordinary Agent/default tools/chat
remain read-only. A model can create `notes`, then write `notes/summary.txt` in
the same turn; it cannot implicitly create `a/b/c`. The standing SYSTEM_PROMPT
is unchanged; host instructions retain output distrust and add these limits.

Both mutations consume the same 256-action admission bound before preparation
or physical effect. Earlier successful actions remain auditable if a later
create is refused. Failure/uncertainty latches mutation failure; model prose
cannot turn the CLI result into success. Startup recovery precedes model
execution, never executes historical AUTHORIZED records and never retries
uncertain mkdir. Human-required reconciliation blocks a new model turn.
Private storage, ownership fencing, full-session capacity/temp recovery and
public categorical logging/action truth retain the PR41 contracts. Browser,
Gmail and their session domains are unchanged.

The configured workspace and endpoint are trusted host configuration. This
boundary protects against untrusted model/file/tool input and unsafe path
objects. It does not defend against malicious same-user code or administrator/
root namespace relocation. POSIX descriptors remain attached to directory
objects after hostile relocation; component checks do not freeze namespace
ancestry. No sandbox or comprehensive race-proof claim is made.

Explicit non-features: recursive mkdir, replacement/editing/append, deletion,
rename/move/copy, shell/process execution, chmod/ACL editing, browser or Gmail
mutation, and arbitrary host filesystem access.
