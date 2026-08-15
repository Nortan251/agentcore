from __future__ import annotations

import pytest

from agentcore.config.schema import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import (
    AlwaysApproveGate,
    DenyAllApprovalGate,
    Outcome,
    PermissionPolicy,
)


def test_levels_are_ordered():
    assert PermissionLevel.OBSERVE < PermissionLevel.PROPOSE
    assert PermissionLevel.PROPOSE < PermissionLevel.CONFIRM
    assert PermissionLevel.CONFIRM < PermissionLevel.AUTONOMOUS
    assert PermissionLevel.AUTONOMOUS >= PermissionLevel.OBSERVE


def test_observe_allows_reads_and_denies_writes():
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.OBSERVE))
    assert policy.evaluate(Capability.FS_READ).outcome is Outcome.ALLOW
    assert policy.evaluate(Capability.FS_WRITE).outcome is Outcome.DENY


def test_propose_still_denies_mutations():
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.PROPOSE))
    assert policy.evaluate(Capability.FS_WRITE).outcome is Outcome.DENY


@pytest.mark.parametrize("capability", [
    Capability.FS_WRITE,
    Capability.FS_DELETE,
    Capability.PROCESS_SPAWN,
    Capability.NET_WRITE,
    Capability.APP_CONTROL,
    Capability.INPUT_CONTROL,
])
def test_confirm_never_lets_a_mutation_run_unattended(capability):
    """Regression: CONFIRM must gate *every* mutating capability.

    A default requirement of CONFIRM would make ``level >= required`` true and
    silently skip the approval gate, which would defeat the level entirely.
    """
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.CONFIRM))
    assert policy.evaluate(capability).outcome is Outcome.REQUIRE_APPROVAL


def test_confirm_level_denies_mutation_when_no_approver(tmp_path):
    """End-to-end: CONFIRM + default (deny-all) gate blocks the tool call."""
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.CONFIRM))
    assert policy.check([Capability.FS_WRITE]).outcome is Outcome.DENY


def test_approval_gate_resolves_pending_decisions():
    cfg = PermissionConfig(level=PermissionLevel.CONFIRM)
    denied = PermissionPolicy(cfg, approval_gate=DenyAllApprovalGate())
    approved = PermissionPolicy(cfg, approval_gate=AlwaysApproveGate())
    caps = [Capability.FS_DELETE]
    assert denied.check(caps).outcome is Outcome.DENY
    assert approved.check(caps).outcome is Outcome.ALLOW


def test_default_gate_denies_when_no_human_present():
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.CONFIRM))
    assert policy.check([Capability.FS_DELETE]).outcome is Outcome.DENY


def test_autonomous_allows_mutations_but_not_blocked_capabilities():
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    assert policy.evaluate(Capability.FS_WRITE).outcome is Outcome.ALLOW
    assert policy.evaluate(Capability.INPUT_CONTROL).outcome is Outcome.ALLOW
    for blocked in (Capability.EMAIL_SEND, Capability.PAYMENT,
                    Capability.CREDENTIAL_ACCESS, Capability.SYSTEM_ADMIN):
        assert policy.evaluate(blocked).outcome is Outcome.DENY


def test_config_can_block_additional_capabilities():
    policy = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.AUTONOMOUS,
        blocked_capabilities=("process.spawn",)))
    assert policy.evaluate(Capability.PROCESS_SPAWN).outcome is Outcome.DENY


def test_capability_override_raises_requirement():
    policy = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.CONFIRM,
        capability_overrides={"fs.write": PermissionLevel.AUTONOMOUS}))
    assert policy.required_level(Capability.FS_WRITE) is PermissionLevel.AUTONOMOUS
    assert policy.evaluate(Capability.FS_WRITE).outcome is Outcome.REQUIRE_APPROVAL


def test_most_restrictive_capability_wins():
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS))
    decision = policy.check([Capability.FS_READ, Capability.PAYMENT])
    assert decision.outcome is Outcome.DENY
    assert decision.capability is Capability.PAYMENT


def test_no_capabilities_is_allowed():
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.OBSERVE))
    assert policy.check([]).allowed


@pytest.mark.parametrize("level", list(PermissionLevel))
def test_read_only_capabilities_allowed_at_every_level(level):
    policy = PermissionPolicy(PermissionConfig(level=level))
    assert policy.evaluate(Capability.FS_READ).outcome is Outcome.ALLOW
