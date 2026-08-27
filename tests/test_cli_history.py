"""Read-only `agentcore history` / `agentcore runs` inspection commands."""

from __future__ import annotations

import json

import pytest
from conftest import journal_for_tools

from agentcore.cli import main, render_history
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
from agentcore.state.models import AgentState
from agentcore.state.store import JsonFileStateStore
from agentcore.tasks.models import (
    Subtask,
    Task,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)
from agentcore.tools.base import ParamType, Tool, ToolParameter, ToolResult, tool_spec

SECRET = "sk-cli-must-not-leak"


# -- fixtures ---------------------------------------------------------------

class ActTool(Tool):
    """Mutating tool taking a credential-shaped argument."""

    def __init__(self, *, fail_times: int = 0, name: str = "act",
                 calls: list[dict] | None = None) -> None:
        self._name = name
        self.calls = calls if calls is not None else []
        self.fail_times = fail_times

    @property
    def spec(self):
        return tool_spec(
            self._name, "Perform an action.",
            parameters=[ToolParameter("variant", ParamType.STRING,
                                      required=False, default=""),
                        ToolParameter("api_key", ParamType.STRING,
                                      required=False, default="")],
            capabilities=[Capability.FS_WRITE], read_only=False)

    def run(self, args, context):
        self.calls.append(dict(args))
        if self.fail_times > 0:
            self.fail_times -= 1
            return ToolResult.failure("temporary glitch", retryable=True)
        return ToolResult.success(f"did {args.get('variant')}")


class SafeTool(ActTool):
    """Read-only variant, so recovery treats a retry as safe."""

    @property
    def spec(self):
        return tool_spec(
            "act", "Perform a read-only action.",
            parameters=[ToolParameter("variant", ParamType.STRING,
                                      required=False, default="")],
            read_only=True)


class Verifier:
    def __init__(self, *statuses: VerificationStatus) -> None:
        self.statuses = list(statuses)

    def verify(self, subtask, state):
        status = (self.statuses.pop(0) if self.statuses
                  else VerificationStatus.PASSED)
        return VerificationResult(status=status,
                                  checked_by="DeterministicVerifier",
                                  detail=f"verifier said {status}")


class SeqPlanner:
    """Yields a plan per call from a variant sequence."""

    def __init__(self, *variants: str, secret: bool = True) -> None:
        self.variants = list(variants)
        self.secret = secret
        self.replans = 0

    def plan(self, objective, *, context=None, replanning=False):
        variant = self.variants.pop(0) if self.variants else "Z"
        tool_name = "act" if variant == "A" else f"act_{variant.lower()}"
        args = {"variant": variant}
        if self.secret:
            args["api_key"] = SECRET
        task = Task(objective=objective)
        task.add_subtask(Subtask(description=f"perform variant {variant}",
                                 tool_name=tool_name, tool_args=args))
        return task

    def replan(self, task, state):
        self.replans += 1
        return self.plan(task.objective)


def config_for(tmp_path, *, max_replans=2,
               level=PermissionLevel.AUTONOMOUS) -> AgentConfig:
    return AgentConfig(
        environment="test", workspace_dir=str(tmp_path),
        model=ModelConfig(),
        permissions=PermissionConfig(level=level),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=max_replans,
                                base_delay_seconds=0.0),
        features=FeatureFlags(recovery=True),
    ).validate()


def make_run(tmp_path, *, planner=None, tool=None, verifier=None,
             max_replans=2, level=PermissionLevel.AUTONOMOUS,
             objective="record the launch date") -> tuple[str, object]:
    """Execute a run against a JSON store and return (run_id, tool)."""
    store = JsonFileStateStore(tmp_path / "runs")
    tool = tool if tool is not None else ActTool()
    tools = [tool]
    if isinstance(tool, ActTool) and not isinstance(tool, SafeTool):
        tools.extend(
            ActTool(name=f"act_{suffix}", calls=tool.calls)
            for suffix in ("b", "c", "z")
        )
    agent = Agent(config_for(tmp_path, max_replans=max_replans, level=level),
                  AgentDependencies(
                      tools=tools,
                      planner=planner or SeqPlanner("A"),
                      verifier=verifier or Verifier(VerificationStatus.PASSED),
                      state_store=store,
                      **journal_for_tools(
                          tools, expose_bounded_text_result=True)))
    agent.execute(objective)
    agent.shutdown()
    return agent.state.run_id, tool


def run_history(tmp_path, run_id, capsys, *, extra=()) -> str:
    code = main(["--runs-dir", str(tmp_path / "runs"), "history", run_id,
                 *extra])
    out = capsys.readouterr().out
    assert code == 0, out
    return out


# ===========================================================================
# Successful run
# ===========================================================================

def test_successful_run_displays(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)

    assert run_id in out
    assert "record the launch date" in out
    assert "PLAN" in out and "TIMELINE" in out and "FINAL" in out
    assert "COMPLETED" in out


def test_header_reports_status_and_phase(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)
    assert "Status:" in out
    assert "phase: done" in out


def test_plan_section_lists_subtasks_and_tools(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)
    assert "perform variant A" in out
    assert "tool=act" in out


def test_timeline_shows_tool_execution_and_output(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)
    assert "tool 'act' -> ok" in out
    assert "output: did A" in out


# ===========================================================================
# Verification display
# ===========================================================================

@pytest.mark.parametrize("status,label", [
    (VerificationStatus.PASSED, "verification PASSED"),
    (VerificationStatus.FAILED, "verification FAILED"),
    (VerificationStatus.UNVERIFIED, "verification UNVERIFIED"),
    (VerificationStatus.SKIPPED, "verification SKIPPED"),
])
def test_verification_statuses_display(tmp_path, capsys, status, label):
    run_id, _ = make_run(tmp_path, verifier=Verifier(status),
                         max_replans=0)
    out = run_history(tmp_path, run_id, capsys)
    assert label in out
    assert "checked by: DeterministicVerifier" in out


def test_verification_reason_displayed(tmp_path, capsys):
    run_id, _ = make_run(tmp_path, verifier=Verifier(VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)
    assert "reason: verifier said passed" in out


# ===========================================================================
# Retry / recovery
# ===========================================================================

def test_retry_is_displayed(tmp_path, capsys):
    """A transient failure on a read-only tool produces a RETRY decision."""
    run_id, tool = make_run(tmp_path, tool=SafeTool(fail_times=1),
                            planner=SeqPlanner("A", "B", secret=False))
    out = run_history(tmp_path, run_id, capsys)

    assert "recovery decision: RETRY" in out
    assert "category: transient" in out
    assert "tool 'act' -> failed" in out
    assert "error: temporary glitch" in out


def test_recovery_replan_decision_displayed(tmp_path, capsys):
    run_id, _ = make_run(
        tmp_path, planner=SeqPlanner("A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)
    assert "recovery decision: REPLAN" in out
    assert "category: verification_failed" in out
    assert "retry safety:" in out


# ===========================================================================
# Replan and rejected proposals
# ===========================================================================

def test_replan_cycle_displayed(tmp_path, capsys):
    run_id, _ = make_run(
        tmp_path, planner=SeqPlanner("A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)

    assert "replan #1 accepted" in out
    assert "new plan: task_" in out
    assert "Replans:    1" in out


def test_rejected_proposal_marked_not_executed(tmp_path, capsys):
    """A duplicate proposal must never look like it ran."""
    run_id, tool = make_run(
        tmp_path, planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)

    assert "rejected proposal #1" in out
    assert "duplicate of an already-failed strategy" in out
    assert "EXECUTED: NO — refused before execution" in out
    assert "rejection reason:" in out
    # the duplicate genuinely did not run
    assert [c["variant"] for c in tool.calls] == ["A", "B"]


def test_rejected_proposal_is_not_a_tool_call(tmp_path, capsys):
    run_id, _ = make_run(
        tmp_path, planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)
    # exactly two executions were recorded, not three
    assert out.count("tool 'act' -> ok") == 1
    assert out.count("tool 'act_b' -> ok") == 1


def test_rejected_proposal_shows_safe_strategy(tmp_path, capsys):
    run_id, _ = make_run(
        tmp_path, planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)
    assert "proposed strategy: act" in out
    assert SECRET not in out


# ===========================================================================
# Escalation
# ===========================================================================

def test_escalation_displayed(tmp_path, capsys):
    """A permission denial escalates and appears as a pending action."""
    run_id, tool = make_run(tmp_path, level=PermissionLevel.OBSERVE,
                            max_replans=0)
    out = run_history(tmp_path, run_id, capsys)

    assert tool.calls == []
    assert "escalation / pending action" in out
    assert "ESCALATED" in out


def test_final_line_reports_outcome(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)
    final = out[out.index("FINAL"):]
    assert "COMPLETED" in final


# ===========================================================================
# Ordering
# ===========================================================================

def test_timeline_is_chronological(tmp_path, capsys):
    run_id, _ = make_run(
        tmp_path, planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)

    first_exec = out.index("tool 'act' -> ok")
    failed_verify = out.index("verification FAILED")
    recovery = out.index("recovery decision: REPLAN")
    rejection = out.index("rejected proposal #1")
    passed_verify = out.index("verification PASSED")
    assert first_exec < failed_verify < recovery < rejection < passed_verify


def test_events_are_numbered(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)
    assert "  01. [" in out
    assert "  02. [" in out


# ===========================================================================
# Redaction and safety
# ===========================================================================

def test_secret_never_appears_in_output(tmp_path, capsys):
    run_id, tool = make_run(
        tmp_path, planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    out = run_history(tmp_path, run_id, capsys)

    # the secret really was used during execution...
    assert tool.calls[0]["api_key"] == SECRET
    # ...but never printed
    assert SECRET not in out
    assert '"api_key": "***"' in out


def test_secret_absent_from_json_mode(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys, extra=["--json"])
    assert SECRET not in out


def test_raw_state_is_not_dumped(tmp_path, capsys):
    """The command prints a report, not the serialised state."""
    run_id, _ = make_run(tmp_path)
    out = run_history(tmp_path, run_id, capsys)
    assert '"scratch"' not in out
    assert '"resume_point"' not in out


# ===========================================================================
# Read-only guarantee
# ===========================================================================

def test_command_does_not_modify_state(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    path = tmp_path / "runs" / f"{run_id}.json"
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    run_history(tmp_path, run_id, capsys)
    run_history(tmp_path, run_id, capsys, extra=["--json"])

    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


def test_no_new_run_files_created(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    before = sorted(p.name for p in (tmp_path / "runs").iterdir())
    run_history(tmp_path, run_id, capsys)
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == before


# ===========================================================================
# Errors: missing, corrupt, legacy
# ===========================================================================

def test_unknown_run_id_errors_cleanly(tmp_path, capsys):
    make_run(tmp_path)
    code = main(["--runs-dir", str(tmp_path / "runs"), "history", "run_nope"])
    captured = capsys.readouterr()
    assert code == 2
    assert "cannot read run 'run_nope'" in captured.err
    assert "Traceback" not in captured.err


def test_unknown_run_lists_available(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    main(["--runs-dir", str(tmp_path / "runs"), "history", "run_missing"])
    assert run_id in capsys.readouterr().err


def test_missing_directory_errors_cleanly(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "nowhere"), "history", "run_x"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no run directory" in captured.err


def test_corrupt_state_errors_cleanly(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    (runs / "run_bad.json").write_text("{not json", encoding="utf-8")

    code = main(["--runs-dir", str(runs), "history", "run_bad"])
    captured = capsys.readouterr()
    assert code == 2
    assert "cannot read run 'run_bad'" in captured.err
    assert "Traceback" not in captured.err


def test_structurally_invalid_state_errors_cleanly(tmp_path, capsys):
    """Valid JSON but not a state document."""
    runs = tmp_path / "runs"
    runs.mkdir(parents=True)
    (runs / "run_odd.json").write_text('{"unexpected": true}', encoding="utf-8")

    code = main(["--runs-dir", str(runs), "history", "run_odd"])
    assert code in (0, 2)          # must not raise
    assert "Traceback" not in capsys.readouterr().err


def test_legacy_state_without_scratch_renders(tmp_path):
    """Old runs predating the replan/recovery ledgers still render."""
    state = AgentState()
    task = Task(objective="legacy objective")
    task.add_subtask(Subtask(description="do it", tool_name="act"))
    state.set_task(task)
    state.scratch.clear()

    out = render_history(state)
    assert "legacy objective" in out
    assert "TIMELINE" in out


def test_state_with_no_task_renders():
    out = render_history(AgentState())
    assert "(none recorded)" in out
    assert "(no events recorded)" in out


@pytest.mark.parametrize("corrupt", ["nope", 123, [], {"records": "bad"}])
def test_corrupt_scratch_does_not_break_rendering(corrupt):
    state = AgentState()
    task = Task(objective="o")
    task.add_subtask(Subtask(description="d", tool_name="act"))
    state.set_task(task)
    state.scratch["replan"] = corrupt
    state.scratch["recovery"] = corrupt
    assert "TIMELINE" in render_history(state)


# ===========================================================================
# JSON mode and runs listing
# ===========================================================================

def test_json_mode_structure(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    payload = json.loads(run_history(tmp_path, run_id, capsys,
                                     extra=["--json"]))

    assert payload["run_id"] == run_id
    assert payload["objective"] == "record the launch date"
    assert isinstance(payload["timeline"], list)
    assert {"kind", "at", "title", "details"} <= set(payload["timeline"][0])
    assert "COMPLETED" in payload["final"]


def test_json_mode_marks_rejected_proposals(tmp_path, capsys):
    run_id, _ = make_run(
        tmp_path, planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED))
    payload = json.loads(run_history(tmp_path, run_id, capsys,
                                     extra=["--json"]))
    kinds = [e["kind"] for e in payload["timeline"]]
    assert "rejected_proposal" in kinds
    rejected = next(e for e in payload["timeline"]
                    if e["kind"] == "rejected_proposal")
    assert any("EXECUTED: NO" in d for d in rejected["details"])


def test_runs_command_lists_saved_runs(tmp_path, capsys):
    """The listing is now a human-readable summary table (see task 11)."""
    run_id, _ = make_run(tmp_path)
    code = main(["--runs-dir", str(tmp_path / "runs"), "runs"])
    out = capsys.readouterr().out
    assert code == 0
    assert run_id in out
    assert "RUN ID" in out


def test_runs_command_json_lists_saved_runs(tmp_path, capsys):
    run_id, _ = make_run(tmp_path)
    code = main(["--runs-dir", str(tmp_path / "runs"), "runs", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [s["run_id"] for s in payload] == [run_id]


def test_runs_command_on_empty_directory(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "empty"), "runs"])
    assert code == 0
    assert "(no runs found)" in capsys.readouterr().out


def test_runs_command_json_on_empty_directory(tmp_path, capsys):
    code = main(["--runs-dir", str(tmp_path / "empty"), "runs", "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


# ===========================================================================
# `run` persists so history can read it
# ===========================================================================

def test_run_command_persists_and_history_reads_it(tmp_path, capsys):
    runs = tmp_path / "runs"
    code = main(["--runs-dir", str(runs), "run", "do a thing", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    run_id = payload["run_id"]
    assert (runs / f"{run_id}.json").exists()

    out = run_history(tmp_path, run_id, capsys)
    assert "do a thing" in out


# ===========================================================================
# END-TO-END acceptance: the whole story, in order
# ===========================================================================

def test_end_to_end_history_tells_the_full_story(tmp_path, capsys):
    """plan -> execute -> FAILED verify -> recover -> duplicate rejected
    -> replan -> execute -> PASSED verify -> completed."""
    run_id, tool = make_run(
        tmp_path,
        planner=SeqPlanner("A", "A", "B"),
        verifier=Verifier(VerificationStatus.FAILED,
                          VerificationStatus.PASSED),
        max_replans=2)

    out = run_history(tmp_path, run_id, capsys)

    # header
    assert run_id in out
    assert "record the launch date" in out
    assert "Replans:    2" in out

    # 1. first attempt executed
    assert "tool 'act' -> ok" in out
    assert "output: did A" in out
    # 2. verification failed
    assert "verification FAILED" in out
    # 3. recovery asked for a new approach
    assert "recovery decision: REPLAN" in out
    # 4. the duplicate proposal was refused, not executed
    assert "rejected proposal #1" in out
    assert "EXECUTED: NO — refused before execution" in out
    # 5. a new plan was accepted and executed
    assert "replan #2 accepted" in out
    assert "output: did B" in out
    # 6. it verified and the run completed
    assert "verification PASSED" in out
    assert "COMPLETED — the task finished" in out

    # ordering holds end to end
    order = [out.index(marker) for marker in (
        "output: did A", "verification FAILED", "recovery decision: REPLAN",
        "rejected proposal #1", "output: did B", "verification PASSED")]
    assert order == sorted(order)

    # only two executions really happened
    assert [c["variant"] for c in tool.calls] == ["A", "B"]
    # and nothing sensitive leaked
    assert SECRET not in out
