# Future controlled Gmail send/reconciliation smoke test

**DO NOT RUN LIVE SEND BEFORE THIS PR IS AUDITED AND MERGED**

**Procedure only. No real email was sent in the transport or host-composition
PR.** Run only after the production host/operator PR is independently audited
and merged, its exact-head Linux/Windows CI passes, and the human explicitly
chooses the test account and authorizes this real action.
Follow the [production host setup](../operations/gmail-production-host-setup.md).
Never turn this into CI, a model tool, a scheduled job or an unattended test.
The coding PR ends READY FOR HUMAN-CONTROLLED SMOKE TEST AFTER AUDIT AND MERGE,
not LIVE TEST COMPLETED.

## Preconditions

1. Prefer a dedicated Google test account. The first message **must be
   self-addressed**: exactly one To equal to the authenticated verified primary
   Gmail address, no Cc and no Bcc. No aliases or alternate recipient.
2. Use the exact reviewed GMAIL_SEND profile (openid/email/profile,
   gmail.readonly, gmail.send). Complete consent only through the protected host
   lifecycle. Do not paste tokens, OAuth callbacks, secrets, vault files or
   private payloads into chat, terminals, issue bodies or log collectors.
3. Verify host storage ACLs and durable journal, approval, protected payload and
   evidence services. Payload/evidence stores are authenticated plaintext;
   protect them separately from DPAPI refresh storage. Disable credential/MIME
   inspection logs and HTTP debug output. Do not enable TLS key logging.
4. Use the isolated `python -m agentcore.gmail_host` path. Send and reconciliation
   construct separate host permissions under the
   [architecture contract](../architecture/gmail-send-live-transport.md).
   Never use direct `Tool.run`, transport methods, token flags or generic HTTP.
   Use a private unredirected Windows console with no transcription, recording,
   terminal logging or agent-controlled input.

## Exact future human commands

After audit, merge and explicit human authorization only:

```powershell
python -m agentcore.gmail_host init
```

Manually configure the Google testing project, Gmail API, consent/test user and
Desktop client. Install the host-only client file using the setup guide; no
secret/token goes on the command line. That guide documents the complete
consent requirement, tester restrictions/warnings and seven-day testing refresh
lifetime with official Google sources. Do not bypass warnings or restrictions.

```powershell
python -m agentcore.gmail_host status
python -m agentcore.gmail_host connect
python -m agentcore.gmail_host status
python -m agentcore.gmail_host prepare-smoke-test
```

Use the hidden prompts for an obvious unique subject and harmless short
plain-text body. The sole recipient is the authenticated primary self address;
no recipient/subject/body flags exist. Record the printed opaque action ID and
substitute that exact value for `act_FROM_HOST_OUTPUT` below:

```powershell
python -m agentcore.gmail_host review act_FROM_HOST_OUTPUT
python -m agentcore.gmail_host action-status act_FROM_HOST_OUTPUT
```

PREPARED is a deliberate boundary: no approval, journal authorization or send
attempt. Do not prepare again. Restart restores the same protected handle and
Message-ID. After the exact private review, the future live command is:

```powershell
python -m agentcore.gmail_host send-smoke-test act_FROM_HOST_OUTPUT --live-send
```

The explicit live command/flag is the first gate. Inside durable approval, a
fresh action-bound challenge immediately follows the exact private review.
Before typing it, personally confirm the correct Google account, primary
sender, self To, exact subject/body, NO Cc and NO Bcc. The review warns:
`THIS WILL SEND A REAL EMAIL IF EXECUTED`.
Wrong, stale, expired or model-supplied confirmation does not approve.

Without the flag the command stops before services/network. With both gates
satisfied, the test permits **one approved action and one physical POST attempt
maximum**. The CLI cannot repeat an attempted action, even FAILED/NOT_INVOKED,
or reset its one-preparation slot. Saved approval alone cannot execute.

## One exact send

1. Prepare one short plain-text message with an obvious unique subject such as
   `AgentCore controlled test - HUMAN-CHOSEN-UNIQUE-MARKER` and a harmless body
   identifying it as a one-message test. The host generates Message-ID and Date.
   The host saves the exact handle privately; retain its opaque action ID.
   Never recreate the preparation for retries.
2. In the private host review UI, confirm the authenticated signed identity and
   current connection, configured primary Gmail address, exact To, empty Cc/Bcc,
   subject and body. Confirm these details again immediately before granting
   durable approval. No recipient may be inferred from past tests or contacts.
3. Invoke the one prepared action once through the executor. Its profile GET
   must match the same primary sender before POST. If preflight fails, stop and
   inspect categorical status; do not change sender or refresh/reconnect as an
   automatic fallback. FAILED before provider entry is not permission to create
   another message automatically. Stop this controlled test and investigate.
4. Record only the action ID, attempt ID and categorical durable result in the
   test report. Keep Gmail IDs, Message-ID, review content and evidence private.
   Do not claim success from browser consent, callback receipt, profile success
   or a transient network receipt. Require durable SUCCEEDED or explicitly
   record RECONCILE_REQUIRED/publication uncertainty.

## Sent copy, correlation and duplicate check

1. In the test account's Gmail UI, inspect Sent and the intended recipient copy.
   Verify one message with the unique subject and the reviewed From/To/body;
   inspect original headers privately and compare the exact prepared Message-ID
   and Date. A Sent copy does not by itself prove delivery to every recipient.
2. Search Sent using `rfc822msgid:actual-marker@agentcore.invalid` (remove the
   RFC angle brackets only in the query). Confirm exactly one matching message
   and check the unique test subject for unexpected duplicate Message-IDs.
   Inspect threads carefully: one thread can contain more than one message.
3. If the action is already durably SUCCEEDED, do not fabricate an uncertain
   journal state to force recovery. Validate correlation read-only in Gmail UI.
   Exercising the production recovery probe in that situation requires a
   separately reviewed host diagnostic/fault-cut procedure that preserves
   durable truth; it must never resend or manually edit the journal.
4. If uncertain, **DO NOT SEND AGAIN**. Retain the same action/preparation and run:

   ```powershell
   python -m agentcore.gmail_host reconcile act_FROM_HOST_OUTPUT
   python -m agentcore.gmail_host action-status act_FROM_HOST_OUTPUT
   ```

   This uses only the existing read-only recovery coordinator with current
   authenticated reconciliation permission and the exact bound connection.
   It cannot invoke send or create an action. One complete strong candidate is
   required before SUCCEEDED. Zero/incomplete results remain PROBE_LATER:
   `No conclusive Sent match found; effect remains uncertain.`
   Multiple/mismatch results require human investigation. Do not press Send
   again, recreate the proposal, opt into risky repeat, or treat 401/429/5xx as
   retry authority. Never report uncertainty as "email was not sent."
5. A delayed Sent search is not proof of absence. Stop after a bounded,
   human-observed read-only check and preserve uncertainty if truth is not yet
   established. Do not add an automatic polling or resend loop for this test.

## Privacy and finish

1. Privately inspect categorical logs, ToolResult, ordinary journal, approval
   records and any session/state output. Confirm they contain no tokens,
   Authorization header, recipients, subject/body, raw responses, MIME or query
   marker. Use a trusted local secret-aware check if available; report only its
   pass/fail outcome, never a matching line, captured secret or screenshot.
2. Confirm there was only one POST attempt, no hidden refresh/resend and no
   duplicate message. If application state or mailbox evidence disagrees, stop
   further sending and retain the private evidence for investigation.
3. The command closes send composition on exit. Keep it disabled by using only
   status/inspection/reconciliation commands. After truth checks finish,
   `python -m agentcore.gmail_host disconnect` may locally disconnect the test
   account; this does not revoke Google consent. Do not delete
   journal/approval/evidence to hide an uncertain result. Human-controlled
   mailbox cleanup is separate and is not a tool feature of this PR.
4. Report audited commit, platforms, one attempted action, categorical result,
   Sent/correlation/duplicate/privacy observations and any unresolved issue.
   Do not claim Bcc isolation, all-recipient delivery, or broad Gmail readiness.

Cc may be tested later with separately reviewed recipients and new explicit
approval. Bcc remains disabled. Its enablement needs a separate reviewed change
and controlled recipient-copy isolation validation; this procedure must not
bypass the live-v1 block. Drafts, replies, forwards, HTML, attachments and aliases
are outside this procedure.

The next action after this PR's independent audit and merge is this **one
explicitly human-authorized, self-addressed real Gmail send**, followed by
Sent/Message-ID/duplicate verification and reconciliation only if uncertain.
It is not another coding task. Do not execute it during this PR.
