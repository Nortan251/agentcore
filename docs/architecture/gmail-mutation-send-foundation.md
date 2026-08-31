# Gmail mutation architecture and direct-send foundation

Baseline: `0d6665d2c444b674d7c24a5dd4b58d4d1ebde0f8` (PR #24).
Official contracts checked on 2026-08-31. Stage 5's
[ActionJournal contract](task-43.1-action-journal.md) remains authoritative.

This foundation has **no production Gmail mutation or reconciliation transport**.
The production transport is unavailable. Explicit test-only transports exercise
the real executor, credential leases, approvals, protected stores, journal and
recovery coordinator. Nothing registers a send tool by default. No real account,
consent, message, or Gmail network test is needed or claimed.

## Scope and consent

The host-only `GoogleAccessProfile.GMAIL_SEND` requests exactly:

* `openid`
* `email`
* `profile`
* `https://www.googleapis.com/auth/gmail.readonly`
* `https://www.googleapis.com/auth/gmail.send`

The future endpoint is
`POST https://gmail.googleapis.com/gmail/v1/users/me/messages/send`.
Google's [messages.send reference](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)
accepts `gmail.send`; its documented request has no application idempotency-key
parameter. This implementation declares no external idempotency contract.
Read-only scope is retained for account-bound reconciliation, not to grant send.

Installed applications use fresh complete consent, not incremental refresh-grant
expansion. The existing host `connect` flow uses new state/nonce/PKCE,
`prompt=consent`, and exact signed Google `(issuer, sub)` verification before
durable publication. Scope aliases are normalized; missing initial or unexpected
scopes fail closed. An omitted refresh scope retains the exact prior grant under
RFC 6749; an explicit response cannot expand or shrink the reviewed Gmail profile. A
read-only refresh cannot become a send grant. See Google's
[installed-app guidance](https://developers.google.com/identity/protocols/oauth2/native-app).
No model parameter, session, approval, or journal field selects scopes.

The existing model-visible Gmail read tools still accept **only** the exact
GMAIL_READONLY profile, including rejecting the broader send profile before GET.
This PR preserves that boundary. The send profile's read scope is usable here
only by the separate private reconciliation contract. Sharing a future send
connection with model-visible reads requires another reviewed composition change;
the current one-active-connection vault does not implicitly provide both profiles.

Drafts are a separate future authority surface. Google documents `gmail.compose`,
`gmail.modify`, or full mail access for
[draft creation](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create),
[update](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/update),
[send](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send), and
[delete](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/delete).
No compose profile or draft tool is implemented. Future creation, editing,
sending and deletion need distinct action kinds and effect evidence: a created
draft is not a sent message, and a draft ID grants no permission. An intervening
edit must invalidate any approval bound to earlier draft content.

## Tool and preparation contract

`gmail_send_message` is a journal-required, non-read-only tool. Model inputs are
`to: array[string]`, optional `cc` and `bcc` arrays, `subject: string`, and
plain-text `body: string`. All unknown keys are rejected. There are no HTML,
attachments, reply/forward/thread IDs, headers, From, Date, Message-ID, raw MIME,
account, provider, credential, scope, URL, or transport options.

Addresses are parsed locally with Python's standards-aware `Address` parser,
restricted further to bare ASCII mailboxes with DNS-style domains. Display
names, groups, comments, quoted local parts, domain literals and SMTPUTF8
addresses are outside v1. There is no DNS lookup or provider discovery.
Domain case and outer ASCII spaces are normalized; local-part spelling is
preserved. Duplicates across To/Cc/Bcc are rejected using conservative
case-insensitive comparison, including consumer Gmail dot/plus/domain aliases.
Google documents [dot equivalence](https://support.google.com/mail/answer/7436150?hl=en),
[plus variants](https://gmail.googleblog.com/2008/03/2-hidden-ways-to-get-more-from-your.html),
and [Googlemail continuity](https://gmail.googleblog.com/2010/05/google-mail-is-becoming-gmail-in-uk.html).
These are conservative rejection keys, never outgoing address rewrites or
account identity. Other providers' arbitrary aliases cannot be discovered locally.
Recipient roles and their order are retained. Bounds are 20 recipients total,
254 bytes per address, 4 KiB of recipient bytes, 1 KiB of UTF-8 subject,
64 KiB of normalized UTF-8 body, and 128 KiB of final MIME. Header controls,
CR/LF injection, surrogates and unsafe format controls are rejected.

The trusted host stages an immutable preparation before invoking the tool.
`GmailSendFoundation.prepare` normalizes the proposed inputs, captures current
account/connection metadata, generates a Message-ID and Date once, builds MIME,
and durably creates/reloads it in `ProtectedActionPayloadStore`. The returned
handle contains only opaque continuity/integrity data. It grants no permission.
The host may restore that handle after restart; it must never recreate the
preparation to resume an existing logical action. A missing or corrupt handle
fails closed. The adapter loads the exact protected object and compares all
normalized model arguments before returning `PreparedActionIntent`.

Normal invocation uses the preparation's trusted deduplication context. A
preparation cannot be reused as a second ordinary logical action. Pending
approval continuation retains that context and the approval request ID. Recovery
also retains the original preparation and actual journal action ID. Stage 5
copies and authenticates the complete protected intent under the actual action
ID; preparation storage is inert pre-approval data, not an AUTHORIZED journal.

## Account binding and credentials

Protected intent binds `provider=google`, the vault's stable `account_id`, and
the exact `connection_id`. In the protected Google vault those identifiers name
the verified signed identity, not an email lookup. A different vault/account or
connection cannot satisfy the binding. The sender mailbox is a separately
validated, trusted-host assertion and is included in the exact intent; it is
never account authority. The existing identity vault does not retain email
claims. Before enabling a real transport the host must verify that assertion
against the same authenticated Gmail account (for example a reviewed `me`
profile read); this PR does not pretend to supply that network verification.

An explicit refresh/rebind may advance the access generation on the **same**
account and connection, preserving intent. No access reference or generation is
persisted in the send intent. At execution the current runtime binding is
captured, the vault re-resolves that exact generation, and provider identity,
account, connection, scope, state and expiry are checked again at dispatch.
No implicit refresh, default account, discovery or fallback is allowed.
Disconnect and any reconnect, even same-account reconnect, invalidate the old
connection binding and require a new intent and approval. Probe failures retain
uncertainty; they do not erase old send truth.

`CredentialRuntime` remains the only bridge to its attached vault. An exact
`AuthenticatedActionTool` receives an executor-only, one-use, process/thread
bound permit after a verified journal claim. Its direct `run` fails without
that permit. The permit and access lease are runtime-only, not persisted,
pickled, logged or returned. Refresh material never reaches a provider.

## Permission and durable approval

Sending declares existing `EMAIL_SEND`, `NET_WRITE`, and `CREDENTIAL_ACCESS`.
Network mutation is not hidden behind a read capability. Ordinary requests for
email/credential authority remain blocked, as do PAYMENT and SYSTEM_ADMIN.
The narrow context requires explicit host `PermissionPolicy(allow_email_send=True)`
and at least CONFIRM, honors explicit blocks and stricter level overrides, and
**always requires durable approval**, including at AUTONOMOUS. It is unavailable
to generic tools, model flags, legacy boolean approval and authenticated reads.
The executor rejects a custom send-policy result that tries to return ALLOW
instead of the required durable-approval decision.
The send opt-in participates in the durable policy fingerprint.

`DurableApprovalAuthority` binds the installation-keyed protected intent digest,
action kind, tool/version, exact capabilities, action ID, host principal/scope,
policy fingerprint, expiry and one-shot lifecycle. A separate host review API
loads the protected recipients, subject, body, sender and generated headers for
the human UI. Generic approval descriptions and records remain categorical.
Changing any semantic field, generated marker or MIME version requires a new
preparation and approval. Historical/consumed receipts cannot authorize another
physical attempt. The existing narrowly defined consumed receipt continuation
for an authorized action with **no attempts** remains subject to current policy,
exact intent, current connection and approval expiry.

Exact registration is leased across credential lookup. Policy is checked before
lookup, then rechecked and leased through final identity/expiry checks, provider
I/O and result scrubbing. The exact durable attempt is reloaded after lookup.
Replacement before dispatch denies
stale checks, including ABA. During admitted dispatch, conflicting policy,
binding, refresh and disconnect operations report busy without claiming success.
Metadata mutexes never span provider or human I/O. PID fencing precedes locks;
forked children inherit no usable runtime, permit, preparation or provider.

## Protected intent and MIME

The versioned protected schema contains preparation identity; exact account and
sender binding; send-profile and MIME version; normalized To/Cc/Bcc, subject and
body; host Message-ID and Date; exact MIME bytes encoded as base64; and an inner
MIME digest. All fields are strictly bounded and validated. The public journal
digest is installation-keyed by `ProtectedActionPayloadStore`, not a plain hash
of an address. The journal has only opaque references, categorical target/kind,
digests, states, attempts and safe provenance. It contains no send content.
No token, lease, callable, OAuth secret or credential reference is in the intent.
Existing protected action storage provides authenticated plaintext, **not
encryption at rest**; confidentiality depends on the host's private storage and
ACLs. This is distinct from DPAPI-encrypted refresh storage.

Message-ID is `<32-random-byte-hex@agentcore.invalid>`: syntactically valid,
cryptographically unique, host generated and digest bound. It is a correlation
marker, not provider deduplication. Separate preparations have different markers.
An exact-action safe retry, approval wait, restart and reconciliation retain the
original bytes; they never generate another marker.

The MIME builder uses standard-library `EmailMessage`, SMTP CRLF policy, fixed
header ordering, UTF-8 plain text and explicit base64 transfer encoding. It
normalizes body line endings and the final newline before intent binding;
non-ASCII subjects use RFC encoded words. Only the reviewed headers are emitted.
The fixed Date comes from the host UTC clock, not the model. Exact built bytes
are persisted; inconsistent or unsupported MIME versions fail closed. See
[Python SMTP policy](https://docs.python.org/3/library/email.policy.html#email.policy.SMTP),
[address parsing](https://docs.python.org/3/library/email.headerregistry.html#email.headerregistry.Address), and
[Google's raw MIME contract](https://developers.google.com/workspace/gmail/api/guides/sending).

Bcc is deliberately retained in the private submission MIME: Gmail addresses
delivery from the To/Cc/Bcc headers, so stripping it before the API would lose
blind recipients. It is never copied into public evidence. RFC 5322 section 3.6.3
defines recipient-copy removal; Google's
[Bcc documentation](https://support.google.com/mail/answer/2819488?hl=en)
describes hiding blind recipients from other recipients. Python byte serialization
retains the submission Bcc header, which tests verify. Recipient-copy stripping
is the provider's responsibility, not something this builder claims to perform.
A controlled recipient-isolation check is a prerequisite of future live wiring;
no SMTP helper, recipient delivery or real Bcc test runs here.

## Attempts, effects and cancellation

The action kind is `gmail.send_message.v1`, eligibility `EXTERNAL_ACCOUNT_MUTATION`,
effect class `NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS`. No idempotency key is
created. The canonical order remains:

```text
ToolRegistry -> validation -> current PermissionPolicy -> durable approval
  -> exact protected intent -> AUTHORIZED -> durable fresh attempt / IN_FLIGHT
  -> final registration / permission / approval / current credential checks
  -> one-use Tool.run permit -> mutator seam -> protected evidence -> durable CAS
```

The mutator seam is entry to the admitted provider send operation. All MIME and
account validation precedes it. Pre-seam denial, invalid/missing protected data,
unavailable credentials/transport and cancellation prove the provider was not
entered. After entry, an exception, timeout, cancellation or malformed/unsafe
response is ambiguous. No exception implies no send. A process crash leaves
IN_FLIGHT, which startup converts to RECONCILE_REQUIRED. Result/evidence storage
failure never causes another provider call or a premature success claim.

A successful provider response must be the bounded, exact reviewed send response
shape with valid Gmail message/thread IDs. The provider wrapper binds it to the
exact submitted action/attempt/intent and MIME fingerprint. That is acceptance
evidence, not proof of recipient delivery, reading, or authorization. The
executor persists detached protected evidence via `EvidenceStore` before
publishing SUCCEEDED. Ordinary results are categorical; Gmail IDs remain private.

Cancellation before the seam may publish CANCELLED / NOT_INVOKED. Cancellation
after the seam never proves no send: unknown truth becomes RECONCILE_REQUIRED;
confirmed success remains success. Logical turn cancellation is independent.

## Reconciliation and recovery

The registered reconciler requires `NET_READ` and `CREDENTIAL_ACCESS`, with
separate host `allow_authenticated_reconciliation=True`. This permits only a
registered, claimed, read-only action probe. Revoking EMAIL_SEND blocks sends
without itself blocking an independently permitted probe. Current account,
connection and credentials still have to match. Read-tool content-disclosure
opt-in neither grants probe authority nor exposes reconciliation content.

The future probe must use authenticated `me`, label `SENT`, and the exact
`rfc822msgid:<marker>` query supported by Google's
[messages.list contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list).
It must retrieve bounded candidate evidence, detect pagination/truncation,
compare exact marker, sender/recipient roles, subject, body and content version,
and never treat Message-ID alone as proof. Gmail can modify transport headers
and Bcc representation: missing information is inconclusive, not a match.

The implemented non-network contract bounds candidates and marks completeness.
Exactly one complete, strong semantic match can establish SUCCEEDED. Zero
matches, multiple matches, incomplete enumeration, mismatch and probe failure
retain RECONCILE_REQUIRED. Zero is never a definitive absence proof. Multiple
matches retain categorical duplicate evidence; none is chosen arbitrarily.
Protected evidence is detached, bounded and linked by the executor to intent,
action, attempt/claim epoch. It grants neither send nor credential permission.

`ActionRecoveryCoordinator` audits/reclassifies/claims; only `ToolExecutor` probes.
Recovery restores no token or permission, never calls send automatically, and
does not reconstruct a consumed approval as new authority. Hosts restore
protected preparation continuity and explicitly bind fresh current runtime
services. Unknown versions, corrupt stores, lost credentials and exhausted
probes remain blocked. Schema v1 and its six states are unchanged.

## Retry and deliberate duplicate risk

There is no blind or hidden retry. The same action can retry only with Stage 5's
definitive no-effect proof and fresh current approval/attempt, retaining the
same MIME. Gmail search absence supplies no such proof. The foundation's probe
does not manufacture retry-safe absence.

An unresolved predecessor is never relabeled to enable another send. Stage 5
owns a deliberate risky-repeat successor, durable acknowledgement of the exact
predecessor snapshot, fresh ordinary approval and the predecessor execution
guard. A trusted adapter comparison allows only a newly prepared marker/Date
and the corresponding regenerated MIME while requiring identical account,
sender, recipients, subject, body, profile and MIME version. Both complete
intents remain independently digest bound. This avoids sharing a reconciliation
marker between logical actions. Any content/account change is rejected by this
repeat path. A still-available reconciliation path blocks risky repeat under
the existing Stage 5 rules. Historical acknowledgement is never permission.

## Future live transport boundary

`GmailReadHttp` and its GET-only endpoints are unchanged. This package adds no
HTTPS POST client, Gmail API client discovery, SMTP, generic request method or
live mutation registration. An injected test-only mutation/probe transport is
the only executable transport, and production defaults fail before its seam.
The next reviewed task must implement a separate fixed send transport plus
bounded production reconciliation, verify the sender assertion and recipient
Bcc isolation, and validate provider response/ambiguity behavior. It must not
weaken the read transport, current policy, durable approval or Stage 5 ordering.
