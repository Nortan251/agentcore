from __future__ import annotations

import pytest

from agentcore.config.schema import PermissionConfig
from agentcore.permissions.levels import (
    DEFAULT_BLOCKED_CAPABILITIES,
    READ_ONLY_CAPABILITIES,
    Capability,
    PermissionLevel,
)
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


@pytest.mark.parametrize("level", list(PermissionLevel))
@pytest.mark.parametrize("opt_in", [False, True])
def test_bound_credential_reads_require_host_opt_in_at_every_level(level, opt_in):
    policy = PermissionPolicy(PermissionConfig(level=level),
                              allow_authenticated_reads=opt_in,
                              approval_gate=AlwaysApproveGate())
    capabilities = {Capability.NET_READ, Capability.CREDENTIAL_ACCESS}
    assert policy.check(capabilities, authenticated_read=True).allowed is opt_in
    # The same vocabulary still denies generic/raw access at any posture.
    assert policy.evaluate(Capability.CREDENTIAL_ACCESS).outcome is Outcome.DENY
    assert policy.check(capabilities).outcome is Outcome.DENY
    for blocked in (Capability.EMAIL_SEND, Capability.PAYMENT, Capability.SYSTEM_ADMIN):
        assert policy.check({blocked}).outcome is Outcome.DENY
        assert policy.check(capabilities | {blocked}, authenticated_read=True).outcome is Outcome.DENY
    assert DEFAULT_BLOCKED_CAPABILITIES == {
        Capability.EMAIL_SEND, Capability.PAYMENT,
        Capability.CREDENTIAL_ACCESS, Capability.SYSTEM_ADMIN,
    }
    assert Capability.CREDENTIAL_ACCESS not in READ_ONLY_CAPABILITIES


@pytest.mark.parametrize("blocked", [Capability.NET_READ, Capability.CREDENTIAL_ACCESS])
def test_explicit_blocks_beat_bound_credential_read_opt_in(blocked):
    policy = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.AUTONOMOUS, blocked_capabilities=(str(blocked),),
        capability_overrides={str(blocked): PermissionLevel.OBSERVE},
    ), allow_authenticated_reads=True, approval_gate=AlwaysApproveGate())
    decision = policy.check({Capability.NET_READ, Capability.CREDENTIAL_ACCESS},
                            authenticated_read=True)
    assert decision.outcome is Outcome.DENY and decision.capability is blocked


@pytest.mark.parametrize("capabilities", [
    (), (Capability.NET_READ,), (Capability.CREDENTIAL_ACCESS,),
    (Capability.NET_READ, Capability.CREDENTIAL_ACCESS, Capability.NET_WRITE),
    (Capability.NET_READ, Capability.CREDENTIAL_ACCESS, Capability.FS_WRITE),
])
def test_bound_credential_context_never_widens_other_capability_contracts(capabilities):
    policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.AUTONOMOUS),
                              allow_authenticated_reads=True)
    assert policy.check(capabilities, authenticated_read=True).outcome is Outcome.DENY


def test_overrides_and_human_approval_cannot_create_credential_read_authority():
    capabilities = {Capability.NET_READ, Capability.CREDENTIAL_ACCESS}
    without_opt_in = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.AUTONOMOUS,
        capability_overrides={"credential.access": PermissionLevel.OBSERVE},
    ), approval_gate=AlwaysApproveGate())
    assert not without_opt_in.check(capabilities, authenticated_read=True).allowed
    restricted = PermissionPolicy(PermissionConfig(
        level=PermissionLevel.CONFIRM,
        capability_overrides={"credential.access": PermissionLevel.AUTONOMOUS},
    ), allow_authenticated_reads=True, approval_gate=AlwaysApproveGate())
    assert restricted.check(capabilities, authenticated_read=True).outcome is Outcome.DENY


def test_published_permission_snapshot_cannot_change_through_configuration_aliases():
    overrides = {"credential.access": PermissionLevel.AUTONOMOUS}
    config = PermissionConfig(capability_overrides=overrides)
    policy = PermissionPolicy(config, allow_authenticated_reads=True)
    overrides["credential.access"] = PermissionLevel.OBSERVE
    capabilities = {Capability.NET_READ, Capability.CREDENTIAL_ACCESS}
    assert not policy.check(capabilities, authenticated_read=True).allowed
    with pytest.raises(TypeError):
        policy.config.capability_overrides["credential.access"] = PermissionLevel.OBSERVE
    with pytest.raises(AttributeError):
        policy.config = PermissionConfig()
    with pytest.raises(AttributeError):
        policy.allow_authenticated_reads = False
    # Configuration used by AgentConfig remains an ordinary serializable value.
    from dataclasses import asdict
    assert asdict(config)["capability_overrides"] == overrides


@pytest.mark.parametrize("flag", [1, "true", object()])
def test_credential_opt_in_requires_an_explicit_boolean(flag):
    with pytest.raises(TypeError):
        PermissionPolicy(allow_authenticated_reads=flag)
    with pytest.raises(TypeError):
        PermissionPolicy().check({Capability.NET_READ}, authenticated_read=flag)
