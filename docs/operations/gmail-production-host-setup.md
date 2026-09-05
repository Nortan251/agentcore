# Production Gmail host setup and private operator interface

**DO NOT RUN LIVE SEND BEFORE THIS PR IS AUDITED AND MERGED**

All real-account commands in this document are **future human operations**.
Development, tests and PR completion must not perform real consent, use a real
token/account, contact Gmail Internet endpoints or send email. The delivery state
is **READY FOR HUMAN-CONTROLLED SMOKE TEST AFTER AUDIT AND MERGE**.

Use a trusted Windows desktop and the audited AgentCore installation. Production
credential storage remains Windows current-user DPAPI; other platforms fail
closed. Linux CI exercises synthetic credentials and real POSIX fork fencing.
Do not expose this CLI as a model tool, generic shell, scheduler or background
service. No new production dependencies are required.

## Explicit host composition

`open_local_services()` opens the protected Google vault and existing
`GoogleConnectionService`, `CredentialRuntime`, installation integrity key,
protected payload/evidence stores, ActionJournal, durable approval store and
authenticated host correlation. Opening services does not enable tools.

`GmailHostComposition` requires those host-owned services, a private operator
and the actual `GmailMutationHttp`. It creates an isolated `ToolRegistry`,
adapter registry, `DurableApprovalAuthority`, `PermissionPolicy`, `ToolExecutor`
and `ActionRecoveryCoordinator`. Its default mode is INSPECT.

| Mode | Explicit permissions and behavior |
| --- | --- |
| INSPECT | No send registration or authenticated network opt-ins. Offline status/review and startup classification. |
| PREPARE | Private authenticated primary-profile read; EMAIL_SEND disabled. One preparation, no approval or POST. |
| LIVE_SEND | Email-send and primary-profile read opt-ins, CONFIRM by default; durable approval always required. |
| RECONCILE | Separate authenticated reconciliation read opt-in; EMAIL_SEND disabled; existing actions only. |

Explicit stricter host capability blocks still apply. Ordinary EMAIL_SEND and
generic credential access remain default blocked. No change auto-wires `Agent`,
`AgentDependencies`, `default_tools()` or model-visible Gmail reads. A transport,
connection, scope, config, environment variable, model request, saved state,
previous approval or journal cannot select LIVE_SEND.

The private profile tool accepts no arguments and runs through the normal
authenticated-read executor. It uses the reviewed fixed
`me/profile?fields=emailAddress` GET with the exact GMAIL_SEND lease.
Its ToolResult contains only a verification boolean; the address stays in a
one-use private host result. This supplies no send permit. The existing send
preflight repeats primary verification under the actual attempt's leases.
See Google's [profile contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile).

```text
explicit host mode -> private primary read -> exact protected preparation
  -> new PENDING durable request for the same action
  -> ToolRegistry -> validation -> current PermissionPolicy -> ToolExecutor
  -> private review/challenge inside durable approval -> consume approval
  -> ActionJournal AUTHORIZED -> IN_FLIGHT -> credential/runtime preflight
  -> one POST maximum -> protected evidence -> categorical durable truth
```

The operator never calls `Tool.run()` or `GmailMutationHttp.send()` directly.
Signed identity, exact connection/profile, current registration/policy/expiry,
durable approval and recovery claims remain authoritative.

## Local configuration and privacy

The CLI asks Windows for the Local AppData known folder; it does not derive its
configuration location from environment variables or the working directory:

```text
<Windows Local AppData>\AgentCore\GmailSmokeHost\
    oauth-client.json         host-supplied Desktop client configuration
    installation-key.json    existing installation integrity-key format
    gmail-host.json           authenticated opaque action/preparation correlation
    google\connection.dpapi  existing protected credential store
    actions\                 categorical ActionJournal
    approvals\               durable approval records
    payloads\                protected exact intent/MIME
    evidence\                protected provider evidence
```

There is no config-path/URL flag, token flag, environment credential lookup,
hidden client identity or repository fallback. The production location must be
local and outside a Git checkout. Initialize it explicitly after audit and merge:

```powershell
python -m agentcore.gmail_host init
```

Initialization creates the key once and an empty authenticated slot. Opening an
existing domain never regenerates missing keys or missing/corrupt correlation.
Partial initialization requires private investigation; never reset a used
domain or delete history to make it appear unused.
If payload creation commits but host slot publication fails or the process
crashes, the orphaned payload makes the domain unavailable. Reopen and another
preparation fail closed, including abandoned temporary payload files. Preserve
the private files for investigation; no automatic cleanup or second proposal
is allowed. The two existing ownership-lock filenames are the only ignored
entries in an otherwise unused payload directory.

The host reuses the existing pinned-directory/atomic-file backend with a separate
closed `gmail-host.json` filename. Ownership excludes another open host process.
Windows DACLs restrict private directories/files to the current user and SYSTEM.
UNC/device/ADS paths, symlinks, junctions/reparse points and multiply linked
private files are unsupported. Files are flushed before atomic publication;
portable test storage also fsyncs directories. No new Windows directory-fsync
or universal power-loss guarantee is claimed.

In a private local editor, create `oauth-client.json` in that initialized
directory with this entire supported shape:

```json
{
  "client_id": "REPLACE_WITH_DESKTOP_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "REPLACE_WITH_DESKTOP_CLIENT_SECRET"
}
```

These are placeholders, not usable credentials. The secret is optional when the
Desktop client does not require it. Copy only these fields from the host-only
Desktop client download; its nested Google download format is not this host
format. No other keys are accepted. Never commit the real file/download or put
values in chat, PRs, shell commands/history, AgentConfig or session state.

Refresh material persists only in the existing protected Google vault. Access
and ID tokens remain volatile. The authenticated host slot contains only the
reserved action ID and exact foundation handle, never mail or credentials.
Payload/evidence stores are authenticated **plaintext**, not encryption at rest;
their DACLs remain essential. The client file and installation key also depend
on private filesystem access controls. Same-user malicious code, administrators,
debuggers, dumps, terminal recording and backups remain outside this boundary.

## Human Google Cloud checklist

Official documentation checked on 2026-08-31:

1. Create/use a development/testing Google Cloud project; enable the Gmail API.
2. Configure OAuth consent and audience. When using Testing status, add the
   chosen dedicated/test Google account to the configured test users.
3. Create an OAuth client of type **Desktop app**. Keep its download host-only
   and copy its client ID/optional secret into the private host file.
4. Review the five scopes below in Cloud configuration and browser consent.

Google's [Gmail setup guide](https://developers.google.com/workspace/gmail/api/quickstart/python)
documents API enablement, consent and Desktop client creation. Use its Cloud
setup instructions only, not its sample token-file/application code as an
alternative to AgentCore's protected lifecycle.

Installed applications do not support incremental authorization. AgentCore
requests the complete profile using system-browser consent, offline access,
new state/nonce, S256 PKCE and a one-shot loopback callback. It does not expand an
existing refresh grant. See Google's
[installed-app guidance](https://developers.google.com/identity/protocols/oauth2/native-app).

Testing projects authorize only configured testers, up to Google's documented
100-user limit. Testers can encounter an unverified/test-access warning.
For this Gmail profile, testing authorizations and offline refresh tokens expire
seven days after consent; the identity-only exception does not apply.
Workspace/Advanced Protection restrictions can also block consent. Public
production use of sensitive/restricted scopes can require verification.
See Google's [app audience guidance](https://support.google.com/cloud/answer/15549945?hl=en).
Read warnings deliberately. Do not automate Cloud Console or consent, bypass
warnings/restrictions, scrape consent, or silently publish the project.

## Exact scope profile

The CLI always requests `GoogleAccessProfile.GMAIL_SEND`, exactly:

```text
openid
email
profile
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

Google classifies gmail.send as sensitive send-on-your-behalf access, and
gmail.readonly as restricted view-mail/settings access.
See [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).
Here read access supports private primary verification and Sent reconciliation;
it does not expose the send connection to model-visible mail reads. There is
no gmail.modify, gmail.compose or full-mail grant.

## Operator commands and one preparation

Private operations require an actual unredirected Windows console. Disable
transcription, terminal logging and recording. Do not use pipes, redirection,
command substitution or an agent-controlled terminal.

```powershell
python -m agentcore.gmail_host status
python -m agentcore.gmail_host connect
python -m agentcore.gmail_host prepare-smoke-test
```

Status reads categorical local connection/profile state only, with an abbreviated
connection correlation; it never refreshes or contacts Google. Reopen can report
`refresh_required`, not current access. Connect explicitly enters the existing
Google lifecycle. Callback receipt is not connection success: only verified
identity and confirmed durable credential commit produce CURRENT.
Connect/scope presence does not enable mutation. The narrow CLI refuses
reconnect once a preparation exists because a new connection cannot adopt it.
An old preparation's provider/account/connection binding is checked before any
OAuth refresh or profile/probe I/O, including after a trusted external reconnect
and process restart. A replacement connection is rejected without contacting it.

Preparation may refresh the **same** connection through the host lifecycle.
It then obtains the primary Gmail address through the private executor read and
sets the sole To recipient to that exact verified address. There is no recipient
flag/prompt. Subject/body use hidden console input, never command-line arguments.

Compatibility note: a connection that already encountered the pre-repair
`oauth_identity_invalid` result for a refresh response without `id_token` has
durably disabled unattended refresh. The protected record deliberately stores
only the conservative `refresh_usable` decision, not a provider failure reason,
so an upgrade cannot safely distinguish that historical result from a genuine
identity mismatch or uncertain refresh and must not clear it. After deploying
the repair, deliberately run `connect` again before preparation for an affected
connection. Do not edit or delete the protected credential record manually.
Bounds are 160 UTF-8 subject bytes and 1,024 normalized body bytes, at most eight
lines; the console requests a harmless single-line body. Empty content,
HTML-looking markup, reply/forward prefixes, Cc/Bcc, attachments and unsupported
fields are rejected.

The foundation generates and saves Message-ID, Date and exact MIME once. Private
review displays sender/To, empty Cc/Bcc, exact normalized subject/body, Message-ID
and action/preparation correlation, with:
`THIS WILL SEND A REAL EMAIL IF EXECUTED`.
Only this private channel receives content; generic logging does not.

Preparation ends at PREPARED: no approval, journal authorization or physical
attempt. Save the printed opaque action ID. It is initially a reserved
correlation, not evidence of authorization. Substitute it below:

```powershell
python -m agentcore.gmail_host review act_FROM_HOST_OUTPUT
python -m agentcore.gmail_host action-status act_FROM_HOST_OUTPUT
```

Offline review is labeled as stored history; it is not current verification.
Restart restores the same handle, MIME, Date, Message-ID and action ID. There is
one preparation slot, including after success/failure, with no reset, second
proposal, risky-repeat or scheduler command. Missing/corrupt continuity fails
closed.

## Two independent live gates

**DO NOT RUN LIVE SEND BEFORE THIS PR IS AUDITED AND MERGED**

After independent audit, merge, green required CI and the human's explicit
choice of the real test account and self recipient:

```powershell
python -m agentcore.gmail_host send-smoke-test act_FROM_HOST_OUTPUT --live-send
```

1. This exact command/flag creates LIVE_SEND for this process only. Omitting the
   flag stops before services or network are opened.
2. Inside durable approval, the human reviews the exact current prepared message
   and types a fresh random challenge containing this action ID within 120
   seconds. The one-use ticket binds the request/action, preparation/digest,
   connection generation, process and thread; it is rechecked after input and
   cleared on exit.

A cached challenge, model response, persistent toggle or historical approval
cannot satisfy these conditions. Every explicit never-attempted continuation
gets a new PENDING durable request. The executor authenticates its complete
binding; the private decision still must be recorded and atomically consumed by
`DurableApprovalAuthority`. Current policy/registration/expiry/credentials are
rechecked at the physical boundary.

Any existing attempt prevents another send through this smoke path, including
FAILED/NOT_INVOKED. AUTHORIZED with zero attempts can continue the same action
with new review/approval. Consumed approval with a missing journal fails closed
rather than recreating an approved action after storage/publication doubt.
Startup converts interrupted IN_FLIGHT to uncertainty, even during preflight.

## Categorical truth and reconciliation

| State/result | Operator behavior |
| --- | --- |
| PREPARED | No journal authorization or attempt; review the same preparation. |
| AUTHORIZED, zero attempts | Same-action continuation with fresh live gates/approval. |
| SUCCEEDED | Do not send again; verify Sent, Message-ID and no duplicate privately. |
| RECONCILE_REQUIRED / IN_FLIGHT | **DO NOT SEND AGAIN.** Inspection/reconciliation only. |
| PROBE_LATER / zero matches | No conclusive Sent match found; effect remains uncertain. |
| Human confirmation required | Preserve uncertainty; no force-settle/send option is provided. |
| DENIED / FAILED / interrupted | Inspect the same action; never automatically create another message. |

The effect display uses final journal truth when settled. The active unresolved
probe counter is existing quota metadata, not lifetime HTTP request count;
terminal settlement clears it.

For an existing uncertain action only:

```powershell
python -m agentcore.gmail_host reconcile act_FROM_HOST_OUTPUT
python -m agentcore.gmail_host action-status act_FROM_HOST_OUTPUT
```

The separate read-only composition obtains current same-connection credentials,
claims through `ActionRecoveryCoordinator` and asks its executor for the bounded
Sent probe. It cannot send, create an action, change the bound account/connection,
widen the 64-probe quota or turn absence into retry authority. Pre-provider
denials retain the existing no-I/O quota refund. No polling is started.
If refresh expiry requires reconnect, the old uncertain intent cannot be adopted
by a new connection; preserve truth and investigate privately.

Send composition ends when the command exits. Status/review/reconciliation do
not enable it. After truth checks finish, the human may explicitly disconnect:

```powershell
python -m agentcore.gmail_host disconnect
```

This removes local access/refresh authority, not remote Google consent. Do not
disconnect prematurely while reconciliation needs the connection. Keep the
slot, journal, approvals and protected evidence.

## Synthetic development boundary

Tests exercise the production factory/parser, real stores, durable consumption,
executor and recovery coordinator. OAuth, terminal input and network boundaries
are synthetic. The proven memory-socket fixture denies DNS and real socket
connect/send/sendall/sendto; crash subprocesses install it before opening
services. Production HTTP serialization, framing and matching remain active.
There is no CLI test flag or alternate approval semantics.

Synthetic success does not establish live Gmail transformations or recipient
delivery. The next action after audit and merge is the
[single human-controlled self-send and verification](../testing/gmail-send-controlled-smoke-test.md),
not another coding phase or broader rollout.
