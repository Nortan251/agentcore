# Protected credentials and verified Google identity connections

Implementation baseline: `a03d9063050a2046236177754cbb1d108a3382df` (PR #22).
Official protocol guidance checked on 2026-08-30.

This stage adds an explicit **host** service for establishing, verifying,
persisting, restoring, refreshing, and locally disconnecting a Google identity.
It adds no model-facing connection tool, Gmail API call, email capability,
remote revocation, default credential discovery, or paid infrastructure.
The [credential foundation](credential-identity-foundation.md) remains the
execution authority contract. Stage 5 action and approval schemas are unchanged.

## Ownership and composition

```text
trusted host GoogleOAuthConfig + system browser
  -> one-shot loopback callback + PKCE exchange
  -> Google signature and OIDC claim verification
  -> protected refresh record, committed atomically
  -> volatile access-only credential generation
  -> explicit CredentialRuntime binding
  -> existing ToolExecutor permission/dispatch leases
  -> AuthenticatedReadProvider / short-lived CredentialLease
```

`GoogleConnectionService` manages credentials, not tool execution. It cannot
grant capabilities, approve an action, execute `Tool.run`, or establish effect
truth for an external mutation. `CredentialRuntime` still attaches to exactly
one vault and executor. Its secret-free bindings and execution permits are
unchanged. Providers receive only the current **access token**, never the refresh
token or OAuth client secret, through the existing synchronous lease.

`ProtectedCredentialVault` implements that same access lease interface plus a
separate private manager seam. The host service and runtime receive different
live object authorities. References, serialized status, model arguments,
approvals, history, and ActionJournal records cannot construct those authorities.

Each protected directory is one domain: one stable Google account and one
installed-app client. A new explicit authorization may reconnect that exact
account; a different account or OAuth client requires another explicitly chosen
domain. There is no default account or email-based account lookup. Multiple
same-domain instances, including within one process, are refused while open.

## Windows protection and filesystem boundary

Production uses `WindowsDpapiProtector`: pywin32 calls Windows
`CryptProtectData` / `CryptUnprotectData` in **current-user scope**, with
`CRYPTPROTECT_UI_FORBIDDEN` and without `CRYPTPROTECT_LOCAL_MACHINE`. DPAPI
provides both confidentiality and integrity; AgentCore implements no encryption
algorithm or key management. A fixed application purpose and canonical directory
binding are supplied as additional entropy. This is not a machine-only binding:
Windows profile/domain recovery and roaming behavior still apply. See
[Microsoft's DPAPI contract](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata).

The complete version-1 record is protected together:

- refresh material and any provider-supplied refresh expiry;
- canonical `(provider, issuer, subject)` and installed-app client ID;
- stable account and connection IDs, current reference, generation, scopes and
  access expiry metadata;
- replaced/revoked reference tombstones and whether refresh needs fresh consent;
- original OIDC nonce and optional authentication time, for refresh validation.

Access tokens and ID tokens are **not persisted**. The optional client secret is
host configuration and is not persisted by this store. It must be reinjected by
the host on restart, kept outside ordinary application config/model state.
Email, display name and profile images are not retained in this stage.

Outside the protected bytes, the OS can observe the directory and fixed
filenames, existence, ciphertext size, timestamps and file-access metadata.
Host-facing `ConnectionStatus`/`CredentialInfo` contain only inert metadata.
No provider result is intended as an export of that binding metadata.

The backend pins an absolute local directory, rejects symlinks, junctions,
reparse points and multiply linked records, and bounds each ciphertext at 4 MiB.
On Windows it pins directory ancestors with handles that deny deletion/rename,
sets a protected DACL allowing the current user and SYSTEM, and creates temporary
records inside that directory. New files inherit that DACL. Network/device/ADS
paths are unsupported. A non-shareable ownership handle excludes a second
writer. The host should use a dedicated directory beneath its private local
application-data area, outside repositories and agent workspaces.

Windows may initially assign a newly created directory to a default owner group
from the process token, especially on elevated hosts. The backend accepts only
the token user or its OS-reported default owner, then sets the directory owner
to the current user when needed, with the same private DACL. It does not enable
ownership privileges or accept an unrelated owner. See Microsoft's
[new-object ownership rules](https://learn.microsoft.com/en-us/windows/win32/secauthz/owner-of-a-new-object).

Linux/macOS **production storage is unsupported and fails closed**. Tests may
explicitly supply a protector with `test_only=True`. The test protector lives
under `tests/`, uses cryptography's Fernet with synthetic material, and is never
selected automatically. The portable test backend uses mode 0700/0600, an OS
`flock`, and POSIX directory fsync. No production plaintext fallback exists.

## Persistence and crash consistency

An empty protected domain is committed at creation. An existing domain with a
missing record is corrupt, not an empty vault. Strict decoding checks schema,
keys, duplicate JSON keys, types, UTC timestamps, IDs, generation relationships,
one current reference, state/material consistency and size bounds. Missing,
corrupt, substituted, or future-schema records are never silently reset.

All generations, including tombstones, count toward a 4,096-record bound. A
capacity failure occurs before starting another refresh; it does not prune or
reuse identities. Archival/compaction requires a separate design. Each
successful refresh gets a fresh random reference and increments generation;
reconnect retains the account ID but creates a different connection ID.

Commit order is: construct a complete candidate; protect it; exclusively create
a same-directory temporary file; write/flush/fsync ciphertext; check the owned
domain and current file again; atomically `os.replace`; fsync the directory on
POSIX; then publish the volatile access generation. The previous protected file
is never removed before replacement. A failure before replace keeps the previous
record; a failure after replace but before confirmed durability poisons the open
instance and requires reopen. It is never reported as a successful publication.
This includes an exception or host interruption after the backend replaced the
file: cleanup cannot rewrite the previous snapshot over that newer generation.
Interrupted loads release the OS ownership handles and strip exception text.

Windows provides record-content fsync and atomic replacement here. This code
does **not** claim a Windows directory-entry fsync or a universal sudden-power-loss
guarantee. Filesystem/hardware guarantees, backups and OS caches remain relevant.
The process-crash tests cut before and after actual replacement and reopen the
surviving complete generation.

Google and the filesystem do not share a transaction. If Google rotates a
refresh token and the response is lost, or the process dies before committing
the replacement, the retained old token may no longer work. Reconnection can
be necessary. Atomic local replacement prevents avoidable local token loss; it
cannot guarantee remote validity in this interval. There is no blind code or
refresh retry, and no claim of distributed exactly-once behavior.

## Desktop authorization and callback rules

The host supplies `GoogleOAuthConfig`, never model arguments. Client ID is
required; the installed-app client secret is optional `SecretMaterial` and stays
outside generic configuration serialization. Provider, issuer, endpoints and
requested scopes are fixed by this implementation. Only `openid email profile`
is requested, with `access_type=offline` and explicit `prompt=consent` when the
host deliberately connects/reconnects. Gmail scopes are not requested.

The protocol follows Google's current
[installed-app guidance](https://developers.google.com/identity/protocols/oauth2/native-app):

1. Bind `127.0.0.1` on port zero, letting the OS choose an available port. Use
   exclusive address use on Windows. Never bind a network interface or accept a
   caller-supplied listen address.
2. Generate fresh cryptographic state, OIDC nonce and an 86-character PKCE
   verifier. Send its SHA-256/base64url S256 challenge, never a plain challenge.
3. Open the system browser with the authorization-code request. There is no
   embedded webview, OOB redirect, manual code entry or copy/paste fallback.
4. Accept only a bounded GET request on the exact root path and exact bound
   host/port. Reject fragments, absolute-form requests, body framing, duplicate
   headers/parameters, malformed escapes, unknown callback fields and conflicting
   success/error fields. Compare state exactly with `hmac.compare_digest`.
   Callback `scope`/`authuser` values are never identity or grant authority.
5. The first valid success or denial consumes the callback. Invalid probes do
   not consume a legitimate state; attempts have a bounded request count and
   lifetime (180 seconds by default, host-configurable from 1 to 600). Both the
   injected monotonic clock and an independent real monotonic bound apply.
6. Exchange a received code once, using the identical redirect URI and verifier.
   A received callback is only a received response; its browser page explicitly
   directs the user back to the host for the actual connection result.

The token and signing-certificate HTTP hosts/paths are fixed. HTTPS uses default
certificate/hostname verification and no environment proxy discovery, redirects,
debug output or automatic retries. Codes, verifier, optional client secret and
refresh token go in the POST body, never the token endpoint URL. Responses are
bounded and strictly parsed. The socket timeout is 10 seconds per I/O, not a
hard preemptive deadline for a slow server. The host can locally disconnect or
close while a remote operation is outstanding; its later result cannot publish.

OAuth HTTPS uses a verified-client SSLContext with default system CAs and modern
strict X.509 verification, but never enables TLS session-key logging. It avoids
the SSLKEYLOGFILE hook in Python's convenience context factory without mutating
the shared process environment. Code exchange, refresh and certificate-fetch
regressions verify this even when SSLKEYLOGFILE is set. See
[Python TLS context behavior](https://docs.python.org/3/library/ssl.html#ssl.create_default_context).

## Identity verification

`google-auth[cryptography]>=2.57,<3` is the maintained Google dependency used for
RS256 verification and Google's issuer/audience/time checks. `cryptography`
provides the RSA implementation; there is no AgentCore RSA/JWK implementation.
The verifier fetches only the fixed Google OAuth signing-certificate endpoint.
It rejects unsupported JOSE headers/algorithms and duplicate JWT JSON keys.
Certificate fetches are currently uncached; failures are closed. See
[Google's verifier API](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html).

After cryptographic verification, the service separately validates required
claim types and strict expiry, exact client audience, optional authorized-party
client, issue/not-before time, stable ASCII `sub`, nonce, optional authentication
time continuity and optional access-token hash binding. Both Google-documented
issuer spellings are accepted by verification and canonicalized to
`https://accounts.google.com`; `AccountIdentity.provider` is always `google`.
No unverified email, decoded JWT payload, or callback field can establish the
account. This follows Google's
[OIDC identity contract](https://developers.google.com/identity/openid-connect/openid-connect).

Initial authorization requires an ID token, refresh token and explicit granted
scopes. Refresh also requires a newly verified ID token; missing proof requires
reconnection rather than publishing unverified access. A refresh ID token may
omit nonce; if present it must match the stored original nonce, as specified by
[OIDC refresh validation](https://openid.net/specs/openid-connect-core-1_0.html#RefreshTokenResponse).
Google's `userinfo.email`/`userinfo.profile` grant spellings normalize to the
requested identity scopes. Granted scopes may shrink, never expand. An omitted
refresh scope means unchanged under the OAuth response contract; explicit null
is invalid. `openid` is required for this identity-only lifecycle.

## Refresh concurrency and generation publication

The host calls `ensure_current(reference)` or `refresh(reference)`. Providers
do not refresh, select credentials, or switch accounts. Reopen restores the
exact protected account/connection and last reference as inert metadata; it
requires refresh before any access lease can succeed.

Preparation checks exact current reference/state and refresh expiry under the
vault mutex. It reserves one operation object, then releases the mutex before
token HTTP and certificate verification. Another refresher fails `credential_busy`
before HTTP. There is no timed takeover of a still-running operation.

The result must match the captured identity and allowed scopes, then win an
exact operation-object/current-reference comparison under the mutex. A new
explicit authorization supersedes an older operation. Disconnect or close
invalidates it. Even a failed reconnect that leaves the previous reference
unchanged invalidates the old response: equality of token strings or snapshots
cannot defeat the operation identity fence. A stale loser never overwrites or
marks the newer connection unusable.

An admitted provider lease excludes successful refresh preparation, disconnect
and close; these return busy without claiming a change. An in-flight refresh or
authorization excludes new provider leases. Metadata remains accessible and no
metadata mutex spans remote HTTP or provider I/O. A provider waiting before its
lease cannot silently adopt a newly published generation. Hosts must explicitly
rebind the same tool/provider/scopes to the returned access reference; reconnect
with a new connection requires the foundation's new concrete tool binding.

Expiry is checked after remote verification, again before commit, and after
local persistence before volatile publication. Conservative deadlines count
remote time from before the token request and bound access by both UTC and
monotonic time. Wall-clock rollback cannot extend a live access generation.
If a durable commit consumes the remaining access lifetime, the refresh record
survives but no expired access secret is published.

PID checks precede inherited mutexes, clocks, OS handles and exceptional lock
cleanup. Provider leases retain the existing creating-thread restriction. This
is a synchronous, process-owned backend, not a multi-process refresh service.

## Local disconnect and failure truth

`disconnect(reference)` requires the exact current reference (or `None` before
the first connection). It commits a revoked local tombstone with no refresh
material, discards owned access material, and fences in-flight responses. Old
runtime bindings cannot resolve. Saved sessions, approvals or journal records
cannot restore the binding or host opt-in. Repeated disconnect of that same
reference is safe. A stale reference cannot disconnect a newer generation.

There is **no remote token-revocation call**. “Locally disconnected” does not
mean Google authorization was revoked. A provider request already admitted must
finish before successful disconnect; a busy result means disconnect has not
occurred. A refresh already on the network may finish after disconnect, but its
result cannot restore local authority. Its temporary in-memory copies are not
a new permission and are discarded when the owning operation returns.

The bounded public states are pending authorization, current, refresh required,
reconnect required and locally disconnected. Store corruption/unavailability are
categorical errors, not successful lifecycle states. `can_refresh` distinguishes
valid current access from a refresh path that now needs consent. Identity/scope
failure, invalid grant or network ambiguity can disable unattended refresh while
preserving an unexpired access generation. A timeout does not prove revocation.

Neither callback acceptance nor token HTTP success means connection success.
Verification and confirmed durable commit must precede the returned generation.
Provider bodies, code-bearing query strings and exception text/chains/notes are
not logged or returned; categorical failures cross the host/runtime boundary.

## Disclosure guarantees and limits

Live vault/service/config/protector/token/operation objects use the existing
`RuntimeOnly` boundary: redacted repr/str and no pickle, shallow/deep copying or
generic dataclass traversal. Public status contains no token material. Ordinary
provider output still passes the foundation's detached JSON and access-token
redaction checks. Automated tests also inspect model requests, persisted
sessions/state, nonempty approvals/journal records, configuration and logs.

This is not an encrypted heap, sandbox for malicious Python, or arbitrary DLP.
The trusted host, verifier, protector, browser opener and provider transport
must not independently log/copy raw material. Python strings/byte buffers,
debuggers/frame locals, memory dumps and system-browser history are outside the
accidental-serialization boundary. Same-user malicious code and administrators
can defeat DPAPI/ACL assumptions. No secure deletion, memory zeroization, backup
erasure or anti-rollback guarantee against restoring old valid ciphertext is
claimed. The ownership/file checks protect cooperating writers and detect
unexpected replacement during an open instance, not hostile OS control.

## Dependencies, verification and next integration seam

Two direct dependencies are added: `google-auth[cryptography]` for supported
Google ID-token verification, and conditional Windows `pywin32>=311` for DPAPI
and native filesystem/security APIs. No cloud authentication discovery methods
are used. Existing CI installs these through the normal project dependencies;
neither workflows nor `AGENTS.md` change.

Tests inject browser/callback/token/identity/refresh doubles with synthetic
values. Separate tests execute the actual Google signature verifier against
generated test RSA keys, real Windows DPAPI against random test-only material,
actual loopback sockets, process crashes, process exclusion and Event-controlled
races. POSIX fork/fsync coverage must be confirmed by Linux CI, not inferred
from Windows skips. No automated test uses a Google account or real token.

The future Gmail read-only stage can request deliberately expanded consent in
a separately reviewed design, select a verified connection in trusted host
composition, obtain its current access generation, and bind a pure-read provider
through `CredentialRuntime`. Gmail scopes are intentionally rejected here.
`NET_READ + CREDENTIAL_ACCESS + allow_authenticated_reads=True` remain required;
`EMAIL_SEND`, `PAYMENT` and `SYSTEM_ADMIN` remain blocked. No Gmail work begins
until this stage's PR is independently reviewed and merged.

The P2 policy-view repair makes `Agent.policy` a compatibility property over
`ToolExecutor.policy`. Conversation prechecks resolve that same current policy
per call; a constructor `policy=` argument must match the executor and is not
retained. Whole-policy replacement immediately affects existing conversation
and autonomous runners, subject to the unchanged active-dispatch busy contract.

## Optional future manual smoke test

Automated correctness does not depend on this procedure. On a trusted Windows
desktop, create an installed **Desktop app** OAuth client in Google's console
and configure its consent screen for the identity scopes. Supply its real client
identity only through trusted host configuration, outside the repository and
model-visible state. Do not enable a Gmail integration for this test.

```python
from pathlib import Path
from agentcore.credentials import (
    GoogleConnectionService, GoogleOAuthConfig, ProtectedCredentialVault,
)

# All three inputs are provided by the trusted host, never the model or a session.
# host_client_secret is None or a SecretMaterial from the host's secure config.
config = GoogleOAuthConfig(host_client_id, client_secret=host_client_secret)
with ProtectedCredentialVault(Path(host_private_credential_directory)) as vault:
    google = GoogleConnectionService(vault, config)
    credential = google.connect()  # system browser; never print tokens
    assert google.restore().state.value == "current"

# After process restart, construct fresh services and authority, using the same
# trusted directory/client configuration; no access token was saved.
with ProtectedCredentialVault(Path(host_private_credential_directory)) as vault:
    google = GoogleConnectionService(vault, config)
    status = google.restore()
    credential = google.ensure_current(status.credential.reference)
    status = google.disconnect(credential.reference)
    assert status.state.value == "locally_disconnected"
```

Inspect only categorical host status. Check the Google consent screen requests
identity access only. Test a second process is refused while the first owns the
domain. Confirm restart preserves the account/connection and local disconnect
blocks subsequent resolution. Remote consent can still exist at Google; do not
describe this local test as remote revocation.
