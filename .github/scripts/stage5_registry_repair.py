from __future__ import annotations

from pathlib import Path

REGISTRY = '''"""Tool registry: name -> tool, filtered by configuration."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from agentcore.config.schema import ToolConfig
from agentcore.errors import ToolError, ToolNotFoundError
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolSpec


class ToolRegistry:
    """Holds the tools available to an agent instance.

    The registry enforces the config allowlist at registration time. A scoped
    execution lease pins one exact name/object binding without holding the
    registry metadata lock across physical tool or provider I/O.
    """

    def __init__(self, config: ToolConfig | None = None) -> None:
        self._config = config or ToolConfig()
        self._tools: dict[str, Tool] = {}
        self._active_execution_leases: dict[str, int] = {}
        self._lock = threading.RLock()
        self._log = get_logger("tools.registry")

    def register(self, tool: Tool, *, replace: bool = False) -> bool:
        """Register a tool. Returns False if config disallows it."""
        name = tool.spec.name
        if not name:
            raise ToolError("tool name must not be empty")
        if not self._config.is_allowed(name):
            self._log.info("tool.skipped", tool=name,
                           reason="disabled by config")
            return False
        with self._lock:
            if self._active_execution_leases.get(name, 0):
                raise ToolError(
                    "tool registration has active executions",
                    tool=name,
                    reason="execution_binding_active",
                )
            if not replace and name in self._tools:
                raise ToolError("tool already registered", tool=name)
            self._tools[name] = tool
        self._log.info(Events.TOOL_REGISTERED, tool=name,
                       capabilities=sorted(str(c) for c in tool.capabilities),
                       read_only=tool.spec.read_only)
        return True

    def register_all(self, tools: list[Tool], *, replace: bool = False) -> int:
        return sum(1 for tool in tools if self.register(tool, replace=replace))

    def unregister(self, name: str) -> None:
        with self._lock:
            if self._active_execution_leases.get(name, 0):
                raise ToolError(
                    "tool registration has active executions",
                    tool=name,
                    reason="execution_binding_active",
                )
            if self._tools.pop(name, None) is None:
                raise ToolNotFoundError("tool is not registered", tool=name)

    def get(self, name: str) -> Tool:
        with self._lock:
            try:
                return self._tools[name]
            except KeyError as exc:
                available = sorted(self._tools)
        raise ToolNotFoundError(
            "tool is not registered", tool=name, available=available
        ) from exc

    @contextmanager
    def execution_lease(self, name: str, expected_tool: Tool) -> Iterator[Tool]:
        """Keep an exact binding stable while an invocation crosses its seam."""
        with self._lock:
            try:
                current = self._tools[name]
            except KeyError as exc:
                raise ToolNotFoundError(
                    "tool is not registered",
                    tool=name,
                    available=sorted(self._tools),
                ) from exc
            if current is not expected_tool:
                raise ToolError(
                    "tool registration changed before execution",
                    tool=name,
                    reason="execution_binding_changed",
                )
            self._active_execution_leases[name] = (
                self._active_execution_leases.get(name, 0) + 1
            )
        try:
            yield expected_tool
        finally:
            with self._lock:
                remaining = self._active_execution_leases.get(name, 0)
                if remaining <= 1:
                    self._active_execution_leases.pop(name, None)
                else:
                    self._active_execution_leases[name] = remaining - 1

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        with self._lock:
            return [self._tools[name].spec for name in sorted(self._tools)]

    def describe(self) -> list[dict[str, object]]:
        return [spec.to_dict() for spec in self.specs()]

    def by_capability(self, capability: Capability) -> list[Tool]:
        with self._lock:
            return [self._tools[name] for name in sorted(self._tools)
                    if capability in self._tools[name].capabilities]

    def read_only_tools(self) -> list[Tool]:
        with self._lock:
            return [self._tools[name] for name in sorted(self._tools)
                    if self._tools[name].spec.read_only]

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        with self._lock:
            snapshot = tuple(self._tools[name] for name in sorted(self._tools))
        return iter(snapshot)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        with self._lock:
            return name in self._tools
'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label} anchor count was {text.count(old)}, expected 1")
    return text.replace(old, new, 1)


Path("src/agentcore/tools/registry.py").write_text(REGISTRY, encoding="utf-8")

executor_path = Path("src/agentcore/tools/executor.py")
executor = executor_path.read_text(encoding="utf-8")
executor = replace_once(
    executor,
    """from agentcore.errors import (\n    AgentError,\n    PermissionDeniedError,\n    ToolValidationError,\n)""",
    """from agentcore.errors import (\n    AgentError,\n    PermissionDeniedError,\n    ToolError,\n    ToolValidationError,\n)""",
    "executor ToolError import",
)
executor = replace_once(
    executor,
    """        predecessor_guard = (\n            store.execution_guard(claimed.predecessor_action_id)\n            if first_risky_attempt\n            and claimed.predecessor_action_id is not None\n            else nullcontext()\n        )\n        try:\n            with predecessor_guard:\n""",
    """        predecessor_guard = (\n            store.execution_guard(claimed.predecessor_action_id)\n            if first_risky_attempt\n            and claimed.predecessor_action_id is not None\n            else nullcontext()\n        )\n        registry_lease = self.registry.execution_lease(tool_spec.name, tool)\n        try:\n            with registry_lease, predecessor_guard:\n""",
    "journaled effect registry lease",
)
executor = replace_once(
    executor,
    """        except (\n            _CurrentAuthorityBlocked,\n            _DuplicateRiskBlocked,\n            _ExternalContractBlocked,\n        ):\n            raise\n        except Exception as exc:\n""",
    """        except ToolError as exc:\n            raise _CurrentAuthorityBlocked(\n                \"action_adapter_invalid\",\n                \"The trusted tool registration changed before execution.\",\n            ) from exc\n        except (\n            _CurrentAuthorityBlocked,\n            _DuplicateRiskBlocked,\n            _ExternalContractBlocked,\n        ):\n            raise\n        except Exception as exc:\n""",
    "journaled effect lease failure",
)
executor = replace_once(
    executor,
    """            context = replace(\n                ActionReconciliationContext.from_record(current),\n                protected_payload=protected_payload,\n                protected_evidence=protected_evidence,\n            ).validate()\n            result = reconciler.probe(context)\n            if not isinstance(result, ActionReconciliationResult):\n""",
    """            context = replace(\n                ActionReconciliationContext.from_record(current),\n                protected_payload=protected_payload,\n                protected_evidence=protected_evidence,\n            ).validate()\n            with self.registry.execution_lease(tool_spec.name, tool):\n                current_tool_spec = tool.spec\n                current_adapter = self._action_adapters.resolve(\n                    tool, tool_spec=current_tool_spec)\n                current_reconciler = self._action_adapters.resolve_reconciler(\n                    tool, tool_spec=current_tool_spec)\n                if current_tool_spec != tool_spec or current_adapter is not adapter or (\n                        current_reconciler is not reconciler) or not (\n                            self._registration_matches_record(\n                                current, adapter.spec, current_tool_spec)):\n                    raise ValueError(\"reconciliation registration changed\")\n                result = reconciler.probe(context)\n            if not isinstance(result, ActionReconciliationResult):\n""",
    "reconciliation registry lease",
)
executor_path.write_text(executor, encoding="utf-8")

test_path = Path("tests/test_stage5_action_safety_integration.py")
tests = test_path.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    """from __future__ import annotations\n\nfrom datetime import datetime, timezone\nfrom typing import Any, Mapping\n""",
    """from __future__ import annotations\n\nimport threading\nfrom datetime import datetime, timezone\nfrom typing import Any, Mapping\n\nimport pytest\n""",
    "integration threading imports",
)
tests = replace_once(
    tests,
    "from agentcore.config.schema import PermissionConfig\n",
    "from agentcore.config.schema import PermissionConfig\nfrom agentcore.errors import ToolError\n",
    "integration ToolError import",
)
tests += r'''


def test_registry_execution_lease_fences_replacement_after_final_validation():
    executor, store, tool = _build()
    replacement = MutationTool()
    validated = threading.Event()
    resume = threading.Event()
    outcome = []
    failures = []
    original_validate = executor._validate_current_execution_authority

    def paused_validate(**kwargs):
        decision = original_validate(**kwargs)
        validated.set()
        assert resume.wait(5)
        return decision

    executor._validate_current_execution_authority = paused_validate

    def invoke_action():
        try:
            outcome.append(executor.invoke(
                "stage5_mutation",
                {"payload": "private"},
                action_context=_context(key="step:leased-registry"),
            ))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=invoke_action)
    worker.start()
    assert validated.wait(5)

    with pytest.raises(ToolError) as replace_error:
        executor.registry.register(replacement, replace=True)
    assert replace_error.value.details["reason"] == "execution_binding_active"

    with pytest.raises(ToolError) as unregister_error:
        executor.registry.unregister("stage5_mutation")
    assert unregister_error.value.details["reason"] == "execution_binding_active"
    assert executor.registry.get("stage5_mutation") is tool

    resume.set()
    worker.join(5)
    assert not worker.is_alive()
    assert failures == []
    assert len(outcome) == 1
    assert outcome[0].ok
    assert tool.runs == 1
    assert replacement.runs == 0

    executor.registry.unregister("stage5_mutation")
    assert executor.registry.register(replacement)
    assert executor.registry.get("stage5_mutation") is replacement


def test_registry_execution_lease_does_not_hold_metadata_lock_over_effect_io():
    executor, store, tool = _build()

    class OtherTool(MutationTool):
        @property
        def spec(self):
            return tool_spec(
                "stage5_other_mutation",
                "Unrelated registration used to prove registry lock release.",
                version=self.version,
                parameters=(ToolParameter("payload", ParamType.STRING),),
                capabilities=(Capability.FS_WRITE,),
                read_only=False,
            )

    other = OtherTool()
    original_run = tool.run
    registration_done = threading.Event()
    registration_failures = []

    def run_with_concurrent_registration(args, context):
        def register_unrelated():
            try:
                executor.registry.register(other)
                registration_done.set()
            except BaseException as exc:
                registration_failures.append(exc)
                registration_done.set()

        registrar = threading.Thread(target=register_unrelated)
        registrar.start()
        assert registration_done.wait(5)
        registrar.join(5)
        assert not registrar.is_alive()
        if registration_failures:
            raise registration_failures[0]
        return original_run(args, context)

    tool.run = run_with_concurrent_registration
    invocation = executor.invoke(
        "stage5_mutation",
        {"payload": "private"},
        action_context=_context(key="step:registry-lock-release"),
    )

    assert invocation.ok
    assert tool.runs == 1
    assert executor.registry.get("stage5_other_mutation") is other


def test_registry_execution_lease_reference_counts_same_tool():
    registry = ToolRegistry()
    tool = MutationTool()
    replacement = MutationTool()
    registry.register(tool)

    with registry.execution_lease("stage5_mutation", tool):
        with registry.execution_lease("stage5_mutation", tool):
            with pytest.raises(ToolError):
                registry.register(replacement, replace=True)
            assert registry.get("stage5_mutation") is tool
        with pytest.raises(ToolError):
            registry.unregister("stage5_mutation")

    registry.register(replacement, replace=True)
    assert registry.get("stage5_mutation") is replacement
'''
test_path.write_text(tests, encoding="utf-8")
