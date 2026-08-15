"""Composite verifier: deterministic first, model-assisted only if needed.

This is the entry point wired into the Agent. It owns the escalation policy,
keeping each individual strategy simple and independently testable.

Flow per subtask::

    build evidence
      -> is verification required at all?         -> SKIPPED
      -> deterministic checks                     -> PASSED / FAILED
      -> inconclusive + model available + needed  -> model judgement
      -> otherwise                                -> UNVERIFIED

The verifier judges completed work. It never executes a tool, never touches the
``ToolExecutor`` and never participates in permission decisions.
"""

from __future__ import annotations

from agentcore.core.interfaces import ModelProvider
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.state.models import AgentState
from agentcore.tasks.models import (
    Subtask,
    VerificationResult,
    VerificationStatus,
)
from agentcore.verification.context import VerificationContext, build_context
from agentcore.verification.deterministic import DeterministicVerifier
from agentcore.verification.model_assisted import ModelAssistedVerifier

CHECKED_BY = "Verifier"


class Verifier:
    """Deterministic-first verifier satisfying the existing Protocol.

    ``model`` is optional: without one, inconclusive evidence simply stays
    ``UNVERIFIED``, which is the safe outcome.
    """

    def __init__(
        self,
        *,
        model: ModelProvider | None = None,
        deterministic: DeterministicVerifier | None = None,
        model_assisted: ModelAssistedVerifier | None = None,
        always_use_model: bool = False,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.deterministic = deterministic or DeterministicVerifier()
        if model_assisted is not None:
            self.model_assisted: ModelAssistedVerifier | None = model_assisted
        elif model is not None:
            self.model_assisted = ModelAssistedVerifier(model)
        else:
            self.model_assisted = None
        # When False (default), the model is consulted only for steps the plan
        # flagged with requires_verification. This keeps cost proportional to
        # what the planner actually asked to be checked.
        self.always_use_model = always_use_model
        self._log = logger or get_logger("verification")

    @property
    def has_model(self) -> bool:
        return self.model_assisted is not None

    # -- Verifier Protocol -------------------------------------------------
    def verify(self, subtask: Subtask, state: AgentState) -> VerificationResult:
        context = build_context(subtask, state)
        log = self._log.bind(subtask_id=context.subtask_id,
                             tool=context.tool_name)
        log.debug(Events.VERIFICATION_STARTED,
                  requires_verification=context.requires_verification,
                  has_expectation=context.has_expectation)

        result = self._evaluate(context, log)

        log.info(Events.VERIFICATION_COMPLETED, status=str(result.status),
                 checked_by=result.checked_by, detail=result.detail)
        return result

    def verify_context(self, context: VerificationContext
                       ) -> VerificationResult:
        """Judge pre-built evidence (used by tests and callers with context)."""
        return self._evaluate(context, self._log)

    # -- strategy ----------------------------------------------------------
    def _evaluate(self, context: VerificationContext,
                  log: StructuredLogger) -> VerificationResult:
        # 1. Nothing to prove: no expectation and none demanded.
        if not context.requires_verification and not context.has_expectation:
            if context.completed and context.tool_ok is not False:
                return VerificationResult(
                    status=VerificationStatus.SKIPPED, checked_by=CHECKED_BY,
                    detail="verification not required for this step")

        # 2. Deterministic evidence first — it is free and authoritative.
        result = self.deterministic.verify_context(context)
        log.debug(Events.VERIFICATION_DETERMINISTIC, status=str(result.status))
        if result.status is not VerificationStatus.UNVERIFIED:
            return result

        # 3. Inconclusive. Escalate to the model only when it can help.
        if not self.has_model:
            return result
        if not (context.requires_verification or self.always_use_model):
            return result
        if not context.completed:
            # Nothing executed yet; a model cannot manufacture evidence.
            return result

        log.debug(Events.VERIFICATION_MODEL_ASSISTED)
        assert self.model_assisted is not None
        model_result = self.model_assisted.verify_context(context)

        # Preserve the deterministic reasoning when the model also fails to
        # decide, so the final detail explains both attempts.
        if model_result.status is VerificationStatus.UNVERIFIED:
            model_result.detail = (f"{model_result.detail} "
                                   f"(deterministic: {result.detail})")
        return model_result
