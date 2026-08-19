"""Thread-safe cooperative cancellation primitives.

These objects only signal and fence logical work.  They do not create worker
threads, stop Python frames, or claim that an external side effect was undone.
Callbacks run synchronously in registration order and outside internal locks.
Callback exceptions are retained for diagnostics and suppressed so one bad
listener cannot skip later listeners or revert cancellation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from enum import Enum
from typing import Protocol, runtime_checkable

from agentcore.realtime.errors import (
    MAX_CANCELLATION_DETAIL_CHARS,
    CancellationClosedError,
    CancellationRequested,
    TurnValidationError,
)


class CancellationReason(str, Enum):
    """Trusted reasons that may terminate logical work."""

    USER_INTERRUPT = "user_interrupt"
    SUPERSEDED = "superseded"
    SUPERSEDED_BEFORE_START = "superseded_before_start"
    OPERATOR_CANCEL = "operator_cancel"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AGENT_SHUTDOWN = "agent_shutdown"
    INTERNAL_FAILURE = "internal_failure"

    def __str__(self) -> str:
        return self.value


CancellationCallback = Callable[[CancellationReason], None]


@runtime_checkable
class CancellationToken(Protocol):
    """Read-only cancellation view passed to logical operations."""

    @property
    def requested(self) -> bool: ...

    @property
    def reason(self) -> CancellationReason | None: ...

    @property
    def detail(self) -> str: ...

    def raise_if_requested(self) -> None: ...

    def register(self, callback: CancellationCallback
                 ) -> CancellationRegistration: ...


class _CallbackEntry:
    __slots__ = ("callback",)

    def __init__(self, callback: CancellationCallback) -> None:
        self.callback = callback


class _CancellationState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requested = False
        self.reason: CancellationReason | None = None
        self.detail = ""
        self.secondary_reasons: list[CancellationReason] = []
        self.callbacks: dict[int, _CallbackEntry] = {}
        self.next_callback_id = 1
        # Exception type names only: deterministic diagnostics without
        # retaining traceback frames, callback objects, or arbitrary messages.
        self.callback_errors: list[str] = []
        self.closed = False

    def unregister(self, callback_id: int | None) -> None:
        if callback_id is None:
            return
        with self.lock:
            self.callbacks.pop(callback_id, None)

    def invoke(self, entry: _CallbackEntry,
               reason: CancellationReason) -> None:
        """Invoke one already-claimed callback without holding ``lock``."""
        try:
            entry.callback(reason)
        except BaseException as exc:  # callback isolation is deliberate
            with self.lock:
                self.callback_errors.append(type(exc).__name__)

    def invoke_immediate(self, callback: CancellationCallback,
                         reason: CancellationReason) -> None:
        self.invoke(_CallbackEntry(callback), reason)


class CancellationRegistration:
    """Idempotent handle that unregisters a not-yet-claimed callback."""

    def __init__(self, state: _CancellationState,
                 callback_id: int | None) -> None:
        self._state = state
        self._callback_id = callback_id
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            callback_id = self._callback_id
            self._callback_id = None
        self._state.unregister(callback_id)

    def __enter__(self) -> CancellationRegistration:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _Token:
    def __init__(self, state: _CancellationState) -> None:
        self._state = state

    @property
    def requested(self) -> bool:
        with self._state.lock:
            return self._state.requested

    @property
    def reason(self) -> CancellationReason | None:
        with self._state.lock:
            return self._state.reason

    @property
    def detail(self) -> str:
        with self._state.lock:
            return self._state.detail

    def raise_if_requested(self) -> None:
        with self._state.lock:
            reason = self._state.reason
            detail = self._state.detail
        if reason is not None:
            raise CancellationRequested(reason, detail=detail)

    def register(self, callback: CancellationCallback
                 ) -> CancellationRegistration:
        if not callable(callback):
            raise TurnValidationError("cancellation callback must be callable")
        with self._state.lock:
            if self._state.closed:
                raise CancellationClosedError("cancellation source is closed")
            reason = self._state.reason
            if reason is None:
                callback_id = self._state.next_callback_id
                self._state.next_callback_id += 1
                self._state.callbacks[callback_id] = _CallbackEntry(callback)
                return CancellationRegistration(self._state, callback_id)

        # Registration-after-cancel is synchronous and exactly once.  This is
        # deliberately outside the state lock.
        self._state.invoke_immediate(callback, reason)
        return CancellationRegistration(self._state, None)


class CancellationSource:
    """Owner of a cancellation token and optional parent propagation link."""

    def __init__(self, *, _parent: CancellationToken | None = None) -> None:
        self._state = _CancellationState()
        self._token = _Token(self._state)
        self._parent_lock = threading.Lock()
        self._parent_registration: CancellationRegistration | None = None
        if _parent is not None:
            registration = _parent.register(self._cancel_from_parent)
            with self._parent_lock:
                self._parent_registration = registration

    @property
    def token(self) -> CancellationToken:
        return self._token

    @property
    def secondary_reasons(self) -> tuple[CancellationReason, ...]:
        with self._state.lock:
            return tuple(self._state.secondary_reasons)

    @property
    def callback_errors(self) -> tuple[str, ...]:
        with self._state.lock:
            return tuple(self._state.callback_errors)

    @property
    def closed(self) -> bool:
        with self._state.lock:
            return self._state.closed

    def _cancel_from_parent(self, reason: CancellationReason) -> None:
        self.cancel(reason, detail="parent cancellation")

    def cancel(self, reason: CancellationReason, *, detail: str = "") -> bool:
        """Request cancellation, returning true only for the first caller."""
        if not isinstance(reason, CancellationReason):
            raise TurnValidationError("cancellation reason is invalid",
                                      value_type=type(reason).__name__)
        if not isinstance(detail, str):
            raise TurnValidationError("cancellation detail must be a string")
        if len(detail) > MAX_CANCELLATION_DETAIL_CHARS:
            raise TurnValidationError(
                "cancellation detail exceeds the maximum length",
                length=len(detail), maximum=MAX_CANCELLATION_DETAIL_CHARS)

        with self._state.lock:
            if self._state.closed:
                return False
            if self._state.requested:
                primary = self._state.reason
                if reason is not primary and \
                        reason not in self._state.secondary_reasons:
                    self._state.secondary_reasons.append(reason)
                return False
            self._state.requested = True
            self._state.reason = reason
            self._state.detail = detail
            # Cancellation owns every listener present at this linearization
            # point.  Claiming them as one atomic batch means a concurrent
            # source/registration close cannot suppress a cancellation that
            # has already won the state-lock race.  Invocation remains outside
            # the lock and preserves registration order.
            callbacks = tuple(self._state.callbacks.values())
            self._state.callbacks.clear()

        for entry in callbacks:
            self._state.invoke(entry, reason)
        return True

    def child(self) -> CancellationSource:
        with self._state.lock:
            if self._state.closed:
                raise CancellationClosedError("cancellation source is closed")
        return CancellationSource(_parent=self.token)

    def close(self) -> None:
        """Release propagation and all callbacks; safe to call repeatedly."""
        with self._state.lock:
            if self._state.closed:
                return
            self._state.closed = True
            # If cancellation won first, it already claimed its listeners into
            # a local immutable batch.  If close won first, this clears them
            # before cancellation can claim them.  Either order is therefore
            # deterministic, and callbacks always run outside this lock.
            self._state.callbacks.clear()
        with self._parent_lock:
            registration = self._parent_registration
            self._parent_registration = None
        if registration is not None:
            registration.close()

    def __enter__(self) -> CancellationSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
