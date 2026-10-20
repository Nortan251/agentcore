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

File replacement and update-read require native Windows (`os.name == "nt"`
and `sys.platform == "win32"`), following the accepted
[file-publication support policy](filesystem-write-v1.md#guarantees).
Linux, macOS and other unsupported hosts refuse before traversal, staging,
metadata changes or publication. There is no POSIX staging or pathname rename
fallback. No usable replacement revision is issued there; ordinary `read_file`,
`inspect`, `list_directory` and Directory Create retain their existing contracts.
Both tools remain registered with explicit platform descriptions.

The old POSIX stage-inode check followed by `os.replace(stage, target)` could
publish a substituted namespace binding. It is removed, rather than replaced
with ownership, mode, mount or pathname heuristics. This containment does not
claim a working secure POSIX writer; future POSIX support is separate work.

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

The Windows replacement flag is not a perfect filesystem
compare-and-swap or intrinsically replace-only syscall. Workspace Host ownership
serializes AgentCore's mutation domain. Observable changes are detected by the
snapshot and final comparison. A noncooperating same-user process changing or
removing the target after that final comparison/during native rename remains
outside the existing host boundary; at that exact excluded seam native rename
may publish after external removal. No stronger CAS claim is made.

## Crash truth, cleanup and recovery

Staging cannot start before protected intent, durable action record and
physical-attempt claim. Proven interruption before possible publication is
NOT_INVOKED; staging failures leave the old final file intact and clean only
verified owned stages. A definite stale check before native rename normalizes
CONFIRMED_NOT_APPLIED. Once replacement may have happened, exceptions,
interruption, flush failures or lost returns produce RECONCILE_REQUIRED, never
an inference of no effect from an exception.

Unsupported update-read reports `filesystem_publication_unsupported` without
issuing a revision or admitting an action. A previously admitted replacement
refused by the platform guard is NOT_INVOKED / CONFIRMED_NOT_APPLIED with
`filesystem.unsupported_publication`, no stage and no modified target. The
executor checks possible physical entry before classifying support errors;
post-entry exceptions remain uncertain.

Legacy uncertain POSIX replacements cannot be settled by matching current
bytes, metadata or absence: their historical publication identity is unverified.
They remain OUTCOME_UNKNOWN / HUMAN_CONFIRMATION_REQUIRED, without retry or
schema/protected-evidence changes.

On supported Windows, uncertain replacement history always remains
OUTCOME_UNKNOWN/HUMAN_CONFIRMATION_REQUIRED under current FS_READ and exact
recovery leases. The protected intent authenticates the original snapshot but
contains no action-specific published-stage identity or completion receipt.
The reconciler therefore does not infer historical application from current
filesystem observations, and does not traverse the target to make that inference.
The original object cannot be falsely confirmed even when OLD and NEW bytes
are identical; a distinct matching object is also insufficient evidence.

The exclusive Workspace Host lock serializes AgentCore, not other principals
with parent namespace rights. Such a principal could move a pre-existing
host-owned sibling with matching bytes and security into place after this
action failed before native rename. Current-token ownership, changed identity,
preserved metadata, ancestry and matching content would all pass without this
action having published. No broader concurrency exclusion is introduced.
OLD could also have been restored after publication; absence could follow
removal. No observation proves non-application or authorizes automatic retry.

Normal native execution still confirms success after owned-handle publication
and flush return successfully, including identical-content replacements. There
is no no-op rejection or content shortcut. A genuine post-publication interruption
without a durable success result now requires human confirmation, even if the
new stage is currently at the target. Automatic crash confirmation would require
separately designed authenticated action-specific publication evidence; the
journal schema and protected intent format are unchanged in this repair.

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
