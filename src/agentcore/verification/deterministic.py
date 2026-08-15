"""Deterministic verification.

Judges a completed subtask using only facts the system already holds — tool
success, recorded errors, presence of a result, and whether that result
satisfies the plan's stated expectation.

The governing rule is **unknown is not success**. A tool returning without an
exception proves the call completed, not that the objective was achieved, so a
subtask with a stated expectation that cannot be checked deterministically
returns ``UNVERIFIED``, never ``PASSED``.
"""

from __future__ import annotations

import re

from agentcore.tasks.models import (
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.verification.context import VerificationContext

CHECKED_BY = "DeterministicVerifier"

#: Words that carry no discriminating power when matching an expectation
#: against a result. Kept small and generic on purpose.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "to", "in", "on", "at", "for", "with", "and", "or", "that", "this",
    "it", "its", "as", "by", "from", "should", "must", "will", "would",
    "output", "result", "returns", "returned", "contains", "containing",
    "value", "values", "text", "string",
})

#: Proportion of an expectation's significant tokens that must appear in the
#: result before a textual match counts as corroborating evidence.
_MATCH_THRESHOLD = 0.6


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in _STOPWORDS and len(t) > 1}


def expectation_match(expected: str, actual: str) -> float:
    """Fraction of the expectation's significant tokens present in ``actual``.

    A blunt instrument by design: it is only ever used as *corroborating*
    evidence for a pass, or to flag a clear mismatch. Anything ambiguous is
    escalated rather than guessed.
    """
    wanted = _tokens(expected)
    if not wanted:
        return 0.0
    # Exact substring is the strongest signal available without a model.
    if expected.strip().lower() in actual.lower():
        return 1.0
    found = _tokens(actual)
    return len(wanted & found) / len(wanted)


def _result(status: VerificationStatus, detail: str) -> VerificationResult:
    return VerificationResult(status=status, checked_by=CHECKED_BY,
                              detail=detail)


class DeterministicVerifier:
    """Evidence-only verifier. Satisfies the existing ``Verifier`` Protocol.

    Returns ``UNVERIFIED`` whenever the available evidence cannot responsibly
    establish success — that is the signal for a model-assisted verifier to
    take over, and the safe answer if none is configured.
    """

    #: Exposed so the composite strategy can report which check ran.
    name = CHECKED_BY

    def __init__(self, *, match_threshold: float = _MATCH_THRESHOLD) -> None:
        self.match_threshold = match_threshold

    def verify_context(self, context: VerificationContext) -> VerificationResult:
        """Judge pre-collected evidence."""
        # 1. Explicit failure evidence always wins.
        if context.status is TaskStatus.FAILED or context.tool_ok is False:
            reason = context.tool_error or (
                context.errors[-1] if context.errors else "execution failed")
            return _result(VerificationStatus.FAILED,
                           f"execution did not succeed: {reason}")

        if context.errors and not context.completed:
            return _result(VerificationStatus.FAILED,
                           f"subtask recorded an error: {context.errors[-1]}")

        # 2. Only completed work can be verified at all.
        if not context.completed:
            return _result(
                VerificationStatus.UNVERIFIED,
                f"subtask is '{context.status}', not completed; "
                "nothing to verify yet")

        # 3. Steps with no tool produced no evidence to inspect.
        if not context.executed_a_tool:
            return _result(
                VerificationStatus.UNVERIFIED,
                "subtask ran no tool, so there is no execution evidence")

        # 4. A completed tool call with no stated expectation: the only
        #    verifiable claim is that the call itself succeeded.
        if not context.has_expectation:
            if context.requires_verification:
                return _result(
                    VerificationStatus.UNVERIFIED,
                    "verification was requested but the plan stated no "
                    "expected result to check against")
            if context.tool_ok is True:
                return _result(
                    VerificationStatus.PASSED,
                    f"tool '{context.tool_name}' reported success and no "
                    "specific outcome was required")
            return _result(
                VerificationStatus.UNVERIFIED,
                "no recorded tool outcome and no expected result to check")

        # 5. There is an expectation — compare it against the actual result.
        if not context.has_result:
            return _result(
                VerificationStatus.FAILED,
                f"expected '{context.expected_result}' but the tool produced "
                "no result")

        score = expectation_match(context.expected_result, context.result_text())
        if score >= self.match_threshold:
            return _result(
                VerificationStatus.PASSED,
                f"result matches the expected outcome "
                f"(match {score:.0%}): {context.expected_result!r}")
        if score == 0.0:
            # No overlap at all: a clear contradiction rather than ambiguity.
            return _result(
                VerificationStatus.FAILED,
                f"result does not match the expected outcome "
                f"{context.expected_result!r}; got {context.result_text()[:120]!r}")

        # Partial overlap is genuinely ambiguous. Escalate, never guess.
        return _result(
            VerificationStatus.UNVERIFIED,
            f"result only partially matches the expected outcome "
            f"(match {score:.0%}); deterministic evidence is inconclusive")

    # -- Verifier Protocol -------------------------------------------------
    def verify(self, subtask, state) -> VerificationResult:
        from agentcore.verification.context import build_context

        return self.verify_context(build_context(subtask, state))
