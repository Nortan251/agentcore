"""The sole validation, permission, journal, and tool-execution boundary.

Read-only tools and statically registered ephemeral controls retain the
original low-latency path. Every journal-required action must publish a
durable intent and exact physical-attempt claim before :meth:`Tool.run` is
entered. ActionJournal records are evidence; they never grant authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import hmac
import threading
import time
import agentcore.errors as framework_errors
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from typing import Any

from agentcore.approvals import (
    ApprovalAuthorization,
    ApprovalAuthorizationStatus,
    ApprovalInvocationContext,
    ApprovalReceipt as DurableApprovalReceipt,
    DurableApprovalAuthority,
)
from agentcore.action_journal import (
    ActionAdapterRegistry,
    ActionAdapterRegistrationError,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionDomainHealth,
    ActionDomainHealthState,
    ActionExecutionPermit,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionOutcome,
    ActionRecord,
    ActionReconciliationContext,
    ActionReconciliationReport,
    ActionReconciliationResult,
    ActionReconciliationStatus,
    ActionRecoveryClaim,
    ActionState,
    ActionStore,
    ActionTransitionProof,
    AuthorityReceipt,
    DuplicateRiskAcknowledgement,
    DuplicateRiskInvocationContext,
    EffectVerdict,
    EvidenceStore,
    ExternalIdempotencyRequest,
    IdempotencyContract,
    NormalizedActionEffect,
    PreparedActionIntent,
    ProtectedActionPayloadStore,
    ProtectedEvidenceView,
    ProtectedPayloadPurpose,
    ReconciliationDisposition,
    ReconciliationMetadata,
    ToolActionAdapter,
    authority_binding_digest,
    deduplication_compatible,
    is_evidence_reference,
    is_protected_payload_reference,
    looks_like_evidence_reference,
    looks_like_protected_payload_reference,
    new_action,
    new_action_attempt_id,
    new_action_transition_id,
    new_duplicate_risk_acknowledgement_id,
    permission_policy_fingerprint,
)
from agentcore.errors import (
    AgentError,
    PermissionDeniedError,
    PolicyLeaseBusyError,
    ToolValidationError,
)
from agentcore.credentials import CredentialError, CredentialFailure, CredentialRuntime
from agentcore.credentials.runtime import CredentialExecution
from agentcore.credentials.action import (
    CredentialActionExecution, CredentialActionIdentity, action_execution, action_snapshot,
)
from agentcore.fs.sensitive import DEFAULT_SENSITIVE_POLICY
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.permissions.levels import READ_ONLY_CAPABILITIES, Capability, PermissionLevel
from agentcore.permissions.policy import Decision, Outcome, PermissionPolicy
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import ExternalReadTool, ToolContext, ToolResult, ToolSpec
from agentcore.tools.authenticated import AuthenticatedReadTool
from agentcore.tools.authenticated_action import AuthenticatedActionTool
from agentcore.tools.builtin.write_file import WriteFileTool, _WriteExecution, _ISSUER
from agentcore.tools.registry import ToolRegistry, ToolRegistryLeaseError
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


_REDACTED = "***"
_SENSITIVE_FAILURE_REASONS = frozenset({"sensitive", "sensitive_target"})
_DUPLICATE_RISK_APPROVAL_ACTION_KIND = "agentcore.duplicate_risk.v1"
_DUPLICATE_RISK_DEDUPLICATION_SCOPE = "agentcore:duplicate-risk:v1"
_DUPLICATE_RISK_POLICY_FINGERPRINT = hashlib.sha256(
    b"agentcore.duplicate-risk.policy-independent.v1"
).hexdigest()
_RESERVED_INTERNAL_DEDUPLICATION_SCOPES = frozenset({
    _DUPLICATE_RISK_DEDUPLICATION_SCOPE,
})


class _DuplicateRiskBlocked(Exception):
    """Internal safe categorical stop before a successor effect seam."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        request_id: str | None = None,
        action_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_message = message
        self.request_id = request_id
        self.action_id = action_id


class _ExternalContractBlocked(Exception):
    """Internal marker for a final provider-contract pre-seam stop."""


class _CredentialActionNotInvoked(Exception):
    """The same live owner proved that the credential provider was not entered."""

    def __init__(self, *, cancellation: CancellationRequested | None = None,
                 interruption: type[BaseException] | None = None) -> None:
        super().__init__("Authenticated action stopped before the provider seam.")
        self.cancellation = cancellation
        self.interruption = interruption


class _CurrentAuthorityBlocked(Exception):
    """Internal safe categorical stop at the final mutation boundary."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        request_id: str | None = None,
        decision: Decision | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.safe_message = message
        self.request_id = request_id
        self.decision = decision


class _FilesystemWriteNotInvoked(_CredentialActionNotInvoked):
    """The live executor proved no filesystem staging boundary was entered."""


def _safe_args_for_log(args: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve legacy read logging while withholding sensitive paths."""
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and DEFAULT_SENSITIVE_POLICY.is_sensitive(value):
            safe[key] = _REDACTED
        else:
            safe[key] = value
    return safe


def _redacted_args(args: Mapping[str, Any]) -> dict[str, str]:
    """Return argument names only; mutation values never cross this boundary."""
    return {str(key): _REDACTED for key in args}


def _redacted_declared_args(
    args: Mapping[str, Any],
    spec: ToolSpec,
) -> dict[str, str]:
    """Redact only trusted schema names; model-supplied unknown keys vanish."""
    declared = {parameter.name for parameter in spec.parameters}
    return {name: _REDACTED for name in sorted(declared.intersection(args))}


@dataclass
class ToolInvocation:
    """Safe result of one call, with optional durable-action correlation."""

    tool: str
    args: dict[str, Any]
    result: ToolResult
    duration_ms: float
    decision: Decision | None = None
    invocation_id: str | None = None
    action_id: str | None = None
    attempt_id: str | None = None
    action_state: str | None = None
    approval_request_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": self.args,
            "result": self.result.to_dict(),
            "duration_ms": round(self.duration_ms, 3),
            "decision": self.decision.to_dict() if self.decision else None,
            "invocation_id": self.invocation_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "action_state": self.action_state,
            "approval_request_id": self.approval_request_id,
        }


class _ExternalReadPolicyChanged(PermissionDeniedError):
    """Internal categorical refusal; never changes ordinary tool errors."""


class ToolExecutor:
    """The only authority permitted to enter ``Tool.run``."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        *,
        workspace_dir: str = ".",
        workspace: Any = None,
        tool_settings: Mapping[str, Mapping[str, Any]] | None = None,
        logger: StructuredLogger | None = None,
        action_store: ActionStore | None = None,
        action_adapters: ActionAdapterRegistry | None = None,
        protected_payload_store: ProtectedActionPayloadStore | None = None,
        evidence_store: EvidenceStore | None = None,
        clock: Clock | None = None,
        id_factory: Callable[[str], str] = new_id,
        action_owner_id: str | None = None,
        action_owner_epoch: int = 1,
        approval_authority: DurableApprovalAuthority | None = None,
        credential_runtime: CredentialRuntime | None = None,
    ) -> None:
        self.registry = registry
        self._policy_owner_pid = os.getpid()
        self._policy_lock = threading.Lock()
        self._policy_generation = 0
        self._credential_policy_leases = 0
        self._external_read_policy_leases = 0
        self._filesystem_write_policy_leases = 0
        self.policy = policy
        self.workspace = workspace
        self.workspace_dir = (str(workspace.root) if workspace is not None
                              else workspace_dir)
        self._tool_settings = dict(tool_settings or {})
        self._log = logger or get_logger("tools.executor")
        self._action_store = action_store
        self._protected_payload_store = protected_payload_store
        self._evidence_store = evidence_store
        self._action_adapters = (
            action_adapters
            if action_adapters is not None
            else ActionAdapterRegistry()
        )
        self._clock = clock or SystemClock()
        self._id_factory = id_factory
        self._action_owner_id = action_owner_id or f"process:{os.getpid()}"
        self._action_owner_epoch = action_owner_epoch
        self._approval_authority = approval_authority
        self._credential_runtime = credential_runtime
        self._credential_authority = object()
        if credential_runtime is not None:
            credential_runtime._attach_executor(self._credential_authority)
        self._action_domain_health_lock = threading.Lock()
        self._action_domain_health = (
            ActionDomainHealth.validation_pending()
            if action_store is not None
            else ActionDomainHealth.healthy()
        )

    def _require_policy_owner(self) -> None:
        if self._policy_owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    def _policy_snapshot(self) -> tuple[PermissionPolicy, int]:
        self._require_policy_owner()
        with self._policy_lock:
            self._require_policy_owner()
            return self._policy, self._policy_generation

    @property
    def policy(self) -> PermissionPolicy:
        return self._policy_snapshot()[0]

    @policy.setter
    def policy(self, policy: PermissionPolicy) -> None:
        """Replace a complete host policy, or fail busy without changing it.

        Replacement from another thread is supported. Once an authenticated
        dispatch or ExternalReadTool leases its evaluated generation, replacement
        fails busy until that dispatch exits. No policy mutex is held across callbacks or
        I/O. In-place changes to published policy authority are unsupported.
        This lease does not change Stage 5 mutation/approval ownership.
        """
        self._require_policy_owner()
        with self._policy_lock:
            self._require_policy_owner()
            if self._credential_policy_leases:
                raise CredentialError(CredentialFailure.BUSY)
            if self._external_read_policy_leases:
                raise PolicyLeaseBusyError("Permission policy is in use by an external read.")
            if self._filesystem_write_policy_leases:
                raise PolicyLeaseBusyError("Permission policy is in use by a filesystem write.")
            self._policy = policy
            self._policy_generation += 1

    @property
    def action_domain_health(self) -> ActionDomainHealth:
        """Return an immutable snapshot of the startup mutation gate."""
        with self._action_domain_health_lock:
            return self._action_domain_health

    def _mark_action_startup_healthy(self, store: ActionStore) -> bool:
        """Accept one exact coordinator/store startup proof.

        This private one-way seam can complete a pending startup.  It can
        never clear a quarantine, and a coordinator for a different store
        cannot authorize this executor's mutation domain.
        """
        with self._action_domain_health_lock:
            if store is not self._action_store:
                return False
            if self._action_domain_health.state is (
                    ActionDomainHealthState.QUARANTINED):
                return False
            self._action_domain_health = ActionDomainHealth.healthy()
            return True

    def quarantine_action_mutations(self, category: str) -> None:
        """Fail future mutations closed after startup journal quarantine."""
        with self._action_domain_health_lock:
            if self._action_domain_health.state is (
                    ActionDomainHealthState.QUARANTINED):
                return
            self._action_domain_health = ActionDomainHealth.quarantined(
                category)

    def validate_action_protected_storage(self, record: ActionRecord) -> None:
        """Validate every protected reference needed by one action.

        Recovery calls this before classifying nonterminal truth.  It performs
        only bounded local reads and grants no execution or reconciliation
        authority.
        """
        self._resolve_action_protected_storage(record)

    def _resolve_action_protected_storage(
        self,
        record: ActionRecord,
    ) -> tuple[Mapping[str, Any] | None, tuple[ProtectedEvidenceView, ...]]:
        if not isinstance(record, ActionRecord):
            raise TypeError("protected action record type is invalid")
        record.validate()
        protected_payload: Mapping[str, Any] | None = None
        if record.intent_ref is not None:
            if not is_protected_payload_reference(record.intent_ref):
                raise ValueError("protected payload reference namespace is invalid")
            if self._protected_payload_store is None:
                raise ValueError("protected payload store is unavailable")
            stored = self._protected_payload_store.load(
                record.intent_ref,
                action_id=record.action_id,
                purpose=ProtectedPayloadPurpose.ACTION_INTENT,
                expected_intent_digest=record.intent_digest,
            )
            protected_payload = stored.payload

        evidence_views: list[ProtectedEvidenceView] = []
        reconciliation = ReconciliationMetadata()
        for transition in record.transitions:
            prior_reconciliation = reconciliation
            for reference in transition.evidence_refs:
                if looks_like_protected_payload_reference(reference):
                    raise ValueError("payload reference used as evidence")
                if not looks_like_evidence_reference(reference):
                    continue
                if not is_evidence_reference(reference):
                    raise ValueError("protected evidence reference is malformed")
                if self._evidence_store is None:
                    raise ValueError("protected evidence store is unavailable")
                if transition.attempt_id is not None:
                    evidence = self._evidence_store.load_attempt_evidence(
                        reference,
                        action_id=record.action_id,
                        attempt_id=transition.attempt_id,
                        intent_digest=record.intent_digest,
                    )
                elif transition.from_state is ActionState.RECONCILE_REQUIRED and (
                        prior_reconciliation.claim_epoch > 0):
                    evidence = self._evidence_store.load_reconciliation_evidence(
                        reference,
                        action_id=record.action_id,
                        claim_epoch=prior_reconciliation.claim_epoch,
                        intent_digest=record.intent_digest,
                    )
                else:
                    raise ValueError("protected evidence lifecycle binding is invalid")
                evidence_views.append(ProtectedEvidenceView(
                    reference=reference,
                    evidence=evidence.evidence,
                ).validate())
            reconciliation = (
                transition.reconciliation
                if transition.to_state is ActionState.RECONCILE_REQUIRED
                else ReconciliationMetadata()
            )
        return protected_payload, tuple(evidence_views)

    def _protected_intent_matches(
        self,
        record: ActionRecord,
        prepared: PreparedActionIntent,
    ) -> bool:
        try:
            if record.intent_ref is None:
                return (
                    prepared.intent_ref is None
                    and prepared.protected_payload is None
                    and not prepared.requires_protected_payload
                    and record.intent_digest == prepared.intent_digest
                )
            if prepared.intent_ref is not None or prepared.protected_payload is None or (
                    self._protected_payload_store is None):
                return False
            stored = self._protected_payload_store.load(
                record.intent_ref,
                action_id=record.action_id,
                purpose=ProtectedPayloadPurpose.ACTION_INTENT,
                expected_intent_digest=record.intent_digest,
            )
            current_digest = self._protected_payload_store.intent_digest(
                prepared.protected_payload,
                purpose=ProtectedPayloadPurpose.ACTION_INTENT,
            )
            return hmac.compare_digest(
                stored.intent_digest, current_digest)
        except Exception:
            return False

    def _context(
        self,
        tool_name: str,
        *,
        action_execution_permit: ActionExecutionPermit | None = None,
        credential_execution: CredentialExecution | CredentialActionExecution | None = None,
        filesystem_write_execution: Any = None,
    ) -> ToolContext:
        return ToolContext(
            config=dict(self._tool_settings.get(tool_name, {})),
            logger=self._log.bind(tool=tool_name),
            workspace_dir=self.workspace_dir,
            workspace=self.workspace,
            action_execution_permit=action_execution_permit,
            credential_execution=credential_execution,
            filesystem_write_execution=filesystem_write_execution,
        )

    def invoke(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        *,
        checkpoint: Callable[[], None] | None = None,
        action_context: ActionInvocationContext | None = None,
        approval_context: ApprovalInvocationContext | None = None,
        duplicate_risk_context: DuplicateRiskInvocationContext | None = None,
        **context_meta: Any,
    ) -> ToolInvocation:
        """Validate, authorize, and invoke through the appropriate boundary."""
        supplied = dict(args or {})
        try:
            tool = self.registry.get(tool_name)
        except ToolRegistryLeaseError:
            return ToolInvocation(
                tool_name,
                {},
                ToolResult.failure(
                    "The trusted tool registration could not be verified.",
                    kind="action_adapter_invalid",
                ),
                0.0,
            )
        tool_spec = tool.spec
        mutation = not tool_spec.read_only
        private = (mutation or type(tool) is AuthenticatedReadTool
                   or "private_arguments" in tool_spec.tags)
        log = (self._log.bind(tool=tool_name) if private
               else self._log.bind(tool=tool_name, **context_meta))
        logged_args = (_redacted_declared_args(supplied, tool_spec) if private
                       else _safe_args_for_log(supplied))
        if private:
            log.info(
                Events.TOOL_INVOKED,
                args=logged_args,
                argument_count=len(supplied),
            )
        else:
            log.info(Events.TOOL_INVOKED, args=logged_args)
        started = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            cleaned = tool.validate_args(supplied)
        except CancellationRequested:
            raise
        except ToolValidationError as exc:
            log.warning(
                Events.TOOL_FAILED,
                reason="validation",
                error=("invalid mutation arguments" if mutation else
                       "invalid authenticated arguments" if private else exc.message),
                details=({} if private else exc.details),
            )
            return ToolInvocation(
                tool_name,
                (_redacted_declared_args(supplied, tool_spec)
                 if private else supplied),
                ToolResult.failure(
                    "Tool arguments were invalid." if private else exc.message,
                    kind="validation",
                    **({} if private else exc.details),
                ),
                elapsed(),
            )
        except Exception:
            if not private:
                raise
            log.warning(
                Events.TOOL_FAILED,
                reason="validation",
                error=("mutation argument validation failed" if mutation else
                       "authenticated argument validation failed"),
            )
            return ToolInvocation(
                tool_name,
                _redacted_declared_args(supplied, tool_spec),
                ToolResult.failure(
                    "Tool arguments could not be validated.",
                    kind="validation",
                ),
                elapsed(),
            )

        # Tool metadata is trusted static registration. A tool whose declared
        # shape changes during validation is not safe to classify or invoke.
        if tool.spec != tool_spec:
            return self._safe_pre_action_failure(
                tool_name, cleaned, elapsed(), log, "tool_shape_changed",
                "The registered tool shape changed during validation.",
            )

        if tool_spec.read_only:
            return self._invoke_fast(
                tool, tool_spec, cleaned, checkpoint, log, elapsed)

        try:
            adapter = self._action_adapters.resolve(tool, tool_spec=tool_spec)
        except ActionAdapterRegistrationError as exc:
            kind = ("action_adapter_version_mismatch"
                    if exc.details.get("reason") == "version_mismatch"
                    else "action_adapter_invalid")
            return self._safe_pre_action_failure(
                tool_name, cleaned, elapsed(), log, kind,
                "The trusted action registration does not match this tool.",
            )
        except Exception:
            return self._safe_pre_action_failure(
                tool_name, cleaned, elapsed(), log, "action_adapter_invalid",
                "The trusted action registration could not be verified.",
            )

        if adapter is None:
            return self._safe_pre_action_failure(
                tool_name, cleaned, elapsed(), log, "action_adapter_missing",
                "A trusted action registration is required.",
            )

        spec = adapter.spec
        if spec.eligibility is ActionJournalEligibility.EPHEMERAL_CONTROL:
            return self._invoke_fast(
                tool, tool_spec, cleaned, checkpoint, log, elapsed)
        if not spec.eligibility.requires_journal:
            return self._safe_pre_action_failure(
                tool_name, cleaned, elapsed(), log, "action_adapter_invalid",
                "The trusted action classification is invalid.",
            )
        action_domain_health = self.action_domain_health
        if action_domain_health.state is not ActionDomainHealthState.HEALTHY:
            startup_pending = action_domain_health.state is (
                ActionDomainHealthState.VALIDATION_PENDING
            )
            return self._safe_pre_action_failure(
                tool_name,
                cleaned,
                elapsed(),
                log,
                ("action_journal_startup_pending" if startup_pending else
                 "action_journal_quarantined"),
                ("Durable action startup validation is still pending."
                 if startup_pending else
                 "Durable action recovery is quarantined for this workspace."),
                quarantine_category=(
                    action_domain_health.error_category
                    or "startup_validation_incomplete"
                ),
            )
        return self._invoke_journaled(
            tool=tool,
            tool_spec=tool_spec,
            cleaned=cleaned,
            adapter=adapter,
            spec=spec,
            checkpoint=checkpoint,
            action_context=action_context,
            approval_context=approval_context,
            duplicate_risk_context=duplicate_risk_context,
            log=log,
            elapsed=elapsed,
        )

    def reconcile_action(
        self,
        claim: ActionRecoveryClaim,
        *,
        checkpoint: Callable[[], None] | None = None,
    ) -> ActionReconciliationReport:
        """Run one exact claimed read-only probe under executor authority."""
        if not isinstance(claim, ActionRecoveryClaim):
            raise TypeError("reconciliation claim type is invalid")
        claim.validate()
        if self.action_domain_health.state is not (
                ActionDomainHealthState.HEALTHY):
            return self._reconciliation_report(
                claim,
                ActionReconciliationStatus.PUBLICATION_UNCERTAIN,
                ActionState.RECONCILE_REQUIRED,
                claim.record_version,
            )
        store = self._action_store
        if store is None:
            return self._reconciliation_report(
                claim,
                ActionReconciliationStatus.ADAPTER_UNAVAILABLE,
                ActionState.RECONCILE_REQUIRED,
                claim.record_version,
            )
        try:
            with store.execution_guard(claim.action_id):
                return self._reconcile_under_action_guard(
                    store,
                    claim,
                    checkpoint=checkpoint,
                )
        except CancellationRequested:
            raise
        except Exception:
            return self._reconciliation_report(
                claim,
                ActionReconciliationStatus.PUBLICATION_UNCERTAIN,
                ActionState.RECONCILE_REQUIRED,
                claim.record_version,
            )

    def _reconcile_under_action_guard(
        self,
        store: ActionStore,
        claim: ActionRecoveryClaim,
        *,
        checkpoint: Callable[[], None] | None,
    ) -> ActionReconciliationReport:
        current = self._load_exact_recovery_claim(store, claim)
        if current is None:
            return self._observed_reconciliation_report(
                store,
                claim,
                ActionReconciliationStatus.STALE_CLAIM,
            )

        try:
            tool = self.registry.get(current.tool_name)
            tool_spec = tool.spec
            adapter = self._action_adapters.resolve(
                tool,
                tool_spec=tool_spec,
            )
            reconciler = self._action_adapters.resolve_reconciler(
                tool,
                tool_spec=tool_spec,
            )
            if adapter is None or reconciler is None or not (
                self._registration_matches_record(
                    current,
                    adapter.spec,
                    tool_spec,
                )
            ):
                raise ValueError("reconciliation registration mismatch")
        except Exception:
            return self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.ADAPTER_UNAVAILABLE,
                probe_entered=False,
            )

        checkpoint_report = self._reconciliation_checkpoint(
            store,
            current,
            claim,
            checkpoint,
            probe_entered=False,
        )
        if checkpoint_report is not None:
            return checkpoint_report

        try:
            probe_capabilities = frozenset(
                Capability(value)
                for value in current.reconciliation_capabilities
            )
            decision = (self.policy.check_authenticated_reconciliation(probe_capabilities)
                        if type(tool) is AuthenticatedActionTool else self.policy.check(
                            probe_capabilities, description=f"Reconcile {current.action_kind}"))
        except Exception:
            return self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.PROBE_FAILED,
                probe_entered=False,
            )
        if not decision.allowed:
            return self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.PROBE_DENIED,
                probe_entered=False,
            )

        checkpoint_report = self._reconciliation_checkpoint(
            store,
            current,
            claim,
            checkpoint,
            probe_entered=False,
        )
        if checkpoint_report is not None:
            return checkpoint_report

        probe_entered = False
        try:
            protected_payload, protected_evidence = (
                self._resolve_action_protected_storage(current)
            )
            context = replace(
                ActionReconciliationContext.from_record(current),
                protected_payload=protected_payload,
                protected_evidence=protected_evidence,
            ).validate()
            if type(tool) is AuthenticatedActionTool:
                with self._authenticated_probe_context(
                    store, current, claim, tool, tool_spec, adapter, reconciler, context, checkpoint,
                ) as probe_context:
                    try:
                        result = reconciler.probe(probe_context)
                    finally:
                        # Read the live permit before its context retires it.
                        # Local resolution/authorization failures consume no
                        # provider quota, including pre-I/O cancellation.
                        probe_entered = probe_context.credential_execution.provider_entered
            else:
                probe_entered = True
                result = reconciler.probe(context)
            if not isinstance(result, ActionReconciliationResult):
                raise TypeError("reconciliation result type is invalid")
            result.validate()
        except CancellationRequested:
            self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.UNRESOLVED,
                probe_entered=probe_entered,
            )
            raise
        except Exception:
            return self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.PROBE_FAILED,
                probe_entered=probe_entered,
            )

        checkpoint_report = self._reconciliation_checkpoint(
            store,
            current,
            claim,
            checkpoint,
            probe_entered=probe_entered,
        )
        if checkpoint_report is not None:
            return checkpoint_report

        try:
            trusted_evidence_refs = self._persist_reconciliation_evidence(
                current, claim, result)
            candidate, status = self._reconciliation_candidate(
                current,
                result,
                trusted_evidence_refs=trusted_evidence_refs,
            )
        except Exception:
            return self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.PROBE_FAILED,
                probe_entered=probe_entered,
            )
        return self._publish_reconciliation_candidate(
            store,
            current,
            candidate,
            claim,
            status,
        )

    def _reconciliation_checkpoint(
        self,
        store: ActionStore,
        current: ActionRecord,
        claim: ActionRecoveryClaim,
        checkpoint: Callable[[], None] | None,
        *,
        probe_entered: bool,
    ) -> ActionReconciliationReport | None:
        if checkpoint is None:
            return None
        try:
            checkpoint()
        except CancellationRequested:
            self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.UNRESOLVED,
                probe_entered=probe_entered,
            )
            raise
        except Exception:
            return self._release_recovery_claim(
                store,
                current,
                claim,
                status=ActionReconciliationStatus.PROBE_FAILED,
                probe_entered=probe_entered,
            )
        return None

    @staticmethod
    def _registration_matches_record(
        record: ActionRecord,
        spec: ActionAdapterSpec,
        tool_spec: ToolSpec,
    ) -> bool:
        return (
            ToolExecutor._execution_registration_matches_record(
                record,
                spec,
                tool_spec,
            )
            and bool(record.reconciliation_capabilities)
        )

    @staticmethod
    def _execution_registration_matches_record(
        record: ActionRecord,
        spec: ActionAdapterSpec,
        tool_spec: ToolSpec,
    ) -> bool:
        """Match the complete immutable registration needed by a mutation."""
        mutation_capabilities = tuple(sorted(
            str(capability)
            for capability in tool_spec.capabilities
            if capability not in READ_ONLY_CAPABILITIES
        ))
        reconciliation_capabilities = tuple(sorted(
            str(capability)
            for capability in spec.reconciliation_capabilities
        ))
        return (
            not tool_spec.read_only
            and record.tool_name == tool_spec.name == spec.tool_name
            and record.tool_version == tool_spec.version == spec.tool_version
            and record.action_kind == spec.action_kind
            and record.eligibility is spec.eligibility
            and record.effect_class is spec.effect_class
            and record.mutation_capabilities == mutation_capabilities
            and record.reconciliation_capabilities == (
                reconciliation_capabilities
            )
        )

    @staticmethod
    def _load_exact_recovery_claim(
        store: ActionStore,
        claim: ActionRecoveryClaim,
    ) -> ActionRecord | None:
        try:
            record = store.load(claim.action_id)
            record.validate()
        except Exception:
            return None
        metadata = record.reconciliation
        if (
            record.version != claim.record_version
            or record.state is not ActionState.RECONCILE_REQUIRED
            or metadata.claim_owner_id != claim.claim_owner_id
            or metadata.claim_epoch != claim.claim_epoch
        ):
            return None
        return record

    def _reconciliation_candidate(
        self,
        current: ActionRecord,
        result: ActionReconciliationResult,
        *,
        trusted_evidence_refs: tuple[str, ...] = (),
    ) -> tuple[ActionRecord, ActionReconciliationStatus]:
        result.validate()
        if not isinstance(trusted_evidence_refs, tuple) or any(
                not is_evidence_reference(reference)
                for reference in trusted_evidence_refs):
            raise ValueError("trusted evidence references are invalid")
        evidence_refs = result.evidence_refs + trusted_evidence_refs
        transition_id = new_action_transition_id(id_factory=self._id_factory)
        at = self._clock.now()
        if result.verdict is EffectVerdict.CONFIRMED_APPLIED:
            outcome = ActionOutcome(
                verdict=result.verdict,
                result_class=result.result_class,
                summary=result.summary,
            )
            return current.transition(
                ActionState.SUCCEEDED,
                transition_id=transition_id,
                at=at,
                effect_verdict=result.verdict,
                evidence_refs=evidence_refs,
                terminal_outcome=outcome,
            ), ActionReconciliationStatus.SETTLED_SUCCEEDED
        if result.verdict is EffectVerdict.CONFIRMED_NOT_APPLIED:
            if result.retry_safe:
                return current.transition(
                    ActionState.AUTHORIZED,
                    transition_id=transition_id,
                    at=at,
                    effect_verdict=result.verdict,
                    proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT,
                    evidence_refs=evidence_refs,
                ), ActionReconciliationStatus.RETRY_AUTHORIZED
            outcome = ActionOutcome(
                verdict=result.verdict,
                result_class=result.result_class,
                summary=result.summary,
            )
            return current.transition(
                ActionState.FAILED,
                transition_id=transition_id,
                at=at,
                effect_verdict=result.verdict,
                evidence_refs=evidence_refs,
                terminal_outcome=outcome,
            ), ActionReconciliationStatus.SETTLED_FAILED
        if result.verdict is EffectVerdict.CONFIRMED_PARTIAL:
            outcome = ActionOutcome(
                verdict=result.verdict,
                result_class=result.result_class,
                summary=result.summary,
                partial_categories=result.partial_categories,
            )
            return current.transition(
                ActionState.FAILED,
                transition_id=transition_id,
                at=at,
                effect_verdict=result.verdict,
                evidence_refs=evidence_refs,
                terminal_outcome=outcome,
            ), ActionReconciliationStatus.SETTLED_FAILED
        metadata = ReconciliationMetadata(
            disposition=result.reconciliation_disposition,
            claim_epoch=current.reconciliation.claim_epoch,
            probe_count=current.reconciliation.probe_count + 1,
        )
        return current.transition(
            ActionState.RECONCILE_REQUIRED,
            transition_id=transition_id,
            at=at,
            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
            evidence_refs=evidence_refs,
            reconciliation=metadata,
        ), ActionReconciliationStatus.UNRESOLVED

    def _release_recovery_claim(
        self,
        store: ActionStore,
        current: ActionRecord,
        claim: ActionRecoveryClaim,
        *,
        status: ActionReconciliationStatus,
        probe_entered: bool,
    ) -> ActionReconciliationReport:
        try:
            metadata = ReconciliationMetadata(
                disposition=ReconciliationDisposition.PROBE_LATER,
                claim_epoch=current.reconciliation.claim_epoch,
                probe_count=(
                    current.reconciliation.probe_count
                    + (1 if probe_entered else 0)
                ),
            )
            candidate = current.transition(
                ActionState.RECONCILE_REQUIRED,
                transition_id=new_action_transition_id(
                    id_factory=self._id_factory
                ),
                at=self._clock.now(),
                effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
                reconciliation=metadata,
            )
        except Exception:
            return self._observed_reconciliation_report(
                store,
                claim,
                ActionReconciliationStatus.PUBLICATION_UNCERTAIN,
            )
        return self._publish_reconciliation_candidate(
            store,
            current,
            candidate,
            claim,
            status,
        )

    def _publish_reconciliation_candidate(
        self,
        store: ActionStore,
        current: ActionRecord,
        candidate: ActionRecord,
        claim: ActionRecoveryClaim,
        success_status: ActionReconciliationStatus,
    ) -> ActionReconciliationReport:
        try:
            store.compare_and_swap(
                current.action_id,
                current.version,
                candidate,
            )
        except Exception:
            pass
        try:
            loaded = store.load(current.action_id)
            loaded.validate()
        except Exception:
            return self._reconciliation_report(
                claim,
                ActionReconciliationStatus.PUBLICATION_UNCERTAIN,
                current.state,
                current.version,
            )
        if loaded == candidate:
            return self._reconciliation_report(
                claim,
                success_status,
                loaded.state,
                loaded.version,
            )
        status = (
            ActionReconciliationStatus.PUBLICATION_UNCERTAIN
            if self._record_owns_recovery_claim(loaded, claim)
            else ActionReconciliationStatus.STALE_CLAIM
        )
        return self._reconciliation_report(
            claim,
            status,
            loaded.state,
            loaded.version,
        )

    @staticmethod
    def _record_owns_recovery_claim(
        record: ActionRecord,
        claim: ActionRecoveryClaim,
    ) -> bool:
        return (
            record.state is ActionState.RECONCILE_REQUIRED
            and record.version == claim.record_version
            and record.reconciliation.claim_owner_id == claim.claim_owner_id
            and record.reconciliation.claim_epoch == claim.claim_epoch
        )

    def _observed_reconciliation_report(
        self,
        store: ActionStore,
        claim: ActionRecoveryClaim,
        status: ActionReconciliationStatus,
    ) -> ActionReconciliationReport:
        try:
            loaded = store.load(claim.action_id)
            loaded.validate()
            state = loaded.state
            version = loaded.version
        except Exception:
            state = ActionState.RECONCILE_REQUIRED
            version = claim.record_version
        return self._reconciliation_report(
            claim,
            status,
            state,
            version,
        )

    @staticmethod
    def _reconciliation_report(
        claim: ActionRecoveryClaim,
        status: ActionReconciliationStatus,
        state: ActionState,
        version: int,
    ) -> ActionReconciliationReport:
        return ActionReconciliationReport(
            action_id=claim.action_id,
            status=status,
            action_state=state,
            record_version=version,
            claim_epoch=claim.claim_epoch,
        ).validate()

    def _invoke_fast(
        self,
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        checkpoint: Callable[[], None] | None,
        log: Any,
        elapsed: Callable[[], float],
    ) -> ToolInvocation:
        """Run one fast-path invocation under its exact registry binding."""
        try:
            with self.registry.execution_lease(tool_spec.name, tool):
                return self._invoke_fast_with_current_binding(
                    tool,
                    tool_spec,
                    cleaned,
                    checkpoint,
                    log,
                    elapsed,
                )
        except CredentialError as exc:
            return self._safe_pre_action_failure(
                tool_spec.name, cleaned, elapsed(), log, exc.reason.value,
                CredentialError(exc.reason).message,
            )
        except ToolRegistryLeaseError:
            log.warning(
                Events.TOOL_FAILED,
                reason="tool_registration_changed",
                error="registered tool changed before execution",
            )
            return ToolInvocation(
                tool_spec.name,
                (_redacted_declared_args(cleaned, tool_spec)
                 if type(tool) is AuthenticatedReadTool or "private_arguments" in tool_spec.tags else cleaned),
                ToolResult.failure(
                    "The registered tool changed before execution.",
                    kind="tool_registration_changed",
                ),
                elapsed(),
            )

    def _invoke_fast_with_current_binding(
        self,
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        checkpoint: Callable[[], None] | None,
        log: Any,
        elapsed: Callable[[], float],
    ) -> ToolInvocation:
        """Validation/checkpoint/policy/checkpoint/run with a live lease."""
        authenticated = type(tool) is AuthenticatedReadTool
        external_read = isinstance(tool, ExternalReadTool)
        private_arguments = "private_arguments" in tool_spec.tags
        safe_args = (_redacted_declared_args(cleaned, tool_spec)
                     if authenticated or private_arguments else cleaned)
        binding = None
        if authenticated:
            tool.validate_contract()
            if self._credential_runtime is None:
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            # Capture account/provider identity before permission or callbacks.
            # Rotation/rebinding during those callbacks cannot substitute it.
            binding = self._credential_runtime._snapshot(tool)
        if checkpoint is not None:
            checkpoint()
        policy, generation = self._policy_snapshot()
        decision = (policy.check(
            tool_spec.capabilities, description=tool_spec.description,
            authenticated_read=True,
        ) if authenticated else policy.check(
            tool_spec.capabilities, description=tool_spec.description))
        log.debug(Events.PERMISSION_CHECKED, **decision.to_dict())
        if not decision.allowed:
            log.warning(Events.TOOL_DENIED, **decision.to_dict())
            denial = PermissionDeniedError(
                decision.reason,
                tool=tool_spec.name,
                capability=(str(decision.capability)
                            if decision.capability else None),
            )
            return ToolInvocation(
                tool_spec.name,
                safe_args,
                ToolResult.failure(
                    denial.message,
                    kind="permission_denied",
                    **denial.details,
                ),
                elapsed(),
                decision,
            )
        if checkpoint is not None:
            checkpoint()
        try:
            if authenticated:
                assert self._credential_runtime is not None and binding is not None
                with self._credential_runtime._execution(
                    tool, binding, cleaned,
                    lambda: self._check_credential_authority(tool, tool_spec, checkpoint),
                    authority=self._credential_authority,
                ) as permit:
                    result = tool.run(cleaned, self._context(
                        tool_spec.name, credential_execution=permit,
                    ))
            else:
                with (self._external_read_policy_lease(tool, tool_spec, policy, generation)
                      if external_read else nullcontext()):
                    result = tool.run(cleaned, self._context(tool_spec.name))
            if not isinstance(result, ToolResult):
                result = ToolResult.success(result)
        except CancellationRequested:
            raise
        except _ExternalReadPolicyChanged:
            result = ToolResult.failure("Current permission authority changed before execution.",
                                        kind="permission_denied")
        except CredentialError as exc:
            result = ToolResult.failure(
                CredentialError(exc.reason).message, kind=exc.reason.value,
            )
        except AgentError as exc:
            if private_arguments and not authenticated:
                log.warning(Events.TOOL_FAILED, reason="private_tool_failed")
                # Preserve trusted framework recovery semantics while omitting
                # messages/details and arbitrary provider class names.
                error_type = type(exc)
                kind = (error_type.__name__ if getattr(framework_errors, error_type.__name__, None) is error_type
                        else "private_tool_failed")
                result = ToolResult.failure("Tool execution failed.", kind=kind, retryable=bool(exc.retryable))
                return ToolInvocation(tool_spec.name, safe_args, result, elapsed(), decision)
            if authenticated:
                return self._safe_pre_action_failure(
                    tool_spec.name, safe_args, elapsed(), log,
                    CredentialFailure.PROVIDER.value,
                    CredentialError(CredentialFailure.PROVIDER).message,
                )
            log.warning(Events.TOOL_FAILED, reason="agent_error",
                        error=exc.message, details=exc.details)
            result = ToolResult.failure(
                exc.message, kind=type(exc).__name__,
                retryable=exc.retryable, **exc.details)
        except Exception as exc:  # noqa: BLE001 - legacy fast-path envelope
            if private_arguments and not authenticated:
                log.warning(Events.TOOL_FAILED, reason="private_tool_failed")
                result = ToolResult.failure("Tool execution failed.", kind="private_tool_failed")
                return ToolInvocation(tool_spec.name, safe_args, result, elapsed(), decision)
            if authenticated:
                # Never stringify provider/runtime exceptions or log traceback.
                return self._safe_pre_action_failure(
                    tool_spec.name, safe_args, elapsed(), log,
                    CredentialFailure.PROVIDER.value,
                    CredentialError(CredentialFailure.PROVIDER).message,
                )
            log.exception(Events.TOOL_FAILED, reason="unhandled_exception",
                          error=str(exc))
            result = ToolResult.failure(
                f"{type(exc).__name__}: {exc}", kind="unhandled_exception")

        duration = elapsed()
        if result.ok:
            log.info(Events.TOOL_SUCCEEDED, duration_ms=round(duration, 3))
        else:
            reason = result.metadata.get("reason") if result.metadata else None
            if private_arguments:
                log.warning(Events.TOOL_FAILED, reason="private_tool_failed",
                            duration_ms=round(duration, 3))
            elif reason in _SENSITIVE_FAILURE_REASONS:
                log.warning(Events.TOOL_FAILED, reason=reason,
                            duration_ms=round(duration, 3))
            else:
                log.warning(Events.TOOL_FAILED, error=result.error,
                            duration_ms=round(duration, 3))
        return ToolInvocation(
            tool_spec.name, safe_args, result, duration, decision)

    @contextmanager
    def _external_read_policy_lease(self, tool, tool_spec, policy, generation) -> Iterator[None]:
        """Fence an evaluated ALLOW across synchronous owned external read I/O.

        Registry execution_lease already pins the exact tool. Completed policy
        replacement, including replacement-back ABA, invalidates this snapshot.
        Reference counts share the setter mutex, which never spans tool/worker
        I/O. The parent retains the lease through worker completion and cleanup.
        """
        if self.registry.get(tool_spec.name) is not tool or tool.spec != tool_spec:
            raise _ExternalReadPolicyChanged("External read binding changed.")
        self._require_policy_owner()
        thread_id = threading.get_ident()
        with self._policy_lock:
            self._require_policy_owner()
            if self._policy is not policy or self._policy_generation != generation:
                raise _ExternalReadPolicyChanged("External read policy changed.")
            self._external_read_policy_leases += 1
        try:
            yield
        finally:
            self._require_policy_owner()
            if threading.get_ident() != thread_id:
                raise _ExternalReadPolicyChanged("External read lease ownership changed.")
            with self._policy_lock:
                self._require_policy_owner()
                self._external_read_policy_leases -= 1

    @contextmanager
    def _check_credential_authority(
        self,
        tool: AuthenticatedReadTool,
        tool_spec: ToolSpec,
        checkpoint: Callable[[], None] | None,
    ) -> Iterator[None]:
        """Lease one currently allowed generation, with no mutex across use.

        The runtime releases the first lease before vault lookup and holds the
        final lease through identity/expiry checks, provider I/O and scrubbing.
        A completed replacement (including ABA) invalidates an earlier check.
        """
        if checkpoint is not None:
            checkpoint()
        if self.registry.get(tool_spec.name) is not tool or tool.spec != tool_spec:
            raise CredentialError(CredentialFailure.BINDING)
        tool.validate_contract()
        policy, generation = self._policy_snapshot()
        decision = policy.check(
            tool_spec.capabilities, description=tool_spec.description,
            authenticated_read=True)
        if not isinstance(decision, Decision) or not decision.allowed:
            raise CredentialError(CredentialFailure.PERMISSION)
        self._require_policy_owner()
        thread_id = threading.get_ident()
        with self._policy_lock:
            self._require_policy_owner()
            if self._policy is not policy or self._policy_generation != generation:
                raise CredentialError(CredentialFailure.PERMISSION)
            self._credential_policy_leases += 1
        try:
            yield
        finally:
            self._require_policy_owner()
            if thread_id != threading.get_ident():
                raise CredentialError(CredentialFailure.OWNERSHIP)
            with self._policy_lock:
                self._require_policy_owner()
                self._credential_policy_leases -= 1

    @contextmanager
    def _journal_credential_authority(self, tool, tool_spec, checkpoint, validate) -> Iterator[None]:
        """Pin current policy through the actual credential provider seam.

        Validation includes exact current registration and the live consumed
        approval (send) or exact recovery claim (probe). No metadata mutex
        spans those callbacks, the credential provider or result scrubbing.
        """
        self._require_policy_owner()
        if checkpoint is not None:
            checkpoint()
        policy, generation = self._policy_snapshot()
        if self.registry.get(tool_spec.name) is not tool or tool.spec != tool_spec:
            raise CredentialError(CredentialFailure.BINDING)
        tool.validate_contract()
        validate()
        thread_id = threading.get_ident()
        with self._policy_lock:
            self._require_policy_owner()
            if self._policy is not policy or self._policy_generation != generation:
                raise CredentialError(CredentialFailure.PERMISSION)
            self._credential_policy_leases += 1
        try:
            yield
        finally:
            self._require_policy_owner()
            if thread_id != threading.get_ident():
                raise CredentialError(CredentialFailure.OWNERSHIP)
            with self._policy_lock:
                self._require_policy_owner()
                self._credential_policy_leases -= 1

    @contextmanager
    def _authenticated_probe_context(self, store, current, claim, tool, tool_spec,
                                     adapter, reconciler, context, checkpoint) -> Iterator[ActionReconciliationContext]:
        runtime = self._credential_runtime
        if runtime is None or context.protected_payload is None or not current.attempts:
            raise CredentialError(CredentialFailure.UNAVAILABLE)

        def validate():
            if self._load_exact_recovery_claim(store, claim) != current or (
                    self._action_adapters.resolve(tool, tool_spec=tool_spec) is not adapter
                    or self._action_adapters.resolve_reconciler(tool, tool_spec=tool_spec) is not reconciler
                    or not self._registration_matches_record(current, adapter.spec, tool_spec)):
                raise CredentialError(CredentialFailure.BINDING)
            decision = self.policy.check_authenticated_reconciliation(
                frozenset(Capability(cap) for cap in current.reconciliation_capabilities))
            if not isinstance(decision, Decision) or not decision.allowed:
                raise CredentialError(CredentialFailure.PERMISSION)

        with self.registry.execution_lease(tool_spec.name, tool):
            binding = action_snapshot(runtime, tool)
            identity = CredentialActionIdentity(current.action_id, current.attempts[-1].attempt_id,
                                                current.intent_digest, claim.claim_epoch)
            with action_execution(
                runtime, tool, binding, {}, context.protected_payload, identity,
                lambda: self._journal_credential_authority(tool, tool_spec, checkpoint, validate),
                authority=self._credential_authority, reconciler=reconciler,
            ) as permit:
                yield replace(context, credential_execution=permit)

    def _run_authenticated_action(self, *, store, claimed, tool, tool_spec, cleaned,
                                  adapter, spec, prepared, current_approval_receipt, checkpoint):
        runtime = self._credential_runtime
        entered = False

        def validate():
            # Credential resolution can run callbacks or take time. The exact
            # durable claim must still exist after that interval; the earlier
            # claimed snapshot alone is not current execution authority.
            current = store.load(claimed.action_id)
            current.validate()
            if current != claimed or current.state is not ActionState.IN_FLIGHT:
                raise CredentialError(CredentialFailure.AUTHORITY)
            self._validate_current_execution_authority(
                record=claimed, tool=tool, tool_spec=tool_spec, adapter=adapter,
                spec=spec, prepared=prepared, current_approval_receipt=current_approval_receipt)
            # Sender verification is a read under current authority, not a
            # consequence of possessing a send grant or historical approval.
            if binding.preflight_capabilities:
                decision = self.policy.check(binding.preflight_capabilities)
                if not isinstance(decision, Decision) or not decision.allowed:
                    raise CredentialError(CredentialFailure.PERMISSION)
            if claimed.duplicate_risk_acknowledgement is not None and len(claimed.attempts) == 1:
                self._validate_current_duplicate_risk_permission(
                    record=claimed, tool_spec=tool_spec, prepared=prepared,
                    current_approval_receipt=current_approval_receipt)
                self._validate_duplicate_risk_record(store=store, record=claimed, tool=tool,
                                                     tool_spec=tool_spec, spec=spec, prepared=prepared)

        try:
            if runtime is None:
                raise CredentialError(CredentialFailure.UNAVAILABLE)
            payload, _ = self._resolve_action_protected_storage(claimed)
            if payload is None:
                raise CredentialError(CredentialFailure.BINDING)
            binding = action_snapshot(runtime, tool)
            identity = CredentialActionIdentity(claimed.action_id, claimed.attempts[-1].attempt_id,
                                                claimed.intent_digest)
            with action_execution(
                runtime, tool, binding, cleaned, payload, identity,
                lambda: self._journal_credential_authority(tool, tool_spec, checkpoint, validate),
                authority=self._credential_authority,
            ) as permit:
                try:
                    result = tool.run(cleaned, self._context(tool_spec.name, credential_execution=permit))
                finally:
                    entered = permit.provider_entered
                normalized = adapter.normalize_effect(result)
                if not isinstance(normalized, NormalizedActionEffect):
                    raise TypeError("action normalization type is invalid")
                return normalized.validate(spec), None
        except CancellationRequested as exc:
            if entered:
                return self._unknown_effect(spec, "gmail.send.cancelled"), exc
            raise _CredentialActionNotInvoked(cancellation=exc) from None
        except (KeyboardInterrupt, SystemExit) as exc:
            if entered:
                return self._unknown_effect(spec, "gmail.send.interrupted"), type(exc)()
            raise _CredentialActionNotInvoked(interruption=type(exc)) from None
        except Exception:
            if entered:
                return self._unknown_effect(spec, "gmail.send.uncertain"), None
        # No provider exception or secret-bearing context leaves this method.
        raise _CredentialActionNotInvoked()

    def _run_filesystem_write(self, *, store, claimed, tool, tool_spec, cleaned,
                              adapter, spec, prepared, current_approval_receipt, checkpoint):
        execution = None

        def validate():
            if checkpoint is not None:
                checkpoint()
            if store.load(claimed.action_id) != claimed or self.workspace is not tool._guard:
                raise ValueError("filesystem write claim or workspace changed")
            self._validate_current_execution_authority(
                record=claimed, tool=tool, tool_spec=tool_spec, adapter=adapter,
                spec=spec, prepared=prepared, current_approval_receipt=current_approval_receipt)

        @contextmanager
        def authorize():
            policy, generation = self._policy_snapshot()
            tool.validate_contract()
            validate()
            with self._policy_lock:
                self._require_policy_owner()
                if self._policy is not policy or self._policy_generation != generation:
                    raise ValueError("filesystem write policy changed")
                self._filesystem_write_policy_leases += 1
            try:
                yield
            finally:
                self._require_policy_owner()
                with self._policy_lock:
                    self._filesystem_write_policy_leases -= 1

        try:
            payload, _ = self._resolve_action_protected_storage(claimed)
            if payload is None or not self._protected_intent_matches(claimed, prepared):
                raise ValueError("verified filesystem intent unavailable")
            execution = _WriteExecution(_ISSUER, tool, payload, authorize, validate)
            result = tool.run(cleaned, self._context(tool_spec.name, filesystem_write_execution=execution))
            return adapter.normalize_effect(result).validate(spec), None
        except (CancellationRequested, KeyboardInterrupt, SystemExit) as exc:
            if execution is not None and execution.entered:
                return self._unknown_effect(spec, "filesystem.interrupted"), exc
            if isinstance(exc, CancellationRequested):
                raise _FilesystemWriteNotInvoked(cancellation=exc) from None
            raise _FilesystemWriteNotInvoked(interruption=type(exc)) from None
        except Exception:
            if execution is not None and execution.entered:
                return self._unknown_effect(spec, "filesystem.uncertain"), None
            raise _FilesystemWriteNotInvoked() from None
        finally:
            if execution is not None:
                execution.active = False

    def _current_policy_decision(
        self,
        tool_spec: ToolSpec,
        *,
        description: str,
    ) -> tuple[Decision, bool]:
        """Return current aggregate policy truth without boolean approval."""
        filesystem_write = (tool_spec.name == "write_file" and
                            type(self.registry.get(tool_spec.name)) is WriteFileTool)
        if filesystem_write and self.policy.level in {PermissionLevel.OBSERVE, PermissionLevel.PROPOSE}:
            return Decision(Outcome.DENY, "filesystem writes require mutation authority",
                            capability=Capability.FS_WRITE), False
        authenticated_send_caps = frozenset({Capability.EMAIL_SEND, Capability.NET_WRITE, Capability.CREDENTIAL_ACCESS})
        current_tool = self.registry.get(tool_spec.name) if tool_spec.capabilities == authenticated_send_caps else None
        if type(current_tool) is AuthenticatedActionTool:
            if current_tool.spec != tool_spec:
                raise CredentialError(CredentialFailure.BINDING)
            current_tool.validate_contract()
            decision = self.policy.evaluate_authenticated_send(tool_spec.capabilities)
            # Durable approval is intrinsic to this composition, even for a
            # custom host policy. ALLOW is not a valid send-context result.
            if not isinstance(decision, Decision) or (
                    decision.outcome is not Outcome.DENY
                    and decision.outcome is not Outcome.REQUIRE_APPROVAL):
                return Decision(Outcome.DENY, "authenticated sends require durable approval",
                                capability=Capability.EMAIL_SEND), False
            return decision, decision.outcome is Outcome.REQUIRE_APPROVAL
        if type(self.policy).check is not PermissionPolicy.check:
            decision = self.policy.check(
                tool_spec.capabilities, description=description)
            if not isinstance(decision, Decision):
                raise TypeError("permission policy returned an invalid decision")
            if filesystem_write and self.policy.level is PermissionLevel.CONFIRM and decision.allowed:
                return Decision(Outcome.REQUIRE_APPROVAL, "filesystem write requires durable approval",
                                capability=Capability.FS_WRITE), True
            return decision, decision.outcome is Outcome.REQUIRE_APPROVAL
        evaluation = self.policy.evaluate_all(tool_spec.capabilities)
        if evaluation.outcome is Outcome.DENY:
            assert evaluation.denied is not None
            return evaluation.denied, False
        if evaluation.outcome is Outcome.REQUIRE_APPROVAL:
            capability = evaluation.approval_capabilities[0]
            return Decision(
                Outcome.REQUIRE_APPROVAL,
                "explicit durable approval is required",
                capability=capability,
                required_level=self.policy.required_level(capability),
            ), True
        if filesystem_write and self.policy.level is PermissionLevel.CONFIRM:
            return Decision(Outcome.REQUIRE_APPROVAL, "filesystem write requires durable approval",
                            capability=Capability.FS_WRITE), True
        return Decision(Outcome.ALLOW, "all capabilities permitted"), False

    def _authorize_approval(
        self,
        *,
        action_kind: str,
        intent_digest: str,
        mutation_capabilities: tuple[str, ...],
        tool_name: str,
        tool_version: str,
        description: str,
        action_id: str | None,
        approval_context: ApprovalInvocationContext | None,
    ) -> ApprovalAuthorization:
        authority = self._approval_authority
        if authority is None:
            raise ValueError("durable approval authority is unavailable")
        binding = authority.binding(
            action_kind=action_kind,
            intent_digest=intent_digest,
            mutation_capabilities=mutation_capabilities,
            tool_name=tool_name,
            tool_version=tool_version,
            policy_fingerprint=permission_policy_fingerprint(self.policy),
        )
        return authority.authorize(
            binding,
            description=description,
            action_id=action_id,
            request_id=(approval_context.request_id
                        if approval_context is not None else None),
        )

    def _validate_consumed_current_approval(
        self,
        *,
        record: ActionRecord,
        receipt: DurableApprovalReceipt,
    ) -> DurableApprovalReceipt:
        """Reauthenticate one exact current-invocation approval receipt."""
        authority = self._approval_authority
        if authority is None:
            raise ValueError("durable approval authority is unavailable")
        if not isinstance(receipt, DurableApprovalReceipt):
            raise TypeError("durable approval receipt type is invalid")
        receipt.validate()
        binding = authority.binding(
            action_kind=record.action_kind,
            intent_digest=record.intent_digest,
            mutation_capabilities=record.mutation_capabilities,
            tool_name=record.tool_name,
            tool_version=record.tool_version,
            policy_fingerprint=permission_policy_fingerprint(self.policy),
        )
        exact = authority.validate_consumed(
            binding,
            request_id=receipt.request_id,
            action_id=record.action_id,
        )
        if exact != receipt:
            raise ValueError("durable approval receipt changed")
        return exact

    def _validate_current_execution_authority(
        self,
        *,
        record: ActionRecord,
        tool: Any,
        tool_spec: ToolSpec,
        adapter: ToolActionAdapter,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        current_approval_receipt: DurableApprovalReceipt | None,
    ) -> Decision:
        """Fail closed on current registration, health, policy, and approval.

        This check runs after the exact durable attempt reload and the final
        checkpoint.  Journal history is deliberately not consulted as a grant:
        a policy that currently requires approval needs the exact consumed
        receipt carried by this invocation.
        """
        health = self.action_domain_health
        if health.state is not ActionDomainHealthState.HEALTHY:
            startup_pending = health.state is (
                ActionDomainHealthState.VALIDATION_PENDING
            )
            raise _CurrentAuthorityBlocked(
                ("action_journal_startup_pending" if startup_pending else
                 "action_journal_quarantined"),
                ("Durable action startup validation is still pending."
                 if startup_pending else
                 "Durable action recovery is quarantined for this workspace."),
            )

        try:
            current_tool = self.registry.get(tool_spec.name)
            if current_tool is not tool:
                raise ValueError("current tool registration changed")
            current_tool_spec = current_tool.spec
            current_adapter = self._action_adapters.resolve(
                current_tool,
                tool_spec=current_tool_spec,
            )
            if current_tool_spec != tool_spec or current_adapter is not adapter or (
                    current_adapter.spec != spec) or not (
                        self._execution_registration_matches_record(
                            record,
                            current_adapter.spec,
                            current_tool_spec,
                        )
                    ):
                raise ValueError("current action registration changed")
        except Exception as exc:
            external = spec.effect_class is (
                ActionEffectClass.EXTERNALLY_IDEMPOTENT
            )
            raise _CurrentAuthorityBlocked(
                ("external_idempotency_contract_unavailable" if external else
                 "action_adapter_invalid"),
                ("The external idempotency registration changed before "
                 "execution." if external else
                 "The trusted action registration changed before execution."),
            ) from exc

        try:
            decision, approval_required = self._current_policy_decision(
                tool_spec,
                description=(prepared.approval_description
                             or tool_spec.description),
            )
        except Exception as exc:
            raise _CurrentAuthorityBlocked(
                "permission_policy_unavailable",
                "Current mutation permission could not be verified.",
            ) from exc
        if decision.outcome is Outcome.DENY:
            raise _CurrentAuthorityBlocked(
                "permission_denied",
                "Current permission policy denies this action.",
                decision=decision,
            )
        if not approval_required:
            return decision
        if current_approval_receipt is None:
            raise _CurrentAuthorityBlocked(
                "approval_policy_changed",
                "Current permission policy requires a fresh approval.",
                decision=decision,
            )
        try:
            self._validate_consumed_current_approval(
                record=record,
                receipt=current_approval_receipt,
            )
        except AgentError as exc:
            expired = exc.details.get("reason") == "expired"
            raise _CurrentAuthorityBlocked(
                "approval_expired" if expired else "approval_policy_changed",
                ("The one-shot approval expired before execution."
                 if expired else
                 "Current permission policy requires a fresh approval."),
                request_id=current_approval_receipt.request_id,
                decision=decision,
            ) from exc
        except Exception as exc:
            raise _CurrentAuthorityBlocked(
                "approval_policy_changed",
                "Current permission policy requires a fresh approval.",
                request_id=current_approval_receipt.request_id,
                decision=decision,
            ) from exc
        return Decision(
            Outcome.ALLOW,
            "authorized by current durable one-shot approval",
            capability=decision.capability,
            required_level=decision.required_level,
        )

    def _validate_current_duplicate_risk_permission(
        self,
        *,
        record: ActionRecord,
        tool_spec: ToolSpec,
        prepared: PreparedActionIntent,
        current_approval_receipt: DurableApprovalReceipt | None = None,
    ) -> Decision:
        """Recheck current mutation authority for a successor first attempt.

        The duplicate-risk receipt grants no capability. If current policy now
        requires ordinary approval, the exact separately consumed approval on
        the successor is reauthenticated without prompting or consuming it a
        second time.
        """
        decision, approval_required = self._current_policy_decision(
            tool_spec,
            description=(prepared.approval_description
                         or tool_spec.description),
        )
        acknowledgement = record.duplicate_risk_acknowledgement
        request_id = (
            acknowledgement.approval_request_id
            if acknowledgement is not None else None
        )
        if decision.outcome is Outcome.DENY:
            raise _DuplicateRiskBlocked(
                "permission_denied",
                "Current permission policy denies this new action.",
                request_id=request_id,
                action_id=record.action_id,
            )
        if not approval_required:
            return decision

        normal_request_id = (
            current_approval_receipt.request_id
            if current_approval_receipt is not None
            else record.authority.approval_request_id
        )
        authority = self._approval_authority
        if normal_request_id is None or authority is None:
            raise _DuplicateRiskBlocked(
                "approval_policy_changed",
                "Current permission policy requires a separate ordinary "
                "approval for this new action.",
                request_id=request_id,
                action_id=record.action_id,
            )
        try:
            if current_approval_receipt is not None:
                self._validate_consumed_current_approval(
                    record=record,
                    receipt=current_approval_receipt,
                )
            else:
                binding = authority.binding(
                    action_kind=record.action_kind,
                    intent_digest=record.intent_digest,
                    mutation_capabilities=record.mutation_capabilities,
                    tool_name=record.tool_name,
                    tool_version=record.tool_version,
                    policy_fingerprint=permission_policy_fingerprint(self.policy),
                )
                receipt = authority.validate_consumed(
                    binding,
                    request_id=normal_request_id,
                    action_id=record.action_id,
                )
                if not (
                    hmac.compare_digest(
                        receipt.binding_digest,
                        record.authority.binding_digest,
                    )
                    and receipt.scope_ref == record.authority.scope_ref
                    and receipt.approved_at == record.authority.authorized_at
                    and receipt.expires_at == record.authority.expires_at
                ):
                    raise ValueError("ordinary approval receipt changed")
        except AgentError as exc:
            reason = exc.details.get("reason")
            raise _DuplicateRiskBlocked(
                ("approval_expired" if reason == "expired"
                 else "approval_policy_changed"),
                ("The ordinary approval expired before execution."
                 if reason == "expired" else
                 "Current permission policy requires a new ordinary approval "
                 "for this action."),
                request_id=normal_request_id,
                action_id=record.action_id,
            ) from exc
        except Exception as exc:
            raise _DuplicateRiskBlocked(
                "approval_policy_changed",
                "Current permission policy requires a new ordinary approval "
                "for this action.",
                request_id=normal_request_id,
                action_id=record.action_id,
            ) from exc
        return Decision(
            Outcome.ALLOW,
            "authorized by current durable one-shot approval",
            capability=decision.capability,
            required_level=decision.required_level,
        )

    def _authorize_duplicate_risk(
        self,
        *,
        store: ActionStore,
        tool: Any,
        tool_spec: ToolSpec,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        public_intent_digest: str,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        context: DuplicateRiskInvocationContext,
        successor_action_id: str,
    ) -> DuplicateRiskAcknowledgement:
        """Obtain one separately bound human duplicate-risk decision."""
        predecessor = self._load_duplicate_risk_predecessor(
            store=store,
            tool=tool,
            tool_spec=tool_spec,
            spec=spec,
            prepared=prepared,
            successor_intent_digest=public_intent_digest,
            successor_target_category=prepared.target_category,
            successor_target_fingerprint=prepared.target_fingerprint,
            mutation_capabilities=mutation_capabilities,
            reconciliation_capabilities=reconciliation_capabilities,
            predecessor_action_id=context.predecessor_action_id,
        )
        authority = self._approval_authority
        if authority is None:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_acknowledgement_required",
                "The earlier action is unresolved. A new action requires "
                "explicit human acknowledgement that it may duplicate the "
                "earlier effect.",
                action_id=successor_action_id,
            )
        binding = self._duplicate_risk_approval_binding(
            authority=authority,
            predecessor=predecessor,
            successor_action_id=successor_action_id,
            action_kind=spec.action_kind,
            intent_digest=public_intent_digest,
            target_category=prepared.target_category,
            target_fingerprint=prepared.target_fingerprint,
            effect_class=spec.effect_class,
            mutation_capabilities=mutation_capabilities,
            reconciliation_capabilities=reconciliation_capabilities,
            tool_name=tool_spec.name,
            tool_version=tool_spec.version,
        )
        try:
            authorization = authority.authorize(
                binding,
                description=(
                    "The earlier action is still unresolved. Approve a new "
                    "action that may duplicate its effect."
                ),
                action_id=successor_action_id,
                request_id=context.approval_request_id,
            )
        except AgentError as exc:
            reason = exc.details.get("reason")
            kind = (
                "duplicate_risk_acknowledgement_expired"
                if reason == "expired"
                else "duplicate_risk_binding_invalid"
            )
            raise _DuplicateRiskBlocked(
                kind,
                ("The duplicate-risk acknowledgement expired."
                 if reason == "expired" else
                 "The duplicate-risk approval does not match this exact new "
                 "action."),
                request_id=context.approval_request_id,
                action_id=successor_action_id,
            ) from exc
        except Exception as exc:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The duplicate-risk approval does not match this exact new "
                "action.",
                request_id=context.approval_request_id,
                action_id=successor_action_id,
            ) from exc

        receipt: DurableApprovalReceipt | None = None
        if authorization.authorized:
            receipt = authorization.receipt
        elif authorization.status is ApprovalAuthorizationStatus.CONSUMED:
            try:
                receipt = authority.validate_consumed(
                    binding,
                    request_id=authorization.request_id,
                    action_id=successor_action_id,
                )
            except AgentError as exc:
                reason = exc.details.get("reason")
                raise _DuplicateRiskBlocked(
                    ("duplicate_risk_acknowledgement_expired"
                     if reason == "expired"
                     else "duplicate_risk_binding_invalid"),
                    ("The duplicate-risk acknowledgement expired."
                     if reason == "expired" else
                     "The consumed duplicate-risk approval is not valid for "
                     "this exact new action."),
                    request_id=authorization.request_id,
                    action_id=successor_action_id,
                ) from exc
        else:
            kinds = {
                ApprovalAuthorizationStatus.PENDING: (
                    "duplicate_risk_acknowledgement_required"),
                ApprovalAuthorizationStatus.DENIED: (
                    "duplicate_risk_acknowledgement_denied"),
                ApprovalAuthorizationStatus.EXPIRED: (
                    "duplicate_risk_acknowledgement_expired"),
            }
            messages = {
                ApprovalAuthorizationStatus.PENDING: (
                    "The earlier action is unresolved. A new action requires "
                    "explicit human acknowledgement that it may duplicate "
                    "the earlier effect."),
                ApprovalAuthorizationStatus.DENIED: (
                    "Human duplicate-risk acknowledgement was denied."),
                ApprovalAuthorizationStatus.EXPIRED: (
                    "The duplicate-risk acknowledgement expired."),
            }
            raise _DuplicateRiskBlocked(
                kinds.get(
                    authorization.status,
                    "duplicate_risk_binding_invalid",
                ),
                messages.get(
                    authorization.status,
                    "The duplicate-risk approval is invalid.",
                ),
                request_id=authorization.request_id,
                action_id=successor_action_id,
            )
        if receipt is None:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The duplicate-risk approval lacks an authenticated receipt.",
                request_id=authorization.request_id,
                action_id=successor_action_id,
            )
        acknowledgement = self._duplicate_risk_acknowledgement(
            predecessor.action_id, receipt)
        self._validate_duplicate_risk_before_create(
            store=store,
            tool=tool,
            tool_spec=tool_spec,
            spec=spec,
            prepared=prepared,
            public_intent_digest=public_intent_digest,
            mutation_capabilities=mutation_capabilities,
            reconciliation_capabilities=reconciliation_capabilities,
            successor_action_id=successor_action_id,
            acknowledgement=acknowledgement,
        )
        return acknowledgement

    def _load_duplicate_risk_predecessor(
        self,
        *,
        store: ActionStore,
        tool: Any,
        tool_spec: ToolSpec,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        successor_intent_digest: str,
        successor_target_category: str,
        successor_target_fingerprint: str | None,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        predecessor_action_id: str,
    ) -> ActionRecord:
        try:
            predecessor = store.load(predecessor_action_id)
            predecessor.validate()
        except Exception as exc:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_predecessor_changed",
                "The unresolved predecessor could not be verified.",
            ) from exc
        if predecessor.state is not ActionState.RECONCILE_REQUIRED or not any(
                attempt.effect_verdict is EffectVerdict.OUTCOME_UNKNOWN
                for attempt in predecessor.attempts):
            raise _DuplicateRiskBlocked(
                "duplicate_risk_predecessor_changed",
                "The earlier action is no longer in the acknowledged "
                "unresolved state.",
            )
        if predecessor.reconciliation.claim_owner_id is not None:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_predecessor_changed",
                "The earlier action is currently being reconciled.",
            )
        if predecessor.reconciliation.disposition is (
                ReconciliationDisposition.PROBE_LATER):
            raise _DuplicateRiskBlocked(
                "duplicate_risk_reconciliation_available",
                "The earlier action still has an authoritative reconciliation "
                "path; resolve that truth before creating a risky repeat.",
            )
        equivalent_intent = (
            hmac.compare_digest(predecessor.intent_digest, successor_intent_digest)
            and self._protected_intent_matches(predecessor, prepared)
        )
        registered = self._action_adapters.resolve(tool, tool_spec=tool_spec)
        compare_repeat = getattr(registered, "repeat_intent_equivalent", None)
        if compare_repeat is not None:
            try:
                old_payload, _ = self._resolve_action_protected_storage(predecessor)
                if old_payload is None or prepared.protected_payload is None:
                    raise ValueError("protected repeat comparison lacks exact payloads")
                equivalent_intent = compare_repeat(old_payload, prepared.protected_payload) is True
            except Exception:
                equivalent_intent = False
        same_intent = (
            predecessor.action_kind == spec.action_kind
            and predecessor.tool_name == tool_spec.name
            and predecessor.tool_version == tool_spec.version
            and predecessor.eligibility is spec.eligibility
            and predecessor.effect_class is spec.effect_class
            and predecessor.mutation_capabilities == mutation_capabilities
            and predecessor.reconciliation_capabilities == (
                reconciliation_capabilities)
            and equivalent_intent
            and predecessor.target_category == successor_target_category
            and predecessor.target_fingerprint == (
                successor_target_fingerprint)
        )
        if not same_intent:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The requested new action is not equivalent to the unresolved "
                "predecessor.",
            )
        if predecessor.effect_class is (
                ActionEffectClass.EXTERNALLY_IDEMPOTENT):
            try:
                self._validate_external_contract_for_record(
                    tool, tool_spec, spec, predecessor)
            except Exception:
                pass
            else:
                raise _DuplicateRiskBlocked(
                    "duplicate_risk_same_action_retry_available",
                    "The earlier action still has a live provider-backed "
                    "same-action retry guarantee.",
                )
        return predecessor

    def _duplicate_risk_approval_binding(
        self,
        *,
        authority: DurableApprovalAuthority,
        predecessor: ActionRecord,
        successor_action_id: str,
        action_kind: str,
        intent_digest: str,
        target_category: str,
        target_fingerprint: str | None,
        effect_class: ActionEffectClass | None,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        tool_name: str,
        tool_version: str,
    ):
        if effect_class is None:
            raise ValueError("duplicate-risk successor lacks an effect class")
        payload = {
            "domain": "agentcore.duplicate-risk.intent.v1",
            "predecessor": {
                "action_id": predecessor.action_id,
                "version": predecessor.version,
                "state": predecessor.state.value,
                "attempts": [
                    {
                        "attempt_id": attempt.attempt_id,
                        "status": attempt.status.value,
                        "effect_verdict": (
                            attempt.effect_verdict.value
                            if attempt.effect_verdict is not None else None
                        ),
                    }
                    for attempt in predecessor.attempts
                ],
                "reconciliation": {
                    "disposition": (
                        predecessor.reconciliation.disposition.value),
                    "claim_epoch": predecessor.reconciliation.claim_epoch,
                    "probe_count": predecessor.reconciliation.probe_count,
                },
            },
            "successor": {
                "action_id": successor_action_id,
                "action_kind": action_kind,
                "intent_digest": intent_digest,
                "target_category": target_category,
                "target_fingerprint": target_fingerprint,
                "effect_class": effect_class.value,
                "mutation_capabilities": list(mutation_capabilities),
                "reconciliation_capabilities": list(
                    reconciliation_capabilities),
                "tool_name": tool_name,
                "tool_version": tool_version,
            },
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        # This inner digest is canonical input only. The durable authority's
        # outer binding digest is the installation-keyed acknowledgement proof.
        risk_intent_digest = hashlib.sha256(
            b"agentcore.duplicate-risk.intent.v1\x00" + encoded
        ).hexdigest()
        return authority.binding(
            action_kind=_DUPLICATE_RISK_APPROVAL_ACTION_KIND,
            intent_digest=risk_intent_digest,
            mutation_capabilities=mutation_capabilities,
            tool_name=tool_name,
            tool_version=tool_version,
            policy_fingerprint=_DUPLICATE_RISK_POLICY_FINGERPRINT,
        )

    @staticmethod
    def _duplicate_risk_acknowledgement(
        predecessor_action_id: str,
        receipt: DurableApprovalReceipt,
    ) -> DuplicateRiskAcknowledgement:
        receipt.validate()
        receipt_suffix = receipt.receipt_id.removeprefix("aprc_")
        acknowledgement_id = new_duplicate_risk_acknowledgement_id(
            id_factory=lambda prefix: f"{prefix}_{receipt_suffix}")
        return DuplicateRiskAcknowledgement(
            acknowledgement_id=acknowledgement_id,
            predecessor_action_id=predecessor_action_id,
            approval_request_id=receipt.request_id,
            binding_digest=receipt.binding_digest,
            acknowledged_at=receipt.approved_at,
        ).validate()

    def _validate_duplicate_risk_before_create(
        self,
        *,
        store: ActionStore,
        tool: Any,
        tool_spec: ToolSpec,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        public_intent_digest: str,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        successor_action_id: str | None,
        acknowledgement: DuplicateRiskAcknowledgement,
    ) -> None:
        if successor_action_id is None:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The duplicate-risk successor identity is unavailable.",
            )
        predecessor = self._load_duplicate_risk_predecessor(
            store=store,
            tool=tool,
            tool_spec=tool_spec,
            spec=spec,
            prepared=prepared,
            successor_intent_digest=public_intent_digest,
            successor_target_category=prepared.target_category,
            successor_target_fingerprint=prepared.target_fingerprint,
            mutation_capabilities=mutation_capabilities,
            reconciliation_capabilities=reconciliation_capabilities,
            predecessor_action_id=acknowledgement.predecessor_action_id,
        )
        authority = self._approval_authority
        if authority is None:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The durable duplicate-risk authority is unavailable.",
                request_id=acknowledgement.approval_request_id,
                action_id=successor_action_id,
            )
        binding = self._duplicate_risk_approval_binding(
            authority=authority,
            predecessor=predecessor,
            successor_action_id=successor_action_id,
            action_kind=spec.action_kind,
            intent_digest=public_intent_digest,
            target_category=prepared.target_category,
            target_fingerprint=prepared.target_fingerprint,
            effect_class=spec.effect_class,
            mutation_capabilities=mutation_capabilities,
            reconciliation_capabilities=reconciliation_capabilities,
            tool_name=tool_spec.name,
            tool_version=tool_spec.version,
        )
        try:
            receipt = authority.validate_consumed(
                binding,
                request_id=acknowledgement.approval_request_id,
                action_id=successor_action_id,
            )
        except AgentError as exc:
            reason = exc.details.get("reason")
            raise _DuplicateRiskBlocked(
                ("duplicate_risk_acknowledgement_expired"
                 if reason == "expired"
                 else "duplicate_risk_predecessor_changed"),
                ("The duplicate-risk acknowledgement expired before the new "
                 "action could execute."
                 if reason == "expired" else
                 "The predecessor changed after duplicate-risk "
                 "acknowledgement."),
                request_id=acknowledgement.approval_request_id,
                action_id=successor_action_id,
            ) from exc
        expected = self._duplicate_risk_acknowledgement(
            predecessor.action_id, receipt)
        if expected != acknowledgement:
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The stored duplicate-risk acknowledgement is not the exact "
                "authenticated receipt.",
                request_id=acknowledgement.approval_request_id,
                action_id=successor_action_id,
            )

    @staticmethod
    def _duplicate_risk_context_matches_record(
        context: DuplicateRiskInvocationContext,
        record: ActionRecord,
    ) -> bool:
        acknowledgement = record.duplicate_risk_acknowledgement
        return (
            acknowledgement is not None
            and context.predecessor_action_id == record.predecessor_action_id
            and context.successor_action_id == record.action_id
            and (
                context.approval_request_id is None
                or context.approval_request_id == (
                    acknowledgement.approval_request_id)
            )
        )

    def _validate_duplicate_risk_record(
        self,
        *,
        store: ActionStore,
        record: ActionRecord,
        tool: Any,
        tool_spec: ToolSpec,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
    ) -> None:
        acknowledgement = record.duplicate_risk_acknowledgement
        if acknowledgement is None:
            return
        if record.predecessor_action_id != (
                acknowledgement.predecessor_action_id):
            raise _DuplicateRiskBlocked(
                "duplicate_risk_binding_invalid",
                "The successor predecessor linkage is invalid.",
                request_id=acknowledgement.approval_request_id,
                action_id=record.action_id,
            )
        self._validate_duplicate_risk_before_create(
            store=store,
            tool=tool,
            tool_spec=tool_spec,
            spec=spec,
            prepared=prepared,
            public_intent_digest=record.intent_digest,
            mutation_capabilities=record.mutation_capabilities,
            reconciliation_capabilities=record.reconciliation_capabilities,
            successor_action_id=record.action_id,
            acknowledgement=acknowledgement,
        )

    @staticmethod
    def _duplicate_risk_blocked_invocation(
        tool_name: str,
        redacted: dict[str, str],
        blocked: _DuplicateRiskBlocked,
        decision: Decision | None,
        invocation_id: str,
        duration: float,
        log: Any,
        *,
        fallback_action_id: str | None = None,
        action_state: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolInvocation:
        action_id = blocked.action_id or fallback_action_id
        log.warning(
            Events.TOOL_DENIED,
            reason=blocked.kind,
            action_id=action_id,
            approval_request_id=blocked.request_id,
        )
        metadata: dict[str, Any] = {"kind": blocked.kind}
        if blocked.request_id is not None:
            metadata["approval_request_id"] = blocked.request_id
        return ToolInvocation(
            tool_name,
            redacted,
            ToolResult.failure(blocked.safe_message, **metadata),
            duration,
            decision,
            invocation_id=invocation_id,
            action_id=action_id,
            attempt_id=attempt_id,
            action_state=action_state,
            approval_request_id=blocked.request_id,
        )

    @staticmethod
    def _approval_invocation(
        tool_name: str,
        redacted: dict[str, str],
        authorization: ApprovalAuthorization,
        decision: Decision,
        invocation_id: str,
        duration: float,
        log: Any,
        *,
        historical_action: ActionRecord | None = None,
        planned_action_id: str | None = None,
    ) -> ToolInvocation:
        kinds = {
            ApprovalAuthorizationStatus.PENDING: "approval_pending",
            ApprovalAuthorizationStatus.DENIED: "approval_denied",
            ApprovalAuthorizationStatus.EXPIRED: "approval_expired",
            ApprovalAuthorizationStatus.CONSUMED: "approval_consumed",
        }
        messages = {
            ApprovalAuthorizationStatus.PENDING: (
                "This exact action is waiting for human approval."),
            ApprovalAuthorizationStatus.DENIED: (
                "Human approval was denied for this exact action."),
            ApprovalAuthorizationStatus.EXPIRED: (
                "The approval for this exact action expired."),
            ApprovalAuthorizationStatus.CONSUMED: (
                "The one-shot approval was already consumed."),
        }
        kind = kinds.get(authorization.status, "approval_invalid")
        message = messages.get(
            authorization.status, "Durable approval is invalid.")
        log.warning(
            Events.TOOL_DENIED,
            reason=kind,
            approval_request_id=authorization.request_id,
            action_id=(historical_action.action_id
                       if historical_action is not None else None),
        )
        return ToolInvocation(
            tool_name,
            redacted,
            ToolResult.failure(
                message,
                kind=kind,
                approval_request_id=authorization.request_id,
            ),
            duration,
            decision,
            invocation_id=invocation_id,
            action_id=(historical_action.action_id
                       if historical_action is not None
                       else planned_action_id),
            action_state=(historical_action.state.value
                          if historical_action is not None else None),
            approval_request_id=authorization.request_id,
        )

    def _invoke_journaled(
        self,
        *,
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        adapter: ToolActionAdapter,
        spec: ActionAdapterSpec,
        checkpoint: Callable[[], None] | None,
        action_context: ActionInvocationContext | None,
        approval_context: ApprovalInvocationContext | None,
        duplicate_risk_context: DuplicateRiskInvocationContext | None,
        log: Any,
        elapsed: Callable[[], float],
    ) -> ToolInvocation:
        redacted = _redacted_args(cleaned)
        invocation_id = self._id_factory("inv")
        try:
            trusted_context = action_context or ActionInvocationContext()
            if not isinstance(trusted_context, ActionInvocationContext):
                raise TypeError("untrusted action context type")
            trusted_context.validate()
            provenance = trusted_context.to_provenance(invocation_id)
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_preflight_failed",
                message="The action intent could not be prepared safely.",
                log=log, invocation_id=invocation_id)

        if trusted_context.deduplication_scope in (
                _RESERVED_INTERNAL_DEDUPLICATION_SCOPES):
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_deduplication_scope_reserved",
                message=("The trusted deduplication scope is reserved for "
                         "an internal AgentCore action flow."),
                log=log, invocation_id=invocation_id)

        try:
            if type(tool) is AuthenticatedActionTool:
                tool.validate_action_context(trusted_context, repeat=duplicate_risk_context is not None)
            prepared = adapter.prepare_intent(cleaned, trusted_context)
            if not isinstance(prepared, PreparedActionIntent):
                raise TypeError("adapter preflight type")
            prepared.validate()
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_preflight_failed",
                message="The action intent could not be prepared safely.",
                log=log, invocation_id=invocation_id)

        try:
            if approval_context is not None:
                if not isinstance(approval_context, ApprovalInvocationContext):
                    raise TypeError("untrusted approval context type")
                approval_context.validate()
            if duplicate_risk_context is not None:
                if not isinstance(
                        duplicate_risk_context,
                        DuplicateRiskInvocationContext):
                    raise TypeError("untrusted duplicate-risk context type")
                duplicate_risk_context.validate()
                if trusted_context.action_id is not None and (
                        duplicate_risk_context.successor_action_id !=
                        trusted_context.action_id):
                    raise ValueError(
                        "duplicate-risk successor does not match action resume")
                if trusted_context.deduplication_scope is not None:
                    raise ValueError(
                        "duplicate-risk creation owns its deduplication identity")
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind=("duplicate_risk_context_invalid"
                      if duplicate_risk_context is not None
                      else "approval_context_invalid"),
                message=(
                    "The trusted duplicate-risk context is invalid."
                    if duplicate_risk_context is not None else
                    "The trusted approval context is invalid."
                ),
                log=log, invocation_id=invocation_id)

        protected_requested = (
            prepared.requires_protected_payload
            or prepared.protected_payload is not None
            or prepared.intent_ref is not None
        )
        if protected_requested and self._protected_payload_store is None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="protected_payload_unavailable",
                message="Protected action payload storage is not available.",
                log=log, invocation_id=invocation_id)
        if prepared.intent_ref is not None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="protected_reference_untrusted",
                message="Protected references are minted only by trusted storage.",
                log=log, invocation_id=invocation_id)
        if prepared.requires_protected_payload and (
                prepared.protected_payload is None):
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="protected_payload_invalid",
                message="The required protected action payload is unavailable.",
                log=log, invocation_id=invocation_id)

        decision: Decision | None = None
        mutation_capabilities = tuple(sorted(
            str(capability) for capability in tool_spec.capabilities
            if capability not in READ_ONLY_CAPABILITIES))
        reconciliation_capabilities = tuple(sorted(
            str(capability) for capability in spec.reconciliation_capabilities))
        try:
            public_intent_digest = prepared.intent_digest
            if prepared.protected_payload is not None:
                assert self._protected_payload_store is not None
                public_intent_digest = self._protected_payload_store.intent_digest(
                    prepared.protected_payload,
                    purpose=ProtectedPayloadPurpose.ACTION_INTENT,
                )
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="protected_payload_unverified",
                message="The protected action intent could not be verified.",
                log=log, invocation_id=invocation_id)

        resume_requested = trusted_context.action_id is not None
        approval_receipt: DurableApprovalReceipt | None = None
        approved_action_id: str | None = None
        duplicate_risk_acknowledgement: (
            DuplicateRiskAcknowledgement | None
        ) = None
        duplicate_risk_successor_id: str | None = None
        creation_context = trusted_context
        if not resume_requested:
            if checkpoint is not None:
                checkpoint()
            decision, approval_required = self._current_policy_decision(
                tool_spec,
                description=(prepared.approval_description
                             or tool_spec.description),
            )
            log.debug(Events.PERMISSION_CHECKED, **decision.to_dict())
            if decision.outcome is Outcome.DENY:
                log.warning(Events.TOOL_DENIED, **decision.to_dict())
                return ToolInvocation(
                    tool_spec.name,
                    redacted,
                    ToolResult.failure(
                        "Permission was not granted for this action.",
                        kind="permission_denied"),
                    elapsed(), decision, invocation_id=invocation_id)
            if self._action_store is None:
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind="action_store_unavailable",
                    message="The durable action journal is unavailable.",
                    log=log, decision=decision, invocation_id=invocation_id)
            if duplicate_risk_context is not None:
                duplicate_risk_successor_id = (
                    duplicate_risk_context.successor_action_id
                    or self._id_factory("act")
                )
                try:
                    duplicate_risk_acknowledgement = (
                        self._authorize_duplicate_risk(
                            store=self._action_store,
                            tool=tool,
                            tool_spec=tool_spec,
                            spec=spec,
                            prepared=prepared,
                            public_intent_digest=public_intent_digest,
                            mutation_capabilities=mutation_capabilities,
                            reconciliation_capabilities=(
                                reconciliation_capabilities),
                            context=duplicate_risk_context,
                            successor_action_id=(
                                duplicate_risk_successor_id),
                        )
                    )
                    creation_context = replace(
                        trusted_context,
                        deduplication_scope=(
                            _DUPLICATE_RISK_DEDUPLICATION_SCOPE),
                        deduplication_key=(
                            "approval:"
                            + duplicate_risk_acknowledgement
                            .approval_request_id),
                    ).validate()
                    approved_action_id = duplicate_risk_successor_id
                except _DuplicateRiskBlocked as exc:
                    return self._duplicate_risk_blocked_invocation(
                        tool_spec.name,
                        redacted,
                        exc,
                        decision,
                        invocation_id,
                        elapsed(),
                        log,
                        fallback_action_id=duplicate_risk_successor_id,
                    )
                except Exception:
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="duplicate_risk_binding_invalid",
                        message=(
                            "The duplicate-risk acknowledgement could not be "
                            "authenticated."
                        ),
                        log=log, decision=decision,
                        invocation_id=invocation_id,
                        action_id=duplicate_risk_successor_id,
                        approval_request_id=(
                            duplicate_risk_context.approval_request_id),
                    )
            if approval_required:
                try:
                    authorization = self._authorize_approval(
                        action_kind=spec.action_kind,
                        intent_digest=public_intent_digest,
                        mutation_capabilities=mutation_capabilities,
                        tool_name=tool_spec.name,
                        tool_version=tool_spec.version,
                        description=(prepared.approval_description
                                     or tool_spec.description),
                        action_id=(
                            duplicate_risk_successor_id
                            if duplicate_risk_successor_id is not None
                            else (None if approval_context is not None
                                  else self._id_factory("act"))
                        ),
                        approval_context=approval_context,
                    )
                except Exception:
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="approval_unavailable",
                        message="Durable human approval is unavailable.",
                        log=log, decision=decision,
                        invocation_id=invocation_id,
                        approval_request_id=(approval_context.request_id
                                             if approval_context else None))
                if not authorization.authorized:
                    return self._approval_invocation(
                        tool_spec.name, redacted, authorization, decision,
                        invocation_id, elapsed(), log,
                        planned_action_id=duplicate_risk_successor_id)
                assert authorization.receipt is not None
                approval_receipt = authorization.receipt
                approved_action_id = authorization.action_id
                rechecked, still_requires = self._current_policy_decision(
                    tool_spec,
                    description=(prepared.approval_description
                                 or tool_spec.description),
                )
                try:
                    current_fingerprint = permission_policy_fingerprint(
                        self.policy)
                except Exception:
                    current_fingerprint = ""
                if rechecked.outcome is Outcome.DENY or not (
                        hmac.compare_digest(
                            current_fingerprint,
                            approval_receipt.policy_fingerprint)):
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="approval_policy_changed",
                        message=("Current mutation authority changed after "
                                 "approval."),
                        log=log, decision=rechecked,
                        invocation_id=invocation_id,
                        approval_request_id=authorization.request_id)
                decision = Decision(
                    Outcome.ALLOW,
                    "authorized by durable one-shot approval",
                    capability=(rechecked.capability if still_requires
                                else None),
                    required_level=rechecked.required_level,
                )
            if checkpoint is not None:
                checkpoint()
            if duplicate_risk_acknowledgement is not None:
                rechecked, now_requires_approval = (
                    self._current_policy_decision(
                        tool_spec,
                        description=(prepared.approval_description
                                     or tool_spec.description),
                    )
                )
                log.debug(Events.PERMISSION_CHECKED, **rechecked.to_dict())
                if rechecked.outcome is Outcome.DENY:
                    log.warning(Events.TOOL_DENIED, **rechecked.to_dict())
                    return ToolInvocation(
                        tool_spec.name,
                        redacted,
                        ToolResult.failure(
                            "Permission was not granted for this action.",
                            kind="permission_denied",
                        ),
                        elapsed(),
                        rechecked,
                        invocation_id=invocation_id,
                        action_id=duplicate_risk_successor_id,
                        approval_request_id=(
                            duplicate_risk_acknowledgement
                            .approval_request_id),
                    )
                try:
                    current_fingerprint = permission_policy_fingerprint(
                        self.policy)
                except Exception:
                    current_fingerprint = ""
                approval_is_current = (
                    approval_receipt is not None
                    and hmac.compare_digest(
                        current_fingerprint,
                        approval_receipt.policy_fingerprint,
                    )
                    and self._clock.now() < approval_receipt.expires_at
                )
                if now_requires_approval and not approval_is_current:
                    return self._journal_failure(
                        tool_spec.name,
                        redacted,
                        elapsed(),
                        kind="approval_policy_changed",
                        message=(
                            "Current permission policy requires a separate "
                            "ordinary approval for this new action."
                        ),
                        log=log,
                        decision=rechecked,
                        invocation_id=invocation_id,
                        action_id=duplicate_risk_successor_id,
                        approval_request_id=(
                            duplicate_risk_acknowledgement
                            .approval_request_id),
                    )
                if approval_receipt is not None and not approval_is_current:
                    return self._journal_failure(
                        tool_spec.name,
                        redacted,
                        elapsed(),
                        kind="approval_policy_changed",
                        message=(
                            "Current mutation authority changed after ordinary "
                            "approval."
                        ),
                        log=log,
                        decision=rechecked,
                        invocation_id=invocation_id,
                        action_id=duplicate_risk_successor_id,
                        approval_request_id=approval_receipt.request_id,
                    )
                decision = (
                    Decision(
                        Outcome.ALLOW,
                        "authorized by durable one-shot approval",
                        capability=(rechecked.capability
                                    if now_requires_approval else None),
                        required_level=rechecked.required_level,
                    )
                    if approval_receipt is not None else rechecked
                )
                try:
                    self._validate_duplicate_risk_before_create(
                        store=self._action_store,
                        tool=tool,
                        tool_spec=tool_spec,
                        spec=spec,
                        prepared=prepared,
                        public_intent_digest=public_intent_digest,
                        mutation_capabilities=mutation_capabilities,
                        reconciliation_capabilities=(
                            reconciliation_capabilities),
                        successor_action_id=(
                            duplicate_risk_successor_id),
                        acknowledgement=(
                            duplicate_risk_acknowledgement),
                    )
                except _DuplicateRiskBlocked as exc:
                    return self._duplicate_risk_blocked_invocation(
                        tool_spec.name,
                        redacted,
                        exc,
                        decision,
                        invocation_id,
                        elapsed(),
                        log,
                        fallback_action_id=duplicate_risk_successor_id,
                    )
                except Exception:
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="duplicate_risk_binding_invalid",
                        message=(
                            "The duplicate-risk acknowledgement could not be "
                            "revalidated before successor creation."
                        ),
                        log=log, decision=decision,
                        invocation_id=invocation_id,
                        action_id=duplicate_risk_successor_id,
                        approval_request_id=(
                            duplicate_risk_acknowledgement
                            .approval_request_id),
                    )

        if self._action_store is None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_store_unavailable",
                message="The durable action journal is unavailable.",
                log=log, decision=decision, invocation_id=invocation_id)
        store = self._action_store
        if resume_requested:
            assert trusted_context.action_id is not None
            try:
                created = store.load(trusted_context.action_id)
                created.validate()
                if not self._matches_resume_request(
                    created,
                    prepared=prepared,
                    spec=spec,
                    tool_spec=tool_spec,
                    mutation_capabilities=mutation_capabilities,
                    reconciliation_capabilities=reconciliation_capabilities,
                    trusted_context=trusted_context,
                ):
                    raise ValueError("resume identity mismatch")
                if duplicate_risk_context is not None and not (
                        self._duplicate_risk_context_matches_record(
                            duplicate_risk_context, created)):
                    raise ValueError("duplicate-risk resume identity mismatch")
            except Exception:
                return self._journal_failure(
                    tool_spec.name,
                    redacted,
                    elapsed(),
                    kind="action_resume_unverified",
                    message="The durable action identity could not be verified.",
                    log=log,
                    invocation_id=invocation_id,
                    action_id=trusted_context.action_id,
                )
            created_here = False
        else:
            assert decision is not None
            try:
                policy_fingerprint = permission_policy_fingerprint(self.policy)
                if approval_receipt is None:
                    authority_scope = f"live:{invocation_id}"
                    receipt = AuthorityReceipt(
                        receipt_id=self._id_factory("auth"),
                        binding_digest=authority_binding_digest(
                            action_kind=spec.action_kind,
                            intent_digest=public_intent_digest,
                            mutation_capabilities=mutation_capabilities,
                            tool_name=tool_spec.name,
                            tool_version=tool_spec.version,
                            invocation_id=invocation_id,
                            authority_scope=authority_scope,
                            policy_fingerprint=policy_fingerprint,
                        ),
                        policy_fingerprint=policy_fingerprint,
                        scope_ref=authority_scope,
                        authorized_at=self._clock.now(),
                    ).validate()
                else:
                    receipt = AuthorityReceipt(
                        receipt_id=self._id_factory("auth"),
                        binding_digest=approval_receipt.binding_digest,
                        policy_fingerprint=approval_receipt.policy_fingerprint,
                        scope_ref=approval_receipt.scope_ref,
                        authorized_at=approval_receipt.approved_at,
                        expires_at=approval_receipt.expires_at,
                        approval_request_id=approval_receipt.request_id,
                    ).validate()
            except Exception:
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind="action_record_invalid",
                    message="The durable action intent could not be constructed.",
                    log=log, decision=decision, invocation_id=invocation_id)

            try:
                created, created_here, create_error, proposed_action_id = (
                    self._create_fresh_action(
                        store=store,
                        tool=tool,
                        tool_spec=tool_spec,
                        spec=spec,
                        prepared=prepared,
                        trusted_context=creation_context,
                        public_intent_digest=public_intent_digest,
                        provenance=provenance,
                        authority=receipt,
                        mutation_capabilities=mutation_capabilities,
                        reconciliation_capabilities=(
                            reconciliation_capabilities),
                        approved_action_id=approved_action_id,
                        predecessor_action_id=(
                            duplicate_risk_context.predecessor_action_id
                            if duplicate_risk_context is not None else None),
                        duplicate_risk_acknowledgement=(
                            duplicate_risk_acknowledgement),
                    )
                )
            except _DuplicateRiskBlocked as exc:
                return self._duplicate_risk_blocked_invocation(
                    tool_spec.name,
                    redacted,
                    exc,
                    decision,
                    invocation_id,
                    elapsed(),
                    log,
                    fallback_action_id=duplicate_risk_successor_id,
                )
            if created is None:
                kind = create_error or "action_create_unverified"
                messages = {
                    "external_idempotency_contract_unavailable": (
                        "Stable external idempotency ownership is not available."
                    ),
                    "protected_payload_unverified": (
                        "The protected action payload could not be stored and "
                        "verified."
                    ),
                    "action_record_invalid": (
                        "The durable action intent could not be constructed."
                    ),
                }
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind=kind,
                    message=messages.get(
                        kind,
                        "The durable action intent could not be verified.",
                    ),
                    log=log, decision=decision, invocation_id=invocation_id,
                    action_id=proposed_action_id)
            try:
                self.validate_action_protected_storage(created)
                if not self._protected_intent_matches(created, prepared):
                    raise ValueError("protected intent mismatch")
            except Exception:
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind="protected_payload_unverified",
                    message="The protected action payload could not be verified.",
                    log=log, decision=decision,
                    invocation_id=invocation_id,
                    action_id=created.action_id)

        guard_entered = False
        try:
            with store.execution_guard(created.action_id):
                guard_entered = True
                return self._under_action_guard(
                    store=store, created=created, created_here=created_here,
                    resume_requested=resume_requested,
                    tool=tool, tool_spec=tool_spec,
                    cleaned=cleaned, redacted=redacted,
                    adapter=adapter, spec=spec, prepared=prepared,
                    checkpoint=checkpoint,
                    approval_context=approval_context,
                    current_approval_receipt=approval_receipt,
                    decision=decision, invocation_id=invocation_id,
                    log=log, elapsed=elapsed)
        except CancellationRequested:
            raise
        except Exception:
            kind = ("action_execution_boundary_uncertain" if guard_entered
                    else "action_guard_unavailable")
            message = (
                "The action execution boundary ended without proven state."
                if guard_entered else
                "The action execution guard is unavailable."
            )
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind=kind,
                message=message,
                log=log, decision=decision, invocation_id=invocation_id,
                action_id=created.action_id)

    def _under_action_guard(
        self,
        *,
        store: ActionStore,
        created: ActionRecord,
        created_here: bool,
        resume_requested: bool,
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        redacted: dict[str, str],
        adapter: ToolActionAdapter,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        checkpoint: Callable[[], None] | None,
        approval_context: ApprovalInvocationContext | None,
        current_approval_receipt: DurableApprovalReceipt | None,
        decision: Decision | None,
        invocation_id: str,
        log: Any,
        elapsed: Callable[[], float],
    ) -> ToolInvocation:
        try:
            current = store.load(created.action_id)
            current.validate()
            if not self._same_action_lineage(current, created):
                raise ValueError("action reload changed immutable intent")
            self.validate_action_protected_storage(current)
            if not self._protected_intent_matches(current, prepared):
                raise ValueError("protected intent mismatch")
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_reload_failed",
                message="The durable action state could not be verified.",
                log=log, decision=decision, invocation_id=invocation_id,
                action_id=created.action_id)

        external_reconcile_retry = (
            resume_requested
            and current.state is ActionState.RECONCILE_REQUIRED
            and spec.effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT
        )
        if current.state is not ActionState.AUTHORIZED and not (
                external_reconcile_retry):
            return self._attached_invocation(
                tool_spec.name, redacted, current, decision,
                invocation_id, elapsed(), log)
        fresh_owner = (
            created_here
            and current.provenance.invocation_id == invocation_id
            and not current.attempts
        )
        explicit_waiting_resume = resume_requested and not current.attempts
        safe_retry = (
            resume_requested
            and self._has_checked_retry_proof(current)
        )
        if not (
            fresh_owner or explicit_waiting_resume or safe_retry
            or external_reconcile_retry
        ):
            return self._attached_invocation(
                tool_spec.name, redacted, current, decision,
                invocation_id, elapsed(), log)

        if resume_requested:
            decision, approval_required = self._current_policy_decision(
                tool_spec,
                description=(prepared.approval_description
                             or tool_spec.description),
            )
            log.debug(Events.PERMISSION_CHECKED, **decision.to_dict())
            if decision.outcome is Outcome.DENY:
                log.warning(Events.TOOL_DENIED, **decision.to_dict())
                return ToolInvocation(
                    tool_spec.name,
                    redacted,
                    ToolResult.failure(
                        "Permission was not granted for this action.",
                        kind="permission_denied"),
                    elapsed(),
                    decision,
                    invocation_id=invocation_id,
                    action_id=current.action_id,
                    action_state=current.state.value,
                )
            if approval_required:
                try:
                    authorization = self._authorize_approval(
                        action_kind=current.action_kind,
                        intent_digest=current.intent_digest,
                        mutation_capabilities=current.mutation_capabilities,
                        tool_name=current.tool_name,
                        tool_version=current.tool_version,
                        description=(prepared.approval_description
                                     or tool_spec.description),
                        # A retry already has durable action identity.  Pass it
                        # even when continuing an existing request so a valid
                        # approval for another same-shaped action cannot be
                        # consumed at this execution seam.
                        action_id=current.action_id,
                        approval_context=approval_context,
                    )
                except Exception:
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="approval_unavailable",
                        message="Durable human approval is unavailable.",
                        log=log, decision=decision,
                        invocation_id=invocation_id,
                        action_id=current.action_id,
                        action_state=current.state.value,
                        approval_request_id=(approval_context.request_id
                                             if approval_context else None))
                if authorization.authorized:
                    assert authorization.receipt is not None
                    current_approval_receipt = authorization.receipt
                elif authorization.status is (
                        ApprovalAuthorizationStatus.CONSUMED) and not (
                            current.attempts) and approval_context is not None:
                    try:
                        assert self._approval_authority is not None
                        binding = self._approval_authority.binding(
                            action_kind=current.action_kind,
                            intent_digest=current.intent_digest,
                            mutation_capabilities=current.mutation_capabilities,
                            tool_name=current.tool_name,
                            tool_version=current.tool_version,
                            policy_fingerprint=permission_policy_fingerprint(
                                self.policy),
                        )
                        current_approval_receipt = (
                            self._approval_authority.validate_consumed(
                                binding,
                                request_id=authorization.request_id,
                                action_id=current.action_id,
                            )
                        )
                    except AgentError as exc:
                        expired = exc.details.get("reason") == "expired"
                        return self._journal_failure(
                            tool_spec.name, redacted, elapsed(),
                            kind=("approval_expired" if expired else
                                  "approval_unavailable"),
                            message=(
                                "The one-shot approval expired before execution."
                                if expired else
                                "Durable human approval is unavailable."
                            ),
                            log=log, decision=decision,
                            invocation_id=invocation_id,
                            action_id=current.action_id,
                            action_state=current.state.value,
                            approval_request_id=authorization.request_id)
                    except Exception:
                        return self._journal_failure(
                            tool_spec.name, redacted, elapsed(),
                            kind="approval_unavailable",
                            message="Durable human approval is unavailable.",
                            log=log, decision=decision,
                            invocation_id=invocation_id,
                            action_id=current.action_id,
                            action_state=current.state.value,
                            approval_request_id=authorization.request_id)
                else:
                    return self._approval_invocation(
                        tool_spec.name, redacted, authorization, decision,
                        invocation_id, elapsed(), log,
                        historical_action=current)
                assert current_approval_receipt is not None
                rechecked, still_requires = self._current_policy_decision(
                    tool_spec,
                    description=(prepared.approval_description
                                 or tool_spec.description),
                )
                try:
                    current_fingerprint = permission_policy_fingerprint(
                        self.policy)
                except Exception:
                    current_fingerprint = ""
                if rechecked.outcome is Outcome.DENY or not hmac.compare_digest(
                        current_fingerprint,
                        current_approval_receipt.policy_fingerprint):
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="approval_policy_changed",
                        message=("Current mutation authority changed after "
                                 "approval."),
                        log=log, decision=rechecked,
                        invocation_id=invocation_id,
                        action_id=current.action_id,
                        action_state=current.state.value,
                        approval_request_id=authorization.request_id)
                if self._clock.now() >= current_approval_receipt.expires_at:
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="approval_expired",
                        message="The one-shot approval expired before execution.",
                        log=log, decision=rechecked,
                        invocation_id=invocation_id,
                        action_id=current.action_id,
                        action_state=current.state.value,
                        approval_request_id=authorization.request_id)
                decision = Decision(
                    Outcome.ALLOW,
                    "authorized by current durable one-shot approval",
                    capability=(rechecked.capability if still_requires
                                else None),
                    required_level=rechecked.required_level,
                )
            if spec.effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT:
                try:
                    self._validate_external_contract_for_record(
                        tool, tool_spec, spec, current)
                    if current.state is ActionState.RECONCILE_REQUIRED:
                        authorized = current.transition(
                            ActionState.AUTHORIZED,
                            transition_id=new_action_transition_id(
                                id_factory=self._id_factory),
                            at=self._clock.now(),
                            effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
                            proof=(
                                ActionTransitionProof
                                .EXTERNAL_IDEMPOTENCY_GUARANTEE
                            ),
                        )
                        stored = store.compare_and_swap(
                            current.action_id, current.version, authorized)
                        exact = store.load(current.action_id)
                        if stored != authorized or exact != authorized:
                            raise ValueError(
                                "retry authorization publication mismatch")
                        current = exact
                except Exception:
                    return self._journal_failure(
                        tool_spec.name, redacted, elapsed(),
                        kind="external_idempotency_contract_unavailable",
                        message=(
                            "The durable external idempotency guarantee could "
                            "not be verified for this attempt."
                        ),
                        log=log, decision=decision,
                        invocation_id=invocation_id,
                        action_id=current.action_id,
                        action_state=current.state.value,
                    )
        assert decision is not None

        if not resume_requested and current.authority.expires_at is not None and (
                self._clock.now() >= current.authority.expires_at):
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="approval_expired",
                message="The one-shot approval expired before execution.",
                log=log, decision=decision,
                invocation_id=invocation_id,
                action_id=current.action_id,
                action_state=current.state.value,
                approval_request_id=current.authority.approval_request_id)

        try:
            if checkpoint is not None:
                checkpoint()
        except CancellationRequested:
            self._publish_preclaim_cancellation(store, current)
            raise
        except Exception:
            published = self._publish_confirmed_no_effect(
                store, current, attempt_status=None,
                result_class="control.failure",
                summary="Execution control failed before an attempt was claimed.")
            return self._published_or_uncertain(
                tool_spec.name, redacted, published, decision,
                invocation_id, None, elapsed(), log,
                fallback_kind="action_control_failed")

        if current.duplicate_risk_acknowledgement is not None and not (
                current.attempts):
            try:
                decision = self._validate_current_duplicate_risk_permission(
                    record=current,
                    tool_spec=tool_spec,
                    prepared=prepared,
                    current_approval_receipt=current_approval_receipt,
                )
                self._validate_duplicate_risk_record(
                    store=store,
                    record=current,
                    tool=tool,
                    tool_spec=tool_spec,
                    spec=spec,
                    prepared=prepared,
                )
            except _DuplicateRiskBlocked as exc:
                return self._duplicate_risk_blocked_invocation(
                    tool_spec.name,
                    redacted,
                    exc,
                    decision,
                    invocation_id,
                    elapsed(),
                    log,
                    fallback_action_id=current.action_id,
                    action_state=current.state.value,
                )
            except Exception:
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind="duplicate_risk_binding_invalid",
                    message=(
                        "The duplicate-risk acknowledgement could not be "
                        "revalidated before the physical-attempt claim."
                    ),
                    log=log, decision=decision,
                    invocation_id=invocation_id,
                    action_id=current.action_id,
                    action_state=current.state.value,
                )

        if spec.effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT:
            try:
                self._validate_external_contract_for_record(
                    tool, tool_spec, spec, current)
            except Exception:
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind="external_idempotency_contract_unavailable",
                    message=(
                        "The external idempotency guarantee expired or changed "
                        "before the physical-attempt claim."
                    ),
                    log=log, decision=decision,
                    invocation_id=invocation_id,
                    action_id=current.action_id,
                    action_state=current.state.value,
                )

        attempt_id = new_action_attempt_id(id_factory=self._id_factory)
        transition_id = new_action_transition_id(id_factory=self._id_factory)
        expected_version = current.version + 1
        try:
            store.claim_attempt(
                current.action_id, current.version,
                attempt_id=attempt_id, invocation_id=invocation_id,
                owner_id=self._action_owner_id,
                owner_epoch=self._action_owner_epoch,
                transition_id=transition_id)
        except Exception:
            pass
        claimed = self._load_exact_claim(
            store, expected_record=current,
            expected_version=expected_version,
            attempt_id=attempt_id, invocation_id=invocation_id,
            owner_id=self._action_owner_id,
            owner_epoch=self._action_owner_epoch,
            transition_id=transition_id)
        if claimed is None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_claim_unverified",
                message="The physical-attempt claim could not be verified.",
                log=log, decision=decision, invocation_id=invocation_id,
                action_id=current.action_id, attempt_id=attempt_id,
                action_state=ActionState.IN_FLIGHT.value)

        try:
            if checkpoint is not None:
                checkpoint()
        except CancellationRequested:
            self._publish_claimed_cancellation(store, claimed)
            raise
        except Exception:
            published = self._publish_confirmed_no_effect(
                store, claimed,
                attempt_status=ActionAttemptStatus.NOT_INVOKED,
                result_class="control.failure",
                summary="Execution control failed before the effect seam.")
            return self._published_or_uncertain(
                tool_spec.name, redacted, published, decision,
                invocation_id, attempt_id, elapsed(), log,
                fallback_kind="action_control_failed")

        try:
            normalized, cancellation, final_decision = (
                self._execute_claimed_effect(
                    store=store,
                    claimed=claimed,
                    tool=tool,
                    tool_spec=tool_spec,
                    cleaned=cleaned,
                    adapter=adapter,
                    spec=spec,
                    prepared=prepared,
                    attempt_id=attempt_id,
                    current_approval_receipt=current_approval_receipt,
                    checkpoint=checkpoint,
                )
            )
            decision = final_decision
        except _CredentialActionNotInvoked as exc:
            if exc.cancellation is not None:
                self._publish_claimed_cancellation(store, claimed)
                raise exc.cancellation
            published = self._publish_confirmed_no_effect(
                store, claimed, attempt_status=ActionAttemptStatus.NOT_INVOKED,
                result_class=("filesystem.not_invoked" if isinstance(exc, _FilesystemWriteNotInvoked)
                              else "gmail.send.not_invoked"),
                summary=("The filesystem staging boundary was not entered."
                         if isinstance(exc, _FilesystemWriteNotInvoked)
                         else "The authenticated send provider was not entered."))
            if exc.interruption is not None:
                raise exc.interruption() from None
            return self._published_or_uncertain(
                tool_spec.name, redacted, published, decision, invocation_id, attempt_id,
                elapsed(), log, fallback_kind=("filesystem_write_unavailable"
                    if isinstance(exc, _FilesystemWriteNotInvoked) else "action_credential_unavailable"),
                fallback_action_id=claimed.action_id)
        except _CurrentAuthorityBlocked as exc:
            if type(tool) in {AuthenticatedActionTool, WriteFileTool}:
                published = self._publish_confirmed_no_effect(
                    store, claimed, attempt_status=ActionAttemptStatus.NOT_INVOKED,
                    result_class=("filesystem.authority_denied" if type(tool) is WriteFileTool
                                  else "gmail.send.authority_denied"),
                    summary=("Current authority prevented entry to filesystem staging."
                             if type(tool) is WriteFileTool else
                             "Current authority prevented entry to the send provider."))
                return self._published_or_uncertain(
                    tool_spec.name, redacted, published, exc.decision or decision,
                    invocation_id, attempt_id, elapsed(), log, fallback_kind=exc.kind,
                    fallback_action_id=claimed.action_id)
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind=exc.kind,
                message=(exc.safe_message + " The claimed attempt remains "
                         "nonterminal and the physical effect seam was not "
                         "entered."),
                log=log, decision=(exc.decision or decision),
                invocation_id=invocation_id,
                action_id=claimed.action_id,
                attempt_id=attempt_id,
                action_state=ActionState.IN_FLIGHT.value,
                approval_request_id=exc.request_id,
            )
        except _DuplicateRiskBlocked as exc:
            return self._duplicate_risk_blocked_invocation(
                tool_spec.name,
                redacted,
                exc,
                decision,
                invocation_id,
                elapsed(),
                log,
                fallback_action_id=claimed.action_id,
                action_state=ActionState.IN_FLIGHT.value,
                attempt_id=attempt_id,
            )
        except _ExternalContractBlocked:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="external_idempotency_contract_unavailable",
                message=(
                    "The external idempotency guarantee expired or changed "
                    "after the physical-attempt claim. The claimed attempt "
                    "remains nonterminal and the provider execution boundary "
                    "was not entered."
                ),
                log=log, decision=decision,
                invocation_id=invocation_id,
                action_id=claimed.action_id,
                attempt_id=attempt_id,
                action_state=ActionState.IN_FLIGHT.value,
            )

        trusted_evidence_refs: tuple[str, ...] = ()
        try:
            try:
                trusted_evidence_refs = self._persist_attempt_evidence(
                    claimed, normalized)
            except Exception:
                normalized = self._unknown_effect(
                    spec, "evidence.storage_failed")
                trusted_evidence_refs = ()
            candidate = self._effect_candidate(
                claimed,
                normalized,
                trusted_evidence_refs=trusted_evidence_refs,
            )
        except Exception:
            try:
                candidate = self._effect_candidate(
                    claimed,
                    self._unknown_effect(spec, "tool.normalization_failed"),
                )
            except Exception:
                if cancellation is not None:
                    raise cancellation
                return self._journal_failure(
                    tool_spec.name, redacted, elapsed(),
                    kind="action_result_publication_uncertain",
                    message=(
                        "The effect ran, but its durable result could not be "
                        "constructed."
                    ),
                    log=log, decision=decision,
                    invocation_id=invocation_id,
                    action_id=claimed.action_id,
                    attempt_id=attempt_id,
                    action_state=ActionState.IN_FLIGHT.value,
                )
        published = self._publish_and_reload(store, claimed, candidate)
        if cancellation is not None:
            if published is None:
                log.warning(
                    Events.TOOL_FAILED,
                    reason="action_result_publication_uncertain",
                    action_id=claimed.action_id,
                    attempt_id=attempt_id)
            raise cancellation
        if published is None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_result_publication_uncertain",
                message="The effect ran, but durable result publication is uncertain.",
                log=log, decision=decision, invocation_id=invocation_id,
                action_id=claimed.action_id, attempt_id=attempt_id,
                action_state=ActionState.IN_FLIGHT.value)
        return self._durable_invocation(
            tool_spec.name, redacted, published, decision,
            invocation_id, attempt_id, elapsed(), log)

    def _execute_claimed_effect(
        self,
        *,
        store: ActionStore,
        claimed: ActionRecord,
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        adapter: ToolActionAdapter,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        attempt_id: str,
        current_approval_receipt: DurableApprovalReceipt | None,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[
        NormalizedActionEffect,
        BaseException | None,
        Decision,
    ]:
        """Perform final trusted checks and enter one physical effect seam.

        For the first attempt of a deliberate risky repeat, the predecessor's
        per-action guard is held from final current-snapshot verification
        through the physical call.  This prevents reconciliation from changing
        predecessor truth in the check/execute gap without holding the general
        ActionStore mutex or any ApprovalStore mutex across provider I/O.
        """
        first_risky_attempt = (
            claimed.duplicate_risk_acknowledgement is not None
            and len(claimed.attempts) == 1
        )
        predecessor_guard = (
            store.execution_guard(claimed.predecessor_action_id)
            if first_risky_attempt
            and claimed.predecessor_action_id is not None
            else nullcontext()
        )
        try:
            with predecessor_guard, self.registry.execution_lease(
                tool_spec.name,
                tool,
            ):
                final_decision = self._validate_current_execution_authority(
                    record=claimed,
                    tool=tool,
                    tool_spec=tool_spec,
                    adapter=adapter,
                    spec=spec,
                    prepared=prepared,
                    current_approval_receipt=current_approval_receipt,
                )
                if spec.effect_class is (
                        ActionEffectClass.EXTERNALLY_IDEMPOTENT):
                    try:
                        # Generic final fail-closed validation remains inside
                        # the exact predecessor snapshot boundary. The provider
                        # then validates again at its actual request seam.
                        self._validate_external_contract_for_record(
                            tool, tool_spec, spec, claimed)
                    except Exception as exc:
                        raise _ExternalContractBlocked() from exc
                if first_risky_attempt:
                    # Risk approval is deliberately the last host authority
                    # validation before the physical tool/provider boundary.
                    self._validate_current_duplicate_risk_permission(
                        record=claimed,
                        tool_spec=tool_spec,
                        prepared=prepared,
                        current_approval_receipt=current_approval_receipt,
                    )
                    self._validate_duplicate_risk_record(
                        store=store,
                        record=claimed,
                        tool=tool,
                        tool_spec=tool_spec,
                        spec=spec,
                        prepared=prepared,
                    )

                cancellation: BaseException | None = None
                if type(tool) is AuthenticatedActionTool:
                    normalized, cancellation = self._run_authenticated_action(
                        store=store, claimed=claimed, tool=tool, tool_spec=tool_spec,
                        cleaned=cleaned, adapter=adapter, spec=spec, prepared=prepared,
                        current_approval_receipt=current_approval_receipt, checkpoint=checkpoint)
                    return normalized, cancellation, final_decision
                if type(tool) is WriteFileTool:
                    normalized, cancellation = self._run_filesystem_write(
                        store=store, claimed=claimed, tool=tool, tool_spec=tool_spec,
                        cleaned=cleaned, adapter=adapter, spec=spec, prepared=prepared,
                        current_approval_receipt=current_approval_receipt, checkpoint=checkpoint)
                    return normalized, cancellation, final_decision
                try:
                    # Conservative physical mutation seam. It is entered once.
                    if spec.effect_class is (
                            ActionEffectClass.EXTERNALLY_IDEMPOTENT):
                        if claimed.idempotency is None:
                            raise ValueError(
                                "claimed action lacks idempotency metadata")
                        permit = ActionExecutionPermit(
                            action_id=claimed.action_id,
                            attempt_id=attempt_id,
                            tool_name=tool_spec.name,
                            tool_version=tool_spec.version,
                            idempotency=claimed.idempotency,
                        ).validate()
                        request = self._external_idempotency_request(
                            claimed.action_id, spec, tool_spec)
                        result = self._action_adapters.execute_external(
                            tool,
                            request,
                            cleaned,
                            self._context(
                                tool_spec.name,
                                action_execution_permit=permit,
                            ),
                            permit,
                            tool_spec=tool_spec,
                        )
                    else:
                        result = tool.run(
                            cleaned, self._context(tool_spec.name))
                    if not isinstance(result, ToolResult):
                        result = ToolResult.success(result)
                    normalized = adapter.normalize_effect(result)
                    if not isinstance(normalized, NormalizedActionEffect):
                        raise TypeError("adapter normalization type")
                    normalized.validate(spec)
                except CancellationRequested as exc:
                    cancellation = exc
                    normalized = self._unknown_effect(
                        spec, "tool.cancelled")
                except Exception:
                    normalized = self._unknown_effect(
                        spec, "tool.exception")
                return normalized, cancellation, final_decision
        except ToolRegistryLeaseError as exc:
            external = spec.effect_class is (
                ActionEffectClass.EXTERNALLY_IDEMPOTENT
            )
            raise _CurrentAuthorityBlocked(
                ("external_idempotency_contract_unavailable" if external else
                 "action_adapter_invalid"),
                ("The external idempotency registration changed before "
                 "execution." if external else
                 "The trusted tool registration changed before execution."),
            ) from exc
        except (
            _CurrentAuthorityBlocked,
            _DuplicateRiskBlocked,
            _ExternalContractBlocked,
            _CredentialActionNotInvoked,
        ):
            raise
        except Exception as exc:
            if first_risky_attempt:
                raise _DuplicateRiskBlocked(
                    "duplicate_risk_predecessor_changed",
                    "The predecessor execution boundary could not be "
                    "verified before the successor effect seam.",
                    request_id=(
                        claimed.duplicate_risk_acknowledgement
                        .approval_request_id
                        if claimed.duplicate_risk_acknowledgement is not None
                        else None),
                    action_id=claimed.action_id,
                ) from exc
            raise

    @staticmethod
    def _create_or_prove(
        store: ActionStore, proposed: ActionRecord,
    ) -> ActionRecord | None:
        try:
            created = store.create(proposed)
            if not isinstance(created, ActionRecord):
                return None
            created.validate()
            return (created if ToolExecutor._is_create_owner(
                created, proposed) else None)
        except Exception:
            try:
                loaded = store.load(proposed.action_id)
                loaded.validate()
                return (loaded if ToolExecutor._is_create_owner(
                    loaded, proposed) else None)
            except Exception:
                return None

    def _create_fresh_action(
        self,
        *,
        store: ActionStore,
        tool: Any,
        tool_spec: ToolSpec,
        spec: ActionAdapterSpec,
        prepared: PreparedActionIntent,
        trusted_context: ActionInvocationContext,
        public_intent_digest: str,
        provenance: Any,
        authority: AuthorityReceipt,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        approved_action_id: str | None,
        predecessor_action_id: str | None = None,
        duplicate_risk_acknowledgement: (
            DuplicateRiskAcknowledgement | None
        ) = None,
    ) -> tuple[ActionRecord | None, bool, str | None, str | None]:
        """Create once per dedup identity, including provider key minting."""
        dedup = (
            (trusted_context.deduplication_scope,
             trusted_context.deduplication_key)
            if trusted_context.deduplication_scope is not None
            else None
        )
        proposed_action_id = approved_action_id
        try:
            supports_creation_guard = (
                dedup is not None
                and callable(getattr(store, "creation_guard", None))
                and callable(getattr(store, "find_by_deduplication", None))
            )
            if dedup is not None and spec.effect_class is (
                    ActionEffectClass.EXTERNALLY_IDEMPOTENT) and not (
                        supports_creation_guard):
                return (
                    None, False, "action_create_unverified",
                    proposed_action_id,
                )
            creation_guard = (
                store.creation_guard(*dedup)
                if supports_creation_guard and dedup is not None
                else nullcontext()
            )
            with creation_guard:
                if supports_creation_guard and dedup is not None:
                    existing = store.find_by_deduplication(*dedup)
                    if existing is not None:
                        existing.validate()
                        if not self._matches_new_request(
                            existing,
                            prepared=prepared,
                            spec=spec,
                            tool_spec=tool_spec,
                            mutation_capabilities=mutation_capabilities,
                            reconciliation_capabilities=(
                                reconciliation_capabilities
                            ),
                            trusted_context=trusted_context,
                        ):
                            return (
                                None, False, "action_create_unverified",
                                existing.action_id,
                            )
                        return existing, False, None, existing.action_id

                if proposed_action_id is None:
                    proposed_action_id = self._id_factory("act")

                protected_intent_ref: str | None = None
                effective_intent_digest = public_intent_digest
                if prepared.protected_payload is not None:
                    try:
                        if self._protected_payload_store is None:
                            raise ValueError("protected payload store unavailable")
                        protected = self._protected_payload_store.create(
                            proposed_action_id,
                            prepared.protected_payload,
                            purpose=ProtectedPayloadPurpose.ACTION_INTENT,
                        )
                        exact_protected = self._protected_payload_store.load(
                            protected.reference,
                            action_id=proposed_action_id,
                            purpose=ProtectedPayloadPurpose.ACTION_INTENT,
                            expected_intent_digest=protected.intent_digest,
                        )
                        if exact_protected != protected:
                            raise ValueError("protected payload reload mismatch")
                        protected_intent_ref = protected.reference
                        effective_intent_digest = protected.intent_digest
                    except Exception:
                        return (
                            None, False, "protected_payload_unverified",
                            proposed_action_id,
                        )

                idempotency: IdempotencyContract | None = None
                if spec.effect_class is (
                        ActionEffectClass.EXTERNALLY_IDEMPOTENT):
                    request = self._external_idempotency_request(
                        proposed_action_id, spec, tool_spec)
                    try:
                        candidate, allows_indefinite = (
                            self._action_adapters.establish_external_idempotency(
                                tool, request, tool_spec=tool_spec)
                        )
                        idempotency = self._require_current_contract(
                            candidate,
                            allows_indefinite=allows_indefinite,
                        )
                    except Exception:
                        return (
                            None, False,
                            "external_idempotency_contract_unavailable",
                            proposed_action_id,
                        )

                # Provider/protected setup may take long enough for the
                # one-shot risk horizon or predecessor snapshot to change.
                # Reauthenticate after that work and immediately before
                # constructing/publishing the successor. ActionStore.create()
                # then atomically rechecks current predecessor truth.
                if duplicate_risk_acknowledgement is not None:
                    self._validate_duplicate_risk_before_create(
                        store=store,
                        tool=tool,
                        tool_spec=tool_spec,
                        spec=spec,
                        prepared=prepared,
                        public_intent_digest=public_intent_digest,
                        mutation_capabilities=mutation_capabilities,
                        reconciliation_capabilities=(
                            reconciliation_capabilities),
                        successor_action_id=proposed_action_id,
                        acknowledgement=duplicate_risk_acknowledgement,
                    )

                try:
                    if spec.effect_class is None:
                        raise ValueError("action effect class is unavailable")
                    proposed = new_action(
                        action_kind=spec.action_kind,
                        tool_name=tool_spec.name,
                        tool_version=tool_spec.version,
                        eligibility=spec.eligibility,
                        mutation_capabilities=mutation_capabilities,
                        reconciliation_capabilities=(
                            reconciliation_capabilities
                        ),
                        effect_class=spec.effect_class,
                        intent_digest=effective_intent_digest,
                        intent_ref=protected_intent_ref,
                        target_category=prepared.target_category,
                        target_fingerprint=prepared.target_fingerprint,
                        provenance=provenance,
                        authority=authority,
                        idempotency=idempotency,
                        action_id=proposed_action_id,
                        deduplication_scope=trusted_context.deduplication_scope,
                        deduplication_key=trusted_context.deduplication_key,
                        predecessor_action_id=predecessor_action_id,
                        duplicate_risk_acknowledgement=(
                            duplicate_risk_acknowledgement),
                        clock=self._clock,
                        id_factory=self._id_factory,
                    )
                except Exception:
                    return (
                        None, False, "action_record_invalid",
                        proposed_action_id,
                    )
                created = self._create_or_prove(store, proposed)
                if created is None:
                    return (
                        None, False, "action_create_unverified",
                        proposed_action_id,
                    )
                return created, created == proposed, None, proposed_action_id
        except _DuplicateRiskBlocked:
            raise
        except Exception:
            return None, False, "action_create_unverified", proposed_action_id

    @staticmethod
    def _external_idempotency_request(
        action_id: str,
        spec: ActionAdapterSpec,
        tool_spec: ToolSpec,
    ) -> ExternalIdempotencyRequest:
        return ExternalIdempotencyRequest(
            action_id=action_id,
            action_kind=spec.action_kind,
            tool_name=tool_spec.name,
            tool_version=tool_spec.version,
            effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT,
        ).validate()

    def _require_current_contract(
        self,
        candidate: Any,
        *,
        allows_indefinite: bool,
        stored: IdempotencyContract | None = None,
    ) -> IdempotencyContract:
        if not isinstance(candidate, IdempotencyContract):
            raise TypeError("provider returned an invalid contract type")
        candidate.validate()
        if stored is not None and candidate != stored:
            raise ValueError("provider rewrote the durable contract")
        if candidate.valid_until is None:
            if not allows_indefinite:
                raise ValueError("indefinite guarantee was not explicit")
        elif self._clock.now() >= candidate.valid_until:
            raise ValueError("external idempotency guarantee expired")
        return candidate

    def _validate_external_contract_for_record(
        self,
        tool: Any,
        tool_spec: ToolSpec,
        spec: ActionAdapterSpec,
        record: ActionRecord,
    ) -> IdempotencyContract:
        if record.idempotency is None:
            raise ValueError("durable idempotency metadata is unavailable")
        if getattr(tool, "spec", None) != tool_spec:
            raise ValueError("registered tool identity or shape changed")
        request = self._external_idempotency_request(
            record.action_id, spec, tool_spec)
        candidate, allows_indefinite = (
            self._action_adapters.validate_external_idempotency(
                tool,
                request,
                record.idempotency,
                tool_spec=tool_spec,
            )
        )
        return self._require_current_contract(
            candidate,
            allows_indefinite=allows_indefinite,
            stored=record.idempotency,
        )

    @staticmethod
    def _is_create_owner(
        record: ActionRecord,
        proposed: ActionRecord,
    ) -> bool:
        if not deduplication_compatible(record, proposed):
            return False
        if record.action_id == proposed.action_id:
            return record.intent_ref == proposed.intent_ref
        identity = proposed.deduplication_identity
        return identity is not None and record.deduplication_identity == identity

    def _matches_resume_request(
        self,
        record: ActionRecord,
        *,
        prepared: PreparedActionIntent,
        spec: ActionAdapterSpec,
        tool_spec: ToolSpec,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        trusted_context: ActionInvocationContext,
    ) -> bool:
        requested_dedup = (
            (trusted_context.deduplication_scope,
             trusted_context.deduplication_key)
            if trusted_context.deduplication_scope is not None
            else None
        )
        return (
            record.action_id == trusted_context.action_id
            and record.action_kind == spec.action_kind
            and record.tool_name == tool_spec.name
            and record.tool_version == tool_spec.version
            and record.eligibility is spec.eligibility
            and record.effect_class is spec.effect_class
            and record.mutation_capabilities == mutation_capabilities
            and record.reconciliation_capabilities == (
                reconciliation_capabilities
            )
            and self._protected_intent_matches(record, prepared)
            and record.target_category == prepared.target_category
            and record.target_fingerprint == prepared.target_fingerprint
            and (
                requested_dedup is None
                or record.deduplication_identity == requested_dedup
            )
        )

    def _matches_new_request(
        self,
        record: ActionRecord,
        *,
        prepared: PreparedActionIntent,
        spec: ActionAdapterSpec,
        tool_spec: ToolSpec,
        mutation_capabilities: tuple[str, ...],
        reconciliation_capabilities: tuple[str, ...],
        trusted_context: ActionInvocationContext,
    ) -> bool:
        requested_dedup = (
            trusted_context.deduplication_scope,
            trusted_context.deduplication_key,
        )
        return (
            record.action_kind == spec.action_kind
            and record.tool_name == tool_spec.name
            and record.tool_version == tool_spec.version
            and record.eligibility is spec.eligibility
            and record.effect_class is spec.effect_class
            and record.mutation_capabilities == mutation_capabilities
            and record.reconciliation_capabilities == (
                reconciliation_capabilities
            )
            and self._protected_intent_matches(record, prepared)
            and record.target_category == prepared.target_category
            and record.target_fingerprint == prepared.target_fingerprint
            and record.deduplication_identity == requested_dedup
        )

    @staticmethod
    def _has_checked_retry_proof(record: ActionRecord) -> bool:
        if not record.attempts or not record.transitions:
            return False
        transition = record.transitions[-1]
        return (
            transition.to_state is ActionState.AUTHORIZED
            and (
                (
                    transition.proof is (
                        ActionTransitionProof.DEFINITIVE_NO_EFFECT
                    )
                    and transition.effect_verdict is (
                        EffectVerdict.CONFIRMED_NOT_APPLIED
                    )
                )
                or (
                    transition.proof is (
                        ActionTransitionProof.EXTERNAL_IDEMPOTENCY_GUARANTEE
                    )
                    and transition.effect_verdict in {
                        EffectVerdict.OUTCOME_UNKNOWN,
                        EffectVerdict.CONFIRMED_NOT_APPLIED,
                    }
                )
            )
        )

    @staticmethod
    def _same_action_lineage(
        record: ActionRecord,
        prior: ActionRecord,
    ) -> bool:
        return (
            record.action_id == prior.action_id
            and deduplication_compatible(record, prior)
            and record.provenance == prior.provenance
            and record.authority == prior.authority
            and record.created_at == prior.created_at
        )

    @staticmethod
    def _load_exact_claim(
        store: ActionStore,
        *,
        expected_record: ActionRecord,
        expected_version: int,
        attempt_id: str,
        invocation_id: str,
        owner_id: str,
        owner_epoch: int,
        transition_id: str,
    ) -> ActionRecord | None:
        try:
            record = store.load(expected_record.action_id)
            record.validate()
        except Exception:
            return None
        if not ToolExecutor._same_action_lineage(
                record, expected_record) or \
                record.version != expected_version or \
                record.state is not ActionState.IN_FLIGHT or not record.attempts:
            return None
        attempt = record.attempts[-1]
        if attempt.attempt_id != attempt_id or \
                attempt.invocation_id != invocation_id or \
                attempt.owner_id != owner_id or \
                attempt.owner_epoch != owner_epoch or \
                attempt.status is not ActionAttemptStatus.CLAIMED:
            return None
        transition = record.transitions[-1]
        if transition.transition_id != transition_id or \
                transition.attempt_id != attempt_id:
            return None
        return record

    def _publish_preclaim_cancellation(
        self, store: ActionStore, current: ActionRecord,
    ) -> None:
        try:
            candidate = current.transition(
                ActionState.CANCELLED,
                transition_id=new_action_transition_id(
                    id_factory=self._id_factory),
                at=self._clock.now())
            self._publish_and_reload(store, current, candidate)
        except Exception:
            return

    def _publish_claimed_cancellation(
        self, store: ActionStore, claimed: ActionRecord,
    ) -> None:
        try:
            if len(claimed.attempts) > 1:
                # A cancelled retry cannot terminalize older attempt history.
                # Prove only this fresh claim harmless and preserve retry state.
                candidate = claimed.transition(
                    ActionState.AUTHORIZED,
                    transition_id=new_action_transition_id(
                        id_factory=self._id_factory),
                    at=self._clock.now(),
                    attempt_status=ActionAttemptStatus.NOT_INVOKED,
                    effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
                    proof=ActionTransitionProof.DEFINITIVE_NO_EFFECT)
            else:
                candidate = claimed.transition(
                    ActionState.CANCELLED,
                    transition_id=new_action_transition_id(
                        id_factory=self._id_factory),
                    at=self._clock.now(),
                    attempt_status=ActionAttemptStatus.NOT_INVOKED,
                    proof=ActionTransitionProof.LIVE_OWNER_NOT_INVOKED)
            self._publish_and_reload(store, claimed, candidate)
        except Exception:
            return

    def _publish_confirmed_no_effect(
        self,
        store: ActionStore,
        current: ActionRecord,
        *,
        attempt_status: ActionAttemptStatus | None,
        result_class: str,
        summary: str,
    ) -> ActionRecord | None:
        try:
            outcome = ActionOutcome(
                verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
                result_class=result_class,
                summary=summary)
            candidate = current.transition(
                ActionState.FAILED,
                transition_id=new_action_transition_id(
                    id_factory=self._id_factory),
                at=self._clock.now(),
                attempt_status=attempt_status,
                effect_verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
                terminal_outcome=outcome)
            return self._publish_and_reload(store, current, candidate)
        except Exception:
            return None

    def _effect_candidate(
        self,
        claimed: ActionRecord,
        normalized: NormalizedActionEffect,
        *,
        trusted_evidence_refs: tuple[str, ...] = (),
    ) -> ActionRecord:
        if not isinstance(trusted_evidence_refs, tuple) or any(
                not is_evidence_reference(reference)
                for reference in trusted_evidence_refs):
            raise ValueError("trusted evidence references are invalid")
        verdict = normalized.verdict
        if verdict is EffectVerdict.CONFIRMED_APPLIED:
            state = ActionState.SUCCEEDED
        elif verdict in {
            EffectVerdict.CONFIRMED_NOT_APPLIED,
            EffectVerdict.CONFIRMED_PARTIAL,
        }:
            state = ActionState.FAILED
        else:
            state = ActionState.RECONCILE_REQUIRED
        outcome = (ActionOutcome(
            verdict=verdict,
            result_class=normalized.result_class,
            summary=normalized.summary,
            partial_categories=normalized.partial_categories)
            if verdict is not EffectVerdict.OUTCOME_UNKNOWN else None)
        reconciliation = (ReconciliationMetadata(
            disposition=normalized.reconciliation_disposition)
            if verdict is EffectVerdict.OUTCOME_UNKNOWN else None)
        return claimed.transition(
            state,
            transition_id=new_action_transition_id(id_factory=self._id_factory),
            at=self._clock.now(),
            attempt_status=ActionAttemptStatus.INVOKED,
            effect_verdict=verdict,
            evidence_refs=normalized.evidence_refs + trusted_evidence_refs,
            reconciliation=reconciliation,
            terminal_outcome=outcome)

    def _persist_attempt_evidence(
        self,
        claimed: ActionRecord,
        normalized: NormalizedActionEffect,
    ) -> tuple[str, ...]:
        if not normalized.protected_evidence:
            return ()
        if self._evidence_store is None or not claimed.attempts:
            raise ValueError("protected evidence storage is unavailable")
        attempt_id = claimed.attempts[-1].attempt_id
        references: list[str] = []
        for material in normalized.protected_evidence:
            stored = self._evidence_store.create_attempt_evidence(
                claimed.action_id,
                attempt_id,
                claimed.intent_digest,
                material,
            )
            exact = self._evidence_store.load_attempt_evidence(
                stored.reference,
                action_id=claimed.action_id,
                attempt_id=attempt_id,
                intent_digest=claimed.intent_digest,
            )
            if exact != stored:
                raise ValueError("protected evidence reload mismatch")
            references.append(stored.reference)
        return tuple(references)

    def _persist_reconciliation_evidence(
        self,
        current: ActionRecord,
        claim: ActionRecoveryClaim,
        result: ActionReconciliationResult,
    ) -> tuple[str, ...]:
        if not result.protected_evidence:
            return ()
        if self._evidence_store is None:
            raise ValueError("protected evidence storage is unavailable")
        references: list[str] = []
        for material in result.protected_evidence:
            stored = self._evidence_store.create_reconciliation_evidence(
                current.action_id,
                claim.claim_epoch,
                current.intent_digest,
                material,
            )
            exact = self._evidence_store.load_reconciliation_evidence(
                stored.reference,
                action_id=current.action_id,
                claim_epoch=claim.claim_epoch,
                intent_digest=current.intent_digest,
            )
            if exact != stored:
                raise ValueError("protected evidence reload mismatch")
            references.append(stored.reference)
        return tuple(references)

    @staticmethod
    def _publish_and_reload(
        store: ActionStore,
        current: ActionRecord,
        candidate: ActionRecord,
    ) -> ActionRecord | None:
        try:
            store.compare_and_swap(
                current.action_id, current.version, candidate)
        except Exception:
            pass
        try:
            loaded = store.load(current.action_id)
            loaded.validate()
            return loaded if loaded == candidate else None
        except Exception:
            return None

    @staticmethod
    def _unknown_effect(
        spec: ActionAdapterSpec,
        result_class: str,
    ) -> NormalizedActionEffect:
        disposition = (
            ReconciliationDisposition.PROBE_LATER
            if spec.reconciliation_capabilities
            else ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
        return NormalizedActionEffect(
            verdict=EffectVerdict.OUTCOME_UNKNOWN,
            result_class=result_class,
            summary="The effect outcome is unknown and requires verification.",
            reconciliation_disposition=disposition,
        ).validate(spec)

    def _attached_invocation(
        self,
        tool_name: str,
        redacted: dict[str, str],
        record: ActionRecord,
        decision: Decision | None,
        invocation_id: str,
        duration: float,
        log: Any,
    ) -> ToolInvocation:
        kinds = {
            ActionState.SUCCEEDED: "action_succeeded",
            ActionState.FAILED: "action_failed",
            ActionState.CANCELLED: "action_cancelled",
            ActionState.IN_FLIGHT: "action_in_progress",
            ActionState.RECONCILE_REQUIRED: "action_reconciliation_required",
        }
        kind = kinds.get(record.state)
        if record.state is ActionState.AUTHORIZED:
            kind = ("action_retry_recovery_required"
                    if record.attempts else "action_pending")
        assert kind is not None
        return self._durable_invocation(
            tool_name, redacted, record, decision, invocation_id,
            record.attempts[-1].attempt_id if record.attempts else None,
            duration, log, kind_override=kind)

    def _durable_invocation(
        self,
        tool_name: str,
        redacted: dict[str, str],
        record: ActionRecord,
        decision: Decision | None,
        invocation_id: str,
        attempt_id: str | None,
        duration: float,
        log: Any,
        *,
        kind_override: str | None = None,
    ) -> ToolInvocation:
        kind = kind_override or {
            ActionState.SUCCEEDED: "action_succeeded",
            ActionState.FAILED: "action_failed",
            ActionState.CANCELLED: "action_cancelled",
            ActionState.IN_FLIGHT: "action_in_progress",
            ActionState.RECONCILE_REQUIRED: "action_reconciliation_required",
            ActionState.AUTHORIZED: "action_pending",
        }[record.state]
        if record.state is ActionState.SUCCEEDED:
            assert record.terminal_outcome is not None
            result = ToolResult.success(
                record.terminal_outcome.summary,
                kind=kind,
                result_class=record.terminal_outcome.result_class,
                action_id=record.action_id)
            log.info(
                Events.TOOL_SUCCEEDED,
                duration_ms=round(duration, 3),
                action_id=record.action_id,
                action_state=record.state.value)
        else:
            summary = (record.terminal_outcome.summary
                       if record.terminal_outcome is not None else {
                           ActionState.CANCELLED: (
                               "The action was cancelled before execution."),
                           ActionState.IN_FLIGHT: (
                               "Action execution or verification is in progress."),
                           ActionState.RECONCILE_REQUIRED: (
                               "The action may have completed and requires verification."),
                           ActionState.AUTHORIZED: (
                               "The action is pending execution ownership."),
                       }.get(record.state, "The action did not complete."))
            result_class = (record.terminal_outcome.result_class
                            if record.terminal_outcome is not None else None)
            result = ToolResult.failure(
                summary, kind=kind, result_class=result_class,
                action_id=record.action_id)
            log.warning(
                Events.TOOL_FAILED,
                reason=kind,
                duration_ms=round(duration, 3),
                action_id=record.action_id,
                action_state=record.state.value)
        return ToolInvocation(
            tool_name, redacted, result, duration, decision,
            invocation_id=invocation_id,
            action_id=record.action_id,
            attempt_id=attempt_id,
            action_state=record.state.value,
            approval_request_id=record.authority.approval_request_id)

    def _published_or_uncertain(
        self,
        tool_name: str,
        redacted: dict[str, str],
        published: ActionRecord | None,
        decision: Decision,
        invocation_id: str,
        attempt_id: str | None,
        duration: float,
        log: Any,
        *,
        fallback_kind: str,
        fallback_action_id: str | None = None,
    ) -> ToolInvocation:
        if published is not None:
            return self._durable_invocation(
                tool_name, redacted, published, decision,
                invocation_id, attempt_id, duration, log,
                kind_override=fallback_kind)
        return self._journal_failure(
            tool_name, redacted, duration,
            kind="action_result_publication_uncertain",
            message="Durable no-effect publication is uncertain.",
            log=log, decision=decision, invocation_id=invocation_id,
            action_id=fallback_action_id, attempt_id=attempt_id)

    @staticmethod
    def _safe_pre_action_failure(
        tool_name: str,
        cleaned: Mapping[str, Any],
        duration: float,
        log: Any,
        kind: str,
        message: str,
        **safe_metadata: Any,
    ) -> ToolInvocation:
        redacted = _redacted_args(cleaned)
        log.warning(Events.TOOL_FAILED, reason=kind)
        return ToolInvocation(
            tool_name,
            redacted,
            ToolResult.failure(message, kind=kind, **safe_metadata),
            duration)

    @staticmethod
    def _journal_failure(
        tool_name: str,
        redacted: dict[str, str],
        duration: float,
        *,
        kind: str,
        message: str,
        log: Any,
        decision: Decision | None = None,
        invocation_id: str | None = None,
        action_id: str | None = None,
        attempt_id: str | None = None,
        action_state: str | None = None,
        approval_request_id: str | None = None,
    ) -> ToolInvocation:
        log.warning(
            Events.TOOL_FAILED, reason=kind,
            action_id=action_id, attempt_id=attempt_id)
        return ToolInvocation(
            tool_name, redacted, ToolResult.failure(message, kind=kind),
            duration, decision, invocation_id=invocation_id,
            action_id=action_id, attempt_id=attempt_id,
            action_state=action_state,
            approval_request_id=approval_request_id)
