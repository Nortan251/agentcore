"""Verification layer: did the completed work actually achieve its goal?

Deterministic evidence is preferred; a model is consulted only when that
evidence is inconclusive and the plan asked for verification.

The governing rule is **unknown is not success**: anything that cannot be
established from real evidence returns ``UNVERIFIED``, never ``PASSED``.

This layer judges completed work only. It never executes tools and never takes
part in permission decisions.
"""

from agentcore.verification.context import VerificationContext, build_context
from agentcore.verification.deterministic import (
    DeterministicVerifier,
    expectation_match,
)
from agentcore.verification.errors import (
    VerificationError,
    VerificationModelError,
    VerificationParseError,
)
from agentcore.verification.model_assisted import (
    Judgement,
    ModelAssistedVerifier,
    judgement_schema,
    parse_judgement,
)
from agentcore.verification.verifier import Verifier

__all__ = [
    "Verifier",
    "DeterministicVerifier",
    "ModelAssistedVerifier",
    "VerificationContext",
    "build_context",
    "expectation_match",
    "Judgement",
    "parse_judgement",
    "judgement_schema",
    "VerificationError",
    "VerificationModelError",
    "VerificationParseError",
]
