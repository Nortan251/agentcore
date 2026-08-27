"""Trusted workflow bindings for durable actions across replans.

The planner sees only opaque, one-use offer tokens. Durable action ids and
deduplication identities remain in trusted state and are never model input.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agentcore.action_journal import ActionInvocationContext, require_action_id
from agentcore.action_journal.errors import ActionValidationError
from agentcore.state.models import AgentState
from agentcore.tasks.models import Task
from agentcore.util.ids import new_id


ACTION_BINDINGS_KEY = "action_journal_bindings"
ACTION_BINDINGS_SCHEMA_VERSION = 2
ACTION_BINDING_OFFERS_CONTEXT_KEY = "trusted_action_binding_offers"
MAX_ACTION_BINDINGS = 256

_LEGACY_ACTION_BINDINGS_SCHEMA_VERSION = 1
_BINDING_FIELDS = frozenset({
    "action_id",
    "deduplication_scope",
    "deduplication_key",
})
_OFFER_TOKEN = re.compile(r"^wfb_[0-9a-f]{12}$")


@dataclass(frozen=True)
class WorkflowActionBinding:
    """Trusted durable identity for one workflow step."""

    action_id: str
    deduplication_scope: str
    deduplication_key: str

    def validate(self) -> WorkflowActionBinding:
        ActionInvocationContext(
            action_id=self.action_id,
            deduplication_scope=self.deduplication_scope,
            deduplication_key=self.deduplication_key,
        ).validate()
        return self

    def to_dict(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "deduplication_scope": self.deduplication_scope,
            "deduplication_key": self.deduplication_key,
        }


@dataclass(frozen=True)
class ReplanActionBindingOffer:
    """Safe planner-visible handle for a trusted prior binding."""

    token: str
    prior_subtask_id: str
    tool_name: str

    def validate(self) -> ReplanActionBindingOffer:
        if not isinstance(self.token, str) or not _OFFER_TOKEN.fullmatch(
            self.token
        ):
            raise ActionValidationError("replan action binding token is invalid")
        _validate_subtask_id(self.prior_subtask_id)
        if (
            not isinstance(self.tool_name, str)
            or not self.tool_name
            or len(self.tool_name) > 128
        ):
            raise ActionValidationError("replan action binding tool is invalid")
        return self

    def to_model_context(self) -> dict[str, str]:
        return {
            "action_binding_token": self.token,
            "prior_subtask_id": self.prior_subtask_id,
            "tool_name": self.tool_name,
        }


@dataclass(frozen=True)
class TrustedReplanBindingCandidate:
    """Process-local association between a safe offer and durable identity."""

    offer: ReplanActionBindingOffer
    binding: WorkflowActionBinding

    def validate(self) -> TrustedReplanBindingCandidate:
        self.offer.validate()
        self.binding.validate()
        return self


def load_action_binding(
    state: AgentState,
    subtask_id: str,
) -> WorkflowActionBinding | None:
    """Load and validate one trusted workflow binding."""
    _validate_subtask_id(subtask_id)
    return _load_action_bindings(state).get(subtask_id)


def record_action_binding(
    state: AgentState,
    subtask_id: str,
    *,
    action_id: str | None,
    deduplication_scope: str,
    deduplication_key: str,
) -> None:
    """Record the exact identity returned by ToolExecutor."""
    if action_id is None:
        raise ActionValidationError(
            "journaled invocation lacks an action identity"
        )
    _validate_subtask_id(subtask_id)
    binding = WorkflowActionBinding(
        action_id=action_id,
        deduplication_scope=deduplication_scope,
        deduplication_key=deduplication_key,
    ).validate()
    actions = _load_action_bindings(state)
    existing = actions.get(subtask_id)
    if existing is not None and existing != binding:
        raise ActionValidationError(
            "workflow step changed durable action identity"
        )
    if subtask_id not in actions and len(actions) >= MAX_ACTION_BINDINGS:
        raise ActionValidationError("action binding ledger is full")
    actions[subtask_id] = binding
    _store_action_bindings(state, actions)


def carry_action_bindings(
    state: AgentState,
    transfers: Mapping[str, WorkflowActionBinding],
) -> None:
    """Atomically carry validated prior bindings onto replacement steps."""
    if not transfers:
        return
    actions = _load_action_bindings(state)
    candidate = dict(actions)
    for subtask_id, binding in transfers.items():
        _validate_subtask_id(subtask_id)
        if not isinstance(binding, WorkflowActionBinding):
            raise ActionValidationError("replan action binding is invalid")
        binding.validate()
        existing = candidate.get(subtask_id)
        if existing is not None and existing != binding:
            raise ActionValidationError(
                "workflow step changed durable action identity"
            )
        candidate[subtask_id] = binding
    if len(candidate) > MAX_ACTION_BINDINGS:
        raise ActionValidationError("action binding ledger is full")
    _store_action_bindings(state, candidate)


def build_replan_binding_candidates(
    state: AgentState,
    previous: Task,
    *,
    token_factory: Callable[[str], str] = new_id,
) -> tuple[
    tuple[ReplanActionBindingOffer, ...],
    dict[str, TrustedReplanBindingCandidate],
]:
    """Create bounded one-use offers for bindings owned by the prior task."""
    bindings = _load_action_bindings(state)
    offers: list[ReplanActionBindingOffer] = []
    candidates: dict[str, TrustedReplanBindingCandidate] = {}
    for subtask in previous.subtasks:
        binding = bindings.get(subtask.id)
        if binding is None or subtask.tool_name is None:
            continue
        offer = ReplanActionBindingOffer(
            token=token_factory("wfb"),
            prior_subtask_id=subtask.id,
            tool_name=subtask.tool_name,
        ).validate()
        if offer.token in candidates:
            raise ActionValidationError("replan action binding token collided")
        trusted = TrustedReplanBindingCandidate(
            offer=offer,
            binding=binding,
        ).validate()
        offers.append(offer)
        candidates[offer.token] = trusted
    return tuple(offers), candidates


def resolve_replan_binding_transfers(
    task: Task,
    candidates: Mapping[str, TrustedReplanBindingCandidate],
) -> tuple[dict[str, WorkflowActionBinding], str | None]:
    """Consume planner echoes and resolve them only against trusted offers."""
    if not candidates:
        return {}, None
    plan_meta = task.metadata.get("plan")
    annotations = (
        plan_meta.get("annotations", {})
        if isinstance(plan_meta, dict)
        else {}
    )
    if not isinstance(annotations, dict):
        annotations = {}

    subtasks = {subtask.id: subtask for subtask in task.subtasks}
    used: set[str] = set()
    selected_subtasks: set[str] = set()
    transfers: dict[str, WorkflowActionBinding] = {}
    for subtask_id, annotation in annotations.items():
        if not isinstance(annotation, dict):
            continue
        token = annotation.pop("action_binding_token", None)
        if token is None:
            continue
        if not isinstance(token, str) or not _OFFER_TOKEN.fullmatch(token):
            return {}, "replan action binding token is invalid"
        trusted = candidates.get(token)
        if trusted is None:
            return {}, "replan action binding token was not offered"
        if token in used:
            return {}, "replan action binding token was reused"
        subtask = subtasks.get(subtask_id)
        if subtask is None or subtask.tool_name != trusted.offer.tool_name:
            return {}, "replan action binding changed tool identity"
        used.add(token)
        selected_subtasks.add(subtask_id)
        transfers[subtask_id] = trusted.binding

    candidate_tools = {
        trusted.offer.tool_name
        for trusted in candidates.values()
    }
    for subtask in task.subtasks:
        if (
            subtask.tool_name in candidate_tools
            and subtask.id not in selected_subtasks
        ):
            return {}, "replan action continuity is unresolved"
    return transfers, None


def _load_action_bindings(
    state: AgentState,
) -> dict[str, WorkflowActionBinding]:
    raw = state.scratch.get(ACTION_BINDINGS_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "actions"}:
        raise ActionValidationError("action binding ledger is invalid")
    schema_version = raw.get("schema_version")
    if schema_version not in {
        _LEGACY_ACTION_BINDINGS_SCHEMA_VERSION,
        ACTION_BINDINGS_SCHEMA_VERSION,
    }:
        raise ActionValidationError("action binding ledger is invalid")
    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, dict) or len(raw_actions) > MAX_ACTION_BINDINGS:
        raise ActionValidationError("action binding ledger is invalid")

    actions: dict[str, WorkflowActionBinding] = {}
    for subtask_id, raw_binding in raw_actions.items():
        _validate_subtask_id(subtask_id)
        try:
            if schema_version == _LEGACY_ACTION_BINDINGS_SCHEMA_VERSION:
                require_action_id(raw_binding)
                binding = WorkflowActionBinding(
                    action_id=raw_binding,
                    deduplication_scope=f"run:{state.run_id}",
                    deduplication_key=f"workflow-step:{subtask_id}",
                )
            else:
                if not isinstance(raw_binding, dict) or set(raw_binding) != (
                    _BINDING_FIELDS
                ):
                    raise ActionValidationError(
                        "action binding ledger is invalid"
                    )
                binding = WorkflowActionBinding(
                    action_id=raw_binding["action_id"],
                    deduplication_scope=raw_binding["deduplication_scope"],
                    deduplication_key=raw_binding["deduplication_key"],
                )
            actions[subtask_id] = binding.validate()
        except Exception as exc:
            raise ActionValidationError(
                "action binding ledger is invalid"
            ) from exc
    return actions


def _store_action_bindings(
    state: AgentState,
    actions: Mapping[str, WorkflowActionBinding],
) -> None:
    state.scratch[ACTION_BINDINGS_KEY] = {
        "schema_version": ACTION_BINDINGS_SCHEMA_VERSION,
        "actions": {
            subtask_id: binding.to_dict()
            for subtask_id, binding in actions.items()
        },
    }


def _validate_subtask_id(subtask_id: Any) -> None:
    try:
        ActionInvocationContext(
            task_id="task_000000000000",
            subtask_id=subtask_id,
        ).validate()
    except Exception as exc:
        raise ActionValidationError("action binding subtask is invalid") from exc
