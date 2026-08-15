"""Structured logging built on the stdlib.

Every record is a flat event with a stable ``event`` name plus arbitrary
key/value context, which is what makes long agent runs debuggable: you can
filter a whole run by ``run_id`` or a single tool by ``tool``.

Context is inherited — ``logger.bind(run_id=...)`` returns a child logger that
stamps every subsequent event, so call sites don't repeat identifiers.
Values whose key matches the configured redaction list are replaced with
``"***"`` before they ever reach a handler.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agentcore.config.schema import LoggingConfig

_CONTEXT_KEY = "agent_context"
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message", "asctime", _CONTEXT_KEY,
}


def _redact(value: Any, redact_keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 6:
        return "<max-depth>"
    if isinstance(value, Mapping):
        return {
            k: ("***" if str(k).lower() in redact_keys
                else _redact(v, redact_keys, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, redact_keys, depth + 1) for v in value]
    return value


def redact(value: Any, redact_keys: tuple[str, ...] | None = None) -> Any:
    """Public wrapper over the log redaction rules.

    Exposed so other layers (e.g. verification, which puts execution context
    into model prompts) can apply exactly the same secret-masking as logging
    instead of maintaining a second, divergent list.
    """
    keys = redact_keys if redact_keys is not None else LoggingConfig().redact_keys
    return _redact(value, tuple(k.lower() for k in keys))


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    return str(value)


class StructuredFormatter(logging.Formatter):
    def __init__(self, config: LoggingConfig) -> None:
        super().__init__()
        self.config = config
        self._redact_keys = tuple(k.lower() for k in config.redact_keys)

    def _payload(self, record: logging.LogRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if self.config.include_timestamps:
            payload["ts"] = datetime.fromtimestamp(
                record.created, tz=timezone.utc).isoformat()

        context: dict[str, Any] = dict(getattr(record, _CONTEXT_KEY, {}) or {})
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                context[key] = value
        if context:
            payload.update(_jsonable(_redact(context, self._redact_keys)))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return payload

    def format(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        if self.config.format == "json":
            return json.dumps(payload, default=str, sort_keys=True)
        head = " ".join(
            str(payload.pop(k)) for k in ("ts", "level", "event") if k in payload
        )
        rest = " ".join(f"{k}={payload[k]!r}" for k in sorted(payload))
        return f"{head} {rest}".strip()


class StructuredLogger:
    """Thin wrapper adding bound context and event-style call signatures."""

    def __init__(self, logger: logging.Logger,
                 context: Mapping[str, Any] | None = None) -> None:
        self._logger = logger
        self._context: dict[str, Any] = dict(context or {})

    @property
    def context(self) -> dict[str, Any]:
        return dict(self._context)

    def bind(self, **context: Any) -> StructuredLogger:
        """Return a child logger carrying additional permanent context."""
        return StructuredLogger(self._logger, {**self._context, **context})

    def _log(self, level: int, event: str, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        self._logger.log(level, event,
                         extra={_CONTEXT_KEY: {**self._context, **fields}})

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        if self._logger.isEnabledFor(logging.ERROR):
            self._logger.error(event, exc_info=True,
                               extra={_CONTEXT_KEY: {**self._context, **fields}})


_ROOT_NAME = "agentcore"
_configured = False


def configure_logging(config: LoggingConfig | None = None) -> None:
    """Install handlers on the ``agentcore`` logger. Safe to call repeatedly."""
    global _configured
    cfg = config or LoggingConfig()
    cfg.validate()

    logger = logging.getLogger(_ROOT_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = StructuredFormatter(cfg)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if cfg.file_path:
        path = Path(cfg.file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.setLevel(cfg.level.upper())
    logger.propagate = False
    _configured = True


def get_logger(name: str = "", **context: Any) -> StructuredLogger:
    """Fetch a structured logger under the ``agentcore`` namespace."""
    if not _configured:
        configure_logging()
    full = f"{_ROOT_NAME}.{name}" if name else _ROOT_NAME
    return StructuredLogger(logging.getLogger(full), context)
