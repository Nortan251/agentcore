"""Narrow host-driven startup recovery and reconciliation selection.

The coordinator classifies and claims work. It never probes a provider,
checks or grants permission, invokes ``Tool.run``, or interprets effect truth.
Those operations remain under :class:`agentcore.tools.executor.ToolExecutor`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from agentcore.action_journal import (
    MAX_RECONCILIATION_PROBES,
    ActionAttemptStatus,
    ActionRecord,
    ActionReconciliationReport,
    ActionRecoveryClaim,
    ActionState,
    ActionStore,
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

    def _require_owner_process(self) -> None:
        if self._owner_pid != os.getpid():
            raise ActionStoreOwnershipError(
                "action recovery coordinator belongs to another process"
            )

    def recover_startup(self) -> ActionStartupRecoveryReport:
        """Validate the domain and conservatively classify nonterminal truth."""
        self._require_owner_process()
        if self._startup_report is not None:
            return self._startup_report
        try:
            action_ids = self._store.list_actions()
            if len(action_ids) > self._max_startup_actions:
                return self._quarantine("startup_scan_bound_exceeded")
            records = self._store.list_nonterminal()
            items: list[ActionRecoveryItem] = []
            for record in records:
                recovered, status = self._recover_record(record)
                items.append(ActionRecoveryItem(
                    action_id=recovered.action_id,
                    status=status,
                    record_version=recovered.version,
                ))
            report = ActionStartupRecoveryReport(
                items=tuple(items),
                terminal_count=len(action_ids) - len(records),
            )
        except ActionStoreOwnershipError:
            raise
        except Exception:
            return self._quarantine("journal_corrupt")
        self._startup_report = report
        return report

    def _recover_record(
        self,
        record: ActionRecord,
    ) -> tuple[ActionRecord, ActionRecoveryStatus]:
        record.validate()
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
        try:
            return self._executor.reconcile_action(
                claim,
                checkpoint=checkpoint,
            )
        except CancellationRequested:
            raise

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
        except Exception:
            pass
        try:
            loaded = self._store.load(current.action_id)
            loaded.validate()
            return loaded if loaded == candidate else None
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
