"""Permission configuration.

Lives in the permissions package rather than :mod:`agentcore.config` to keep
the layering acyclic: ``permissions`` is a lower layer that ``config``
aggregates, not the other way round. It is re-exported from
``agentcore.config.schema`` so callers still see one configuration surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentcore.errors import ConfigError
from agentcore.permissions.levels import PermissionLevel


@dataclass(frozen=True)
class PermissionConfig:
    """Default permission posture plus per-capability overrides."""

    level: PermissionLevel = PermissionLevel.OBSERVE
    # capability name -> minimum level required to run it unattended
    capability_overrides: dict[str, PermissionLevel] = field(default_factory=dict)
    # capabilities that are hard-blocked regardless of level
    blocked_capabilities: tuple[str, ...] = ()

    def validate(self) -> None:
        for cap, lvl in self.capability_overrides.items():
            if not isinstance(lvl, PermissionLevel):
                raise ConfigError(
                    "permissions.capability_overrides values must be PermissionLevel",
                    capability=cap,
                )
