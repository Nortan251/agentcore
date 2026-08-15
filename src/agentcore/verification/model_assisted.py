"""Model-assisted verification.

Used only when deterministic evidence is inconclusive. Reuses the existing
``ModelProvider`` abstraction — no second model client, no vendor coupling.

Two safety properties matter here:

* **Model output never mutates state.** The reply is parsed and validated into
  a :class:`Judgement`, then translated into a ``VerificationResult``. It can
  set a verdict and nothing else.
* **A claim of success is not proof.** An unparseable reply, a low-confidence
  reply, or a "uncertain" verdict all become ``UNVERIFIED`` — never ``PASSED``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentcore.core.interfaces import ModelProvider
from agentcore.tasks.models import VerificationResult, VerificationStatus
from agentcore.verification.context import VerificationContext
from agentcore.verification.errors import (
    VerificationModelError,
    VerificationParseError,
)

CHECKED_BY = "ModelAssistedVerifier"

#: Below this reported confidence a "pass" is downgraded to UNVERIFIED.
DEFAULT_MIN_CONFIDENCE = 0.7

SYSTEM_PROMPT = """\
You are the verification component of an autonomous agent.

You are given a step the agent has already executed, what it was expected to \
achieve, and what actually happened. Your only job is to judge whether the \
step genuinely achieved its expected outcome.

Rules:
1. Reply with a single JSON object and nothing else. No prose, no markdown.
2. `verdict` must be exactly one of: "pass", "fail", "uncertain".
3. Answer "pass" only if the actual result clearly demonstrates the expected \
outcome was achieved. A tool merely running without error is NOT proof.
4. Answer "fail" if the evidence shows the outcome was not achieved.
5. Answer "uncertain" if the evidence is insufficient to decide. This is a \
valid and useful answer — never guess.
6. `confidence` is your certainty in the verdict, from 0.0 to 1.0.
7. `reason` must cite the specific evidence you used, in one or two sentences.
"""


def judgement_schema() -> dict[str, Any]:
    """JSON Schema for the verifier's structured reply."""
    return {
        "type": "object",
        "required": ["verdict", "reason"],
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string",
                        "enum": ["pass", "fail", "uncertain"]},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason": {"type": "string"},
            "missing_evidence": {"type": "string"},
        },
    }


def response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": "verification_judgement", "strict": True,
                        "schema": judgement_schema()},
    }


@dataclass(frozen=True)
class Judgement:
    """A parsed, validated model verdict."""

    verdict: str
    reason: str
    confidence: float = 0.0
    missing_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason,
                "confidence": self.confidence,
                "missing_evidence": self.missing_evidence}


def parse_judgement(text: str) -> Judgement:
    """Strictly parse a model reply into a :class:`Judgement`.

    Rejects anything malformed rather than defaulting to a verdict — a broken
    reply must never be able to produce a pass.
    """
    # Reuse the planner's tolerant-but-strict JSON extraction.
    from agentcore.planning.schema import extract_json_object
    from agentcore.planning.errors import PlanParseError

    try:
        payload = extract_json_object(text)
    except PlanParseError as exc:
        raise VerificationParseError("verifier reply was not a JSON object",
                                     reason=exc.message) from exc

    verdict = payload.get("verdict")
    if not isinstance(verdict, str):
        raise VerificationParseError("verifier reply has no 'verdict' string",
                                     got=type(verdict).__name__)
    verdict = verdict.strip().lower()
    if verdict not in {"pass", "fail", "uncertain"}:
        raise VerificationParseError("verifier reply has an unknown verdict",
                                     verdict=verdict)

    reason = payload.get("reason", "")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        raise VerificationParseError("verifier 'reason' must be a string",
                                     got=type(reason).__name__)

    confidence = payload.get("confidence", 0.0)
    if confidence is None:
        confidence = 0.0
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise VerificationParseError("verifier 'confidence' must be a number",
                                     got=type(confidence).__name__)
    if not 0.0 <= float(confidence) <= 1.0:
        raise VerificationParseError("verifier 'confidence' must be within "
                                     "[0, 1]", value=confidence)

    missing = payload.get("missing_evidence", "")
    if not isinstance(missing, str):
        missing = ""

    return Judgement(verdict=verdict, reason=reason.strip(),
                     confidence=float(confidence),
                     missing_evidence=missing.strip())


def build_prompt(context: VerificationContext) -> str:
    """Render the redacted evidence for the model."""
    evidence = context.to_dict()  # already redacted
    lines = [
        f"OVERALL OBJECTIVE:\n{evidence['objective'] or '(not recorded)'}",
        f"STEP DESCRIPTION:\n{evidence['description']}",
        f"EXPECTED RESULT:\n{evidence['expected_result'] or '(none stated)'}",
        f"TOOL USED: {evidence['tool_name'] or '(none)'}",
        f"TOOL ARGUMENTS:\n{json.dumps(evidence['tool_args'], default=str)}",
        f"EXECUTION STATUS: {evidence['status']}"
        f" (tool reported ok={evidence['tool_ok']})",
        f"ACTUAL RESULT:\n{evidence['result'] or '(no result recorded)'}",
    ]
    if evidence["errors"]:
        lines.append("RECORDED ERRORS:\n" + "\n".join(evidence["errors"]))
    lines.append(
        "Judge whether the step achieved its expected outcome. Respond with "
        "the JSON object only, matching this schema:\n"
        + json.dumps(judgement_schema(), indent=2))
    return "\n\n".join(lines)


class ModelAssistedVerifier:
    """Asks a model to judge evidence. Satisfies the ``Verifier`` Protocol."""

    name = CHECKED_BY

    def __init__(self, model: ModelProvider, *,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 temperature: float = 0.0) -> None:
        if model is None:
            raise VerificationModelError(
                "ModelAssistedVerifier requires a model provider")
        self.model = model
        self.min_confidence = min_confidence
        self.temperature = temperature

    def judge(self, context: VerificationContext) -> Judgement:
        """Call the model and parse its verdict."""
        try:
            reply = self.model.complete(
                build_prompt(context),
                system=SYSTEM_PROMPT,
                temperature=self.temperature,
                response_format=response_format(),
            )
        except VerificationParseError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise provider failures
            raise VerificationModelError(
                "verification model call failed",
                reason=getattr(exc, "message", str(exc)),
                error_type=type(exc).__name__) from exc
        return parse_judgement(reply)

    def verify_context(self, context: VerificationContext) -> VerificationResult:
        """Judge evidence, converting failures into conservative outcomes."""
        try:
            judgement = self.judge(context)
        except (VerificationModelError, VerificationParseError) as exc:
            # A broken or unreachable judge proves nothing.
            return VerificationResult(
                status=VerificationStatus.UNVERIFIED,
                checked_by=CHECKED_BY,
                detail=f"model verification unavailable: {exc.message}")

        if judgement.verdict == "fail":
            return VerificationResult(
                status=VerificationStatus.FAILED, checked_by=CHECKED_BY,
                detail=f"model judged the step failed: {judgement.reason}")

        if judgement.verdict == "uncertain":
            detail = f"model was uncertain: {judgement.reason}"
            if judgement.missing_evidence:
                detail += f" (missing: {judgement.missing_evidence})"
            return VerificationResult(
                status=VerificationStatus.UNVERIFIED, checked_by=CHECKED_BY,
                detail=detail)

        # verdict == "pass" — accept only if the model is confident enough.
        if judgement.confidence < self.min_confidence:
            return VerificationResult(
                status=VerificationStatus.UNVERIFIED, checked_by=CHECKED_BY,
                detail=(f"model reported success but confidence "
                        f"{judgement.confidence:.2f} is below the required "
                        f"{self.min_confidence:.2f}: {judgement.reason}"))
        return VerificationResult(
            status=VerificationStatus.PASSED, checked_by=CHECKED_BY,
            detail=(f"model confirmed the expected outcome "
                    f"(confidence {judgement.confidence:.2f}): "
                    f"{judgement.reason}"))

    # -- Verifier Protocol -------------------------------------------------
    def verify(self, subtask, state) -> VerificationResult:
        from agentcore.verification.context import build_context

        return self.verify_context(build_context(subtask, state))
