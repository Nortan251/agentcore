from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentcore.action_journal import (
    ActionAdapterRegistry,
    ActionAdapterSpec,
    ActionEffectClass,
    ActionJournalEligibility,
    EffectVerdict,
    InMemoryActionStore,
    NormalizedActionEffect,
    PreparedActionIntent,
)
from agentcore.config.schema import (
    AgentConfig,
    LoggingConfig,
    PermissionConfig,
    ToolConfig,
)
from agentcore.permissions.levels import PermissionLevel
from agentcore.permissions.levels import Capability
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.builtin import default_tools
from agentcore.tools.executor import ToolExecutor
from agentcore.tools.registry import ToolRegistry
from agentcore.util.clock import FixedClock


_TEST_ACTION_TIME = datetime(2026, 8, 25, tzinfo=timezone.utc)


class _TrustedTestActionAdapter:
    """Semantic contract for deterministic fake mutators in legacy tests.

    Each registered fake guarantees that its ``ToolResult`` is definitive;
    production code never receives or auto-discovers this adapter.
    """

    def __init__(self, tool, *, expose_bounded_text_result: bool) -> None:
        self._expose_bounded_text_result = expose_bounded_text_result
        capabilities = set(tool.spec.capabilities)
        eligibility = (
            ActionJournalEligibility.EXTERNAL_ACCOUNT_MUTATION
            if Capability.EMAIL_SEND in capabilities
            else ActionJournalEligibility.DURABLE_LOCAL_MUTATION
        )
        effect_class = (
            ActionEffectClass.NON_IDEMPOTENT_OR_EXTERNALLY_AMBIGUOUS
            if capabilities & {Capability.EMAIL_SEND, Capability.FS_DELETE}
            else ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE
        )
        self.spec = ActionAdapterSpec(
            tool_name=tool.spec.name,
            tool_version=tool.spec.version,
            action_kind=f"test.{tool.spec.name}.v1",
            eligibility=eligibility,
            effect_class=effect_class,
        )

    def prepare_intent(self, cleaned, context):
        return PreparedActionIntent(
            intent_digest="d" * 64,
            target_category="test.target",
        )

    def normalize_effect(self, result):
        if result.ok:
            summary = "The test effect was confirmed."
            if self._expose_bounded_text_result:
                if not isinstance(result.output, str):
                    raise TypeError("trusted test result must be text")
                summary = result.output[:512]
            return NormalizedActionEffect(
                verdict=EffectVerdict.CONFIRMED_APPLIED,
                result_class="test.applied",
                summary=summary,
            )
        return NormalizedActionEffect(
            verdict=EffectVerdict.CONFIRMED_NOT_APPLIED,
            result_class="test.not_applied",
            summary="The test effect was confirmed absent.",
        )


def journal_for_tools(
    tools, *, expose_bounded_text_result: bool = False,
) -> dict[str, object]:
    """Return explicit Stage-5 dependencies for test-only mutating tools.

    ``expose_bounded_text_result`` is an opt-in semantic promise made only by
    fakes whose output contract is already bounded, non-secret text.  It lets
    older deterministic-verifier tests receive useful normalized evidence
    without weakening the production executor's raw-result prohibition.
    """
    adapters = ActionAdapterRegistry()
    mutations = [tool for tool in tools if not tool.spec.read_only]
    for tool in mutations:
        adapters.register(
            tool,
            _TrustedTestActionAdapter(
                tool,
                expose_bounded_text_result=expose_bounded_text_result,
            ),
        )
    return {
        "action_store": InMemoryActionStore(
            clock=FixedClock(_TEST_ACTION_TIME)),
        "action_adapters": adapters,
    }


@pytest.fixture
def autonomous_config(tmp_path) -> AgentConfig:
    return AgentConfig(
        environment="test",
        workspace_dir=str(tmp_path),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        tools=ToolConfig(),
    ).validate()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(default_tools())
    return reg


@pytest.fixture
def executor(registry, tmp_path) -> ToolExecutor:
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    return ToolExecutor(registry, policy, workspace_dir=str(tmp_path))
