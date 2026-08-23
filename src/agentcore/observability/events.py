"""Canonical event names.

Emitting free-form strings makes logs unqueryable six months later, so all
framework events are named here and reused by the components that emit them.
"""

from __future__ import annotations

from typing import Final


class Events:
    # lifecycle
    AGENT_STARTED: Final = "agent.started"
    AGENT_STOPPED: Final = "agent.stopped"

    # tasks
    TASK_CREATED: Final = "task.created"
    TASK_STARTED: Final = "task.started"
    TASK_COMPLETED: Final = "task.completed"
    TASK_FAILED: Final = "task.failed"
    TASK_BLOCKED: Final = "task.blocked"
    TASK_CANCELLED: Final = "task.cancelled"
    SUBTASK_STARTED: Final = "subtask.started"
    SUBTASK_FINISHED: Final = "subtask.finished"

    # tools
    TOOL_REGISTERED: Final = "tool.registered"
    TOOL_INVOKED: Final = "tool.invoked"
    TOOL_SUCCEEDED: Final = "tool.succeeded"
    TOOL_FAILED: Final = "tool.failed"
    TOOL_DENIED: Final = "tool.denied"

    # state
    STATE_TRANSITION: Final = "state.transition"
    STATE_CHECKPOINT: Final = "state.checkpoint"
    STATE_RESUMED: Final = "state.resumed"

    # verification
    VERIFICATION_RECORDED: Final = "verification.recorded"
    VERIFICATION_STARTED: Final = "verification.started"
    VERIFICATION_DETERMINISTIC: Final = "verification.deterministic"
    VERIFICATION_MODEL_ASSISTED: Final = "verification.model_assisted"
    VERIFICATION_COMPLETED: Final = "verification.completed"
    VERIFICATION_FAILED: Final = "verification.failed"
    VERIFIER_CONFIGURED: Final = "verifier.configured"

    # recovery
    RECOVERY_ATTEMPTED: Final = "recovery.attempted"
    RECOVERY_STARTED: Final = "recovery.started"
    RECOVERY_CLASSIFIED: Final = "recovery.classified"
    RECOVERY_DECIDED: Final = "recovery.decided"
    RECOVERY_RETRY_SCHEDULED: Final = "recovery.retry_scheduled"
    RECOVERY_REPLAN_REQUESTED: Final = "recovery.replan_requested"
    RECOVERY_ESCALATED: Final = "recovery.escalated"
    RECOVERY_STOPPED: Final = "recovery.stopped"
    RECOVERY_CONFIGURED: Final = "recovery.configured"

    # permissions
    PERMISSION_CHECKED: Final = "permission.checked"
    APPROVAL_REQUESTED: Final = "approval.requested"
    APPROVAL_GRANTED: Final = "approval.granted"
    APPROVAL_REJECTED: Final = "approval.rejected"

    # planning
    PLAN_REQUESTED: Final = "plan.requested"
    PLAN_PARSED: Final = "plan.parsed"
    PLAN_CREATED: Final = "plan.created"
    PLAN_REVISED: Final = "plan.revised"
    PLAN_FAILED: Final = "plan.failed"
    # replan orchestration
    REPLAN_STARTED: Final = "replan.started"
    REPLAN_SUCCEEDED: Final = "replan.succeeded"
    REPLAN_REJECTED: Final = "replan.rejected"
    REPLAN_DUPLICATE_REJECTED: Final = "replan.duplicate_rejected"
    REPLAN_FAILED: Final = "replan.failed"
    REPLAN_EXHAUSTED: Final = "replan.exhausted"
    REPLAN_PLAN_ACCEPTED: Final = "replan.plan_accepted"
    REPLAN_EXECUTION_RESUMED: Final = "replan.execution_resumed"
    PLANNER_CONFIGURED: Final = "planner.configured"
    PLANNER_UNAVAILABLE: Final = "planner.unavailable"

    # memory
    MEMORY_CREATED: Final = "memory.created"
    MEMORY_UPDATED: Final = "memory.updated"
    MEMORY_DELETED: Final = "memory.deleted"
    MEMORY_SEARCHED: Final = "memory.searched"
    MEMORY_MISSED: Final = "memory.missed"
    MEMORY_EXPIRED: Final = "memory.expired"
    MEMORY_UNREADABLE: Final = "memory.unreadable"
    MEMORY_CONFIGURED: Final = "memory.configured"
    # conversational capture
    MEMORY_CAPTURED: Final = "memory.captured"
    MEMORY_CAPTURE_DECIDED: Final = "memory.capture_decided"
    MEMORY_CAPTURE_FAILED: Final = "memory.capture_failed"
    # correction / conflict resolution
    MEMORY_RESOLVED: Final = "memory.resolved"
    MEMORY_RESOLVE_FAILED: Final = "memory.resolve_failed"
    MEMORY_SUPERSEDED: Final = "memory.superseded"
    MEMORY_CONFLICT_PENDING: Final = "memory.conflict_pending"
    MEMORY_CONFLICT_RESOLVED: Final = "memory.conflict_resolved"
    MEMORY_FORGOTTEN: Final = "memory.forgotten"
    MEMORY_FORGET_UNRESOLVED: Final = "memory.forget_unresolved"
    # durable pending conflicts
    MEMORY_CONFLICT_RECORDED: Final = "memory.conflict_recorded"
    MEMORY_CONFLICT_UNREADABLE: Final = "memory.conflict_unreadable"
    MEMORY_CONFLICT_DELETED: Final = "memory.conflict_deleted"
    MEMORY_CONFLICT_INCOMPLETE: Final = "memory.conflict_incomplete"
    MEMORY_CAPTURE_RESULT: Final = "memory.capture_result"
    MEMORY_CONFLICT_DETECTED: Final = "memory.conflict_detected"
    MEMORY_CORRECTION_APPLIED: Final = "memory.correction_applied"

    # conversation runtime
    CONVERSATION_STARTED: Final = "conversation.started"
    CONVERSATION_RESUMED: Final = "conversation.resumed"
    CONVERSATION_SAVED: Final = "conversation.saved"
    CONVERSATION_FAILED: Final = "conversation.failed"
    CONVERSATION_TURN_COMPLETED: Final = "conversation.turn_completed"
    USER_MESSAGE_RECEIVED: Final = "conversation.user_message_received"
    ASSISTANT_MESSAGE_GENERATED: Final = "conversation.assistant_message_generated"
    # conversational tool use
    CONVERSATION_TOOL_REQUESTED: Final = "conversation.tool_requested"
    CONVERSATION_TOOL_STARTED: Final = "conversation.tool_started"
    CONVERSATION_TOOL_COMPLETED: Final = "conversation.tool_completed"
    CONVERSATION_TOOL_FAILED: Final = "conversation.tool_failed"
    CONVERSATION_TOOL_DENIED: Final = "conversation.tool_denied"
    CONVERSATION_TOOL_APPROVAL_REQUIRED: Final = "conversation.tool_approval_required"
    CONVERSATION_TOOL_ROUNDS_EXHAUSTED: Final = "conversation.tool_rounds_exhausted"

    # realtime turn lifecycle (Task 42.3 opt-in conversational integration).
    # These carry the opaque turn-session correlation key, never a raw
    # conversation session id, message text, tool output or arguments.
    TURN_BRIDGE_CONFIGURED: Final = "turn.bridge_configured"
    TURN_ADMITTED: Final = "turn.admitted"
    TURN_MODEL_STARTED: Final = "turn.model_started"
    TURN_TOOL_STARTED: Final = "turn.tool_started"
    TURN_TOOL_FINISHED: Final = "turn.tool_finished"
    TURN_STALE_RESULT_REJECTED: Final = "turn.stale_result_rejected"
    TURN_TIMED_OUT: Final = "turn.timed_out"
    TURN_FINALIZATION_STARTED: Final = "turn.finalization_started"
    TURN_FINALIZED: Final = "turn.finalized"

    # autonomous correlation (Task 42.4d)
    RUN_CORRELATED: Final = "run.correlated"

    # model layer
    MODEL_CONFIGURED: Final = "model.configured"
    MODEL_UNAVAILABLE: Final = "model.unavailable"
    MODEL_REQUEST: Final = "model.request"
    MODEL_RESPONSE: Final = "model.response"
    MODEL_FAILED: Final = "model.failed"
    MODEL_RETRY: Final = "model.retry"
