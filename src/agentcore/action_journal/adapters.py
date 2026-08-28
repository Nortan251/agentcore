"""Trusted semantic adapters for the ToolExecutor action boundary.

Models and tool arguments never select journal eligibility or effect truth.
Trusted composition code registers one fixed semantic adapter against the
exact name/version/capability shape of a concrete tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

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
    require_action_id,
)
from agentcore.action_journal.idempotency import (
    ActionExecutionPermit,
    ExternalIdempotencyProvider,
    ExternalIdempotencyRequest,
)
from agentcore.action_journal.reconciliation import ActionReconciler
from agentcore.action_journal.protected_storage import (
    looks_like_evidence_reference,
    looks_like_protected_payload_reference,
)
from agentcore.permissions.levels import (
    READ_ONLY_CAPABILITIES,
    Capability,
    PermissionLevel,
)


_LOWER_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_APPROVAL_REQUEST_ID = re.compile(r"^apr_[0-9a-f]{12}$")
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
_RESERVED_IDEMPOTENCY_ARGUMENTS = frozenset({
    "idempotency_key",
    "idempotency_contract",
    "idempotency_scope",
    "idempotency_valid_until",
    "external_idempotency_key",
    "external_idempotency_contract",
    "external_idempotency_scope",
    "external_idempotency_valid_until",
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
    protected_payload: Mapping[str, Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

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
        if self.protected_payload is not None and not isinstance(
                self.protected_payload, Mapping):
            raise ActionValidationError("protected payload must be a mapping")
        if self.intent_ref is not None and self.protected_payload is not None:
            raise ActionValidationError(
                "trusted payload content cannot carry a preselected reference")
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
    protected_evidence: tuple[Mapping[str, Any], ...] = field(
        default=(),
        repr=False,
        compare=False,
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
        if any(looks_like_evidence_reference(reference)
               or looks_like_protected_payload_reference(reference)
               for reference in self.evidence_refs):
            raise ActionEffectNormalizationError(
                "protected references are minted by trusted storage")
        if not isinstance(self.protected_evidence, tuple) or len(
                self.protected_evidence) > MAX_EVIDENCE_REFS or any(
                    not isinstance(item, Mapping)
                    for item in self.protected_evidence):
            raise ActionEffectNormalizationError(
                "protected effect evidence is invalid")
        if len(self.evidence_refs) + len(self.protected_evidence) > (
                MAX_EVIDENCE_REFS):
            raise ActionEffectNormalizationError(
                "effect evidence bound is exceeded")
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

    action_id: str | None = None
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
        if self.action_id is not None:
            require_action_id(self.action_id)
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


@dataclass(frozen=True)
class DuplicateRiskInvocationContext:
    """Trusted host continuity for one deliberate new risky-repeat action.

    This object is accepted only as an explicit executor keyword argument. It
    is never reconstructed from model-visible tool arguments, generic
    ``ToolContext.metadata``, or provider output.  The successor identity is
    minted by trusted runtime code before the human risk decision and remains
    stable across pending/approved continuation.
    """

    predecessor_action_id: str
    successor_action_id: str | None = None
    approval_request_id: str | None = None

    def validate(self) -> DuplicateRiskInvocationContext:
        require_action_id(self.predecessor_action_id)
        if self.successor_action_id is not None:
            require_action_id(self.successor_action_id)
            if self.successor_action_id == self.predecessor_action_id:
                raise ActionValidationError(
                    "duplicate-risk successor must be a new action")
        if self.approval_request_id is not None:
            if not isinstance(self.approval_request_id, str) or not (
                    _APPROVAL_REQUEST_ID.fullmatch(
                        self.approval_request_id)):
                raise ActionValidationError(
                    "duplicate-risk approval request id is invalid")
            if self.successor_action_id is None:
                raise ActionValidationError(
                    "duplicate-risk approval continuation requires the "
                    "successor action id")
        return self


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
    tool: Any
    tool_name: str
    tool_version: str
    read_only: bool
    capabilities: tuple[str, ...]
    reconciler: ActionReconciler | None
    establishes_external_contract: Callable[..., Any] | None
    validates_external_contract: Callable[..., Any] | None
    executes_external_effect_if_current: Callable[..., Any] | None
    allows_indefinite_retention: bool

    def prepare_intent(
        self,
        cleaned: Mapping[str, Any],
        context: Any,
    ) -> PreparedActionIntent:
        return self.adapter.prepare_intent(cleaned, context)

    def normalize_effect(self, result: Any) -> NormalizedActionEffect:
        return self.adapter.normalize_effect(result)


class ActionAdapterRegistry:
    """Process-owned, thread-safe trusted semantic registrations."""

    def __init__(self) -> None:
        self._owner_pid = os.getpid()
        self._lock = threading.RLock()
        self._by_tool: dict[tuple[str, str], _RegisteredAdapter] = {}
        self._by_kind: dict[str, tuple[str, str]] = {}

    def _require_owner_process(self) -> None:
        if self._owner_pid != os.getpid():
            raise ActionAdapterRegistrationError(
                "action adapter registry belongs to another process",
                reason="ownership_mismatch",
            )

    def register(
        self,
        tool: Any,
        adapter: ToolActionAdapter,
        *,
        reconciler: ActionReconciler | None = None,
        idempotency_provider: ExternalIdempotencyProvider | None = None,
    ) -> None:
        self._require_owner_process()
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
        external = (
            adapter_spec.effect_class is ActionEffectClass.EXTERNALLY_IDEMPOTENT
        )
        if external and idempotency_provider is None:
            raise ActionAdapterRegistrationError(
                "externally idempotent adapter lacks a trusted provider")
        if not external and idempotency_provider is not None:
            raise ActionAdapterRegistrationError(
                "non-external adapter carries an idempotency provider")
        establish: Callable[..., Any] | None = None
        validate: Callable[..., Any] | None = None
        execute_if_current: Callable[..., Any] | None = None
        indefinite = False
        if idempotency_provider is not None:
            establish = getattr(idempotency_provider, "establish_contract", None)
            validate = getattr(idempotency_provider, "validate_contract", None)
            execute_if_current = getattr(
                idempotency_provider, "execute_if_current", None)
            indefinite = getattr(
                idempotency_provider, "allows_indefinite_retention", None)
            if not all(callable(method) for method in (
                    establish, validate, execute_if_current)) or not isinstance(
                        indefinite, bool):
                raise ActionAdapterRegistrationError(
                    "external idempotency provider is malformed")
            parameter_names = {
                getattr(parameter, "name", None)
                for parameter in getattr(tool_spec, "parameters", ())
            }
            if parameter_names & _RESERVED_IDEMPOTENCY_ARGUMENTS:
                raise ActionAdapterRegistrationError(
                    "model-visible arguments cannot select idempotency metadata")
        if reconciler is not None and not callable(
                getattr(reconciler, "probe", None)):
            raise ActionAdapterRegistrationError(
                "action reconciler lacks a read-only probe")
        if reconciler is not None and not (
                adapter_spec.reconciliation_capabilities):
            raise ActionAdapterRegistrationError(
                "action reconciler lacks registered probe capabilities")
        key = (adapter_spec.tool_name, adapter_spec.tool_version)
        binding = _RegisteredAdapter(
            adapter=adapter,
            spec=adapter_spec,
            tool=tool,
            tool_name=adapter_spec.tool_name,
            tool_version=adapter_spec.tool_version,
            read_only=read_only,
            capabilities=tuple(sorted(str(capability)
                                      for capability in capabilities)),
            reconciler=reconciler,
            establishes_external_contract=establish,
            validates_external_contract=validate,
            executes_external_effect_if_current=execute_if_current,
            allows_indefinite_retention=indefinite,
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
        self._require_owner_process()
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
        if binding.tool is not tool:
            raise ActionAdapterRegistrationError(
                "registered action adapter belongs to a different tool object",
                reason="tool_identity_mismatch",
            )
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

    def resolve_reconciler(
        self,
        tool: Any,
        *,
        tool_spec: Any | None = None,
    ) -> ActionReconciler | None:
        binding = self.resolve(tool, tool_spec=tool_spec)
        if binding is None:
            return None
        return binding.reconciler

    def establish_external_idempotency(
        self,
        tool: Any,
        request: ExternalIdempotencyRequest,
        *,
        tool_spec: Any | None = None,
    ) -> tuple[Any, bool]:
        """Call the registered key owner outside the registry mutex."""
        binding = self.resolve(tool, tool_spec=tool_spec)
        if binding is None or binding.establishes_external_contract is None:
            raise ActionAdapterRegistrationError(
                "external idempotency provider is unavailable")
        request.validate()
        return (
            binding.establishes_external_contract(request),
            binding.allows_indefinite_retention,
        )

    def validate_external_idempotency(
        self,
        tool: Any,
        request: ExternalIdempotencyRequest,
        stored_contract: Any,
        *,
        tool_spec: Any | None = None,
    ) -> tuple[Any, bool]:
        """Revalidate the exact durable guarantee under current registration."""
        binding = self.resolve(tool, tool_spec=tool_spec)
        if binding is None or binding.validates_external_contract is None:
            raise ActionAdapterRegistrationError(
                "external idempotency provider is unavailable")
        request.validate()
        return (
            binding.validates_external_contract(request, stored_contract),
            binding.allows_indefinite_retention,
        )

    def execute_external(
        self,
        tool: Any,
        request: ExternalIdempotencyRequest,
        cleaned: Mapping[str, Any],
        context: Any,
        permit: ActionExecutionPermit,
        *,
        tool_spec: Any | None = None,
    ) -> Any:
        """Enter the provider's validate-current-and-execute boundary.

        The provider must revalidate the exact durable permit against current
        provider semantics immediately before it sends the physical request.
        """
        binding = self.resolve(tool, tool_spec=tool_spec)
        if binding is None or \
                binding.executes_external_effect_if_current is None:
            raise ActionAdapterRegistrationError(
                "external idempotency provider is unavailable")
        request.validate()
        permit.validate()
        if binding.tool_name != permit.tool_name or \
                binding.tool_version != permit.tool_version:
            raise ActionAdapterRegistrationError(
                "execution permit does not match registered tool")
        if request.action_id != permit.action_id or \
                request.tool_name != binding.tool_name or \
                request.tool_version != binding.tool_version or \
                request.action_kind != binding.spec.action_kind or \
                request.effect_class is not binding.spec.effect_class:
            raise ActionAdapterRegistrationError(
                "execution request does not match registered action")
        return binding.executes_external_effect_if_current(
            request, tool, cleaned, context, permit)

    def __len__(self) -> int:
        self._require_owner_process()
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
