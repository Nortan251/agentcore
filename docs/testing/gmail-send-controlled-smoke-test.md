# Future controlled Gmail send/reconciliation smoke test

**Procedure only. Not executed in the live-transport PR.** Run only after that
PR is independently audited and merged, its exact-head Linux/Windows CI passes,
and the human explicitly authorizes a separate real-account task. Production
host UX/composition must be reviewed before using this procedure. Never turn it
into CI, a scheduled job, or an unattended automated test.

## Preconditions

1. Prefer a dedicated Google test account and a self-addressed message. Choose
   one explicitly reviewed recipient; start with To only, no Cc or Bcc.
2. Use the exact reviewed GMAIL_SEND profile (openid/email/profile,
   gmail.readonly, gmail.send). Complete consent only through the protected host
   lifecycle. Do not paste tokens, OAuth callbacks, secrets, vault files or
   private payloads into chat, terminals, issue bodies or log collectors.
3. Verify host storage ACLs and durable journal, approval, protected payload and
   evidence services. Payload/evidence stores are authenticated plaintext;
   protect them separately from DPAPI refresh storage. Disable credential/MIME
   inspection logs and HTTP debug output. Do not enable TLS key logging.
4. Explicitly compose `GmailMutationHttp`, the current exact send-profile
   connection, `allow_email_send=True`, and independent read-only reconciliation
   permission using the [architecture contract](../architecture/gmail-send-live-transport.md).
   Do not use direct `Tool.run`, transport methods, CLI token flags, or generic
   HTTP clients to bypass journal/approval authority.

## One exact send

1. Prepare one short plain-text message with an obvious unique subject such as
   `AgentCore controlled test - HUMAN-CHOSEN-UNIQUE-MARKER` and a harmless body
   identifying it as a one-message test. The host generates Message-ID and Date.
   Save the exact preparation handle privately; do not recreate it for retries.
2. In the private host review UI, confirm the authenticated signed identity and
   current connection, configured primary Gmail address, exact To, empty Cc/Bcc,
   subject and body. Confirm these details again immediately before granting
   durable approval. No recipient may be inferred from past tests or contacts.
3. Invoke the one prepared action once through the executor. Its profile GET
   must match the same primary sender before POST. If preflight fails, stop and
   inspect categorical status; do not change sender or refresh/reconnect as an
   automatic fallback. Corrected intent needs its own human review and approval.
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
4. If the original attempt is uncertain, retain the same action/preparation and
   use only the existing read-only recovery coordinator with current permission
   and credentials. Require one complete strong candidate before SUCCEEDED.
   Zero/incomplete results remain PROBE_LATER; multiple/mismatch results remain
   unknown for human resolution. Do not press Send again, recreate the proposal,
   opt into risky repeat, or interpret 401/429/5xx as permission to retry.
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
3. Disable the explicit host send opt-in after the test; disconnect the test
   connection if appropriate after read-only truth checks finish. Do not delete
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
