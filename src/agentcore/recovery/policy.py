"""Recovery policy: decides RETRY / REPLAN / ESCALATE / STOP.

Satisfies the existing ``RecoveryStrategy`` Protocol via ``should_retry()``,
while exposing the richer ``decide()`` for callers that can act on a replan or
an escalation.

Design rules:

* **Deterministic.** No model call — classification and safety analysis are
  rule-based, so recovery costs nothing and behaves predictably.
* **Bounded.** Every path is limited by ``max_attempts`` / ``max_replans``;
  there is no branch that can retry forever.
* **Conservative.** A retry requires *proof* it is safe. Unknown safety, a
  permission denial, or a repeating failure signature all stop the loop rather
  than gambling on another attempt.
* **Advisory.** The policy records history and returns a decision; it never
  executes a tool, mutates a result, or touches permissions.
"""

from __future__ import annotations

from typing import Any

from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.recovery.classify import (
    assess_retry_safety,
    classify,
    failed_before_side_effect,
    failure_signature,
    is_recoverable_state,
)
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.decision import (
    Escalation,
    FailureCategory,
    RecoveryAction,
    RecoveryDecision,
    RetrySafety,
)
from agentcore.recovery.history import RecoveryHistory
from agentcore.state.models import AgentState
from agentcore.tasks.models import Subtask, TaskStatus, VerificationStatus
from agentcore.tools.registry import ToolRegistry


class RecoveryPolicy:
    """Bounded, deterministic recovery decisions."""

    def __init__(
        self,
        config: RecoveryConfig | None = None,
        *,
        registry: ToolRegistry | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.config = config or RecoveryConfig()
        self.config.validate()
        # Optional: without it, retry safety cannot be established and the
        # policy becomes correspondingly more conservative.
        self.registry = registry
        self._log = logger or get_logger("recovery")

    # -- RecoveryStrategy Protocol ----------------------------------------
    def should_retry(self, subtask: Subtask, state: AgentState) -> bool:
        """Protocol-compatible entry point used by the runner's retry hook."""
        return self.decide(subtask, state).should_retry

    # -- full decision -----------------------------------------------------
    def decide(self, subtask: Subtask, state: AgentState) -> RecoveryDecision:
        """Evaluate a failed or unverified subtask and record the outcome."""
        history = RecoveryHistory.load(state)
        log = self._log.bind(subtask_id=subtask.id, attempt=subtask.attempts)

        decision = self._evaluate(subtask, state, history, log)

        history.record(subtask.id, decision, signature=failure_signature(subtask))
        history.save(state)

        log.info(Events.RECOVERY_DECIDED, **{
            k: v for k, v in decision.to_dict().items() if k != "escalation"})
        if decision.escalation is not None:
            # Surface the hand-off through the existing pending-action channel
            # so operators see it without a new mechanism.
            self._register_escalation(state, decision, log)
        return decision

    # -- evaluation --------------------------------------------------------
    def _evaluate(self, subtask: Subtask, state: AgentState,
                  history: RecoveryHistory,
                  log: StructuredLogger) -> RecoveryDecision:
        log.debug(Events.RECOVERY_STARTED, status=str(subtask.status))

        # 0. Is there anything to recover from at all?
        if not is_recoverable_state(subtask):
            return RecoveryDecision.none(
                f"subtask is '{subtask.status}' and needs no recovery")
        if not self._needs_recovery(subtask):
            return RecoveryDecision.none("no failure or adverse verdict")

        signature = failure_signature(subtask)
        repeated = history.repeats(subtask.id, signature)
        category = classify(subtask, repeated=repeated)
        safety = assess_retry_safety(subtask, self.registry)
        # A failure raised before the tool ran cannot have duplicated anything.
        if safety is not RetrySafety.SAFE and failed_before_side_effect(subtask):
            safety = RetrySafety.SAFE

        log.debug(Events.RECOVERY_CLASSIFIED, category=str(category),
                  safety=str(safety), repeated=repeated)

        attempts = subtask.attempts
        exhausted = attempts >= self.config.max_attempts

        # 1. Permission denials are never retried around.
        if category is FailureCategory.PERMISSION_DENIED:
            return self._escalate(
                subtask, category, history,
                reason="the action was refused by the permission policy",
                question=("Approve this action, raise the permission level, or "
                          "amend the objective?"),
                unresolved=self._last_error(subtask),
                safety=safety)

        # 2. Deterministic failures: repeating the identical call is pointless.
        if category is FailureCategory.PERSISTENT:
            if history.replans(subtask.id) < self.config.max_replans:
                return RecoveryDecision.replan(
                    category,
                    "failure is deterministic; the current approach cannot "
                    f"succeed unchanged ({self._last_error(subtask)})",
                    subtask_id=subtask.id, attempt=attempts, safety=safety)
            return self._exhausted(subtask, category, history, safety,
                                   "deterministic failure and no replans left")

        # 3. The same failure recurring means more attempts will not help.
        if category is FailureCategory.REPEATED:
            if history.replans(subtask.id) < self.config.max_replans:
                return RecoveryDecision.replan(
                    category,
                    "the identical failure recurred; a different approach is "
                    "needed",
                    subtask_id=subtask.id, attempt=attempts, safety=safety)
            return self._exhausted(subtask, category, history, safety,
                                   "the same failure keeps recurring")

        # 4. Attempt budget spent.
        if exhausted:
            return self._exhausted(
                subtask, category, history, safety,
                f"exhausted {attempts} of {self.config.max_attempts} attempts")

        # 5. Category-specific handling for the remaining cases.
        if category is FailureCategory.TRANSIENT:
            return self._maybe_retry(subtask, category, safety, history,
                                     "the failure looks transient")

        if category is FailureCategory.VERIFICATION_FAILED:
            if not self.config.retry_verification_failures:
                return self._replan_or_exhaust(
                    subtask, category, history, safety,
                    "verification failed and retrying it is disabled")
            return self._maybe_retry(
                subtask, category, safety, history,
                "verification failed; another attempt may satisfy the "
                "expected result",
                fallback_reason="verification failed and a retry is not "
                                "provably safe")

        if category is FailureCategory.UNVERIFIED:
            if not self.config.retry_unverified:
                # Unknown is not success — surface it rather than proceeding.
                return self._replan_or_exhaust(
                    subtask, category, history, safety,
                    "the outcome could not be verified")
            return self._maybe_retry(
                subtask, category, safety, history,
                "retrying may produce verifiable evidence",
                fallback_reason="outcome unverified and a retry is not "
                                "provably safe")

        return RecoveryDecision.stop(category, "no recovery rule applies",
                                     subtask_id=subtask.id, attempt=attempts,
                                     safety=safety)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _needs_recovery(subtask: Subtask) -> bool:
        if subtask.status is TaskStatus.FAILED or subtask.errors:
            return True
        return subtask.verification.status in (VerificationStatus.FAILED,
                                               VerificationStatus.UNVERIFIED)

    @staticmethod
    def _last_error(subtask: Subtask) -> str:
        if subtask.errors:
            return subtask.errors[-1].message
        return subtask.verification.detail or "no detail recorded"

    def _maybe_retry(self, subtask: Subtask, category: FailureCategory,
                     safety: RetrySafety, history: RecoveryHistory,
                     reason: str,
                     fallback_reason: str | None = None) -> RecoveryDecision:
        """Retry only when repeating the action is provably safe."""
        if safety is RetrySafety.SAFE:
            delay = self.config.delay_for(subtask.attempts)
            return RecoveryDecision.retry(
                category, reason, subtask_id=subtask.id,
                attempt=subtask.attempts, delay_seconds=delay, safety=safety)

        # Not provably safe: never auto-repeat a possible side effect.
        detail = fallback_reason or (
            f"retrying is not provably safe (safety={safety})")
        if safety is RetrySafety.UNSAFE:
            return self._escalate(
                subtask, category, history,
                reason=f"{detail}; the action is irreversible",
                question="Should this irreversible action be attempted again?",
                unresolved=self._last_error(subtask), safety=safety,
                safe_to_pause=True)
        if history.replans(subtask.id) < self.config.max_replans:
            return RecoveryDecision.replan(
                category, f"{detail}; seeking a safer approach",
                subtask_id=subtask.id, attempt=subtask.attempts, safety=safety)
        return self._escalate(
            subtask, category, history, reason=detail,
            question="How should this step proceed?",
            unresolved=self._last_error(subtask), safety=safety)

    def _replan_or_exhaust(self, subtask: Subtask, category: FailureCategory,
                           history: RecoveryHistory, safety: RetrySafety,
                           reason: str) -> RecoveryDecision:
        if history.replans(subtask.id) < self.config.max_replans:
            return RecoveryDecision.replan(
                category, f"{reason}; a different approach is needed",
                subtask_id=subtask.id, attempt=subtask.attempts, safety=safety)
        return self._exhausted(subtask, category, history, safety, reason)

    def _exhausted(self, subtask: Subtask, category: FailureCategory,
                   history: RecoveryHistory, safety: RetrySafety,
                   reason: str) -> RecoveryDecision:
        """Terminal path once automatic recovery is spent."""
        if self.config.escalate_on_exhaustion:
            return self._escalate(
                subtask, category, history,
                reason=reason,
                question="Automatic recovery is exhausted. How should this "
                         "proceed?",
                unresolved=self._last_error(subtask), safety=safety)
        return RecoveryDecision.stop(category, reason, subtask_id=subtask.id,
                                     attempt=subtask.attempts, safety=safety)

    def _escalate(self, subtask: Subtask, category: FailureCategory,
                  history: RecoveryHistory, *, reason: str, question: str,
                  unresolved: str, safety: RetrySafety,
                  safe_to_pause: bool = True) -> RecoveryDecision:
        escalation = Escalation(
            reason=reason,
            question=question,
            subtask_id=subtask.id,
            attempted=history.summary(subtask.id) or [
                f"{subtask.attempts} execution attempt(s)"],
            unresolved=unresolved,
            safe_to_pause=safe_to_pause,
        )
        return RecoveryDecision.escalate(category, reason,
                                         escalation=escalation,
                                         subtask_id=subtask.id,
                                         attempt=subtask.attempts,
                                         safety=safety)

    def _register_escalation(self, state: AgentState,
                             decision: RecoveryDecision,
                             log: StructuredLogger) -> None:
        from agentcore.state.models import PendingAction

        escalation = decision.escalation
        assert escalation is not None
        state.add_pending_action(PendingAction(
            description=escalation.question,
            reason=escalation.reason,
            subtask_id=escalation.subtask_id,
        ))
        log.warning(Events.RECOVERY_ESCALATED, **escalation.to_dict())


def build_policy(config: RecoveryConfig | None = None,
                 registry: ToolRegistry | None = None,
                 **kwargs: Any) -> RecoveryPolicy:
    """Small factory mirroring the other layers' construction helpers."""
    return RecoveryPolicy(config, registry=registry, **kwargs)
