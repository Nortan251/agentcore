from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from agentcore.realtime import (
    LEGAL_TRANSITIONS,
    CancellationReason,
    CancellationSource,
    Deadline,
    InMemoryTurnStore,
    OperationContext,
    TransitionRecord,
    TurnController,
    TurnCounters,
    TurnNotFoundError,
    TurnOperationConflictError,
    TurnPhase,
    TurnRecord,
    TurnStoreError,
    TurnTransitionError,
    TurnValidationError,
    TurnVersionConflictError,
    new_operation_id,
    new_turn,
    next_generation,
)
from agentcore.util.clock import FixedClock


NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


class _IndependentClock:
    def __init__(self) -> None:
        self.wall = NOW
        self.mono = 100.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono


def _op(number: int) -> str:
    return f"op_{number:012x}"


def _ids(number: int):
    return lambda prefix: f"{prefix}_{number:012x}"


_PATHS = {
    TurnPhase.CREATED: (),
    TurnPhase.QUEUED: (TurnPhase.QUEUED,),
    TurnPhase.GENERATING: (TurnPhase.QUEUED, TurnPhase.GENERATING),
    TurnPhase.EXECUTING_TOOL: (
        TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL),
    TurnPhase.WAITING_APPROVAL: (
        TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.WAITING_APPROVAL),
    TurnPhase.VERIFYING: (
        TurnPhase.QUEUED, TurnPhase.GENERATING,
        TurnPhase.EXECUTING_TOOL, TurnPhase.VERIFYING),
    TurnPhase.RECOVERING: (
        TurnPhase.QUEUED, TurnPhase.GENERATING, TurnPhase.EXECUTING_TOOL,
        TurnPhase.VERIFYING, TurnPhase.RECOVERING),
}

_EXPECTED_TRANSITIONS = {
    TurnPhase.CREATED: frozenset({TurnPhase.QUEUED, TurnPhase.CANCELLING}),
    TurnPhase.QUEUED: frozenset({
        TurnPhase.GENERATING,
        TurnPhase.CANCELLING,
    }),
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
    TurnPhase.WAITING_APPROVAL: frozenset({
        TurnPhase.QUEUED,
        TurnPhase.CANCELLING,
    }),
    TurnPhase.VERIFYING: frozenset({
        TurnPhase.RECOVERING,
        TurnPhase.FINALIZING,
        TurnPhase.CANCELLING,
    }),
    TurnPhase.RECOVERING: frozenset({
        TurnPhase.EXECUTING_TOOL,
        TurnPhase.FINALIZING,
        TurnPhase.CANCELLING,
    }),
    TurnPhase.CANCELLING: frozenset({TurnPhase.FINALIZING}),
    TurnPhase.FINALIZING: frozenset({
        TurnPhase.COMPLETED,
        TurnPhase.FAILED,
        TurnPhase.CANCELLED,
        TurnPhase.TIMED_OUT,
    }),
    TurnPhase.COMPLETED: frozenset(),
    TurnPhase.FAILED: frozenset(),
    TurnPhase.CANCELLED: frozenset(),
    TurnPhase.TIMED_OUT: frozenset(),
}


def _reason_for(terminal: TurnPhase) -> CancellationReason | None:
    return {
        TurnPhase.COMPLETED: None,
        TurnPhase.FAILED: CancellationReason.INTERNAL_FAILURE,
        TurnPhase.CANCELLED: CancellationReason.USER_INTERRUPT,
        TurnPhase.TIMED_OUT: CancellationReason.DEADLINE_EXCEEDED,
    }[terminal]


def _record_at(phase: TurnPhase, *, final_target: TurnPhase = TurnPhase.COMPLETED,
               turn_number: int = 1):
    clock = FixedClock(NOW)
    store = InMemoryTurnStore()
    record = store.create(new_turn(
        "conv_000000000001", 1, clock=clock,
        id_factory=_ids(turn_number)))
    controller = TurnController(store, record.turn_id, clock=clock)
    operation = 1

    if phase is TurnPhase.CANCELLING:
        controller.transition(
            TurnPhase.CREATED, TurnPhase.CANCELLING,
            expected_version=0, operation_id=_op(operation),
            cancellation_reason=CancellationReason.INTERNAL_FAILURE,
            requested_terminal=TurnPhase.FAILED)
        return store, controller, clock, store.load(record.turn_id), operation + 1

    if phase is TurnPhase.FINALIZING or phase.is_terminal:
        if final_target is TurnPhase.COMPLETED:
            path = (TurnPhase.QUEUED, TurnPhase.GENERATING)
            current = TurnPhase.CREATED
            version = 0
            for target in path:
                controller.transition(current, target, expected_version=version,
                                      operation_id=_op(operation))
                current, version, operation = target, version + 1, operation + 1
            controller.transition(
                current, TurnPhase.FINALIZING, expected_version=version,
                operation_id=_op(operation),
                requested_terminal=final_target)
        else:
            controller.transition(
                TurnPhase.CREATED, TurnPhase.CANCELLING,
                expected_version=0, operation_id=_op(operation),
                cancellation_reason=_reason_for(final_target),
                requested_terminal=final_target)
            operation += 1
            controller.transition(
                TurnPhase.CANCELLING, TurnPhase.FINALIZING,
                expected_version=1, operation_id=_op(operation),
                cancellation_reason=_reason_for(final_target),
                requested_terminal=final_target)
        operation += 1
        if phase.is_terminal:
            current = store.load(record.turn_id)
            controller.transition(
                TurnPhase.FINALIZING, final_target,
                expected_version=current.version, operation_id=_op(operation))
            operation += 1
        return store, controller, clock, store.load(record.turn_id), operation

    current = TurnPhase.CREATED
    version = 0
    for target in _PATHS[phase]:
        controller.transition(current, target, expected_version=version,
                              operation_id=_op(operation))
        current, version, operation = target, version + 1, operation + 1
    return store, controller, clock, store.load(record.turn_id), operation


def _transition_kwargs(source: TurnPhase, target: TurnPhase,
                       record: TurnRecord) -> dict:
    if target is TurnPhase.CANCELLING:
        return {
            "cancellation_reason": CancellationReason.INTERNAL_FAILURE,
            "requested_terminal": TurnPhase.FAILED,
        }
    if target is TurnPhase.FINALIZING:
        if source is TurnPhase.CANCELLING:
            return {
                "cancellation_reason": record.cancellation_reason,
                "requested_terminal": record.requested_terminal,
            }
        return {"requested_terminal": TurnPhase.COMPLETED}
    return {}


LEGAL_CASES = tuple(
    (source, target)
    for source, targets in _EXPECTED_TRANSITIONS.items()
    for target in targets
)


@pytest.mark.parametrize(("source", "target"), LEGAL_CASES)
def test_every_approved_transition_is_accepted(source, target):
    final_target = target if source is TurnPhase.FINALIZING else TurnPhase.COMPLETED
    store, controller, _, record, operation = _record_at(
        source, final_target=final_target)
    result = controller.transition(
        source, target, expected_version=record.version,
        operation_id=_op(operation),
        **_transition_kwargs(source, target, record))
    assert result.phase is target
    assert result.version == record.version + 1
    assert store.load(record.turn_id).phase is target


ILLEGAL_CASES = tuple(
    (source, target)
    for source in TurnPhase
    for target in TurnPhase
    if target not in _EXPECTED_TRANSITIONS[source]
)


def test_transition_graph_matches_independent_contract_and_is_immutable():
    assert dict(LEGAL_TRANSITIONS) == _EXPECTED_TRANSITIONS
    with pytest.raises(TypeError):
        LEGAL_TRANSITIONS[TurnPhase.CREATED] = frozenset()  # type: ignore[index]


@pytest.mark.parametrize(("source", "target"), ILLEGAL_CASES)
def test_every_unapproved_transition_is_rejected(source, target):
    store, controller, _, record, operation = _record_at(
        source, final_target=source if source.is_terminal else TurnPhase.COMPLETED)
    with pytest.raises(TurnTransitionError):
        controller.transition(
            source, target, expected_version=record.version,
            operation_id=_op(operation),
            **_transition_kwargs(source, target, record))
    assert store.load(record.turn_id) == record


def test_duplicate_operation_replays_exact_accepted_snapshot_after_later_work():
    store, controller, _, record, operation = _record_at(TurnPhase.CREATED)
    queued = controller.transition(
        TurnPhase.CREATED, TurnPhase.QUEUED,
        expected_version=record.version, operation_id=_op(operation))
    controller.transition(
        TurnPhase.QUEUED, TurnPhase.GENERATING,
        expected_version=queued.version, operation_id=_op(operation + 1))

    replay = controller.transition(
        TurnPhase.CREATED, TurnPhase.QUEUED,
        expected_version=record.version, operation_id=_op(operation))

    assert replay == queued
    assert store.load(record.turn_id).phase is TurnPhase.GENERATING
    assert len(store.load(record.turn_id).transitions) == 2


def test_reused_operation_id_with_different_intent_is_rejected():
    _, controller, _, record, operation = _record_at(TurnPhase.CREATED)
    controller.transition(
        TurnPhase.CREATED, TurnPhase.QUEUED,
        expected_version=record.version, operation_id=_op(operation))
    with pytest.raises(TurnOperationConflictError):
        controller.transition(
            TurnPhase.CREATED, TurnPhase.CANCELLING,
            expected_version=record.version, operation_id=_op(operation),
            cancellation_reason=CancellationReason.INTERNAL_FAILURE,
            requested_terminal=TurnPhase.FAILED)


def test_replay_cannot_fill_outcome_arguments_from_the_accepted_transition():
    _, controller, _, generating, operation = _record_at(TurnPhase.GENERATING)
    controller.transition(
        TurnPhase.GENERATING,
        TurnPhase.FINALIZING,
        expected_version=generating.version,
        operation_id=_op(operation),
        requested_terminal=TurnPhase.COMPLETED,
    )

    with pytest.raises(TurnOperationConflictError):
        controller.transition(
            TurnPhase.GENERATING,
            TurnPhase.FINALIZING,
            expected_version=generating.version,
            operation_id=_op(operation),
        )


def test_stale_version_and_phase_cannot_overwrite_newer_state():
    store, controller, _, record, operation = _record_at(TurnPhase.CREATED)
    controller.transition(
        TurnPhase.CREATED, TurnPhase.QUEUED,
        expected_version=0, operation_id=_op(operation))
    with pytest.raises(TurnVersionConflictError):
        controller.transition(
            TurnPhase.CREATED, TurnPhase.CANCELLING,
            expected_version=0, operation_id=_op(operation + 1),
            cancellation_reason=CancellationReason.INTERNAL_FAILURE,
            requested_terminal=TurnPhase.FAILED)
    assert store.load(record.turn_id).phase is TurnPhase.QUEUED


def test_terminal_transition_only_replays_same_terminal_operation():
    store, controller, _, finalizing, operation = _record_at(TurnPhase.FINALIZING)
    terminal = controller.transition(
        TurnPhase.FINALIZING, TurnPhase.COMPLETED,
        expected_version=finalizing.version, operation_id=_op(operation))
    assert controller.transition(
        TurnPhase.FINALIZING, TurnPhase.COMPLETED,
        expected_version=finalizing.version,
        operation_id=_op(operation)) == terminal
    with pytest.raises(TurnTransitionError):
        controller.transition(
            TurnPhase.COMPLETED, TurnPhase.COMPLETED,
            expected_version=terminal.version,
            operation_id=_op(operation + 1))
    with pytest.raises(TurnTransitionError):
        controller.transition(
            TurnPhase.COMPLETED, TurnPhase.FAILED,
            expected_version=terminal.version,
            operation_id=_op(operation + 2))
    assert store.load(finalizing.turn_id) == TurnRecord.from_dict(
        store.load(finalizing.turn_id).to_dict())


def test_terminal_selection_is_fixed_when_finalizing_begins():
    store, controller, _, record, operation = _record_at(TurnPhase.GENERATING)
    finalizing = controller.transition(
        TurnPhase.GENERATING, TurnPhase.FINALIZING,
        expected_version=record.version, operation_id=_op(operation),
        requested_terminal=TurnPhase.COMPLETED)
    with pytest.raises(TurnTransitionError):
        controller.transition(
            TurnPhase.FINALIZING, TurnPhase.FAILED,
            expected_version=finalizing.version,
            operation_id=_op(operation + 1))
    assert store.load(record.turn_id).requested_terminal is TurnPhase.COMPLETED


@pytest.mark.parametrize(("terminal", "reason"), [
    (TurnPhase.CANCELLED, CancellationReason.USER_INTERRUPT),
    (TurnPhase.TIMED_OUT, CancellationReason.DEADLINE_EXCEEDED),
])
def test_cancelled_and_timed_out_outcomes_must_pass_through_cancelling(
    terminal, reason,
):
    source = TurnPhase.VERIFYING
    store, controller, _, record, operation = _record_at(source)
    with pytest.raises(TurnTransitionError):
        controller.transition(
            source,
            TurnPhase.FINALIZING,
            expected_version=record.version,
            operation_id=_op(operation),
            requested_terminal=terminal,
            cancellation_reason=reason,
        )
    assert store.load(record.turn_id) == record


@pytest.mark.parametrize("source", [
    TurnPhase.GENERATING,
    TurnPhase.EXECUTING_TOOL,
    TurnPhase.VERIFYING,
    TurnPhase.RECOVERING,
])
def test_internal_fault_can_finalize_directly_as_failed(source):
    store, controller, _, record, operation = _record_at(source)
    finalizing = controller.transition(
        source,
        TurnPhase.FINALIZING,
        expected_version=record.version,
        operation_id=_op(operation),
        requested_terminal=TurnPhase.FAILED,
        cancellation_reason=CancellationReason.INTERNAL_FAILURE,
    )
    failed = controller.transition(
        TurnPhase.FINALIZING,
        TurnPhase.FAILED,
        expected_version=finalizing.version,
        operation_id=_op(operation + 1),
    )
    assert failed.phase is TurnPhase.FAILED
    assert failed.cancellation_reason is CancellationReason.INTERNAL_FAILURE
    assert TurnRecord.from_dict(store.load(record.turn_id).to_dict()).phase \
        is TurnPhase.FAILED


def test_restart_failure_can_override_only_a_pending_completed_outcome():
    store, controller, _, finalizing, operation = _record_at(
        TurnPhase.FINALIZING)
    failed = controller.transition(
        TurnPhase.FINALIZING,
        TurnPhase.FAILED,
        expected_version=finalizing.version,
        operation_id=_op(operation),
        note="restart reconciliation",
        cancellation_reason=CancellationReason.INTERNAL_FAILURE,
    )

    assert failed.requested_terminal is TurnPhase.COMPLETED
    assert failed.final_terminal is TurnPhase.FAILED
    assert failed.cancellation_reason is CancellationReason.INTERNAL_FAILURE
    assert controller.transition(
        TurnPhase.FINALIZING,
        TurnPhase.FAILED,
        expected_version=finalizing.version,
        operation_id=_op(operation),
        note="restart reconciliation",
        cancellation_reason=CancellationReason.INTERNAL_FAILURE,
    ) == failed
    with pytest.raises(TurnOperationConflictError):
        controller.transition(
            TurnPhase.FINALIZING,
            TurnPhase.FAILED,
            expected_version=finalizing.version,
            operation_id=_op(operation),
            note="restart reconciliation",
        )
    assert TurnRecord.from_dict(store.load(finalizing.turn_id).to_dict()).phase \
        is TurnPhase.FAILED


def test_restart_failure_override_is_narrow_and_requires_internal_evidence():
    for number, (target, reason) in enumerate((
        (TurnPhase.FAILED, None),
        (TurnPhase.CANCELLED, CancellationReason.USER_INTERRUPT),
        (TurnPhase.TIMED_OUT, CancellationReason.DEADLINE_EXCEEDED),
    ), start=20):
        store, controller, _, finalizing, operation = _record_at(
            TurnPhase.FINALIZING, turn_number=number)
        with pytest.raises(TurnTransitionError):
            controller.transition(
                TurnPhase.FINALIZING,
                target,
                expected_version=finalizing.version,
                operation_id=_op(operation),
                cancellation_reason=reason,
            )
        assert store.load(finalizing.turn_id) == finalizing


def test_transition_reserve_cannot_strand_cancellation_and_finalization():
    store, controller, _, record, operation = _record_at(TurnPhase.GENERATING)
    phase = record.phase
    version = record.version
    while version < 253:
        target = (TurnPhase.EXECUTING_TOOL
                  if phase is TurnPhase.GENERATING else TurnPhase.GENERATING)
        snapshot = controller.transition(
            phase,
            target,
            expected_version=version,
            operation_id=_op(operation),
        )
        phase, version, operation = target, snapshot.version, operation + 1

    ordinary_target = (TurnPhase.EXECUTING_TOOL
                       if phase is TurnPhase.GENERATING
                       else TurnPhase.GENERATING)
    with pytest.raises(TurnTransitionError):
        controller.transition(
            phase,
            ordinary_target,
            expected_version=version,
            operation_id=_op(operation),
        )

    cancelling = controller.transition(
        phase,
        TurnPhase.CANCELLING,
        expected_version=version,
        operation_id=_op(operation + 1),
        cancellation_reason=CancellationReason.INTERNAL_FAILURE,
        requested_terminal=TurnPhase.FAILED,
    )
    finalizing = controller.transition(
        TurnPhase.CANCELLING,
        TurnPhase.FINALIZING,
        expected_version=cancelling.version,
        operation_id=_op(operation + 2),
        cancellation_reason=CancellationReason.INTERNAL_FAILURE,
        requested_terminal=TurnPhase.FAILED,
    )
    terminal = controller.transition(
        TurnPhase.FINALIZING,
        TurnPhase.FAILED,
        expected_version=finalizing.version,
        operation_id=_op(operation + 3),
    )
    assert terminal.version == 256
    assert terminal.phase is TurnPhase.FAILED
    accepted = store.load(record.turn_id)
    assert len(accepted.transitions) == 256

    forged_ledger = list(accepted.transitions)
    forged_ledger[253] = replace(
        forged_ledger[253],
        to_phase=ordinary_target,
        cancellation_reason=None,
        requested_terminal=None,
    )
    forged_ledger[254] = replace(
        forged_ledger[254],
        from_phase=ordinary_target,
        to_phase=TurnPhase.FINALIZING,
        cancellation_reason=None,
        requested_terminal=TurnPhase.COMPLETED,
    )
    forged_ledger[255] = replace(
        forged_ledger[255],
        to_phase=TurnPhase.COMPLETED,
        cancellation_reason=None,
        requested_terminal=TurnPhase.COMPLETED,
        final_terminal=TurnPhase.COMPLETED,
    )
    forged = replace(
        accepted,
        phase=TurnPhase.COMPLETED,
        transitions=tuple(forged_ledger),
        cancellation_reason=None,
        requested_terminal=TurnPhase.COMPLETED,
        final_terminal=TurnPhase.COMPLETED,
        active_operation_id=forged_ledger[-1].operation_id,
    )
    with pytest.raises(TurnValidationError):
        forged.validate()


@pytest.mark.parametrize(
    ("reason", "terminal"),
    [
        (CancellationReason.USER_INTERRUPT, TurnPhase.CANCELLED),
        (CancellationReason.OPERATOR_CANCEL, TurnPhase.CANCELLED),
        (CancellationReason.SUPERSEDED, TurnPhase.CANCELLED),
        (CancellationReason.SUPERSEDED_BEFORE_START, TurnPhase.CANCELLED),
        (CancellationReason.AGENT_SHUTDOWN, TurnPhase.CANCELLED),
        (CancellationReason.DEADLINE_EXCEEDED, TurnPhase.TIMED_OUT),
        (CancellationReason.INTERNAL_FAILURE, TurnPhase.FAILED),
    ],
)
def test_cancellation_reason_deterministically_selects_terminal(reason, terminal):
    store, controller, _, record, operation = _record_at(TurnPhase.CREATED)
    cancelling = controller.transition(
        TurnPhase.CREATED, TurnPhase.CANCELLING,
        expected_version=record.version, operation_id=_op(operation),
        cancellation_reason=reason, requested_terminal=terminal)
    assert cancelling.requested_terminal is terminal
    assert cancelling.cancellation_reason is reason
    finalizing = controller.transition(
        TurnPhase.CANCELLING, TurnPhase.FINALIZING,
        expected_version=cancelling.version,
        operation_id=_op(operation + 1),
        cancellation_reason=reason, requested_terminal=terminal)
    result = controller.transition(
        TurnPhase.FINALIZING, terminal,
        expected_version=finalizing.version,
        operation_id=_op(operation + 2))
    assert result.phase is terminal
    assert result.final_terminal is terminal
    assert store.load(record.turn_id).phase is terminal


def test_record_and_snapshot_are_deeply_immutable():
    record = new_turn(
        "conv_000000000001", 1, clock=FixedClock(NOW),
        id_factory=_ids(1), action_refs=("act_000000000001",))
    with pytest.raises(FrozenInstanceError):
        record.phase = TurnPhase.QUEUED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        record.counters.tool_calls = 1  # type: ignore[misc]
    assert isinstance(record.transitions, tuple)
    assert isinstance(record.action_refs, tuple)
    assert record.snapshot().action_refs == ("act_000000000001",)


def test_schema_v1_round_trip_is_exact_and_utc_aware():
    deadline = Deadline.after(30, clock=FixedClock(NOW))
    record = new_turn(
        "conv_000000000001", 2, run_id="run_000000000001",
        deadline=deadline, clock=FixedClock(NOW), id_factory=_ids(3),
        action_refs=("act_000000000001",))
    restored = TurnRecord.from_dict(record.to_dict())
    assert restored == record
    assert restored.created_at.utcoffset() == timedelta(0)
    assert restored.deadline_at is not None
    assert restored.deadline_at.utcoffset() == timedelta(0)
    serialized = restored.to_dict()
    assert "monotonic_expires_at" not in str(serialized)
    assert not any(key in serialized for key in (
        "content", "model_delta", "tool_content", "provider", "listeners"))


@pytest.mark.parametrize("mutation", [
    lambda payload: payload.update(schema_version=2),
    lambda payload: payload.pop("turn_id"),
    lambda payload: payload.update(extra="future"),
    lambda payload: payload.update(generation=True),
    lambda payload: payload.update(created_at="2026-01-01T00:00:00"),
    lambda payload: payload.update(transitions={}),
    lambda payload: payload.update(action_refs={}),
])
def test_malformed_or_future_turn_schema_is_rejected(mutation):
    payload = new_turn(
        "conv_000000000001", 1, clock=FixedClock(NOW),
        id_factory=_ids(1)).to_dict()
    mutation(payload)
    with pytest.raises(TurnValidationError):
        TurnRecord.from_dict(payload)


def test_schema_rejects_mixed_keys_without_echoing_unknown_content():
    payload = new_turn(
        "conv_000000000001", 1, clock=FixedClock(NOW),
        id_factory=_ids(1)).to_dict()
    sentinel = "DO_NOT_PERSIST_UNKNOWN_FIELD"
    payload[sentinel] = "secret"
    payload[7] = "also secret"  # type: ignore[index]

    with pytest.raises(TurnValidationError) as captured:
        TurnRecord.from_dict(payload)
    assert captured.value.details["unknown_count"] == 2
    assert captured.value.details["unknown_types"] == ["int", "str"]
    assert sentinel not in str(captured.value.details)


@pytest.mark.parametrize("mutation", [
    lambda record: replace(record, cancellation_reason="secret"),
    lambda record: replace(record, requested_terminal=TurnPhase.GENERATING),
    lambda record: replace(record, final_terminal="failed"),
    lambda record: replace(record, action_refs=(".env",)),
    lambda record: replace(record, action_refs=([],)),
    lambda record: replace(record, counters=[]),
    lambda record: replace(record, generation=2**63),
    lambda record: replace(record, run_id="run_not_trusted"),
])
def test_direct_record_construction_is_strict_and_categorical(mutation):
    record = new_turn(
        "conv_000000000001", 1, clock=FixedClock(NOW),
        id_factory=_ids(1))
    with pytest.raises(TurnValidationError):
        mutation(record).validate()


@pytest.mark.parametrize(("field", "value"), [
    ("from_phase", "created"),
    ("to_phase", object()),
    ("cancellation_reason", "internal_failure"),
    ("requested_terminal", "failed"),
    ("final_terminal", TurnPhase.GENERATING),
    ("from_version", 2**63),
])
def test_direct_transition_construction_validates_every_typed_field(field, value):
    transition = TransitionRecord(
        operation_id=_op(1),
        from_phase=TurnPhase.CREATED,
        to_phase=TurnPhase.QUEUED,
        from_version=0,
        to_version=1,
        at=NOW,
    )
    with pytest.raises(TurnValidationError):
        replace(transition, **{field: value}).to_dict()


def test_direct_transition_parser_rejects_invalid_shape_immediately():
    payload = TransitionRecord(
        operation_id=_op(1),
        from_phase=TurnPhase.CREATED,
        to_phase=TurnPhase.QUEUED,
        from_version=0,
        to_version=1,
        at=NOW,
    ).to_dict()
    payload["to_version"] = 0
    with pytest.raises(TurnValidationError):
        TransitionRecord.from_dict(payload)


def test_direct_counter_serialization_validates_before_emitting():
    with pytest.raises(TurnValidationError):
        TurnCounters(model_requests=2**63).to_dict()


def test_new_turn_rejects_untrusted_deadline_and_action_container():
    with pytest.raises(TurnValidationError):
        new_turn(
            "conv_000000000001", 1, deadline=object(),
            clock=FixedClock(NOW), id_factory=_ids(1))  # type: ignore[arg-type]
    with pytest.raises(TurnValidationError):
        new_turn(
            "conv_000000000001", 1,
            action_refs=["act_000000000001"],  # type: ignore[arg-type]
            clock=FixedClock(NOW), id_factory=_ids(1))


def test_turn_correlation_ids_are_strict_scalar_references():
    record = new_turn(
        "conv_000000000001",
        1,
        clock=FixedClock(NOW),
        id_factory=_ids(1),
        run_id="run_000000000001",
        task_id="task_000000000001",
        subtask_id="sub_000000000001",
        action_refs=("act_000000000001",),
    )
    payload = record.to_dict()
    assert payload["run_id"] == "run_000000000001"
    assert payload["task_id"] == "task_000000000001"
    assert payload["subtask_id"] == "sub_000000000001"
    assert payload["action_refs"] == ["act_000000000001"]
    assert TurnRecord.from_dict(payload) == record

    with pytest.raises(TurnValidationError):
        new_turn(
            "conv_000000000001", 1,
            clock=FixedClock(NOW), id_factory=_ids(2),
            subtask_id="sub_000000000001")
    with pytest.raises(TurnValidationError):
        new_turn(
            "conv_000000000001", 1,
            clock=FixedClock(NOW), id_factory=_ids(3),
            task_id="task-not-valid")


def test_transition_notes_are_trusted_categories_not_arbitrary_content():
    store, controller, _, record, operation = _record_at(TurnPhase.CREATED)
    sentinel = "model supplied .env instruction"
    with pytest.raises(TurnValidationError) as captured:
        controller.transition(
            TurnPhase.CREATED,
            TurnPhase.QUEUED,
            expected_version=record.version,
            operation_id=_op(operation),
            note=sentinel,
        )
    assert sentinel not in str(captured.value.details)
    assert store.load(record.turn_id) == record


@pytest.mark.parametrize(("session_id", "generation"), [
    ("", 1), ("../conv", 1), ("conv_1", 0), ("conv_1", True),
])
def test_turn_identity_validation_is_strict(session_id, generation):
    with pytest.raises(TurnValidationError):
        new_turn(session_id, generation, clock=FixedClock(NOW),
                 id_factory=_ids(1))
    with pytest.raises(TurnValidationError):
        new_turn("conv_1", 1, clock=FixedClock(NOW),
                 id_factory=lambda _: "turn_not-valid")


def test_generation_and_operation_id_primitives_are_validated():
    assert next_generation(None) == 1
    assert next_generation(7) == 8
    assert new_operation_id(id_factory=_ids(15)) == "op_00000000000f"
    with pytest.raises(TurnValidationError):
        next_generation(0)
    with pytest.raises(TurnValidationError):
        next_generation(2**63 - 1)
    with pytest.raises(TurnValidationError):
        next_generation(2**63)
    with pytest.raises(TurnValidationError):
        new_operation_id(id_factory=lambda _: "op_bad")


def test_deadline_uses_monotonic_time_and_child_never_extends_parent():
    clock = FixedClock(NOW)
    parent = Deadline.after(10, clock=clock)
    child = parent.child(30, clock=clock)
    shorter = parent.child(3, clock=clock)
    assert child.monotonic_expires_at == parent.monotonic_expires_at
    assert child.expires_at_utc <= parent.expires_at_utc
    assert shorter.remaining(clock) == 3
    clock.advance(3)
    assert shorter.expired(clock)
    assert not parent.expired(clock)
    clock.advance(7)
    assert parent.expired(clock)
    assert parent.remaining(clock) == 0


@pytest.mark.parametrize(
    "seconds", [0, -1, float("inf"), float("nan"), 1e308, 10**1000, True])
def test_invalid_deadline_durations_are_rejected(seconds):
    with pytest.raises(TurnValidationError):
        Deadline.after(seconds, clock=FixedClock(NOW))


def test_direct_deadline_rejects_invalid_monotonic_values_categorically():
    for value in (-1, float("nan"), 10**1000, True):
        with pytest.raises(TurnValidationError):
            Deadline(value, NOW)


def test_deadline_rejects_wall_projection_overflow_categorically():
    near_datetime_limit = datetime.max.replace(tzinfo=timezone.utc)
    with pytest.raises(TurnValidationError):
        Deadline.after(1, clock=FixedClock(near_datetime_limit))


def test_wall_clock_jumps_do_not_change_elapsed_deadline_decisions():
    clock = _IndependentClock()
    deadline = Deadline.after(10, clock=clock)
    clock.wall = NOW + timedelta(days=365)
    assert deadline.remaining(clock) == 10
    assert not deadline.expired(clock)
    clock.wall = NOW - timedelta(days=365)
    clock.mono += 10
    assert deadline.expired(clock)


def test_accepts_fences_generation_operation_phase_cancel_and_expiry():
    clock = FixedClock(NOW)
    deadline = Deadline.after(20, clock=clock)
    store = InMemoryTurnStore()
    record = store.create(new_turn(
        "conv_000000000001", 4, deadline=deadline, clock=clock,
        id_factory=_ids(1)))
    controller = TurnController(store, record.turn_id, clock=clock)
    queued = controller.transition(
        TurnPhase.CREATED, TurnPhase.QUEUED, expected_version=0,
        operation_id=_op(1))
    source = CancellationSource()

    def context(*, session_id=record.session_id, generation=4,
                operation_id=_op(1),
                operation_deadline=deadline, token=source.token):
        return OperationContext(record.turn_id, session_id, generation,
                                operation_id, token, operation_deadline)

    assert controller.accepts(context(), expected_phase=TurnPhase.QUEUED)
    assert not controller.accepts(context(session_id="conv_000000000002"),
                                  expected_phase=TurnPhase.QUEUED)
    assert not controller.accepts(context(generation=3),
                                  expected_phase=TurnPhase.QUEUED)
    assert not controller.accepts(context(operation_id=_op(2)),
                                  expected_phase=TurnPhase.QUEUED)
    assert not controller.accepts(context(), expected_phase=TurnPhase.GENERATING)
    extended = Deadline.after(30, clock=clock)
    assert not controller.accepts(context(operation_deadline=extended),
                                  expected_phase=TurnPhase.QUEUED)
    invalid_clock = _IndependentClock()
    invalid_clock.mono = float("nan")
    assert not TurnController(
        store, record.turn_id, clock=invalid_clock).accepts(
            context(), expected_phase=TurnPhase.QUEUED)
    source.cancel(CancellationReason.USER_INTERRUPT)
    assert not controller.accepts(context(), expected_phase=TurnPhase.QUEUED)

    fresh = CancellationSource()
    clock.advance(20)
    assert not controller.accepts(
        context(token=fresh.token), expected_phase=TurnPhase.QUEUED)
    assert store.load(record.turn_id).version == queued.version


def test_accepts_rejects_terminal_records():
    _, controller, clock, terminal, _ = _record_at(TurnPhase.COMPLETED)
    source = CancellationSource()
    context = OperationContext(
        terminal.turn_id,
        terminal.session_id,
        terminal.generation,
        terminal.active_operation_id,
        source.token,
        Deadline.after(10, clock=clock),
    )
    assert not controller.accepts(context, expected_phase=TurnPhase.COMPLETED)


@pytest.mark.parametrize("failure", [
    TurnNotFoundError("turn record does not exist"),
    TurnStoreError("turn store is unavailable"),
    TurnValidationError("saved turn record is corrupt"),
])
def test_accepts_fails_closed_on_categorical_store_errors_without_cas(failure):
    _, _, clock, queued, _ = _record_at(TurnPhase.QUEUED)
    source = CancellationSource()
    context = OperationContext(
        queued.turn_id,
        queued.session_id,
        queued.generation,
        queued.active_operation_id,
        source.token,
        Deadline.after(10, clock=clock),
    )

    class MissingStore:
        cas_calls = 0

        def load(self, turn_id):
            raise failure

        def compare_and_swap(self, turn_id, expected_version, next_record):
            self.cas_calls += 1
            raise AssertionError("CAS must not run")

    store = MissingStore()
    missing = TurnController(store, queued.turn_id, clock=clock)
    assert not missing.accepts(context, expected_phase=TurnPhase.QUEUED)
    assert store.cas_calls == 0


def test_transition_audit_time_never_regresses_after_clock_rollback():
    store, controller, _, queued, operation = _record_at(TurnPhase.QUEUED)
    rolled_back = TurnController(
        store,
        queued.turn_id,
        clock=FixedClock(NOW - timedelta(days=1)),
    )
    generating = rolled_back.transition(
        TurnPhase.QUEUED,
        TurnPhase.GENERATING,
        expected_version=queued.version,
        operation_id=_op(operation),
    )
    assert generating.updated_at == queued.updated_at
    assert TurnRecord.from_dict(store.load(queued.turn_id).to_dict()).phase \
        is TurnPhase.GENERATING


def test_deadline_reconstruction_uses_wall_time_once_then_monotonic_decisions():
    clock = FixedClock(NOW)
    deadline = Deadline.from_utc(NOW + timedelta(seconds=5), clock=clock)
    clock.advance(5)
    assert deadline.expired(clock)
    assert deadline.expires_at_utc == NOW + timedelta(seconds=5)


def test_dataclass_replace_cannot_forge_inconsistent_record():
    record = new_turn(
        "conv_000000000001", 1, clock=FixedClock(NOW),
        id_factory=_ids(1))
    forged = replace(record, phase=TurnPhase.COMPLETED)
    with pytest.raises(TurnValidationError):
        forged.validate()
