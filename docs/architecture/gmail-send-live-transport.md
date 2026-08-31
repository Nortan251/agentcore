# Gmail production direct-send and reconciliation transport

Baseline: `e4168ff9c240091807d51e60117bfd28527deb01` (merged PR #25).
Official contracts checked on 2026-08-31. The
[send foundation](gmail-mutation-send-foundation.md) and
[Stage 5 ActionJournal contract](task-43.1-action-journal.md) remain authoritative.
ActionJournal schema v1, protected intent/MIME v1, approval, deduplication and
deliberate risky-repeat contracts are unchanged.

`GmailMutationHttp` is an explicit trusted-host transport beneath
`GmailSendFoundation`. It supplies only primary-sender verification, one exact
prepared send, and one bounded private Sent probe. It does not expose a generic
HTTP operation, credential lifecycle, model search, or registration tool.
`GmailReadHttp`, the read-only tools and `default_tools()` are unchanged.
There are no new production dependencies; this uses `http.client`, `ssl`, `json`,
`base64`, the standard-library email parsers, and existing hardened TLS/JSON code.

## Explicit host composition

Omitting `transport` still installs `UnavailableGmailMutationTransport`. Live
composition accepts only the exact production class; a test transport still
requires explicit `test_only=True`. Scopes, saved sessions, approvals and journal
records cannot install transport or enable permission. No application or UI is
automatically wired by this change.

The following illustrates composition only. It is not a command to run a send.
All variables represent already-provisioned, trusted, current host services:

```python
from agentcore.integrations.gmail.mutation_http import GmailMutationHttp
from agentcore.integrations.gmail.send import GmailSendFoundation
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.executor import ToolExecutor

foundation = GmailSendFoundation(
    protected_payload_store,
    sender=host_configured_primary_address,
    transport=GmailMutationHttp(),
)
foundation.bind(credential_runtime, current_send_connection_reference)
registry.register(foundation.tool)
action_adapters.register(
    foundation.tool, foundation.adapter, reconciler=foundation.reconciler,
)
policy = PermissionPolicy(
    host_permission_config,  # at least CONFIRM; explicit blocks still apply
    allow_email_send=True,
    allow_authenticated_reconciliation=True,  # separate read-only recovery opt-in
)
executor = ToolExecutor(
    registry, policy,
    action_store=action_journal,
    action_adapters=action_adapters,
    protected_payload_store=protected_payload_store,
    evidence_store=protected_evidence_store,
    approval_authority=durable_human_approval_authority,
    credential_runtime=credential_runtime,
)
```

The host must use the existing prepare/review/approval/invoke flow: persist the
preparation handle, show `foundation.review(handle)` only in a private human UI,
and invoke through `ToolExecutor` with that handle's action context. Continuation
retains the exact preparation, action and approval request; recovery uses
`ActionRecoveryCoordinator`, never direct transport or `Tool.run`. Production
host UX/consent setup is a separate task. Protected action/evidence files are
authenticated **plaintext**, not encrypted; private host storage and ACLs remain
required. Refresh storage is separately protected by the reviewed Google vault.

The connection must have exactly `GoogleAccessProfile.GMAIL_SEND`:

* `openid`, `email`, `profile`
* `https://www.googleapis.com/auth/gmail.readonly`
* `https://www.googleapis.com/auth/gmail.send`

There is no `gmail.modify`, `gmail.compose` or `mail.google.com` grant. Signed
Google `(issuer, sub)` remains identity authority. The protected intent binds
Google, stable account ID, exact connection ID, exact profile and sender. A
trusted refresh/rebind may advance access generation on the same connection.
Reconnect, even with the same `sub`, cannot execute or probe an old connection's
intent. Generic credential access, EMAIL_SEND, PAYMENT and SYSTEM_ADMIN remain
blocked outside their existing reviewed compositions.

## Primary-sender verification and the effect boundary

The smallest extension is a separately declared read-only preflight in
`CredentialActionExecution`. After acquiring the current credential and policy
leases, but **before** `provider_entered`, it calls:

```text
GET https://gmail.googleapis.com/gmail/v1/users/me/profile?fields=emailAddress
```

Google's [profile reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile)
documents `emailAddress` and accepts `gmail.readonly`. Only a JSON object with
that one field is accepted. Counts, `historyId`, other metadata, aliases and
email claims never become account authority. Address normalization is exactly
the frozen bare-mailbox rule: strip outer ASCII spaces and fold domain case,
preserving local-part spelling. No dot/plus alias rewriting or send-as support.

The sender is already fixed in the authenticated protected intent and exact
MIME at preparation. Preflight must prove that **same value** equals the current
primary Gmail address; it cannot rewrite or regenerate any prepared field. A
matching read issues an in-memory, one-use object bound to the transport,
process/thread, exact access lease, action/attempt/digest, MIME and fingerprint.
It contains the already-encoded exact send body. It is neither persisted nor
serialized; no cached mailbox-address grant or new durable metadata is added.
Thus the approved durable sender is the value verified at the live attempt,
without treating an email string as a substitute for signed identity.

The additional read needs current `NET_READ` permission. Send still requires
EMAIL_SEND + NET_WRITE + CREDENTIAL_ACCESS, explicit send opt-in and durable
approval. A blocked NET_READ prevents even the profile GET; reconciliation's
separate opt-in is not needed for this read precondition. Current claim,
registration, policy, approval expiry, credential expiry and cancellation are
checked again after profile I/O. The same credential/policy leases span GET and
POST, while metadata mutexes are released during I/O. Conflicting policy,
binding, refresh/disconnect and exact tool replacement return busy.

```text
exact registration -> validation -> current policy -> durable approval
  -> exact protected intent -> AUTHORIZED -> fresh durable IN_FLIGHT attempt
  -> current leases -> read-only profile verification -> revalidate authority
  -> mark send provider entry -> consume one-use verification -> one POST
  -> protected evidence -> durable result publication
```

A locally observed profile failure proves no POST: the attempt is NOT_INVOKED
and normally FAILED / CONFIRMED_NOT_APPLIED (host cancellation can be CANCELLED).
An interrupted process cannot publish that local observation: a crash during
IN_FLIGHT, including preflight, remains conservatively uncertain at startup.
There is no new persistent "profile passed" permission bit.

**Bcc live delivery disabled pending controlled Gmail validation.** Non-empty
Bcc is rejected by live preparation and tool/adapter admission before approval
and IN_FLIGHT, with further provider/HTTP defenses. Restored foundation Bcc
intents cannot bypass the block. The underlying Bcc normalization, durable
schema, MIME and repeat semantics remain intact for non-network foundation work.

## Exact POST and HTTP protection

Google's [send reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)
and [MIME sending guide](https://developers.google.com/workspace/gmail/api/guides/sending)
specify a Gmail Message resource with base64URL MIME and accept `gmail.send`.
The fixed production request is:

```text
POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send
Authorization: Bearer <runtime access lease>
Content-Type: application/json
Content-Length: <exact JSON byte length>

{"raw":"<canonical unpadded base64URL of exact protected MIME bytes>"}
```

There is no upload path, upload-type query, caller-supplied method/host/path/JSON
or other userId. No MIME, Date, Message-ID, recipient, subject or body is rebuilt
from Gmail state. Exactly one `HTTPSConnection.request` is issued for POST per
physical attempt. There is no retry loop, redirect following, auth refresh,
401/429/5xx resend, SMTP fallback, or generic endpoint adapter.

TLS uses the reviewed verified-client context, hostname verification, default
trusted CAs and strict X.509 flags. It explicitly disables key logging even with
`SSLKEYLOGFILE`, disables connection/response debug output, and never discovers
environment proxies or changes the process environment. The socket timeout is
10 seconds per I/O, not a preemptive total-operation deadline.

HTTP status/interim headers, chunk size lines and trailers share one framing
budget before parsing: 16 KiB total, 4096 bytes per line, at most 66 lines.
Header defects and duplicate security-relevant headers are rejected. JSON
content type permits only optional UTF-8 charset; non-identity Content-Encoding
is rejected. Content-Length must be a bounded nonnegative decimal and match
the bytes read. Chunked framing is accepted only without Content-Length, with
the same decoded-body cap. Chunk sizes must be unsigned hexadecimal, with no
extensions; negative sizes cannot trigger an unbounded underlying read.
Strict UTF-8, JSON object shape, duplicate keys,
non-finite/nonnumeric types, depth/node/string bounds and provider IDs are checked.

| Operation | Maximum response bytes |
| --- | ---: |
| Profile | 4,096 |
| Send | 16,384 |
| Sent search | 8,192 |
| Each candidate | 262,144 |

Only the access lease reaches this transport; refresh tokens do not. The access
token is placed solely in Authorization. Private payload/result checks withhold
literal and supported encoded credential echoes before further requests or
evidence. Exceptions, chains and notes are replaced by categorical failures;
KeyboardInterrupt/SystemExit are reconstructed without provider text. No raw
HTTP response or provider error is logged, returned or stored.

Success requires HTTP 200 and a bounded object containing valid `id` and
`threadId`, plus optional bounded `labelIds` including SENT. Other fields or
malformed response metadata are untrusted. Minimal protected evidence is bound
to the actual action, attempt, marker and MIME fingerprint before SUCCEEDED.
This proves accepted send truth under the reviewed provider contract, not
recipient delivery or reading.

Google's [error guidance](https://developers.google.com/workspace/gmail/api/guides/handle-errors)
describes generic retries/backoff, but the reviewed direct-send contracts do
not provide an application idempotency key or an explicit status-based
send-not-applied guarantee. Therefore this implementation treats **every**
non-200 response after send entry as unknown, including 401/429/5xx. It does not
read error bodies or act on Location/Retry-After. Timeout, reset, malformed or
secret-bearing success and provider cancellation also yield RECONCILE_REQUIRED.
Interruptions publish uncertain truth before propagating when storage permits;
a publication failure leaves recoverable IN_FLIGHT. Confirmed success is not
erased by later logical cancellation. No uncertain result authorizes resend.

## Private reconciliation

Only an executor/recovery claim can reach the registered reconciler, with current
NET_READ + CREDENTIAL_ACCESS and `allow_authenticated_reconciliation=True`.
Revoking send permission need not revoke this independent read permission.
Current account, exact connection, exact profile and live access lease are
rechecked; old claims, disconnected/stale bindings and historical approvals
grant nothing. Inherited process objects are rejected before locks or I/O.

The search is fixed:

```text
GET /gmail/v1/users/me/messages
    ?labelIds=SENT
    &q=rfc822msgid:<hex>@agentcore.invalid
    &maxResults=5
    &fields=messages(id,threadId),nextPageToken,resultSizeEstimate
```

Here `<hex>` denotes the generated hexadecimal characters, **not literal angle
brackets**. For protected header `<abc@agentcore.invalid>`, the query value is
`rfc822msgid:abc@agentcore.invalid`, URL encoded once. Google's
[search-operator help](https://support.google.com/mail/answer/7190?hl=en)
shows the bracket-free representation. The
[messages.list reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
also gives a bracketed example; we do not claim brackets are invalid. We choose
the documented bracket-free form and retain the exact brackets for header
comparison. No user/model query is accepted; the marker is correlation only.

At most five distinct validated ID/thread references are accepted. There is no
automatic pagination. Any `nextPageToken` key, including an empty string, or
an estimate larger than the returned set marks the result incomplete. Invalid
shapes, IDs, estimates, duplicates and overlarge responses fail the probe.

Each candidate uses a fixed
[messages.get](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)
with `userId=me`, `format=full`, and this projection:

```text
id,threadId,labelIds,payload(mimeType,filename,headers,
body(size,data,attachmentId),parts(partId))
```

The projection requests only root content and child part identifiers. No
attachment endpoint, nested part body, remote resource or image request exists.
HTML, attachments, multipart bodies, filenames, external attachment IDs and
unsupported content are inconclusive. Unexpected provider data is bounded and
discarded rather than exposed to the model.

A strong candidate must have the searched ID/thread and SENT label, complete
plain-text UTF-8 root data with canonical base64URL and matching size, unique
required headers, and the exact protected semantics:

* bracketed Message-ID; normalized primary From; ordered To and Cc roles; no Bcc;
* subject decoded without defects or replacement characters using agreeing
  standard-library interpretations; exact normalized UTF-8 plain-text body;
* exact UTC Date second (canonical GMT and equivalent +0000 only);
* MIME-Version 1.0, text/plain charset UTF-8, and base64 transfer encoding.

Reviewed CRLF folding, subject encoded words, domain case, body line endings and
GMT/+0000 representations are handled without changing semantics. Missing,
duplicate, mismatching or unprovably transformed fields cannot match. Unsupported
Sender/Reply-To/Resent/reply/content headers are also inconclusive. `internalDate`
or a fuzzy timestamp window never substitutes for the approved Date.

Exactly one candidate, one strong match and a complete search may establish
CONFIRMED_APPLIED and durable SUCCEEDED. Zero or incomplete search stays
OUTCOME_UNKNOWN / PROBE_LATER. Multiple or mismatching candidates require the
existing human disposition. None is arbitrarily selected; `retry_safe=False`
always. Even an exact matching candidate plus another mismatch stays unknown.

Only safe candidate IDs and match booleans plus bounded categorical reason,
marker/fingerprint and claim binding enter protected evidence. Private headers,
subject and body are comparison inputs only; raw Gmail responses are not stored.
Ordinary journal/approval/results/logs remain content-free. Local pre-provider
probe failures do not consume the existing quota; entered probes do.

## Verification and manual boundary

The production suites replace only `HTTPSConnection.connect` with in-memory
sockets, leaving real request serialization, HTTPResponse framing, TLS context
creation and production matching active. An autouse guard denies DNS and real
socket connect/send operations; crash subprocesses install the same guard.
All tokens, identities, MIME and mailboxes are synthetic. No automated test or
task step sends a real message or performs a Gmail internet request.

Tests cover exact outbound bytes, credentials only in Authorization, malformed
HTTP/JSON bounds, ambiguity/no retry, primary-only sender, Bcc admission, current
authority during paused profile/POST/probe I/O, cancellation and secret handling.
Production reconciliation tests cover fixed GETs, conservative matching,
pagination, privacy and process crashes before result/publication. Restart
refreshes only the same synthetic connection, retains exact protected MIME and
Message-ID, converts abandoned IN_FLIGHT to RECONCILE_REQUIRED, and never sends
automatically. The real fork test requires POSIX; Windows skips do not prove it.

The separate [controlled manual procedure](../testing/gmail-send-controlled-smoke-test.md)
must not be run until independent audit and merge plus explicit human approval
of that future task. No live sender, recipient delivery, Gmail transformation
or Bcc recipient-isolation behavior is claimed from these synthetic tests.
Drafts, replies, forwards, HTML, attachments, send-as aliases and live Bcc remain
unsupported. The next task is controlled real-account send/reconciliation
validation plus explicit host UX/composition wiring, not broader Gmail mutation.
