"""`agentcore inbox`: the operator-attention view.

The inbox answers exactly one question — "which runs need me to do something
now?" — and its only inclusion criterion is `pending_actions > 0`. The most
important regression here is the partially decided run: one action approved,
another still unresolved. Such a run's DECISION label is APPROVED, so
`runs --decision PENDING` misses it; the inbox must not.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from conftest import journal_for_tools

from agentcore.cli import (
    DECISION_APPROVED,
    DECISION_REJECTED,
    DEFAULT_RUN_LIMIT,
    INBOX_EMPTY,
    PENDING_DECISION,
    _decision_label,
    filter_attention,
    main,
    needs_attention,
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

SECRET = "sk-inbox-must-not-leak"


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


class FailingTool(Tool):
    @property
    def spec(self):
        return tool_spec("boom", "Always fails.", parameters=[],
                         capabilities=[Capability.FS_READ], read_only=True)

    def run(self, args, context):
        return ToolResult.failure("nope")


class PassVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="V", detail="ok")


class WritePlanner:
    """One executable step (used for approve/reject fixtures)."""

    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="write", tool_name="write_note",
                                 tool_args={"text": "hi"}))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class NoToolPlanner:
    """One step with no tool -> exactly one pending action."""

    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="a step needing a human"))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class TwoHumanStepsPlanner:
    """Two steps with no tool -> two independent pending actions."""

    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="human step one"))
        task.add_subtask(Subtask(description="human step two"))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class FailingPlanner:
    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="doomed", tool_name="boom"))
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
    tools = [WriteTool(), FailingTool()]
    agent = Agent(config_for(tmp_path, level=level), AgentDependencies(
        tools=tools, planner=planner,
        verifier=PassVerifier(),
        state_store=JsonFileStateStore(tmp_path / "runs"),
        **journal_for_tools(tools)))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.005)          # distinct persisted timestamps for ordering
    return agent.state.run_id


def cli(tmp_path, capsys, *argv, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), *argv])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


# --- run shapes -------------------------------------------------------------

def pending_run(tmp_path, objective="awaiting a human"):
    """Run A: exactly one unresolved pending action."""
    return seed(tmp_path, objective, planner=NoToolPlanner())


def partially_decided_run(tmp_path, capsys, objective="half decided"):
    """Run B: one action APPROVED, a second still unresolved."""
    run_id = seed(tmp_path, objective, planner=TwoHumanStepsPlanner())
    store = JsonFileStateStore(tmp_path / "runs")
    actions = store.load(run_id).pending_actions
    assert len(actions) == 2
    cli(tmp_path, capsys, "approve", run_id, "--action-id", actions[0].id)
    return run_id


def rejected_run(tmp_path, capsys, objective="reject me"):
    """Run C: the single action was REJECTED, nothing left pending."""
    run_id = seed(tmp_path, objective, planner=WritePlanner(),
                  level=PermissionLevel.OBSERVE)
    cli(tmp_path, capsys, "reject", run_id, "--reason", "declined")
    return run_id


def approved_run(tmp_path, capsys, objective="approve me"):
    """Single action APPROVED and cleared -> nothing left pending."""
    run_id = seed(tmp_path, objective, planner=WritePlanner(),
                  level=PermissionLevel.OBSERVE)
    cli(tmp_path, capsys, "approve", run_id)
    return run_id


def completed_run(tmp_path, objective="a completed run"):
    """Run D."""
    return seed(tmp_path, objective, planner=WritePlanner())


def failed_run(tmp_path, objective="a failed run"):
    """Run E: failed with no pending action."""
    return seed(tmp_path, objective, planner=FailingPlanner())


def corrupt_run(tmp_path, name="run_corrupt0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def legacy_run(tmp_path, name="run_legacy0001") -> str:
    """A readable state document predating pending actions."""
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text(json.dumps({
        "run_id": name, "phase": "idle",
        "current_task": {"objective": "a legacy run", "id": "t",
                         "status": "completed", "subtasks": []}}),
        encoding="utf-8")
    return name


# --- helpers ---------------------------------------------------------------

def ids_of(tmp_path, capsys, *flags) -> list[str]:
    out = cli(tmp_path, capsys, "inbox", *flags).out
    return [line.split()[0] for line in out.splitlines()
            if line.startswith("run_")]


def json_of(tmp_path, capsys, *flags) -> list[dict]:
    return json.loads(cli(tmp_path, capsys, "inbox", "--json", *flags).out)


def summaries_of(tmp_path):
    from agentcore.cli import _collect_summaries
    return _collect_summaries(JsonFileStateStore(tmp_path / "runs"))


def digest(tmp_path) -> dict[str, tuple[str, int]]:
    return {p.name: (hashlib.md5(p.read_bytes()).hexdigest(),
                     p.stat().st_mtime_ns)
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Inclusion criterion
# ===========================================================================

def test_needs_attention_is_pending_count_only():
    assert needs_attention({"pending_actions": 1})
    assert needs_attention({"pending_actions": 5})
    assert not needs_attention({"pending_actions": 0})
    # Unreadable runs carry None: never guessed to need attention.
    assert not needs_attention({"pending_actions": None})
    assert not needs_attention({})
    # A truthy status/decision must never substitute for the count.
    assert not needs_attention({"pending_actions": 0, "status": "ESCALATED",
                                "decision": PENDING_DECISION})


def test_filter_attention_preserves_order():
    rows = [{"run_id": "c", "pending_actions": 2},
            {"run_id": "b", "pending_actions": 0},
            {"run_id": "a", "pending_actions": 1}]
    assert [r["run_id"] for r in filter_attention(rows)] == ["c", "a"]


def test_filter_attention_copies_summaries():
    rows = [{"run_id": "a", "pending_actions": 1}]
    out = filter_attention(rows)
    out[0]["run_id"] = "mutated"
    assert rows[0]["run_id"] == "a"


# ===========================================================================
# Basic inbox
# ===========================================================================

def test_pending_run_appears(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    assert ids_of(tmp_path, capsys) == [run_id]


def test_run_with_no_pending_actions_absent(tmp_path, capsys):
    completed_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox").out
    assert INBOX_EMPTY in out


def test_header_reports_the_count(tmp_path, capsys):
    pending_run(tmp_path)
    assert "1 run(s) awaiting an operator decision" in \
        cli(tmp_path, capsys, "inbox").out


# ===========================================================================
# The key regression: a partially decided run
# ===========================================================================

def test_partially_decided_run_appears(tmp_path, capsys):
    """One action approved, another unresolved -> still needs attention."""
    run_id = partially_decided_run(tmp_path, capsys)
    assert ids_of(tmp_path, capsys) == [run_id]


def test_partially_decided_run_is_invisible_to_decision_pending(tmp_path,
                                                                capsys):
    """Documents exactly the gap the inbox closes."""
    run_id = partially_decided_run(tmp_path, capsys)
    summary = next(s for s in summaries_of(tmp_path) if s["run_id"] == run_id)
    assert summary["pending_actions"] == 1
    assert _decision_label(summary) == DECISION_APPROVED   # not PENDING

    runs_out = cli(tmp_path, capsys, "runs", "--decision", "PENDING",
                   "--all").out
    assert run_id not in runs_out
    assert run_id in cli(tmp_path, capsys, "inbox").out


def test_partially_decided_run_shows_remaining_count(tmp_path, capsys):
    run_id = partially_decided_run(tmp_path, capsys)
    payload = json_of(tmp_path, capsys)
    assert [s["pending_actions"] for s in payload] == [1]
    assert payload[0]["decision"] == DECISION_APPROVED
    assert payload[0]["run_id"] == run_id


def test_decision_label_is_not_the_criterion(tmp_path, capsys):
    """Runs with every decision label can appear, or not, purely on count."""
    partial = partially_decided_run(tmp_path, capsys)      # APPROVED, 1 left
    pending = pending_run(tmp_path)                        # PENDING, 1 left
    approved_run(tmp_path, capsys)                         # APPROVED, 0 left
    rejected_run(tmp_path, capsys)                         # REJECTED, 0 left
    completed_run(tmp_path)                                # '-', 0 left
    assert set(ids_of(tmp_path, capsys, "--all")) == {partial, pending}


# ===========================================================================
# Exclusions
# ===========================================================================

def test_rejected_run_absent(tmp_path, capsys):
    run_id = rejected_run(tmp_path, capsys)
    summary = next(s for s in summaries_of(tmp_path) if s["run_id"] == run_id)
    assert summary["decision"] == DECISION_REJECTED
    assert summary["pending_actions"] == 0
    assert run_id not in cli(tmp_path, capsys, "inbox").out


def test_approved_and_cleared_run_absent(tmp_path, capsys):
    run_id = approved_run(tmp_path, capsys)
    assert run_id not in cli(tmp_path, capsys, "inbox").out


def test_completed_run_absent(tmp_path, capsys):
    run_id = completed_run(tmp_path)
    assert run_id not in cli(tmp_path, capsys, "inbox").out


def test_failed_run_without_pending_actions_absent(tmp_path, capsys):
    run_id = failed_run(tmp_path)
    summary = next(s for s in summaries_of(tmp_path) if s["run_id"] == run_id)
    assert summary["status"] == "FAILED"
    assert summary["pending_actions"] == 0
    assert run_id not in cli(tmp_path, capsys, "inbox").out


def test_failed_run_with_pending_action_appears(tmp_path, capsys):
    """Failure alone is not the criterion — an unresolved action is."""
    escalated = seed(tmp_path, "denied write", planner=WritePlanner(),
                     level=PermissionLevel.OBSERVE)
    summary = next(s for s in summaries_of(tmp_path)
                   if s["run_id"] == escalated)
    assert summary["pending_actions"] >= 1
    assert escalated in cli(tmp_path, capsys, "inbox").out


# ===========================================================================
# Multiple pending actions
# ===========================================================================

def test_multiple_pending_actions_counted(tmp_path, capsys):
    run_id = seed(tmp_path, "two humans", planner=TwoHumanStepsPlanner())
    store = JsonFileStateStore(tmp_path / "runs")
    assert len(store.load(run_id).pending_actions) == 2
    assert json_of(tmp_path, capsys)[0]["pending_actions"] == 2
    row = next(line for line in cli(tmp_path, capsys, "inbox").out.splitlines()
               if line.startswith(run_id))
    assert "2" in row.split()


def test_count_drops_as_actions_are_resolved(tmp_path, capsys):
    run_id = seed(tmp_path, "two humans", planner=TwoHumanStepsPlanner())
    store = JsonFileStateStore(tmp_path / "runs")
    actions = store.load(run_id).pending_actions
    assert json_of(tmp_path, capsys)[0]["pending_actions"] == 2

    cli(tmp_path, capsys, "approve", run_id, "--action-id", actions[0].id)
    assert json_of(tmp_path, capsys)[0]["pending_actions"] == 1

    cli(tmp_path, capsys, "approve", run_id, "--action-id", actions[1].id)
    assert json_of(tmp_path, capsys) == []
    assert INBOX_EMPTY in cli(tmp_path, capsys, "inbox").out


# ===========================================================================
# Ordering
# ===========================================================================

def test_newest_first(tmp_path, capsys):
    first = pending_run(tmp_path, "oldest waiting run")
    second = pending_run(tmp_path, "middle waiting run")
    third = pending_run(tmp_path, "newest waiting run")
    assert ids_of(tmp_path, capsys, "--all") == [third, second, first]


def test_order_matches_runs_listing(tmp_path, capsys):
    pending_run(tmp_path, "one")
    completed_run(tmp_path, "not waiting")
    pending_run(tmp_path, "two")
    waiting = ids_of(tmp_path, capsys, "--all")
    all_runs = [line.split()[0]
                for line in cli(tmp_path, capsys, "runs", "--all").out
                .splitlines() if line.startswith("run_")]
    assert waiting == [r for r in all_runs if r in set(waiting)]


def test_order_is_deterministic_not_filesystem_order(tmp_path, capsys):
    for i in range(4):
        pending_run(tmp_path, f"waiting {i}")
    assert ids_of(tmp_path, capsys, "--all") == ids_of(tmp_path, capsys,
                                                       "--all")


# ===========================================================================
# Limit / all
# ===========================================================================

def test_default_limit(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 3):
        pending_run(tmp_path, f"waiting {i}")
    shown = ids_of(tmp_path, capsys)
    assert len(shown) == DEFAULT_RUN_LIMIT
    assert f"showing {DEFAULT_RUN_LIMIT} of {DEFAULT_RUN_LIMIT + 3}" in \
        cli(tmp_path, capsys, "inbox").out


def test_explicit_limit(tmp_path, capsys):
    ids = [pending_run(tmp_path, f"waiting {i}") for i in range(4)]
    assert ids_of(tmp_path, capsys, "--limit", "2") == \
        list(reversed(ids))[:2]


def test_all_shows_everything(tmp_path, capsys):
    for i in range(DEFAULT_RUN_LIMIT + 2):
        pending_run(tmp_path, f"waiting {i}")
    assert len(ids_of(tmp_path, capsys, "--all")) == DEFAULT_RUN_LIMIT + 2
    assert "use --limit N or --all" not in cli(tmp_path, capsys,
                                               "inbox", "--all").out


def test_limit_and_all_are_mutually_exclusive(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "inbox", "--limit", "2",
              "--all"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_invalid_limit_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "inbox", "--limit", "0"])
    assert exc.value.code == 2
    capsys.readouterr()


# ===========================================================================
# JSON parity
# ===========================================================================

def test_json_matches_human_ids_and_order(tmp_path, capsys):
    for i in range(3):
        pending_run(tmp_path, f"waiting {i}")
    completed_run(tmp_path)
    assert [s["run_id"] for s in json_of(tmp_path, capsys, "--all")] == \
        ids_of(tmp_path, capsys, "--all")


def test_json_respects_limit(tmp_path, capsys):
    for i in range(4):
        pending_run(tmp_path, f"waiting {i}")
    assert len(json_of(tmp_path, capsys, "--limit", "2")) == 2


def test_json_uses_the_same_summary_objects(tmp_path, capsys):
    """Identical keys to `runs --json`; no second serialisation format."""
    run_id = pending_run(tmp_path)
    inbox_row = json_of(tmp_path, capsys)[0]
    runs_row = next(s for s in json.loads(
        cli(tmp_path, capsys, "runs", "--json", "--all").out)
        if s["run_id"] == run_id)
    assert inbox_row == runs_row
    assert "sort_key" not in inbox_row


def test_json_empty_inbox_is_an_empty_list(tmp_path, capsys):
    completed_run(tmp_path)
    assert json_of(tmp_path, capsys) == []


# ===========================================================================
# Empty inbox
# ===========================================================================

def test_empty_inbox_exits_zero(tmp_path, capsys):
    completed_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox", expect=0).out
    assert out.strip() == INBOX_EMPTY


def test_missing_runs_directory(tmp_path, capsys):
    out = cli(tmp_path, capsys, "inbox", expect=0).out
    assert out.strip() == INBOX_EMPTY


def test_missing_runs_directory_json(tmp_path, capsys):
    assert json.loads(cli(tmp_path, capsys, "inbox", "--json").out) == []


# ===========================================================================
# Redaction
# ===========================================================================

def test_sensitive_objective_withheld(tmp_path, capsys):
    pending_run(tmp_path, f"call the api_key {SECRET} endpoint")
    out = cli(tmp_path, capsys, "inbox").out
    assert SECRET not in out
    assert SECRET not in json.dumps(json_of(tmp_path, capsys))


def test_sensitive_operator_name_withheld(tmp_path, capsys):
    run_id = seed(tmp_path, "two humans", planner=TwoHumanStepsPlanner())
    store = JsonFileStateStore(tmp_path / "runs")
    actions = store.load(run_id).pending_actions
    cli(tmp_path, capsys, "approve", run_id, "--action-id", actions[0].id,
        "--operator", f"token {SECRET}")
    assert SECRET not in cli(tmp_path, capsys, "inbox").out
    assert SECRET not in json.dumps(json_of(tmp_path, capsys))


def test_long_objective_truncated(tmp_path, capsys):
    pending_run(tmp_path, "z" * 400)
    assert "z" * 400 not in cli(tmp_path, capsys, "inbox").out
    assert len(json_of(tmp_path, capsys)[0]["objective"]) < 400


# ===========================================================================
# Legacy / unreadable runs
# ===========================================================================

def test_unreadable_run_excluded_and_reported(tmp_path, capsys):
    corrupt_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox").out
    assert INBOX_EMPTY in out
    assert "1 unreadable run(s) were skipped" in out


def test_unreadable_run_noted_alongside_real_entries(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    corrupt_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox").out
    assert run_id in out
    assert "unreadable run(s) were skipped" in out
    assert "run_corrupt0001" not in out


def test_unreadable_run_absent_from_json(tmp_path, capsys):
    corrupt_run(tmp_path)
    pending_run(tmp_path)
    assert all(s["error"] is None for s in json_of(tmp_path, capsys))


def test_legacy_run_without_pending_actions_absent(tmp_path, capsys):
    legacy_run(tmp_path)
    out = cli(tmp_path, capsys, "inbox").out
    assert INBOX_EMPTY in out
    assert "unreadable" not in out


# ===========================================================================
# Read-only guarantees
# ===========================================================================

def test_inbox_does_not_touch_run_files(tmp_path, capsys):
    pending_run(tmp_path)
    partially_decided_run(tmp_path, capsys)
    completed_run(tmp_path)
    before = digest(tmp_path)
    cli(tmp_path, capsys, "inbox", "--all")
    cli(tmp_path, capsys, "inbox", "--json", "--all")
    assert digest(tmp_path) == before


def test_inbox_constructs_no_agent(tmp_path, capsys, monkeypatch):
    pending_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    assert cli(tmp_path, capsys, "inbox").out


def test_inbox_invokes_no_tool_or_model(tmp_path, capsys, monkeypatch):
    pending_run(tmp_path)

    from agentcore.models.base import BaseModelProvider
    from agentcore.tools.executor import ToolExecutor

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("inbox must not execute anything")

    monkeypatch.setattr(ToolExecutor, "invoke", explode)
    monkeypatch.setattr(BaseModelProvider, "complete", explode)
    assert cli(tmp_path, capsys, "inbox").out
