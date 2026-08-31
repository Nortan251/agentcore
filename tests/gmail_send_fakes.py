"""Synthetic Gmail send lifecycle fixtures. Never a real account or network."""

import base64
import json
import os
from pathlib import Path

from agentcore.action_journal import (
    ActionAdapterRegistry, ActionState, JsonFileActionStore, JsonFileEvidenceStore,
    JsonFileProtectedActionPayloadStore, StaticInstallationKeyProvider,
)
from agentcore.approvals import DurableApprovalAuthority, HumanApprovalDecision, JsonFileApprovalStore
from agentcore.credentials import CredentialRuntime, GoogleAccessProfile
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.integrations.gmail.send import (
    GmailProbeCandidate, GmailProbeResponse, GmailSendFoundation,
)
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.recovery.action import ActionRecoveryCoordinator
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from google_oauth_fakes import ACCESS, REFRESH, TestProtector, bundle


ARGS = {"to": ["recipient@sample.invalid"], "cc": ["copy@sample.invalid"],
        "bcc": ["blind@sample.invalid"], "subject": "Private send subject",
        "body": "Private send body\nsecond line"}
SENDER = "sender@sample.invalid"
KEY = StaticInstallationKeyProvider(bytes(range(32)))
PROTECTOR_KEY = base64.urlsafe_b64encode(bytes(range(32)))
SCOPES = " ".join(sorted(GOOGLE_GMAIL_SEND_SCOPES))


class RecordingLogger:
    def __init__(self, events=None):
        self.events = [] if events is None else events

    def bind(self, **fields):
        return self

    def debug(self, event, **fields):
        self.events.append((event, fields))

    info = warning = error = exception = debug


class DecisionGate:
    def __init__(self, decision=HumanApprovalDecision.APPROVE):
        self.decision = decision
        self.prompts = []

    def decide(self, prompt):
        self.prompts.append(prompt)
        return self.decision


class FakeGmailMutation:
    test_only = True

    def __init__(self):
        self.sends = []
        self.probes = []
        self.send_hook = None
        self.probe_hook = None
        self.after_effect = None
        self.before_effect = None
        self.response = {"id": "syntheticmessage1", "threadId": "syntheticthread1", "labelIds": ["SENT"]}
        self.candidates = None
        self.complete = True
        self.lease = None

    def send(self, submission, credential):
        assert credential.reveal() in {ACCESS, "replacement-" + ACCESS}
        self.lease = credential
        if self.before_effect:
            self.before_effect(submission)
        self.sends.append({"identity": submission.identity, "mime": submission.mime_bytes,
                           "semantics": submission.semantics, "marker": submission.message_id})
        if self.after_effect == "timeout":
            raise TimeoutError(ACCESS + REFRESH)
        if self.after_effect == "interrupt":
            raise SystemExit(ACCESS + REFRESH)
        if self.after_effect == "crash":
            os._exit(71)
        if self.send_hook:
            return self.send_hook(submission, credential)
        return dict(self.response)

    def probe(self, request, credential):
        assert credential.reveal() in {ACCESS, "replacement-" + ACCESS}
        assert request.user_id == "me" and request.label_ids == ("SENT",)
        assert request.query == "rfc822msgid:" + request.message_id[1:-1]
        self.probes.append(request.identity)
        if self.probe_hook:
            return self.probe_hook(request, credential)
        candidates = self.candidates
        if candidates is None:
            candidates = (GmailProbeCandidate("syntheticmessage1", "syntheticthread1", True, True, request.semantics),)
        return GmailProbeResponse(candidates, self.complete)


def send_policy(*, level=PermissionLevel.CONFIRM, send=True, probe=True, blocked=()):
    return PermissionPolicy(PermissionConfig(level=level, blocked_capabilities=tuple(blocked)),
                            allow_email_send=send, allow_authenticated_reconciliation=probe)


class Harness:
    def __init__(self, directory: Path, *, gate=None, policy=None, production=False,
                 no_journal=False, no_approval=False, no_evidence=False, as_agent=False,
                 account_subject=None, google_directory=None, model=None, live_transport=None):
        self.directory = directory
        self.google = bundle(google_directory or directory / "google", protector=TestProtector(PROTECTOR_KEY))
        if account_subject is not None:
            self.google.verifier.changes["sub"] = account_subject
        self.google.transport.initial["scope"] = SCOPES
        self.google.transport.refreshed["scope"] = SCOPES
        previous = self.google.service.restore().credential
        self.info = (self.google.service.refresh(previous.reference) if previous is not None else
                     self.google.service.connect(profile=GoogleAccessProfile.GMAIL_SEND))
        self.clock = self.google.clock
        self.runtime = CredentialRuntime(self.google.vault)
        self.payloads = JsonFileProtectedActionPayloadStore(directory / "payloads", KEY)
        self.evidence = JsonFileEvidenceStore(directory / "evidence", KEY)
        self.actions = JsonFileActionStore(directory / "actions", clock=self.clock)
        self.approvals = JsonFileApprovalStore(directory / "approvals")
        self.fake = FakeGmailMutation()
        self.foundation = GmailSendFoundation(self.payloads, sender=SENDER, clock=self.clock,
                                              transport=(live_transport if live_transport is not None else
                                                         None if production else self.fake),
                                              test_only=not production and live_transport is None)
        self.foundation.bind(self.runtime, self.info.reference)
        self.registry = ToolRegistry()
        self.registry.register(self.foundation.tool)
        self.adapters = ActionAdapterRegistry()
        self.adapters.register(self.foundation.tool, self.foundation.adapter, reconciler=self.foundation.reconciler)
        self.gate = gate or DecisionGate()
        self.authority = DurableApprovalAuthority(self.approvals, KEY, principal_ref="host:owner",
                                                 scope_ref="workspace:send", gate=self.gate, clock=self.clock)
        self.logger = RecordingLogger()
        if as_agent:
            from agentcore.config.schema import AgentConfig, ConversationConfig, LoggingConfig
            from agentcore.core.agent import Agent, AgentDependencies
            from agentcore.state.store import JsonFileStateStore
            self.agent = Agent(AgentConfig(
                workspace_dir=str(directory), permissions=PermissionConfig(level=PermissionLevel.CONFIRM),
                conversation=ConversationConfig(tools_enabled=True), logging=LoggingConfig(level="CRITICAL")),
                AgentDependencies(tools=[self.foundation.tool], model=model, action_store=self.actions, action_adapters=self.adapters,
                                  protected_payload_store=self.payloads, evidence_store=self.evidence,
                                  approval_authority=self.authority, credential_runtime=self.runtime,
                                  allow_authenticated_reads=True, state_store=JsonFileStateStore(directory / "states")))
            self.executor, self.registry = self.agent.executor, self.agent.registry
            self.executor._clock = self.clock
        else:
            self.executor = ToolExecutor(
                self.registry, policy or send_policy(), action_store=None if no_journal else self.actions,
                action_adapters=self.adapters, protected_payload_store=self.payloads,
                evidence_store=None if no_evidence else self.evidence,
                approval_authority=None if no_approval else self.authority,
                credential_runtime=self.runtime, clock=self.clock, logger=self.logger,
            )
        self.recovery = ActionRecoveryCoordinator(self.actions, self.executor, clock=self.clock)
        if not no_journal:
            assert not self.recovery.recover_startup().quarantined
        self.preparation = None
        self.closed = False

    def prepare(self, args=None):
        self.preparation = self.foundation.prepare(dict(ARGS if args is None else args))
        return self.preparation

    def invoke(self, args=None, *, preparation=None, action_id=None, approval=None, checkpoint=None, risk=None):
        from agentcore.approvals import ApprovalInvocationContext
        preparation = preparation or self.preparation
        if preparation is None:
            preparation = self.prepare(args)
        return self.executor.invoke(
            "gmail_send_message", dict(ARGS if args is None else args),
            action_context=preparation.context(action_id=action_id, repeat=risk is not None),
            approval_context=ApprovalInvocationContext(approval) if approval else None,
            duplicate_risk_context=risk, checkpoint=checkpoint,
        )

    def ordinary_json(self):
        return "\n".join(path.read_text(encoding="utf-8") for name in ("actions", "approvals")
                         for path in (self.directory / name).glob("*.json"))

    def reconcile(self, action_id, *, checkpoint=None):
        record = self.actions.load(action_id)
        claim = self.recovery.claim(action_id, expected_version=record.version)
        assert claim is not None
        return self.recovery.reconcile(claim, checkpoint=checkpoint)

    def close(self):
        if not self.closed:
            self.runtime.close()
            self.google.vault.close()
            for store in (self.actions, self.approvals, self.payloads, self.evidence):
                store.close()
            self.closed = True
