from __future__ import annotations

import threading

import pytest

from agentcore.realtime import (
    CancellationClosedError,
    CancellationReason,
    CancellationRequested,
    CancellationSource,
    TurnValidationError,
)
from agentcore.realtime.errors import MAX_CANCELLATION_DETAIL_CHARS


def _join_all(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=5)
    assert not [thread for thread in threads if thread.is_alive()]


def test_first_reason_wins_and_later_reasons_are_secondary_only():
    source = CancellationSource()
    assert source.cancel(CancellationReason.USER_INTERRUPT, detail="first")
    assert not source.cancel(CancellationReason.DEADLINE_EXCEEDED,
                             detail="later")
    assert not source.cancel(CancellationReason.USER_INTERRUPT)
    assert source.token.requested
    assert source.token.reason is CancellationReason.USER_INTERRUPT
    assert source.token.detail == "first"
    assert source.secondary_reasons == (
        CancellationReason.DEADLINE_EXCEEDED,)


def test_raise_if_requested_carries_primary_reason_and_detail():
    source = CancellationSource()
    source.token.raise_if_requested()
    source.cancel(CancellationReason.AGENT_SHUTDOWN, detail="closing")
    with pytest.raises(CancellationRequested) as caught:
        source.token.raise_if_requested()
    assert caught.value.reason is CancellationReason.AGENT_SHUTDOWN
    assert caught.value.detail == "closing"


def test_cancellation_detail_is_ephemeral_and_not_error_metadata():
    source = CancellationSource()
    sensitive_detail = "operator-only cancellation context"
    source.cancel(CancellationReason.AGENT_SHUTDOWN,
                  detail=sensitive_detail)

    with pytest.raises(CancellationRequested) as caught:
        source.token.raise_if_requested()

    assert caught.value.detail == sensitive_detail
    serialized = caught.value.to_dict()
    assert sensitive_detail not in str(serialized)
    assert serialized["details"] == {
        "reason": "agent_shutdown",
        "has_detail": True,
        "detail_chars": len(sensitive_detail),
    }


def test_cancellation_detail_bound_is_enforced_before_state_changes():
    source = CancellationSource()
    detail = "x" * (MAX_CANCELLATION_DETAIL_CHARS + 1)

    with pytest.raises(TurnValidationError) as caught:
        source.cancel(CancellationReason.USER_INTERRUPT, detail=detail)

    assert not source.token.requested
    assert detail not in str(caught.value.to_dict())
    assert caught.value.details == {
        "length": len(detail),
        "maximum": MAX_CANCELLATION_DETAIL_CHARS,
    }


def test_invalid_reason_does_not_echo_untrusted_value():
    source = CancellationSource()
    sentinel = "untrusted-reason-sentinel"

    with pytest.raises(TurnValidationError) as caught:
        source.cancel(sentinel)  # type: ignore[arg-type]

    assert sentinel not in str(caught.value.to_dict())
    assert caught.value.details == {"value_type": "str"}


def test_direct_cancellation_exception_bounds_ephemeral_detail():
    detail = "x" * (MAX_CANCELLATION_DETAIL_CHARS + 1)

    error = CancellationRequested(CancellationReason.INTERNAL_FAILURE,
                                  detail=detail)

    assert error.detail == detail[:MAX_CANCELLATION_DETAIL_CHARS]
    assert detail not in str(error.to_dict())
    assert error.details == {
        "reason": "internal_failure",
        "has_detail": True,
        "detail_chars": MAX_CANCELLATION_DETAIL_CHARS,
    }


def test_callbacks_run_once_in_registration_order_outside_lock():
    source = CancellationSource()
    called: list[str] = []

    def first(reason):
        called.append(f"first:{reason.value}")
        # These calls would deadlock if callbacks ran under the state lock.
        assert source.token.reason is reason
        source.token.register(
            lambda nested: called.append(f"nested:{nested.value}"))

    source.token.register(first)
    source.token.register(lambda reason: called.append(f"second:{reason.value}"))
    source.cancel(CancellationReason.OPERATOR_CANCEL)
    source.cancel(CancellationReason.USER_INTERRUPT)

    assert called == [
        "first:operator_cancel",
        "nested:operator_cancel",
        "second:operator_cancel",
    ]


def test_callback_failure_is_suppressed_and_does_not_skip_later_callbacks():
    source = CancellationSource()
    called: list[str] = []

    def broken(_):
        called.append("broken")
        raise RuntimeError("listener failed")

    source.token.register(broken)
    source.token.register(lambda _: called.append("healthy"))

    assert source.cancel(CancellationReason.INTERNAL_FAILURE)
    assert called == ["broken", "healthy"]
    assert source.callback_errors == ("RuntimeError",)


def test_cancel_winner_claims_callbacks_before_close_can_suppress_them():
    source = CancellationSource()
    first_started = threading.Event()
    release_first = threading.Event()
    called: list[str] = []

    def first(_):
        called.append("first")
        first_started.set()
        assert release_first.wait(timeout=5)

    source.token.register(first)
    second_registration = source.token.register(
        lambda _: called.append("second"))
    won: list[bool] = []
    cancel_thread = threading.Thread(
        target=lambda: won.append(
            source.cancel(CancellationReason.USER_INTERRUPT)))
    cancel_thread.start()
    assert first_started.wait(timeout=5)

    # Cancellation already won and claimed both callbacks.  Neither closing
    # the source nor closing a registration may revoke that accepted signal.
    source.close()
    second_registration.close()
    release_first.set()
    _join_all([cancel_thread])

    assert won == [True]
    assert source.closed
    assert called == ["first", "second"]


def test_parent_cancel_winner_propagates_after_parent_is_closed():
    parent = CancellationSource()
    first_started = threading.Event()
    release_first = threading.Event()

    def blocking_listener(_):
        first_started.set()
        assert release_first.wait(timeout=5)

    parent.token.register(blocking_listener)
    child = parent.child()
    won: list[bool] = []
    cancel_thread = threading.Thread(
        target=lambda: won.append(
            parent.cancel(CancellationReason.AGENT_SHUTDOWN)))
    cancel_thread.start()
    assert first_started.wait(timeout=5)

    # The child propagation listener was in the batch claimed by cancellation.
    parent.close()
    release_first.set()
    _join_all([cancel_thread])

    assert won == [True]
    assert child.token.requested
    assert child.token.reason is CancellationReason.AGENT_SHUTDOWN


def test_callback_can_reentrantly_close_without_skipping_followers():
    source = CancellationSource()
    called: list[str] = []

    def first(_):
        called.append("first")
        source.close()

    source.token.register(first)
    source.token.register(lambda _: called.append("second"))

    assert source.cancel(CancellationReason.INTERNAL_FAILURE)
    assert source.closed
    assert called == ["first", "second"]


def test_registration_after_cancellation_invokes_once_synchronously():
    source = CancellationSource()
    source.cancel(CancellationReason.DEADLINE_EXCEEDED)
    called = []
    registration = source.token.register(called.append)
    assert called == [CancellationReason.DEADLINE_EXCEEDED]
    registration.close()
    registration.close()
    assert called == [CancellationReason.DEADLINE_EXCEEDED]


def test_registration_close_prevents_pending_callback_and_is_idempotent():
    source = CancellationSource()
    called = []
    registration = source.token.register(called.append)
    registration.close()
    registration.close()
    source.cancel(CancellationReason.USER_INTERRUPT)
    assert called == []


def test_parent_cancellation_propagates_to_existing_and_future_children():
    parent = CancellationSource()
    child = parent.child()
    sibling = parent.child()
    parent.cancel(CancellationReason.AGENT_SHUTDOWN)
    future = parent.child()

    for source in (child, sibling, future):
        assert source.token.requested
        assert source.token.reason is CancellationReason.AGENT_SHUTDOWN


def test_child_cancellation_does_not_affect_parent_or_sibling():
    parent = CancellationSource()
    child = parent.child()
    sibling = parent.child()
    child.cancel(CancellationReason.USER_INTERRUPT)
    assert not parent.token.requested
    assert not sibling.token.requested
    assert child.token.reason is CancellationReason.USER_INTERRUPT


def test_closing_child_detaches_parent_and_clears_local_listeners():
    parent = CancellationSource()
    child = parent.child()
    called = []
    child.token.register(called.append)
    child.close()
    child.close()
    parent.cancel(CancellationReason.AGENT_SHUTDOWN)
    assert not child.token.requested
    assert called == []
    with pytest.raises(CancellationClosedError):
        child.token.register(called.append)
    with pytest.raises(CancellationClosedError):
        child.child()


def test_simultaneous_cancel_has_exactly_one_winner():
    source = CancellationSource()
    reasons = list(CancellationReason)
    barrier = threading.Barrier(len(reasons))
    results: list[tuple[CancellationReason, bool]] = []
    result_lock = threading.Lock()

    def cancel(reason):
        barrier.wait(timeout=5)
        won = source.cancel(reason)
        with result_lock:
            results.append((reason, won))

    threads = [threading.Thread(target=cancel, args=(reason,))
               for reason in reasons]
    for thread in threads:
        thread.start()
    _join_all(threads)

    winners = [reason for reason, won in results if won]
    assert len(winners) == 1
    assert source.token.reason is winners[0]
    assert set(source.secondary_reasons) == set(reasons) - set(winners)


def test_register_racing_cancel_invokes_callback_exactly_once():
    source = CancellationSource()
    barrier = threading.Barrier(2)
    called: list[CancellationReason] = []
    registration = []

    def register():
        barrier.wait(timeout=5)
        registration.append(source.token.register(called.append))

    def cancel():
        barrier.wait(timeout=5)
        source.cancel(CancellationReason.SUPERSEDED)

    threads = [threading.Thread(target=register), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    _join_all(threads)
    assert called == [CancellationReason.SUPERSEDED]
    registration[0].close()


def test_close_racing_cancel_never_invokes_more_than_once():
    source = CancellationSource()
    barrier = threading.Barrier(2)
    called: list[CancellationReason] = []
    registration = source.token.register(called.append)

    def close():
        barrier.wait(timeout=5)
        registration.close()

    def cancel():
        barrier.wait(timeout=5)
        source.cancel(CancellationReason.USER_INTERRUPT)

    threads = [threading.Thread(target=close), threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    _join_all(threads)
    assert len(called) <= 1
    if called:
        assert called == [CancellationReason.USER_INTERRUPT]


def test_child_creation_racing_parent_cancel_returns_cancelled_child():
    parent = CancellationSource()
    barrier = threading.Barrier(2)
    children: list[CancellationSource] = []

    def create_child():
        barrier.wait(timeout=5)
        children.append(parent.child())

    def cancel():
        barrier.wait(timeout=5)
        parent.cancel(CancellationReason.AGENT_SHUTDOWN)

    threads = [threading.Thread(target=create_child),
               threading.Thread(target=cancel)]
    for thread in threads:
        thread.start()
    _join_all(threads)
    assert len(children) == 1
    assert children[0].token.requested
    assert children[0].token.reason is CancellationReason.AGENT_SHUTDOWN


def test_context_manager_closes_source_without_cancelling_it():
    with CancellationSource() as source:
        token = source.token
        assert not source.closed
    assert source.closed
    assert not token.requested
    assert not source.cancel(CancellationReason.AGENT_SHUTDOWN)
