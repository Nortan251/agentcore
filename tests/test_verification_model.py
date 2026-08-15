"""Tests for model-assisted verification.

The central property under test: no malformed, unreachable, uncertain or
low-confidence model reply may ever produce a PASSED result.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agentcore.models.errors import ModelTimeoutError
from agentcore.tasks.models import TaskStatus, VerificationStatus
from agentcore.verification.context import VerificationContext
from agentcore.verification.errors import (
    VerificationModelError,
    VerificationParseError,
)
from agentcore.verification.model_assisted import (
    DEFAULT_MIN_CONFIDENCE,
    ModelAssistedVerifier,
    build_prompt,
    judgement_schema,
    parse_judgement,
    response_format,
)


class FakeModel:
    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 **options: Any) -> str:
        self.calls.append({"prompt": prompt, "system": system,
                           "options": options})
        if not self.outputs:
            raise AssertionError("FakeModel exhausted")
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, str) else json.dumps(out)


def ctx(**overrides) -> VerificationContext:
    defaults = dict(
        subtask_id="sub_1", description="produce a report",
        status=TaskStatus.COMPLETED, objective="reporting",
        tool_name="echo", tool_args={"message": "hi"},
        result="some output", has_result=True, tool_ok=True,
        expected_result="a report", requires_verification=True,
    )
    defaults.update(overrides)
    return VerificationContext(**defaults)


def judgement(**overrides) -> dict[str, Any]:
    base = {"verdict": "pass", "confidence": 0.95, "reason": "output matches"}
    base.update(overrides)
    return base


# -- parsing ----------------------------------------------------------------

def test_valid_judgement_parsed():
    result = parse_judgement(json.dumps(judgement()))
    assert result.verdict == "pass"
    assert result.confidence == 0.95
    assert result.reason == "output matches"


def test_fenced_json_accepted():
    text = "```json\n" + json.dumps(judgement(verdict="fail")) + "\n```"
    assert parse_judgement(text).verdict == "fail"


def test_verdict_case_and_space_normalised():
    assert parse_judgement(json.dumps(judgement(verdict="  PASS "))).verdict == "pass"


def test_optional_fields_default():
    parsed = parse_judgement(json.dumps({"verdict": "uncertain"}))
    assert parsed.confidence == 0.0
    assert parsed.reason == ""


def test_missing_evidence_captured():
    parsed = parse_judgement(json.dumps(judgement(
        verdict="uncertain", missing_evidence="no file listing available")))
    assert parsed.missing_evidence == "no file listing available"


@pytest.mark.parametrize("bad", [
    "",                                    # empty
    "I think it worked fine.",             # prose
    "{not json",                           # broken
    json.dumps([1, 2]),                    # not an object
    json.dumps({}),                        # no verdict
    json.dumps({"verdict": 5}),            # wrong type
    json.dumps({"verdict": "maybe"}),      # unknown verdict
    json.dumps({"verdict": "pass", "confidence": "high"}),   # wrong type
    json.dumps({"verdict": "pass", "confidence": 1.5}),      # out of range
    json.dumps({"verdict": "pass", "confidence": -0.2}),     # out of range
    json.dumps({"verdict": "pass", "reason": {"a": 1}}),     # wrong type
])
def test_malformed_judgements_rejected(bad):
    with pytest.raises(VerificationParseError):
        parse_judgement(bad)


def test_boolean_confidence_rejected():
    """bool is an int subclass; it must not pass as a confidence number."""
    with pytest.raises(VerificationParseError):
        parse_judgement(json.dumps({"verdict": "pass", "confidence": True}))


# -- schema / prompt --------------------------------------------------------

def test_schema_is_small_and_strict():
    schema = judgement_schema()
    json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["verdict", "reason"]
    assert schema["properties"]["verdict"]["enum"] == ["pass", "fail",
                                                       "uncertain"]


def test_response_format_requests_the_schema():
    assert response_format()["json_schema"]["schema"] == judgement_schema()


def test_prompt_contains_the_evidence():
    prompt = build_prompt(ctx(expected_result="a CSV file",
                              result="wrote data.csv"))
    assert "a CSV file" in prompt
    assert "wrote data.csv" in prompt
    assert "produce a report" in prompt
    assert "reporting" in prompt


def test_prompt_redacts_secrets():
    prompt = build_prompt(ctx(tool_args={"token": "sk-super-secret",
                                         "path": "/tmp/x"}))
    assert "sk-super-secret" not in prompt
    assert "***" in prompt


def test_system_prompt_forbids_guessing():
    verifier = ModelAssistedVerifier(FakeModel(judgement()))
    verifier.verify_context(ctx())
    system = verifier.model.calls[0]["system"]
    assert "never guess" in system.lower()
    assert "NOT proof" in system


def test_structured_output_requested():
    verifier = ModelAssistedVerifier(FakeModel(judgement()))
    verifier.verify_context(ctx())
    options = verifier.model.calls[0]["options"]
    assert options["response_format"]["type"] == "json_schema"
    assert options["temperature"] == 0.0


# -- verdict translation ----------------------------------------------------

def test_confident_pass_verifies():
    result = ModelAssistedVerifier(FakeModel(judgement())).verify_context(ctx())
    assert result.status is VerificationStatus.PASSED
    assert result.checked_by == "ModelAssistedVerifier"
    assert "output matches" in result.detail


def test_fail_verdict_fails():
    result = ModelAssistedVerifier(
        FakeModel(judgement(verdict="fail", reason="no report produced"))
    ).verify_context(ctx())
    assert result.status is VerificationStatus.FAILED
    assert "no report produced" in result.detail


def test_uncertain_verdict_is_unverified():
    result = ModelAssistedVerifier(
        FakeModel(judgement(verdict="uncertain", reason="cannot tell",
                            missing_evidence="no file listing"))
    ).verify_context(ctx())
    assert result.status is VerificationStatus.UNVERIFIED
    assert "cannot tell" in result.detail
    assert "no file listing" in result.detail


def test_low_confidence_pass_is_downgraded():
    """A hedged 'pass' is not proof."""
    result = ModelAssistedVerifier(
        FakeModel(judgement(confidence=0.3))
    ).verify_context(ctx())
    assert result.status is VerificationStatus.UNVERIFIED
    assert "below the required" in result.detail


def test_confidence_threshold_is_configurable():
    model = FakeModel(judgement(confidence=0.5), judgement(confidence=0.5))
    strict = ModelAssistedVerifier(model, min_confidence=0.9)
    lenient = ModelAssistedVerifier(model, min_confidence=0.4)
    assert strict.verify_context(ctx()).status is VerificationStatus.UNVERIFIED
    assert lenient.verify_context(ctx()).status is VerificationStatus.PASSED


def test_default_threshold_applied():
    just_below = DEFAULT_MIN_CONFIDENCE - 0.01
    result = ModelAssistedVerifier(
        FakeModel(judgement(confidence=just_below))).verify_context(ctx())
    assert result.status is VerificationStatus.UNVERIFIED


# -- failure modes never produce success ------------------------------------

def test_malformed_reply_becomes_unverified_not_passed():
    result = ModelAssistedVerifier(
        FakeModel("total nonsense")).verify_context(ctx())
    assert result.status is VerificationStatus.UNVERIFIED
    assert "unavailable" in result.detail


def test_model_failure_becomes_unverified():
    result = ModelAssistedVerifier(
        FakeModel(ModelTimeoutError("timed out"))).verify_context(ctx())
    assert result.status is VerificationStatus.UNVERIFIED


def test_unexpected_exception_becomes_unverified():
    result = ModelAssistedVerifier(
        FakeModel(RuntimeError("boom"))).verify_context(ctx())
    assert result.status is VerificationStatus.UNVERIFIED


def test_judge_raises_typed_errors():
    with pytest.raises(VerificationModelError):
        ModelAssistedVerifier(FakeModel(RuntimeError("x"))).judge(ctx())
    with pytest.raises(VerificationParseError):
        ModelAssistedVerifier(FakeModel("nope")).judge(ctx())


def test_requires_a_model():
    with pytest.raises(VerificationModelError):
        ModelAssistedVerifier(None)


@pytest.mark.parametrize("reply", [
    "", "not json", json.dumps({"verdict": "definitely"}),
    json.dumps({"result": "ok"}), json.dumps({"verdict": "pass",
                                              "confidence": 99}),
])
def test_no_malformed_reply_can_ever_pass(reply):
    """Exhaustive guard on the critical safety property."""
    result = ModelAssistedVerifier(FakeModel(reply)).verify_context(ctx())
    assert result.status is not VerificationStatus.PASSED
