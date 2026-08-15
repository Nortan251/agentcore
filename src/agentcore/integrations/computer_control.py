"""Placeholder adapter for the future local computer-control layer.

SCOPE: this file deliberately contains **no** automation. It does not move a
mouse, press a key, capture a screen or drive a native application, and it does
not attempt to reach any host. It exists to pin down the contract so the real
adapter — which will run against a separate, user-installed local service — can
be dropped in without touching the core agent.

It always reports UNAVAILABLE and provides zero tools.
"""

from __future__ import annotations

from typing import Sequence

from agentcore.integrations.base import Integration, IntegrationStatus
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool

#: Capabilities the eventual implementation will declare on its tools. Listed
#: here so the permission policy can be configured and tested ahead of time.
PLANNED_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.SCREEN_OBSERVE,
    Capability.INPUT_CONTROL,
    Capability.APP_CONTROL,
})


class ComputerControlAdapter(Integration):
    """Contract stub for the local-control layer. Not implemented."""

    def __init__(self, settings: dict | None = None) -> None:
        super().__init__("computer_control", settings)

    def connect(self) -> IntegrationStatus:
        self.status = IntegrationStatus.UNAVAILABLE
        self._log.info(
            "integration.not_implemented",
            detail="local computer-control layer is a future task; "
                   "no connection attempted",
            planned_capabilities=sorted(str(c) for c in PLANNED_CAPABILITIES),
        )
        return self.status

    def provided_tools(self) -> Sequence[Tool]:
        return ()
