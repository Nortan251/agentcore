from __future__ import annotations

import json
import logging

from agentcore.config.schema import LoggingConfig
from agentcore.observability.logging import (
    StructuredFormatter,
    configure_logging,
    get_logger,
)


def _record(**context):
    record = logging.LogRecord("agentcore.test", logging.INFO, "f", 1,
                               "tool.invoked", (), None)
    record.agent_context = context
    return record


def test_json_format_contains_event_and_context():
    formatter = StructuredFormatter(LoggingConfig(format="json"))
    payload = json.loads(formatter.format(_record(run_id="r1", tool="echo")))
    assert payload["event"] == "tool.invoked"
    assert payload["level"] == "INFO"
    assert payload["run_id"] == "r1"
    assert payload["tool"] == "echo"
    assert "ts" in payload


def test_secrets_are_redacted():
    formatter = StructuredFormatter(LoggingConfig(format="json"))
    payload = json.loads(formatter.format(
        _record(api_key="abc123", nested={"token": "xyz", "safe": "ok"})))
    assert payload["api_key"] == "***"
    assert payload["nested"]["token"] == "***"
    assert payload["nested"]["safe"] == "ok"


def test_text_format_is_readable():
    formatter = StructuredFormatter(LoggingConfig(format="text"))
    line = formatter.format(_record(run_id="r1"))
    assert "tool.invoked" in line
    assert "run_id='r1'" in line


def test_timestamps_can_be_disabled():
    formatter = StructuredFormatter(LoggingConfig(include_timestamps=False))
    assert "ts" not in json.loads(formatter.format(_record()))


def test_bind_accumulates_context_without_mutating_parent():
    logger = get_logger("test.bind", a=1)
    child = logger.bind(b=2)
    assert logger.context == {"a": 1}
    assert child.context == {"a": 1, "b": 2}


def test_logs_are_written_to_file(tmp_path):
    path = tmp_path / "logs" / "agent.log"
    configure_logging(LoggingConfig(level="DEBUG", format="json",
                                    file_path=str(path)))
    get_logger("test.file", run_id="r9").info("task.started", task_id="t1")
    logging.getLogger("agentcore").handlers[-1].flush()

    lines = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert lines[-1]["event"] == "task.started"
    assert lines[-1]["run_id"] == "r9"
    assert lines[-1]["task_id"] == "t1"
    configure_logging(LoggingConfig(level="CRITICAL"))


def test_level_filtering(tmp_path):
    path = tmp_path / "agent.log"
    configure_logging(LoggingConfig(level="WARNING", file_path=str(path)))
    log = get_logger("test.level")
    log.debug("ignored.event")
    log.warning("kept.event")
    logging.getLogger("agentcore").handlers[-1].flush()

    events = [json.loads(line)["event"] for line in path.read_text().splitlines()
              if line]
    assert "ignored.event" not in events
    assert "kept.event" in events
    configure_logging(LoggingConfig(level="CRITICAL"))


def test_unserialisable_values_do_not_break_logging():
    formatter = StructuredFormatter(LoggingConfig(format="json"))
    payload = json.loads(formatter.format(_record(obj=object())))
    assert isinstance(payload["obj"], str)
