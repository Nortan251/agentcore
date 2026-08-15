"""Recovery layer: decide what to do after a step fails or cannot be verified.

Sits behind the existing ``RecoveryStrategy`` Protocol. ``should_retry()``
keeps the runner's boolean contract; ``decide()`` exposes the full
RETRY / REPLAN / ESCALATE / STOP decision.

Recovery is advisory and deterministic. It never executes a tool, never grants
approval, never turns an adverse verdict into success, and every path is
bounded so it cannot loop forever.

Import note: exports are resolved lazily (PEP 562). ``config.schema``
re-exports :class:`RecoveryConfig` from this package, so eagerly importing
``policy`` here — which reaches ``observability`` and therefore back into
``config.schema`` — would create an import cycle. Lazy attribute access keeps
the public API identical while letting ``agentcore.recovery.config`` be
imported on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agentcore.recovery.classify import (
        assess_retry_safety,
        classify,
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
    from agentcore.recovery.history import RecoveryHistory, SubtaskRecoveryRecord
    from agentcore.recovery.policy import RecoveryPolicy, build_policy

#: Public name -> defining submodule.
_EXPORTS: dict[str, str] = {
    "RecoveryPolicy": "agentcore.recovery.policy",
    "build_policy": "agentcore.recovery.policy",
    "RecoveryConfig": "agentcore.recovery.config",
    "RecoveryDecision": "agentcore.recovery.decision",
    "RecoveryAction": "agentcore.recovery.decision",
    "FailureCategory": "agentcore.recovery.decision",
    "RetrySafety": "agentcore.recovery.decision",
    "Escalation": "agentcore.recovery.decision",
    "RecoveryHistory": "agentcore.recovery.history",
    "SubtaskRecoveryRecord": "agentcore.recovery.history",
    "classify": "agentcore.recovery.classify",
    "assess_retry_safety": "agentcore.recovery.classify",
    "failure_signature": "agentcore.recovery.classify",
    "is_recoverable_state": "agentcore.recovery.classify",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted(__all__)
