"""`agentcore approve <run_id>`: the operator decision → resume bridge.

Approval records a human decision and makes an escalated run resumable. It
never executes the action and never grants permission — the engine re-evaluates
everything on resume.
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
from agentcore.state.models import AgentPhase, PendingAction
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-approve-must-not-leak"


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
    """Plans a step with no tool -> the runner escalates (task BLOCKED)."""

    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="a step needing a human"))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


def config_for(tmp_path, *, level=PermissionLevel.AUTONOMOUS,
               max_replans=0) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(recovery=True),
    ).validate()


def seed(tmp_path, objective, *, planner, tool=None, verifier=None,
         level=PermissionLevel.AUTONOMOUS) -> tuple[str, object]:
    store = JsonFileStateStore(tmp_path / "runs")
    tool = tool if tool is not None else WriteTool()
    agent = Agent(config_for(tmp_path, level=level), AgentDependencies(
        tools=[tool], planner=planner, verifier=verifier or PassVerifier(),
        state_store=store, **journal_for_tools([tool])))
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.004)
    return agent.state.run_id, tool


def escalated_run(tmp_path, objective="record a note", *, secret=False):
    """Permission denial -> terminal FAILED run with one pending action."""
    return seed(tmp_path, objective,
                planner=WritePlanner(secret=secret),
                level=PermissionLevel.OBSERVE)


def blocked_run(tmp_path, objective="needs a human"):
    """Tool-less step -> BLOCKED (non-terminal) with one pending action."""
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


def approve(tmp_path, capsys, run_id, *flags, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), "approve", run_id,
                 *flags])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def digest(tmp_path) -> dict[str, str]:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Basic approval
# ===========================================================================

def test_escalated_run_has_a_pending_action(tmp_path):
    run_id, tool = escalated_run(tmp_path)
    state = load(tmp_path, run_id)
    assert state.current_task.status.is_terminal
    assert len(state.pending_actions) == 1
    assert tool.calls == [], "the denied tool never ran"


def test_approval_succeeds(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = approve(tmp_path, capsys, run_id).out
    assert run_id in out
    assert "APPROVED" in out
    assert "RESUMABLE" in out


def test_approval_clears_the_pending_action(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    assert load(tmp_path, run_id).pending_actions == []


def test_approval_makes_the_run_resumable(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    assert load(tmp_path, run_id).current_task.status.is_terminal

    approve(tmp_path, capsys, run_id)

    state = load(tmp_path, run_id)
    assert not state.current_task.status.is_terminal
    assert state.current_task.status is TaskStatus.PENDING
    assert state.phase is AgentPhase.RECOVERING


def test_approval_preserves_the_run_id(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    assert load(tmp_path, run_id).run_id == run_id


def test_approval_creates_no_new_run(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    approve(tmp_path, capsys, run_id)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


def test_approval_suggests_the_resume_command(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = approve(tmp_path, capsys, run_id).out
    assert f"agentcore run --resume {run_id}" in out


def test_blocked_run_can_also_be_approved(tmp_path, capsys):
    """A non-terminal BLOCKED run with a pending action is also eligible."""
    run_id, _ = blocked_run(tmp_path)
    out = approve(tmp_path, capsys, run_id).out
    assert "APPROVED" in out
    assert load(tmp_path, run_id).pending_actions == []


# ===========================================================================
# Action id handling
# ===========================================================================

def test_explicit_action_id_is_accepted(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    out = approve(tmp_path, capsys, run_id, "--action-id", action_id).out
    assert action_id in out


def test_wrong_action_id_is_rejected(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    captured = approve(tmp_path, capsys, run_id,
                       "--action-id", "act_wrong", expect=2)
    assert "no pending action with id 'act_wrong'" in captured.err
    assert "Traceback" not in captured.err


def test_wrong_action_id_lists_available_actions(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    captured = approve(tmp_path, capsys, run_id,
                       "--action-id", "act_nope", expect=2)
    assert action_id in captured.err


def test_multiple_actions_require_explicit_selection(tmp_path, capsys):
    """Never guess, and never approve them all."""
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.add_pending_action(PendingAction(description="a second decision"))
    store.save(state)
    assert len(load(tmp_path, run_id).pending_actions) == 2

    captured = approve(tmp_path, capsys, run_id, expect=2)
    assert "2 pending actions" in captured.err
    assert "--action-id" in captured.err
    # nothing was approved
    assert len(load(tmp_path, run_id).pending_actions) == 2


def test_multiple_actions_can_be_approved_one_at_a_time(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    first = state.pending_actions[0].id
    extra = state.add_pending_action(PendingAction(description="second"))
    store.save(state)

    out = approve(tmp_path, capsys, run_id, "--action-id", first).out
    assert "1 pending action(s) still awaiting" in out
    remaining = load(tmp_path, run_id).pending_actions
    assert [a.id for a in remaining] == [extra.id]

    approve(tmp_path, capsys, run_id, "--action-id", extra.id)
    assert load(tmp_path, run_id).pending_actions == []


def test_already_resolved_action_is_rejected(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    approve(tmp_path, capsys, run_id, "--action-id", action_id)

    captured = approve(tmp_path, capsys, run_id,
                       "--action-id", action_id, expect=2)
    assert "no pending action" in captured.err


# ===========================================================================
# Audit history
# ===========================================================================

def test_decision_is_recorded_in_history(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    approve(tmp_path, capsys, run_id, "--action-id", action_id)

    entries = [h for h in load(tmp_path, run_id).history
               if h.kind == "operator_decision"]
    assert len(entries) == 1
    data = entries[0].data
    assert data["decision"] == "APPROVED"
    assert data["operator"] == "operator"
    assert data["action_id"] == action_id


def test_decision_records_a_timestamp(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    entry = next(h for h in load(tmp_path, run_id).history
                 if h.kind == "operator_decision")
    assert entry.at is not None


def test_custom_operator_and_note_are_recorded(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id, "--operator", "alice",
            "--note", "reviewed the diff")
    data = next(h for h in load(tmp_path, run_id).history
                if h.kind == "operator_decision").data
    assert data["operator"] == "alice"
    assert data["note"] == "reviewed the diff"


def test_decision_never_records_tool_arguments(tmp_path, capsys):
    """An audit record must not carry potentially sensitive values."""
    run_id, _ = escalated_run(tmp_path, secret=True)
    approve(tmp_path, capsys, run_id)
    entry = next(h for h in load(tmp_path, run_id).history
                 if h.kind == "operator_decision")
    assert "tool_args" not in entry.data
    assert SECRET not in json.dumps(entry.to_dict())


def test_decision_appears_in_history_command(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)

    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    out = capsys.readouterr().out
    assert code == 0
    assert "operator_decision" in out
    assert "APPROVED" in out


def test_prior_history_is_preserved(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    before = load(tmp_path, run_id)
    history_before = len(before.history)
    tool_calls_before = len(before.tool_calls)
    errors_before = len(before.errors)

    approve(tmp_path, capsys, run_id)

    after = load(tmp_path, run_id)
    assert len(after.history) > history_before
    assert len(after.tool_calls) == tool_calls_before
    assert len(after.errors) == errors_before


def test_counters_are_not_reset(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    before_replans = ReplanLedger.load(load(tmp_path, run_id)).count
    before_recovery = RecoveryHistory.load(load(tmp_path, run_id)).to_dict()

    approve(tmp_path, capsys, run_id)

    after = load(tmp_path, run_id)
    assert ReplanLedger.load(after).count == before_replans
    assert RecoveryHistory.load(after).to_dict() == before_recovery


# ===========================================================================
# Permission safety
# ===========================================================================

def test_approval_does_not_execute_the_action(tmp_path, capsys):
    run_id, tool = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)
    assert tool.calls == [], "approve must execute zero tools"


def test_approval_constructs_no_agent(tmp_path, capsys, monkeypatch):
    run_id, _ = escalated_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("approve must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    approve(tmp_path, capsys, run_id)


def test_approval_does_not_change_the_permission_level(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)

    agent = Agent(config_for(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(
                      state_store=JsonFileStateStore(tmp_path / "runs")))
    assert agent.policy.level is PermissionLevel.OBSERVE


def test_resume_after_approval_still_enforces_permissions(tmp_path, capsys):
    """The key safety property: approval authorises continuation, not access.

    Resuming at OBSERVE must still refuse the mutating tool.
    """
    run_id, tool = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)

    store = JsonFileStateStore(tmp_path / "runs")
    agent = Agent(config_for(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(tools=[tool], planner=WritePlanner(),
                                    verifier=PassVerifier(),
                                    state_store=store,
                                    **journal_for_tools([tool])))
    agent.resume(run_id)
    agent.run_task()

    assert tool.calls == [], "the tool must still be blocked at OBSERVE"
    assert agent.policy.level is PermissionLevel.OBSERVE


def test_resume_after_approval_proceeds_when_policy_allows(tmp_path, capsys):
    """With a permitting policy the normal engine executes the work."""
    run_id, tool = escalated_run(tmp_path)
    approve(tmp_path, capsys, run_id)

    store = JsonFileStateStore(tmp_path / "runs")
    agent = Agent(config_for(tmp_path, level=PermissionLevel.AUTONOMOUS),
                  AgentDependencies(tools=[tool], planner=WritePlanner(),
                                    verifier=PassVerifier(),
                                    state_store=store,
                                    **journal_for_tools([tool])))
    agent.resume(run_id)
    report = agent.run_task()

    assert tool.calls, "the tool now runs through the normal executor"
    assert report.status is TaskStatus.COMPLETED


# ===========================================================================
# Terminal-run safety
# ===========================================================================

def test_completed_run_cannot_be_approved(tmp_path, capsys):
    run_id, _ = completed_run(tmp_path)
    assert load(tmp_path, run_id).pending_actions == []
    captured = approve(tmp_path, capsys, run_id, expect=2)
    assert "no pending action" in captured.err


def test_completed_run_is_not_revived(tmp_path, capsys):
    run_id, _ = completed_run(tmp_path)
    before = load(tmp_path, run_id).current_task.status
    approve(tmp_path, capsys, run_id, expect=2)
    assert load(tmp_path, run_id).current_task.status == before


def test_failed_run_without_pending_action_cannot_be_approved(tmp_path,
                                                              capsys):
    """A terminal run only becomes resumable via a genuine pending action."""
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.pending_actions.clear()          # a plain failure, no escalation
    store.save(state)

    captured = approve(tmp_path, capsys, run_id, expect=2)
    assert "no pending action" in captured.err
    assert load(tmp_path, run_id).current_task.status.is_terminal


# ===========================================================================
# Errors and read-only preflight
# ===========================================================================

def test_unknown_run_errors_cleanly(tmp_path, capsys):
    escalated_run(tmp_path)
    captured = approve(tmp_path, capsys, "run_unknown0001", expect=2)
    assert "cannot approve run 'run_unknown0001'" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_run_lists_available(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    captured = approve(tmp_path, capsys, "run_missing", expect=2)
    assert run_id in captured.err


def test_corrupt_run_errors_cleanly(tmp_path, capsys):
    name = corrupt_run(tmp_path)
    captured = approve(tmp_path, capsys, name, expect=2)
    assert "cannot approve" in captured.err
    assert "Traceback" not in captured.err


def test_missing_directory_errors_cleanly(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "nowhere"), "approve", "run_x"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no run directory" in captured.err


def test_missing_run_id_argument_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "approve"])
    assert exc.value.code == 2


@pytest.mark.parametrize("flags", [
    ("run_unknown0002",),
    ("__RUN__", "--action-id", "act_bogus"),
])
def test_failed_validation_is_read_only(tmp_path, capsys, flags):
    run_id, _ = escalated_run(tmp_path)
    resolved = tuple(run_id if f == "__RUN__" else f for f in flags)
    before = digest(tmp_path)

    main(["--runs-dir", str(tmp_path / "runs"), "approve", *resolved])
    capsys.readouterr()

    assert digest(tmp_path) == before


def test_multiple_action_refusal_is_read_only(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    store = JsonFileStateStore(tmp_path / "runs")
    state = store.load(run_id)
    state.add_pending_action(PendingAction(description="second"))
    store.save(state)
    before = digest(tmp_path)

    approve(tmp_path, capsys, run_id, expect=2)
    assert digest(tmp_path) == before


def test_failed_validation_constructs_no_agent(tmp_path, capsys, monkeypatch):
    escalated_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("failed approval must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    approve(tmp_path, capsys, "run_unknown0003", expect=2)
    approve(tmp_path, capsys, corrupt_run(tmp_path, "run_corrupt0002"),
            expect=2)


# ===========================================================================
# Output
# ===========================================================================

def test_json_output_shape(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    action_id = action_id_of(tmp_path, run_id)
    payload = json.loads(approve(tmp_path, capsys, run_id, "--json").out)

    assert payload["run_id"] == run_id
    assert payload["action_id"] == action_id
    assert payload["decision"] == "APPROVED"
    assert payload["resumable"] is True
    assert payload["remaining_pending_actions"] == 0


def test_json_and_human_agree(tmp_path, capsys):
    first, _ = escalated_run(tmp_path)
    human = approve(tmp_path, capsys, first).out

    second, _ = escalated_run(tmp_path, "another note")
    payload = json.loads(approve(tmp_path, capsys, second, "--json").out)

    assert payload["decision"] in human
    assert "RESUMABLE" in human and payload["resumable"] is True


def test_output_hides_secrets(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path, secret=True)
    captured = approve(tmp_path, capsys, run_id)
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_json_output_hides_secrets(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path, secret=True)
    captured = approve(tmp_path, capsys, run_id, "--json")
    assert SECRET not in captured.out


def test_stdout_is_not_polluted_by_logs(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    out = approve(tmp_path, capsys, run_id).out
    assert out.startswith("Run:")


def test_json_stdout_is_parseable(tmp_path, capsys):
    run_id, _ = escalated_run(tmp_path)
    json.loads(approve(tmp_path, capsys, run_id, "--json").out)


# ===========================================================================
# END-TO-END acceptance: escalation -> approve -> resume
# ===========================================================================

def test_end_to_end_escalation_approve_resume(tmp_path, capsys):
    """The full operator loop, through the real CLI and engine."""
    # 1-3. a mutating action is refused at OBSERVE and the run escalates.
    run_id, tool = escalated_run(tmp_path, "record the launch date")

    # 4. it is terminal and carries exactly one pending action.
    before = load(tmp_path, run_id)
    assert before.current_task.status.is_terminal
    assert len(before.pending_actions) == 1
    assert tool.calls == []
    action_id = before.pending_actions[0].id
    history_before = len(before.history)
    files_before = sorted(p.name for p in (tmp_path / "runs").iterdir())

    # resume is refused while the run is terminal
    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "--resume", run_id])
    assert code == 2
    assert "already finished" in capsys.readouterr().err

    # 5-6. the operator approves the specific action; it is audited.
    out = approve(tmp_path, capsys, run_id, "--action-id", action_id,
                  "--operator", "alice", "--note", "checked manually").out
    assert "APPROVED" in out and action_id in out

    approved = load(tmp_path, run_id)
    decision = next(h for h in approved.history
                    if h.kind == "operator_decision")
    assert decision.data["operator"] == "alice"
    assert decision.data["action_id"] == action_id
    assert approved.pending_actions == []

    # 7. the run is now resumable, with all prior history intact.
    assert not approved.current_task.status.is_terminal
    assert len(approved.history) > history_before
    assert approved.run_id == run_id

    # approve executed nothing
    assert tool.calls == []

    # 8-9. resume through the normal engine, with a policy that permits it.
    store = JsonFileStateStore(tmp_path / "runs")
    agent = Agent(config_for(tmp_path, level=PermissionLevel.AUTONOMOUS),
                  AgentDependencies(tools=[tool], planner=WritePlanner(),
                                    verifier=PassVerifier(),
                                    state_store=store,
                                    **journal_for_tools([tool])))
    agent.resume(run_id)
    report = agent.run_task()
    agent.shutdown()

    # 10-12. the action ran through the executor, verified, and completed.
    assert tool.calls, "the approved action executed via ToolExecutor"
    assert report.status is TaskStatus.COMPLETED
    final = load(tmp_path, run_id)
    assert final.current_task.subtasks[0].verification.status is \
        VerificationStatus.PASSED

    # 13. the complete story survives, in one run, in order.
    assert final.run_id == run_id
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == files_before
    kinds = [h.kind for h in final.history]
    assert "pending_action" in kinds
    assert "operator_decision" in kinds
    assert kinds.index("pending_action") < kinds.index("operator_decision")

    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    timeline = capsys.readouterr().out
    assert code == 0
    assert "escalation / pending action" in timeline
    assert "APPROVED" in timeline
    assert "verification PASSED" in timeline
    assert SECRET not in timeline
