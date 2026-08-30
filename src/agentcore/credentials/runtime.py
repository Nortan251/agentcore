"""Exact tool/account bindings and executor-issued, one-use credential access.

The host registers a provider operation; the model never selects a provider,
credential, account, scope, or callable. This first slice admits only pure read
tools. Mutation approvals and reconciliation need a separately reviewed durable
account binding and are intentionally unsupported here.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import (
    CredentialInfo,
    CredentialState,
    RuntimeOnly,
    require_provider,
    require_scopes,
)
from agentcore.credentials.vault import CredentialLease, CredentialVault
from agentcore.realtime.errors import CancellationRequested

if TYPE_CHECKING:
    from agentcore.tools.authenticated import AuthenticatedReadTool
    from agentcore.tools.base import ToolResult


MAX_PUBLIC_BYTES = 65_536
MAX_BINDINGS = 256
_RESERVED_KEYS = frozenset({
    "account", "accountid", "connection", "connectionid", "credential",
    "credentials", "credentialref", "credentialreference", "credentialexecution",
    "token", "accesstoken", "refreshtoken", "idtoken", "apikey", "secret",
    "clientsecret", "password", "authorization", "auth", "cookie", "cookies",
    "provider", "providerid", "scope", "scopes", "capability", "capabilities",
    "oauth", "oauthtoken", "bearer", "xapikey", "setcookie",
})


def reserved_credential_key(value: str) -> bool:
    return value.lower().replace("_", "").replace("-", "") in _RESERVED_KEYS


def _metadata_call(callback: Callable[[], Any]) -> Any:
    """A pluggable backend/provider may fail without exposing its exception."""
    reason = CredentialFailure.UNAVAILABLE
    interruption: type[BaseException] | None = None
    try:
        return callback()
    except CredentialError as exc:
        reason = exc.reason
    except BaseException as exc:
        interruption = _interruption_type(exc)
    if interruption is not None:
        raise interruption()
    raise CredentialError(reason)


def _interruption_type(error: BaseException) -> type[BaseException] | None:
    # Preserve host interruption semantics without provider-controlled text,
    # exit codes, subclasses, exception notes, or exception chains.
    if isinstance(error, KeyboardInterrupt):
        return KeyboardInterrupt
    if isinstance(error, SystemExit):
        return SystemExit
    return None


def _provider_identity(provider: Any) -> str:
    return require_provider(_metadata_call(lambda: provider.provider_id))


def detached_public_json(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Bound and detach plain JSON without repr, default=str, or custom hooks.

    Reject credential-shaped fields and literal/common transport encodings of
    the leased secret. This is defense in depth for trusted provider
    normalizers, not general DLP or a sandbox for malicious adapters.
    """
    remaining = MAX_PUBLIC_BYTES
    nodes = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining, nodes
        nodes += 1
        if depth > 8 or nodes > 2048:
            raise CredentialError(CredentialFailure.UNSAFE_RESULT)
        if item is None or type(item) is bool:
            remaining -= 5
            result = item
        elif type(item) is int:
            if item.bit_length() > 128:
                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
            remaining -= 40
            result = item
        elif type(item) is float:
            if not math.isfinite(item):
                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
            remaining -= 32
            result = item
        elif type(item) is str:
            if len(item) > MAX_PUBLIC_BYTES or any(secret in item for secret in secrets):
                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
            remaining -= len(item.encode("utf-8"))
            result = item
        elif type(item) is list:
            if len(item) > 1024:
                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
            result = [visit(child, depth + 1) for child in item]
        elif type(item) is dict:
            if len(item) > 1024:
                raise CredentialError(CredentialFailure.UNSAFE_RESULT)
            result = {}
            for key, child in item.items():
                if type(key) is not str or len(key) > MAX_PUBLIC_BYTES or (
                        reserved_credential_key(key)):
                    raise CredentialError(CredentialFailure.UNSAFE_RESULT)
                result[visit(key, depth + 1)] = visit(child, depth + 1)
        else:
            raise CredentialError(CredentialFailure.UNSAFE_RESULT)
        if remaining < 0:
            raise CredentialError(CredentialFailure.UNSAFE_RESULT)
        return result

    result = visit(value, 0)
    serialized = json.dumps(result, ensure_ascii=True, allow_nan=False)
    # Inspect the serialized representation as well: numeric credential text
    # must not escape simply because a provider cast it to int/float/bool.
    if len(serialized.encode("utf-8")) > MAX_PUBLIC_BYTES or any(
            secret in serialized for secret in secrets):
        raise CredentialError(CredentialFailure.UNSAFE_RESULT)
    return result


class AuthenticatedReadProvider(Protocol):
    """Trusted, synchronous pure-read transport plus safe result normalization.

    No refresh, redirect to another credential recipient, mutation, hidden
    retry, request/response debug logging, or plaintext caching is permitted.
    OAuth and transport implementations must separately verify identity,
    audience, endpoint, and scopes. The runtime never infers these from a token.
    """

    provider_id: str

    def read(self, args: Mapping[str, Any], credential: CredentialLease) -> ToolResult: ...


class _Binding(RuntimeOnly):
    __slots__ = ("tool", "spec", "provider", "read", "info", "scopes", "active")

    def __init__(
        self,
        tool: AuthenticatedReadTool,
        provider: AuthenticatedReadProvider,
        info: CredentialInfo,
        scopes: frozenset[str],
        read: Callable[..., Any],
    ) -> None:
        self.tool = tool
        self.spec = tool.spec
        self.provider = provider
        self.read = read
        self.info = info
        self.scopes = scopes
        self.active = 0


class CredentialExecution(RuntimeOnly):
    """A live executor-created permit. Never construct, cache, or serialize it."""

    __slots__ = ("_binding", "_runtime", "_arguments", "_authorize",
                 "_owner_pid", "_thread_id", "_active", "_used", "_dispatched")

    def __init__(
        self,
        runtime: CredentialRuntime,
        binding: _Binding,
        arguments: dict[str, Any],
        authorize: Callable[[], None],
        *,
        authority: object,
    ) -> None:
        runtime._require_authority(authority)
        self._runtime = runtime
        self._binding = binding
        self._arguments = detached_public_json(arguments)
        self._authorize: Callable[[], None] | None = authorize
        self._owner_pid = os.getpid()
        self._thread_id = threading.get_ident()
        self._active = True
        self._used = False
        self._dispatched = False

    def _require_owner(self) -> None:
        if self._owner_pid != os.getpid() or self._thread_id != threading.get_ident():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if not self._active:
            raise CredentialError(CredentialFailure.AUTHORITY)

    def invoke(self, tool: AuthenticatedReadTool, args: Mapping[str, Any]) -> ToolResult:
        self._require_owner()
        if self._used or tool is not self._binding.tool or type(args) is not dict:
            raise CredentialError(CredentialFailure.AUTHORITY)
        if detached_public_json(args) != self._arguments:
            raise CredentialError(CredentialFailure.AUTHORITY)
        self._used = True
        return self._runtime._read(self)

    def _finish(self) -> None:
        self._require_owner()
        self._active = False
        self._authorize = None
        self._arguments = {}


class CredentialRuntime(RuntimeOnly):
    """Host-owned binding registry attached to one vault and one executor.

    Binding replacement is explicit, per-name, and refused during execution.
    Rotation may rebind the same tool only to the same connection, account,
    provider and required scopes. Switching accounts needs a new tool object.
    """

    __slots__ = ("_owner_pid", "_vault", "_vault_authority", "_authority",
                 "_lock", "_bindings", "_closed")

    def __init__(self, vault: CredentialVault) -> None:
        self._owner_pid = os.getpid()
        self._vault = vault
        self._vault_authority = object()
        self._authority: object | None = None
        self._lock = threading.RLock()
        self._bindings: dict[str, _Binding] = {}
        self._closed = False
        _metadata_call(lambda: vault._bind_runtime(self._vault_authority))

    def _require_owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._closed:
            raise CredentialError(CredentialFailure.UNAVAILABLE)

    def _attach_executor(self, authority: object) -> None:
        self._require_owner()
        with self._lock:
            self._require_owner()
            if authority is None or self._authority is not None:
                raise CredentialError(CredentialFailure.AUTHORITY)
            self._authority = authority

    def _require_authority(self, authority: object) -> None:
        self._require_owner()
        if self._authority is None or authority is not self._authority:
            raise CredentialError(CredentialFailure.AUTHORITY)

    def bind(
        self,
        tool: AuthenticatedReadTool,
        *,
        reference: str,
        provider: AuthenticatedReadProvider,
        required_scopes: frozenset[str],
        replace_existing: bool = False,
    ) -> CredentialInfo:
        """Trusted composition only. A binding cannot authorize a tool call."""
        from agentcore.tools.authenticated import AuthenticatedReadTool

        self._require_owner()
        if type(tool) is not AuthenticatedReadTool:
            raise CredentialError(CredentialFailure.UNSUPPORTED)
        tool.validate_contract()
        require_scopes(required_scopes)
        provider_id = _provider_identity(provider)
        info = _metadata_call(lambda: self._vault.describe(reference))
        if type(info) is not CredentialInfo or info.state is not CredentialState.ACTIVE:
            raise CredentialError(CredentialFailure.BINDING)
        if provider_id != info.provider:
            raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
        if not required_scopes <= info.scopes:
            raise CredentialError(CredentialFailure.SCOPE_MISMATCH)
        read = _metadata_call(lambda: provider.read)
        if not callable(read):
            raise CredentialError(CredentialFailure.INVALID)
        binding = _Binding(tool, provider, info, required_scopes, read)
        self._require_owner()
        with self._lock:
            self._require_owner()
            current = self._bindings.get(tool.spec.name)
            if current is not None:
                if not replace_existing:
                    raise CredentialError(CredentialFailure.BINDING)
                if current.active:
                    raise CredentialError(CredentialFailure.BUSY)
                if current.tool is tool and (
                    current.info.connection_id != info.connection_id
                    or current.info.account_id != info.account_id
                    or current.provider is not provider
                    or current.scopes != required_scopes
                    or current.spec != tool.spec
                ):
                    raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
            elif len(self._bindings) >= MAX_BINDINGS:
                raise CredentialError(CredentialFailure.CAPACITY)
            self._bindings[tool.spec.name] = binding
        return info

    def _snapshot(self, tool: AuthenticatedReadTool) -> _Binding:
        self._require_owner()
        with self._lock:
            self._require_owner()
            binding = self._bindings.get(tool.spec.name)
            if binding is None or binding.tool is not tool or binding.spec != tool.spec:
                raise CredentialError(CredentialFailure.BINDING)
            return binding

    @contextmanager
    def _execution(
        self,
        tool: AuthenticatedReadTool,
        binding: _Binding,
        args: dict[str, Any],
        authorize: Callable[[], None],
        *,
        authority: object,
    ) -> Iterator[CredentialExecution]:
        self._require_authority(authority)
        with self._lock:
            self._require_owner()
            if self._snapshot(tool) is not binding:
                raise CredentialError(CredentialFailure.BINDING)
            permit = CredentialExecution(self, binding, args, authorize, authority=authority)
            binding.active += 1
        try:
            yield permit
        finally:
            self._require_owner()
            permit._finish()
            with self._lock:
                self._require_owner()
                binding.active -= 1

    def _read(self, permit: CredentialExecution) -> ToolResult:
        from agentcore.tools.base import ToolResult

        self._require_owner()
        if type(permit) is not CredentialExecution or permit._runtime is not self:
            raise CredentialError(CredentialFailure.AUTHORITY)
        permit._require_owner()
        if not permit._used or permit._dispatched:
            raise CredentialError(CredentialFailure.AUTHORITY)
        permit._dispatched = True
        binding, args = permit._binding, permit._arguments
        assert permit._authorize is not None
        # No plaintext exists at either current permission/checkpoint check.
        # Even the private dispatch seam cannot bypass or replay this check.
        permit._authorize()
        permit._require_owner()
        if self._snapshot(binding.tool) is not binding or binding.active <= 0:
            raise CredentialError(CredentialFailure.BINDING)
        if _provider_identity(binding.provider) != binding.info.provider:
            raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
        failure: CredentialFailure | None = None
        interruption: type[BaseException] | None = None
        host_cancellation: CancellationRequested | None = None
        safe_result: ToolResult | None = None
        try:
            with self._vault._lease(
                binding.info, binding.scopes, authority=self._vault_authority,
            ) as lease:
                if type(lease) is not CredentialLease or lease.info != binding.info:
                    raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
                lease._check_current()
                # A backend lookup can wait while policy or provider identity
                # changes. Check again before revealing anything to the
                # provider. The vault metadata mutex is not held here.
                if _provider_identity(binding.provider) != binding.info.provider:
                    raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
                permit._require_owner()
                try:
                    permit._authorize()
                except CancellationRequested as exc:
                    host_cancellation = exc
                    raise
                permit._require_owner()
                # The last host callback can itself wait or change state.
                # Nothing may retarget the recipient or admit an expired
                # credential between that callback and provider dispatch.
                if _provider_identity(binding.provider) != binding.info.provider:
                    raise CredentialError(CredentialFailure.IDENTITY_MISMATCH)
                lease._check_current()
                # Only this registered pure-read provider sees the live lease.
                # Never let a provider exception (including its context/chain)
                # reach the legacy fast-path exception logger.
                try:
                    raw = binding.read(detached_public_json(args), lease)
                except BaseException as exc:
                    interruption = _interruption_type(exc)
                    failure = CredentialFailure.PROVIDER
                else:
                    if type(raw) is not ToolResult or type(raw.ok) is not bool:
                        failure = CredentialFailure.UNSAFE_RESULT
                    elif not raw.ok:
                        failure = CredentialFailure.PROVIDER
                    elif raw.error is not None or type(raw.metadata) is not dict:
                        failure = CredentialFailure.UNSAFE_RESULT
                    else:
                        public = detached_public_json(
                            {"output": raw.output, "metadata": raw.metadata},
                            secrets=lease._secret_variants(),
                        )
                        safe_result = ToolResult.success(
                            public["output"], **public["metadata"])
        except CancellationRequested as exc:
            # Only the exact exception from our host callback may propagate.
            # Backend enter/exit errors can use the same exception class with
            # credential-bearing detail and must still be sanitized.
            if exc is host_cancellation:
                raise
            failure = CredentialFailure.UNAVAILABLE
        except CredentialError as exc:
            failure = exc.reason
        except BaseException as exc:
            interruption = _interruption_type(exc)
            failure = CredentialFailure.UNAVAILABLE
        # Raise outside handlers: even __context__ must not retain a provider
        # exception containing a token, response body, or authorization header.
        if interruption is not None:
            raise interruption()
        if failure is not None:
            raise CredentialError(failure)
        if safe_result is None:
            raise CredentialError(CredentialFailure.PROVIDER)
        return safe_result

    def close(self) -> None:
        self._require_owner()
        with self._lock:
            self._require_owner()
            if any(binding.active for binding in self._bindings.values()):
                raise CredentialError(CredentialFailure.BUSY)
            self._bindings.clear()
            self._closed = True
