"""The sole validation, permission, journal, and tool-execution boundary.

Read-only tools and statically registered ephemeral controls retain the
original low-latency path. Every journal-required action must publish a
durable intent and exact physical-attempt claim before :meth:`Tool.run` is
entered. ActionJournal records are evidence; they never grant authority.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agentcore.action_journal import (
    ActionAdapterRegistry,
    ActionAdapterRegistrationError,
    ActionAdapterSpec,
    ActionAttemptStatus,
    ActionEffectClass,
    ActionInvocationContext,
    ActionJournalEligibility,
    ActionOutcome,
    ActionRecord,
    ActionState,
    ActionStore,
    ActionTransitionProof,
    AuthorityReceipt,
    EffectVerdict,
    NormalizedActionEffect,
    PreparedActionIntent,
    ReconciliationDisposition,
    ReconciliationMetadata,
    ToolActionAdapter,
    authority_binding_digest,
    deduplication_compatible,
    new_action,
    new_action_attempt_id,
    new_action_transition_id,
    permission_policy_fingerprint,
)
from agentcore.errors import (
    AgentError,
    PermissionDeniedError,
    ToolValidationError,
)
from agentcore.fs.sensitive import DEFAULT_SENSITIVE_POLICY
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.permissions.levels import READ_ONLY_CAPABILITIES
from agentcore.permissions.policy import Decision, PermissionPolicy
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.base import ToolContext, ToolResult, ToolSpec
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


_REDACTED = "***"
_SENSITIVE_FAILURE_REASONS = frozenset({"sensitive", "sensitive_target"})


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
        }


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
        clock: Clock | None = None,
        id_factory: Callable[[str], str] = new_id,
        action_owner_id: str | None = None,
        action_owner_epoch: int = 1,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.workspace = workspace
        self.workspace_dir = (str(workspace.root) if workspace is not None
                              else workspace_dir)
        self._tool_settings = dict(tool_settings or {})
        self._log = logger or get_logger("tools.executor")
        self._action_store = action_store
        self._action_adapters = (
            action_adapters
            if action_adapters is not None
            else ActionAdapterRegistry()
        )
        self._clock = clock or SystemClock()
        self._id_factory = id_factory
        self._action_owner_id = action_owner_id or f"process:{os.getpid()}"
        self._action_owner_epoch = action_owner_epoch

    def _context(self, tool_name: str) -> ToolContext:
        return ToolContext(
            config=dict(self._tool_settings.get(tool_name, {})),
            logger=self._log.bind(tool=tool_name),
            workspace_dir=self.workspace_dir,
            workspace=self.workspace,
        )

    def invoke(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None = None,
        *,
        checkpoint: Callable[[], None] | None = None,
        action_context: ActionInvocationContext | None = None,
        **context_meta: Any,
    ) -> ToolInvocation:
        """Validate, authorize, and invoke through the appropriate boundary."""
        supplied = dict(args or {})
        tool = self.registry.get(tool_name)
        tool_spec = tool.spec
        mutation = not tool_spec.read_only
        log = (self._log.bind(tool=tool_name) if mutation
               else self._log.bind(tool=tool_name, **context_meta))
        logged_args = (_redacted_declared_args(supplied, tool_spec) if mutation
                       else _safe_args_for_log(supplied))
        if mutation:
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
                error=("invalid mutation arguments" if mutation else exc.message),
                details=({} if mutation else exc.details),
            )
            return ToolInvocation(
                tool_name,
                (_redacted_declared_args(supplied, tool_spec)
                 if mutation else supplied),
                ToolResult.failure(
                    "Tool arguments were invalid." if mutation else exc.message,
                    kind="validation",
                    **({} if mutation else exc.details),
                ),
                elapsed(),
            )
        except Exception:
            if not mutation:
                raise
            log.warning(
                Events.TOOL_FAILED,
                reason="validation",
                error="mutation argument validation failed",
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
        return self._invoke_journaled(
            tool=tool,
            tool_spec=tool_spec,
            cleaned=cleaned,
            adapter=adapter,
            spec=spec,
            checkpoint=checkpoint,
            action_context=action_context,
            log=log,
            elapsed=elapsed,
        )

    def _invoke_fast(
        self,
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        checkpoint: Callable[[], None] | None,
        log: Any,
        elapsed: Callable[[], float],
    ) -> ToolInvocation:
        """Original validation/checkpoint/policy/checkpoint/run sequence."""
        if checkpoint is not None:
            checkpoint()
        decision = self.policy.check(
            tool_spec.capabilities, description=tool_spec.description)
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
                cleaned,
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
            result = tool.run(cleaned, self._context(tool_spec.name))
            if not isinstance(result, ToolResult):
                result = ToolResult.success(result)
        except CancellationRequested:
            raise
        except AgentError as exc:
            log.warning(Events.TOOL_FAILED, reason="agent_error",
                        error=exc.message, details=exc.details)
            result = ToolResult.failure(
                exc.message, kind=type(exc).__name__,
                retryable=exc.retryable, **exc.details)
        except Exception as exc:  # noqa: BLE001 - legacy fast-path envelope
            log.exception(Events.TOOL_FAILED, reason="unhandled_exception",
                          error=str(exc))
            result = ToolResult.failure(
                f"{type(exc).__name__}: {exc}", kind="unhandled_exception")

        duration = elapsed()
        if result.ok:
            log.info(Events.TOOL_SUCCEEDED, duration_ms=round(duration, 3))
        else:
            reason = result.metadata.get("reason") if result.metadata else None
            if reason in _SENSITIVE_FAILURE_REASONS:
                log.warning(Events.TOOL_FAILED, reason=reason,
                            duration_ms=round(duration, 3))
            else:
                log.warning(Events.TOOL_FAILED, error=result.error,
                            duration_ms=round(duration, 3))
        return ToolInvocation(
            tool_spec.name, cleaned, result, duration, decision)

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

        if prepared.requires_protected_payload or prepared.intent_ref is not None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="protected_payload_unavailable",
                message="Protected action payload storage is not available.",
                log=log, invocation_id=invocation_id)

        if checkpoint is not None:
            checkpoint()
        decision = self.policy.check(
            tool_spec.capabilities,
            description=(prepared.approval_description
                         or tool_spec.description),
        )
        log.debug(Events.PERMISSION_CHECKED, **decision.to_dict())
        if not decision.allowed:
            log.warning(Events.TOOL_DENIED, **decision.to_dict())
            return ToolInvocation(
                tool_spec.name,
                redacted,
                ToolResult.failure(
                    "Permission was not granted for this action.",
                    kind="permission_denied"),
                elapsed(), decision, invocation_id=invocation_id)
        if checkpoint is not None:
            checkpoint()

        if self._action_store is None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_store_unavailable",
                message="The durable action journal is unavailable.",
                log=log, decision=decision, invocation_id=invocation_id)
        if spec.effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="external_idempotency_contract_unavailable",
                message="Stable external idempotency ownership is not available.",
                log=log, decision=decision, invocation_id=invocation_id)

        mutation_capabilities = tuple(sorted(
            str(capability) for capability in tool_spec.capabilities
            if capability not in READ_ONLY_CAPABILITIES))
        reconciliation_capabilities = tuple(sorted(
            str(capability) for capability in spec.reconciliation_capabilities))
        try:
            policy_fingerprint = permission_policy_fingerprint(self.policy)
            authority_scope = f"live:{invocation_id}"
            receipt = AuthorityReceipt(
                receipt_id=self._id_factory("auth"),
                binding_digest=authority_binding_digest(
                    action_kind=spec.action_kind,
                    intent_digest=prepared.intent_digest,
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
            assert spec.effect_class is not None
            proposed = new_action(
                action_kind=spec.action_kind,
                tool_name=tool_spec.name,
                tool_version=tool_spec.version,
                eligibility=spec.eligibility,
                mutation_capabilities=mutation_capabilities,
                reconciliation_capabilities=reconciliation_capabilities,
                effect_class=spec.effect_class,
                intent_digest=prepared.intent_digest,
                intent_ref=None,
                target_category=prepared.target_category,
                target_fingerprint=prepared.target_fingerprint,
                provenance=provenance,
                authority=receipt,
                deduplication_scope=trusted_context.deduplication_scope,
                deduplication_key=trusted_context.deduplication_key,
                clock=self._clock,
                id_factory=self._id_factory,
            )
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_record_invalid",
                message="The durable action intent could not be constructed.",
                log=log, decision=decision, invocation_id=invocation_id)

        store = self._action_store
        created = self._create_or_prove(store, proposed)
        if created is None:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_create_unverified",
                message="The durable action intent could not be verified.",
                log=log, decision=decision, invocation_id=invocation_id,
                action_id=proposed.action_id)
        created_here = created == proposed

        guard_entered = False
        try:
            with store.execution_guard(created.action_id):
                guard_entered = True
                return self._under_action_guard(
                    store=store, created=created, created_here=created_here,
                    tool=tool, tool_spec=tool_spec,
                    cleaned=cleaned, redacted=redacted,
                    adapter=adapter, spec=spec, checkpoint=checkpoint,
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
        tool: Any,
        tool_spec: ToolSpec,
        cleaned: dict[str, Any],
        redacted: dict[str, str],
        adapter: ToolActionAdapter,
        spec: ActionAdapterSpec,
        checkpoint: Callable[[], None] | None,
        decision: Decision,
        invocation_id: str,
        log: Any,
        elapsed: Callable[[], float],
    ) -> ToolInvocation:
        try:
            current = store.load(created.action_id)
            current.validate()
            if not self._same_action_lineage(current, created):
                raise ValueError("action reload changed immutable intent")
        except Exception:
            return self._journal_failure(
                tool_spec.name, redacted, elapsed(),
                kind="action_reload_failed",
                message="The durable action state could not be verified.",
                log=log, decision=decision, invocation_id=invocation_id,
                action_id=created.action_id)

        if not created_here or current.provenance.invocation_id != invocation_id:
            return self._attached_invocation(
                tool_spec.name, redacted, current, decision,
                invocation_id, elapsed(), log)
        if current.state is not ActionState.AUTHORIZED or current.attempts:
            return self._attached_invocation(
                tool_spec.name, redacted, current, decision,
                invocation_id, elapsed(), log)

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

        cancellation: CancellationRequested | None = None
        try:
            # Conservative physical mutation seam. It is entered exactly once.
            result = tool.run(cleaned, self._context(tool_spec.name))
            if not isinstance(result, ToolResult):
                result = ToolResult.success(result)
            normalized = adapter.normalize_effect(result)
            if not isinstance(normalized, NormalizedActionEffect):
                raise TypeError("adapter normalization type")
            normalized.validate(spec)
        except CancellationRequested as exc:
            cancellation = exc
            normalized = self._unknown_effect(spec, "tool.cancelled")
        except Exception:
            normalized = self._unknown_effect(spec, "tool.exception")

        try:
            candidate = self._effect_candidate(claimed, normalized)
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

    @staticmethod
    def _is_create_owner(
        record: ActionRecord,
        proposed: ActionRecord,
    ) -> bool:
        if not deduplication_compatible(record, proposed):
            return False
        if record.action_id == proposed.action_id:
            return True
        identity = proposed.deduplication_identity
        return identity is not None and record.deduplication_identity == identity

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
    ) -> ActionRecord:
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
            evidence_refs=normalized.evidence_refs,
            reconciliation=reconciliation,
            terminal_outcome=outcome)

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
        decision: Decision,
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
        decision: Decision,
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
            action_state=record.state.value)

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
            attempt_id=attempt_id)

    @staticmethod
    def _safe_pre_action_failure(
        tool_name: str,
        cleaned: Mapping[str, Any],
        duration: float,
        log: Any,
        kind: str,
        message: str,
    ) -> ToolInvocation:
        redacted = _redacted_args(cleaned)
        log.warning(Events.TOOL_FAILED, reason=kind)
        return ToolInvocation(
            tool_name, redacted, ToolResult.failure(message, kind=kind),
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
    ) -> ToolInvocation:
        log.warning(
            Events.TOOL_FAILED, reason=kind,
            action_id=action_id, attempt_id=attempt_id)
        return ToolInvocation(
            tool_name, redacted, ToolResult.failure(message, kind=kind),
            duration, decision, invocation_id=invocation_id,
            action_id=action_id, attempt_id=attempt_id,
            action_state=action_state)
