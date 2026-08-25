"""`agentcore runs` filtering and limiting: --limit, --all, --status."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import journal_for_tools

from agentcore.cli import (
    DEFAULT_RUN_LIMIT,
    UNREADABLE_STATUS,
    filter_summaries,
    main,
    valid_statuses,
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
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    @property
    def spec(self):
        return tool_spec(
            "act", "Perform an action.",
            parameters=[ToolParameter("variant", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        if self.fail:
            return ToolResult.failure("hard failure", retryable=False)
        return ToolResult.success("ok")


class Verifier:
    def __init__(self, *statuses: VerificationStatus) -> None:
        self.statuses = list(statuses)

    def verify(self, subtask, state):
        status = (self.statuses.pop(0) if self.statuses
                  else VerificationStatus.PASSED)
        return VerificationResult(status=status, checked_by="V", detail="d")


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


def make_run(tmp_path, objective, *, fail=False,
             level=PermissionLevel.AUTONOMOUS, recovery=True,
             planner=None, verifier=None) -> str:
    store = JsonFileStateStore(tmp_path / "runs")
    config = AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=0, base_delay_seconds=0.0),
        features=FeatureFlags(recovery=recovery),
    ).validate()
    tool = ActTool(fail=fail)
    agent = Agent(config, AgentDependencies(
        tools=[tool],
        planner=planner or SeqPlanner("A"),
        verifier=verifier or Verifier(VerificationStatus.PASSED),
        state_store=store, **journal_for_tools([tool])))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.004)          # distinct persisted timestamps
    return agent.state.run_id


def completed(tmp_path, objective) -> str:
    return make_run(tmp_path, objective)


def failed(tmp_path, objective) -> str:
    """Recovery off, so a hard tool failure ends FAILED (not ESCALATED)."""
    return make_run(tmp_path, objective, fail=True, recovery=False)


def escalated(tmp_path, objective) -> str:
    return make_run(tmp_path, objective, level=PermissionLevel.OBSERVE)


def corrupt(tmp_path, name="run_corrupt0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def run_cmd(tmp_path, capsys, *flags, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), "runs", *flags])
    out = capsys.readouterr().out
    assert code == expect, out
    return out


def rows(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("run_")]


def ids(output: str) -> list[str]:
    return [line.split()[0] for line in rows(output)]


def json_ids(tmp_path, capsys, *flags) -> list[str]:
    out = run_cmd(tmp_path, capsys, "--json", *flags)
    return [s["run_id"] for s in json.loads(out)]


# ===========================================================================
# Status vocabulary is derived, not hard-coded
# ===========================================================================

def test_vocabulary_derives_from_task_status():
    from agentcore.tasks.models import TaskStatus

    vocabulary = valid_statuses()
    for status in TaskStatus:
        assert status.value.upper() in vocabulary


def test_vocabulary_includes_outcome_verdicts():
    vocabulary = valid_statuses()
    for verdict in ("COMPLETED", "FAILED", "BLOCKED", "ESCALATED", "UNKNOWN",
                    UNREADABLE_STATUS):
        assert verdict in vocabulary


# ===========================================================================
# --limit
# ===========================================================================

def test_default_limit_is_twenty(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 5):
        completed(tmp_path, f"Objective {i}")
    assert len(rows(run_cmd(tmp_path, capsys))) == DEFAULT_RUN_LIMIT


def test_fewer_than_limit_shows_all(tmp_path, capsys):
    for i in range(3):
        completed(tmp_path, f"Objective {i}")
    out = run_cmd(tmp_path, capsys)
    assert len(rows(out)) == 3
    assert "use --limit N or --all" not in out


def test_exactly_at_limit_shows_no_note(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT):
        completed(tmp_path, f"Objective {i}")
    out = run_cmd(tmp_path, capsys)
    assert len(rows(out)) == DEFAULT_RUN_LIMIT
    assert "use --limit N or --all" not in out


def test_over_limit_shows_a_note(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 2):
        completed(tmp_path, f"Objective {i}")
    out = run_cmd(tmp_path, capsys)
    assert f"showing {DEFAULT_RUN_LIMIT} of {DEFAULT_RUN_LIMIT + 2}" in out


def test_limit_one(tmp_path, capsys):
    for i in range(5):
        completed(tmp_path, f"Objective {i}")
    assert len(rows(run_cmd(tmp_path, capsys, "--limit", "1"))) == 1


@pytest.mark.parametrize("n", [2, 3, 7])
def test_limit_n(tmp_path, capsys, n):
    for i in range(8):
        completed(tmp_path, f"Objective {i}")
    assert len(rows(run_cmd(tmp_path, capsys, "--limit", str(n)))) == n


def test_limit_larger_than_available(tmp_path, capsys):
    for i in range(3):
        completed(tmp_path, f"Objective {i}")
    assert len(rows(run_cmd(tmp_path, capsys, "--limit", "99"))) == 3


def test_limit_selects_the_newest(tmp_path, capsys):
    older = completed(tmp_path, "Older")
    newer = completed(tmp_path, "Newer")
    assert ids(run_cmd(tmp_path, capsys, "--limit", "1")) == [newer]
    assert older not in run_cmd(tmp_path, capsys, "--limit", "1")


# -- invalid limits ---------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "-1", "-99", "abc", "1.5", ""])
def test_invalid_limit_rejected(tmp_path, capsys, value):
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "runs", "--limit", value])
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_missing_limit_value_rejected(tmp_path, capsys):
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit):
        main(["--runs-dir", str(tmp_path / "runs"), "runs", "--limit"])


def test_limit_error_message_is_helpful(tmp_path, capsys):
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit):
        main(["--runs-dir", str(tmp_path / "runs"), "runs", "--limit", "0"])
    assert "at least 1" in capsys.readouterr().err


# ===========================================================================
# --all
# ===========================================================================

def test_all_shows_every_run(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 6):
        completed(tmp_path, f"Objective {i}")
    out = run_cmd(tmp_path, capsys, "--all")
    assert len(rows(out)) == DEFAULT_RUN_LIMIT + 6
    assert "use --limit N or --all" not in out


def test_all_includes_unreadable_runs(tmp_path, capsys):
    completed(tmp_path, "Healthy")
    name = corrupt(tmp_path)
    out = run_cmd(tmp_path, capsys, "--all")
    assert name in out
    assert UNREADABLE_STATUS in out


def test_all_still_filters_by_status(tmp_path, capsys):
    for i in range(3):
        completed(tmp_path, f"Completed {i}")
    for i in range(2):
        failed(tmp_path, f"Failed {i}")
    out = run_cmd(tmp_path, capsys, "--all", "--status", "COMPLETED")
    assert len(rows(out)) == 3
    assert "FAILED" not in out


def test_all_still_redacts(tmp_path, capsys):
    completed(tmp_path, f"Rotate the api_key {SECRET} now")
    out = run_cmd(tmp_path, capsys, "--all")
    assert SECRET not in out
    assert "withheld" in out


def test_limit_and_all_are_mutually_exclusive(tmp_path, capsys):
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--limit", "5", "--all"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not allowed with argument" in err
    assert "Traceback" not in err


# ===========================================================================
# --status
# ===========================================================================

def test_status_completed(tmp_path, capsys):
    for i in range(3):
        completed(tmp_path, f"Completed {i}")
    failed(tmp_path, "A failure")
    out = run_cmd(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert len(rows(out)) == 3
    assert all("COMPLETED" in row for row in rows(out))


def test_status_failed(tmp_path, capsys):
    completed(tmp_path, "Fine")
    for i in range(2):
        failed(tmp_path, f"Failure {i}")
    out = run_cmd(tmp_path, capsys, "--status", "FAILED", "--all")
    assert len(rows(out)) == 2
    assert all("FAILED" in row for row in rows(out))


def test_status_escalated(tmp_path, capsys):
    completed(tmp_path, "Fine")
    escalated(tmp_path, "Blocked by permissions")
    out = run_cmd(tmp_path, capsys, "--status", "ESCALATED", "--all")
    assert len(rows(out)) == 1
    assert "ESCALATED" in out


@pytest.mark.parametrize("spelling", ["completed", "COMPLETED", "Completed",
                                      "  completed  "])
def test_status_is_case_insensitive(tmp_path, capsys, spelling):
    completed(tmp_path, "Objective")
    failed(tmp_path, "Failure")
    out = run_cmd(tmp_path, capsys, "--status", spelling, "--all")
    assert len(rows(out)) == 1


def test_status_filter_respects_limit(tmp_path, capsys):
    for i in range(5):
        failed(tmp_path, f"Failure {i}")
    out = run_cmd(tmp_path, capsys, "--status", "FAILED", "--limit", "2")
    assert len(rows(out)) == 2
    assert "showing 2 of 5" in out


def test_status_with_no_matches_is_clean(tmp_path, capsys):
    completed(tmp_path, "Only a completed run")
    out = run_cmd(tmp_path, capsys, "--status", "FAILED")
    assert "no runs with status FAILED" in out
    assert rows(out) == []


def test_status_matches_history_status(tmp_path, capsys):
    """--status must use the same status the listing/history display."""
    run_id = failed(tmp_path, "A failing run")
    listed = run_cmd(tmp_path, capsys, "--status", "FAILED", "--all")
    assert run_id in listed

    main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    history = capsys.readouterr().out
    assert "FAILED" in history[history.index("FINAL"):]


# -- invalid status ---------------------------------------------------------

def test_invalid_status_rejected(tmp_path, capsys):
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--status", "nonsense"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown status 'nonsense'" in err
    assert "Traceback" not in err


def test_invalid_status_lists_valid_values(tmp_path, capsys):
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit):
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--status", "typo"])
    err = capsys.readouterr().err
    for expected in ("COMPLETED", "FAILED", "BLOCKED"):
        assert expected in err


def test_typo_does_not_silently_return_empty(tmp_path, capsys):
    """A typo must be an error, not an innocent-looking empty listing."""
    completed(tmp_path, "Objective")
    with pytest.raises(SystemExit):
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--status", "COMPLETE"])   # missing the D


# ===========================================================================
# Unreadable runs and filtering
# ===========================================================================

def test_unreadable_run_does_not_match_a_status_filter(tmp_path, capsys):
    """A run with no parseable state has no trustworthy status."""
    completed(tmp_path, "Healthy")
    corrupt(tmp_path)
    out = run_cmd(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert len(rows(out)) == 1
    assert UNREADABLE_STATUS not in out


def test_unreadable_run_matches_its_own_status(tmp_path, capsys):
    completed(tmp_path, "Healthy")
    name = corrupt(tmp_path)
    out = run_cmd(tmp_path, capsys, "--status", UNREADABLE_STATUS, "--all")
    assert ids(out) == [name]


def test_unreadable_run_appears_unfiltered(tmp_path, capsys):
    completed(tmp_path, "Healthy")
    name = corrupt(tmp_path)
    assert name in run_cmd(tmp_path, capsys, "--all")


def test_corrupt_run_never_crashes_filtering(tmp_path, capsys):
    completed(tmp_path, "Healthy")
    for index, content in enumerate(["{not json", "[]", '"str"', "{}"]):
        (tmp_path / "runs" / f"run_bad{index:08d}.json").write_text(
            content, encoding="utf-8")
    out = run_cmd(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert "Healthy" in out


# ===========================================================================
# Ordering under filtering
# ===========================================================================

def test_filtered_results_keep_newest_first(tmp_path, capsys):
    first = failed(tmp_path, "Failure one")
    completed(tmp_path, "A completed run in between")
    second = failed(tmp_path, "Failure two")
    third = failed(tmp_path, "Failure three")

    assert ids(run_cmd(tmp_path, capsys, "--status", "FAILED", "--all")) == [
        third, second, first]


def test_limit_takes_newest_matches_not_arbitrary_ones(tmp_path, capsys):
    """The documented A/B/C/D/E case: filter first, then take the newest."""
    oldest_match = failed(tmp_path, "Old failure")
    completed(tmp_path, "Newer but wrong status")
    newest_match = failed(tmp_path, "New failure")

    out = run_cmd(tmp_path, capsys, "--status", "FAILED", "--limit", "1")
    assert ids(out) == [newest_match]
    assert oldest_match not in out


def test_ordering_is_stable_across_invocations(tmp_path, capsys):
    for i in range(4):
        failed(tmp_path, f"Failure {i}")
    first = run_cmd(tmp_path, capsys, "--status", "FAILED", "--all")
    second = run_cmd(tmp_path, capsys, "--status", "FAILED", "--all")
    assert first == second


def test_undated_runs_still_sort_last_when_filtered(tmp_path, capsys):
    dated = completed(tmp_path, "Dated run")
    (tmp_path / "runs" / "run_aaaa0000.json").write_text(json.dumps({
        "run_id": "run_aaaa0000", "phase": "idle",
        "current_task": {"objective": "Undated", "id": "t",
                         "status": "completed", "subtasks": []}}),
        encoding="utf-8")
    out = run_cmd(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert ids(out) == [dated, "run_aaaa0000"]


# ===========================================================================
# JSON parity
# ===========================================================================

def test_json_respects_default_limit(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 3):
        completed(tmp_path, f"Objective {i}")
    assert len(json_ids(tmp_path, capsys)) == DEFAULT_RUN_LIMIT


def test_json_respects_explicit_limit(tmp_path, capsys):
    for i in range(5):
        completed(tmp_path, f"Objective {i}")
    assert len(json_ids(tmp_path, capsys, "--limit", "2")) == 2


def test_json_respects_all(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 2):
        completed(tmp_path, f"Objective {i}")
    assert len(json_ids(tmp_path, capsys, "--all")) == DEFAULT_RUN_LIMIT + 2


def test_json_respects_status(tmp_path, capsys):
    completed(tmp_path, "Fine")
    failed(tmp_path, "Broken")
    listed = json_ids(tmp_path, capsys, "--status", "FAILED", "--all")
    assert len(listed) == 1


def test_json_and_human_return_identical_rows(tmp_path, capsys):
    for i in range(4):
        failed(tmp_path, f"Failure {i}")
    completed(tmp_path, "Fine")

    human = ids(run_cmd(tmp_path, capsys, "--status", "FAILED", "--limit", "3"))
    machine = json_ids(tmp_path, capsys, "--status", "FAILED", "--limit", "3")
    assert human == machine


def test_json_empty_result_is_an_empty_list(tmp_path, capsys):
    completed(tmp_path, "Only completed")
    out = run_cmd(tmp_path, capsys, "--json", "--status", "FAILED")
    assert json.loads(out) == []


def test_json_never_leaks_internal_sort_key(tmp_path, capsys):
    completed(tmp_path, "Objective")
    out = run_cmd(tmp_path, capsys, "--json")
    assert "sort_key" not in out


# ===========================================================================
# Redaction regression
# ===========================================================================

def test_secret_safe_in_filtered_human_output(tmp_path, capsys):
    completed(tmp_path, f"Rotate the api_key {SECRET} in prod")
    out = run_cmd(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert SECRET not in out
    assert "withheld" in out


def test_secret_safe_in_filtered_json_output(tmp_path, capsys):
    completed(tmp_path, f"Rotate the api_key {SECRET} in prod")
    out = run_cmd(tmp_path, capsys, "--json", "--status", "COMPLETED", "--all")
    assert SECRET not in out


def test_secret_safe_with_limit(tmp_path, capsys):
    completed(tmp_path, f"Rotate the token {SECRET} now")
    assert SECRET not in run_cmd(tmp_path, capsys, "--limit", "1")


# ===========================================================================
# Legacy state and read-only
# ===========================================================================

def test_legacy_run_can_be_filtered(tmp_path, capsys):
    (tmp_path / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "runs" / "run_legacy0001.json").write_text(json.dumps({
        "run_id": "run_legacy0001", "phase": "idle",
        "current_task": {"objective": "An old run", "id": "t",
                         "status": "completed", "subtasks": []}}),
        encoding="utf-8")
    out = run_cmd(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert "An old run" in out


def test_filtering_does_not_modify_state(tmp_path, capsys):
    run_id = completed(tmp_path, "Untouched")
    path = tmp_path / "runs" / f"{run_id}.json"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns

    run_cmd(tmp_path, capsys, "--all")
    run_cmd(tmp_path, capsys, "--status", "COMPLETED")
    run_cmd(tmp_path, capsys, "--json", "--limit", "1")

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_filtering_creates_no_files(tmp_path, capsys):
    completed(tmp_path, "Objective")
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    run_cmd(tmp_path, capsys, "--all", "--status", "COMPLETED")
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


def test_no_agent_or_model_is_constructed(tmp_path, capsys, monkeypatch):
    """Listing runs must never start an agent or call a model."""
    completed(tmp_path, "Objective")

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("runs must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    run_cmd(tmp_path, capsys, "--all")
    run_cmd(tmp_path, capsys, "--status", "COMPLETED")


# ===========================================================================
# filter_summaries unit behaviour
# ===========================================================================

def test_filter_summaries_without_status_returns_everything():
    data = [{"run_id": "a", "status": "COMPLETED"},
            {"run_id": "b", "status": "FAILED"}]
    assert len(filter_summaries(data, None)) == 2


def test_filter_summaries_preserves_order():
    data = [{"run_id": "a", "status": "FAILED"},
            {"run_id": "b", "status": "COMPLETED"},
            {"run_id": "c", "status": "FAILED"}]
    assert [s["run_id"] for s in filter_summaries(data, "FAILED")] == ["a", "c"]


def test_filter_summaries_excludes_unreadable():
    data = [{"run_id": "a", "status": "COMPLETED", "error": "boom"},
            {"run_id": "b", "status": "COMPLETED"}]
    assert [s["run_id"] for s in filter_summaries(data, "COMPLETED")] == ["b"]


def test_filter_summaries_does_not_mutate_input():
    data = [{"run_id": "a", "status": "COMPLETED"}]
    result = filter_summaries(data, "COMPLETED")
    result[0]["status"] = "CHANGED"
    assert data[0]["status"] == "COMPLETED"


# ===========================================================================
# END-TO-END acceptance
# ===========================================================================

def test_end_to_end_filtering_and_limits(tmp_path, capsys):
    """A realistic archive: 12 completed, 8 failed, 4 escalated, 1 corrupt."""
    completed_ids = [completed(tmp_path, f"Completed objective {i}")
                     for i in range(12)]
    failed_ids = [failed(tmp_path, f"Failed objective {i}") for i in range(8)]
    escalated_ids = [escalated(tmp_path, f"Escalated objective {i}")
                     for i in range(4)]
    corrupt_id = corrupt(tmp_path, "run_corrupt9999")
    assert len(list((tmp_path / "runs").glob("*.json"))) == 25

    # --- default: newest 20 readable summaries -----------------------------
    default_out = run_cmd(tmp_path, capsys)
    assert len(rows(default_out)) == DEFAULT_RUN_LIMIT
    assert "showing 20 of 25" in default_out
    # newest overall is the last escalated run
    assert ids(default_out)[0] == escalated_ids[-1]

    # --- --status FAILED --limit 5: only failures, newest first, max five --
    failed_out = run_cmd(tmp_path, capsys, "--status", "FAILED", "--limit", "5")
    assert ids(failed_out) == list(reversed(failed_ids))[:5]
    assert all("FAILED" in row for row in rows(failed_out))
    assert "showing 5 of 8" in failed_out

    # --- --all --status COMPLETED: every completed run ---------------------
    completed_out = run_cmd(tmp_path, capsys, "--all", "--status", "COMPLETED")
    assert ids(completed_out) == list(reversed(completed_ids))
    assert len(rows(completed_out)) == 12

    # --- JSON returns exactly the same logical rows ------------------------
    assert json_ids(tmp_path, capsys, "--status", "FAILED", "--limit", "5") \
        == ids(failed_out)

    # --- the corrupt run is visible but never matches a real status --------
    assert corrupt_id in run_cmd(tmp_path, capsys, "--all")
    assert corrupt_id not in completed_out
    assert corrupt_id not in failed_out

    # --- escalated runs are filterable too ---------------------------------
    esc_out = run_cmd(tmp_path, capsys, "--all", "--status", "ESCALATED")
    assert ids(esc_out) == list(reversed(escalated_ids))

    # --- an id from the listing still opens in history ---------------------
    chosen = ids(failed_out)[0]
    code = main(["--runs-dir", str(tmp_path / "runs"), "history", chosen])
    assert code == 0
    assert "Failed objective" in capsys.readouterr().out
