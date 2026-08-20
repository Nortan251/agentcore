"""Immutable real-time turn records and deterministic lifecycle control.

This module is intentionally not integrated with conversation or autonomous
execution yet.  It defines only trusted identities, timing/fencing values, and
the durable state machine approved by Task 42.1.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from agentcore.realtime.cancellation import (
    CancellationReason,
    CancellationToken,
)
from agentcore.realtime.errors import (
    RealtimeError,
    TurnOperationConflictError,
    TurnTransitionError,
    TurnValidationError,
    TurnVersionConflictError,
)
from agentcore.util.clock import Clock, SystemClock
from agentcore.util.ids import new_id


TURN_SCHEMA_VERSION = 1
MAX_TRANSITIONS = 256
MAX_ACTION_REFERENCES = 64
MAX_NOTE_CHARS = 256
MAX_COUNTER_VALUE = 2**63 - 1

_TURN_ID = re.compile(r"^turn_[0-9a-f]{12}$")
_OPERATION_ID = re.compile(r"^op_[0-9a-f]{12}$")
_ACTION_ID = re.compile(r"^act_[0-9a-f]{12}$")
_RUN_ID = re.compile(r"^run_[0-9a-f]{12}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{12}$")
_SUBTASK_ID = re.compile(r"^sub_[0-9a-f]{12}$")
_SAFE_LINK_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CATEGORICAL_NOTES = frozenset({"", "restart reconciliation"})

# Domain separator for the opaque turn-session correlation key.  It is fixed
# and reserved for this derivation so the digest cannot collide with any other
# AgentCore key material.  The trailing NUL terminates the domain, so no raw
# session id can extend the domain string and forge another key.
_SESSION_KEY_DOMAIN = b"agentcore.realtime.turn.session.v1\x00"
_SESSION_KEY_PREFIX = "session_"
_SESSION_KEY_HEX_CHARS = 32


class TurnPhase(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    GENERATING = "generating"
    EXECUTING_TOOL = "executing_tool"
    WAITING_APPROVAL = "waiting_approval"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    CANCELLING = "cancelling"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    def __str__(self) -> str:
        return self.value

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_PHASES


TERMINAL_PHASES = frozenset({
    TurnPhase.COMPLETED,
    TurnPhase.FAILED,
    TurnPhase.CANCELLED,
    TurnPhase.TIMED_OUT,
})

LEGAL_TRANSITIONS: Mapping[TurnPhase, frozenset[TurnPhase]] = MappingProxyType({
    TurnPhase.CREATED: frozenset({TurnPhase.QUEUED, TurnPhase.CANCELLING}),
    TurnPhase.QUEUED: frozenset({TurnPhase.GENERATING,
                                 TurnPhase.CANCELLING}),
    TurnPhase.GENERATING: frozenset({
        TurnPhase.EXECUTING_TOOL,
        TurnPhase.WAITING_APPROVAL,
        TurnPhase.FINALIZING,
        TurnPhase.CANCELLING,
    }),
    TurnPhase.EXECUTING_TOOL: frozenset({
        TurnPhase.GENERATING,
        TurnPhase.VERIFYING,
        TurnPhase.FINALIZING,
        TurnPhase.CANCELLING,
    }),
    TurnPhase.WAITING_APPROVAL: frozenset({TurnPhase.QUEUED,
                                           TurnPhase.CANCELLING}),
    TurnPhase.VERIFYING: frozenset({TurnPhase.RECOVERING,
                                    TurnPhase.FINALIZING,
                                    TurnPhase.CANCELLING}),
    TurnPhase.RECOVERING: frozenset({TurnPhase.EXECUTING_TOOL,
                                     TurnPhase.FINALIZING,
                                     TurnPhase.CANCELLING}),
    TurnPhase.CANCELLING: frozenset({TurnPhase.FINALIZING}),
    TurnPhase.FINALIZING: TERMINAL_PHASES,
    TurnPhase.COMPLETED: frozenset(),
    TurnPhase.FAILED: frozenset(),
    TurnPhase.CANCELLED: frozenset(),
    TurnPhase.TIMED_OUT: frozenset(),
})


@dataclass(frozen=True)
class TurnIdentity:
    turn_id: str
    session_id: str
    generation: int

    def validate(self) -> TurnIdentity:
        _validate_turn_id(self.turn_id)
        _validate_link_id(self.session_id, "session_id")
        _require_positive_int(self.generation, "generation")
        return self


@dataclass(frozen=True)
class TurnCounters:
    """Fixed, bounded counters; never a bag of model-controlled keys."""

    model_requests: int = 0
    tool_calls: int = 0
    verification_attempts: int = 0
    recovery_attempts: int = 0
    stale_results: int = 0

    def validate(self) -> TurnCounters:
        for name in (
            "model_requests",
            "tool_calls",
            "verification_attempts",
            "recovery_attempts",
            "stale_results",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or \
                    not 0 <= value <= MAX_COUNTER_VALUE:
                raise TurnValidationError("turn counter is invalid",
                                          field=name)
        return self

    def to_dict(self) -> dict[str, int]:
        self.validate()
        return {
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "verification_attempts": self.verification_attempts,
            "recovery_attempts": self.recovery_attempts,
            "stale_results": self.stale_results,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnCounters:
        expected = {
            "model_requests",
            "tool_calls",
            "verification_attempts",
            "recovery_attempts",
            "stale_results",
        }
        _require_exact_keys(data, expected, "turn counters")
        return cls(**{name: _require_nonnegative_int(data[name], name)
                      for name in expected}).validate()


@dataclass(frozen=True)
class TransitionRecord:
    """Accepted transition plus enough result state for exact replay."""

    operation_id: str
    from_phase: TurnPhase
    to_phase: TurnPhase
    from_version: int
    to_version: int
    at: datetime
    note: str = ""
    cancellation_reason: CancellationReason | None = None
    requested_terminal: TurnPhase | None = None
    final_terminal: TurnPhase | None = None

    def to_dict(self) -> dict[str, Any]:
        _validate_transition_shape(self)
        return {
            "operation_id": self.operation_id,
            "from_phase": self.from_phase.value,
            "to_phase": self.to_phase.value,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "at": _format_utc(self.at, "transition.at"),
            "note": self.note,
            "cancellation_reason": (
                self.cancellation_reason.value
                if self.cancellation_reason is not None else None
            ),
            "requested_terminal": (
                self.requested_terminal.value
                if self.requested_terminal is not None else None
            ),
            "final_terminal": (
                self.final_terminal.value
                if self.final_terminal is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TransitionRecord:
        expected = {
            "operation_id",
            "from_phase",
            "to_phase",
            "from_version",
            "to_version",
            "at",
            "note",
            "cancellation_reason",
            "requested_terminal",
            "final_terminal",
        }
        _require_exact_keys(data, expected, "turn transition")
        transition = cls(
            operation_id=_require_operation_id(data["operation_id"]),
            from_phase=_parse_phase(data["from_phase"], "from_phase"),
            to_phase=_parse_phase(data["to_phase"], "to_phase"),
            from_version=_require_nonnegative_int(data["from_version"],
                                                  "from_version"),
            to_version=_require_nonnegative_int(data["to_version"],
                                                "to_version"),
            at=_parse_utc(data["at"], "transition.at"),
            note=_validate_note(data["note"]),
            cancellation_reason=_parse_reason(data["cancellation_reason"]),
            requested_terminal=_parse_optional_phase(
                data["requested_terminal"], "requested_terminal"),
            final_terminal=_parse_optional_phase(data["final_terminal"],
                                                 "final_terminal"),
        )
        _validate_transition_shape(transition)
        return transition


@dataclass(frozen=True)
class TurnSnapshot:
    turn_id: str
    session_id: str
    generation: int
    phase: TurnPhase
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: int = TURN_SCHEMA_VERSION
    run_id: str | None = None
    task_id: str | None = None
    subtask_id: str | None = None
    cancellation_reason: CancellationReason | None = None
    requested_terminal: TurnPhase | None = None
    final_terminal: TurnPhase | None = None
    deadline_at: datetime | None = None
    counters: TurnCounters = field(default_factory=TurnCounters)
    action_refs: tuple[str, ...] = ()
    active_operation_id: str | None = None


@dataclass(frozen=True)
class TurnRecord:
    """Schema-v1 durable state for exactly one real-time turn."""

    turn_id: str
    session_id: str
    generation: int
    phase: TurnPhase
    version: int
    transitions: tuple[TransitionRecord, ...]
    created_at: datetime
    updated_at: datetime
    schema_version: int = TURN_SCHEMA_VERSION
    run_id: str | None = None
    task_id: str | None = None
    subtask_id: str | None = None
    cancellation_reason: CancellationReason | None = None
    requested_terminal: TurnPhase | None = None
    final_terminal: TurnPhase | None = None
    deadline_at: datetime | None = None
    counters: TurnCounters = field(default_factory=TurnCounters)
    action_refs: tuple[str, ...] = ()
    active_operation_id: str | None = None

    @property
    def identity(self) -> TurnIdentity:
        return TurnIdentity(self.turn_id, self.session_id, self.generation)

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    def snapshot(self) -> TurnSnapshot:
        return TurnSnapshot(
            turn_id=self.turn_id,
            session_id=self.session_id,
            generation=self.generation,
            phase=self.phase,
            version=self.version,
            created_at=self.created_at,
            updated_at=self.updated_at,
            schema_version=self.schema_version,
            run_id=self.run_id,
            task_id=self.task_id,
            subtask_id=self.subtask_id,
            cancellation_reason=self.cancellation_reason,
            requested_terminal=self.requested_terminal,
            final_terminal=self.final_terminal,
            deadline_at=self.deadline_at,
            counters=self.counters,
            action_refs=self.action_refs,
            active_operation_id=self.active_operation_id,
        )

    def snapshot_after(self, transition: TransitionRecord) -> TurnSnapshot:
        """Reconstruct the exact accepted state for idempotent replay."""
        return replace(
            self.snapshot(),
            phase=transition.to_phase,
            version=transition.to_version,
            updated_at=transition.at,
            cancellation_reason=transition.cancellation_reason,
            requested_terminal=transition.requested_terminal,
            final_terminal=transition.final_terminal,
            active_operation_id=transition.operation_id,
        )

    def validate(self) -> TurnRecord:
        if isinstance(self.schema_version, bool) or \
                not isinstance(self.schema_version, int) or \
                self.schema_version != TURN_SCHEMA_VERSION:
            raise TurnValidationError("unsupported turn schema version",
                                      supported=TURN_SCHEMA_VERSION)
        self.identity.validate()
        if self.run_id is not None:
            _require_run_id(self.run_id)
        if self.task_id is not None:
            _require_task_id(self.task_id)
        if self.subtask_id is not None:
            _require_subtask_id(self.subtask_id)
            if self.task_id is None:
                raise TurnValidationError(
                    "subtask_id requires a task_id correlation")
        if not isinstance(self.phase, TurnPhase):
            raise TurnValidationError("turn phase is invalid")
        _require_nonnegative_int(self.version, "version")
        if not isinstance(self.transitions, tuple):
            raise TurnValidationError("turn transitions must be a tuple")
        if self.version != len(self.transitions):
            raise TurnValidationError("record version does not match ledger",
                                      version=self.version,
                                      transitions=len(self.transitions))
        phase_limit = _transition_limit_for_phase(self.phase)
        if self.version > phase_limit:
            raise TurnValidationError(
                "turn transition reserve is invalid for phase",
                phase=self.phase.value,
                version=self.version,
                maximum=phase_limit,
            )
        created = _ensure_utc(self.created_at, "created_at")
        updated = _ensure_utc(self.updated_at, "updated_at")
        if updated < created:
            raise TurnValidationError("updated_at precedes created_at")
        if self.deadline_at is not None:
            _ensure_utc(self.deadline_at, "deadline_at")
        if not isinstance(self.counters, TurnCounters):
            raise TurnValidationError("turn counters are invalid")
        self.counters.validate()
        _validate_action_refs(self.action_refs)
        _validate_optional_reason(self.cancellation_reason)
        _validate_optional_terminal(self.requested_terminal,
                                    "requested_terminal")
        _validate_optional_terminal(self.final_terminal, "final_terminal")

        phase = TurnPhase.CREATED
        cancellation_reason: CancellationReason | None = None
        requested_terminal: TurnPhase | None = None
        final_terminal: TurnPhase | None = None
        previous_time = created
        operation_ids: set[str] = set()
        for index, transition in enumerate(self.transitions, start=1):
            _validate_transition_shape(transition)
            if transition.operation_id in operation_ids:
                raise TurnValidationError("duplicate transition operation id",
                                          operation_id=transition.operation_id)
            operation_ids.add(transition.operation_id)
            if transition.from_version != index - 1 or \
                    transition.to_version != index:
                raise TurnValidationError("transition versions are not linear",
                                          operation_id=transition.operation_id)
            transition_limit = _transition_limit_for_phase(
                transition.to_phase)
            if transition.to_version > transition_limit:
                raise TurnValidationError(
                    "ledger exhausted the transition reserve",
                    operation_id=transition.operation_id,
                    phase=transition.to_phase.value,
                    maximum=transition_limit,
                )
            if transition.from_phase is not phase:
                raise TurnValidationError("transition phase chain is broken",
                                          operation_id=transition.operation_id)
            if transition.at < previous_time:
                raise TurnValidationError("transition timestamps regress",
                                          operation_id=transition.operation_id)
            cancellation_reason, requested_terminal, final_terminal = \
                _validated_result_state(
                    phase,
                    transition.to_phase,
                    cancellation_reason,
                    requested_terminal,
                    final_terminal,
                    transition.cancellation_reason,
                    transition.requested_terminal,
                    transition.final_terminal,
                )
            phase = transition.to_phase
            previous_time = transition.at

        if phase is not self.phase:
            raise TurnValidationError("record phase does not match ledger")
        if self.transitions and updated != self.transitions[-1].at:
            raise TurnValidationError("updated_at does not match ledger")
        if not self.transitions and (self.phase is not TurnPhase.CREATED or
                                     updated != created):
            raise TurnValidationError("new turn record is inconsistent")
        if cancellation_reason is not self.cancellation_reason or \
                requested_terminal is not self.requested_terminal or \
                final_terminal is not self.final_terminal:
            raise TurnValidationError("record outcome does not match ledger")
        expected_active = (self.transitions[-1].operation_id
                           if self.transitions else None)
        if self.active_operation_id != expected_active:
            raise TurnValidationError("active operation does not match ledger")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "generation": self.generation,
            "phase": self.phase.value,
            "version": self.version,
            "transitions": [item.to_dict() for item in self.transitions],
            "created_at": _format_utc(self.created_at, "created_at"),
            "updated_at": _format_utc(self.updated_at, "updated_at"),
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "subtask_id": self.subtask_id,
            "cancellation_reason": (
                self.cancellation_reason.value
                if self.cancellation_reason is not None else None
            ),
            "requested_terminal": (
                self.requested_terminal.value
                if self.requested_terminal is not None else None
            ),
            "final_terminal": (
                self.final_terminal.value
                if self.final_terminal is not None else None
            ),
            "deadline_at": (
                _format_utc(self.deadline_at, "deadline_at")
                if self.deadline_at is not None else None
            ),
            "counters": self.counters.to_dict(),
            "action_refs": list(self.action_refs),
            "active_operation_id": self.active_operation_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnRecord:
        expected = {
            "turn_id",
            "session_id",
            "generation",
            "phase",
            "version",
            "transitions",
            "created_at",
            "updated_at",
            "schema_version",
            "run_id",
            "task_id",
            "subtask_id",
            "cancellation_reason",
            "requested_terminal",
            "final_terminal",
            "deadline_at",
            "counters",
            "action_refs",
            "active_operation_id",
        }
        _require_exact_keys(data, expected, "turn record")
        schema_version = _require_positive_int(data["schema_version"],
                                               "schema_version")
        if schema_version != TURN_SCHEMA_VERSION:
            raise TurnValidationError("unsupported turn schema version",
                                      schema_version=schema_version,
                                      supported=TURN_SCHEMA_VERSION)
        raw_transitions = data["transitions"]
        if not isinstance(raw_transitions, list):
            raise TurnValidationError("turn transitions must be a list")
        raw_counters = data["counters"]
        if not isinstance(raw_counters, Mapping):
            raise TurnValidationError("turn counters must be a mapping")
        raw_actions = data["action_refs"]
        if not isinstance(raw_actions, list):
            raise TurnValidationError("action_refs must be a list")
        record = cls(
            turn_id=_require_turn_id(data["turn_id"]),
            session_id=_require_link_id(data["session_id"], "session_id"),
            generation=_require_positive_int(data["generation"], "generation"),
            phase=_parse_phase(data["phase"], "phase"),
            version=_require_nonnegative_int(data["version"], "version"),
            transitions=tuple(TransitionRecord.from_dict(item)
                              if isinstance(item, Mapping) else
                              _raise_transition_mapping()
                              for item in raw_transitions),
            created_at=_parse_utc(data["created_at"], "created_at"),
            updated_at=_parse_utc(data["updated_at"], "updated_at"),
            schema_version=schema_version,
            run_id=(_require_run_id(data["run_id"])
                    if data["run_id"] is not None else None),
            task_id=(_require_task_id(data["task_id"])
                     if data["task_id"] is not None else None),
            subtask_id=(_require_subtask_id(data["subtask_id"])
                        if data["subtask_id"] is not None else None),
            cancellation_reason=_parse_reason(data["cancellation_reason"]),
            requested_terminal=_parse_optional_phase(
                data["requested_terminal"], "requested_terminal"),
            final_terminal=_parse_optional_phase(data["final_terminal"],
                                                 "final_terminal"),
            deadline_at=(_parse_utc(data["deadline_at"], "deadline_at")
                         if data["deadline_at"] is not None else None),
            counters=TurnCounters.from_dict(raw_counters),
            action_refs=tuple(_require_action_id(value)
                              for value in raw_actions),
            active_operation_id=(
                _require_operation_id(data["active_operation_id"])
                if data["active_operation_id"] is not None else None
            ),
        )
        return record.validate()


@dataclass(frozen=True)
class Deadline:
    """Monotonic decision deadline with a UTC persistence projection.

    Both fields are ``None`` for the unbounded deadline defined by the Task
    42.3 amendment.  An unbounded deadline never expires, carries no synthetic
    far-future timestamp, and is never persisted as an actual expiry.  It is
    scaffolding for the ``OperationContext`` a stale fence needs, not timeout
    enforcement.
    """

    monotonic_expires_at: float | None
    expires_at_utc: datetime | None

    def __post_init__(self) -> None:
        if self.monotonic_expires_at is None or self.expires_at_utc is None:
            if self.monotonic_expires_at is not None or \
                    self.expires_at_utc is not None:
                raise TurnValidationError(
                    "an unbounded deadline carries neither expiry")
            return
        _validate_monotonic(self.monotonic_expires_at)
        _ensure_utc(self.expires_at_utc, "expires_at_utc")

    @classmethod
    def unbounded(cls) -> Deadline:
        """A named deadline that never expires and is never persisted."""
        return cls(None, None)

    @property
    def is_unbounded(self) -> bool:
        return self.monotonic_expires_at is None

    @classmethod
    def after(cls, seconds: float, *, clock: Clock) -> Deadline:
        duration = _validate_duration(seconds)
        now_mono = _validate_monotonic(clock.monotonic())
        now_utc = _ensure_utc(clock.now(), "clock.now")
        return cls(_checked_monotonic_add(now_mono, duration),
                   _checked_utc_add(now_utc, duration))

    @classmethod
    def from_utc(cls, expires_at_utc: datetime, *, clock: Clock) -> Deadline:
        expiry = _ensure_utc(expires_at_utc, "expires_at_utc")
        now_utc = _ensure_utc(clock.now(), "clock.now")
        remaining = max(0.0, (expiry - now_utc).total_seconds())
        now_mono = _validate_monotonic(clock.monotonic())
        return cls(_checked_monotonic_add(now_mono, remaining), expiry)

    def remaining(self, clock: Clock) -> float:
        now_mono = _validate_monotonic(clock.monotonic())
        if self.is_unbounded:
            return math.inf
        remaining = self.monotonic_expires_at - now_mono
        if not math.isfinite(remaining):
            raise TurnValidationError("monotonic deadline range is invalid")
        return max(0.0, remaining)

    def expired(self, clock: Clock) -> bool:
        now_mono = _validate_monotonic(clock.monotonic())
        if self.is_unbounded:
            return False
        return now_mono >= self.monotonic_expires_at

    def child(self, max_seconds: float, *, clock: Clock) -> Deadline:
        duration = _validate_duration(max_seconds)
        now_mono = _validate_monotonic(clock.monotonic())
        now_utc = _ensure_utc(clock.now(), "clock.now")
        requested_mono = _checked_monotonic_add(now_mono, duration)
        if self.is_unbounded:
            # A child may shorten an unbounded parent; it can never extend one,
            # because an unbounded parent has no expiry to extend.
            return Deadline(requested_mono,
                            _checked_utc_add(now_utc, duration))
        child_mono = min(self.monotonic_expires_at, requested_mono)
        actual = max(0.0, child_mono - now_mono)
        child_utc = min(self.expires_at_utc,
                        _checked_utc_add(now_utc, actual))
        return Deadline(child_mono, child_utc)


@dataclass(frozen=True)
class OperationContext:
    turn_id: str
    session_id: str
    generation: int
    operation_id: str
    cancellation: CancellationToken
    deadline: Deadline

    def __post_init__(self) -> None:
        _validate_turn_id(self.turn_id)
        _validate_link_id(self.session_id, "session_id")
        _require_positive_int(self.generation, "generation")
        _require_operation_id(self.operation_id)
        if not isinstance(self.deadline, Deadline):
            raise TurnValidationError("operation deadline is invalid")
        for attribute in ("requested", "reason", "raise_if_requested",
                          "register"):
            if not hasattr(self.cancellation, attribute):
                raise TurnValidationError("operation cancellation token is invalid")


class _TurnStore(Protocol):
    def load(self, turn_id: str) -> TurnRecord: ...

    def compare_and_swap(self, turn_id: str, expected_version: int,
                         next_record: TurnRecord) -> TurnRecord: ...


class TurnController:
    """CAS-backed mutation authority for one durable turn record."""

    def __init__(self, store: _TurnStore, turn_id: str, *,
                 clock: Clock | None = None) -> None:
        self._store = store
        self._turn_id = _require_turn_id(turn_id)
        self._clock = clock or SystemClock()

    def snapshot(self) -> TurnSnapshot:
        return self._store.load(self._turn_id).snapshot()

    def transition(
        self,
        expected_phase: TurnPhase,
        target: TurnPhase,
        *,
        expected_version: int,
        operation_id: str,
        note: str = "",
        requested_terminal: TurnPhase | None = None,
        cancellation_reason: CancellationReason | None = None,
    ) -> TurnSnapshot:
        expected_phase = _require_phase(expected_phase, "expected_phase")
        target = _require_phase(target, "target")
        expected_version = _require_nonnegative_int(expected_version,
                                                    "expected_version")
        operation_id = _require_operation_id(operation_id)
        note = _validate_note(note)
        if requested_terminal is not None:
            requested_terminal = _require_terminal(requested_terminal)
        if cancellation_reason is not None and \
                not isinstance(cancellation_reason, CancellationReason):
            raise TurnValidationError("cancellation reason is invalid")

        current = self._store.load(self._turn_id)
        replay = _find_transition(current, operation_id)
        if replay is not None:
            if current.is_terminal and replay.to_phase is not current.phase:
                raise TurnTransitionError(
                    "terminal turn only permits replay of its terminal transition",
                    turn_id=self._turn_id,
                )
            _validate_replay(current, replay, expected_phase, target,
                             expected_version, note, requested_terminal,
                             cancellation_reason)
            return current.snapshot_after(replay)
        if current.version != expected_version:
            raise TurnVersionConflictError(
                "turn record version conflict",
                turn_id=self._turn_id,
                expected_version=expected_version,
                actual_version=current.version,
            )
        if current.phase is not expected_phase:
            raise TurnTransitionError(
                "turn phase does not match expected phase",
                turn_id=self._turn_id,
                expected=expected_phase.value,
                actual=current.phase.value,
            )
        observed_at = _ensure_utc(self._clock.now(), "clock.now")
        next_record = _apply_transition(
            current,
            target,
            operation_id=operation_id,
            at=max(observed_at, current.updated_at),
            note=note,
            requested_terminal=requested_terminal,
            cancellation_reason=cancellation_reason,
        )
        try:
            accepted = self._store.compare_and_swap(
                self._turn_id, expected_version, next_record)
        except TurnVersionConflictError:
            latest = self._store.load(self._turn_id)
            replay = _find_transition(latest, operation_id)
            if replay is None:
                raise
            if latest.is_terminal and replay.to_phase is not latest.phase:
                raise TurnTransitionError(
                    "terminal turn only permits replay of its terminal transition",
                    turn_id=self._turn_id,
                )
            _validate_replay(latest, replay, expected_phase, target,
                             expected_version, note, requested_terminal,
                             cancellation_reason)
            return latest.snapshot_after(replay)
        return accepted.snapshot()

    def accepts(self, context: OperationContext, *,
                expected_phase: TurnPhase) -> bool:
        """Truthfully fence stale, cancelled, or expired logical callbacks."""
        if not isinstance(context, OperationContext):
            return False
        if context.turn_id != self._turn_id:
            return False
        try:
            expected_phase = _require_phase(expected_phase, "expected_phase")
            current = self._store.load(self._turn_id)
            if current.is_terminal or \
                    current.session_id != context.session_id or \
                    current.generation != context.generation or \
                    current.phase is not expected_phase or \
                    current.active_operation_id != context.operation_id:
                return False
            if context.cancellation.requested or \
                    context.deadline.expired(self._clock):
                return False
            if current.deadline_at is not None and (
                    context.deadline.expires_at_utc is None
                    or context.deadline.expires_at_utc > current.deadline_at):
                # A record-bound deadline is authoritative; an unbounded or
                # longer operation deadline can never satisfy it.
                return False
            return True
        except (RealtimeError, ValueError):
            return False


def new_turn(
    session_id: str,
    generation: int,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
    subtask_id: str | None = None,
    deadline: Deadline | None = None,
    action_refs: tuple[str, ...] = (),
    clock: Clock | None = None,
    id_factory: Callable[[str], str] = new_id,
) -> TurnRecord:
    """Create a validated schema-v1 record without admitting or running it."""
    clock = clock or SystemClock()
    turn_id = _require_turn_id(id_factory("turn"))
    session_id = _require_link_id(session_id, "session_id")
    generation = _require_positive_int(generation, "generation")
    if run_id is not None:
        run_id = _require_run_id(run_id)
    if task_id is not None:
        task_id = _require_task_id(task_id)
    if subtask_id is not None:
        subtask_id = _require_subtask_id(subtask_id)
        if task_id is None:
            raise TurnValidationError(
                "subtask_id requires a task_id correlation")
    if deadline is not None and not isinstance(deadline, Deadline):
        raise TurnValidationError("turn deadline is invalid")
    _validate_action_refs(action_refs)
    now = _ensure_utc(clock.now(), "clock.now")
    record = TurnRecord(
        turn_id=turn_id,
        session_id=session_id,
        generation=generation,
        phase=TurnPhase.CREATED,
        version=0,
        transitions=(),
        created_at=now,
        updated_at=now,
        run_id=run_id,
        task_id=task_id,
        subtask_id=subtask_id,
        deadline_at=deadline.expires_at_utc if deadline is not None else None,
        action_refs=action_refs,
    )
    return record.validate()


def new_operation_id(*,
                     id_factory: Callable[[str], str] = new_id) -> str:
    return _require_operation_id(id_factory("op"))


def require_session_id(value: Any) -> str:
    """Validate one conversation-session correlation identifier."""
    return _require_link_id(value, "session_id")


def derive_session_key(raw_session_id: str) -> str:
    """Map a raw conversation session id onto its opaque correlation key.

    Task 42.3 stores this derived key in ``TurnRecord.session_id`` instead of
    the raw ``ConversationSession`` identifier, so raw ids keeping spaces,
    Unicode, ``#``, ``.`` or ``..`` stay valid at the conversation layer while
    durable turn records keep the safe link-id charset.  The derivation is a
    domain-separated SHA-256 over the exact UTF-8 bytes of the raw id: it is
    deterministic, collision-resistant, and one way, so the raw id cannot be
    recovered from a record, an error, or an event.  An empty raw id is a
    caller error, never a valid key.
    """
    if not isinstance(raw_session_id, str) or not raw_session_id:
        raise TurnValidationError("conversation session id is required")
    digest = hashlib.sha256(
        _SESSION_KEY_DOMAIN + raw_session_id.encode("utf-8")).hexdigest()
    return _require_link_id(
        _SESSION_KEY_PREFIX + digest[:_SESSION_KEY_HEX_CHARS], "session_id")


def next_generation(previous: int | None) -> int:
    """Validated allocation primitive; admission ownership arrives later."""
    if previous is None:
        return 1
    value = _require_positive_int(previous, "previous_generation")
    if value >= MAX_COUNTER_VALUE:
        raise TurnValidationError("generation limit reached")
    return value + 1


def terminal_for_reason(reason: CancellationReason) -> TurnPhase:
    if not isinstance(reason, CancellationReason):
        raise TurnValidationError("cancellation reason is invalid")
    if reason is CancellationReason.DEADLINE_EXCEEDED:
        return TurnPhase.TIMED_OUT
    if reason is CancellationReason.INTERNAL_FAILURE:
        return TurnPhase.FAILED
    return TurnPhase.CANCELLED


def _apply_transition(
    current: TurnRecord,
    target: TurnPhase,
    *,
    operation_id: str,
    at: datetime,
    note: str,
    requested_terminal: TurnPhase | None,
    cancellation_reason: CancellationReason | None,
) -> TurnRecord:
    if current.phase.is_terminal:
        raise TurnTransitionError("terminal turn cannot transition",
                                  turn_id=current.turn_id,
                                  current=current.phase.value,
                                  target=target.value)
    if target not in LEGAL_TRANSITIONS[current.phase]:
        raise TurnTransitionError("illegal turn phase transition",
                                  turn_id=current.turn_id,
                                  current=current.phase.value,
                                  target=target.value)
    next_version = current.version + 1
    phase_limit = _transition_limit_for_phase(target)
    if next_version > phase_limit:
        raise TurnTransitionError(
            "turn transition reserve reached",
            turn_id=current.turn_id,
            target=target.value,
            maximum=phase_limit,
        )
    next_reason, next_requested, next_final = _resolve_result_state(
        current.phase,
        target,
        current.cancellation_reason,
        current.requested_terminal,
        current.final_terminal,
        requested_terminal,
        cancellation_reason,
        turn_id=current.turn_id,
    )

    transition = TransitionRecord(
        operation_id=operation_id,
        from_phase=current.phase,
        to_phase=target,
        from_version=current.version,
        to_version=next_version,
        at=at,
        note=note,
        cancellation_reason=next_reason,
        requested_terminal=next_requested,
        final_terminal=next_final,
    )
    return replace(
        current,
        phase=target,
        version=next_version,
        transitions=(*current.transitions, transition),
        updated_at=at,
        cancellation_reason=next_reason,
        requested_terminal=next_requested,
        final_terminal=next_final,
        active_operation_id=operation_id,
    ).validate()


def _transition_limit_for_phase(phase: TurnPhase) -> int:
    """Reserve enough ledger entries to cancel, finalize, and terminate."""
    if phase.is_terminal:
        return MAX_TRANSITIONS
    if phase is TurnPhase.FINALIZING:
        return MAX_TRANSITIONS - 1
    if phase is TurnPhase.CANCELLING:
        return MAX_TRANSITIONS - 2
    return MAX_TRANSITIONS - 3


def _resolve_result_state(
    current_phase: TurnPhase,
    target: TurnPhase,
    current_reason: CancellationReason | None,
    current_requested: TurnPhase | None,
    current_final: TurnPhase | None,
    requested_terminal: TurnPhase | None,
    cancellation_reason: CancellationReason | None,
    *,
    turn_id: str,
) -> tuple[CancellationReason | None, TurnPhase | None, TurnPhase | None]:
    """Resolve one trusted transition request into its durable outcome."""
    next_reason = current_reason
    next_requested = current_requested
    next_final = current_final

    if target is TurnPhase.CANCELLING:
        if cancellation_reason is None:
            raise TurnTransitionError("cancelling requires a reason",
                                      turn_id=turn_id)
        if next_reason is not None and next_reason is not cancellation_reason:
            raise TurnTransitionError("cancellation reason is immutable",
                                      turn_id=turn_id)
        next_reason = cancellation_reason
        derived = terminal_for_reason(cancellation_reason)
        if requested_terminal is not None and requested_terminal is not derived:
            raise TurnTransitionError("cancellation terminal does not match reason",
                                      turn_id=turn_id)
        if next_requested is not None and next_requested is not derived:
            raise TurnTransitionError("requested terminal is immutable",
                                      turn_id=turn_id)
        next_requested = derived
    elif target is TurnPhase.FINALIZING:
        if current_phase is not TurnPhase.CANCELLING:
            success = (requested_terminal is TurnPhase.COMPLETED
                       and cancellation_reason is None)
            internal_failure = (
                requested_terminal is TurnPhase.FAILED
                and cancellation_reason is CancellationReason.INTERNAL_FAILURE
            )
            if not (success or internal_failure):
                raise TurnTransitionError(
                    "cancellation and timeout must pass through cancelling",
                    turn_id=turn_id,
                )
            next_reason = cancellation_reason
            next_requested = requested_terminal
        else:
            if cancellation_reason is not None:
                if next_reason is not cancellation_reason:
                    raise TurnTransitionError(
                        "cancellation reason is immutable", turn_id=turn_id)
            selected = requested_terminal or next_requested
            if selected is None:
                raise TurnTransitionError(
                    "finalizing requires a terminal outcome", turn_id=turn_id)
            _validate_terminal_selection(selected, next_reason)
            if next_requested is not selected:
                raise TurnTransitionError("requested terminal is immutable",
                                          turn_id=turn_id)
            next_requested = selected
    elif current_phase is TurnPhase.FINALIZING:
        restart_failure = (
            target is TurnPhase.FAILED
            and current_requested is TurnPhase.COMPLETED
            and cancellation_reason is CancellationReason.INTERNAL_FAILURE
            and requested_terminal is None
        )
        if restart_failure:
            # A crash while finalizing success is evidence of failure, never
            # permission to manufacture the previously requested success.
            next_reason = CancellationReason.INTERNAL_FAILURE
        else:
            if requested_terminal is not None or cancellation_reason is not None:
                raise TurnTransitionError(
                    "outcome data is not accepted for terminalization",
                    turn_id=turn_id,
                )
            if target is not current_requested:
                raise TurnTransitionError(
                    "terminal outcome is already fixed",
                    turn_id=turn_id,
                    requested=(current_requested.value
                               if current_requested else None),
                    target=target.value,
                )
        next_final = target
    else:
        if requested_terminal is not None or cancellation_reason is not None:
            raise TurnTransitionError(
                "outcome data is only accepted while cancelling or finalizing",
                turn_id=turn_id,
            )
    return next_reason, next_requested, next_final


def _validated_result_state(
    current_phase: TurnPhase,
    target: TurnPhase,
    current_reason: CancellationReason | None,
    current_requested: TurnPhase | None,
    current_final: TurnPhase | None,
    resulting_reason: CancellationReason | None,
    resulting_requested: TurnPhase | None,
    resulting_final: TurnPhase | None,
) -> tuple[CancellationReason | None, TurnPhase | None, TurnPhase | None]:
    if target not in LEGAL_TRANSITIONS[current_phase]:
        raise TurnValidationError("ledger contains an illegal transition",
                                  current=current_phase.value,
                                  target=target.value)
    restart_failure = False
    if current_phase is TurnPhase.FINALIZING:
        restart_failure = (
            current_requested is TurnPhase.COMPLETED
            and target is TurnPhase.FAILED
            and resulting_reason is CancellationReason.INTERNAL_FAILURE
            and resulting_requested is TurnPhase.COMPLETED
            and resulting_final is TurnPhase.FAILED
        )
        if not restart_failure and (
                target is not current_requested or resulting_final is not target):
            raise TurnValidationError("ledger changed a fixed terminal outcome")
    elif resulting_final is not current_final:
        raise TurnValidationError("ledger set a terminal outcome too early")

    if target is TurnPhase.CANCELLING:
        if resulting_reason is None or \
                resulting_requested is not terminal_for_reason(resulting_reason):
            raise TurnValidationError("ledger cancellation outcome is invalid")
    elif target is TurnPhase.FINALIZING:
        if resulting_requested is None:
            raise TurnValidationError("ledger finalizing outcome is missing")
        _validate_terminal_selection(
            resulting_requested,
            resulting_reason,
            error_type=TurnValidationError,
        )
        if current_phase is not TurnPhase.CANCELLING:
            direct_success = (
                resulting_requested is TurnPhase.COMPLETED
                and resulting_reason is None
            )
            direct_internal_failure = (
                resulting_requested is TurnPhase.FAILED
                and resulting_reason is CancellationReason.INTERNAL_FAILURE
            )
            if not (direct_success or direct_internal_failure):
                raise TurnValidationError(
                    "ledger bypassed cancelling for cancellation or timeout")
    elif not restart_failure and (
        resulting_reason is not current_reason
        or resulting_requested is not current_requested
    ):
        raise TurnValidationError("ledger changed outcome outside its boundary")
    if not restart_failure and current_reason is not None and \
            resulting_reason is not current_reason:
        raise TurnValidationError("ledger changed cancellation reason")
    if current_requested is not None and \
            resulting_requested is not current_requested:
        raise TurnValidationError("ledger changed requested terminal")
    return resulting_reason, resulting_requested, resulting_final


def _validate_terminal_selection(
    selected: TurnPhase,
    reason: CancellationReason | None,
    *,
    error_type: type[TurnTransitionError] | type[TurnValidationError] =
        TurnTransitionError,
) -> None:
    _require_terminal(selected)
    if selected is TurnPhase.COMPLETED:
        if reason is not None:
            raise error_type("completed turn cannot carry cancellation")
        return
    if reason is None or terminal_for_reason(reason) is not selected:
        raise error_type("terminal outcome does not match reason",
                         terminal=selected.value,
                         reason=reason.value if reason else None)


def _find_transition(record: TurnRecord,
                     operation_id: str) -> TransitionRecord | None:
    return next((item for item in record.transitions
                 if item.operation_id == operation_id), None)


def _validate_replay(
    record: TurnRecord,
    transition: TransitionRecord,
    expected_phase: TurnPhase,
    target: TurnPhase,
    expected_version: int,
    note: str,
    requested_terminal: TurnPhase | None,
    cancellation_reason: CancellationReason | None,
) -> None:
    signature_matches = (
        transition.from_phase is expected_phase
        and transition.to_phase is target
        and transition.from_version == expected_version
        and transition.note == note
    )
    if not signature_matches:
        _raise_operation_conflict(transition.operation_id)

    if transition.from_version == 0:
        prior_reason = None
        prior_requested = None
        prior_final = None
    else:
        prior = record.transitions[transition.from_version - 1]
        prior_reason = prior.cancellation_reason
        prior_requested = prior.requested_terminal
        prior_final = prior.final_terminal
    try:
        result = _resolve_result_state(
            expected_phase,
            target,
            prior_reason,
            prior_requested,
            prior_final,
            requested_terminal,
            cancellation_reason,
            turn_id=record.turn_id,
        )
    except (TurnTransitionError, TurnValidationError) as exc:
        raise TurnOperationConflictError(
            "operation id was reused for a different transition",
            operation_id=transition.operation_id,
        ) from exc
    if result != (
        transition.cancellation_reason,
        transition.requested_terminal,
        transition.final_terminal,
    ):
        _raise_operation_conflict(transition.operation_id)


def _raise_operation_conflict(operation_id: str) -> None:
    raise TurnOperationConflictError(
        "operation id was reused for a different transition",
        operation_id=operation_id,
    )


def _validate_transition_shape(transition: TransitionRecord) -> None:
    if not isinstance(transition, TransitionRecord):
        raise TurnValidationError("turn ledger entry is invalid")
    _require_operation_id(transition.operation_id)
    _require_phase(transition.from_phase, "from_phase")
    _require_phase(transition.to_phase, "to_phase")
    _require_nonnegative_int(transition.from_version, "from_version")
    _require_positive_int(transition.to_version, "to_version")
    _ensure_utc(transition.at, "transition.at")
    _validate_note(transition.note)
    _validate_optional_reason(transition.cancellation_reason)
    _validate_optional_terminal(transition.requested_terminal,
                                "requested_terminal")
    _validate_optional_terminal(transition.final_terminal, "final_terminal")


def _require_exact_keys(data: Mapping[str, Any], expected: set[str],
                        label: str) -> None:
    if not isinstance(data, Mapping):
        raise TurnValidationError(f"{label} must be a mapping")
    keys = tuple(data)
    known = {key for key in keys if isinstance(key, str)}
    unknown = tuple(key for key in keys
                    if not isinstance(key, str) or key not in expected)
    if known != expected or unknown:
        raise TurnValidationError(
            f"{label} fields are invalid",
            missing=sorted(expected - known),
            unknown_count=len(unknown),
            unknown_types=sorted({type(key).__name__ for key in unknown}),
        )


def _require_turn_id(value: Any) -> str:
    _validate_turn_id(value)
    return value


def _validate_turn_id(value: Any) -> None:
    if not isinstance(value, str) or not _TURN_ID.fullmatch(value):
        raise TurnValidationError("turn id is invalid")


def _require_operation_id(value: Any) -> str:
    if not isinstance(value, str) or not _OPERATION_ID.fullmatch(value):
        raise TurnValidationError("operation id is invalid")
    return value


def _validate_link_id(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not _SAFE_LINK_ID.fullmatch(value):
        raise TurnValidationError(f"{field_name} is invalid")


def _require_link_id(value: Any, field_name: str) -> str:
    _validate_link_id(value, field_name)
    return value


def _require_action_id(value: Any) -> str:
    if not isinstance(value, str) or not _ACTION_ID.fullmatch(value):
        raise TurnValidationError("action_ref is invalid")
    return value


def _require_run_id(value: Any) -> str:
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise TurnValidationError("run_id is invalid")
    return value


def _require_task_id(value: Any) -> str:
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise TurnValidationError("task_id is invalid")
    return value


def _require_subtask_id(value: Any) -> str:
    if not isinstance(value, str) or not _SUBTASK_ID.fullmatch(value):
        raise TurnValidationError("subtask_id is invalid")
    return value


def _require_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or \
            not 0 <= value <= MAX_COUNTER_VALUE:
        raise TurnValidationError(f"{field_name} must be a non-negative integer")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or \
            not 1 <= value <= MAX_COUNTER_VALUE:
        raise TurnValidationError(f"{field_name} must be a positive integer")
    return value


def _require_phase(value: Any, field_name: str) -> TurnPhase:
    if not isinstance(value, TurnPhase):
        raise TurnValidationError(f"{field_name} is invalid")
    return value


def _require_terminal(value: Any) -> TurnPhase:
    phase = _require_phase(value, "terminal outcome")
    if not phase.is_terminal:
        raise TurnValidationError("terminal outcome is not terminal",
                                  value=phase.value)
    return phase


def _parse_phase(value: Any, field_name: str) -> TurnPhase:
    try:
        return TurnPhase(value)
    except (TypeError, ValueError) as exc:
        raise TurnValidationError(f"{field_name} is invalid") from exc


def _parse_optional_phase(value: Any, field_name: str) -> TurnPhase | None:
    return None if value is None else _parse_phase(value, field_name)


def _parse_reason(value: Any) -> CancellationReason | None:
    if value is None:
        return None
    try:
        return CancellationReason(value)
    except (TypeError, ValueError) as exc:
        raise TurnValidationError("cancellation reason is invalid") from exc


def _validate_optional_reason(value: Any) -> None:
    if value is not None and not isinstance(value, CancellationReason):
        raise TurnValidationError("cancellation reason is invalid")


def _validate_optional_terminal(value: Any, field_name: str) -> None:
    if value is not None:
        try:
            _require_terminal(value)
        except TurnValidationError as exc:
            raise TurnValidationError(f"{field_name} is invalid") from exc


def _ensure_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or \
            value.utcoffset() != timedelta(0):
        raise TurnValidationError(f"{field_name} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TurnValidationError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TurnValidationError(f"{field_name} is invalid") from exc
    return _ensure_utc(parsed, field_name)


def _format_utc(value: datetime, field_name: str) -> str:
    return _ensure_utc(value, field_name).isoformat()


def _validate_duration(seconds: Any) -> float:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TurnValidationError("deadline duration must be numeric")
    try:
        duration = float(seconds)
    except (OverflowError, ValueError) as exc:
        raise TurnValidationError("deadline duration is invalid") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise TurnValidationError("deadline duration must be finite and positive")
    return duration


def _validate_monotonic(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TurnValidationError("monotonic clock value is invalid")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise TurnValidationError("monotonic clock value is invalid") from exc
    if not math.isfinite(result) or result < 0:
        raise TurnValidationError("monotonic clock value is invalid")
    return result


def _checked_monotonic_add(base: float, seconds: float) -> float:
    result = base + seconds
    if not math.isfinite(result):
        raise TurnValidationError("deadline duration exceeds safe range")
    return result


def _checked_utc_add(base: datetime, seconds: float) -> datetime:
    try:
        return base + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise TurnValidationError(
            "deadline duration exceeds safe range") from exc


def _validate_note(value: Any) -> str:
    if not isinstance(value, str):
        raise TurnValidationError("transition note must be a string")
    if len(value) > MAX_NOTE_CHARS or value not in _CATEGORICAL_NOTES:
        raise TurnValidationError("transition note is invalid",
                                  allowed=sorted(_CATEGORICAL_NOTES))
    return value


def _validate_action_refs(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple):
        raise TurnValidationError("action_refs must be a tuple")
    if len(values) > MAX_ACTION_REFERENCES:
        raise TurnValidationError("action reference limit exceeded",
                                  maximum=MAX_ACTION_REFERENCES)
    for value in values:
        _require_action_id(value)
    if len(set(values)) != len(values):
        raise TurnValidationError("action references must be unique")


def _raise_transition_mapping() -> TransitionRecord:
    raise TurnValidationError("each turn transition must be a mapping")
