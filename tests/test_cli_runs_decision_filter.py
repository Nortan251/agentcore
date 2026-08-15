"""`agentcore runs --decision`: filtering by operator decision state.

Decision filtering matches the same label the listing displays, so the filter,
the table and `history` can never disagree.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest

from agentcore.cli import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    DEFAULT_RUN_LIMIT,
    NO_DECISION,
    NO_DECISION_ALIAS,
    PENDING_DECISION,
    _decision_label,
    filter_summaries,
    main,
    valid_decisions,
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

SECRET = "sk-filter-must-not-leak"


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
        task.add_subtask(Subtask(description="write", tool_name="write_note",
                                 tool_args={"text": "hi"}))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class NoToolPlanner:
    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="a step needing a human"))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


def config_for(tmp_path, *, level=PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=0, base_delay_seconds=0.0),
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


def approved_run(tmp_path, capsys, objective="approve me"):
    run_id = seed(tmp_path, objective, planner=WritePlanner(),
                  level=PermissionLevel.OBSERVE)
    cli(tmp_path, capsys, "approve", run_id)
    return run_id


def rejected_run(tmp_path, capsys, objective="reject me"):
    run_id = seed(tmp_path, objective, planner=WritePlanner(),
                  level=PermissionLevel.OBSERVE)
    cli(tmp_path, capsys, "reject", run_id, "--reason", "declined")
    return run_id


def pending_run(tmp_path, objective="awaiting a human"):
    return seed(tmp_path, objective, planner=NoToolPlanner())


def plain_run(tmp_path, objective="a plain run"):
    return seed(tmp_path, objective, planner=WritePlanner())


def corrupt_run(tmp_path, name="run_corrupt0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def legacy_run(tmp_path, name="run_legacy0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text(json.dumps({
        "run_id": name, "phase": "idle",
        "current_task": {"objective": "a legacy run", "id": "t",
                         "status": "completed", "subtasks": []}}),
        encoding="utf-8")
    return name


def ids_of(tmp_path, capsys, *flags) -> list[str]:
    out = cli(tmp_path, capsys, "runs", *flags).out
    return [line.split()[0] for line in out.splitlines()
            if line.startswith("run_")]


def json_of(tmp_path, capsys, *flags) -> list[dict]:
    return json.loads(cli(tmp_path, capsys, "runs", "--json", *flags).out)


def digest(tmp_path) -> dict[str, str]:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Vocabulary
# ===========================================================================

def test_vocabulary_covers_every_label():
    """Every value `_decision_label` can emit must be filterable."""
    vocabulary = valid_decisions()
    for label in (DECISION_APPROVED, DECISION_REJECTED, PENDING_DECISION,
                  NO_DECISION):
        assert label in vocabulary
    assert NO_DECISION_ALIAS in vocabulary


def test_vocabulary_matches_recorded_decisions(tmp_path, capsys):
    """The values approve/reject actually write are filterable."""
    approved = approved_run(tmp_path, capsys)
    rejected = rejected_run(tmp_path, capsys)
    payload = json_of(tmp_path, capsys, "--all")
    recorded = {s["decision"] for s in payload if s["decision"]}
    assert recorded <= valid_decisions()
    assert recorded == {DECISION_APPROVED, DECISION_REJECTED}
    assert approved and rejected


# ===========================================================================
# Decision-only filtering
# ===========================================================================

def test_filter_approved(tmp_path, capsys):
    approved = approved_run(tmp_path, capsys)
    rejected_run(tmp_path, capsys)
    plain_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", "APPROVED", "--all") == \
        [approved]


def test_filter_rejected(tmp_path, capsys):
    approved_run(tmp_path, capsys)
    rejected = rejected_run(tmp_path, capsys)
    plain_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", "REJECTED", "--all") == \
        [rejected]


def test_filter_pending(tmp_path, capsys):
    approved_run(tmp_path, capsys)
    pending = pending_run(tmp_path)
    plain_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", "PENDING", "--all") == \
        [pending]


def test_filter_none_alias(tmp_path, capsys):
    approved_run(tmp_path, capsys)
    pending_run(tmp_path)
    plain = plain_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", "NONE", "--all") == [plain]


def test_filter_literal_dash(tmp_path, capsys):
    """The internal marker is accepted too (argparse tolerates '-')."""
    approved_run(tmp_path, capsys)
    plain = plain_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", "-", "--all") == [plain]


def test_none_and_pending_are_distinct(tmp_path, capsys):
    pending = pending_run(tmp_path)
    plain = plain_run(tmp_path)

    assert ids_of(tmp_path, capsys, "--decision", "NONE", "--all") == [plain]
    assert ids_of(tmp_path, capsys, "--decision", "PENDING", "--all") == \
        [pending]


def test_pending_means_unresolved_action_not_task_status(tmp_path, capsys):
    """A task whose *status* is PENDING is not a PENDING decision."""
    approved = approved_run(tmp_path, capsys)
    summary = next(s for s in json_of(tmp_path, capsys, "--all")
                   if s["run_id"] == approved)
    assert summary["status"] == "PENDING"        # task status
    assert summary["decision"] == DECISION_APPROVED

    assert approved not in ids_of(tmp_path, capsys,
                                  "--decision", "PENDING", "--all")


def test_no_filter_returns_everything(tmp_path, capsys):
    approved_run(tmp_path, capsys)
    pending_run(tmp_path)
    plain_run(tmp_path)
    assert len(ids_of(tmp_path, capsys, "--all")) == 3


# ===========================================================================
# Case handling
# ===========================================================================

@pytest.mark.parametrize("spelling", ["rejected", "REJECTED", "ReJeCtEd",
                                      "  rejected  "])
def test_decision_is_case_insensitive(tmp_path, capsys, spelling):
    rejected = rejected_run(tmp_path, capsys)
    plain_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", spelling, "--all") == \
        [rejected]


@pytest.mark.parametrize("spelling", ["none", "None", "NONE"])
def test_none_alias_is_case_insensitive(tmp_path, capsys, spelling):
    plain = plain_run(tmp_path)
    approved_run(tmp_path, capsys)
    assert ids_of(tmp_path, capsys, "--decision", spelling, "--all") == [plain]


# ===========================================================================
# Invalid input
# ===========================================================================

def test_invalid_decision_rejected(tmp_path, capsys):
    plain_run(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--decision", "nonsense"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown decision 'nonsense'" in err
    assert "Traceback" not in err


def test_invalid_decision_lists_valid_values(tmp_path, capsys):
    plain_run(tmp_path)
    with pytest.raises(SystemExit):
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--decision", "typo"])
    err = capsys.readouterr().err
    for expected in ("APPROVED", "REJECTED", "PENDING", "NONE"):
        assert expected in err


def test_typo_is_an_error_not_an_empty_result(tmp_path, capsys):
    rejected_run(tmp_path, capsys)
    with pytest.raises(SystemExit):
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--decision", "REJECT"])          # missing the ED


def test_missing_decision_value_rejected(tmp_path, capsys):
    plain_run(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "runs", "--decision"])
    assert exc.value.code == 2


# ===========================================================================
# Combined filters
# ===========================================================================

def test_status_and_decision_must_both_match(tmp_path, capsys):
    rejected = rejected_run(tmp_path, capsys)
    status = next(s for s in json_of(tmp_path, capsys, "--all")
                  if s["run_id"] == rejected)["status"]

    assert ids_of(tmp_path, capsys, "--status", status,
                  "--decision", "REJECTED", "--all") == [rejected]


def test_conflicting_filters_return_nothing(tmp_path, capsys):
    rejected_run(tmp_path, capsys)
    out = cli(tmp_path, capsys, "runs", "--status", "COMPLETED",
              "--decision", "REJECTED").out
    assert "no runs with status COMPLETED and decision REJECTED" in out


def test_status_filter_alone_still_works(tmp_path, capsys):
    plain_run(tmp_path)
    rejected_run(tmp_path, capsys)
    filtered = json_of(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert all(s["status"] == "COMPLETED" for s in filtered)


def test_decision_with_limit(tmp_path, capsys):
    for i in range(4):
        rejected_run(tmp_path, capsys, f"reject {i}")
    assert len(ids_of(tmp_path, capsys, "--decision", "REJECTED",
                      "--limit", "2")) == 2


def test_decision_with_all(tmp_path, capsys):
    for i in range(3):
        approved_run(tmp_path, capsys, f"approve {i}")
    assert len(ids_of(tmp_path, capsys, "--decision", "APPROVED",
                      "--all")) == 3


def test_status_decision_and_limit(tmp_path, capsys):
    for i in range(4):
        rejected_run(tmp_path, capsys, f"reject {i}")
    status = json_of(tmp_path, capsys, "--all")[0]["status"]
    rows = ids_of(tmp_path, capsys, "--status", status,
                  "--decision", "REJECTED", "--limit", "2")
    assert len(rows) == 2


def test_status_decision_and_all(tmp_path, capsys):
    for i in range(3):
        rejected_run(tmp_path, capsys, f"reject {i}")
    plain_run(tmp_path)
    status = next(s["status"] for s in json_of(tmp_path, capsys, "--all")
                  if s["decision"] == DECISION_REJECTED)
    rows = ids_of(tmp_path, capsys, "--status", status,
                  "--decision", "REJECTED", "--all")
    assert len(rows) == 3


def test_default_limit_applies_to_decision_filter(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 3):
        plain_run(tmp_path, f"plain {i}")
    assert len(ids_of(tmp_path, capsys, "--decision", "NONE")) == \
        DEFAULT_RUN_LIMIT


def test_limit_and_all_remain_exclusive(tmp_path, capsys):
    plain_run(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "runs",
              "--decision", "NONE", "--limit", "2", "--all"])
    assert exc.value.code == 2


# ===========================================================================
# Ordering
# ===========================================================================

def test_filtered_results_stay_newest_first(tmp_path, capsys):
    first = rejected_run(tmp_path, capsys, "reject one")
    plain_run(tmp_path, "unrelated")
    second = rejected_run(tmp_path, capsys, "reject two")
    third = rejected_run(tmp_path, capsys, "reject three")

    assert ids_of(tmp_path, capsys, "--decision", "REJECTED", "--all") == \
        [third, second, first]


def test_limit_takes_the_newest_matches(tmp_path, capsys):
    oldest = rejected_run(tmp_path, capsys, "old")
    plain_run(tmp_path, "newer but undecided")
    newest = rejected_run(tmp_path, capsys, "new")

    rows = ids_of(tmp_path, capsys, "--decision", "REJECTED", "--limit", "1")
    assert rows == [newest]
    assert oldest not in rows


def test_ordering_is_stable(tmp_path, capsys):
    for i in range(3):
        rejected_run(tmp_path, capsys, f"reject {i}")
    first = cli(tmp_path, capsys, "runs", "--decision", "REJECTED",
                "--all").out
    second = cli(tmp_path, capsys, "runs", "--decision", "REJECTED",
                 "--all").out
    assert first == second


# ===========================================================================
# JSON parity
# ===========================================================================

def test_json_matches_human_ids_and_order(tmp_path, capsys):
    for i in range(4):
        rejected_run(tmp_path, capsys, f"reject {i}")
    plain_run(tmp_path)

    human = ids_of(tmp_path, capsys, "--decision", "REJECTED", "--limit", "3")
    machine = [s["run_id"] for s in
               json_of(tmp_path, capsys, "--decision", "REJECTED",
                       "--limit", "3")]
    assert human == machine


def test_json_respects_combined_filters(tmp_path, capsys):
    rejected = rejected_run(tmp_path, capsys)
    plain_run(tmp_path)
    status = next(s["status"] for s in json_of(tmp_path, capsys, "--all")
                  if s["run_id"] == rejected)

    payload = json_of(tmp_path, capsys, "--status", status,
                      "--decision", "REJECTED", "--all")
    assert [s["run_id"] for s in payload] == [rejected]


def test_json_empty_result_is_a_list(tmp_path, capsys):
    plain_run(tmp_path)
    assert json_of(tmp_path, capsys, "--decision", "REJECTED") == []


def test_json_never_leaks_internal_sort_key(tmp_path, capsys):
    rejected_run(tmp_path, capsys)
    out = cli(tmp_path, capsys, "runs", "--json",
              "--decision", "REJECTED").out
    assert "sort_key" not in out


# ===========================================================================
# Unreadable and legacy runs
# ===========================================================================

@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED", "PENDING"])
def test_unreadable_run_never_matches_a_real_decision(tmp_path, capsys,
                                                      decision):
    """A corrupt run has no trustworthy decision to match on."""
    approved_run(tmp_path, capsys)
    rejected_run(tmp_path, capsys)
    pending_run(tmp_path)
    name = corrupt_run(tmp_path)
    assert name not in ids_of(tmp_path, capsys, "--decision", decision,
                              "--all")


def test_unreadable_run_matches_no_decision(tmp_path, capsys):
    """It genuinely has no decision, so NONE is the honest match."""
    name = corrupt_run(tmp_path)
    assert name in ids_of(tmp_path, capsys, "--decision", "NONE", "--all")


def test_legacy_run_matches_no_decision(tmp_path, capsys):
    name = legacy_run(tmp_path)
    assert name in ids_of(tmp_path, capsys, "--decision", "NONE", "--all")


def test_legacy_run_never_matches_a_decision(tmp_path, capsys):
    name = legacy_run(tmp_path)
    for decision in ("APPROVED", "REJECTED", "PENDING"):
        assert name not in ids_of(tmp_path, capsys, "--decision", decision,
                                  "--all")


def test_corrupt_run_does_not_break_filtering(tmp_path, capsys):
    rejected = rejected_run(tmp_path, capsys)
    corrupt_run(tmp_path)
    assert ids_of(tmp_path, capsys, "--decision", "REJECTED", "--all") == \
        [rejected]


def test_empty_directory_with_filter(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "nowhere"), "runs",
                 "--decision", "REJECTED"])
    assert code == 0
    assert "(no runs found)" in capsys.readouterr().out


# ===========================================================================
# filter_summaries unit behaviour
# ===========================================================================

def test_filter_summaries_decision_is_keyword_only():
    data = [{"run_id": "a", "status": "FAILED", "decision": "REJECTED"},
            {"run_id": "b", "status": "FAILED", "decision": None}]
    # positional status still works (existing callers rely on this)
    assert len(filter_summaries(data, "FAILED")) == 2
    assert [s["run_id"] for s in
            filter_summaries(data, None, decision="REJECTED")] == ["a"]


def test_filter_summaries_combines_both():
    data = [{"run_id": "a", "status": "FAILED", "decision": "REJECTED"},
            {"run_id": "b", "status": "COMPLETED", "decision": "REJECTED"},
            {"run_id": "c", "status": "FAILED", "decision": "APPROVED"}]
    assert [s["run_id"] for s in
            filter_summaries(data, "FAILED", decision="REJECTED")] == ["a"]


def test_filter_summaries_preserves_order():
    data = [{"run_id": "a", "decision": "REJECTED"},
            {"run_id": "b", "decision": "APPROVED"},
            {"run_id": "c", "decision": "REJECTED"}]
    assert [s["run_id"] for s in
            filter_summaries(data, None, decision="REJECTED")] == ["a", "c"]


def test_filter_summaries_none_alias_matches_no_decision():
    data = [{"run_id": "a", "decision": None, "pending_actions": 0},
            {"run_id": "b", "decision": "APPROVED"}]
    assert [s["run_id"] for s in
            filter_summaries(data, None, decision="NONE")] == ["a"]
    assert [s["run_id"] for s in
            filter_summaries(data, None, decision=NO_DECISION)] == ["a"]


def test_filter_summaries_pending_uses_the_label():
    data = [{"run_id": "a", "decision": None, "pending_actions": 1},
            {"run_id": "b", "decision": None, "pending_actions": 0}]
    assert [s["run_id"] for s in
            filter_summaries(data, None, decision="PENDING")] == ["a"]


def test_filter_summaries_does_not_mutate_input():
    data = [{"run_id": "a", "decision": "APPROVED"}]
    result = filter_summaries(data, None, decision="APPROVED")
    result[0]["decision"] = "CHANGED"
    assert data[0]["decision"] == "APPROVED"


# ===========================================================================
# Data safety and read-only
# ===========================================================================

def test_operator_note_never_appears(tmp_path, capsys):
    run_id = seed(tmp_path, "sensitive", planner=WritePlanner(),
                  level=PermissionLevel.OBSERVE)
    cli(tmp_path, capsys, "reject", run_id, "--reason",
        f"rotate api_key {SECRET}")

    out = cli(tmp_path, capsys, "runs", "--decision", "REJECTED", "--all").out
    assert SECRET not in out
    assert SECRET not in json.dumps(json_of(tmp_path, capsys,
                                            "--decision", "REJECTED", "--all"))


def test_filtering_is_read_only(tmp_path, capsys):
    approved_run(tmp_path, capsys)
    rejected_run(tmp_path, capsys)
    pending_run(tmp_path)
    before = digest(tmp_path)

    cli(tmp_path, capsys, "runs", "--decision", "PENDING")
    cli(tmp_path, capsys, "runs", "--decision", "NONE", "--all", "--json")
    cli(tmp_path, capsys, "runs", "--status", "FAILED",
        "--decision", "REJECTED")

    assert digest(tmp_path) == before


def test_filtering_constructs_no_agent(tmp_path, capsys, monkeypatch):
    approved_run(tmp_path, capsys)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("runs must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    cli(tmp_path, capsys, "runs", "--decision", "APPROVED", "--all")
    cli(tmp_path, capsys, "runs", "--decision", "NONE", "--json")


# ===========================================================================
# END-TO-END acceptance
# ===========================================================================

def test_end_to_end_decision_filtering(tmp_path, capsys):
    """A realistic archive of >20 runs across every decision state."""
    approved = [approved_run(tmp_path, capsys, f"approve me {i}")
                for i in range(6)]
    rejected = [rejected_run(tmp_path, capsys, f"reject me {i}")
                for i in range(6)]
    pending = [pending_run(tmp_path, f"awaiting {i}") for i in range(5)]
    plain = [plain_run(tmp_path, f"plain run {i}") for i in range(6)]
    corrupt = corrupt_run(tmp_path, "run_corrupt9999")
    legacy = legacy_run(tmp_path, "run_legacy9999")
    assert len(list((tmp_path / "runs").glob("*.json"))) == 25

    # --- pending decisions: only unresolved operator work ------------------
    pending_rows = ids_of(tmp_path, capsys, "--decision", "PENDING", "--all")
    assert pending_rows == list(reversed(pending))

    # --- rejected, newest five ---------------------------------------------
    rejected_rows = ids_of(tmp_path, capsys, "--decision", "REJECTED",
                           "--limit", "5")
    assert rejected_rows == list(reversed(rejected))[:5]

    # --- combined: both conditions must hold -------------------------------
    status = next(s["status"] for s in json_of(tmp_path, capsys, "--all")
                  if s["run_id"] == rejected[-1])
    combined = ids_of(tmp_path, capsys, "--status", status,
                      "--decision", "REJECTED", "--all")
    assert combined == list(reversed(rejected))
    assert not set(combined) & set(approved)

    # --- all approved -------------------------------------------------------
    assert ids_of(tmp_path, capsys, "--decision", "APPROVED", "--all") == \
        list(reversed(approved))

    # --- no decision: plain runs plus the undecidable ones ------------------
    none_rows = ids_of(tmp_path, capsys, "--decision", "NONE", "--all")
    assert set(none_rows) == set(plain) | {corrupt, legacy}

    # --- JSON produces the same logical ids and ordering --------------------
    for flags in (("--decision", "PENDING", "--all"),
                  ("--decision", "REJECTED", "--limit", "5"),
                  ("--status", status, "--decision", "REJECTED", "--all")):
        assert [s["run_id"] for s in json_of(tmp_path, capsys, *flags)] == \
            ids_of(tmp_path, capsys, *flags)

    # --- every listed row agrees with its own label -------------------------
    for summary in json_of(tmp_path, capsys, "--all"):
        label = _decision_label(summary)
        assert summary["run_id"] in ids_of(tmp_path, capsys,
                                           "--decision", label, "--all")
