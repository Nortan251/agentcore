"""Permission levels and the capability vocabulary.

Levels are ordered, from least to most autonomous:

``OBSERVE``     read-only; nothing that changes the world may run.
``PROPOSE``     the agent may describe an action but never execute a mutation.
``CONFIRM``     mutations run only after an explicit human approval.
``AUTONOMOUS``  mutations run unattended, subject to per-capability gates.
"""

from __future__ import annotations

from enum import Enum

_ORDER = {"observe": 0, "propose": 1, "confirm": 2, "autonomous": 3}


class PermissionLevel(str, Enum):
    OBSERVE = "observe"
    PROPOSE = "propose"
    CONFIRM = "confirm"
    AUTONOMOUS = "autonomous"

    @property
    def rank(self) -> int:
        return _ORDER[self.value]

    # NOTE: all four comparisons are defined explicitly. This enum inherits
    # ``str``, which already provides lexicographic ordering, so neither
    # ``functools.total_ordering`` nor a lone ``__lt__`` would take effect for
    # ``>``/``>=`` — comparisons would silently fall back to alphabetical order
    # ("autonomous" < "observe") and quietly invert permission checks.
    def __lt__(self, other: object) -> bool:
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, PermissionLevel):
            return NotImplemented
        return self.rank >= other.rank

    def __str__(self) -> str:  # keeps log output readable
        return self.value


class Capability(str, Enum):
    """Named capabilities a tool may require.

    This is the gating vocabulary. Listing a capability here does *not* mean an
    implementation exists — the computer-control and browser capabilities are
    intentionally declared but unimplemented in this foundation.
    """

    # read-only
    FS_READ = "fs.read"
    NET_READ = "net.read"
    SCREEN_OBSERVE = "screen.observe"

    # mutating
    FS_WRITE = "fs.write"
    FS_DELETE = "fs.delete"
    PROCESS_SPAWN = "process.spawn"
    NET_WRITE = "net.write"
    APP_CONTROL = "app.control"
    INPUT_CONTROL = "input.control"

    # high-risk; blocked by default policy in this foundation
    EMAIL_SEND = "email.send"
    PAYMENT = "payment"
    # Any live secret use, including a bound provider's authentication. The
    # sole contextual exception is host-authorized authenticated pure reads;
    # this is never an ordinary read capability or a model-selected grant.
    CREDENTIAL_ACCESS = "credential.access"
    SYSTEM_ADMIN = "system.admin"

    def __str__(self) -> str:
        return self.value


#: Capabilities that never mutate state and are safe at OBSERVE.
READ_ONLY_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.FS_READ,
    Capability.NET_READ,
    Capability.SCREEN_OBSERVE,
})

#: Ordinary requests for these capabilities are refused at every level. Only
#: CREDENTIAL_ACCESS has a reviewed, explicitly host-enabled pure-read context
#: in PermissionPolicy.check; the generic block and other capabilities remain.
DEFAULT_BLOCKED_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.EMAIL_SEND,
    Capability.PAYMENT,
    Capability.CREDENTIAL_ACCESS,
    Capability.SYSTEM_ADMIN,
})

#: Minimum level at which a capability may run *unattended* (no approval).
#:
#: Every mutating capability defaults to AUTONOMOUS, which is what makes the
#: CONFIRM level meaningful: below AUTONOMOUS a mutation is never silent, it
#: must pass through the approval gate. Operators can lower an individual entry
#: via ``permissions.capability_overrides`` to let a specific, well-understood
#: capability run unattended at a lower level.
DEFAULT_REQUIREMENTS: dict[Capability, PermissionLevel] = {
    Capability.FS_WRITE: PermissionLevel.AUTONOMOUS,
    Capability.FS_DELETE: PermissionLevel.AUTONOMOUS,
    Capability.PROCESS_SPAWN: PermissionLevel.AUTONOMOUS,
    Capability.NET_WRITE: PermissionLevel.AUTONOMOUS,
    Capability.APP_CONTROL: PermissionLevel.AUTONOMOUS,
    Capability.INPUT_CONTROL: PermissionLevel.AUTONOMOUS,
}
