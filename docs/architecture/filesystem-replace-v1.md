# Filesystem Replace v1

The explicit Workspace Host exposes `read_file_for_update(path)` and
`replace_file(path, revision, content)`. Update-read is `{FS_READ}` and read-only;
replacement is `{FS_WRITE}` and mutating. Ordinary `Agent()`, `default_tools()`
and `agentcore chat` remain read-only and omit both tools. The Workspace
catalogue is exactly `inspect`, `list_directory`, `read_file`,
`read_file_for_update`, `create_directory`, `write_file`, `replace_file`.

## Complete reads and ephemeral revisions

Normal `read_file` remains inspection and can truncate. Update-read uses the
strict mutation boundary, reads an existing regular single-link file in bounded
chunks, and decodes strict UTF-8. It rejects unsafe/sensitive/native-invalid
paths, symlinks/reparse points, directories/special files, missing files and
unsupported metadata. It returns one deterministic JSON object containing
`content` and an opaque `revision`. File text is JSON-escaped and cannot spoof
the top-level host metadata. Tool/file content remains UNTRUSTED DATA.

The complete ASCII-escaped JSON, 68-character revision and exact conversation
tool-result frame must fit the current `conversation.max_tool_result_chars`.
Oversized update-reads fail with `update_read_too_large`, without issuing a
token. The normal default stays 2000 characters. Bytes are independently capped
by the minimum of the current file-read and durable write bounds before and
during reading. This is intentionally a small-file workflow; ordinary reads,
global context limits and large-file editing are unchanged.

Each host owns a bounded in-memory registry. Random tokens carry 256 bits of
host-generated entropy, are PID/thread/host-instance-bound, cannot be selected
by config/model input and cannot be serialized. Capacity is the smaller of
256 and the configured tool-round/call product (96 by default). Snapshots hold
metadata and digests, not retained raw file bodies. Tokens are consumed once
at replacement preparation, including path-mismatched use, and cleared on
host close. Replayed/fabricated/foreign tokens fail before filesystem observation.

The Workspace-only session-store projection preserves useful complete TOOL
content but saves its top-level revision as `<expired-revision>`. Current
in-memory model protocol keeps the live revision. Ordinary and Browser
persistence are unchanged. A resumed host starts with an empty registry;
history/journal records never restore revision authority. Model prose or file
text containing revision-like strings does not populate the registry.

## Snapshot, authority and protected intent

A trusted snapshot binds canonical workspace fingerprint, normalized relative
target, verified parent identities, native object identity, exact byte size,
SHA-256 of complete old bytes, change metadata, bounded security metadata,
revision schema and exact read/replacement tool version. Native IDs, absolute
paths and descriptors never appear in model-visible revision metadata.

The fixed route is registry validation -> current PermissionPolicy ->
ToolExecutor -> durable protected intent/ActionJournal -> one-use exact
filesystem mutation context -> WorkspaceGuard -> native publication. The
executor adds only the exact concrete `ReplaceFileTool` to its reviewed
filesystem mutation types. Subclasses/names/tags/capabilities do not confer the
context. Create/write/replace contexts cannot cross exact tool/workspace
bindings; PID/thread/one-use checks remain mandatory.

The host still requires current explicit `--enable-write`, AUTONOMOUS,
FS_READ and FS_WRITE. A revision grants no write capability. Replacement
preparation and current physical validation independently require FS_READ.
Policy/registration/health/claim leases and validation remain in force through
I/O and cleanup. No metadata mutex spans physical I/O.

Preparation first consumes the trusted revision, then reopens through the
strict boundary and requires exact snapshot equality. Changed bytes, identity,
link count, ancestry or metadata refuse with `stale_revision` before action
admission. Missing targets never become ordinary create behavior. A stale
comparison after admission but before publication records definite no-effect
truth; the model must update-read again.

Replacement uses `DURABLE_LOCAL_MUTATION` and
`LOCALLY_VERIFIABLE_OR_REVERSIBLE`, with FS_READ reconciliation. Protected intent
contains schema/tool/version, mode `replace`, target, full bounded old snapshot,
new UTF-8 content once, new size and digest. It never retains the old raw body or
revision token. Public records/approval/logs/CLI contain no paths, contents,
native IDs or tokens. Protected storage remains authenticated private
plaintext; its confidentiality still relies on the private host boundary.

New content obeys `filesystem.max_write_bytes` (at most 128 KiB). Conservative
worst-case protected JSON accounting is
`6*131072 + 12*4096 + 32768 + 16384 = 884736 < 1048576`: content escaping,
two bounded target spellings, at most 16 KiB full security descriptor rendered
as hex, and reserve for native IDs/schema/authenticated envelope. The global
protected payload ceiling is unchanged. Worst escaping is tested using the
real protected store.

## Native publication and metadata

On POSIX, verified parent descriptors and `O_NOFOLLOW` opens reject links and
special objects. Replacement requires current-user ownership and only ordinary
permission bits, preserving UID/GID and executable bits. Setuid/setgid/sticky
file modes are refused. Descriptor xattr enumeration must be available and
empty; ACLs/capabilities/security xattrs fail closed rather than being dropped.
A stage inheriting a default ACL is also refused. Platforms unable to prove
this metadata posture refuse the target.

Linux additionally requires bounded descriptor queries for `FS_IOC_GETFLAGS`
and `FS_IOC_FSGETXATTR`, as defined by the
[Linux filesystem UAPI](https://github.com/torvalds/linux/blob/master/include/uapi/linux/fs.h).
Every inode flag except the ordinary extent-storage flag is refused, including
nodump, immutable, append, noatime and nocow. Extended flags, nonzero project IDs
and extent-size policy hints are refused. The stage must match the permitted
old flags before publication. Unsupported ioctl/filesystem/architecture or
non-Linux POSIX metadata querying fails closed rather than losing policy state.

An exclusive random 0600 stage is created in the same verified parent after
durable intent and attempt claim. Required ownership is applied, but the stage
remains 0600 while exact bounded bytes are written and fsynced. Stage metadata
must match that private mode and permitted old metadata. The final target is reopened and fully
compared against the snapshot a second time. Parent/stage identities and current
authority are revalidated before descriptor-relative atomic `os.replace`.
After rename, the final file temporarily remains owner-private while the
original mode is restored through the owned descriptor, verified and fsynced.
Then the parent directory is fsynced. Other principals cannot read/execute a
partial stage under a public target mode. The final name never exposes a
partially written body. Failure after rename, including mode restoration,
requires reconciliation; a complete final file with unrestored metadata remains
UNKNOWN/HUMAN rather than applied. Temporary restriction can deny reads until
successful restoration; it does not broaden access.

On Windows, targets/stages use `NtCreateFile` relative to verified parents.
Target read handles omit write/delete sharing. Full security descriptor
retrieval requires **already-granted ACCESS_SYSTEM_SECURITY**, including an
already-enabled OS SeSecurityPrivilege. The host does not enable privileges,
elevate or introduce a CLI authority flag. Ordinary tokens lacking this access
categorically refuse update-read before any revision is issued; owner/DACL-only
queries are not treated as proof of an empty SACL. This availability limitation
is explicit and does not weaken the preservation contract.

Only current-token owner/default-owner ordinary single-link disk files with
bounded complete descriptors are eligible. Readonly/system, encrypted,
compressed, reparse, sparse/offline/integrity/recall and other unusual attributes
are refused. Bounded native EA/stream queries refuse extended attributes and
alternate data streams. The empty stage is created with a protected current-user
DACL, then locked with an empty protected DACL before writing through the
already-granted owned handle. New data-read/execute handles are denied even
after close, so failed deletion or process death cannot expose partial residue.
Old owner/group/full SACL are carried into the private descriptor. Only after
complete writing/flush and final validation does possible-publication truth
begin, before restoring the full old DACL/control semantics and ordinary allowed
attributes. Exact descriptor/metadata equality is required before native rename.
Replacement stages additionally use zero sharing. The old target remains pinned
without write/delete sharing through restoration and is closed just before
rename. The owned stage handle stays open through publication/flush.
Unsupported rights/APIs/security semantics fail closed.

After exact writes and FlushFileBuffers, the second comparison and current
authority checks precede full target metadata restoration and handle-relative
FileRenameInformation replacement. Uncertainty begins at descriptor restoration
because a completed residue could become readable after cleanup/close fails;
it is never definite no-effect. Pre-restoration residue remains unreadable even
when discard fails. Complete post-restoration crash residue can inherit the old
target's access policy; that possible disclosure remains journaled uncertainty.
Traditional NT replacement requires closing the inspected target handle before
publication. That loses the target's no-write/no-delete sharing protection at
the close-to-rename seam. No arbitrary DOS-path fallback is used. Stages and
parent handles close deterministically. The published file is flushed; Windows
does not promise POSIX parent-directory fsync durability.

Neither POSIX rename nor the Windows replacement flag is a perfect filesystem
compare-and-swap or intrinsically replace-only syscall. Workspace Host ownership
serializes AgentCore's mutation domain. Observable changes are detected by the
snapshot and final comparison. A noncooperating same-user process changing or
removing the target after that final comparison/during native rename remains
outside the existing host boundary; at that exact excluded seam native rename
may publish after external removal. POSIX advisory locks would not defeat a
writer ignoring them. No stronger CAS claim is made.

## Crash truth, cleanup and recovery

Staging cannot start before protected intent, durable action record and
physical-attempt claim. Proven interruption before possible publication is
NOT_INVOKED; staging failures leave the old final file intact and clean only
verified owned stages. A definite stale check before native rename normalizes
CONFIRMED_NOT_APPLIED. Once replacement may have happened, exceptions,
interruption, flush failures or lost returns produce RECONCILE_REQUIRED, never
an inference of no effect from an exception.

Reconciliation under current FS_READ and exact recovery leases confirms
application only when safe final ancestry, preserved metadata, size and digest
match NEW intended state. OLD state, absence, other content, unsafe objects or
unverifiable metadata remain OUTCOME_UNKNOWN/HUMAN_CONFIRMATION_REQUIRED. OLD
could have been restored after publication; absence could follow removal.
No automated retry uses history, the action ID or an old revision.

Startup reconciliation precedes model execution. Unresolved replacement blocks
the new mutation turn; historical AUTHORIZED actions are never automatically
executed. Replacement shares the 256-action admission ceiling and mutation
failure latch with both create tools. Update-read consumes no action capacity.
Earlier successful effects retain durable truth if a later replacement fails,
and later friendly model prose cannot turn failure/uncertainty into CLI success.

Normal success/failure removes owned stages. After ambiguous publication, cleanup
never deletes the final target blindly. A process crash can leave one random
stage per attempted replacement; bounded inert residue is preferable to data
loss. No exchange/backup publication or broad cleanup daemon is introduced.

Non-features: patches/diffs, chunks/large-file editing, append, delete,
rename/move/copy tools, chmod, recursive mkdir, binary writes, shell/process
execution, Browser/Gmail mutation, and writable ordinary Agent composition.
