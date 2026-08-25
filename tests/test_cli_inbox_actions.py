"""`agentcore inbox --action-details`: expanded pending-action visibility.

The point of the flag is to close the loop INBOX -> see the action -> copy its
id -> approve/reject, without a detour through `history`. So the critical
guarantees tested here are that every unresolved action is shown, that the id
shown is byte-for-byte the id `--action-id` resolves, and that raw tool
arguments never appear.
"""

from __future__ import annotations

import hashlib
import json
import re
import time

import pytest
from conftest import journal_for_tools

from agentcore.cli import (
    MAX_ACTION_TEXT,
    NO_ACTION_ID,
    NOT_AVAILABLE,
    _action_details,
    _action_id,
    _collect_summaries,
    _run_summary,
    main,
    render_action_details,
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
from agentcore.state.models import AgentState, PendingAction
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-actions-must-not-leak"
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
    """One executable step carrying a sensitive argument value."""

    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="write the note",
                                 tool_name="write_note",
                                 tool_args={"text": ARG_VALUE}))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class TwoHumanStepsPlanner:
    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="human step one"))
        task.add_subtask(Subtask(description="human step two"))
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
    tool = WriteTool()
    agent = Agent(config_for(tmp_path, level=level), AgentDependencies(
        tools=[tool], planner=planner, verifier=PassVerifier(),
        state_store=JsonFileStateStore(tmp_path / "runs"),
        **journal_for_tools([tool])))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.005)
    return agent.state.run_id


def cli(tmp_path, capsys, *argv, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), *argv])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def two_action_run(tmp_path, objective="needs two decisions"):
    return seed(tmp_path, objective, planner=TwoHumanStepsPlanner())


def denied_write_run(tmp_path, objective="a denied write"):
    """Escalates from a permission denial: the action carries a tool name."""
    return seed(tmp_path, objective, planner=WritePlanner(),
                level=PermissionLevel.OBSERVE)


def store_for(tmp_path) -> JsonFileStateStore:
    return JsonFileStateStore(tmp_path / "runs")


def stored_actions(tmp_path, run_id):
    return store_for(tmp_path).load(run_id).pending_actions


def write_raw(tmp_path, name, actions, *, objective="legacy run"):
    """Persist a hand-written state document with the given raw actions."""
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


def details_json(tmp_path, capsys, *flags):
    return json.loads(cli(tmp_path, capsys, "inbox", "--json",
                          "--action-details", *flags).out)


def action_ids_in(text: str) -> list[str]:
    return re.findall(r"^\s*ACTION \d+: (\S+)", text, flags=re.MULTILINE)


def digest(tmp_path) -> dict[str, tuple[str, int]]:
    return {p.name: (hashlib.md5(p.read_bytes()).hexdigest(),
                     p.stat().st_mtime_ns)
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Default output is unchanged
# ===========================================================================

def test_default_inbox_shows_no_action_blocks(tmp_path, capsys):
    two_action_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox").out
    assert "ACTION" not in out
    assert "Description:" not in out


def test_default_inbox_text_identical_with_and_without_the_feature(tmp_path,
                                                                   capsys):
    """The flag is purely additive: omitting it reproduces the old listing."""
    two_action_run(tmp_path)
    denied_write_run(tmp_path)
    plain = cli(tmp_path, capsys, "inbox").out
    detailed = cli(tmp_path, capsys, "inbox", "--action-details").out
    for line in plain.splitlines():
        assert line in detailed.splitlines()


def test_default_json_has_no_actions_key(tmp_path, capsys):
    """Backward compatibility: existing consumers see the same schema."""
    two_action_run(tmp_path)
    payload = json.loads(cli(tmp_path, capsys, "inbox", "--json").out)
    assert payload and all("actions" not in row for row in payload)


def test_runs_json_unaffected(tmp_path, capsys):
    two_action_run(tmp_path)
    payload = json.loads(cli(tmp_path, capsys, "runs", "--json", "--all").out)
    assert payload and all("actions" not in row for row in payload)


def test_runs_listing_has_no_action_blocks(tmp_path, capsys):
    two_action_run(tmp_path)
    assert "ACTION" not in cli(tmp_path, capsys, "runs", "--all").out


# ===========================================================================
# Basic details
# ===========================================================================

def test_action_details_shows_actions(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert run_id in out
    assert "ACTION 1:" in out and "ACTION 2:" in out
    assert "human step one" in out and "human step two" in out


def test_details_include_every_field_the_engine_records(tmp_path, capsys):
    """Both engine producers record description + reason + subtask, no tool."""
    run_id = denied_write_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    action = stored_actions(tmp_path, run_id)[0]
    assert action.id in out
    assert "Tool:" in out and "Description:" in out and "Reason:" in out
    assert action.description[:40] in out
    assert action.reason.split()[0] in out
    assert action.subtask_id in out
    # Documented today: no producer populates tool_name, so it reads as absent
    # rather than being invented.
    assert action.tool_name is None
    assert f"Tool:        {NOT_AVAILABLE}" in out


def test_tool_name_shown_when_a_record_has_one(tmp_path, capsys):
    """The field is displayed whenever it is actually present."""
    write_raw(tmp_path, "run_withtool01", [
        {"id": "act_tooled", "description": "write the file",
         "tool_name": "filesystem_write", "reason": "needs approval"}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert "Tool:        filesystem_write" in out
    assert details_json(tmp_path, capsys)[0]["actions"][0]["tool_name"] == \
        "filesystem_write"


def test_subtask_id_shown_when_present(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    subtask_id = stored_actions(tmp_path, run_id)[0].subtask_id
    assert subtask_id
    assert subtask_id in cli(tmp_path, capsys, "inbox",
                             "--action-details").out


def test_details_hint_at_the_decision_command(tmp_path, capsys):
    two_action_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert "agentcore approve|reject <run_id> --action-id" in out


def test_no_actions_no_blocks(tmp_path, capsys):
    """A run with nothing pending is not in the inbox at all."""
    seed(tmp_path, "clean run", planner=WritePlanner())
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert "ACTION" not in out


# ===========================================================================
# Multiple actions
# ===========================================================================

def test_all_actions_appear_with_correct_ids(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    stored = [a.id for a in stored_actions(tmp_path, run_id)]
    assert len(stored) == 2
    assert action_ids_in(cli(tmp_path, capsys, "inbox",
                             "--action-details").out) == stored


def test_actions_are_not_merged(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert out.count("ACTION 1:") == 1
    assert out.count("ACTION 2:") == 1
    descriptions = re.findall(r"Description: (.+)", out)
    assert "human step one" in descriptions
    assert "human step two" in descriptions


def test_ordering_follows_the_stored_collection(tmp_path, capsys):
    """Documented rule: the pending_actions append order, as persisted."""
    state = AgentState(run_id="run_ordered001")
    for i in range(5):
        state.add_pending_action(PendingAction(description=f"step {i}",
                                               id=f"act_{i}"))
    store_for(tmp_path).save(state)
    shown = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    assert shown == [f"act_{i}" for i in range(5)]


def test_ordering_is_stable_across_invocations(tmp_path, capsys):
    two_action_run(tmp_path)
    first = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    second = action_ids_in(cli(tmp_path, capsys, "inbox",
                               "--action-details").out)
    assert first == second and len(first) == 2


def test_actions_attach_to_the_right_run(tmp_path, capsys):
    first = two_action_run(tmp_path, "first waiting run")
    second = denied_write_run(tmp_path, "second waiting run")
    out = cli(tmp_path, capsys, "inbox", "--action-details", "--all").out

    blocks = {}
    current = None
    for line in out.splitlines():
        if line.startswith("run_"):
            current = line.split()[0]
            blocks[current] = []
        elif current and line.strip().startswith("ACTION"):
            blocks[current].append(line.split(": ")[1])

    assert blocks[first] == [a.id for a in stored_actions(tmp_path, first)]
    assert blocks[second] == [a.id for a in stored_actions(tmp_path, second)]


def test_run_ordering_unchanged_by_the_flag(tmp_path, capsys):
    ids = [two_action_run(tmp_path, f"waiting {i}") for i in range(3)]
    plain = [line.split()[0] for line
             in cli(tmp_path, capsys, "inbox", "--all").out.splitlines()
             if line.startswith("run_")]
    detailed = [line.split()[0] for line
                in cli(tmp_path, capsys, "inbox", "--action-details",
                       "--all").out.splitlines() if line.startswith("run_")]
    assert plain == detailed == list(reversed(ids))


# ===========================================================================
# Safety: raw arguments and secrets
# ===========================================================================

def test_tool_arguments_never_appear(tmp_path, capsys):
    """`PendingAction.tool_args` is a real field; it must never be rendered."""
    write_raw(tmp_path, "run_withargs01", [
        {"id": "act_args", "description": "write the file",
         "tool_name": "filesystem_write",
         "tool_args": {"text": ARG_VALUE, "api_key": SECRET},
         "reason": "needs approval"}])
    # The arguments really are stored on the action being displayed.
    assert stored_actions(tmp_path, "run_withargs01")[0].tool_args == {
        "text": ARG_VALUE, "api_key": SECRET}

    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert ARG_VALUE not in out
    assert SECRET not in out
    assert "act_args" in out            # the action itself is still shown
    payload = json.dumps(details_json(tmp_path, capsys))
    assert ARG_VALUE not in payload and SECRET not in payload


def test_action_details_carry_no_tool_args_key(tmp_path, capsys):
    write_raw(tmp_path, "run_withargs02", [
        {"id": "act_args2", "description": "d",
         "tool_args": {"text": ARG_VALUE}}])
    for row in details_json(tmp_path, capsys):
        for action in row["actions"]:
            assert "tool_args" not in action
            assert set(action) == {"id", "tool_name", "description", "reason",
                                   "subtask_id"}


def test_sensitive_description_withheld(tmp_path, capsys):
    write_raw(tmp_path, "run_secretdesc1", [
        {"id": "act_secret1", "description": f"use api_key {SECRET} now",
         "reason": "needs approval"}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert SECRET not in out
    assert "withheld" in out
    assert "act_secret1" in out          # the id itself stays usable


def test_sensitive_reason_withheld(tmp_path, capsys):
    write_raw(tmp_path, "run_secretreas", [
        {"id": "act_secret2", "description": "do the thing",
         "reason": f"password {SECRET} was rejected"}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert SECRET not in out
    assert SECRET not in json.dumps(details_json(tmp_path, capsys))


def test_sensitive_tool_name_withheld(tmp_path, capsys):
    write_raw(tmp_path, "run_secrettool", [
        {"id": "act_secret3", "description": "d",
         "tool_name": f"token_{SECRET}"}])
    assert SECRET not in cli(tmp_path, capsys, "inbox",
                             "--action-details").out


def test_long_free_text_truncated(tmp_path, capsys):
    write_raw(tmp_path, "run_longtext01", [
        {"id": "act_long1", "description": "d" * 500, "reason": "r" * 500}])
    row = details_json(tmp_path, capsys)[0]["actions"][0]
    assert len(row["description"]) <= MAX_ACTION_TEXT
    assert len(row["reason"]) <= MAX_ACTION_TEXT
    assert "d" * 500 not in cli(tmp_path, capsys, "inbox",
                                "--action-details").out


def test_newlines_collapsed(tmp_path, capsys):
    """Multi-line text must not break the indented block layout."""
    write_raw(tmp_path, "run_multiline1", [
        {"id": "act_ml1", "description": "line one\nline two\nline three"}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert "line one line two line three" in out


# ===========================================================================
# Action id usability
# ===========================================================================

def test_displayed_id_is_verbatim(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    stored = [a.id for a in stored_actions(tmp_path, run_id)]
    shown = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    assert shown == stored                 # no truncation, no ellipsis
    assert all("…" not in i for i in shown)


def test_id_from_output_approves_exactly_that_action(tmp_path, capsys):
    """The core usability contract, end to end through the real CLI."""
    run_id = two_action_run(tmp_path)
    before = [a.id for a in stored_actions(tmp_path, run_id)]

    shown = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    chosen = shown[0]
    cli(tmp_path, capsys, "approve", run_id, "--action-id", chosen)

    after = [a.id for a in stored_actions(tmp_path, run_id)]
    assert chosen not in after
    assert after == [i for i in before if i != chosen]


def test_id_from_output_rejects_exactly_that_action(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    shown = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    chosen = shown[1]
    cli(tmp_path, capsys, "reject", run_id, "--action-id", chosen,
        "--reason", "not allowed")
    after = [a.id for a in stored_actions(tmp_path, run_id)]
    assert chosen not in after and shown[0] in after


def test_id_from_json_is_equally_usable(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    payload = details_json(tmp_path, capsys)
    chosen = payload[0]["actions"][0]["id"]
    cli(tmp_path, capsys, "approve", run_id, "--action-id", chosen)
    assert chosen not in [a.id for a in stored_actions(tmp_path, run_id)]


def test_details_reflect_a_resolved_action(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    shown = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    cli(tmp_path, capsys, "approve", run_id, "--action-id", shown[0])

    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert action_ids_in(out) == [shown[1]]
    assert shown[0] not in out


def test_non_string_stored_id_is_usable(tmp_path, capsys):
    """A legacy numeric id must display, and resolve, without crashing."""
    write_raw(tmp_path, "run_numericid1", [
        {"id": 12345, "description": "numeric id action"},
        {"id": "act_ok", "description": "normal id action"}])
    shown = action_ids_in(cli(tmp_path, capsys, "inbox",
                              "--action-details").out)
    assert shown == ["12345", "act_ok"]

    cli(tmp_path, capsys, "approve", "run_numericid1", "--action-id", "12345")
    remaining = stored_actions(tmp_path, "run_numericid1")
    assert [str(a.id) for a in remaining] == ["act_ok"]


def test_unknown_action_id_fails_cleanly_with_legacy_ids(tmp_path, capsys):
    """Regression: listing the available ids must not raise on a numeric id."""
    write_raw(tmp_path, "run_numericid2", [
        {"id": 999, "description": "numeric"},
        {"id": "act_other", "description": "other"}])
    captured = cli(tmp_path, capsys, "approve", "run_numericid2",
                   "--action-id", "nope", expect=2)
    assert "no pending action with id 'nope'" in captured.err
    assert "999" in captured.err
    assert "Traceback" not in captured.err


# ===========================================================================
# Missing / malformed action data
# ===========================================================================

def test_missing_optional_fields_use_a_clear_fallback(tmp_path, capsys):
    write_raw(tmp_path, "run_sparse0001", [
        {"id": "act_sparse", "description": "just a description"}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert "act_sparse" in out
    assert f"Tool:        {NOT_AVAILABLE}" in out
    assert f"Reason:      {NOT_AVAILABLE}" in out
    assert "Description: just a description" in out


def test_missing_fields_are_null_in_json(tmp_path, capsys):
    write_raw(tmp_path, "run_sparse0002", [
        {"id": "act_sparse2", "description": "d"}])
    action = details_json(tmp_path, capsys)[0]["actions"][0]
    assert action["tool_name"] is None
    assert action["reason"] is None
    assert action["subtask_id"] is None
    assert action["description"] == "d"


def test_unpersisted_id_is_not_fabricated(tmp_path, capsys):
    """`from_dict` mints an id per load, so it would not resolve later."""
    write_raw(tmp_path, "run_noid00001", [{"description": "an action"}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert NO_ACTION_ID in out
    assert "no id was persisted" in out
    # Whatever id the loader invented must not be presented as usable.
    invented = stored_actions(tmp_path, "run_noid00001")[0].id
    assert invented not in out
    assert details_json(tmp_path, capsys)[0]["actions"][0]["id"] is None


def test_blank_id_treated_as_unpersisted(tmp_path, capsys):
    write_raw(tmp_path, "run_blankid001", [
        {"id": "", "description": "blank id action"}])
    assert NO_ACTION_ID in cli(tmp_path, capsys, "inbox",
                               "--action-details").out


def test_empty_description_is_reported_not_invented(tmp_path, capsys):
    write_raw(tmp_path, "run_emptydesc1", [
        {"id": "act_empty", "description": ""}])
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert f"Description: {NOT_AVAILABLE}" in out


def test_unreadable_run_still_excluded(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "run_corrupt0001.json").write_text("{not json", encoding="utf-8")
    out = cli(tmp_path, capsys, "inbox", "--action-details").out
    assert "run_corrupt0001" not in out
    assert "unreadable run(s) were skipped" in out
    assert details_json(tmp_path, capsys) == []


def test_unreadable_summary_reports_empty_actions():
    """The details key exists but claims nothing for an unparseable run."""
    class Broken:
        directory = None

        def load(self, run_id):
            raise ValueError("broken")

    summary = _run_summary(Broken(), "run_x", with_actions=True)
    assert summary["actions"] == []
    assert summary["pending_actions"] is None


# ===========================================================================
# Helper units
# ===========================================================================

def test_action_id_coerces_without_sanitising():
    assert _action_id(PendingAction(description="d", id="act_abc")) == "act_abc"

    class Numeric:
        id = 42

    assert _action_id(Numeric()) == "42"


def test_action_details_reads_only_safe_fields():
    state = AgentState(run_id="run_units00001")
    state.add_pending_action(PendingAction(
        description="do it", id="act_u1", tool_name="write_note",
        tool_args={"text": ARG_VALUE}, reason="needs approval",
        subtask_id="sub_1"))
    [row] = _action_details(state, [{"id": "act_u1"}])
    assert row == {"id": "act_u1", "tool_name": "write_note",
                   "description": "do it", "reason": "needs approval",
                   "subtask_id": "sub_1"}


def test_render_action_details_is_pure_formatting():
    lines = render_action_details([{"id": "act_1", "tool_name": None,
                                    "description": None, "reason": None,
                                    "subtask_id": None}])
    assert lines[0].strip() == "ACTION 1: act_1"
    assert any(NOT_AVAILABLE in line for line in lines)


def test_render_action_details_empty_is_empty():
    assert render_action_details([]) == []


# ===========================================================================
# JSON
# ===========================================================================

def test_json_includes_actions(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    [row] = details_json(tmp_path, capsys)
    assert row["run_id"] == run_id
    assert row["pending_actions"] == 2
    assert [a["id"] for a in row["actions"]] == \
        [a.id for a in stored_actions(tmp_path, run_id)]


def test_json_actions_count_matches_pending_count(tmp_path, capsys):
    two_action_run(tmp_path)
    denied_write_run(tmp_path)
    for row in details_json(tmp_path, capsys, "--all"):
        assert len(row["actions"]) == row["pending_actions"]


def test_human_and_json_contain_the_same_actions(tmp_path, capsys):
    two_action_run(tmp_path, "first")
    denied_write_run(tmp_path, "second")
    human = action_ids_in(cli(tmp_path, capsys, "inbox", "--action-details",
                              "--all").out)
    payload = details_json(tmp_path, capsys, "--all")
    assert human == [a["id"] for row in payload for a in row["actions"]]


def test_json_adds_only_the_actions_key(tmp_path, capsys):
    two_action_run(tmp_path)
    plain = json.loads(cli(tmp_path, capsys, "inbox", "--json").out)[0]
    detailed = details_json(tmp_path, capsys)[0]
    assert set(detailed) - set(plain) == {"actions"}
    assert {k: v for k, v in detailed.items() if k != "actions"} == plain


def test_json_has_no_sort_key(tmp_path, capsys):
    two_action_run(tmp_path)
    assert "sort_key" not in details_json(tmp_path, capsys)[0]


def test_json_respects_limit_and_all(tmp_path, capsys):
    for i in range(4):
        two_action_run(tmp_path, f"waiting {i}")
    assert len(details_json(tmp_path, capsys, "--limit", "2")) == 2
    assert len(details_json(tmp_path, capsys, "--all")) == 4


def test_empty_inbox_with_details(tmp_path, capsys):
    seed(tmp_path, "nothing pending", planner=WritePlanner())
    assert details_json(tmp_path, capsys) == []
    assert "No runs require operator attention" in \
        cli(tmp_path, capsys, "inbox", "--action-details").out


# ===========================================================================
# Collection plumbing
# ===========================================================================

def test_collect_summaries_opt_in(tmp_path):
    two_action_run(tmp_path)
    store = store_for(tmp_path)
    assert all("actions" not in s for s in _collect_summaries(store))
    assert all("actions" in s
               for s in _collect_summaries(store, with_actions=True))


def test_each_run_loaded_once(tmp_path, capsys, monkeypatch):
    """The flag must not introduce a second read pass."""
    two_action_run(tmp_path)
    two_action_run(tmp_path, "another")

    calls = []
    original = JsonFileStateStore.load

    def counting(self, run_id):
        calls.append(run_id)
        return original(self, run_id)

    monkeypatch.setattr(JsonFileStateStore, "load", counting)
    cli(tmp_path, capsys, "inbox", "--action-details", "--all")
    assert len(calls) == len(set(calls)) == 2


# ===========================================================================
# Read-only guarantees
# ===========================================================================

def test_files_and_mtimes_unchanged(tmp_path, capsys):
    two_action_run(tmp_path)
    denied_write_run(tmp_path)
    before = digest(tmp_path)
    cli(tmp_path, capsys, "inbox", "--action-details", "--all")
    cli(tmp_path, capsys, "inbox", "--json", "--action-details", "--all")
    assert digest(tmp_path) == before


def test_no_agent_constructed(tmp_path, capsys, monkeypatch):
    two_action_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    assert cli(tmp_path, capsys, "inbox", "--action-details").out


def test_no_tool_or_model_invoked(tmp_path, capsys, monkeypatch):
    two_action_run(tmp_path)

    from agentcore.models.base import BaseModelProvider
    from agentcore.tools.executor import ToolExecutor

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not execute anything")

    monkeypatch.setattr(ToolExecutor, "invoke", explode)
    monkeypatch.setattr(BaseModelProvider, "complete", explode)
    assert cli(tmp_path, capsys, "inbox", "--action-details").out


def test_no_state_is_saved(tmp_path, capsys, monkeypatch):
    two_action_run(tmp_path)

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not write state")

    monkeypatch.setattr(JsonFileStateStore, "save", explode)
    assert cli(tmp_path, capsys, "inbox", "--action-details", "--all").out


def test_displaying_an_action_does_not_resolve_it(tmp_path, capsys):
    run_id = two_action_run(tmp_path)
    for _ in range(3):
        cli(tmp_path, capsys, "inbox", "--action-details")
    assert len(stored_actions(tmp_path, run_id)) == 2
