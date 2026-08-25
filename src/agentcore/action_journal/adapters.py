"""Trusted semantic adapters for the ToolExecutor action boundary.

Models and tool arguments never select journal eligibility or effect truth.
Trusted composition code registers one fixed semantic adapter against the
exact name/version/capability shape of a concrete tool.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agentcore.action_journal.errors import (
    ActionAdapterRegistrationError,
    ActionEffectNormalizationError,
    ActionValidationError,
)
from agentcore.action_journal.models import (
    INTENT_DIGEST_HEX_CHARS,
    MAX_ACTION_KIND_CHARS,
    MAX_CAPABILITY_NAME_CHARS,
    MAX_DEDUPLICATION_KEY_CHARS,
    MAX_DEDUPLICATION_SCOPE_CHARS,
    MAX_EVIDENCE_REFS,
    MAX_EVIDENCE_REF_CHARS,
    MAX_INTENT_REF_CHARS,
    MAX_PARTIAL_CATEGORIES,
    MAX_PARTIAL_CATEGORY_CHARS,
    MAX_RESULT_CLASS_CHARS,
    MAX_RESULT_SUMMARY_CHARS,
    MAX_TARGET_CATEGORY_CHARS,
    MAX_TOOL_NAME_CHARS,
    MAX_TOOL_VERSION_CHARS,
    TARGET_FINGERPRINT_HEX_CHARS,
    ActionEffectClass,
    ActionJournalEligibility,
    ActionOutcome,
    ActionProvenance,
    EffectVerdict,
    ReconciliationDisposition,
)
from agentcore.permissions.levels import (
    READ_ONLY_CAPABILITIES,
    Capability,
    PermissionLevel,
)


_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CATEGORICAL = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_VERSION_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@+=-]*$")
_EPHEMERAL_INCOMPATIBLE_CAPABILITIES = frozenset({
    Capability.FS_WRITE,
    Capability.FS_DELETE,
    Capability.EMAIL_SEND,
    Capability.CREDENTIAL_ACCESS,
    Capability.SYSTEM_ADMIN,
    Capability.PAYMENT,
})


@dataclass(frozen=True)
class ActionAdapterSpec:
    """Static trusted classification for one exact tool name/version."""

    tool_name: str
    tool_version: str
    action_kind: str
    eligibility: ActionJournalEligibility
    effect_class: ActionEffectClass | None = None
    reconciliation_capabilities: frozenset[Capability] = frozenset()

    def validate(self) -> ActionAdapterSpec:
        try:
            _categorical(
                self.tool_name, MAX_TOOL_NAME_CHARS, "adapter tool name")
            _version(
                self.tool_version, MAX_TOOL_VERSION_CHARS,
                "adapter tool version")
            _categorical(
                self.action_kind, MAX_ACTION_KIND_CHARS, "action kind")
        except ActionValidationError as exc:
            raise ActionAdapterRegistrationError(
                "action adapter identity is invalid") from exc
        if not isinstance(self.eligibility, ActionJournalEligibility):
            raise ActionAdapterRegistrationError(
                "action adapter eligibility is invalid")
        if not isinstance(self.reconciliation_capabilities, frozenset) or any(
                not isinstance(capability, Capability)
                for capability in self.reconciliation_capabilities):
            raise ActionAdapterRegistrationError(
                "reconciliation capabilities must be a capability frozenset")
        if any(capability not in READ_ONLY_CAPABILITIES
               for capability in self.reconciliation_capabilities):
            raise ActionAdapterRegistrationError(
                "reconciliation capabilities must be read-only")
        if self.eligibility.requires_journal:
            if not isinstance(self.effect_class, ActionEffectClass):
                raise ActionAdapterRegistrationError(
                    "journal-required adapter lacks an effect class")
        elif self.effect_class is not None:
            raise ActionAdapterRegistrationError(
                "non-journaled adapter carries an effect class")
        if not self.eligibility.requires_journal and \
                self.reconciliation_capabilities:
            raise ActionAdapterRegistrationError(
                "non-journaled adapter carries reconciliation capabilities")
        return self


@dataclass(frozen=True)
class PreparedActionIntent:
    """Pure/local canonical intent prepared before permission evaluation."""

    intent_digest: str
    target_category: str
    intent_ref: str | None = None
    target_fingerprint: str | None = None
    approval_description: str | None = None
    requires_protected_payload: bool = False

    def validate(self) -> PreparedActionIntent:
        _digest(self.intent_digest, "intent digest")
        _categorical(self.target_category, MAX_TARGET_CATEGORY_CHARS,
                     "target category")
        if self.intent_ref is not None:
            _reference(self.intent_ref, MAX_INTENT_REF_CHARS, "intent ref")
        if self.target_fingerprint is not None:
            _digest(self.target_fingerprint, "target fingerprint")
        if self.approval_description is not None:
            _safe_text(self.approval_description, MAX_RESULT_SUMMARY_CHARS,
                       "approval description")
        if not isinstance(self.requires_protected_payload, bool):
            raise ActionValidationError(
                "requires_protected_payload must be boolean")
        return self


@dataclass(frozen=True)
class NormalizedActionEffect:
    """Bounded capability-owned effect truth; never a provider raw payload."""

    verdict: EffectVerdict
    result_class: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    partial_categories: tuple[str, ...] = ()
    reconciliation_disposition: ReconciliationDisposition = (
        ReconciliationDisposition.NONE
    )

    def validate(self, spec: ActionAdapterSpec) -> NormalizedActionEffect:
        spec.validate()
        if not isinstance(self.verdict, EffectVerdict):
            raise ActionEffectNormalizationError("effect verdict is invalid")
        _categorical(self.result_class, MAX_RESULT_CLASS_CHARS,
                     "effect result class", normalization=True)
        _safe_text(self.summary, MAX_RESULT_SUMMARY_CHARS,
                   "effect summary", normalization=True)
        _reference_tuple(self.evidence_refs, MAX_EVIDENCE_REFS,
                         MAX_EVIDENCE_REF_CHARS, "effect evidence refs")
        _categorical_tuple(
            self.partial_categories,
            MAX_PARTIAL_CATEGORIES,
            MAX_PARTIAL_CATEGORY_CHARS,
            "partial categories",
        )
        if not isinstance(
                self.reconciliation_disposition,
                ReconciliationDisposition):
            raise ActionEffectNormalizationError(
                "reconciliation disposition is invalid")
        if self.verdict is EffectVerdict.OUTCOME_UNKNOWN:
            if self.partial_categories:
                raise ActionEffectNormalizationError(
                    "unknown effect carries partial categories")
            if self.reconciliation_disposition is (
                    ReconciliationDisposition.NONE):
                raise ActionEffectNormalizationError(
                    "unknown effect lacks a reconciliation disposition")
            if self.reconciliation_disposition is (
                    ReconciliationDisposition.PROBE_LATER) and not (
                        spec.reconciliation_capabilities):
                raise ActionEffectNormalizationError(
                    "probe-later disposition lacks a registered probe capability")
        else:
            if self.reconciliation_disposition is not (
                    ReconciliationDisposition.NONE):
                raise ActionEffectNormalizationError(
                    "resolved effect carries reconciliation disposition")
            try:
                ActionOutcome(
                    verdict=self.verdict,
                    result_class=self.result_class,
                    summary=self.summary,
                    partial_categories=self.partial_categories,
                ).validate()
            except ActionValidationError as exc:
                raise ActionEffectNormalizationError(
                    "normalized terminal effect is invalid") from exc
        return self


@dataclass(frozen=True)
class ActionInvocationContext:
    """Trusted provenance/dedup supplied outside generic observability metadata."""

    turn_id: str | None = None
    operation_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    subtask_id: str | None = None
    schedule_occurrence_id: str | None = None
    deduplication_scope: str | None = None
    deduplication_key: str | None = None

    def validate(self) -> ActionInvocationContext:
        # ActionProvenance owns exact identity validation.  The placeholder is
        # fixed and never leaves this validation call.
        self.to_provenance("inv_000000000000")
        if (self.deduplication_scope is None) != (
                self.deduplication_key is None):
            raise ActionValidationError(
                "trusted deduplication scope and key must appear together")
        if self.deduplication_scope is not None:
            _reference(
                self.deduplication_scope,
                MAX_DEDUPLICATION_SCOPE_CHARS,
                "trusted deduplication scope",
            )
            _reference(
                self.deduplication_key,
                MAX_DEDUPLICATION_KEY_CHARS,
                "trusted deduplication key",
            )
        return self

    def to_provenance(self, invocation_id: str) -> ActionProvenance:
        return ActionProvenance(
            invocation_id=invocation_id,
            turn_id=self.turn_id,
            operation_id=self.operation_id,
            run_id=self.run_id,
            task_id=self.task_id,
            subtask_id=self.subtask_id,
            schedule_occurrence_id=self.schedule_occurrence_id,
        ).validate()


class ToolActionAdapter(Protocol):
    """Trusted capability adapter.  It never receives an ActionStore."""

    @property
    def spec(self) -> ActionAdapterSpec: ...

    def prepare_intent(
        self,
        cleaned: Mapping[str, Any],
        context: Any,
    ) -> PreparedActionIntent: ...

    def normalize_effect(self, result: Any) -> NormalizedActionEffect: ...


@dataclass(frozen=True)
class _RegisteredAdapter:
    adapter: ToolActionAdapter
    spec: ActionAdapterSpec
    tool_name: str
    tool_version: str
    read_only: bool
    capabilities: tuple[str, ...]

    def prepare_intent(
        self,
        cleaned: Mapping[str, Any],
        context: Any,
    ) -> PreparedActionIntent:
        return self.adapter.prepare_intent(cleaned, context)

    def normalize_effect(self, result: Any) -> NormalizedActionEffect:
        return self.adapter.normalize_effect(result)


class ActionAdapterRegistry:
    """Thread-safe, no-replacement trusted semantic registration."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_tool: dict[tuple[str, str], _RegisteredAdapter] = {}
        self._by_kind: dict[str, tuple[str, str]] = {}

    def register(self, tool: Any, adapter: ToolActionAdapter) -> None:
        tool_spec = getattr(tool, "spec", None)
        adapter_spec = getattr(adapter, "spec", None)
        if tool_spec is None or not isinstance(adapter_spec, ActionAdapterSpec):
            raise ActionAdapterRegistrationError(
                "action adapter registration is malformed")
        adapter_spec.validate()
        if adapter_spec.tool_name != getattr(tool_spec, "name", None) or \
                adapter_spec.tool_version != getattr(tool_spec, "version", None):
            raise ActionAdapterRegistrationError(
                "action adapter does not match the exact tool name/version")
        read_only = getattr(tool_spec, "read_only", None)
        if not isinstance(read_only, bool):
            raise ActionAdapterRegistrationError("tool read-only flag is invalid")
        capabilities = getattr(tool_spec, "capabilities", None)
        if not isinstance(capabilities, frozenset) or any(
                not isinstance(capability, Capability)
                for capability in capabilities):
            raise ActionAdapterRegistrationError(
                "tool capabilities are invalid")
        if read_only:
            if adapter_spec.eligibility is not ActionJournalEligibility.PURE_READ:
                raise ActionAdapterRegistrationError(
                    "a read-only tool cannot register a mutation adapter")
        elif adapter_spec.eligibility is ActionJournalEligibility.PURE_READ:
            raise ActionAdapterRegistrationError(
                "a mutating tool cannot register as a pure read")
        if not read_only and not any(
                capability not in READ_ONLY_CAPABILITIES
                for capability in capabilities):
            raise ActionAdapterRegistrationError(
                "a mutating tool must declare an authority capability")
        if Capability.PAYMENT in capabilities:
            raise ActionAdapterRegistrationError(
                "financial actions are outside AgentCore authority")
        if adapter_spec.eligibility is (
                ActionJournalEligibility.EPHEMERAL_CONTROL) and (
                    capabilities & _EPHEMERAL_INCOMPATIBLE_CAPABILITIES):
            raise ActionAdapterRegistrationError(
                "ephemeral control carries a durable or forbidden capability")
        if adapter_spec.eligibility.requires_journal:
            if not callable(getattr(adapter, "prepare_intent", None)) or \
                    not callable(getattr(adapter, "normalize_effect", None)):
                raise ActionAdapterRegistrationError(
                    "journal-required adapter lacks preflight/normalization")
        key = (adapter_spec.tool_name, adapter_spec.tool_version)
        binding = _RegisteredAdapter(
            adapter=adapter,
            spec=adapter_spec,
            tool_name=adapter_spec.tool_name,
            tool_version=adapter_spec.tool_version,
            read_only=read_only,
            capabilities=tuple(sorted(str(capability)
                                      for capability in capabilities)),
        )
        with self._lock:
            if key in self._by_tool:
                raise ActionAdapterRegistrationError(
                    "action adapter is already registered")
            if adapter_spec.action_kind in self._by_kind:
                raise ActionAdapterRegistrationError(
                    "action kind is already registered")
            self._by_tool[key] = binding
            self._by_kind[adapter_spec.action_kind] = key

    def resolve(
        self,
        tool: Any,
        *,
        tool_spec: Any | None = None,
    ) -> ToolActionAdapter | None:
        tool_spec = tool_spec if tool_spec is not None else getattr(tool, "spec", None)
        name = getattr(tool_spec, "name", None)
        version = getattr(tool_spec, "version", None)
        with self._lock:
            binding = self._by_tool.get((name, version))
            same_name = any(key[0] == name for key in self._by_tool)
        if binding is None:
            if same_name:
                raise ActionAdapterRegistrationError(
                    "registered action adapter tool version does not match",
                    reason="version_mismatch",
                )
            return None
        current = tuple(sorted(str(capability)
                               for capability in tool_spec.capabilities))
        if binding.read_only is not tool_spec.read_only or \
                binding.capabilities != current:
            raise ActionAdapterRegistrationError(
                "registered action adapter no longer matches the tool shape",
                reason="tool_shape_mismatch",
            )
        # Return the immutable registration snapshot, not the adapter's live
        # ``spec`` property. A mutable/dynamic adapter cannot change its
        # eligibility or effect class after trusted registration.
        return binding

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_tool)


def permission_policy_fingerprint(policy: Any) -> str:
    """Hash only deterministic trusted policy configuration, never a gate."""
    config = getattr(policy, "config", None)
    level = getattr(config, "level", None)
    overrides = getattr(config, "capability_overrides", None)
    blocked = getattr(config, "blocked_capabilities", None)
    if not isinstance(level, PermissionLevel) or not isinstance(
            overrides, Mapping) or not isinstance(blocked, tuple):
        raise ActionValidationError("permission policy configuration is invalid")
    normalized_overrides: list[list[str]] = []
    for capability, required in overrides.items():
        if not isinstance(capability, str) or not isinstance(
                required, PermissionLevel):
            raise ActionValidationError(
                "permission policy override is invalid")
        normalized_overrides.append([capability, required.value])
    if any(not isinstance(value, str) for value in blocked):
        raise ActionValidationError(
            "permission policy blocked capability is invalid")
    return _canonical_digest({
        "domain": "agentcore.permission-policy.v1",
        "level": level.value,
        "capability_overrides": sorted(normalized_overrides),
        "blocked_capabilities": sorted(set(blocked)),
    })


def authority_binding_digest(
    *,
    action_kind: str,
    intent_digest: str,
    mutation_capabilities: tuple[str, ...],
    tool_name: str,
    tool_version: str,
    invocation_id: str,
    authority_scope: str,
    policy_fingerprint: str,
) -> str:
    """Bind one historical allow to exact intent/tool/live invocation scope."""
    return _canonical_digest({
        "domain": "agentcore.action-authority.v1",
        "action_kind": action_kind,
        "intent_digest": intent_digest,
        "mutation_capabilities": list(mutation_capabilities),
        "tool_name": tool_name,
        "tool_version": tool_version,
        "invocation_id": invocation_id,
        "authority_scope": authority_scope,
        "policy_fingerprint": policy_fingerprint,
    })


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != INTENT_DIGEST_HEX_CHARS or \
            not _LOWER_HEX_DIGEST.fullmatch(value):
        raise ActionValidationError(f"{field} is invalid")
    return value


def _categorical(value: Any, maximum: int, field: str,
                 *, normalization: bool = False) -> str:
    error = (ActionEffectNormalizationError if normalization
             else ActionValidationError)
    if not isinstance(value, str) or len(value) > maximum or not (
            _CATEGORICAL.fullmatch(value)):
        raise error(f"{field} is invalid")
    return value


def _version(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or not (
            _VERSION_TOKEN.fullmatch(value)):
        raise ActionAdapterRegistrationError(f"{field} is invalid")
    return value


def _reference(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or not (
            _OPAQUE_REFERENCE.fullmatch(value)):
        raise ActionValidationError(f"{field} is invalid")
    return value


def _safe_text(value: Any, maximum: int, field: str,
               *, normalization: bool = False) -> str:
    error = (ActionEffectNormalizationError if normalization
             else ActionValidationError)
    if not isinstance(value, str) or not value or len(value) > maximum or any(
            ord(character) < 32 or ord(character) == 127
            for character in value):
        raise error(f"{field} is invalid")
    return value


def _reference_tuple(values: Any, maximum_items: int, maximum_chars: int,
                     field: str) -> None:
    if not isinstance(values, tuple) or len(values) > maximum_items:
        raise ActionEffectNormalizationError(f"{field} is invalid")
    if len(set(values)) != len(values):
        raise ActionEffectNormalizationError(f"{field} contains duplicates")
    for value in values:
        try:
            _reference(value, maximum_chars, field)
        except ActionValidationError as exc:
            raise ActionEffectNormalizationError(f"{field} is invalid") from exc


def _categorical_tuple(values: Any, maximum_items: int, maximum_chars: int,
                       field: str) -> None:
    if not isinstance(values, tuple) or len(values) > maximum_items or \
            tuple(sorted(set(values))) != values:
        raise ActionEffectNormalizationError(f"{field} is invalid")
    for value in values:
        _categorical(value, maximum_chars, field, normalization=True)


__all__ = [
    "ActionAdapterRegistry",
    "ActionAdapterSpec",
    "ActionInvocationContext",
    "NormalizedActionEffect",
    "PreparedActionIntent",
    "ToolActionAdapter",
    "authority_binding_digest",
    "permission_policy_fingerprint",
]
