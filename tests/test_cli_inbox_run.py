"""`agentcore inbox <run_id>`: single-run drill-down.

The operator loop is scan -> choose -> inspect -> decide. This command is the
"inspect" step: it scopes the inbox to one named run, implies
`--action-details`, and loads exactly that one state file.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import pytest

from agentcore.cli import (
    INBOX_EMPTY,
    INBOX_RUN_CLEAR,
    NOT_AVAILABLE,
    main,
)
from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.recovery.config import RecoveryConfig
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-single-run-must-not-leak"
ARG_VALUE = "supersecret-argument-value"


# -- fixtures ---------------------------------------------------------------

class WriteTool(Tool):
    @property
    def spec(self):
        return tool_spec(
            "write_note", "Write a note.",
            parameters=[ToolParameter("text", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        return ToolResult.success("wrote")


class PassVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="V", detail="ok")


class WritePlanner:
    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="write the note",
                                 tool_name="write_note",
                                 tool_args={"text": ARG_VALUE}))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class HumanStepsPlanner:
    """N tool-less steps -> N pending actions."""

    def __init__(self, count):
        self.count = count

    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        for i in range(self.count):
            task.add_subtask(Subtask(description=f"human step {i + 1}"))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


def config_for(tmp_path, *, level=PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=0, base_delay_seconds=0.0,
                                max_attempts=1, escalate_on_exhaustion=False),
        features=FeatureFlags(recovery=True),
    ).validate()


def seed(tmp_path, objective, *, planner, level=PermissionLevel.AUTONOMOUS):
    agent = Agent(config_for(tmp_path, level=level), AgentDependencies(
        tools=[WriteTool()], planner=planner, verifier=PassVerifier(),
        state_store=JsonFileStateStore(tmp_path / "runs")))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.005)
    return agent.state.run_id


def cli(tmp_path, capsys, *argv, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), *argv])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def waiting_run(tmp_path, objective="needs a human", count=1):
    return seed(tmp_path, objective, planner=HumanStepsPlanner(count))


def clean_run(tmp_path, objective="nothing pending"):
    return seed(tmp_path, objective, planner=WritePlanner())


def corrupt_run(tmp_path, name="run_corrupt0001"):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def write_raw(tmp_path, name, actions, *, objective="legacy run"):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text(json.dumps({
        "run_id": name, "phase": "executing",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "current_task": {"objective": objective, "id": "t",
                         "status": "blocked", "subtasks": []},
        "pending_actions": actions,
    }), encoding="utf-8")
    return name


def stored_actions(tmp_path, run_id):
    return JsonFileStateStore(tmp_path / "runs").load(run_id).pending_actions


def action_ids_in(text: str) -> list[str]:
    return re.findall(r"^\s*ACTION \d+: (\S+)", text, flags=re.MULTILINE)


def digest(tmp_path) -> dict[str, tuple[str, int]]:
    return {p.name: (hashlib.md5(p.read_bytes()).hexdigest(),
                     p.stat().st_mtime_ns)
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Basic single-run display
# ===========================================================================

def test_shows_the_requested_run(tmp_path, capsys):
    run_id = waiting_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert run_id in out
    assert "1 run awaiting an operator decision" in out


def test_only_the_requested_run_appears(tmp_path, capsys):
    target = waiting_run(tmp_path, "the run I want", count=2)
    other = waiting_run(tmp_path, "an unrelated waiting run")
    done = clean_run(tmp_path, "a finished run")

    out = cli(tmp_path, capsys, "inbox", target).out
    assert target in out
    assert other not in out
    assert done not in out
    assert "an unrelated waiting run" not in out


def test_pending_count_shown(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=3)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    row = next(line for line in out.splitlines() if line.startswith(run_id))
    assert "3" in row.split()
    assert len(action_ids_in(out)) == 3


def test_decide_hint_names_the_run(tmp_path, capsys):
    run_id = waiting_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert f"agentcore approve|reject {run_id} --action-id" in out


# ===========================================================================
# Details are implied
# ===========================================================================

def test_details_shown_without_any_flag(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert "ACTION 1:" in out and "ACTION 2:" in out
    assert "Description: human step 1" in out
    assert "Description: human step 2" in out
    assert "Tool:" in out and "Reason:" in out


def test_all_action_fields_present(tmp_path, capsys):
    write_raw(tmp_path, "run_fields0001", [
        {"id": "act_full", "description": "publish the report",
         "tool_name": "filesystem_write", "reason": "needs approval",
         "subtask_id": "sub_42"}])
    out = cli(tmp_path, capsys, "inbox", "run_fields0001").out
    assert "ACTION 1: act_full" in out
    assert "Tool:        filesystem_write" in out
    assert "Description: publish the report" in out
    assert "Reason:      needs approval" in out
    assert "Subtask:     sub_42" in out


def test_redundant_action_details_flag_is_harmless(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    plain = cli(tmp_path, capsys, "inbox", run_id).out
    explicit = cli(tmp_path, capsys, "inbox", run_id, "--action-details").out
    assert plain == explicit


def test_matches_the_full_inbox_view_of_the_same_run(tmp_path, capsys):
    """The two views must never disagree about one run."""
    run_id = waiting_run(tmp_path, count=2)
    waiting_run(tmp_path, "another waiting run")

    single = cli(tmp_path, capsys, "inbox", run_id).out
    full = cli(tmp_path, capsys, "inbox", "--action-details", "--all").out
    assert action_ids_in(single) == [a.id for a in
                                     stored_actions(tmp_path, run_id)]
    for action_id in action_ids_in(single):
        assert action_id in full


def test_missing_optional_fields_use_the_shared_fallback(tmp_path, capsys):
    write_raw(tmp_path, "run_sparse0001", [
        {"id": "act_sparse", "description": "just a description"}])
    out = cli(tmp_path, capsys, "inbox", "run_sparse0001").out
    assert f"Tool:        {NOT_AVAILABLE}" in out
    assert f"Reason:      {NOT_AVAILABLE}" in out


# ===========================================================================
# Run with no pending actions
# ===========================================================================

def test_clean_run_gets_a_distinct_message(tmp_path, capsys):
    run_id = clean_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert out.strip() == INBOX_RUN_CLEAR.format(run_id=run_id)


def test_clean_run_message_is_not_the_empty_inbox_message(tmp_path, capsys):
    run_id = clean_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert INBOX_EMPTY not in out
    assert run_id in out


def test_clean_run_exits_zero(tmp_path, capsys):
    run_id = clean_run(tmp_path)
    cli(tmp_path, capsys, "inbox", run_id, expect=0)


def test_clean_run_json_is_an_empty_list(tmp_path, capsys):
    run_id = clean_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", run_id, "--json").out
    assert json.loads(out) == []


def test_fully_resolved_run_becomes_clean(tmp_path, capsys):
    run_id = waiting_run(tmp_path)
    action_id = action_ids_in(cli(tmp_path, capsys, "inbox", run_id).out)[0]
    cli(tmp_path, capsys, "approve", run_id, "--action-id", action_id)
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert out.strip() == INBOX_RUN_CLEAR.format(run_id=run_id)


# ===========================================================================
# Unknown / unreadable runs
# ===========================================================================

def test_unknown_run_errors_cleanly(tmp_path, capsys):
    waiting_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", "run_does_not_exist", expect=2)
    assert "cannot read run 'run_does_not_exist'" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_unknown_run_does_not_dump_every_id(tmp_path, capsys):
    ids = [waiting_run(tmp_path, f"waiting {i}") for i in range(6)]
    captured = cli(tmp_path, capsys, "inbox", "run_nope", expect=2)
    assert not any(run_id in captured.err for run_id in ids)
    assert "agentcore inbox" in captured.err


def test_corrupt_run_errors_cleanly(tmp_path, capsys):
    name = corrupt_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", name, expect=2)
    assert f"cannot read run '{name}'" in captured.err
    assert "corrupt" in captured.err
    assert "Traceback" not in captured.err


def test_corrupt_run_is_not_claimed_to_be_pending(tmp_path, capsys):
    name = corrupt_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", name, expect=2)
    assert "awaiting an operator decision" not in captured.out
    assert "ACTION" not in captured.out


def test_invalid_run_id_is_refused(tmp_path, capsys):
    """The store rejects path-like ids; the CLI must report, not traceback."""
    waiting_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", "../escape", expect=2)
    assert "invalid run id" in captured.err
    assert "Traceback" not in captured.err


def test_missing_runs_directory(tmp_path, capsys):
    captured = cli(tmp_path, capsys, "inbox", "run_anything", expect=2)
    assert "no run directory" in captured.err


def test_unknown_run_writes_nothing(tmp_path, capsys):
    waiting_run(tmp_path)
    before = digest(tmp_path)
    cli(tmp_path, capsys, "inbox", "run_nope", expect=2)
    cli(tmp_path, capsys, "inbox", "../escape", expect=2)
    assert digest(tmp_path) == before


# ===========================================================================
# Option interactions
# ===========================================================================

def test_limit_with_run_id_is_refused(tmp_path, capsys):
    run_id = waiting_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", run_id, "--limit", "5", expect=2)
    assert "--limit cannot be combined with a run id" in captured.err
    assert "Traceback" not in captured.err


def test_all_with_run_id_is_refused(tmp_path, capsys):
    run_id = waiting_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", run_id, "--all", expect=2)
    assert "--all cannot be combined with a run id" in captured.err


def test_limit_and_all_still_work_without_a_run_id(tmp_path, capsys):
    for i in range(3):
        waiting_run(tmp_path, f"waiting {i}")
    assert len(re.findall(r"^run_", cli(tmp_path, capsys, "inbox", "--limit",
                                        "2").out, re.MULTILINE)) == 2
    assert len(re.findall(r"^run_", cli(tmp_path, capsys, "inbox",
                                        "--all").out, re.MULTILINE)) == 3


def test_bare_inbox_still_lists_everything(tmp_path, capsys):
    """The positional is optional: existing behaviour is untouched."""
    first = waiting_run(tmp_path, "first")
    second = waiting_run(tmp_path, "second")
    out = cli(tmp_path, capsys, "inbox").out
    assert first in out and second in out
    assert "ACTION" not in out          # still compact by default


def test_bare_inbox_json_unchanged(tmp_path, capsys):
    waiting_run(tmp_path)
    payload = json.loads(cli(tmp_path, capsys, "inbox", "--json").out)
    assert payload and all("actions" not in row for row in payload)


# ===========================================================================
# JSON
# ===========================================================================

def test_json_has_exactly_one_object(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    waiting_run(tmp_path, "another")
    payload = json.loads(cli(tmp_path, capsys, "inbox", run_id, "--json").out)
    assert len(payload) == 1
    assert payload[0]["run_id"] == run_id


def test_json_schema_matches_detailed_inbox(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    single = json.loads(cli(tmp_path, capsys, "inbox", run_id, "--json").out)[0]
    full = next(row for row in json.loads(
        cli(tmp_path, capsys, "inbox", "--json", "--action-details",
            "--all").out) if row["run_id"] == run_id)
    assert single == full


def test_json_action_ordering_matches_storage(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=4)
    payload = json.loads(cli(tmp_path, capsys, "inbox", run_id, "--json").out)
    assert [a["id"] for a in payload[0]["actions"]] == \
        [a.id for a in stored_actions(tmp_path, run_id)]


def test_json_and_human_agree(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=3)
    human = action_ids_in(cli(tmp_path, capsys, "inbox", run_id).out)
    payload = json.loads(cli(tmp_path, capsys, "inbox", run_id, "--json").out)
    assert human == [a["id"] for a in payload[0]["actions"]]


def test_json_has_no_sort_key(tmp_path, capsys):
    run_id = waiting_run(tmp_path)
    payload = json.loads(cli(tmp_path, capsys, "inbox", run_id, "--json").out)
    assert "sort_key" not in payload[0]


def test_json_unknown_run_errors(tmp_path, capsys):
    waiting_run(tmp_path)
    captured = cli(tmp_path, capsys, "inbox", "run_nope", "--json", expect=2)
    assert captured.out == ""
    assert "cannot read run" in captured.err


# ===========================================================================
# Data safety
# ===========================================================================

def test_tool_arguments_never_appear(tmp_path, capsys):
    write_raw(tmp_path, "run_args00001", [
        {"id": "act_args", "description": "write the file",
         "tool_name": "filesystem_write",
         "tool_args": {"text": ARG_VALUE, "api_key": SECRET}}])
    out = cli(tmp_path, capsys, "inbox", "run_args00001").out
    assert ARG_VALUE not in out and SECRET not in out
    payload = cli(tmp_path, capsys, "inbox", "run_args00001", "--json").out
    assert ARG_VALUE not in payload and SECRET not in payload
    assert "tool_args" not in payload


def test_sensitive_description_withheld(tmp_path, capsys):
    write_raw(tmp_path, "run_secret0001", [
        {"id": "act_s", "description": f"use api_key {SECRET}"}])
    out = cli(tmp_path, capsys, "inbox", "run_secret0001").out
    assert SECRET not in out
    assert "withheld" in out
    assert "act_s" in out


def test_sensitive_objective_withheld(tmp_path, capsys):
    run_id = waiting_run(tmp_path, f"call the api_key {SECRET} endpoint")
    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert SECRET not in out
    assert SECRET not in cli(tmp_path, capsys, "inbox", run_id, "--json").out


# ===========================================================================
# Action-id usability
# ===========================================================================

def test_id_from_single_run_output_approves_that_action(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    before = [a.id for a in stored_actions(tmp_path, run_id)]

    shown = action_ids_in(cli(tmp_path, capsys, "inbox", run_id).out)
    assert shown == before
    chosen = shown[0]
    cli(tmp_path, capsys, "approve", run_id, "--action-id", chosen)

    after = [a.id for a in stored_actions(tmp_path, run_id)]
    assert chosen not in after
    assert after == [i for i in before if i != chosen]


def test_id_from_single_run_output_rejects_that_action(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    shown = action_ids_in(cli(tmp_path, capsys, "inbox", run_id).out)
    cli(tmp_path, capsys, "reject", run_id, "--action-id", shown[1],
        "--reason", "not allowed")
    after = [a.id for a in stored_actions(tmp_path, run_id)]
    assert shown[1] not in after and shown[0] in after


def test_view_reflects_a_resolved_action(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=3)
    shown = action_ids_in(cli(tmp_path, capsys, "inbox", run_id).out)
    cli(tmp_path, capsys, "approve", run_id, "--action-id", shown[1])

    out = cli(tmp_path, capsys, "inbox", run_id).out
    assert action_ids_in(out) == [shown[0], shown[2]]
    assert shown[1] not in out
    row = next(line for line in out.splitlines() if line.startswith(run_id))
    assert "2" in row.split()


def test_id_from_json_is_usable(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    payload = json.loads(cli(tmp_path, capsys, "inbox", run_id, "--json").out)
    chosen = payload[0]["actions"][1]["id"]
    cli(tmp_path, capsys, "approve", run_id, "--action-id", chosen)
    assert chosen not in [a.id for a in stored_actions(tmp_path, run_id)]


# ===========================================================================
# Targeted loading
# ===========================================================================

def test_only_the_requested_state_file_is_loaded(tmp_path, capsys):
    target = waiting_run(tmp_path, "target")
    for i in range(4):
        waiting_run(tmp_path, f"noise {i}")

    loaded = []
    original = JsonFileStateStore.load

    def counting(self, run_id):
        loaded.append(run_id)
        return original(self, run_id)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(JsonFileStateStore, "load", counting)
    try:
        cli(tmp_path, capsys, "inbox", target)
    finally:
        monkey.undo()
    assert loaded == [target]


def test_run_directory_is_not_enumerated(tmp_path, capsys):
    target = waiting_run(tmp_path, "target")
    for i in range(3):
        waiting_run(tmp_path, f"noise {i}")

    calls = []
    original = JsonFileStateStore.list_runs

    def counting(self):
        calls.append(1)
        return original(self)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(JsonFileStateStore, "list_runs", counting)
    try:
        cli(tmp_path, capsys, "inbox", target)
    finally:
        monkey.undo()
    assert calls == []


# ===========================================================================
# Read-only guarantees
# ===========================================================================

def test_files_and_mtimes_unchanged(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    clean_run(tmp_path)
    before = digest(tmp_path)
    cli(tmp_path, capsys, "inbox", run_id)
    cli(tmp_path, capsys, "inbox", run_id, "--json")
    assert digest(tmp_path) == before


def test_no_agent_constructed(tmp_path, capsys, monkeypatch):
    run_id = waiting_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    assert cli(tmp_path, capsys, "inbox", run_id).out


def test_no_tool_or_model_invoked(tmp_path, capsys, monkeypatch):
    run_id = waiting_run(tmp_path)

    from agentcore.models.base import BaseModelProvider
    from agentcore.tools.executor import ToolExecutor

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not execute anything")

    monkeypatch.setattr(ToolExecutor, "invoke", explode)
    monkeypatch.setattr(BaseModelProvider, "complete", explode)
    assert cli(tmp_path, capsys, "inbox", run_id).out


def test_no_state_saved(tmp_path, capsys, monkeypatch):
    run_id = waiting_run(tmp_path)

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not write state")

    monkeypatch.setattr(JsonFileStateStore, "save", explode)
    assert cli(tmp_path, capsys, "inbox", run_id).out


def test_repeated_inspection_does_not_resolve_actions(tmp_path, capsys):
    run_id = waiting_run(tmp_path, count=2)
    for _ in range(3):
        cli(tmp_path, capsys, "inbox", run_id)
    assert len(stored_actions(tmp_path, run_id)) == 2
