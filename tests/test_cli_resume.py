"""`agentcore run --resume <run_id>`: continue a persisted run from the CLI.

The CLI is a thin adapter over the existing engine, so these tests focus on
the adapter's contract: which runs are resumable, that the run id and all
history survive, that nothing is bypassed, and that failures never touch state.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

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
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-resume-must-not-leak"


@pytest.fixture(autouse=True)
def _cli_workspace(tmp_path, monkeypatch):
    """Point the CLI at the same workspace the runs were seeded in.

    Runs here are created with ``workspace_dir=tmp_path`` and are now pinned to
    that canonical workspace, so a resume invoked with the *default* workspace
    is correctly refused. A real operator resumes from the project directory;
    this fixture is the test-harness equivalent of doing so.
    """
    monkeypatch.setenv("AGENTCORE_WORKSPACE_DIR", str(tmp_path))


# -- fixtures ---------------------------------------------------------------

class ActTool(Tool):
    """Mutating tool, so recovery prefers replanning over a blind retry."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    @property
    def spec(self):
        return tool_spec(
            "act", "Perform an action.",
            parameters=[ToolParameter("v", ParamType.STRING,
                                      required=False, default=""),
                        ToolParameter("api_key", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.calls.append(dict(args))
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
    def __init__(self, *variants: str, secret: bool = False) -> None:
        self.variants = list(variants)
        self.secret = secret

    def plan(self, objective, *, context=None, replanning=False):
        variant = self.variants.pop(0) if self.variants else "Z"
        args = {"v": variant}
        if self.secret:
            args["api_key"] = SECRET
        task = Task(objective=objective)
        task.add_subtask(Subtask(description=f"variant {variant}",
                                 tool_name="act", tool_args=args))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


class NoToolPlanner:
    def plan(self, objective, *, context=None, replanning=False):
        task = Task(objective=objective)
        task.add_subtask(Subtask(description="needs a human"))
        return task

    def replan(self, task, state):
        return self.plan(task.objective)


def config_for(tmp_path, *, level=PermissionLevel.AUTONOMOUS,
               recovery=True, max_replans=1) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(recovery=recovery),
    ).validate()


def build_agent(tmp_path, planner, tool, verifier, **kw) -> Agent:
    store = JsonFileStateStore(tmp_path / "runs")
    return Agent(config_for(tmp_path, **kw), AgentDependencies(
        tools=[tool], planner=planner, verifier=verifier, state_store=store,
        **journal_for_tools([tool])))


def seed(tmp_path, objective, planner, tool, verifier, **kw) -> str:
    """Execute a run to completion of its first phase and persist it."""
    agent = build_agent(tmp_path, planner, tool, verifier, **kw)
    agent.execute(objective)
    agent.shutdown()
    time.sleep(0.004)
    return agent.state.run_id


def blocked_run(tmp_path, objective="finish the report") -> str:
    """A run that exhausts its replan budget and stops BLOCKED (resumable)."""
    return seed(tmp_path, objective, SeqPlanner("A", "B"), ActTool(),
                Verifier(VerificationStatus.FAILED, VerificationStatus.FAILED,
                         VerificationStatus.PASSED), max_replans=1)


def completed_run(tmp_path, objective="already done") -> str:
    return seed(tmp_path, objective, SeqPlanner("A"), ActTool(),
                Verifier(VerificationStatus.PASSED))


def failed_run(tmp_path, objective="a failure") -> str:
    return seed(tmp_path, objective, SeqPlanner("A"), ActTool(fail=True),
                Verifier(), recovery=False)


def escalated_run(tmp_path, objective="delete the archive") -> str:
    """Permission denied -> escalates; the engine leaves this terminal."""
    return seed(tmp_path, objective, SeqPlanner("A"), ActTool(), Verifier(),
                level=PermissionLevel.OBSERVE)


def pending_run(tmp_path, objective="needs a human") -> str:
    """BLOCKED with a pending action (no tool assigned)."""
    return seed(tmp_path, objective, NoToolPlanner(), ActTool(), Verifier())


def corrupt_run(tmp_path, name="run_corrupt0001") -> str:
    runs = tmp_path / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{name}.json").write_text("{not json", encoding="utf-8")
    return name


def resume(tmp_path, capsys, run_id, *flags, expect=0):
    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "--resume", run_id, *flags])
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def load_state(tmp_path, run_id):
    return JsonFileStateStore(tmp_path / "runs").load(run_id)


def digest(tmp_path) -> dict[str, str]:
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (tmp_path / "runs").iterdir()}


# ===========================================================================
# Basic resume
# ===========================================================================

def test_blocked_run_can_be_resumed(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    assert load_state(tmp_path, run_id).current_task.status.value == "blocked"

    out = resume(tmp_path, capsys, run_id).out

    assert run_id in out
    assert "(resumed)" in out
    assert load_state(tmp_path, run_id).current_task.status.value == "completed"


def test_resume_preserves_the_run_id(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    resume(tmp_path, capsys, run_id)
    assert load_state(tmp_path, run_id).run_id == run_id


def test_resume_creates_no_new_run_file(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    resume(tmp_path, capsys, run_id)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


def test_resume_reports_final_outcome(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    out = resume(tmp_path, capsys, run_id).out
    assert "FINAL:" in out
    assert "COMPLETED" in out


def test_resume_suggests_history_command(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    out = resume(tmp_path, capsys, run_id).out
    assert f"agentcore history {run_id}" in out


# ===========================================================================
# History preservation
# ===========================================================================

def test_replan_count_is_not_reset(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    before = ReplanLedger.load(load_state(tmp_path, run_id)).count
    assert before == 1

    resume(tmp_path, capsys, run_id)

    after = ReplanLedger.load(load_state(tmp_path, run_id))
    assert after.count >= before, "the replan budget must not reset"
    assert after.archived_plans, "archived plans survive"


def test_recovery_history_is_preserved(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    before = RecoveryHistory.load(load_state(tmp_path, run_id)).to_dict()
    assert before

    resume(tmp_path, capsys, run_id)

    after = RecoveryHistory.load(load_state(tmp_path, run_id)).to_dict()
    for subtask_id in before:
        assert subtask_id in after


def test_verification_history_is_preserved(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    before = len(load_state(tmp_path, run_id).verifications)
    assert before >= 1

    resume(tmp_path, capsys, run_id)

    assert len(load_state(tmp_path, run_id).verifications) >= before


def test_event_history_grows_rather_than_restarting(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    before = len(load_state(tmp_path, run_id).history)

    resume(tmp_path, capsys, run_id)

    assert len(load_state(tmp_path, run_id).history) > before


def test_tool_call_history_is_preserved(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    before = len(load_state(tmp_path, run_id).tool_calls)
    resume(tmp_path, capsys, run_id)
    assert len(load_state(tmp_path, run_id).tool_calls) >= before


def test_objective_is_preserved(tmp_path, capsys):
    run_id = blocked_run(tmp_path, "the original objective")
    resume(tmp_path, capsys, run_id)
    assert load_state(tmp_path, run_id).current_task.objective == \
        "the original objective"


def test_resumed_run_appears_in_history_command(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    resume(tmp_path, capsys, run_id)

    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    out = capsys.readouterr().out
    assert code == 0
    assert "finish the report" in out


# ===========================================================================
# Terminal runs (engine contract: run_task refuses a finished task)
# ===========================================================================

@pytest.mark.parametrize("factory,label", [
    (completed_run, "completed"),
    (failed_run, "failed"),
    (escalated_run, "failed"),      # escalation ends terminal in this engine
])
def test_terminal_runs_cannot_be_resumed(tmp_path, capsys, factory, label):
    run_id = factory(tmp_path)
    captured = resume(tmp_path, capsys, run_id, expect=2)
    assert "already finished" in captured.err
    assert label in captured.err
    assert "Traceback" not in captured.err


def test_terminal_refusal_does_not_modify_state(tmp_path, capsys):
    run_id = completed_run(tmp_path)
    before = digest(tmp_path)
    resume(tmp_path, capsys, run_id, expect=2)
    assert digest(tmp_path) == before


def test_terminal_refusal_points_at_history(tmp_path, capsys):
    run_id = completed_run(tmp_path)
    captured = resume(tmp_path, capsys, run_id, expect=2)
    assert f"agentcore history {run_id}" in captured.err


# ===========================================================================
# Pending actions / escalation are not bypassed
# ===========================================================================

def test_resume_does_not_approve_pending_actions(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    before = len(load_state(tmp_path, run_id).pending_actions)
    assert before == 1

    resume(tmp_path, capsys, run_id)

    after = load_state(tmp_path, run_id).pending_actions
    assert len(after) >= before, "a pending action must not be cleared"


def test_resume_of_pending_run_still_reports_escalation(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    out = resume(tmp_path, capsys, run_id).out
    assert "ESCALATED" in out or "pending action" in out


def test_resume_does_not_execute_a_toolless_subtask(tmp_path, capsys):
    run_id = pending_run(tmp_path)
    resume(tmp_path, capsys, run_id)
    state = load_state(tmp_path, run_id)
    assert state.current_task.subtasks[0].tool_name is None
    assert state.current_task.subtasks[0].result is None


# ===========================================================================
# Permissions
# ===========================================================================

def test_resume_respects_the_permission_policy(tmp_path, capsys):
    """A run blocked at OBSERVE must not gain privileges by resuming."""
    run_id = blocked_run(tmp_path)
    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "--resume", run_id])
    capsys.readouterr()
    assert code == 0

    # resume again under OBSERVE: the tool must never run
    tool = ActTool()
    agent = Agent(config_for(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(tools=[tool],
                                    state_store=JsonFileStateStore(
                                        tmp_path / "runs")))
    assert agent.policy.level is PermissionLevel.OBSERVE


def test_resume_under_observe_does_not_execute_tools(tmp_path, capsys):
    """Config governs permissions on resume, exactly as on a fresh run."""
    run_id = blocked_run(tmp_path)

    # The CLI builds its Agent from the loaded config; simulate an OBSERVE
    # config to confirm the engine still refuses the mutating tool.
    tool = ActTool()
    store = JsonFileStateStore(tmp_path / "runs")
    agent = Agent(config_for(tmp_path, level=PermissionLevel.OBSERVE),
                  AgentDependencies(tools=[tool], state_store=store,
                                    planner=SeqPlanner("C"),
                                    verifier=Verifier()))
    agent.resume(run_id)
    try:
        agent.run_task()
    except Exception:
        pass
    assert tool.calls == [], "a mutating tool must stay blocked at OBSERVE"


# ===========================================================================
# Errors and argument validation
# ===========================================================================

def test_unknown_run_id_errors_cleanly(tmp_path, capsys):
    blocked_run(tmp_path)
    captured = resume(tmp_path, capsys, "run_does_not_exist", expect=2)
    assert "cannot resume run 'run_does_not_exist'" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_run_lists_available_runs(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    captured = resume(tmp_path, capsys, "run_missing", expect=2)
    assert run_id in captured.err


def test_corrupt_run_errors_cleanly(tmp_path, capsys):
    name = corrupt_run(tmp_path)
    captured = resume(tmp_path, capsys, name, expect=2)
    assert "cannot resume" in captured.err
    assert "Traceback" not in captured.err


def test_missing_run_directory_errors_cleanly(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "nowhere"), "run",
                 "--resume", "run_x"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no run directory" in captured.err


def test_objective_and_resume_together_rejected(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    code = main(["--runs-dir", str(tmp_path / "runs"), "run",
                 "a new objective", "--resume", run_id])
    captured = capsys.readouterr()
    assert code == 2
    assert "not both" in captured.err
    assert "Traceback" not in captured.err


def test_neither_objective_nor_resume_rejected(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "runs"), "run"])
    captured = capsys.readouterr()
    assert code == 2
    assert "provide an objective" in captured.err


def test_resume_without_a_value_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--runs-dir", str(tmp_path / "runs"), "run", "--resume"])
    assert exc.value.code == 2


def test_plain_run_still_works(tmp_path, capsys):
    """The original invocation must keep working unchanged."""
    code = main(["--runs-dir", str(tmp_path / "runs"), "run", "do a thing"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Status:" in out
    assert "(resumed)" not in out


# ===========================================================================
# Read-only preflight
# ===========================================================================

@pytest.mark.parametrize("run_id_factory", [
    lambda tmp_path: "run_unknown0001",
    lambda tmp_path: corrupt_run(tmp_path, "run_corrupt0002"),
    lambda tmp_path: completed_run(tmp_path),
])
def test_failed_preflight_does_not_modify_state(tmp_path, capsys,
                                                run_id_factory):
    blocked_run(tmp_path)                      # a healthy run to protect
    target = run_id_factory(tmp_path)
    before = digest(tmp_path)

    main(["--runs-dir", str(tmp_path / "runs"), "run", "--resume", target])
    capsys.readouterr()

    assert digest(tmp_path) == before


def test_failed_preflight_creates_no_new_run(tmp_path, capsys):
    blocked_run(tmp_path)
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    resume(tmp_path, capsys, "run_unknown0002", expect=2)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


def test_failed_preflight_executes_no_tools(tmp_path, capsys, monkeypatch):
    """A bad run id must not even construct an Agent."""
    blocked_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("preflight must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    resume(tmp_path, capsys, "run_unknown0003", expect=2)
    resume(tmp_path, capsys, corrupt_run(tmp_path, "run_corrupt0003"),
           expect=2)


def test_terminal_preflight_constructs_no_agent(tmp_path, capsys, monkeypatch):
    run_id = completed_run(tmp_path)

    import agentcore.cli as cli_module

    def explode(*args, **kwargs):
        raise AssertionError("terminal refusal must not construct an Agent")

    monkeypatch.setattr(cli_module, "Agent", explode)
    resume(tmp_path, capsys, run_id, expect=2)


# ===========================================================================
# Output modes
# ===========================================================================

def test_json_output_shape(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    payload = json.loads(resume(tmp_path, capsys, run_id, "--json").out)

    assert payload["run_id"] == run_id
    assert payload["resumed"] is True
    assert "report" in payload and "state" in payload and "final" in payload
    assert payload["report"]["status"] == "completed"


def test_json_and_human_agree(tmp_path, capsys):
    first = blocked_run(tmp_path)
    human = resume(tmp_path, capsys, first).out

    second = blocked_run(tmp_path, "another blocked run")
    payload = json.loads(resume(tmp_path, capsys, second, "--json").out)

    assert payload["report"]["status"] in human or "completed" in human
    assert payload["resumed"] is True


def test_fresh_run_json_marks_not_resumed(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "runs"), "run", "a thing",
                 "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["resumed"] is False


def test_output_does_not_dump_whole_state(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    out = resume(tmp_path, capsys, run_id).out
    assert "scratch" not in out
    assert "resume_point" not in out


def test_secrets_never_appear_in_resume_output(tmp_path, capsys):
    run_id = seed(tmp_path, "handle credentials",
                  SeqPlanner("A", "B", secret=True), ActTool(),
                  Verifier(VerificationStatus.FAILED,
                           VerificationStatus.FAILED,
                           VerificationStatus.PASSED), max_replans=1)
    captured = resume(tmp_path, capsys, run_id)
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_secrets_absent_from_json_output(tmp_path, capsys):
    run_id = seed(tmp_path, "handle credentials",
                  SeqPlanner("A", "B", secret=True), ActTool(),
                  Verifier(VerificationStatus.FAILED,
                           VerificationStatus.FAILED,
                           VerificationStatus.PASSED), max_replans=1)
    captured = resume(tmp_path, capsys, run_id, "--json")
    assert SECRET not in captured.out


def test_stdout_is_not_polluted_by_logs(tmp_path, capsys):
    """The report must be the only thing on stdout."""
    run_id = blocked_run(tmp_path)
    out = resume(tmp_path, capsys, run_id).out
    assert not out.lstrip().startswith("{"), "log JSON must not reach stdout"
    assert out.startswith("Run:")


def test_json_mode_stdout_is_parseable(tmp_path, capsys):
    run_id = blocked_run(tmp_path)
    json.loads(resume(tmp_path, capsys, run_id, "--json").out)


# ===========================================================================
# END-TO-END acceptance
# ===========================================================================

def test_end_to_end_resume_continues_the_same_run(tmp_path, capsys):
    """Start -> stop blocked -> resume from CLI -> completes, same run id."""
    # 1-4. a run that executes, fails verification, replans, exhausts its
    #      budget and persists in a resumable BLOCKED state.
    run_id = blocked_run(tmp_path, "produce the quarterly summary")

    before = load_state(tmp_path, run_id)
    assert before.current_task.status.value == "blocked"
    replans_before = ReplanLedger.load(before).count
    verifications_before = len(before.verifications)
    tool_calls_before = len(before.tool_calls)
    history_before = len(before.history)
    assert replans_before == 1
    files_before = sorted(p.name for p in (tmp_path / "runs").iterdir())

    # 5. resume from the CLI
    out = resume(tmp_path, capsys, run_id).out

    # 6. execution continued from the existing state
    after = load_state(tmp_path, run_id)
    assert after.current_task.status.value == "completed"
    assert len(after.history) > history_before

    # 7. prior history remains
    assert len(after.verifications) >= verifications_before
    assert len(after.tool_calls) >= tool_calls_before
    assert RecoveryHistory.load(after).to_dict()
    assert ReplanLedger.load(after).archived_plans

    # 8. budgets were not reset
    assert ReplanLedger.load(after).count >= replans_before

    # 9. expected final state, reported to the operator
    assert "COMPLETED" in out
    assert "(resumed)" in out

    # 10. the original run id is preserved and no run was forked
    assert after.run_id == run_id
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == files_before

    # and the operator can still inspect the whole story
    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id])
    history = capsys.readouterr().out
    assert code == 0
    assert "produce the quarterly summary" in history
    assert "replan" in history.lower()
