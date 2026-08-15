"""Configuration loading.

Precedence (lowest to highest):
    1. dataclass defaults
    2. TOML file
    3. AGENTCORE_* environment variables
    4. explicit overrides passed by the caller

Only variable *names* for secrets are accepted; secret values are read from the
process environment at use time (see :meth:`ModelConfig.resolve_api_key`).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

from agentcore.config.schema import (
    AgentConfig,
    ConversationConfig,
    RecoveryConfig,
    FeatureFlags,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    PermissionConfig,
    ToolConfig,
)
from agentcore.errors import ConfigError
from agentcore.permissions.levels import PermissionLevel

ENV_PREFIX = "AGENTCORE_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(value: str, key: str) -> bool:
    low = value.strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError("expected a boolean value", key=key, value=value)


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError("config file not found", path=str(path))
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - message passthrough
        raise ConfigError("config file is not valid TOML", path=str(path),
                          reason=str(exc)) from exc


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Map AGENTCORE_SECTION__FIELD=value into a nested dict.

    Examples::

        AGENTCORE_ENVIRONMENT=production
        AGENTCORE_LOGGING__LEVEL=DEBUG
        AGENTCORE_FEATURES__MEMORY=true
    """
    out: dict[str, Any] = {}
    for raw_key, raw_value in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        path = raw_key[len(ENV_PREFIX):].lower().split("__")
        cursor = out
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                raise ConfigError("conflicting environment override", key=raw_key)
        cursor[path[-1]] = raw_value
    return out


def _coerce(target_type: type, value: Any, key: str) -> Any:
    if isinstance(value, str):
        if target_type is bool:
            return _as_bool(value, key)
        if target_type is int:
            try:
                return int(value)
            except ValueError as exc:
                raise ConfigError("expected an integer", key=key, value=value) from exc
        if target_type is float:
            try:
                return float(value)
            except ValueError as exc:
                raise ConfigError("expected a number", key=key, value=value) from exc
        if target_type is tuple:
            return tuple(p.strip() for p in value.split(",") if p.strip())
    if target_type is tuple and isinstance(value, list):
        return tuple(value)
    return value


def _build_model(data: Mapping[str, Any]) -> ModelConfig:
    d = ModelConfig()
    return ModelConfig(
        provider=data.get("provider", d.provider),
        name=data.get("name", d.name),
        api_key_env=data.get("api_key_env", d.api_key_env),
        base_url=data.get("base_url", d.base_url),
        temperature=_coerce(float, data.get("temperature", d.temperature),
                            "model.temperature"),
        max_output_tokens=_coerce(int, data.get("max_output_tokens",
                                                d.max_output_tokens),
                                  "model.max_output_tokens"),
        timeout_seconds=_coerce(float, data.get("timeout_seconds", d.timeout_seconds),
                                "model.timeout_seconds"),
        max_retries=_coerce(int, data.get("max_retries", d.max_retries),
                            "model.max_retries"),
    )


def _build_tools(data: Mapping[str, Any]) -> ToolConfig:
    d = ToolConfig()
    return ToolConfig(
        enabled=_coerce(tuple, data.get("enabled", d.enabled), "tools.enabled"),
        disabled=_coerce(tuple, data.get("disabled", d.disabled), "tools.disabled"),
        default_timeout_seconds=_coerce(
            float, data.get("default_timeout_seconds", d.default_timeout_seconds),
            "tools.default_timeout_seconds"),
        max_retries=_coerce(int, data.get("max_retries", d.max_retries),
                            "tools.max_retries"),
        settings=dict(data.get("settings", {})),
    )


def _to_level(value: Any, key: str) -> PermissionLevel:
    if isinstance(value, PermissionLevel):
        return value
    try:
        return PermissionLevel(str(value).lower())
    except ValueError as exc:
        raise ConfigError(
            "unknown permission level", key=key, value=value,
            allowed=[lvl.value for lvl in PermissionLevel],
        ) from exc


def _build_permissions(data: Mapping[str, Any]) -> PermissionConfig:
    d = PermissionConfig()
    overrides = {
        cap: _to_level(lvl, f"permissions.capability_overrides.{cap}")
        for cap, lvl in data.get("capability_overrides", {}).items()
    }
    return PermissionConfig(
        level=_to_level(data.get("level", d.level), "permissions.level"),
        capability_overrides=overrides,
        blocked_capabilities=_coerce(
            tuple, data.get("blocked_capabilities", d.blocked_capabilities),
            "permissions.blocked_capabilities"),
    )


def _build_recovery(data: Mapping[str, Any]) -> RecoveryConfig:
    d = RecoveryConfig()
    return RecoveryConfig(
        max_attempts=_coerce(int, data.get("max_attempts", d.max_attempts),
                             "recovery.max_attempts"),
        base_delay_seconds=_coerce(
            float, data.get("base_delay_seconds", d.base_delay_seconds),
            "recovery.base_delay_seconds"),
        backoff_factor=_coerce(float, data.get("backoff_factor",
                                               d.backoff_factor),
                               "recovery.backoff_factor"),
        max_delay_seconds=_coerce(
            float, data.get("max_delay_seconds", d.max_delay_seconds),
            "recovery.max_delay_seconds"),
        retry_verification_failures=_coerce(
            bool, data.get("retry_verification_failures",
                           d.retry_verification_failures),
            "recovery.retry_verification_failures"),
        retry_unverified=_coerce(bool, data.get("retry_unverified",
                                                d.retry_unverified),
                                 "recovery.retry_unverified"),
        max_replans=_coerce(int, data.get("max_replans", d.max_replans),
                            "recovery.max_replans"),
        escalate_on_exhaustion=_coerce(
            bool, data.get("escalate_on_exhaustion", d.escalate_on_exhaustion),
            "recovery.escalate_on_exhaustion"),
    )


def _build_logging(data: Mapping[str, Any]) -> LoggingConfig:
    d = LoggingConfig()
    return LoggingConfig(
        level=str(data.get("level", d.level)).upper(),
        format=str(data.get("format", d.format)),
        file_path=data.get("file_path", d.file_path),
        include_timestamps=_coerce(
            bool, data.get("include_timestamps", d.include_timestamps),
            "logging.include_timestamps"),
        redact_keys=_coerce(tuple, data.get("redact_keys", d.redact_keys),
                            "logging.redact_keys"),
    )


def _build_memory(data: Mapping[str, Any]) -> MemoryConfig:
    d = MemoryConfig()
    return MemoryConfig(
        directory=data.get("directory", d.directory),
        max_records=_coerce(int, data.get("max_records", d.max_records),
                            "memory.max_records"),
        max_search_results=_coerce(
            int, data.get("max_search_results", d.max_search_results),
            "memory.max_search_results"),
        max_content_chars=_coerce(
            int, data.get("max_content_chars", d.max_content_chars),
            "memory.max_content_chars"),
        max_planning_memories=_coerce(
            int, data.get("max_planning_memories", d.max_planning_memories),
            "memory.max_planning_memories"),
        max_planning_memory_chars=_coerce(
            int, data.get("max_planning_memory_chars",
                          d.max_planning_memory_chars),
            "memory.max_planning_memory_chars"),
        capture_enabled=_coerce(
            bool, data.get("capture_enabled", d.capture_enabled),
            "memory.capture_enabled"),
        max_capture_candidates=_coerce(
            int, data.get("max_capture_candidates", d.max_capture_candidates),
            "memory.max_capture_candidates"),
        max_capture_chars=_coerce(
            int, data.get("max_capture_chars", d.max_capture_chars),
            "memory.max_capture_chars"),
        capture_inferred=_coerce(
            bool, data.get("capture_inferred", d.capture_inferred),
            "memory.capture_inferred"),
    )


def _build_conversation(data: Mapping[str, Any]) -> ConversationConfig:
    d = ConversationConfig()
    return ConversationConfig(
        directory=data.get("directory", d.directory),
        max_context_messages=_coerce(
            int, data.get("max_context_messages", d.max_context_messages),
            "conversation.max_context_messages"),
        max_context_chars=_coerce(
            int, data.get("max_context_chars", d.max_context_chars),
            "conversation.max_context_chars"),
        max_message_chars=_coerce(
            int, data.get("max_message_chars", d.max_message_chars),
            "conversation.max_message_chars"),
        max_session_messages=_coerce(
            int, data.get("max_session_messages", d.max_session_messages),
            "conversation.max_session_messages"),
        max_memories=_coerce(int, data.get("max_memories", d.max_memories),
                             "conversation.max_memories"),
        max_memory_chars=_coerce(
            int, data.get("max_memory_chars", d.max_memory_chars),
            "conversation.max_memory_chars"),
        tools_enabled=_coerce(bool, data.get("tools_enabled", d.tools_enabled),
                              "conversation.tools_enabled"),
        max_tool_rounds=_coerce(
            int, data.get("max_tool_rounds", d.max_tool_rounds),
            "conversation.max_tool_rounds"),
        max_tool_result_chars=_coerce(
            int, data.get("max_tool_result_chars", d.max_tool_result_chars),
            "conversation.max_tool_result_chars"),
    )


def _build_features(data: Mapping[str, Any]) -> FeatureFlags:
    d = FeatureFlags()
    return FeatureFlags(**{
        name: _coerce(bool, data.get(name, getattr(d, name)), f"features.{name}")
        for name in vars(d)
    })


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> AgentConfig:
    """Build a validated :class:`AgentConfig`.

    ``path`` is optional; with no file and no environment the defaults are a
    safe, fully offline OBSERVE-level configuration.
    """
    data: dict[str, Any] = {}
    if path is not None:
        data = _deep_merge(data, _read_toml(Path(path)))
    data = _deep_merge(data, _env_overrides(os.environ if env is None else env))
    if overrides:
        data = _deep_merge(data, overrides)

    defaults = AgentConfig()
    config = AgentConfig(
        environment=str(data.get("environment", defaults.environment)),
        agent_name=str(data.get("agent_name", defaults.agent_name)),
        workspace_dir=str(data.get("workspace_dir", defaults.workspace_dir)),
        model=_build_model(data.get("model", {})),
        tools=_build_tools(data.get("tools", {})),
        permissions=_build_permissions(data.get("permissions", {})),
        recovery=_build_recovery(data.get("recovery", {})),
        logging=_build_logging(data.get("logging", {})),
        memory=_build_memory(data.get("memory", {})),
        conversation=_build_conversation(data.get("conversation", {})),
        features=_build_features(data.get("features", {})),
    )
    return config.validate()
