# Gmail read-only integration

> This document specifies the lower-level reviewed Gmail read primitive. The
> ordinary model-facing production composition, narrower semantic contract,
> and additional model-context bounds are specified in
> [Gmail agent read integration](gmail-agent-read-integration.md). A trusted
> host must use that composition for an ordinary Agent; the raw `q` and
> `page_token` surface described here is not registered by it.

Implementation baseline: `660128455fd9c569dc1bbce31777429a1bf13d09` (PR #23).
Official Google documentation checked on 2026-08-30.

This slice adds two model-usable authenticated **pure reads**: search for message
references, and read one selected message. It extends the
[credential foundation](credential-identity-foundation.md) and
[protected Google connection](google-oauth-protected-credentials.md). It does not
introduce an account picker, credential discovery, a generic Google API tool,
Gmail mutations, or a new permission authority.

## Separate trusted consent profiles

`GoogleConnectionService.connect()` remains an explicit trusted-host operation.
Its keyword-only `profile` accepts the `GoogleAccessProfile` enum, not strings or
lists from model arguments. The reviewed profiles are:

| Host profile | Exact requested scope set |
| --- | --- |
| `IDENTITY` (default, existing behavior) | `openid`, `email`, `profile` |
| `GMAIL_READONLY` | `openid`, `email`, `profile`, `https://www.googleapis.com/auth/gmail.readonly` |

Google's [installed-app OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app)
states that incremental authorization is not supported for installed apps.
Gmail authorization therefore starts a **fresh full consent/reconnection** with
`prompt=consent`, offline access, new state/nonce/PKCE, and the existing one-shot
loopback callback. It does not add scopes to an existing identity-only refresh
request and does not use `include_granted_scopes`.

Both first-time Gmail connection and identity-to-Gmail upgrade require explicit
granted scopes, a refresh token, and the existing verified Google ID token. After
the existing `userinfo.email`/`userinfo.profile` alias normalization, Gmail grants
must equal all four reviewed scopes. Missing identity or Gmail scopes, unknown
scopes, and broader scopes such as `gmail.modify`, `gmail.send`, `gmail.compose`,
`gmail.labels`, or `mail.google.com` fail closed. Identity-only authorization and
its existing bounded scope-shrink behavior remain supported; those credentials
cannot bind these Gmail tools. No Google scope is a model tool parameter.

The exact signed `(google, https://accounts.google.com, sub)` must match an
existing protected domain. Selecting a different account during consent fails
before any protected commit or new access publication. Email addresses, browser
account hints, callback fields, and unsigned token claims never replace `sub`.
Successful reconnect preserves the stable account ID and creates a new
connection ID/reference, using the existing vault lifecycle without schema changes.

Authorization failure or a pre-replacement storage failure preserves the prior
local connection and protected refresh record. Pending authorization temporarily
excludes provider leases; it does not claim successful Gmail authority. A failure
after atomic replacement poisons the open vault and requires reopen, just as in
PR #23: do not overwrite the potentially newer durable generation with an old
snapshot. Reopen restores complete protected metadata but no persisted access
token. A lost/rotated remote token cannot be made transactional with local disk;
retaining prior local state does not prove its refresh token remains valid at Google.

## Host composition and current authority

```text
trusted host fresh consent / ensure_current / refresh
  -> protected vault current access generation
  -> explicit GmailReadonlyTools.bind(CredentialRuntime, reference)

ToolRegistry -> argument validation -> current PermissionPolicy -> ToolExecutor
  -> AuthenticatedReadTool -> CredentialRuntime execution permit + access lease
  -> bound Google Gmail provider -> fixed Gmail HTTPS GET
```

`GmailReadonlyTools` constructs exactly `gmail_search_messages` and
`gmail_read_message`. The host registers those concrete objects or supplies them
through `AgentDependencies.tools`, along with `credential_runtime` and
`allow_authenticated_reads=True`. They are not auto-discovered or added to the
default registry. The existing Agent conversation and autonomous runner share
the same executor; Gmail adds no alternate execution path.

Message disclosure is an additional explicit **host output choice**:
`GmailReadonlyTools(allow_message_content=True)`. It defaults to false. Without
that choice, search can still return references, but `gmail_read_message` returns
`authenticated_read_disclosure_required` before any Gmail GET or body/header/
snippet processing. This opt-in determines whether private mail may enter the
configured model and session path; it cannot grant executor capabilities. Only
a literal host boolean is accepted. No tool argument, OAuth response, saved
session, or historical approval can enable it. Changing this choice requires
explicit new tool/provider composition and binding, not an in-flight toggle.

Both tools require `NET_READ` **and** `CREDENTIAL_ACCESS`, plus explicit host
authenticated-read opt-in. Current policy can deny either capability. Public
`NET_READ` is unchanged. `EMAIL_SEND`, `PAYMENT`, and `SYSTEM_ADMIN` remain blocked.
Direct `Tool.run`, fabricated leases, saved bindings, and approval/journal/session
metadata cannot substitute for a current executor permit. The provider also
checks the live lease belongs to Google and has exactly the Gmail read-only
profile, rejecting an incorrectly host-bound broader token.

The provider never selects an account/reference, refreshes a token, opens a
browser, calls another tool, requests scopes, or grants permission. The host must
obtain the current generation before binding. A successful refresh makes old
bindings stale; reuse the same `GmailReadonlyTools` and explicitly call
`bind(..., replace_existing=True)` with the new reference. Binding is the existing
per-tool atomic operation, not an atomic two-tool transaction: a partial/busy
rebind needs explicit host completion, and no stale binding silently adopts a
new generation. Successful reconnect creates a different connection and requires
new concrete Gmail tool/provider objects and explicit registry replacement.

Refresh cannot expand the protected scope ceiling. Gmail refresh requires the
exact full profile; an omitted refresh `scope` means unchanged under the existing
OAuth response contract. Explicit missing/broader grants are rejected before
publication. A failed refresh may leave still-valid old access usable while
marking future refresh as requiring consent; it does not turn an identity-only
generation into Gmail authority. Existing operation-object fencing prevents late
refresh/consent responses from undoing reconnect or disconnect. No metadata mutex
is held across Gmail/OAuth network I/O. Active read leases make refresh,
reconnect, disconnect, and conflicting policy changes return busy as applicable.

Local disconnect must succeed before the host claims disconnection. It removes
local refresh/access authority, persists the existing tombstone, and blocks old
Gmail bindings. It makes no remote revocation request or claim. Vault, runtime,
lease, Gmail provider, and transport ownership checks reject inherited authority
in forked children before touching inherited locks or sockets.

## Search contract and quota limits

`gmail_search_messages` accepts only:

| Argument | Bound |
| --- | --- |
| `q` | Optional Gmail query, at most 1,024 characters; C0/C1 controls and surrogate code points rejected |
| `max_results` | Integer 1 through 50; default 10; out-of-range values rejected |
| `page_token` | Optional opaque token, 1 through 2,048 printable ASCII characters without whitespace |

Limits are declared on the tool and enforced before permission/dispatch, then
checked again at the provider/transport boundary. Unknown parameters, including
account, email, `userId`, URLs, scopes, or access profiles, are rejected. Queries
and page tokens are encoded as complete query values; neither is interpreted as
authority, interpolated into paths, or copied into logs/errors/results. Provider
message/thread IDs are 1 through 128 ASCII letters, digits, underscore or hyphen.

The detached JSON result contains `messages: [{id, thread_id}]`, with optional
`next_page_token` and `result_size_estimate` (unsigned 32-bit integer). No snippet,
headers, bodies, or raw provider fields are added to list results. Google
[documents listing as references](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list);
details require a separate get.

Each search performs exactly one GET and returns at most the requested count.
It never fetches another page, reads a result automatically, reads a thread, or
downloads an attachment. Pagination needs another explicit bounded invocation.
Each message read similarly performs one GET. There is no automatic retry,
background polling, or quota-expanding N+1 behavior. See Google's current
[quota model](https://developers.google.com/workspace/gmail/api/reference/quota).

## Single-message contract

`gmail_read_message` accepts only `message_id`, using the same 1-to-128-character
safe identifier alphabet. It requests fixed `format=full`, Gmail's parsed MIME
representation, never `raw`. It verifies the returned ID equals the requested
one. There is no format override, arbitrary query parameter, URL, account, or
header selector. See the official
[messages.get contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get).

The allowlisted, detached result is:

```text
id, thread_id, label_ids, internal_date, snippet,
headers: {from?, to?, cc?, reply_to?, subject?, date?, message_id?},
body, body_source, omitted_body_parts,
attachments: [{filename, mime_type, size}], attachments_omitted,
truncated, truncation
```

`internal_date` is the provider's validated nonnegative millisecond string or
null. `body_source` is `plain`, `html_to_text`, `mixed`, or `none`. Duplicate
selected headers are combined under their allowlisted key. History IDs,
attachment IDs, unexpected headers, raw MIME/HTML/base64, and provider error
details are not exported. The selected labels are inert IDs; reading never marks
a message read or changes labels.

## Bounded MIME and inert HTML

| Resource | Implemented limit and behavior |
| --- | --- |
| HTTPS JSON response | 1,048,576 bytes; reject oversized responses before parsing |
| JSON structure | Lexical depth 32, 16,384 nodes including keys; duplicate keys, invalid UTF-8/surrogates, floats and nonfinite values rejected |
| Inspected MIME tree | Depth 8 with root at depth 0; 100 parts including containers and attachment metadata nodes |
| Input headers | 200 across inspected parts; name 128 characters; value 4,096 characters |
| Decoded text | 65,536 bytes per part; 131,072 across all decoded text parts, including alternatives not selected |
| HTML parser | 8,192 events; 64 nested suppressed elements; no renderer or resource loader |
| Public headers / snippet | 512 characters per allowlisted header / 256 characters |
| Public body | At most 8,000 characters |
| Public text budget | 12,000 content characters and 40,000 ASCII-JSON-escaped content characters, whichever is reached first |
| Final message JSON | At most 48,000 serialized ASCII bytes, below the runtime's 65,536-byte envelope |
| Labels | At most 100 input IDs, 16 output IDs, each at most 128 characters |
| Attachments | At most 20 metadata entries; output filename 128 characters; omitted count explicit |

Invalid input, unsafe structure, unsupported charset, malformed/noncanonical
base64url, or exceeded parsing/decoded-byte bounds fail closed with no partial
message. Valid content that exceeds **output** bounds is truncated explicitly:
`truncated=true` and a bounded `truncation` list identify body/header/snippet/label/
attachment/output-budget limits. Emoji JSON expansion counts toward the budget.
The conversation's own smaller `max_tool_result_chars` can truncate further using
its existing untrusted-result framing.

`multipart/alternative` prefers `text/plain`, even when that plain alternative is
empty. Other multipart messages combine bounded nonattachment text. HTML-only
content goes through the standard-library non-rendering `HTMLParser`: attributes
and comments are ignored; head/script/style/template/iframe/object/embed/svg/math/
noscript contents are suppressed. Only text and line breaks are returned. There
is no JavaScript/CSS evaluation, browser opening, link resolution, image loading,
remote resource request, or general content fetch. Controls and format characters
are removed from public text. This is inert text extraction, not browser-style
sanitization or proof that email text is trustworthy.

Text and RFC 2047 header decoding accept only UTF-8, ASCII, ISO-8859-1, and
Windows-1252 (with a small explicit alias map). Other encodings fail safely.
RFC 2231 encoded **charset** parameters are unsupported and rejected without
invoking a provider-selected codec. Filename parameters can mark attachments
without being decoded into message bodies or used as filesystem paths.

Binary parts, named text attachments, explicit attachment dispositions, and
attached messages are metadata only. Their base64 is not decoded or exposed.
Once a part is withheld, its subtree is not inspected: complex forwarded-message
attachments cannot exhaust the visible body's MIME budget. The HTTP byte limit
and JSON depth/node limits still bound the **entire** response, including those
opaque attachment subtrees. The MIME limits above apply only to inspected nodes.
Google's `full` representation can include inline attachment data in the bounded
JSON wire response; this implementation discards it, rather than claiming it can
prevent Google from including it. No attachment-specific request is made. A body
part stored only behind an attachment ID is omitted with `omitted_body_parts`
instead of downloading it. No attachment ID becomes a callable model reference.

## Transport and categorical failures

`GmailReadHttp` has only `list_messages` and `read_message`. Its private helper
accepts structured search or a validated message ID, not a method/URL/host/path.
All requests are HTTPS GET to `gmail.googleapis.com`:

- `/gmail/v1/users/me/messages`, with fixed list-field projection;
- `/gmail/v1/users/me/messages/{message_id}`, with `format=full` and fixed field
  projection `id,threadId,labelIds,internalDate,snippet,payload`.

The Gmail transport contains no POST, PUT, PATCH, DELETE, generic request entry
point, or other Gmail endpoint. OAuth's separate POST token exchange remains a
trusted-host credential operation, never a Gmail provider operation. The access
token appears only in `Authorization: Bearer ...`; it is never in a URL or query.
An accidental exact token echo in a search/identifier is rejected before HTTP.

The transport reuses PR #23's verified-client TLS context with default trusted
CAs, certificate and hostname verification, modern verification flags, and no
`SSLKEYLOGFILE` hook. It neither changes process environment nor discovers
environment proxies. `http.client` does not follow redirects; debug output is
explicitly disabled on every connection. There are no retries. The 10-second
timeout is per socket I/O, not a hard total response deadline. Response type must
be JSON with optional UTF-8 charset, content encoding must be identity, and an
announced Content-Length must be valid, bounded, and consistent with bytes read.

Read HTTP framing now uses a narrow local response class with the same budgets
and unsigned chunk grammar as the reviewed PR #26 mutation transport. Keeping
it local avoids changing send/reconciliation parsing or semantics. Status lines,
interim headers, chunk-size lines and trailers share a 16,384-byte framing budget,
4,096-byte line limit and 66-line limit; chunk CRLF delimiters also consume the
byte budget. A rejected line may consume one extra byte to detect overflow.
Malformed fields, obsolete folding, bare LF, missing terminators and framing or
content-type/encoding overrides in trailers are rejected. Response debug output
is disabled, and both response streams and connections are closed.

Content-Length is a strict nonnegative decimal of at most 20 digits, no larger
than the existing 1,048,576-byte JSON maximum, and must match the bytes returned.
Duplicate Content-Length, Transfer-Encoding, Content-Type and Content-Encoding
fields are rejected. Only a single `chunked` Transfer-Encoding (case-insensitive)
is accepted, and only without Content-Length. Chunk sizes require 1 through 8
unsigned hexadecimal digits and CRLF, with no extensions, signs or whitespace.
Each declared chunk must fit the remaining decoded-body budget before any body
read; the chunk delimiter must be exactly CRLF. Trailer bytes and chunk overhead
cannot reset the framing budget. Length-delimited, bounded chunked and
connection-close-delimited JSON retain their existing public read results.

The previous **negative-chunk unbounded-read P2 is closed**: `-1\r\n` is rejected
before stdlib integer parsing can lead to `read(-1)`. An additional stream wrapper
rejects omitted, negative or oversized body-read requests; every underlying
`read` is at most 1,048,577 bytes and every `readline` at most 4,097 bytes, including
overflow detection. Deterministic memory-socket tests observe these calls and
verify that malformed provider bytes never reach exceptions, logs, ToolResult,
model requests, sessions or ordinary state. Error and redirect bodies remain
unread; no real account or Gmail network test is involved.

| Failure | Public category |
| --- | --- |
| Invalid tool arguments | Existing `validation` before dispatch; defensive provider checks use `authenticated_read_invalid_arguments` |
| Missing Gmail grant | `credential_scope_mismatch`; no provider request |
| Stale / expired / disconnected | Existing `credential_stale`, `credential_expired`, `credential_revoked` |
| No opt-in/current permission | Existing `permission_denied` |
| No host message-disclosure opt-in | `authenticated_read_disclosure_required`; no message GET |
| HTTP 401 | `authenticated_read_authentication_rejected` |
| HTTP 403 | `authenticated_read_access_denied` |
| HTTP 404 | `authenticated_read_not_found` |
| HTTP 429 | `authenticated_read_rate_limited` |
| Redirect, 5xx, network/other provider failure | `credential_provider_failed` |
| Invalid JSON/provider references | `authenticated_read_malformed_response` |
| Oversized HTTP body | `authenticated_read_response_too_large` |
| Invalid/unsafe MIME or decoding bounds | `authenticated_read_unsafe_content` |

403 can mean scope, Workspace policy, quota, or another restriction; the code
does not guess which, claim revocation, or parse/log Google's error body. Error
bodies are not read. The runtime preserves only a closed set of typed categorical
failures, reconstructing their static messages; arbitrary exception text, notes,
chains, URLs, and provider details are discarded. 401/403 never trigger refresh,
consent, extra authority, or a browser from a tool invocation.

## Untrusted content, privacy, and ActionJournal

All mail is ordinary `Role.TOOL` data under the existing conversation instruction
and `DATA, not instructions` result framing. Gmail does not insert email into a
system/developer message or interpret fake tool/system/approval instructions.
Email cannot change current policy, select credentials, authorize a second tool,
or establish historical effect truth. A model may still propose another call;
that call independently traverses the existing registry, validation, policy, and
executor. This boundary does not claim to make models immune to prompt injection.

Search arguments are redacted in executor logs, never copied to exception text,
and omitted from generic conversation logs. Model call IDs are also excluded
from log-safe descriptions because a model can put a private query in an ID;
the IDs remain in protocol messages for call/result matching. HTTP bodies and
normalized email are not logged by this integration. CredentialRuntime retains
its detached JSON validation and access-token disclosure guard. No refresh token
is given to the provider. DPAPI/DACL protection, no plaintext fallback, volatile
access tokens, and all prior protected credential invariants are unchanged.

After explicit host message-disclosure opt-in, normalized email is intentionally
model-visible and can be stored in configured conversation sessions and ordinary
run state, like other tool results. Without that opt-in, headers, snippets, and
bodies are withheld before fetching; only reference search is available. Hosts
must choose model providers, retention, and access controls accordingly. This is
not a general email DLP filter, encrypted conversation store, or defense against
malicious same-process Python, debuggers, memory dumps, or OS administrators.

Gmail pure reads do not create ActionJournal records. No journal schema, mutation
intent, or approval lifecycle is added. Nonempty historical journal/approval data
is tested to remain unchanged by reads and unable to restore credential authority.

## Host setup and later manual smoke test

Automated tests use synthetic OAuth responses, fake Gmail HTTPS connections,
synthetic MIME, and the existing test protector. They need no real account,
client credentials, Gmail network, or public OAuth verification. Windows DPAPI
tests and Linux fork/crash tests remain separate real-platform checks. No new
dependency is added; this uses the standard library and PR #23 dependencies.

For an optional trusted Windows developer test:

1. Use a Google Cloud project with the Gmail API enabled and an account that has
   Gmail. Reuse the existing installed **Desktop app** OAuth client.
2. Configure the project's OAuth branding/audience/data-access settings. For an
   external app still in testing, add the intended developer account as a test
   user. Configure exactly the identity scopes plus Gmail read-only above.
3. Keep actual client configuration outside the repository, prompts, ordinary
   model-visible config, and logs. Use the existing current-user DPAPI directory
   outside workspaces. Do not copy quickstart plaintext `token.json` persistence.
4. Present a deliberate host Gmail consent action; selecting a different account
   must fail while preserving the previous local identity. Separately opt into
   message-content disclosure only for the intended model/session destination.
   No Cloud Console changes are automated by this integration.

See Google's [setup guide](https://developers.google.com/workspace/gmail/api/quickstart/python)
and [OAuth scope classification](https://developers.google.com/workspace/gmail/api/auth/scopes).
Gmail read-only is a restricted scope. Public apps using sensitive/restricted
Google user-data scopes can require Google's verification process; restricted
data storage/transmission can also require a security assessment. Review the
[restricted-scope requirements](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
and user-data policies before deploying, especially before sending email to a
remote model provider. This PR does not assert production verification/compliance.

An example host composition (all `host_*` inputs are trusted, never model args):

```python
from pathlib import Path
from agentcore.credentials import (
    CredentialRuntime, GoogleAccessProfile, GoogleConnectionService,
    GoogleOAuthConfig, ProtectedCredentialVault,
)
from agentcore.integrations.gmail import GmailReadonlyTools
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry

config = GoogleOAuthConfig(host_client_id, client_secret=host_client_secret)
with ProtectedCredentialVault(Path(host_private_credential_directory)) as vault:
    google = GoogleConnectionService(vault, config)
    current = google.connect(profile=GoogleAccessProfile.GMAIL_READONLY)
    runtime = CredentialRuntime(vault)
    # Explicitly allow bounded private mail in the configured model/session path.
    gmail = GmailReadonlyTools(allow_message_content=True)
    gmail.bind(runtime, current.reference)
    registry = ToolRegistry()
    registry.register_all(list(gmail.tools))
    executor = ToolExecutor(
        registry, PermissionPolicy(allow_authenticated_reads=True),
        credential_runtime=runtime,
    )
    references = executor.invoke("gmail_search_messages", {"max_results": 5})
    # Only after explicit selection of a returned id:
    selected = executor.invoke("gmail_read_message", {"message_id": host_selected_id})
    # Never log entire results. Refresh, when needed, is another host action:
    current = google.refresh(current.reference)
    gmail.bind(runtime, current.reference, replace_existing=True)
    google.disconnect(current.reference)
```

For model use, compose the same tools/runtime into `AgentDependencies` with
`allow_authenticated_reads=True` and `ConversationConfig(tools_enabled=True)`;
let Agent create the single executor instead of creating a second one. Test that
search returns only references, default message disclosure is refused, explicit
host disclosure plus explicit read returns inert bounded text, refresh requires
rebinding, and successful local disconnect blocks further Gmail reads.
No real Gmail smoke test is claimed by the automated suite.

## Deliberately unsupported and next boundary

No send, reply, forward, draft, delete, trash/untrash, star, read/unread change,
label mutation, settings operation, thread mutation/read expansion, attachment
download, watch/stop, history polling, remote OAuth revocation, Contacts, or
Calendar is implemented. No Gmail mutation tool is registered or executable.

A future Gmail mutation design must separately review exact account-bound intent,
current permission, durable approval, ActionJournal effect truth, and ambiguous
outcome reconciliation for drafting/sending. Read-only consent or historical read
results cannot authorize it. That architecture and its scopes are outside this PR.
