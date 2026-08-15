from __future__ import annotations

import pytest

from agentcore.config.loader import load_config
from agentcore.config.schema import AgentConfig, ModelConfig, ToolConfig
from agentcore.errors import ConfigError
from agentcore.permissions.levels import PermissionLevel


def test_defaults_are_safe():
    cfg = AgentConfig().validate()
    assert cfg.permissions.level is PermissionLevel.OBSERVE
    assert cfg.features.enabled_flags() == ()
    assert cfg.model.provider == "none"


def test_load_from_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        """
environment = "test"
agent_name = "tester"

[model]
provider = "openai"
name = "gpt-4o"
api_key_env = "MY_KEY"

[permissions]
level = "confirm"
blocked_capabilities = ["net.write"]

[permissions.capability_overrides]
"fs.write" = "propose"

[logging]
level = "debug"
format = "text"

[features]
memory = true
""",
        encoding="utf-8",
    )
    cfg = load_config(path, env={})
    assert cfg.agent_name == "tester"
    assert cfg.model.name == "gpt-4o"
    assert cfg.permissions.level is PermissionLevel.CONFIRM
    assert cfg.permissions.capability_overrides["fs.write"] is PermissionLevel.PROPOSE
    assert cfg.permissions.blocked_capabilities == ("net.write",)
    assert cfg.logging.level == "DEBUG"
    assert cfg.features.memory is True


def test_env_overrides_beat_file(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text('environment = "development"\n[logging]\nlevel = "INFO"\n',
                    encoding="utf-8")
    cfg = load_config(path, env={
        "AGENTCORE_ENVIRONMENT": "production",
        "AGENTCORE_LOGGING__LEVEL": "ERROR",
        "AGENTCORE_FEATURES__PLANNING": "true",
    })
    assert cfg.environment == "production"
    assert cfg.logging.level == "ERROR"
    assert cfg.features.planning is True


def test_explicit_overrides_beat_env():
    cfg = load_config(None, env={"AGENTCORE_ENVIRONMENT": "production"},
                      overrides={"environment": "staging"})
    assert cfg.environment == "staging"


def test_secrets_read_from_environment_not_config(monkeypatch):
    model = ModelConfig(api_key_env="SECRET_KEY_VAR")
    assert model.resolve_api_key() is None
    monkeypatch.setenv("SECRET_KEY_VAR", "s3cret")
    assert model.resolve_api_key() == "s3cret"
    # the literal secret is never part of the config object
    assert "s3cret" not in repr(model)


def test_invalid_values_are_rejected():
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_ENVIRONMENT": "nonsense"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_LOGGING__LEVEL": "LOUD"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_PERMISSIONS__LEVEL": "godmode"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_FEATURES__MEMORY": "maybe"})


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml", env={})


def test_tool_allow_and_deny_lists():
    cfg = ToolConfig(enabled=("a", "b"), disabled=("b",))
    assert cfg.is_allowed("a")
    assert not cfg.is_allowed("b")
    assert not cfg.is_allowed("c")
    assert ToolConfig().is_allowed("anything")


# -- recovery configuration (Task 5) ----------------------------------------

def test_recovery_defaults():
    from agentcore.recovery.config import RecoveryConfig

    cfg = AgentConfig().validate()
    assert cfg.features.recovery is False          # off by default
    assert cfg.recovery == RecoveryConfig()
    assert cfg.recovery.max_attempts == 3


def test_recovery_settings_load_from_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        """
[features]
recovery = true

[recovery]
max_attempts = 5
base_delay_seconds = 1.5
backoff_factor = 3.0
max_delay_seconds = 12.0
retry_verification_failures = false
retry_unverified = true
max_replans = 2
escalate_on_exhaustion = false
""", encoding="utf-8")
    cfg = load_config(path, env={})
    assert cfg.features.recovery is True
    assert cfg.recovery.max_attempts == 5
    assert cfg.recovery.base_delay_seconds == 1.5
    assert cfg.recovery.backoff_factor == 3.0
    assert cfg.recovery.retry_verification_failures is False
    assert cfg.recovery.retry_unverified is True
    assert cfg.recovery.max_replans == 2
    assert cfg.recovery.escalate_on_exhaustion is False


def test_recovery_env_overrides(tmp_path):
    cfg = load_config(None, env={
        "AGENTCORE_FEATURES__RECOVERY": "true",
        "AGENTCORE_RECOVERY__MAX_ATTEMPTS": "7",
        "AGENTCORE_RECOVERY__RETRY_UNVERIFIED": "yes",
    })
    assert cfg.features.recovery is True
    assert cfg.recovery.max_attempts == 7
    assert cfg.recovery.retry_unverified is True


def test_invalid_recovery_settings_rejected():
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_RECOVERY__MAX_ATTEMPTS": "0"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_RECOVERY__BACKOFF_FACTOR": "0.5"})
    with pytest.raises(ConfigError):
        load_config(None, env={"AGENTCORE_RECOVERY__MAX_REPLANS": "-1"})
