"""Host-specific diagnostics omit all data-bearing fields and tracebacks."""

from agentcore.observability.logging import get_logger


class HostLogger:
    # Retain event ordering and numeric counts. Even read arguments, arbitrary
    # exception type names and error details can contain mutation content.
    def __init__(self, logger=None):
        self._logger = logger or get_logger("workspace.host")

    def bind(self, **fields):
        return self

    def _emit(self, method, event, fields):
        safe = {key: value for key, value in fields.items()
                if type(value) in (bool, int, float) and key in {
                    "duration_ms", "chars", "messages", "argument_count",
                    "result_chars", "bytes_read", "truncated", "rounds",
                    "tools_executed", "memories_used"}}
        getattr(self._logger, method)(event, **safe)

    def debug(self, event, **fields):
        self._emit("debug", event, fields)

    def info(self, event, **fields):
        self._emit("info", event, fields)

    def warning(self, event, **fields):
        self._emit("warning", event, fields)

    def error(self, event, **fields):
        self._emit("error", event, fields)

    def exception(self, event, **fields):
        self.error(event, **fields)
