"""Explicit host orchestration over the reviewed executor and durable stores."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from agentcore.action_journal import (
    ActionAdapterRegistry, ActionState, ReconciliationDisposition, permission_policy_fingerprint,
)
from agentcore.action_journal.errors import ActionNotFoundError
from agentcore.approvals import (
    ApprovalAuthorizationStatus, ApprovalInvocationContext, ApprovalPrompt,
    ApprovalState, DurableApprovalAuthority, HumanApprovalDecision,
)
from agentcore.credentials import GoogleAccessProfile
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.protected import ConnectionState
from agentcore.gmail_host.errors import GmailHostError, HostFailure
from agentcore.gmail_host.operator import PrivateOperator, show_review
from agentcore.gmail_host.storage import SmokeTestSlot
from agentcore.integrations.gmail.mutation_http import GmailMutationHttp
from agentcore.integrations.gmail.send import GmailSendFoundation, SEND_ACTION_KIND
from agentcore.integrations.gmail.send_intent import (
    PREPARATION_SCOPE, normalize_address, normalize_send_arguments, validate_send_payload,
)
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import Outcome, PermissionPolicy
from agentcore.recovery.action import ActionRecoveryCoordinator
from agentcore.tools.authenticated import AuthenticatedReadTool
from agentcore.tools.base import ToolResult, tool_spec
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import SystemClock
from agentcore.util.ids import new_id


MAX_SMOKE_SUBJECT_BYTES = 160
MAX_SMOKE_BODY_BYTES = 1024
CONFIRMATION_SECONDS = 120


class GmailHostMode(Enum):
    INSPECT = "inspect"
    PREPARE = "prepare"
    LIVE_SEND = "live_send"
    RECONCILE = "reconcile"


def smoke_arguments(arguments: dict, primary: str) -> dict:
    """Additional operator restriction; the general send contract is unchanged."""
    message = normalize_send_arguments(arguments)
    if message["to"] != [normalize_address(primary)] or message["cc"] or message["bcc"]:
        raise GmailHostError(HostFailure.INVALID_INPUT)
    if not message["subject"].strip() or not message["body"].strip() or (
        len(message["subject"].encode("utf-8")) > MAX_SMOKE_SUBJECT_BYTES
        or len(message["body"].encode("utf-8")) > MAX_SMOKE_BODY_BYTES
        or message["body"].count("\n") > 8
        or re.search(r"<[/!A-Za-z][^>]*>", message["body"])
        or message["subject"].lower().startswith(("re:", "fw:", "fwd:"))
    ):
        raise GmailHostError(HostFailure.INVALID_INPUT)
    return message


class GmailHostServices(RuntimeOnly):
    """Only trusted constructor dependencies, with no automatic composition."""

    def __init__(self, *, google, runtime, storage, payloads, evidence, actions,
                 approvals, clock=None) -> None:
        self._pid = os.getpid()
        self.google, self.runtime, self.storage = google, runtime, storage
        self.payloads, self.evidence, self.actions = payloads, evidence, actions
        self.approvals = approvals
        self.clock = clock or SystemClock()

    def require_owner(self) -> None:
        if self._pid != os.getpid():
            raise GmailHostError(HostFailure.OWNERSHIP)

    def connect(self, operator: PrivateOperator) -> None:
        self.require_owner()
        operator.require_interactive()
        # Reconnect cannot repair an old intent's exact connection binding.
        if self.storage.load_slot() is not None:
            raise GmailHostError(HostFailure.EXISTING)
        self.google.connect(profile=GoogleAccessProfile.GMAIL_SEND)

    def disconnect(self) -> None:
        self.require_owner()
        status = self.google.restore()
        self.google.disconnect(status.credential.reference if status.credential else None)


class _PrimaryProfile(RuntimeOnly):
    """Host-only authenticated read. No address enters a ToolResult or logs."""

    def __init__(self, transport: GmailMutationHttp) -> None:
        self._pid = os.getpid()
        self._transport = transport
        self._verified = None

    @property
    def provider_id(self) -> str:
        return "google"

    def read(self, args, credential) -> ToolResult:
        if self._pid != os.getpid() or args != {}:
            raise GmailHostError(HostFailure.OWNERSHIP)
        self._verified = None
        address = self._transport.primary_address(credential)
        credential._check_current()
        self._verified = (credential.info, address)
        return ToolResult.success({"primary_profile_verified": True})

    def take(self, info) -> str:
        if self._pid != os.getpid():
            raise GmailHostError(HostFailure.OWNERSHIP)
        verified, self._verified = self._verified, None
        if verified is None or verified[0] != info:
            raise GmailHostError(HostFailure.CONNECTION)
        return verified[1]


class _PrivateApprovalGate(RuntimeOnly):
    """A one-use interactive review for one freshly minted durable request."""

    def __init__(self, host: GmailHostComposition) -> None:
        self._pid = os.getpid()
        self._host = host
        self._armed = None

    @contextmanager
    def arm(self, request_id: str, slot: SmokeTestSlot):
        self._host._owner()
        if self._armed is not None or self._host.mode is not GmailHostMode.LIVE_SEND:
            raise GmailHostError(HostFailure.CONFIRMATION)
        ticket = (request_id, slot, threading.get_ident(), time.monotonic())
        self._armed = ticket
        try:
            yield
        finally:
            self._host._owner()
            self._armed = None

    def decide(self, prompt: ApprovalPrompt) -> HumanApprovalDecision:
        self._host._owner()
        if self._pid != os.getpid():
            raise GmailHostError(HostFailure.OWNERSHIP)
        ticket, self._armed = self._armed, None
        if ticket is None:
            return HumanApprovalDecision.PENDING
        request_id, slot, thread_id, started = ticket
        if type(prompt) is not ApprovalPrompt or prompt.request_id != request_id or (
            thread_id != threading.get_ident() or time.monotonic() - started > CONFIRMATION_SECONDS
        ):
            return HumanApprovalDecision.DENY
        request = self._host.services.approvals.load(request_id)
        # The authority authenticated this request before calling its gate;
        # this comparison additionally binds the private UX to the same intent.
        if request.state is not ApprovalState.PENDING or request.action_id != slot.action_id or (
            request.as_binding() != self._host._approval_binding(slot)
        ):
            return HumanApprovalDecision.DENY
        review = self._host._checked_review(slot)
        show_review(self._host.operator, slot, review)
        challenge = f"SEND {slot.action_id} {secrets.token_hex(8)}"
        self._host.operator.write_private(
            "\nConfirm the correct Google account, primary sender, self recipient,\n"
            "exact subject and body, NO Cc and NO Bcc. This approves ONE real send.\n"
            f"Type this one-time challenge within {CONFIRMATION_SECONDS} seconds:\n{challenge}\n"
        )
        answer = self._host.operator.read_private("Confirmation (hidden): ", maximum=96)
        self._host._owner()
        if thread_id != threading.get_ident() or time.monotonic() - started > CONFIRMATION_SECONDS or (
            self._host._checked_review(slot) != review
        ):
            return HumanApprovalDecision.DENY
        return (HumanApprovalDecision.APPROVE if type(answer) is str and answer.isascii() and hmac.compare_digest(answer, challenge)
                else HumanApprovalDecision.DENY)


@dataclass(frozen=True)
class HostActionStatus:
    action_id: str
    state: str
    attempts: int
    probe_count: int
    disposition: str
    last_attempt: str
    effect: str
    attempt_id: str


class GmailHostComposition(RuntimeOnly):
    """An isolated registry/executor, never installed in an Agent or scheduler.

    INSPECT is the default. A transport, a connection, and saved correlation
    never select LIVE_SEND. The CLI constructs that mode only for its explicit
    --live-send command; the private durable gate remains independently required.
    """

    def __init__(self, services: GmailHostServices, operator: PrivateOperator, *,
                 transport: GmailMutationHttp, mode: GmailHostMode = GmailHostMode.INSPECT,
                 permissions: PermissionConfig | None = None, logger=None) -> None:
        if type(services) is not GmailHostServices or type(mode) is not GmailHostMode or (
            type(transport) is not GmailMutationHttp
        ):
            raise GmailHostError(HostFailure.DISABLED)
        self._pid, self._thread = os.getpid(), threading.get_ident()
        services.require_owner()
        self.services, self.operator, self.mode, self.transport = services, operator, mode, transport
        self.info = self.primary = None
        self.registry = ToolRegistry()
        self.adapters = ActionAdapterRegistry()
        self.foundation = None
        self.gate = _PrivateApprovalGate(self)
        self.authority = DurableApprovalAuthority(
            services.approvals, services.storage.key_provider,
            principal_ref="principal:local-gmail-operator", scope_ref="scope:gmail-smoke-host",
            gate=self.gate, clock=services.clock,
        )
        policy = PermissionPolicy(
            permissions or PermissionConfig(level=PermissionLevel.CONFIRM),
            allow_email_send=mode is GmailHostMode.LIVE_SEND,
            allow_authenticated_reads=mode in {GmailHostMode.PREPARE, GmailHostMode.LIVE_SEND},
            allow_authenticated_reconciliation=mode is GmailHostMode.RECONCILE,
        )
        self.executor = ToolExecutor(
            self.registry, policy, action_store=services.actions, action_adapters=self.adapters,
            protected_payload_store=services.payloads, evidence_store=services.evidence,
            approval_authority=self.authority, credential_runtime=services.runtime,
            clock=services.clock, logger=logger,
        )
        slot = services.storage.load_slot()
        if slot is not None:
            stored = services.payloads.load(
                slot.preparation.reference, action_id=slot.preparation.storage_action_id,
                expected_intent_digest=slot.preparation.intent_digest,
            )
            payload = validate_send_payload(stored.payload)
            self._foundation(payload["account"]["sender"])
            self.foundation.restore(slot.preparation)
        self.recovery = ActionRecoveryCoordinator(services.actions, self.executor, clock=services.clock)
        if self.recovery.recover_startup().quarantined:
            raise GmailHostError(HostFailure.STORAGE)

    def _owner(self) -> None:
        if self._pid != os.getpid() or self._thread != threading.get_ident():
            raise GmailHostError(HostFailure.OWNERSHIP)
        self.services.require_owner()

    @property
    def mutation_enabled(self) -> bool:
        self._owner()
        return self.mode is GmailHostMode.LIVE_SEND and self.executor.policy.allow_email_send

    def _foundation(self, sender: str) -> None:
        self._owner()
        if self.foundation is not None:
            raise GmailHostError(HostFailure.EXISTING)
        self.foundation = GmailSendFoundation(
            self.services.payloads, sender=sender, clock=self.services.clock,
            transport=None if self.mode is GmailHostMode.INSPECT else self.transport,
        )
        if self.mode is not GmailHostMode.INSPECT:
            self.registry.register(self.foundation.tool)
            self.adapters.register(self.foundation.tool, self.foundation.adapter, reconciler=self.foundation.reconciler)

    def _activate(self, *, verify_primary: bool) -> None:
        self._owner()
        status = self.services.google.restore()
        if status.credential is None or status.state is ConnectionState.DISCONNECTED or (
            status.credential.scopes != GOOGLE_GMAIL_SEND_SCOPES
        ):
            raise GmailHostError(HostFailure.CONNECTION)
        # Explicit host lifecycle work before binding, never a provider retry.
        self.info = self.services.google.ensure_current(status.credential.reference)
        if verify_primary:
            provider = _PrimaryProfile(self.transport)
            tool = AuthenticatedReadTool(tool_spec(
                "gmail_host_primary_profile", "Verify the bound primary mailbox privately.",
                capabilities=(Capability.NET_READ, Capability.CREDENTIAL_ACCESS),
            ))
            self.services.runtime.bind(
                tool, reference=self.info.reference, provider=provider, required_scopes=GOOGLE_GMAIL_SEND_SCOPES,
            )
            self.registry.register(tool)
            result = self.executor.invoke(tool.spec.name, {})
            if not result.result.ok:
                raise GmailHostError(HostFailure.CONNECTION)
            self.primary = provider.take(self.info)
        if self.foundation is None:
            if self.primary is None:
                raise GmailHostError(HostFailure.MISSING)
            self._foundation(self.primary)
        self.foundation.bind(self.services.runtime, self.info.reference)

    def _slot(self, action_id: str | None = None) -> SmokeTestSlot:
        self._owner()
        slot = self.services.storage.load_slot()
        if slot is None or (action_id is not None and slot.action_id != action_id):
            raise GmailHostError(HostFailure.MISSING)
        return slot

    def _record(self, slot: SmokeTestSlot):
        self._owner()
        try:
            record = self.services.actions.load(slot.action_id)
        except ActionNotFoundError:
            # Detect a missing/misdirected correlation instead of creating a
            # successor for the same preparation's existing logical action.
            if self.services.actions.find_by_deduplication(PREPARATION_SCOPE, slot.preparation.preparation_id) is not None:
                raise GmailHostError(HostFailure.STORAGE)
            # Consumed history cannot grant execution, but can prove that an
            # absent journal is no longer an untouched preparation. Do not
            # recreate an approved action after journal loss/publication doubt.
            for request_id in self.services.approvals.list_requests():
                request = self.services.approvals.load(request_id)
                if request.action_id == slot.action_id and request.state is ApprovalState.CONSUMED:
                    raise GmailHostError(HostFailure.STORAGE)
            return None
        if record.intent_digest != slot.preparation.intent_digest or record.deduplication_scope != PREPARATION_SCOPE or (
            record.deduplication_key != slot.preparation.preparation_id or record.action_kind != SEND_ACTION_KIND
        ):
            raise GmailHostError(HostFailure.STORAGE)
        return record

    def action_status(self, action_id: str | None = None) -> HostActionStatus:
        slot = self._slot(action_id)
        record = self._record(slot)
        return HostActionStatus(
            slot.action_id, record.state.value.upper() if record else "PREPARED",
            len(record.attempts) if record else 0,
            record.reconciliation.probe_count if record else 0,
            record.reconciliation.disposition.value if record else "none",
            record.attempts[-1].status.value.upper() if record and record.attempts else "NONE",
            (record.terminal_outcome.verdict.value.upper() if record and record.terminal_outcome else
             record.attempts[-1].effect_verdict.value.upper()
             if record and record.attempts and record.attempts[-1].effect_verdict else "NONE"),
            record.attempts[-1].attempt_id if record and record.attempts else "NONE",
        )

    def review(self, action_id: str | None = None) -> None:
        self._owner()
        slot = self._slot(action_id)
        review = self.foundation.review(slot.preparation)
        smoke_arguments(review["message"], review["sender"])
        # Offline review is historical protected intent, not current verification.
        self.operator.write_private("Stored preparation; current account is reverified before any approval.\n")
        show_review(self.operator, slot, review)

    def prepare_smoke_test(self, arguments: dict | None = None) -> SmokeTestSlot:
        self._owner()
        if self.mode is not GmailHostMode.PREPARE:
            raise GmailHostError(HostFailure.DISABLED)
        if self.services.storage.load_slot() is not None or self.services.actions.list_actions():
            raise GmailHostError(HostFailure.EXISTING)
        self.operator.require_interactive()
        self._activate(verify_primary=True)
        if arguments is None:
            self.operator.write_private(
                "Prepare one self-addressed plain-text test. Input is hidden and is not logged.\n"
                "Use an obvious unique subject and a short harmless one-line body.\n"
            )
            subject = self.operator.read_private("Subject (hidden): ", maximum=MAX_SMOKE_SUBJECT_BYTES)
            body = self.operator.read_private("Body (hidden): ", maximum=MAX_SMOKE_BODY_BYTES - 2)
            arguments = {"to": [self.primary], "subject": subject, "body": body}
        handle = self.foundation.prepare(smoke_arguments(arguments, self.primary))
        slot = SmokeTestSlot(new_id("act"), handle).validate()
        self.services.storage.save_once(slot)
        self.review()
        return slot

    def _admit_send(self, slot: SmokeTestSlot):
        if not self.mutation_enabled:
            raise GmailHostError(HostFailure.DISABLED)
        if self.services.storage.load_slot() != slot:
            raise GmailHostError(HostFailure.MISSING)
        record = self._record(slot)
        if record is not None:
            if record.state in {ActionState.IN_FLIGHT, ActionState.RECONCILE_REQUIRED}:
                raise GmailHostError(HostFailure.UNCERTAIN)
            # Even definitively failed pre-provider attempts cannot be retried
            # by this first-smoke UX. Only a never-attempted action may continue.
            if record.attempts or record.state is not ActionState.AUTHORIZED:
                raise GmailHostError(HostFailure.ACTION)
        return record

    def _checked_review(self, slot: SmokeTestSlot) -> dict:
        self._owner()
        self._admit_send(slot)
        current = self.services.google.restore()
        if current.state is not ConnectionState.CURRENT or current.credential != self.info:
            raise GmailHostError(HostFailure.CONNECTION)
        self.info.require_current(self.services.clock.now())
        review = self.foundation.review(slot.preparation)
        account = review["account"]
        if self.primary != review["sender"] or account["account_id"] != self.info.account_id or (
            account["connection_id"] != self.info.connection_id
        ):
            raise GmailHostError(HostFailure.CONNECTION)
        smoke_arguments(review["message"], self.primary)
        return review

    def _approval_binding(self, slot: SmokeTestSlot):
        return self.authority.binding(
            action_kind=SEND_ACTION_KIND, intent_digest=slot.preparation.intent_digest,
            mutation_capabilities=tuple(sorted(str(cap) for cap in self.foundation.tool.spec.capabilities)),
            tool_name=self.foundation.tool.spec.name, tool_version=self.foundation.tool.spec.version,
            policy_fingerprint=permission_policy_fingerprint(self.executor.policy),
        )

    def send_smoke_test(self, action_id: str):
        self._owner()
        slot = self._slot(action_id)
        record = self._admit_send(slot)
        self.operator.require_interactive()
        self._activate(verify_primary=True)
        review = self._checked_review(slot)
        if self.executor.policy.evaluate_authenticated_send(self.foundation.tool.spec.capabilities).outcome is Outcome.DENY:
            raise GmailHostError(HostFailure.DISABLED)
        # Reserve a NEW PENDING request for this exact reserved action. This
        # grants nothing. A prior approval/confirmation is never replayed.
        pending = self.authority.authorize(
            self._approval_binding(slot), action_id=slot.action_id,
            description=self.foundation.tool.spec.description,
        )
        if pending.status is not ApprovalAuthorizationStatus.PENDING:
            raise GmailHostError(HostFailure.CONFIRMATION)
        with self.gate.arm(pending.request_id, slot):
            return self.executor.invoke(
                self.foundation.tool.spec.name, review["message"],
                action_context=slot.preparation.context(action_id=slot.action_id if record else None),
                approval_context=ApprovalInvocationContext(pending.request_id),
            )

    def reconcile(self, action_id: str):
        self._owner()
        if self.mode is not GmailHostMode.RECONCILE:
            raise GmailHostError(HostFailure.DISABLED)
        slot = self._slot(action_id)
        record = self._record(slot)
        if record is None or record.state is not ActionState.RECONCILE_REQUIRED:
            raise GmailHostError(HostFailure.ACTION)
        if record.reconciliation.disposition is not ReconciliationDisposition.PROBE_LATER:
            return None  # inspect the categorical human-required disposition
        self.operator.require_interactive()
        self._activate(verify_primary=False)
        claim = self.recovery.claim(action_id, expected_version=record.version)
        return self.recovery.reconcile(claim) if claim is not None else None
