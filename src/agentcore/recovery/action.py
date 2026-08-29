"""Narrow host-driven startup recovery and reconciliation selection.

The coordinator classifies and claims work. It never probes a provider,
checks or grants permission, invokes ``Tool.run``, or interprets effect truth.
Those operations remain under :class:`agentcore.tools.executor.ToolExecutor`.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from agentcore.action_journal import (
    MAX_RECONCILIATION_PROBES,
    ActionAttemptStatus,
    ActionRecord,
    ActionDomainHealthState,
    ActionReconciliationReport,
    ActionRecoveryClaim,
    ActionState,
    ActionStore,
    ActionStartupScanBoundExceededError,
    EffectVerdict,
    ReconciliationDisposition,
    ReconciliationMetadata,
    new_action_transition_id,
    require_action_id,
)
from agentcore.action_journal.errors import (
    ActionStoreError,
    ActionStoreOwnershipError,
    ActionValidationError,
)
from agentcore.realtime.errors import CancellationRequested
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


MAX_STARTUP_ACTIONS = 256


class ActionRecoveryStatus(str, Enum):
    WAITING_OWNER = "waiting_owner"
    RETRY_READY = "retry_ready"
    RECONCILE_REQUIRED = "reconcile_required"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ActionRecoveryItem:
    action_id: str
    status: ActionRecoveryStatus
    record_version: int


@dataclass(frozen=True)
class ActionStartupRecoveryReport:
    items: tuple[ActionRecoveryItem, ...] = ()
    terminal_count: int = 0
    quarantined: bool = False
    error_category: str | None = None


class ActionRecoveryCoordinator:
    """Select and fence bounded action recovery work for one process."""

    def __init__(
        self,
        store: ActionStore,
        executor,
        *,
        clock: Clock | None = None,
        id_factory: Callable[[str], str] = new_id,
        owner_id: str | None = None,
        max_startup_actions: int = MAX_STARTUP_ACTIONS,
    ) -> None:
        if isinstance(max_startup_actions, bool) or not isinstance(
            max_startup_actions, int
        ) or max_startup_actions < 1:
            raise ActionValidationError("startup recovery bound is invalid")
        self._store = store
        self._executor = executor
        self._clock = clock or SystemClock()
        self._id_factory = id_factory
        self._owner_id = owner_id or f"recovery:{os.getpid()}"
        self._max_startup_actions = max_startup_actions
        self._owner_pid = os.getpid()
        self._startup_report: ActionStartupRecoveryReport | None = None
        self._startup_lock = threading.Lock()

    def _require_owner_process(self) -> None:
        if self._owner_pid != os.getpid():
            raise ActionStoreOwnershipError(
                "action recovery coordinator belongs to another process"
            )

    def recover_startup(self) -> ActionStartupRecoveryReport:
        """Validate the domain and conservatively classify nonterminal truth."""
        self._require_owner_process()
        with self._startup_lock:
            if self._startup_report is not None:
                return self._startup_report
            try:
                # First prove the complete bounded core domain and every
                # nonterminal protected dependency before rewriting even a
                # valid owner-lost record.
                records = self._store.audit(
                    maximum_records=self._max_startup_actions)
                self._validate_nonterminal_dependencies(records)

                items: list[ActionRecoveryItem] = []
                classified: dict[str, ActionRecord] = {}
                for record in records:
                    if record.is_terminal:
                        classified[record.action_id] = record
                        continue
                    recovered, status = self._recover_record(record)
                    classified[recovered.action_id] = recovered
                    if status is None:
                        continue
                    items.append(ActionRecoveryItem(
                        action_id=recovered.action_id,
                        status=status,
                        record_version=recovered.version,
                    ))

                # Recovery may publish conservative classifications.  Re-audit
                # the resulting whole domain and its live dependencies before
                # opening the executor mutation gate.
                final_records = self._store.audit(
                    maximum_records=self._max_startup_actions)
                self._validate_nonterminal_dependencies(final_records)
                final_by_id = {
                    record.action_id: record for record in final_records}
                if final_by_id != classified:
                    raise ActionStoreError(
                        "action journal changed during startup validation")
                report = ActionStartupRecoveryReport(
                    items=tuple(items),
                    terminal_count=sum(
                        record.is_terminal for record in final_records),
                )
                mark_healthy = getattr(
                    self._executor, "_mark_action_startup_healthy", None)
                if not callable(mark_healthy) or not mark_healthy(self._store):
                    return self._quarantine("journal_corrupt")
            except ActionStoreOwnershipError:
                raise
            except ActionStartupScanBoundExceededError:
                return self._quarantine("startup_scan_bound_exceeded")
            except Exception:
                return self._quarantine("journal_corrupt")
            self._startup_report = report
            return report

    def _validate_nonterminal_dependencies(
        self,
        records: list[ActionRecord],
    ) -> None:
        validate_protected = getattr(
            self._executor, "validate_action_protected_storage", None)
        if not callable(validate_protected):
            raise ActionValidationError(
                "protected action validation boundary is unavailable")
        for record in records:
            if not record.is_terminal:
                validate_protected(record)

    def _recover_record(
        self,
        record: ActionRecord,
    ) -> tuple[ActionRecord, ActionRecoveryStatus | None]:
        # The startup scan released the store mutex before reaching here.
        # Serialize only this action, then classify the authoritative reload.
        with self._store.execution_guard(record.action_id):
            current = self._store.load(record.action_id)
            current.validate()
            validate_protected = getattr(
                self._executor, "validate_action_protected_storage", None)
            if callable(validate_protected):
                validate_protected(current)
            return self._recover_guarded_record(current)

    def _recover_guarded_record(
        self,
        record: ActionRecord,
    ) -> tuple[ActionRecord, ActionRecoveryStatus | None]:
        if record.state.is_terminal:
            return record, None
        if record.state is ActionState.AUTHORIZED:
            status = (
                ActionRecoveryStatus.RETRY_READY
                if record.attempts
                else ActionRecoveryStatus.WAITING_OWNER
            )
            return record, status
        if record.state is ActionState.IN_FLIGHT:
            disposition = (
                ReconciliationDisposition.PROBE_LATER
                if record.reconciliation_capabilities
                else ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED
            )
            candidate = record.transition(
                ActionState.RECONCILE_REQUIRED,
                transition_id=new_action_transition_id(
                    id_factory=self._id_factory
                ),
                at=self._clock.now(),
                attempt_status=ActionAttemptStatus.POSSIBLY_INVOKED,
                effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
                reconciliation=ReconciliationMetadata(
                    disposition=disposition,
                ),
            )
            recovered = self._publish_exact(record, candidate)
            if recovered is None:
                raise ActionStoreError(
                    "startup in-flight classification could not be verified",
                    action_id=record.action_id,
                )
            return recovered, ActionRecoveryStatus.RECONCILE_REQUIRED
        if record.state is ActionState.RECONCILE_REQUIRED:
            if record.reconciliation.claim_owner_id is not None:
                metadata = ReconciliationMetadata(
                    disposition=record.reconciliation.disposition,
                    claim_epoch=record.reconciliation.claim_epoch + 1,
                    probe_count=record.reconciliation.probe_count,
                    next_probe_at=record.reconciliation.next_probe_at,
                )
                candidate = record.transition(
                    ActionState.RECONCILE_REQUIRED,
                    transition_id=new_action_transition_id(
                        id_factory=self._id_factory
                    ),
                    at=self._clock.now(),
                    effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
                    reconciliation=metadata,
                )
                recovered = self._publish_exact(record, candidate)
                if recovered is None:
                    raise ActionStoreError(
                        "startup reconciliation fence could not be verified",
                        action_id=record.action_id,
                    )
                record = recovered
            return record, ActionRecoveryStatus.RECONCILE_REQUIRED
        raise ActionValidationError(
            "startup nonterminal scan returned terminal action"
        )

    def claim(
        self,
        action_id: str,
        *,
        expected_version: int,
    ) -> ActionRecoveryClaim | None:
        """CAS one fresh owner identity and epoch onto an unresolved action."""
        self._require_owner_process()
        self._require_action_domain_healthy()
        action_id = require_action_id(action_id)
        if isinstance(expected_version, bool) or not isinstance(
            expected_version, int
        ) or expected_version < 0:
            raise ActionValidationError("recovery expected version is invalid")
        try:
            current = self._store.load(action_id)
            current.validate()
            if current.version != expected_version or current.state is not (
                ActionState.RECONCILE_REQUIRED
            ):
                return None
            metadata = current.reconciliation
            if metadata.disposition is not ReconciliationDisposition.PROBE_LATER:
                return None
            if metadata.claim_owner_id is not None:
                return None
            if metadata.probe_count >= MAX_RECONCILIATION_PROBES:
                return None
            claim_owner_id = f"{self._owner_id}:{self._id_factory('rcl')}"
            claim_epoch = metadata.claim_epoch + 1
            candidate = current.transition(
                ActionState.RECONCILE_REQUIRED,
                transition_id=new_action_transition_id(
                    id_factory=self._id_factory
                ),
                at=self._clock.now(),
                effect_verdict=EffectVerdict.OUTCOME_UNKNOWN,
                reconciliation=ReconciliationMetadata(
                    disposition=ReconciliationDisposition.PROBE_LATER,
                    claim_owner_id=claim_owner_id,
                    claim_epoch=claim_epoch,
                    claimed_at=self._clock.now(),
                    probe_count=metadata.probe_count,
                ),
            )
            claimed = self._publish_exact(current, candidate)
            if claimed is None:
                return None
            return ActionRecoveryClaim(
                action_id=claimed.action_id,
                record_version=claimed.version,
                claim_owner_id=claim_owner_id,
                claim_epoch=claim_epoch,
            ).validate()
        except ActionStoreOwnershipError:
            raise
        except Exception:
            return None

    def claim_next(self, *, limit: int = 1) -> tuple[ActionRecoveryClaim, ...]:
        """Select a bounded prefix of currently claimable records."""
        self._require_owner_process()
        self._require_action_domain_healthy()
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ActionValidationError("recovery claim limit is invalid")
        limit = min(limit, self._max_startup_actions)
        claims: list[ActionRecoveryClaim] = []
        for record in self._store.list_nonterminal():
            if len(claims) >= limit:
                break
            claim = self.claim(
                record.action_id,
                expected_version=record.version,
            )
            if claim is not None:
                claims.append(claim)
        return tuple(claims)

    def reconcile(
        self,
        claim: ActionRecoveryClaim,
        *,
        checkpoint=None,
    ) -> ActionReconciliationReport:
        """Ask the sole executor authority to run the claimed read-only probe."""
        self._require_owner_process()
        self._require_action_domain_healthy()
        try:
            return self._executor.reconcile_action(
                claim,
                checkpoint=checkpoint,
            )
        except CancellationRequested:
            raise

    def _require_action_domain_healthy(self) -> None:
        health = getattr(self._executor, "action_domain_health", None)
        if health is None or health.state is not ActionDomainHealthState.HEALTHY:
            raise ActionStoreError(
                "action recovery is unavailable before healthy startup")

    def _publish_exact(
        self,
        current: ActionRecord,
        candidate: ActionRecord,
    ) -> ActionRecord | None:
        try:
            self._store.compare_and_swap(
                current.action_id,
                current.version,
                candidate,
            )
        except ActionStoreOwnershipError:
            raise
        except Exception:
            pass
        try:
            loaded = self._store.load(current.action_id)
            loaded.validate()
            return loaded if loaded == candidate else None
        except ActionStoreOwnershipError:
            raise
        except Exception:
            return None

    def _quarantine(self, category: str) -> ActionStartupRecoveryReport:
        quarantine = getattr(self._executor, "quarantine_action_mutations", None)
        if callable(quarantine):
            quarantine(category)
        report = ActionStartupRecoveryReport(
            quarantined=True,
            error_category=category,
        )
        self._startup_report = report
        return report


__all__ = [
    "ActionRecoveryCoordinator",
    "ActionRecoveryItem",
    "ActionRecoveryStatus",
    "ActionStartupRecoveryReport",
    "MAX_STARTUP_ACTIONS",
]
