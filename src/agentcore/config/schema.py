"""Configuration schema.

Plain dataclasses, no third-party dependency, validated explicitly. Secrets are
never stored here as literals: model credentials are referenced by the *name*
of an environment variable (``api_key_env``) which is resolved at call time by
whichever provider adapter needs it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcore.errors import ConfigError
# Re-exported so callers see a single configuration surface; defined in their
# own packages to keep the layering acyclic.
from agentcore.conversation.config import ConversationConfig  # noqa: E402
from agentcore.fs.config import FilesystemConfig  # noqa: E402 (leaf module)
from agentcore.memory.config import MemoryConfig  # noqa: E402 (leaf module)
from agentcore.permissions.config import PermissionConfig
from agentcore.recovery.config import RecoveryConfig  # noqa: E402 (leaf module; see recovery/__init__)


@dataclass(frozen=True)
class ModelConfig:
    """Which LLM provider/model the reasoning layer should eventually use."""

    provider: str = "none"
    name: str = "none"
    api_key_env: str | None = None
    #: Endpoint root, e.g. ``https://api.openai.com/v1``. Optional: the factory
    #: supplies a well-known default for recognised providers.
    base_url: str | None = None
    temperature: float = 0.2
    max_output_tokens: int = 4096
    timeout_seconds: float = 60.0
    #: Attempts per request, including the first. Retries apply only to
    #: transient failures (timeout, connection, rate limit, 5xx).
    max_retries: int = 3
    #: Optional chat-template setting; None preserves the provider's default.
    enable_thinking: bool | None = None

    def resolve_api_key(self) -> str | None:
        """Read the secret from the environment. Never logged, never persisted."""
        if not self.api_key_env:
            return None
        return os.environ.get(self.api_key_env)

    def validate(self) -> None:
        if not 0.0 <= self.temperature <= 2.0:
            raise ConfigError("model.temperature must be within [0, 2]",
                              value=self.temperature)
        if self.max_output_tokens <= 0:
            raise ConfigError("model.max_output_tokens must be positive",
                              value=self.max_output_tokens)
        if self.timeout_seconds <= 0:
            raise ConfigError("model.timeout_seconds must be positive",
                              value=self.timeout_seconds)
        if self.max_retries < 1:
            raise ConfigError("model.max_retries must be at least 1",
                              value=self.max_retries)
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ConfigError("model.enable_thinking must be a boolean when set")
        if self.base_url is not None and not str(self.base_url).startswith(
                ("http://", "https://")):
            raise ConfigError("model.base_url must start with http:// or https://",
                              value=self.base_url)


@dataclass(frozen=True)
class ToolConfig:
    """Tool-layer settings.

    ``enabled`` is an allowlist; empty means "every registered tool".
    ``disabled`` always wins over ``enabled``.
    """

    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    default_timeout_seconds: float = 30.0
    max_retries: int = 1
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def is_allowed(self, tool_name: str) -> bool:
        if tool_name in self.disabled:
            return False
        return not self.enabled or tool_name in self.enabled

    def for_tool(self, tool_name: str) -> dict[str, Any]:
        return dict(self.settings.get(tool_name, {}))

    def validate(self) -> None:
        if self.default_timeout_seconds <= 0:
            raise ConfigError("tools.default_timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ConfigError("tools.max_retries must be >= 0")


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"  # "json" | "text"
    file_path: str | None = None
    include_timestamps: bool = True
    redact_keys: tuple[str, ...] = (
        "api_key", "apikey", "token", "password", "secret", "authorization",
    )

    def validate(self) -> None:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.level.upper() not in valid:
            raise ConfigError("logging.level invalid", value=self.level,
                              allowed=sorted(valid))
        if self.format not in {"json", "text"}:
            raise ConfigError("logging.format must be 'json' or 'text'",
                              value=self.format)


@dataclass(frozen=True)
class FeatureFlags:
    """Off-by-default switches for capability layers landing in later tasks."""

    memory: bool = False
    planning: bool = False
    verification: bool = False
    recovery: bool = False
    computer_control: bool = False
    browser: bool = False

    def enabled_flags(self) -> tuple[str, ...]:
        return tuple(k for k, v in vars(self).items() if v)


@dataclass(frozen=True)
class AgentConfig:
    """Root configuration object passed into the agent at construction."""

    environment: str = "development"
    agent_name: str = "agentcore"
    workspace_dir: str = "./.agent_workspace"
    model: ModelConfig = field(default_factory=ModelConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    conversation: ConversationConfig = field(
        default_factory=ConversationConfig)
    filesystem: FilesystemConfig = field(default_factory=FilesystemConfig)
    features: FeatureFlags = field(default_factory=FeatureFlags)

    def validate(self) -> AgentConfig:
        if self.environment not in {"development", "test", "staging", "production"}:
            raise ConfigError("environment invalid", value=self.environment)
        self.model.validate()
        self.tools.validate()
        self.permissions.validate()
        self.recovery.validate()
        self.logging.validate()
        self.memory.validate()
        self.conversation.validate()
        self.filesystem.validate()
        # Fail here rather than at first filesystem use: an unusable workspace
        # is a configuration error, and the canonical root is security-relevant.
        self.canonical_workspace()
        return self

    def canonical_workspace(self) -> Path:
        """The one absolute, symlink-free workspace root for this config.

        Every layer that needs the workspace should call this rather than
        interpreting ``workspace_dir`` itself, so the root is defined once.
        """
        from agentcore.fs.paths import canonical_workspace

        return canonical_workspace(self.workspace_dir)
