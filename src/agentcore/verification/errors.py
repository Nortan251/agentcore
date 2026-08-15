"""Verification error hierarchy.

Extends the project's existing :class:`AgentError` tree, matching the approach
used by the model and planning layers.

These are raised *inside* the verification layer and are deliberately caught by
the verifiers themselves: a verification failure must degrade to a conservative
``UNVERIFIED`` result rather than crash a run that has already done its work.
"""

from __future__ import annotations

from agentcore.errors import AgentError


class VerificationError(AgentError):
    """Base class for verification-layer failures."""


class VerificationModelError(VerificationError):
    """The verification model could not be reached or configured."""

    retryable = True


class VerificationParseError(VerificationError):
    """The model's judgement was missing, malformed or out of range."""
