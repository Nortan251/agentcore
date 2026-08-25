"""`agentcore reject <run_id>`: the operator refusal that closes a run.

Mirror image of `approve`: the decision is audited and the action cleared, but
the run is left terminal so it can never be resumed. The action is never run.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from conftest import journal_for_tools

from agentcore.cli import main
from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.core.replan import ReplanLedger
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.recovery.config import RecoveryConfig
from agentcore.recovery.history import RecoveryHistory
from agentcore.state.models import PendingAction
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-reject-must-not-leak"


# -- fixtures ---------------------------------------------------------------

class WriteTool(Tool):
    """Mutating tool: denied at OBSERVE, permitted at AUTONOMOUS."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def spec(self):
        return tool_spec(
            "write_note", "Write a note.",
            parameters=[ToolParameter("text", ParamType.STRING,
                                      required=False, default=""),
                        ToolParameter("api_key", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.calls.append(dict(args))
        return ToolResult.success(f"wrote {args.get('text')}")


class PassVerifier:
    def verify(self, subtask, state):
        return VerificationResult(status=VerificationStatus.PASSED,
                                  checked_by="V", detail="ok")


class WritePlanner:
    def __init__(self, *, secret: bool = False) -> None:
        self.secret = secret

    def plan(self, objective, *, context=None, replanning=False):
        args = {"text": "hello"}
        if self.secret:
            args["api_key"] = SECRET
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="write the note",
                                 tool_name="write_note", tool_args=args))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class NoToolPlanner:
    """Plans a tool-less step -> runner escalates, task stays BLOCKED."""

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


def seed(tmp_path, objective, *, planner, tool=None,
         level=PermissionLevel.AUTONOMOUS):
    store = JsonFileStateStore(tmp_path / "runs")
    tool = tool if tool is not None else WriteTool()
    agent = Agent(config_for(tmp_path, level=level), AgentDependencies(
        tools=[tool], planner=planner, verifier=PassVerifier(),
        state_store=store, **journal_for_tools([tool])))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.004)
    return agent.state.run_id, tool


def escalated_run(tmp_path, objective="record a note", *, secret=False):
    """Permission denial -> terminal FAILED run with one pending action."""
    return seed(tmp_path, objective, planner=WritePlanner(secret=secret),
                level=PermissionLevel.OBSERVE)


def blocked_run(tmp_path, objective="needs a human"):
    """Tool-less step -> BLOCKED (resumable) with one pending action."""
    return seed(tmp_path, objective, planner=NoToolPlanner())


def completed_run(tmp_path, objective="all done"):
    return seed(tmp_path, objective, planner=WritePlanner())


def corrupt_run(tmp_path, name="run_corrupt0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def load(tmp_path, run_id):
    return JsonFileStateStore(tmp_path / "runs").load(run_id)


def action_id_of(tmp_path, run_id) -> str:
    return load(tmp_path, run_id).pending_actions[0].id


def reject(tmp_path, capsys, run_id, *flags, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), "reject", run_id,
                 *flags])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def approve(tmp_path, capsys, run_id, *flags, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), "approve", run_id,
                 *flags])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def digest(tmp_path) -> dict[str, str]:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (tmp_path / "runs").iterdir()}


def decisions(tmp_path, run_id):
    return [h for h in load(tmp_path, run_id).history
            if h.kind == "operator_decision"]


# ===========================================================================
# Basic rejection
# ===========================================================================

def test_rejection_succeeds(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = reject(tmp_path, capsys, run_id).out
    assert run_id in out
    assert "REJECTED" in out
    assert "TERMINAL" in out
    assert "Resume:   NOT ALLOWED" in out


def test_rejection_clears_the_pending_action(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    assert load(tmp_path, run_id).pending_actions == []


def test_rejected_escalated_run_stays_terminal(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    assert load(tmp_path, run_id).current_task.status.is_terminal


def test_rejected_blocked_run_becomes_terminal(tmp_path, capsys):
    """The important case: a resumable run must be closed by rejection."""
    run_id, _ = blocked_run(tmp_path)
    assert not load(tmp_path, run_id).current_task.status.is_terminal

    reject(tmp_path, capsys, run_id)

    state = load(tmp_path, run_id)
    assert state.current_task.status is TaskStatus.CANCELLED
    assert state.current_task.status.is_terminal


def test_rejected_blocked_run_cancels_its_subtasks(tmp_path, capsys):
    run_id, _ = blocked_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    statuses = {s.status for s in load(tmp_path, run_id).current_task.subtasks}
    assert statuses == {TaskStatus.CANCELLED}


def test_cancelled_is_a_dead_end(tmp_path, capsys):
    """CANCELLED has no outgoing transitions, so nothing can reopen it."""
    from agentcore.tasks.models import can_transition

    run_id, _ = blocked_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    status = load(tmp_path, run_id).current_task.status
    assert not any(can_transition(status, s) for s in TaskStatus)


def test_rejection_preserves_the_run_id(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    assert load(tmp_path, run_id).run_id == run_id


def test_rejection_creates_no_new_run(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    reject(tmp_path, capsys, run_id)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


def test_rejection_offers_no_resume_hint(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = reject(tmp_path, capsys, run_id).out
    assert "--resume" not in out


# ===========================================================================
# Resume safety
# ===========================================================================

def test_resume_is_refused_after_rejecting_an_escalated_run(tmp_path, capsys):
    run_id, tool = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)

    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "--resume", run_id])
    err = capsys.readouterr().err
    assert code == 2
    assert "already finished" in err
    assert tool.calls == []


def test_resume_is_refused_after_rejecting_a_blocked_run(tmp_path, capsys):
    """Previously resumable; rejection must close that door."""
    run_id, _ = blocked_run(tmp_path)
    reject(tmp_path, capsys, run_id)

    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "--resume", run_id])
    err = capsys.readouterr().err
    assert code == 2
    assert "cancelled" in err


def test_engine_also_refuses_a_rejected_run(tmp_path, capsys):
    """Not just the CLI: the engine itself will not run a cancelled task."""
    from agentcore.errors import StateError

    run_id, tool = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)

    agent = Agent(config_for(tmp_path, level=PermissionLevel.AUTONOMOUS),
                  AgentDependencies(tools=[tool], planner=WritePlanner(),
                                    verifier=PassVerifier(),
                                    state_store=JsonFileStateStore(
                                        tmp_path / "runs"),
                                    **journal_for_tools([tool])))
    agent.resume(run_id)
    with pytest.raises(StateError):
        agent.run_task()
    assert tool.calls == []


# ===========================================================================
# Action id handling
# ===========================================================================

def test_explicit_action_id_is_accepted(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    out = reject(tmp_path, capsys, run_id, "--action-id", action_id).out
    assert action_id in out


def test_wrong_action_id_is_rejected(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    captured = reject(tmp_path, capsys, run_id,
                      "--action-id", "act_wrong", expect=2)
    assert "no pending action with id 'act_wrong'" in captured.err
    assert "Traceback" not in captured.err


def test_wrong_action_id_lists_available(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    captured = reject(tmp_path, capsys, run_id,
                      "--action-id", "act_nope", expect=2)
    assert action_id in captured.err


def test_multiple_actions_require_explicit_selection(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.add_pending_action(PendingAction(description="a second decision"))
    store.save(state)

    captured = reject(tmp_path, capsys, run_id, expect=2)
    assert "2 pending actions" in captured.err
    assert "--action-id" in captured.err
    assert len(load(tmp_path, run_id).pending_actions) == 2


def test_rejecting_one_action_leaves_others_intact(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    first = state.pending_actions[0].id
    other = state.add_pending_action(PendingAction(description="second"))
    store.save(state)

    out = reject(tmp_path, capsys, run_id, "--action-id", first).out
    assert "1 pending action(s) still awaiting" in out
    remaining = load(tmp_path, run_id).pending_actions
    assert [a.id for a in remaining] == [other.id]


def test_missing_run_id_argument_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "reject"])
    assert exc.value.code == 2


# ===========================================================================
# Reason handling
# ===========================================================================

def test_default_reason_is_recorded(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = reject(tmp_path, capsys, run_id).out
    assert "operator rejected action" in out
    assert decisions(tmp_path, run_id)[0].data["note"] == \
        "operator rejected action"


def test_custom_reason_is_preserved(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id, "--reason", "too risky for production")
    assert decisions(tmp_path, run_id)[0].data["note"] == \
        "too risky for production"


def test_reason_appears_in_output(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = reject(tmp_path, capsys, run_id, "--reason", "declined by policy").out
    assert "Reason:   declined by policy" in out


def test_secret_like_reason_is_withheld(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    captured = reject(tmp_path, capsys, run_id,
                      "--reason", f"rotate api_key {SECRET} first")
    assert SECRET not in captured.out
    assert "withheld" in captured.out
    assert SECRET not in json.dumps(
        decisions(tmp_path, run_id)[0].to_dict())


def test_long_reason_is_truncated(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id, "--reason", "y" * 500)
    note = decisions(tmp_path, run_id)[0].data["note"]
    assert len(note) <= 161


def test_reason_is_not_otherwise_modified(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reason = "Declined: the customer asked us to hold."
    reject(tmp_path, capsys, run_id, "--reason", reason)
    assert decisions(tmp_path, run_id)[0].data["note"] == reason


# ===========================================================================
# Audit history
# ===========================================================================

def test_decision_is_recorded(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    reject(tmp_path, capsys, run_id, "--action-id", action_id,
           "--operator", "alice", "--reason", "not now")

    entries = decisions(tmp_path, run_id)
    assert len(entries) == 1
    data = entries[0].data
    assert data["decision"] == "REJECTED"
    assert data["operator"] == "alice"
    assert data["action_id"] == action_id
    assert data["note"] == "not now"
    assert entries[0].at is not None


def test_decision_never_records_tool_arguments(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path, secret=True)
    reject(tmp_path, capsys, run_id)
    entry = decisions(tmp_path, run_id)[0]
    assert "tool_args" not in entry.data
    assert SECRET not in json.dumps(entry.to_dict())


def test_decision_appears_in_history_command(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id, "--reason", "declined")

    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    out = capsys.readouterr().out
    assert code == 0
    assert "operator_decision" in out
    assert "REJECTED" in out


def test_prior_history_is_preserved(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    before = load(tmp_path, run_id)
    history_before = len(before.history)
    tool_calls_before = len(before.tool_calls)
    errors_before = len(before.errors)

    reject(tmp_path, capsys, run_id)

    after = load(tmp_path, run_id)
    assert len(after.history) > history_before
    assert len(after.tool_calls) == tool_calls_before
    assert len(after.errors) == errors_before


def test_counters_are_not_reset(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    replans_before = ReplanLedger.load(load(tmp_path, run_id)).count
    recovery_before = RecoveryHistory.load(load(tmp_path, run_id)).to_dict()

    reject(tmp_path, capsys, run_id)

    after = load(tmp_path, run_id)
    assert ReplanLedger.load(after).count == replans_before
    assert RecoveryHistory.load(after).to_dict() == recovery_before


# ===========================================================================
# Approval / rejection symmetry
# ===========================================================================

def test_approved_action_cannot_then_be_rejected(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    approve(tmp_path, capsys, run_id, "--action-id", action_id)

    captured = reject(tmp_path, capsys, run_id,
                      "--action-id", action_id, expect=2)
    assert "no pending action" in captured.err


def test_rejected_action_cannot_then_be_approved(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    reject(tmp_path, capsys, run_id, "--action-id", action_id)

    captured = approve(tmp_path, capsys, run_id,
                       "--action-id", action_id, expect=2)
    assert "no pending action" in captured.err


def test_double_rejection_fails_cleanly(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    before = digest(tmp_path)

    captured = reject(tmp_path, capsys, run_id, expect=2)
    assert "no pending action" in captured.err
    assert digest(tmp_path) == before, "a repeat must not touch history"


def test_repeated_decision_does_not_duplicate_history(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    reject(tmp_path, capsys, run_id, expect=2)
    assert len(decisions(tmp_path, run_id)) == 1


def test_approval_still_makes_a_run_resumable(tmp_path, capsys):
    """Regression: adding reject must not change approval's behaviour."""
    run_id, _ = escalated_run(tmp_path)
    out = approve(tmp_path, capsys, run_id).out
    assert "RESUMABLE" in out
    assert not load(tmp_path, run_id).current_task.status.is_terminal


# ===========================================================================
# Permission and execution safety
# ===========================================================================

def test_rejection_executes_no_tools(tmp_path, capsys):
    run_id, tool = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)
    assert tool.calls == []


def test_rejection_constructs_no_agent(tmp_path, capsys, monkeypatch):
    run_id, _ = escalated_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("reject must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    reject(tmp_path, capsys, run_id)


def test_rejection_does_not_change_permissions(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    reject(tmp_path, capsys, run_id)

    agent = Agent(config_for(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(
                      state_store=JsonFileStateStore(tmp_path / "runs")))
    assert agent.policy.level is PermissionLevel.OBSERVE


# ===========================================================================
# Errors and read-only preflight
# ===========================================================================

def test_unknown_run_errors_cleanly(tmp_path, capsys):
    escalated_run(tmp_path)
    captured = reject(tmp_path, capsys, "run_unknown0001", expect=2)
    assert "cannot reject run 'run_unknown0001'" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_run_lists_available(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    captured = reject(tmp_path, capsys, "run_missing", expect=2)
    assert run_id in captured.err


def test_corrupt_run_errors_cleanly(tmp_path, capsys):
    name = corrupt_run(tmp_path)
    captured = reject(tmp_path, capsys, name, expect=2)
    assert "cannot reject" in captured.err
    assert "Traceback" not in captured.err


def test_missing_directory_errors_cleanly(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "nowhere"), "reject", "run_x"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no run directory" in captured.err


def test_run_without_pending_action_cannot_be_rejected(tmp_path, capsys):
    run_id, _ = completed_run(tmp_path)
    captured = reject(tmp_path, capsys, run_id, expect=2)
    assert "no pending action" in captured.err


def test_completed_run_is_not_altered_by_a_failed_rejection(tmp_path, capsys):
    run_id, _ = completed_run(tmp_path)
    before = load(tmp_path, run_id).current_task.status
    reject(tmp_path, capsys, run_id, expect=2)
    assert load(tmp_path, run_id).current_task.status == before


@pytest.mark.parametrize("flags", [
    ("run_unknown0002",),
    ("__RUN__", "--action-id", "act_bogus"),
])
def test_failed_validation_is_read_only(tmp_path, capsys, flags):
    run_id, _ = escalated_run(tmp_path)
    resolved = tuple(run_id if f == "__RUN__" else f for f in flags)
    before = digest(tmp_path)

    main(["--runs-dir", str(tmp_path / "runs"), "reject", *resolved])
    capsys.readouterr()

    assert digest(tmp_path) == before


def test_ambiguous_rejection_is_read_only(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.add_pending_action(PendingAction(description="second"))
    store.save(state)
    before = digest(tmp_path)

    reject(tmp_path, capsys, run_id, expect=2)
    assert digest(tmp_path) == before


def test_failed_validation_constructs_no_agent(tmp_path, capsys, monkeypatch):
    escalated_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("failed rejection must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    reject(tmp_path, capsys, "run_unknown0003", expect=2)
    reject(tmp_path, capsys, corrupt_run(tmp_path, "run_corrupt0002"),
           expect=2)


# ===========================================================================
# Output
# ===========================================================================

def test_json_output_shape(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    payload = json.loads(reject(tmp_path, capsys, run_id,
                                "--reason", "declined", "--json").out)

    assert payload["run_id"] == run_id
    assert payload["action_id"] == action_id
    assert payload["decision"] == "REJECTED"
    assert payload["reason"] == "declined"
    assert payload["resumable"] is False
    assert payload["remaining_pending_actions"] == 0


def test_json_schema_matches_approval(tmp_path, capsys):
    """Both decisions must use the same field set."""
    rejected_id, _ = escalated_run(tmp_path, "one")
    approved_id, _ = escalated_run(tmp_path, "two")

    rejected = json.loads(reject(tmp_path, capsys, rejected_id, "--json").out)
    approved = json.loads(approve(tmp_path, capsys, approved_id,
                                  "--json").out)
    assert set(rejected) == set(approved)


def test_json_and_human_agree(tmp_path, capsys):
    first, _ = escalated_run(tmp_path, "one")
    human = reject(tmp_path, capsys, first, "--reason", "no thanks").out

    second, _ = escalated_run(tmp_path, "two")
    payload = json.loads(reject(tmp_path, capsys, second,
                                "--reason", "no thanks", "--json").out)

    assert payload["decision"] in human
    assert payload["reason"] in human
    assert payload["resumable"] is False and "TERMINAL" in human


def test_output_hides_secrets(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path, secret=True)
    captured = reject(tmp_path, capsys, run_id)
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_json_output_hides_secrets(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path, secret=True)
    captured = reject(tmp_path, capsys, run_id, "--json")
    assert SECRET not in captured.out


def test_stdout_is_not_polluted_by_logs(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = reject(tmp_path, capsys, run_id).out
    assert out.startswith("Run:")


def test_json_stdout_is_parseable(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    json.loads(reject(tmp_path, capsys, run_id, "--json").out)


# ===========================================================================
# END-TO-END acceptance: escalation -> reject -> refused resume
# ===========================================================================

def test_end_to_end_escalation_reject_refused_resume(tmp_path, capsys):
    """The full refusal loop, through the real CLI."""
    # 1-2. a mutating action is refused at OBSERVE and the run escalates.
    run_id, tool = escalated_run(tmp_path, "record the launch date")

    # 3-4. a pending action exists and the run is terminal.
    before = load(tmp_path, run_id)
    assert len(before.pending_actions) == 1
    assert before.current_task.status.is_terminal
    action_id = before.pending_actions[0].id
    history_before = len(before.history)
    tool_calls_before = len(before.tool_calls)
    files_before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    assert tool.calls == []

    # 5. the operator explicitly refuses the specific action.
    out = reject(tmp_path, capsys, run_id, "--action-id", action_id,
                 "--operator", "alice", "--reason", "Operator declined").out
    assert "REJECTED" in out and action_id in out
    assert "Operator declined" in out
    assert "Resume:   NOT ALLOWED" in out

    # 6. the decision is audited and the action cleared.
    after = load(tmp_path, run_id)
    entry = decisions(tmp_path, run_id)[0]
    assert entry.data["decision"] == "REJECTED"
    assert entry.data["operator"] == "alice"
    assert entry.data["note"] == "Operator declined"
    assert entry.data["action_id"] == action_id
    assert after.pending_actions == []
    assert after.current_task.status.is_terminal

    # permissions untouched
    agent = Agent(config_for(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(
                      state_store=JsonFileStateStore(tmp_path / "runs")))
    assert agent.policy.level is PermissionLevel.OBSERVE

    # 7-8. resume is refused.
    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "--resume", run_id])
    err = capsys.readouterr().err
    assert code == 2
    assert "already finished" in err

    # 9. the rejected tool never ran.
    assert tool.calls == []

    # 10-11. no run was forked and prior history survives.
    assert after.run_id == run_id
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == files_before
    assert len(after.history) > history_before
    assert len(after.tool_calls) == tool_calls_before

    kinds = [h.kind for h in after.history]
    assert kinds.index("pending_action") < kinds.index("operator_decision")

    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    timeline = capsys.readouterr().out
    assert code == 0
    assert "escalation / pending action" in timeline
    assert "REJECTED" in timeline
    assert SECRET not in timeline
