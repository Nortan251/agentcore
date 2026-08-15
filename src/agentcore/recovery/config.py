"""Recovery configuration.

Lives in the recovery package (like ``permissions/config.py``) to keep the
layering acyclic, and is re-exported from :mod:`agentcore.config.schema` so
callers still see one configuration surface.

Only settings the policy actually consumes are defined here — existing
``tools.max_retries`` (transport-level retries inside one tool call) and
``model.max_retries`` (HTTP retries) are deliberately *not* reused: this is a
different concern, namely how many times the orchestrator re-attempts a step.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentcore.errors import ConfigError


@dataclass(frozen=True)
class RecoveryConfig:
    """Bounds and delays for the recovery policy."""

    #: Total attempts per subtask, including the first. 1 disables retrying.
    max_attempts: int = 3
    #: Delay before the first retry, in seconds.
    base_delay_seconds: float = 0.5
    #: Multiplier applied per additional attempt.
    backoff_factor: float = 2.0
    #: Ceiling on any single delay.
    max_delay_seconds: float = 30.0
    #: Whether a FAILED verification may be retried at all.
    retry_verification_failures: bool = True
    #: Whether an UNVERIFIED outcome may be retried to gather more evidence.
    retry_unverified: bool = False
    #: Maximum number of replans requested for a single task.
    max_replans: int = 1
    #: Escalate (rather than silently stop) when automatic recovery is spent.
    escalate_on_exhaustion: bool = True

    def delay_for(self, attempt: int) -> float:
        """Deterministic backoff: base * factor^(attempt-1), capped.

        ``attempt`` is 1-based and refers to the attempt already made.
        """
        if attempt < 1:
            return 0.0
        delay = self.base_delay_seconds * (self.backoff_factor ** (attempt - 1))
        return min(delay, self.max_delay_seconds)

    def validate(self) -> None:
        if self.max_attempts < 1:
            raise ConfigError("recovery.max_attempts must be at least 1",
                              value=self.max_attempts)
        if self.base_delay_seconds < 0:
            raise ConfigError("recovery.base_delay_seconds must be >= 0",
                              value=self.base_delay_seconds)
        if self.backoff_factor < 1.0:
            raise ConfigError("recovery.backoff_factor must be >= 1.0",
                              value=self.backoff_factor)
        if self.max_delay_seconds < 0:
            raise ConfigError("recovery.max_delay_seconds must be >= 0",
                              value=self.max_delay_seconds)
        if self.max_replans < 0:
            raise ConfigError("recovery.max_replans must be >= 0",
                              value=self.max_replans)
