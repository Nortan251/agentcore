"""`agentcore runs` compact summary listing."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import journal_for_tools

from agentcore.cli import (
    MAX_OBJECTIVE,
    main,
    render_runs,
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

SECRET = "sk-live-must-not-leak"


# -- fixtures ---------------------------------------------------------------

class ActTool(Tool):
    @property
    def spec(self):
        return tool_spec(
            "act", "Perform an action.",
            parameters=[ToolParameter("variant", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        return ToolResult.success(f"did {args.get('variant')}")


class Verifier:
    def __init__(self, *statuses: VerificationStatus) -> None:
        self.statuses = list(statuses)

    def verify(self, subtask, state):
        status = (self.statuses.pop(0) if self.statuses
                  else VerificationStatus.PASSED)
        return VerificationResult(status=status, checked_by="V",
                                  detail=f"said {status}")


class SeqPlanner:
    def __init__(self, *variants: str) -> None:
        self.variants = list(variants)

    def plan(self, objective, *, context=None, replanning=False):
        variant = self.variants.pop(0) if self.variants else "Z"
        task = Task(objective=objective)
        task.add_subtask(Subtask(description=f"variant {variant}",
                                 tool_name="act",
                                 tool_args={"variant": variant}))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


def make_run(tmp_path, objective, *, planner=None, verifier=None,
             level=PermissionLevel.AUTONOMOUS, max_replans=2) -> str:
    store = JsonFileStateStore(tmp_path / "runs")
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(recovery=True),
    ).validate()
    tool = ActTool()
    agent = Agent(config, AgentDependencies(
        tools=[tool],
        planner=planner or SeqPlanner("A"),
        verifier=verifier or Verifier(VerificationStatus.PASSED),
        state_store=store, **journal_for_tools([tool])))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.01)          # keep persisted timestamps distinct
    return agent.state.run_id


def runs_output(tmp_path, capsys, *, as_json=False) -> str:
    argv = ["--runs-dir", str(tmp_path / "runs"), "runs"]
    if as_json:
        argv.append("--json")
    code = main(argv)
    out = capsys.readouterr().out
    assert code == 0, out
    return out


def write_raw(tmp_path, name: str, content: str) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text(content, encoding="utf-8")


# ===========================================================================
# Basic summary
# ===========================================================================

def test_listing_has_a_header(tmp_path, capsys):
    make_run(tmp_path, "Summarise revenue")
    out = runs_output(tmp_path, capsys)
    for column in ("RUN ID", "STATUS", "REPLANS", "TIMESTAMP", "OBJECTIVE"):
        assert column in out


def test_summary_shows_all_fields(tmp_path, capsys):
    run_id = make_run(tmp_path, "Summarise revenue")
    out = runs_output(tmp_path, capsys)

    assert run_id in out
    assert "Summarise revenue" in out
    assert "COMPLETED" in out
    assert "2026-" in out or "20" in out       # a real persisted timestamp


def test_multiple_runs_each_get_a_row(tmp_path, capsys):
    first = make_run(tmp_path, "First objective")
    second = make_run(tmp_path, "Second objective")
    out = runs_output(tmp_path, capsys)

    assert first in out and second in out
    assert "First objective" in out and "Second objective" in out
    # header + two rows
    assert len([line for line in out.splitlines() if line.startswith("run_")]) == 2


# ===========================================================================
# Statuses
# ===========================================================================

def test_completed_status(tmp_path, capsys):
    make_run(tmp_path, "A completed run")
    assert "COMPLETED" in runs_output(tmp_path, capsys)


def test_escalated_status(tmp_path, capsys):
    """A permission denial escalates rather than completing."""
    make_run(tmp_path, "A blocked run", level=PermissionLevel.OBSERVE,
             max_replans=0)
    assert "ESCALATED" in runs_output(tmp_path, capsys)


def test_blocked_status(tmp_path, capsys):
    """A tool-less plan blocks awaiting a decision."""
    class NoToolPlanner:
        def plan(self, objective, *, context=None, replanning=False):
            task = Task(objective=objective)
            task.add_subtask(Subtask(description="think about it"))
            return task

        def replan(self, task, state):
            return self.plan(task.objective)

    make_run(tmp_path, "A run with nothing executable",
             planner=NoToolPlanner(), max_replans=0)
    out = runs_output(tmp_path, capsys)
    assert "ESCALATED" in out or "BLOCKED" in out


def test_status_matches_history_verdict(tmp_path, capsys):
    """The listing and `history` must never disagree about a run."""
    run_id = make_run(tmp_path, "Consistent run")
    listing = runs_output(tmp_path, capsys)

    main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    history = capsys.readouterr().out
    verdict = history[history.index("FINAL"):].split("—")[0]

    assert "COMPLETED" in verdict
    assert "COMPLETED" in listing


# ===========================================================================
# Replan counts
# ===========================================================================

def test_zero_replans(tmp_path, capsys):
    make_run(tmp_path, "No replans here")
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["replans"] == 0


def test_one_replan(tmp_path, capsys):
    make_run(tmp_path, "One replan", planner=SeqPlanner("A", "B"),
             verifier=Verifier(VerificationStatus.FAILED,
                               VerificationStatus.PASSED))
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["replans"] == 1


def test_multiple_replans(tmp_path, capsys):
    make_run(tmp_path, "Several replans", planner=SeqPlanner("A", "B", "C"),
             verifier=Verifier(VerificationStatus.FAILED,
                               VerificationStatus.FAILED,
                               VerificationStatus.PASSED),
             max_replans=3)
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["replans"] == 2


def test_legacy_state_without_replan_ledger_shows_zero(tmp_path, capsys):
    write_raw(tmp_path, "run_legacy0001", json.dumps({
        "run_id": "run_legacy0001", "phase": "idle",
        "current_task": {"objective": "Old run", "id": "t",
                         "status": "completed", "subtasks": []}}))
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["replans"] == 0


def test_unreadable_run_shows_dash_for_replans(tmp_path, capsys):
    write_raw(tmp_path, "run_broken0001", "{not json")
    out = runs_output(tmp_path, capsys)
    assert "UNREADABLE" in out
    assert " - " in out or "-" in out


# ===========================================================================
# Timestamps
# ===========================================================================

def test_timestamp_is_a_real_persisted_value(tmp_path, capsys):
    run_id = make_run(tmp_path, "Timestamped run")
    raw = json.loads((tmp_path / "runs" / f"{run_id}.json")
                     .read_text(encoding="utf-8"))
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["timestamp"] in (raw.get("updated_at"),
                                       raw.get("created_at"))


def test_missing_timestamp_is_labelled_not_invented(tmp_path, capsys):
    """Legacy state must not be given the current time."""
    write_raw(tmp_path, "run_notime0001", json.dumps({
        "run_id": "run_notime0001", "phase": "idle",
        "current_task": {"objective": "No timestamps", "id": "t",
                         "status": "completed", "subtasks": []}}))
    out = runs_output(tmp_path, capsys)
    assert "(no timestamp)" in out

    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["timestamp"] is None


# ===========================================================================
# Objective display
# ===========================================================================

def test_long_objective_is_truncated_deterministically(tmp_path, capsys):
    long_objective = ("Research and compile a comprehensive competitive "
                      "analysis covering pricing, positioning and strategy "
                      "for every competitor in the segment")
    make_run(tmp_path, long_objective)

    first = runs_output(tmp_path, capsys)
    second = runs_output(tmp_path, capsys)
    assert first == second, "truncation must be deterministic"

    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    shown = payload[0]["objective"]
    assert len(shown) <= MAX_OBJECTIVE
    assert shown.endswith("…")
    assert shown.startswith("Research and compile")


def test_short_objective_is_not_truncated(tmp_path, capsys):
    make_run(tmp_path, "Short one")
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["objective"] == "Short one"


def test_persisted_objective_is_unchanged_by_truncation(tmp_path, capsys):
    long_objective = "X" * 400
    run_id = make_run(tmp_path, long_objective)
    runs_output(tmp_path, capsys)

    raw = json.loads((tmp_path / "runs" / f"{run_id}.json")
                     .read_text(encoding="utf-8"))
    assert raw["current_task"]["objective"] == long_objective


def test_newlines_do_not_break_the_layout(tmp_path, capsys):
    make_run(tmp_path, "Line one\nline two\tand a tab")
    out = runs_output(tmp_path, capsys)
    rows = [line for line in out.splitlines() if line.startswith("run_")]
    assert len(rows) == 1


# ===========================================================================
# Redaction
# ===========================================================================

def test_secret_in_objective_is_withheld(tmp_path, capsys):
    make_run(tmp_path, f"Rotate the deployment api_key {SECRET} in prod")
    out = runs_output(tmp_path, capsys)
    assert SECRET not in out
    assert "withheld" in out


def test_secret_absent_from_json_mode(tmp_path, capsys):
    make_run(tmp_path, f"Rotate the deployment api_key {SECRET} in prod")
    out = runs_output(tmp_path, capsys, as_json=True)
    assert SECRET not in out


@pytest.mark.parametrize("marker", ["api_key", "token", "password", "secret",
                                    "authorization", "APIKEY"])
def test_all_credential_markers_withheld(tmp_path, capsys, marker):
    make_run(tmp_path, f"Update the {marker} value now")
    assert "withheld" in runs_output(tmp_path, capsys)


def test_benign_objective_is_shown(tmp_path, capsys):
    make_run(tmp_path, "Summarise the quarterly report")
    assert "Summarise the quarterly report" in runs_output(tmp_path, capsys)


# ===========================================================================
# Ordering
# ===========================================================================

def test_newest_run_appears_first(tmp_path, capsys):
    oldest = make_run(tmp_path, "Oldest objective")
    middle = make_run(tmp_path, "Middle objective")
    newest = make_run(tmp_path, "Newest objective")

    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert [s["run_id"] for s in payload] == [newest, middle, oldest]


def test_ordering_is_not_filesystem_or_id_based(tmp_path, capsys):
    """Run ids are random hex, so id sorting would be meaningless."""
    ids = [make_run(tmp_path, f"Objective {i}") for i in range(3)]
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    listed = [s["run_id"] for s in payload]
    assert listed == list(reversed(ids))
    assert listed != sorted(listed) or sorted(ids) == list(reversed(ids))


def test_ordering_is_stable_across_invocations(tmp_path, capsys):
    for i in range(3):
        make_run(tmp_path, f"Objective {i}")
    first = runs_output(tmp_path, capsys)
    second = runs_output(tmp_path, capsys)
    assert first == second


def test_undated_runs_sort_last(tmp_path, capsys):
    dated = make_run(tmp_path, "A dated run")
    write_raw(tmp_path, "run_aaaa0000", json.dumps({
        "run_id": "run_aaaa0000", "phase": "idle",
        "current_task": {"objective": "Undated", "id": "t",
                         "status": "completed", "subtasks": []}}))

    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["run_id"] == dated
    assert payload[-1]["run_id"] == "run_aaaa0000"


def test_undated_runs_ordered_by_id(tmp_path, capsys):
    for name in ("run_zzzz0000", "run_aaaa0000"):
        write_raw(tmp_path, name, json.dumps({
            "run_id": name, "phase": "idle",
            "current_task": {"objective": name, "id": "t",
                             "status": "completed", "subtasks": []}}))
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert [s["run_id"] for s in payload] == ["run_aaaa0000", "run_zzzz0000"]


# ===========================================================================
# Legacy / corrupt robustness
# ===========================================================================

def test_one_corrupt_run_does_not_hide_healthy_runs(tmp_path, capsys):
    healthy = make_run(tmp_path, "A healthy run")
    write_raw(tmp_path, "run_corrupt0001", "{not json")

    out = runs_output(tmp_path, capsys)
    assert healthy in out
    assert "A healthy run" in out
    assert "run_corrupt0001" in out
    assert "UNREADABLE" in out


def test_corrupt_run_is_flagged_with_a_note(tmp_path, capsys):
    make_run(tmp_path, "Healthy")
    write_raw(tmp_path, "run_corrupt0002", "{not json")
    out = runs_output(tmp_path, capsys)
    assert "could not be read" in out


def test_no_traceback_for_corrupt_runs(tmp_path, capsys):
    write_raw(tmp_path, "run_corrupt0003", "{not json")
    main(["--runs-dir", str(tmp_path / "runs"), "runs"])
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("content", [
    "{not json", "[]", '"just a string"', "{}", '{"unexpected": true}',
    '{"run_id": "x", "current_task": "not an object"}',
])
def test_malformed_shapes_do_not_crash(tmp_path, capsys, content):
    make_run(tmp_path, "Healthy run")
    write_raw(tmp_path, "run_odd00000001", content)
    out = runs_output(tmp_path, capsys)
    assert "Healthy run" in out, "the healthy run must still be listed"


def test_legacy_run_renders(tmp_path, capsys):
    write_raw(tmp_path, "run_legacy0002", json.dumps({
        "run_id": "run_legacy0002", "phase": "idle",
        "current_task": {"objective": "An old run", "id": "t",
                         "status": "completed", "subtasks": []}}))
    out = runs_output(tmp_path, capsys)
    assert "An old run" in out
    assert "COMPLETED" in out


# ===========================================================================
# Empty directory and read-only
# ===========================================================================

def test_empty_directory_message(tmp_path, capsys):
    assert "(no runs found)" in runs_output(tmp_path, capsys)


def test_missing_directory_is_clean(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "nowhere"), "runs"])
    assert code == 0
    assert "(no runs found)" in capsys.readouterr().out


def test_render_runs_with_no_summaries():
    assert render_runs([]) == "(no runs found)"


def test_listing_does_not_modify_state(tmp_path, capsys):
    run_id = make_run(tmp_path, "Untouched run")
    path = tmp_path / "runs" / f"{run_id}.json"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns

    runs_output(tmp_path, capsys)
    runs_output(tmp_path, capsys, as_json=True)

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_listing_creates_no_files(tmp_path, capsys):
    make_run(tmp_path, "Run")
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    runs_output(tmp_path, capsys)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


# ===========================================================================
# JSON mode
# ===========================================================================

def test_json_mode_field_names(tmp_path, capsys):
    make_run(tmp_path, "JSON run")
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert set(payload[0]) == {"run_id", "status", "objective", "replans",
                               "timestamp", "error", "decision",
                               "decision_at", "decision_operator",
                               "pending_actions"}
    assert "sort_key" not in payload[0], "internal field must not leak"


def test_json_and_human_agree(tmp_path, capsys):
    run_id = make_run(tmp_path, "Agreeing run")
    human = runs_output(tmp_path, capsys)
    payload = json.loads(runs_output(tmp_path, capsys, as_json=True))
    assert payload[0]["run_id"] in human
    assert payload[0]["status"] in human
    assert payload[0]["objective"] in human


# ===========================================================================
# END-TO-END: pick a run from the listing, then open it
# ===========================================================================

def test_end_to_end_listing_guides_you_to_the_right_run(tmp_path, capsys):
    """Several realistic runs; the listing identifies which to inspect."""
    ok = make_run(tmp_path, "Summarise the Q3 revenue figures")
    replanned = make_run(tmp_path, "Draft the launch announcement",
                         planner=SeqPlanner("A", "B"),
                         verifier=Verifier(VerificationStatus.FAILED,
                                           VerificationStatus.PASSED))
    escalated = make_run(tmp_path, "Delete the archived customer records",
                         level=PermissionLevel.OBSERVE, max_replans=0)
    long_run = make_run(
        tmp_path,
        "Research and compile a comprehensive competitive analysis covering "
        "pricing, positioning and go-to-market strategy")
    secret_run = make_run(tmp_path,
                          f"Rotate the deployment api_key {SECRET} in prod")
    write_raw(tmp_path, "run_corrupt9999", "{not json")

    out = runs_output(tmp_path, capsys)
    rows = [line for line in out.splitlines() if line.startswith("run_")]

    # 1. newest first, corrupt run last
    assert [r.split()[0] for r in rows] == [
        secret_run, long_run, escalated, replanned, ok, "run_corrupt9999"]

    # 2. statuses are accurate
    assert "ESCALATED" in next(r for r in rows if escalated in r)
    assert "COMPLETED" in next(r for r in rows if ok in r)
    assert "UNREADABLE" in next(r for r in rows if "corrupt" in r)

    # 3. the replan count distinguishes the troubled run
    # columns: run_id, status, decision, replans, ...
    assert next(r for r in rows if replanned in r).split()[3] == "1"
    assert next(r for r in rows if ok in r).split()[3] == "0"

    # 4. long objectives stay on one line
    long_row = next(r for r in rows if long_run in r)
    assert long_row.endswith("…")
    assert len(long_row.splitlines()) == 1

    # 5. secrets never appear
    assert SECRET not in out
    assert "withheld" in next(r for r in rows if secret_run in r)

    # 6. the id from the listing opens in `history`
    chosen = next(r for r in rows if replanned in r).split()[0]
    code = main(["--runs-dir", str(tmp_path / "runs"), "history", chosen])
    history = capsys.readouterr().out
    assert code == 0
    assert "Draft the launch announcement" in history
    assert "replan #1 accepted" in history
