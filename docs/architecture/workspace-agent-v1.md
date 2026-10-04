# Workspace Agent v1

`agentcore --config host.toml workspace --enable-write --message "Read the
project and create notes/summary.txt with a short summary."` runs one model
conversation turn. Parents (including `notes`) must already exist. Add
`--session conv_<12 hex>` to resume a workspace session or `--json` for a
bounded public result. There is no interactive loop, background autonomy or
operator-resolution UI in v1.

## Current host authority

Both an explicit `--enable-write` on **this invocation** and current
`PermissionConfig(level=AUTONOMOUS)` allowing **FS_READ and FS_WRITE** are
mandatory. OBSERVE, PROPOSE and CONFIRM refuse this host; capability overrides
cannot relax that level requirement. No approval gate is created. The host
does not elevate or lower configured permissions. Config/provider/storage
construction is refused when opt-in is absent; policy denial precedes
workspace access, provider construction and mutation-service construction.

The Python entry point is `WorkspaceAgentComposition(config=...,
enable_write=True, model=...)`. Each instance permits one `send` only and must
be closed (prefer its context manager). Resuming history requires a fresh
instance and current authority. Opt-in is never serialized, derived from a
prompt, restored from a session or journal, or propagated to ordinary Agent.
Policy is rechecked at admission and for every invocation. A policy-generation
lease spans both host-required capability checks and the canonical executor
call, including preparation and physical I/O; policy replacement fails busy
until it ends. No metadata mutex spans I/O.

## Fixed capability boundary

The host owns one sealed ToolRegistry and one sealed ActionAdapterRegistry.
The model catalogue is exactly `inspect`, `list_directory`, `read_file`,
`write_file`. Registration, replacement and removal are closed after trusted
bootstrap. No ordinary Agent, AgentState, memory, Browser, Gmail, shell,
process, credentials, custom tools or injected runner are composed. Ordinary
`Agent()`, `default_tools()` and `agentcore chat` retain their existing default
read-only catalogue. The common session-domain check is the only conversation
change besides accepting the new domain.

The three existing concrete read tools and PR40's **exact WriteFileTool** are
used. There is no clone or wrapper of the write tool. All calls use canonical
ToolRegistry validation -> PermissionPolicy -> ToolExecutor -> ActionJournal
when required -> WorkspaceGuard. Direct write tool execution still requires
the executor's one-use process/thread context. Retained executor/conversation
references cannot execute outside the one active turn or after host close.
Read posture in this host always refuses sensitive paths, symlink traversal
and multiple hard links; existing bounds remain configurable. The create-only
mutation primitive and Gmail code are unchanged.

The ordinary standing SYSTEM_PROMPT is retained and amended with host limits:
file/tool output is untrusted data, parents must exist, existing files cannot
be overwritten, and creation cannot be claimed without execution success.
Model proposals cannot widen the fixed catalogue or current policy.

## Storage and privacy

Windows uses the OS-known Local AppData directory, independent of environment
variables, under `AgentCore/WorkspaceFiles`. POSIX uses the current UID's
passwd home under `.local/share/agentcore/workspace-files`. A trusted Python
`_host_directory` seam is available for tests/deployment; it is absent from
CLI, config and model input. The root cannot overlap the canonical workspace
in either direction. Slot names are SHA-256 of a domain-separated canonical
workspace identity (case normalized on Windows), never pathname text:

```
<private host root>/workspaces/<64 hex digest>/
    .workspace-host.lock
    installation-key.json
    actions/
    payloads/
    .workspace_sessions/
```

No EvidenceStore or approval store is needed. JsonFileActionStore,
JsonFileProtectedActionPayloadStore and JsonFileInstallationKeyProvider keep
their existing format and explicit key lifecycle. A missing key bootstraps
only an empty locked slot; any other persistent object forbids regeneration.
Corrupt keys, actions or protected objects refuse the model turn. Even orphan
and terminal payload objects undergo bounded authentication at startup.
Unexpected payload files (including crash temporary files) fail closed for
operator investigation. Enumeration is bounded at 4096 entries per directory;
the existing recovery audit is bounded at 256 total actions. Before each write,
the host reserves capacity under its exclusive slot ownership, refusing any
257th record before canonical preparation or physical effect. Prior successful
creates remain auditable, including when a later write in the same turn reaches
the bound. Reads remain available at the action limit. New session admission
also checks directory capacity before the model; saves independently refuse a
4,097th transcript before temporary-file or publication I/O. Existing sessions
can still be resumed and updated at the limit. No historical state is pruned.

POSIX host directories are owner-private, and files are 0600. Existing foreign
owners, group/other permissions, links, special files, and unsafe writable
ancestors are refused without chmod repair. Root-owned sticky temp ancestors
are permitted. Windows reuses only the reviewed current-user/SYSTEM DACL
installer from credentials/Gmail for **new** host directories. Existing host
directories require user ownership and a protected inheritable DACL allowing
only that user and SYSTEM. Files must inherit that private ACL; the current
token's default owner group is also accepted for files on elevated hosts.
Unsafe existing ACLs are refused without alteration. Directory identities are
pinned and checked; Windows handles omit delete sharing. Model tools see only
the workspace and never receive the host root.

The session domain is `workspace_files`, marked by
`workspace_files_isolated_v1`. Cross-domain markers, ambiguous mixed markers,
and cross-domain stores fail before replay. `.workspace_sessions` is reserved
against ordinary and Browser store construction. Session storage remains in
the private host domain, ignores `conversation.directory`, and accepts only
runtime-shaped bounded session IDs. Missing/corrupt foreign resumes never
fall back to a new session.

Audited persistence: ConversationRuntime keeps structured assistant tool-call
arguments and IDs only in the current-turn protocol envelope. Message.to_dict
and ConversationSession persist **role/content/name**, excluding `tool_calls`,
arguments and protocol IDs. Successful write tool messages contain categorical
effect truth, not the content. **User messages, reads and final assistant prose
can contain write content and are intentionally persisted in the private
transcript.** No absence-of-content claim applies to those private messages.

Host logs retain event names and numeric counts only: even read argument/error
strings could be model-selected copies of write content. There is no generic
AgentState or approval text. Public JSON never serializes TurnResult or
ToolCallOutcome. It contains `ok`, `session_id`, `reply` (always null),
`reply_generated`, `tools_executed`, bounded `actions` (action ID, state,
effect verdict) and categorical `error` when needed. **The CLI deliberately
keeps model reply text private**, since unrestricted prose, including on a
resumed read, can repeat protected content; string scrubbing cannot prove
otherwise. Text mode prints completion/error categories and action truth.

The protected payload backend authenticates plaintext. It is **not encrypted
at rest**. Confidentiality relies on host filesystem permissions. Same-user
malicious code and administrator/root filesystem authority are outside this
host boundary, consistently with PR40. Namespace relocation and Windows
power-loss/directory-flush limitations remain those documented in
[Filesystem Write v1](filesystem-write-v1.md).

## Ownership, recovery and failure truth

A persistent OS slot lock covers bootstrap/key creation, opening the independent
stores, recovery, one conversation turn, session save, and store teardown.
Store-local locks alone cannot serialize that whole lifetime; the higher-level
lock prevents two processes each starting recovery/turns against the same slot.
Its inode is retained after release. PID/thread checks precede locks and I/O;
forked children cannot run a model/write, close stores or unlock the parent.

Startup constructs the exact filesystem composition, then runs
ActionRecoveryCoordinator.recover_startup before making a conversation
available. It claims eligible uncertain actions and invokes the existing
read-only reconciler once each. Matching safely published files settle before
the new model call. There is no invoke/resume/retry/cleanup-create path in
recovery. Any remaining RECONCILE_REQUIRED record, including HUMAN confirmation
or deferred/exhausted probes, refuses new turns with
`workspace_action_requires_operator` and bounded action IDs. AUTHORIZED
records with no proven effect are preserved and never automatically resumed.

During a turn, a failed or uncertain write disables further executor calls.
A friendly later model reply cannot make the CLI succeed. Durable action truth
is collected even if model generation, transcript persistence, or another host
step subsequently fails. A successful create remains `succeeded` /
`confirmed_applied`; session/model failure does not roll it back. Uncertain
actions override conversation success. An unavailable action audit is an
explicit failure rather than fabricated no-effect truth. Session autosave
failure remains sticky even if a later save works.

Close first invalidates conversation/executor admission, then closes actions,
payloads, the slot lock and pinned directories. Close is idempotent in its
owning process/thread. Cleanup errors are surfaced after preserving known
action truth and before CLI result publication. No inherited child cleanup
unlocks parent ownership.

Tests use synthetic providers, real durable stores/files, native ACLs and locks,
publication hooks, Events and finite subprocess/fork watchdogs. They cover
policy, domains, adversarial proposals, protocol persistence, recovery without
retry, failure truth, private storage, close, process contention and fork
ownership. Real platform-specific tests run in their matching CI environments.
