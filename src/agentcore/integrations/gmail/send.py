"""Non-network Gmail send foundation. No production send/probe transport.

The host prepares private intent and supplies ordinary Stage 5 services. This
module does not grant permission, consume approval, claim attempts, or recover
actions. Tests explicitly inject a test-only transport; defaults fail closed.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agentcore.action_journal import (
    ActionAdapterSpec, ActionEffectClass, ActionInvocationContext, ActionJournalEligibility,
    ActionReconciliationContext, ActionReconciliationResult, EffectVerdict,
    NormalizedActionEffect, PreparedActionIntent, ProtectedActionPayloadStore,
    ReconciliationDisposition,
)
from agentcore.credentials import CredentialInfo, CredentialRuntime
from agentcore.credentials.action import CredentialActionExecution, CredentialActionIdentity
from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.vault import CredentialLease
from agentcore.integrations.gmail.send_intent import (
    MAX_BODY_BYTES, MAX_SUBJECT_BYTES, PREPARATION_SCOPE,
    SendIntentError, make_send_payload, message_fingerprint, message_semantics,
    normalize_address, normalize_send_arguments, repeat_intent_equivalent, validate_send_payload,
)
from agentcore.permissions.levels import Capability
from agentcore.tools.authenticated_action import AuthenticatedActionTool
from agentcore.tools.base import ParamType, ToolParameter, ToolResult, tool_spec
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


SEND_ACTION_KIND = "gmail.send_message.v1"
MAX_PREPARATIONS = 256
MAX_PROBE_CANDIDATES = 5
_PROVIDER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _provider_id(value: Any) -> str:
    if type(value) is not str or _PROVIDER_ID.fullmatch(value) is None:
        raise SendIntentError()
    return value


@dataclass(frozen=True)
class GmailSendPreparation:
    """Inert host continuity. No content, credential, approval or execution."""

    preparation_id: str
    storage_action_id: str
    reference: str
    intent_digest: str

    def context(self, *, action_id: str | None = None, repeat: bool = False) -> ActionInvocationContext:
        return ActionInvocationContext(
            action_id=action_id, intent_preparation_id=self.preparation_id,
            deduplication_scope=None if repeat else PREPARATION_SCOPE,
            deduplication_key=None if repeat else self.preparation_id,
        ).validate()


class GmailSendSubmission(RuntimeOnly):
    """Private provider input, never a model raw-MIME parameter."""

    def __init__(self, payload: Mapping[str, Any], identity: CredentialActionIdentity) -> None:
        self._owner_pid = os.getpid()
        self._payload = validate_send_payload(payload)
        self.identity = identity.validate()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @property
    def mime_bytes(self) -> bytes:
        self._owner()
        return base64.b64decode(self._payload["mime_base64"], validate=True)

    @property
    def semantics(self) -> dict[str, Any]:
        self._owner()
        return json.loads(json.dumps(message_semantics(self._payload)))

    @property
    def message_id(self) -> str:
        self._owner()
        return self._payload["message_id"]

    @property
    def fingerprint(self) -> str:
        self._owner()
        return message_fingerprint(self._payload)


class GmailProbeRequest(GmailSendSubmission):
    """Structured future read probe; the transport cannot choose an account."""

    user_id = "me"
    label_ids = ("SENT",)
    max_candidates = MAX_PROBE_CANDIDATES

    @property
    def query(self) -> str:
        return "rfc822msgid:" + self.message_id


@dataclass(frozen=True, repr=False)
class GmailProbeCandidate:
    message_id: str
    thread_id: str
    sent: bool
    complete: bool
    semantics: Mapping[str, Any]


@dataclass(frozen=True, repr=False)
class GmailProbeResponse:
    candidates: tuple[GmailProbeCandidate, ...]
    complete: bool


class GmailMutationTransport(Protocol):
    """No implementation ships here. Inject only deterministic test fakes.

    Future send implements one fixed request without retry. Probe performs
    bounded reads only and must report incomplete/truncated candidate truth.
    """

    test_only: bool

    def send(self, submission: GmailSendSubmission, credential: CredentialLease) -> Mapping[str, Any]: ...
    def probe(self, request: GmailProbeRequest, credential: CredentialLease) -> GmailProbeResponse: ...


class UnavailableGmailMutationTransport(RuntimeOnly):
    """Production default deliberately contains no HTTP or SMTP client."""

    def send(self, submission, credential):
        raise CredentialError(CredentialFailure.UNSUPPORTED)

    def probe(self, request, credential):
        raise CredentialError(CredentialFailure.UNSUPPORTED)


class _SendProvider(RuntimeOnly):
    def __init__(self, sender: str, transport: GmailMutationTransport | None) -> None:
        self._owner_pid, self._sender = os.getpid(), normalize_address(sender)
        self._available = transport is not None
        self._transport = transport if transport is not None else UnavailableGmailMutationTransport()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @property
    def provider_id(self) -> str:
        self._owner()
        return "google"

    def validate_intent(self, payload: Mapping[str, Any], info: CredentialInfo, *, reconcile: bool) -> None:
        self._owner()
        value = validate_send_payload(payload)
        account = value["account"]
        if info.provider != "google" or account["account_id"] != info.account_id or (
                account["connection_id"] != info.connection_id or account["sender"] != self._sender):
            raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
        if info.scopes != GOOGLE_GMAIL_SEND_SCOPES:
            raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
        if not self._available:
            raise CredentialError(CredentialFailure.UNSUPPORTED)

    def send(self, payload: Mapping[str, Any], credential: CredentialLease,
             identity: CredentialActionIdentity) -> ToolResult:
        self.validate_intent(payload, credential.info, reconcile=False)
        credential._check_current()
        submission = GmailSendSubmission(payload, identity)
        raw = self._transport.send(submission, credential)
        self._owner()
        if type(raw) is not dict or not {"id", "threadId"} <= set(raw) or not set(raw) <= {"id", "threadId", "labelIds"}:
            raise SendIntentError()
        message_id, thread_id = _provider_id(raw["id"]), _provider_id(raw["threadId"])
        if "labelIds" in raw:
            labels = raw["labelIds"]
            if type(labels) is not list or not 1 <= len(labels) <= 20 or "SENT" not in labels:
                raise SendIntentError()
            for label in labels:
                _provider_id(label)
        return ToolResult.success({
            "effect": "applied", "message_id": message_id, "thread_id": thread_id,
            "marker": submission.message_id, "fingerprint": submission.fingerprint,
            "action_id": identity.action_id, "attempt_id": identity.attempt_id,
        })

    def reconcile(self, payload: Mapping[str, Any], credential: CredentialLease,
                  identity: CredentialActionIdentity) -> ToolResult:
        self.validate_intent(payload, credential.info, reconcile=True)
        credential._check_current()
        request = GmailProbeRequest(payload, identity)
        response = self._transport.probe(request, credential)
        self._owner()
        if type(response) is not GmailProbeResponse or type(response.complete) is not bool or (
                type(response.candidates) is not tuple or len(response.candidates) > MAX_PROBE_CANDIDATES):
            raise SendIntentError()
        candidates = []
        seen = set()
        expected = request.semantics
        for item in response.candidates:
            if type(item) is not GmailProbeCandidate or type(item.sent) is not bool or type(item.complete) is not bool:
                raise SendIntentError()
            message_id, thread_id = _provider_id(item.message_id), _provider_id(item.thread_id)
            if message_id in seen:
                raise SendIntentError()
            seen.add(message_id)
            # Missing fields never establish complete recipient/content truth.
            semantics = item.semantics
            if type(semantics) is not dict or not set(semantics) <= set(expected):
                raise SendIntentError()
            safe = {}
            if "sender" in semantics:
                safe["sender"] = normalize_address(semantics["sender"])
            if "message" in semantics:
                message = semantics["message"]
                if type(message) is not dict or set(message) != {"to", "cc", "bcc", "subject", "body"}:
                    raise SendIntentError()
                safe["message"] = normalize_send_arguments(message)
            for name in ("message_id", "date"):
                if name in semantics:
                    value = semantics[name]
                    if type(value) is not str or len(value) > 128 or any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
                        raise SendIntentError()
                    safe[name] = value
            if "mime_version" in semantics:
                if type(semantics["mime_version"]) is not int:
                    raise SendIntentError()
                safe["mime_version"] = semantics["mime_version"]
            strong = item.sent and item.complete and safe == expected
            candidates.append({"message_id": message_id, "thread_id": thread_id, "strong_match": strong})
        strong = response.complete and len(candidates) == 1 and candidates[0]["strong_match"]
        reason = ("single_match" if strong else "incomplete" if not response.complete else
                  "no_match" if not candidates else "multiple_matches" if len(candidates) > 1 else "mismatch")
        return ToolResult.success({
            "effect": "applied" if strong else "unknown", "reason": reason,
            "candidates": candidates, "complete": response.complete,
            "marker": request.message_id, "fingerprint": request.fingerprint,
            "action_id": identity.action_id, "attempt_id": identity.attempt_id,
        })


class GmailSendActionAdapter(RuntimeOnly):
    def __init__(self, foundation: GmailSendFoundation) -> None:
        self._foundation = foundation
        self.spec = ActionAdapterSpec(
            tool_name=foundation.tool.spec.name, tool_version=foundation.tool.spec.version,
            action_kind=SEND_ACTION_KIND, eligibility=ActionJournalEligibility.EXTERNAL_ACCOUNT_MUTATION,
            effect_class=ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS,
            reconciliation_capabilities=frozenset({Capability.NET_READ, Capability.CREDENTIAL_ACCESS}),
        )

    def prepare_intent(self, cleaned, context) -> PreparedActionIntent:
        payload, handle = self._foundation._load_preparation(context.intent_preparation_id)
        if normalize_send_arguments(cleaned) != payload["message"]:
            raise SendIntentError()
        return PreparedActionIntent(
            intent_digest=handle.intent_digest, target_category="gmail.mailbox",
            requires_protected_payload=True, protected_payload=payload,
            approval_description="Send one plain-text Gmail message. Review the exact protected message in the host UI.",
        ).validate()

    def normalize_effect(self, result) -> NormalizedActionEffect:
        self._foundation._owner()
        if type(result) is not ToolResult or result.ok is not True or type(result.output) is not dict or result.output.get("effect") != "applied":
            raise SendIntentError()
        return NormalizedActionEffect(
            EffectVerdict.CONFIRMED_APPLIED, "gmail.send.accepted", "Gmail accepted the message for sending.",
            protected_evidence=(result.output,),
        )

    def repeat_intent_equivalent(self, before, after) -> bool:
        self._foundation._owner()
        return repeat_intent_equivalent(before, after)


class GmailSendReconciler(RuntimeOnly):
    def __init__(self) -> None:
        self._owner_pid = os.getpid()

    def probe(self, context: ActionReconciliationContext) -> ActionReconciliationResult:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        permit = context.credential_execution
        if type(permit) is not CredentialActionExecution:
            raise CredentialError(CredentialFailure.AUTHORITY)
        result = permit.invoke_probe(self)
        output = result.output
        applied = output["effect"] == "applied"
        disposition = (ReconciliationDisposition.NONE if applied else
                       ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
                       if output["reason"] in {"multiple_matches", "mismatch"} else
                       ReconciliationDisposition.PROBE_LATER)
        return ActionReconciliationResult(
            verdict=EffectVerdict.CONFIRMED_APPLIED if applied else EffectVerdict.OUTCOME_UNKNOWN,
            result_class="gmail.reconcile." + output["reason"],
            summary="The intended sent message was verified." if applied else "Gmail send truth remains unresolved.",
            reconciliation_disposition=disposition, protected_evidence=(output,), retry_safe=False,
        )


class GmailSendFoundation(RuntimeOnly):
    """Trusted staging and composition, with an unavailable production default."""

    def __init__(self, payload_store: ProtectedActionPayloadStore, *, sender: str,
                 transport: GmailMutationTransport | None = None, test_only: bool = False,
                 clock: Clock | None = None) -> None:
        if type(test_only) is not bool or (transport is not None and
                (not test_only or getattr(transport, "test_only", None) is not True)):
            raise CredentialError(CredentialFailure.UNSUPPORTED)
        self._owner_pid = os.getpid()
        self._store, self._clock = payload_store, clock or SystemClock()
        self._provider = _SendProvider(sender, transport)
        self._runtime: CredentialRuntime | None = None
        self._preparations: dict[str, GmailSendPreparation] = {}
        self._lock = threading.RLock()
        self.tool = AuthenticatedActionTool(tool_spec(
            "gmail_send_message", "Propose one plain-text email for protected host review and durable approval. Live sending is unavailable.",
            parameters=(
                ToolParameter("to", ParamType.ARRAY, "Bare mailboxes; at least one. At most 20 total To/Cc/Bcc recipients."),
                ToolParameter("cc", ParamType.ARRAY, "Optional bare mailboxes.", required=False),
                ToolParameter("bcc", ParamType.ARRAY, "Optional blind recipients, privately submitted to Gmail.", required=False),
                ToolParameter("subject", ParamType.STRING, "At most 1024 UTF-8 bytes; no header controls.", max_length=MAX_SUBJECT_BYTES),
                ToolParameter("body", ParamType.STRING, "Plain text only; at most 65536 normalized UTF-8 bytes.", max_length=MAX_BODY_BYTES),
            ), capabilities=(Capability.EMAIL_SEND, Capability.NET_WRITE, Capability.CREDENTIAL_ACCESS),
            read_only=False, tags=("gmail", "authenticated-action", "foundation-only")),
            normalize_send_arguments, preparation_scope=PREPARATION_SCOPE,
        )
        self.adapter = GmailSendActionAdapter(self)
        self.reconciler = GmailSendReconciler()

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def bind(self, runtime: CredentialRuntime, reference: str, *, replace_existing: bool = False) -> CredentialInfo:
        self._owner()
        if self._runtime is not None and runtime is not self._runtime:
            raise CredentialError(CredentialFailure.BINDING)
        info = runtime.bind_action(self.tool, reference=reference, provider=self._provider,
                                   required_scopes=GOOGLE_GMAIL_SEND_SCOPES, replace_existing=replace_existing)
        self._runtime = runtime
        return info

    def prepare(self, arguments: Mapping[str, Any]) -> GmailSendPreparation:
        self._owner()
        if self._runtime is None:
            raise CredentialError(CredentialFailure.BINDING)
        with self._lock:
            if len(self._preparations) >= MAX_PREPARATIONS:
                raise CredentialError(CredentialFailure.CAPACITY)
        info = self._runtime.action_info(self.tool)
        payload = make_send_payload(arguments, info, sender=self._provider._sender, now=self._clock.now())
        stored = self._store.create(new_id("act"), payload)
        handle = GmailSendPreparation(payload["preparation_id"], stored.action_id, stored.reference, stored.intent_digest)
        self.restore(handle)
        return handle

    def restore(self, handle: GmailSendPreparation) -> None:
        self._owner()
        if type(handle) is not GmailSendPreparation:
            raise SendIntentError()
        stored = self._store.load(handle.reference, action_id=handle.storage_action_id,
                                  expected_intent_digest=handle.intent_digest)
        payload = validate_send_payload(stored.payload)
        if payload["preparation_id"] != handle.preparation_id:
            raise SendIntentError()
        with self._lock:
            self._owner()
            existing = self._preparations.get(handle.preparation_id)
            if existing is not None and existing != handle:
                raise SendIntentError()
            if existing is None and len(self._preparations) >= MAX_PREPARATIONS:
                raise CredentialError(CredentialFailure.CAPACITY)
            self._preparations[handle.preparation_id] = handle

    def release(self, handle: GmailSendPreparation) -> bool:
        """Forget an exact host handle and reclaim its in-process slot.

        This does not cancel admitted work, revoke approval, or delete durable
        intent. Retain the handle and restore it to resume the same action.
        """
        self._owner()
        if type(handle) is not GmailSendPreparation:
            raise SendIntentError()
        with self._lock:
            self._owner()
            existing = self._preparations.get(handle.preparation_id)
            if existing is None:
                return False
            if existing != handle:
                raise SendIntentError()
            del self._preparations[handle.preparation_id]
            return True

    def _load_preparation(self, preparation_id: str) -> tuple[dict[str, Any], GmailSendPreparation]:
        self._owner()
        with self._lock:
            handle = self._preparations.get(preparation_id)
        if handle is None:
            raise SendIntentError()
        stored = self._store.load(handle.reference, action_id=handle.storage_action_id,
                                  expected_intent_digest=handle.intent_digest)
        return validate_send_payload(stored.payload), handle

    def review(self, handle: GmailSendPreparation) -> dict[str, Any]:
        """Explicit private host UI only. Never pass this to generic logs/state."""
        self._owner()
        payload, exact = self._load_preparation(handle.preparation_id)
        if exact != handle:
            raise SendIntentError()
        return {"account": payload["account"], **message_semantics(payload)}
