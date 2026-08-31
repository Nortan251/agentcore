"""Journal-bound credential permits beneath ToolExecutor, never authority.

This module does not approve, claim, retry, recover, or publish an action. Its
only dispatcher requires the attached executor's live authority and callback.
The pure-read runtime path is deliberately separate and unchanged.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from agentcore.action_journal.models import require_action_id
from agentcore.action_journal.protected_storage import MAX_PROTECTED_ACTION_PAYLOAD_BYTES, _canonical_content
from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import CredentialInfo, CredentialState, RuntimeOnly, require_scopes
from agentcore.credentials.runtime import (
    MAX_BINDINGS, CredentialRuntime, _interruption_type, _metadata_call,
    _provider_identity, detached_public_json,
)
from agentcore.credentials.vault import CredentialLease
from agentcore.realtime.errors import CancellationRequested
from agentcore.tools.authenticated_action import AuthenticatedActionTool
from agentcore.tools.base import ToolResult


def _detach(value: Mapping[str, Any]) -> dict[str, Any]:
    # Reuse protected storage's bounded, credential-key-rejecting JSON grammar.
    return _canonical_content(value, MAX_PROTECTED_ACTION_PAYLOAD_BYTES)[0]


@dataclass(frozen=True)
class CredentialActionIdentity:
    """Correlation only, minted from a verified executor claim."""

    action_id: str
    attempt_id: str
    intent_digest: str
    claim_epoch: int | None = None

    def validate(self) -> CredentialActionIdentity:
        require_action_id(self.action_id)
        if type(self.attempt_id) is not str or re.fullmatch(r"aat_[0-9a-f]{12}", self.attempt_id) is None or (
                type(self.intent_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", self.intent_digest) is None):
            raise CredentialError(CredentialFailure.INVALID)
        if self.claim_epoch is not None and (type(self.claim_epoch) is not int or self.claim_epoch < 1):
            raise CredentialError(CredentialFailure.INVALID)
        return self


class JournaledCredentialProvider(Protocol):
    """Trusted bounded synchronous operations; no refresh or hidden retries.

    validate_intent is pure/local, checks the durable account and exact scope,
    and rejects unavailable transport before the conservative send seam.
    Results are private inputs to the registered effect/reconciliation adapter.
    """

    provider_id: str

    def validate_intent(self, payload: Mapping[str, Any], info: CredentialInfo, *, reconcile: bool) -> None: ...
    def send(self, payload: Mapping[str, Any], credential: CredentialLease,
             identity: CredentialActionIdentity) -> ToolResult: ...
    def reconcile(self, payload: Mapping[str, Any], credential: CredentialLease,
                  identity: CredentialActionIdentity) -> ToolResult: ...


class _ActionBinding(RuntimeOnly):
    def __init__(self, tool, provider, info, scopes) -> None:
        self.tool, self.spec, self.provider, self.info, self.scopes = tool, tool.spec, provider, info, scopes
        # Capture exact bound operations, not mutable callable attributes at I/O.
        self.validate = _metadata_call(lambda: provider.validate_intent)
        self.send = _metadata_call(lambda: provider.send)
        self.reconcile = _metadata_call(lambda: provider.reconcile)
        if not all(callable(method) for method in (self.validate, self.send, self.reconcile)):
            raise CredentialError(CredentialFailure.INVALID)
        self.active = 0


def bind_action(runtime: CredentialRuntime, tool: AuthenticatedActionTool, *, reference: str,
                provider: JournaledCredentialProvider, required_scopes: frozenset[str],
                replace_existing: bool = False) -> CredentialInfo:
    runtime._require_owner()
    if type(tool) is not AuthenticatedActionTool:
        raise CredentialError(CredentialFailure.UNSUPPORTED)
    tool.validate_contract()
    require_scopes(required_scopes)
    provider_id = _provider_identity(provider)
    info = _metadata_call(lambda: runtime._vault.describe(reference))
    if type(info) is not CredentialInfo or info.state is not CredentialState.ACTIVE:
        raise CredentialError(CredentialFailure.BINDING)
    if provider_id != info.provider:
        raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
    if required_scopes != info.scopes:
        raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
    binding = _ActionBinding(tool, provider, info, required_scopes)
    with runtime._lock:
        runtime._require_owner()
        if tool.spec.name in runtime._bindings:
            raise CredentialError(CredentialFailure.BINDING)
        current = runtime._action_bindings.get(tool.spec.name)
        if current is not None:
            if not replace_existing:
                raise CredentialError(CredentialFailure.BINDING)
            if current.active:
                raise CredentialError(CredentialFailure.BUSY)
            if current.tool is tool and (
                current.info.connection_id != info.connection_id
                or current.info.account_id != info.account_id
                or current.provider is not provider
                or current.scopes != required_scopes or current.spec != tool.spec
            ):
                raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
        elif len(runtime._bindings) + len(runtime._action_bindings) >= MAX_BINDINGS:
            raise CredentialError(CredentialFailure.CAPACITY)
        runtime._action_bindings[tool.spec.name] = binding
    return info


def action_snapshot(runtime: CredentialRuntime, tool: AuthenticatedActionTool) -> _ActionBinding:
    runtime._require_owner()
    with runtime._lock:
        runtime._require_owner()
        binding = runtime._action_bindings.get(tool.spec.name)
        if binding is None or binding.tool is not tool or binding.spec != tool.spec:
            raise CredentialError(CredentialFailure.BINDING)
        return binding


class CredentialActionExecution(RuntimeOnly):
    """Live one-use permit, pinned to an exact action, attempt and operation."""

    def __init__(self, runtime, binding, arguments, payload, identity, authorize, *,
                 authority: object, reconciler: object | None = None) -> None:
        runtime._require_authority(authority)
        self._runtime, self._binding = runtime, binding
        self._arguments, self._payload = _detach(arguments), _detach(payload)
        self._identity = identity.validate()
        self._authorize = authorize
        self._reconciler = reconciler
        if (reconciler is None) != (identity.claim_epoch is None):
            raise CredentialError(CredentialFailure.AUTHORITY)
        self._owner_pid, self._thread_id = os.getpid(), threading.get_ident()
        self._active, self._used, self._dispatched, self._entered = True, False, False, False

    def _owner(self) -> None:
        if self._owner_pid != os.getpid() or self._thread_id != threading.get_ident():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if not self._active:
            raise CredentialError(CredentialFailure.AUTHORITY)

    @property
    def provider_entered(self) -> bool:
        self._owner()
        return self._entered

    def invoke(self, tool: AuthenticatedActionTool, arguments: Mapping[str, Any]) -> ToolResult:
        self._owner()
        if self._used or self._reconciler is not None or tool is not self._binding.tool or (
                type(arguments) is not dict or _detach(arguments) != self._arguments):
            raise CredentialError(CredentialFailure.AUTHORITY)
        self._used = True
        return self._dispatch()

    def invoke_probe(self, reconciler: object) -> ToolResult:
        self._owner()
        if self._used or self._reconciler is None or reconciler is not self._reconciler:
            raise CredentialError(CredentialFailure.AUTHORITY)
        self._used = True
        return self._dispatch()

    def _dispatch(self) -> ToolResult:
        self._owner()
        if not self._used or self._dispatched:
            raise CredentialError(CredentialFailure.AUTHORITY)
        self._dispatched = True
        runtime, binding = self._runtime, self._binding
        runtime._require_owner()
        reconcile = self._reconciler is not None
        # First current check is before a vault lookup. The second is leased
        # across final identity/expiry checks, provider I/O and result scrubbing.
        with self._authorize():
            pass
        if action_snapshot(runtime, binding.tool) is not binding or binding.active < 1:
            raise CredentialError(CredentialFailure.BINDING)
        _metadata_call(lambda: binding.validate(_detach(self._payload), binding.info, reconcile=reconcile))
        failure = None
        interruption = None
        host_cancellation = None
        safe = None
        try:
            if _provider_identity(binding.provider) != binding.info.provider:
                raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
            with runtime._vault._lease(binding.info, binding.scopes, authority=runtime._vault_authority) as lease:
                if type(lease) is not CredentialLease or lease.info != binding.info:
                    raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
                lease._check_current()
                _metadata_call(lambda: binding.validate(_detach(self._payload), lease.info, reconcile=reconcile))
                try:
                    with self._authorize():
                        self._owner()
                        if _provider_identity(binding.provider) != binding.info.provider:
                            raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
                        lease._check_current()
                        operation = binding.reconcile if reconcile else binding.send
                        # No provider byte can cross before this bit. Once set,
                        # exceptions and cancellation never imply no effect.
                        self._entered = True
                        try:
                            raw = operation(_detach(self._payload), lease, self._identity)
                        except BaseException as exc:
                            interruption = _interruption_type(exc)
                            failure = CredentialFailure.PROVIDER
                        else:
                            if type(raw) is not ToolResult or raw.ok is not True or raw.error is not None or type(raw.metadata) is not dict:
                                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
                            detached = detached_public_json(
                                {"output": raw.output, "metadata": raw.metadata},
                                secrets=lease._redaction_variants((binding.info.reference,
                                                                  binding.info.account_id, binding.info.connection_id)),
                            )
                            safe = ToolResult(ok=True, output=detached["output"], metadata=detached["metadata"])
                except CancellationRequested as exc:
                    # The provider exception above never reaches this handler.
                    host_cancellation = exc
                    raise
        except CancellationRequested as exc:
            if exc is host_cancellation:
                raise
            failure = CredentialFailure.UNAVAILABLE
        except CredentialError as exc:
            failure = exc.reason
        except BaseException as exc:
            interruption = _interruption_type(exc)
            failure = CredentialFailure.UNAVAILABLE
        if interruption is not None:
            raise interruption()
        if failure is not None:
            raise CredentialError(failure)
        if safe is None:
            raise CredentialError(CredentialFailure.PROVIDER)
        return safe

    def _finish(self) -> None:
        self._owner()
        self._active = False
        self._arguments, self._payload = {}, {}
        self._authorize = None


@contextmanager
def action_execution(runtime: CredentialRuntime, tool: AuthenticatedActionTool, binding: _ActionBinding,
                     arguments: dict[str, Any], payload: Mapping[str, Any], identity: CredentialActionIdentity,
                     authorize: Callable[[], AbstractContextManager[None]], *, authority: object,
                     reconciler: object | None = None) -> Iterator[CredentialActionExecution]:
    runtime._require_authority(authority)
    permit = CredentialActionExecution(runtime, binding, arguments, payload, identity, authorize,
                                       authority=authority, reconciler=reconciler)
    with runtime._lock:
        runtime._require_owner()
        if action_snapshot(runtime, tool) is not binding:
            raise CredentialError(CredentialFailure.BINDING)
        binding.active += 1
    try:
        yield permit
    finally:
        runtime._require_owner()
        permit._finish()
        with runtime._lock:
            runtime._require_owner()
            binding.active -= 1
