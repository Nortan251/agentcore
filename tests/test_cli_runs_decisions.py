"""Operator-decision visibility in `agentcore runs`.

A decision is established *only* by an ``operator_decision`` history entry —
never inferred from a task status. Pending actions, decisions and task status
are three separate facts and must stay visibly distinct.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from conftest import journal_for_tools

from agentcore.cli import (
    NO_DECISION,
    PENDING_DECISION,
    _decision_label,
    _latest_decision,
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
from agentcore.state.models import AgentState, PendingAction
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-decision-must-not-leak"


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
    tool = WriteTool()
    agent = Agent(config_for(tmp_path, level=level), AgentDependencies(
        tools=[tool], planner=planner, verifier=PassVerifier(),
        state_store=JsonFileStateStore(tmp_path / "runs"),
        **journal_for_tools([tool])))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.006)
    return agent.state.run_id


def plain_run(tmp_path, objective="a plain run"):
    """Completes normally: no pending action, no decision."""
    return seed(tmp_path, objective, planner=WritePlanner())


def escalated_run(tmp_path, objective="needs approval"):
    """Permission denied -> terminal with one pending action."""
    return seed(tmp_path, objective, planner=WritePlanner(),
                level=PermissionLevel.OBSERVE)


def pending_run(tmp_path, objective="awaiting a human"):
    """Tool-less step -> BLOCKED with one pending action, no decision."""
    return seed(tmp_path, objective, planner=NoToolPlanner())


def corrupt_run(tmp_path, name="run_corrupt0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def legacy_run(tmp_path, name="run_legacy0001", objective="a legacy run"):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text(json.dumps({
        "run_id": name, "phase": "idle",
        "current_task": {"objective": objective, "id": "t",
                         "status": "completed", "subtasks": []}}),
        encoding="utf-8")
    return name


def load(tmp_path, run_id):
    return JsonFileStateStore(tmp_path / "runs").load(run_id)


def cli(tmp_path, capsys, *argv, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), *argv])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def runs_out(tmp_path, capsys, *flags) -> str:
    return cli(tmp_path, capsys, "runs", *flags).out


def runs_json(tmp_path, capsys, *flags) -> list[dict]:
    return json.loads(cli(tmp_path, capsys, "runs", "--json", *flags).out)


def row_for(output: str, run_id: str) -> str:
    return next(line for line in output.splitlines()
                if line.startswith(run_id))


def summary_for(payload: list[dict], run_id: str) -> dict:
    return next(s for s in payload if s["run_id"] == run_id)


def approve(tmp_path, capsys, run_id, *flags):
    return cli(tmp_path, capsys, "approve", run_id, *flags)


def reject(tmp_path, capsys, run_id, *flags):
    return cli(tmp_path, capsys, "reject", run_id, *flags)


def digest(tmp_path) -> dict[str, str]:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Column presence
# ===========================================================================

def test_listing_has_a_decision_column(tmp_path, capsys):
    plain_run(tmp_path)
    assert "DECISION" in runs_out(tmp_path, capsys)


def test_decision_column_precedes_replans(tmp_path, capsys):
    plain_run(tmp_path)
    header = runs_out(tmp_path, capsys).splitlines()[0]
    assert header.index("DECISION") < header.index("REPLANS")


# ===========================================================================
# No decision
# ===========================================================================

def test_plain_run_shows_no_decision(tmp_path, capsys):
    run_id = plain_run(tmp_path)
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == \
        NO_DECISION


def test_plain_run_json_decision_is_null(tmp_path, capsys):
    run_id = plain_run(tmp_path)
    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] is None
    assert summary["decision_at"] is None


def test_failed_run_is_not_assumed_rejected(tmp_path, capsys):
    """A run that failed by itself must not look like a human refused it."""
    run_id = escalated_run(tmp_path)          # terminal FAILED, undecided
    assert load(tmp_path, run_id).current_task.status.is_terminal
    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] is None


def test_cancelled_run_without_history_is_not_assumed_rejected(tmp_path,
                                                               capsys):
    """CANCELLED alone establishes nothing about operator involvement."""
    from agentcore.tasks.models import TaskStatus

    run_id = pending_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.pending_actions.clear()
    state.current_task.transition_to(TaskStatus.CANCELLED)
    store.save(state)

    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] is None
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == \
        NO_DECISION


# ===========================================================================
# Approved / rejected
# ===========================================================================

def test_approved_run_shows_approved(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == "APPROVED"


def test_rejected_run_shows_rejected(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == "REJECTED"


def test_approved_and_rejected_are_distinguishable(tmp_path, capsys):
    approved = escalated_run(tmp_path, "one")
    rejected = escalated_run(tmp_path, "two")
    approve(tmp_path, capsys, approved)
    reject(tmp_path, capsys, rejected)

    out = runs_out(tmp_path, capsys)
    assert row_for(out, approved).split()[2] == "APPROVED"
    assert row_for(out, rejected).split()[2] == "REJECTED"


def test_decision_metadata_recorded(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id, "--operator", "alice")
    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] == "APPROVED"
    assert summary["decision_operator"] == "alice"
    assert summary["decision_at"] is not None


# ===========================================================================
# Multiple decisions: latest wins
# ===========================================================================

def _two_pending(tmp_path, capsys):
    run_id = escalated_run(tmp_path, "two decisions")
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.add_pending_action(PendingAction(description="a second decision"))
    store.save(state)
    ids = [a.id for a in load(tmp_path, run_id).pending_actions]
    return run_id, ids


def test_latest_decision_wins_reject_after_approve(tmp_path, capsys):
    run_id, (first, second) = _two_pending(tmp_path, capsys)
    approve(tmp_path, capsys, run_id, "--action-id", first,
            "--operator", "alice")
    reject(tmp_path, capsys, run_id, "--action-id", second,
           "--operator", "bob")

    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] == "REJECTED"
    assert summary["decision_operator"] == "bob"


def test_latest_decision_wins_approve_after_reject(tmp_path, capsys):
    run_id, (first, second) = _two_pending(tmp_path, capsys)
    reject(tmp_path, capsys, run_id, "--action-id", first)
    approve(tmp_path, capsys, run_id, "--action-id", second,
            "--operator", "carol")

    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] == "APPROVED"
    assert summary["decision_operator"] == "carol"


def test_underlying_history_keeps_every_decision(tmp_path, capsys):
    run_id, (first, second) = _two_pending(tmp_path, capsys)
    approve(tmp_path, capsys, run_id, "--action-id", first)
    reject(tmp_path, capsys, run_id, "--action-id", second)

    entries = [h for h in load(tmp_path, run_id).history
               if h.kind == "operator_decision"]
    assert [e.data["decision"] for e in entries] == ["APPROVED", "REJECTED"]


def test_history_shows_all_decisions_while_listing_shows_latest(tmp_path,
                                                                capsys):
    run_id, (first, second) = _two_pending(tmp_path, capsys)
    approve(tmp_path, capsys, run_id, "--action-id", first)
    reject(tmp_path, capsys, run_id, "--action-id", second)

    timeline = cli(tmp_path, capsys, "history", run_id).out
    assert timeline.count("operator decision:") == 2
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == "REJECTED"


# ===========================================================================
# Pending actions are not decisions
# ===========================================================================

def test_pending_action_without_decision_shows_pending(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    assert load(tmp_path, run_id).pending_actions
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == \
        PENDING_DECISION


def test_pending_is_not_a_decision_in_json(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["decision"] is None, "PENDING is not a decision"
    assert summary["pending_actions"] == 1


def test_decision_takes_precedence_over_remaining_pending(tmp_path, capsys):
    """A recorded decision is a fact and outranks an outstanding action."""
    run_id, (first, _second) = _two_pending(tmp_path, capsys)
    approve(tmp_path, capsys, run_id, "--action-id", first)

    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["pending_actions"] == 1
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == "APPROVED"


def test_decision_label_rules():
    assert _decision_label({"decision": "APPROVED"}) == "APPROVED"
    assert _decision_label({"decision": None, "pending_actions": 2}) == \
        PENDING_DECISION
    assert _decision_label({"decision": None, "pending_actions": 0}) == \
        NO_DECISION
    assert _decision_label({}) == NO_DECISION


# ===========================================================================
# Status is unaffected
# ===========================================================================

def test_status_still_derives_from_state_not_from_the_decision(tmp_path,
                                                               capsys):
    """Deciding does not *set* a status; the status keeps tracking state.

    ESCALATED is derived from having pending actions, so resolving the last
    one legitimately reveals the underlying task status (here FAILED). The
    invariant is that STATUS mirrors task/pending state while DECISION
    reports the operator fact — they are computed independently.
    """
    run_id = escalated_run(tmp_path)
    assert summary_for(runs_json(tmp_path, capsys), run_id)["status"] == \
        "ESCALATED"

    reject(tmp_path, capsys, run_id)
    after = summary_for(runs_json(tmp_path, capsys), run_id)

    assert after["status"] == "FAILED"       # underlying task, not "REJECTED"
    assert after["decision"] == "REJECTED"   # the operator fact
    assert after["status"] != after["decision"]


def test_status_and_decision_are_separate_columns(tmp_path, capsys):
    """An approved run reopens to task status PENDING — not a decision."""
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    summary = summary_for(runs_json(tmp_path, capsys), run_id)
    assert summary["status"] == "PENDING"      # task status
    assert summary["decision"] == "APPROVED"   # operator fact


# ===========================================================================
# History consistency
# ===========================================================================

@pytest.mark.parametrize("decide,expected", [
    ("approve", "APPROVED"),
    ("reject", "REJECTED"),
])
def test_listing_agrees_with_history(tmp_path, capsys, decide, expected):
    run_id = escalated_run(tmp_path)
    cli(tmp_path, capsys, decide, run_id)

    listed = summary_for(runs_json(tmp_path, capsys), run_id)["decision"]
    timeline = cli(tmp_path, capsys, "history", run_id).out
    recorded = next(h for h in load(tmp_path, run_id).history
                    if h.kind == "operator_decision").data["decision"]

    assert listed == expected
    assert recorded == expected
    assert f"operator decision: {expected}" in timeline


def test_history_renders_decision_details(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    action_id = load(tmp_path, run_id).pending_actions[0].id
    reject(tmp_path, capsys, run_id, "--action-id", action_id,
           "--operator", "alice", "--reason", "declined by policy")

    timeline = cli(tmp_path, capsys, "history", run_id).out
    assert "operator decision: REJECTED" in timeline
    assert "by: alice" in timeline
    assert f"action: {action_id}" in timeline
    assert "declined by policy" in timeline


# ===========================================================================
# Filtering interaction
# ===========================================================================

def test_decision_survives_limit(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    assert summary_for(runs_json(tmp_path, capsys, "--limit", "5"),
                       run_id)["decision"] == "APPROVED"


def test_decision_survives_all(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    assert summary_for(runs_json(tmp_path, capsys, "--all"),
                       run_id)["decision"] == "REJECTED"


def test_decision_survives_status_filter(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    status = summary_for(runs_json(tmp_path, capsys, "--all"),
                         run_id)["status"]

    filtered = runs_json(tmp_path, capsys, "--status", status, "--all")
    assert summary_for(filtered, run_id)["decision"] == "REJECTED"


def test_limit_still_caps_rows(tmp_path, capsys):
    for i in range(4):
        plain_run(tmp_path, f"run {i}")
    assert len(runs_json(tmp_path, capsys, "--limit", "2")) == 2


def test_status_filter_still_works(tmp_path, capsys):
    plain_run(tmp_path, "completed one")
    escalated_run(tmp_path, "escalated one")
    filtered = runs_json(tmp_path, capsys, "--status", "COMPLETED", "--all")
    assert all(s["status"] == "COMPLETED" for s in filtered)


# ===========================================================================
# JSON parity
# ===========================================================================

def test_json_and_human_agree_on_decision(tmp_path, capsys):
    approved = escalated_run(tmp_path, "one")
    rejected = escalated_run(tmp_path, "two")
    pending = pending_run(tmp_path, "three")
    plain = plain_run(tmp_path, "four")
    approve(tmp_path, capsys, approved)
    reject(tmp_path, capsys, rejected)

    out = runs_out(tmp_path, capsys)
    payload = runs_json(tmp_path, capsys)

    for run_id in (approved, rejected, pending, plain):
        label = row_for(out, run_id).split()[2]
        assert label == _decision_label(summary_for(payload, run_id))


def test_json_exposes_no_operator_note(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id, "--reason", "a long internal rationale")
    payload = runs_json(tmp_path, capsys)
    assert "note" not in summary_for(payload, run_id)
    assert "a long internal rationale" not in json.dumps(payload)


# ===========================================================================
# Redaction
# ===========================================================================

def test_secret_in_operator_note_never_reaches_the_listing(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id, "--reason", f"rotate api_key {SECRET}")
    assert SECRET not in runs_out(tmp_path, capsys)
    assert SECRET not in json.dumps(runs_json(tmp_path, capsys))


def test_secret_in_operator_name_is_withheld(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id, "--operator", f"token {SECRET}")
    payload = runs_json(tmp_path, capsys)
    assert SECRET not in json.dumps(payload)
    assert "withheld" in summary_for(payload, run_id)["decision_operator"]


def test_long_operator_name_is_bounded(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id, "--operator", "n" * 200)
    operator = summary_for(runs_json(tmp_path, capsys),
                           run_id)["decision_operator"]
    assert len(operator) <= 33


# ===========================================================================
# Legacy / corrupt / read-only
# ===========================================================================

def test_corrupt_run_shows_no_decision(tmp_path, capsys):
    plain_run(tmp_path)
    name = corrupt_run(tmp_path)
    assert row_for(runs_out(tmp_path, capsys), name).split()[2] == NO_DECISION
    assert summary_for(runs_json(tmp_path, capsys), name)["decision"] is None


def test_legacy_run_shows_no_decision(tmp_path, capsys):
    name = legacy_run(tmp_path)
    summary = summary_for(runs_json(tmp_path, capsys), name)
    assert summary["decision"] is None
    assert summary["pending_actions"] == 0


def test_corrupt_run_does_not_hide_decided_runs(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    corrupt_run(tmp_path)
    assert row_for(runs_out(tmp_path, capsys), run_id).split()[2] == "APPROVED"


def test_state_without_history_is_safe():
    assert _latest_decision(AgentState())["decision"] is None


@pytest.mark.parametrize("bogus", [
    {"kind": "operator_decision", "data": {}},
    {"kind": "operator_decision", "data": {"decision": None}},
])
def test_malformed_decision_entry_is_safe(bogus):
    from agentcore.state.models import HistoryEntry

    state = AgentState()
    state.history.append(HistoryEntry(kind=bogus["kind"], summary="",
                                      data=bogus["data"]))
    assert _latest_decision(state)["decision"] is None


def test_listing_remains_read_only(tmp_path, capsys):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    before = digest(tmp_path)

    runs_out(tmp_path, capsys)
    runs_json(tmp_path, capsys, "--all")
    runs_out(tmp_path, capsys, "--status", "PENDING")

    assert digest(tmp_path) == before


def test_listing_constructs_no_agent(tmp_path, capsys, monkeypatch):
    run_id = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("runs must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    runs_out(tmp_path, capsys)
    runs_json(tmp_path, capsys, "--all")


# ===========================================================================
# END-TO-END acceptance
# ===========================================================================

def test_end_to_end_decision_visibility(tmp_path, capsys):
    """Every decision state is distinguishable at a glance, and agrees with
    history and JSON."""
    plain = plain_run(tmp_path, "a plain completed run")
    approved = escalated_run(tmp_path, "to be approved")
    rejected = escalated_run(tmp_path, "to be rejected")
    pending = pending_run(tmp_path, "awaiting a human")
    multi, (first, second) = _two_pending(tmp_path, capsys)
    corrupt = corrupt_run(tmp_path, "run_corrupt9999")
    legacy = legacy_run(tmp_path, "run_legacy9999")

    approve(tmp_path, capsys, approved, "--operator", "alice")
    reject(tmp_path, capsys, rejected, "--reason", "declined by policy")
    approve(tmp_path, capsys, multi, "--action-id", first,
            "--operator", "alice")
    reject(tmp_path, capsys, multi, "--action-id", second,
           "--operator", "bob")

    out = runs_out(tmp_path, capsys, "--all")

    # 1. each state is clearly distinguished
    assert row_for(out, plain).split()[2] == NO_DECISION
    assert row_for(out, approved).split()[2] == "APPROVED"
    assert row_for(out, rejected).split()[2] == "REJECTED"
    assert row_for(out, pending).split()[2] == PENDING_DECISION
    assert row_for(out, multi).split()[2] == "REJECTED"   # latest wins
    assert row_for(out, corrupt).split()[2] == NO_DECISION
    assert row_for(out, legacy).split()[2] == NO_DECISION

    # 2. history agrees for the decided runs
    for run_id, expected in ((approved, "APPROVED"), (rejected, "REJECTED")):
        timeline = cli(tmp_path, capsys, "history", run_id).out
        assert f"operator decision: {expected}" in timeline

    # the multi run lists both decisions in history
    multi_timeline = cli(tmp_path, capsys, "history", multi).out
    assert multi_timeline.count("operator decision:") == 2

    # 3. JSON carries the same logical information
    payload = runs_json(tmp_path, capsys, "--all")
    assert summary_for(payload, approved)["decision"] == "APPROVED"
    assert summary_for(payload, approved)["decision_operator"] == "alice"
    assert summary_for(payload, rejected)["decision"] == "REJECTED"
    assert summary_for(payload, multi)["decision_operator"] == "bob"
    assert summary_for(payload, plain)["decision"] is None
    assert summary_for(payload, pending)["decision"] is None
    assert summary_for(payload, pending)["pending_actions"] == 1

    # 4. human and JSON agree row by row
    for summary in payload:
        assert row_for(out, summary["run_id"]).split()[2] == \
            _decision_label(summary)

    # 5. nothing sensitive leaked, and the listing stayed read-only
    assert SECRET not in out
    assert "declined by policy" not in out, "notes belong in history"
