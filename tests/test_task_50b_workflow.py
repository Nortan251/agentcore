"""Synthetic planner/UI; real executor, journal, stores and native Windows I/O."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from agentcore.approvals import HumanApprovalDecision
from agentcore.config.schema import AgentConfig
from agentcore.permissions.config import PermissionConfig
from agentcore.permissions.levels import Capability, PermissionLevel
from agentcore.permissions.policy import PermissionPolicy
from agentcore.tools.builtin.write_file import WriteFileTool
from agentcore.workspace_host.errors import WorkspaceHostError
from agentcore.workspace_host.workflow import SupervisedWorkspaceWorkflow, WorkflowProfile
from agentcore.workspace_host import workflow_cli

BODY = "private-body-é\nexact second line\n"
NATIVE = pytest.mark.skipif(os.name != "nt", reason="native Windows publication only")


def plan():
    steps = []
    for index, (tool, args) in enumerate([
        ("inspect", {"path": "."}),
        ("read_file", {"path": "input.txt"}),
        ("write_file", {"path": "out.txt", "content": ""}),
        ("read_file", {"path": "out.txt"}),
    ]):
        steps.append({"id": f"s{index}", "description": tool, "tool_name": tool,
            "tool_args": args, "depends_on": [f"s{index - 1}"] if index else [],
            "expected_result": "", "requires_verification": True})
    return {"objective": "Inspect input and create output", "steps": steps}


class Model:
    def __init__(self, body=BODY, proposal=None):
        self.body, self.proposal = body, proposal or plan()
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return json.dumps(self.proposal) if len(self.calls) == 1 else self.body


class Human:
    accept = True
    decision = HumanApprovalDecision.APPROVE

    def __init__(self):
        self.plans, self.previews, self.requests = [], [], []

    def accept_plan(self, review):
        self.plans.append(review)
        return self.accept

    def approve_create(self, preview, prompt):
        self.previews.append(preview)
        self.requests.append(prompt.request_id)
        return self.decision

    def accept_verification(self, review):
        return True


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "input.txt").write_bytes(b"Declared input only\n")
    config = AgentConfig(workspace_dir=str(root), permissions=PermissionConfig(level=PermissionLevel.CONFIRM))
    private = tmp_path / "p"
    hosts = []

    def compose(*, model=None, human=None, enabled=True, cfg=None, **kwargs):
        host = SupervisedWorkspaceWorkflow(config=cfg or config, human=human or Human(),
            model=model or Model(), enable_write=enabled, _host_directory=private, **kwargs)
        hosts.append(host)
        return host

    yield root, private, config, compose
    for host in hosts:
        host.close()


def run(host):
    return host.run("Inspect input and create output", WorkflowProfile(("input.txt",), "out.txt"))


@NATIVE
@pytest.mark.parametrize("body", [BODY, "", "😀\r\n\x1b[31m\x00end"])
def test_native_complete_separate_read_and_private_evidence(setup, monkeypatch, body):
    root, private, _, compose = setup
    model, human = Model(body), Human()
    host = compose(model=model, human=human)
    attempts = []
    original = WriteFileTool.run

    def counted(tool, args, context):
        attempts.append(1)
        return original(tool, args, context)

    monkeypatch.setattr(WriteFileTool, "run", counted)
    result = run(host)
    assert result["ok"], result
    assert result["status"] == "completed_verified"
    assert len(attempts) == 1 and (root / "out.txt").read_bytes() == body.encode()
    assert human.previews == [{"destination": str(root / "out.txt"), "content": body,
        "size": len(body.encode()), "sha256": __import__("hashlib").sha256(body.encode()).hexdigest()}]
    assert len(human.plans) == len(human.requests) == 1
    assert [s["tool"] for s in host._steps] == ["inspect", "read_file", "write_file", "read_file"]
    assert host._state.tool_calls[-1]["tool"] == "read_file"
    assert body == "" or body not in json.dumps(host._state.to_dict(), ensure_ascii=False)
    assert "Declared input only" not in json.dumps(host._state.to_dict())
    assert body == "" or body not in json.dumps(result, ensure_ascii=False)
    records = host._services.actions.audit(maximum_records=256)
    assert len(records) == 1 and len(records[0].attempts) == 1
    assert body == "" or body not in json.dumps(records[0].to_dict(), ensure_ascii=False)
    workflow_id = result["workflow_id"]
    host.close()
    resumed = compose(enabled=False, cfg=replace(setup[2], permissions=PermissionConfig(
        level=PermissionLevel.CONFIRM, blocked_capabilities=(Capability.FS_WRITE.value,))))
    historical = resumed.resume(workflow_id)
    assert historical["ok"] and historical["historical"]
    assert len(attempts) == 1


def test_declined_plan_has_no_tool_or_action(setup):
    root, _, _, compose = setup
    human = Human()
    human.accept = False
    host = compose(human=human)
    result = run(host)
    assert result["status"] == "plan_declined"
    assert not host._services.actions.list_actions() and not host._observations
    assert not (root / "out.txt").exists()


@pytest.mark.parametrize("mutation", ["second_write", "extra_tool", "extra_argument", "path", "dependency", "missing_input", "content"])
def test_untrusted_plan_rejected_before_any_tool(setup, mutation):
    proposal = plan()
    if mutation == "second_write":
        proposal["steps"][0].update(tool_name="write_file", tool_args={"path": "extra.txt", "content": ""})
    elif mutation == "extra_tool":
        proposal["steps"][0]["tool_name"] = "echo"
    elif mutation == "extra_argument":
        proposal["steps"][0]["tool_args"]["unexpected"] = True
    elif mutation == "path":
        proposal["steps"][1]["tool_args"]["path"] = "undeclared.txt"
    elif mutation == "dependency":
        proposal["steps"][2]["depends_on"] = []
    elif mutation == "missing_input":
        proposal["steps"][1].update(tool_name="inspect", tool_args={"path": "."})
    else:
        proposal["steps"][2]["tool_args"]["content"] = "model claims approval"
    host = setup[3](model=Model(proposal=proposal))
    with pytest.raises(Exception):
        run(host)
    assert not host._observations and not host._services.actions.list_actions()


@pytest.mark.parametrize("level", [PermissionLevel.OBSERVE, PermissionLevel.PROPOSE, PermissionLevel.AUTONOMOUS])
def test_current_confirm_required(setup, level):
    host = setup[3](cfg=replace(setup[2], permissions=PermissionConfig(level=level)))
    with pytest.raises(WorkspaceHostError, match="workflow_authority_denied"):
        run(host)
    assert not host._observations


def test_current_opt_in_required(setup):
    host = setup[3](enabled=False)
    with pytest.raises(WorkspaceHostError, match="workspace_write_opt_in_required"):
        run(host)


@NATIVE
def test_denied_approval_no_mutation_and_no_replay(setup):
    human = Human()
    human.decision = HumanApprovalDecision.DENY
    host = setup[3](human=human)
    result = run(host)
    assert not result["ok"] and result["status"] == "action_denied", result
    assert not setup[0].joinpath("out.txt").exists()
    assert not host._services.actions.list_actions()
    workflow_id = result["workflow_id"]
    host.close()
    resumed = setup[3](enabled=False)
    assert resumed.resume(workflow_id)["status"] == "action_denied"
    assert not resumed._observations


@NATIVE
@pytest.mark.parametrize("fault", ["lost_return", "changed", "deleted", "read_denied"])
def test_created_but_incomplete_and_read_only_resume(setup, monkeypatch, fault):
    root, _, config, compose = setup
    host = compose()
    original = host.executor.invoke

    def interrupted(name, *args, **kwargs):
        invocation = original(name, *args, **kwargs)
        if name == "write_file":
            if fault == "lost_return":
                raise RuntimeError("lost return")
            if fault == "changed":
                (root / "out.txt").write_bytes(b"x" * len(BODY.encode()))
            if fault == "deleted":
                (root / "out.txt").unlink()
            if fault == "read_denied":
                host.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.CONFIRM,
                    blocked_capabilities=(Capability.FS_READ.value,)))
        return invocation

    monkeypatch.setattr(host.executor, "invoke", interrupted)
    result = run(host)
    assert not result["ok"] and result["action"]["state"] == "succeeded", result
    assert result["verification"] is None
    assert len(host._services.actions.audit(maximum_records=256)[0].attempts) == 1
    workflow_id = result["workflow_id"]
    host.close()
    resumed = compose(enabled=False, cfg=replace(config, permissions=PermissionConfig(
        level=PermissionLevel.CONFIRM, blocked_capabilities=(Capability.FS_WRITE.value,))))
    result2 = resumed.resume(workflow_id)
    assert result2["ok"] is (fault in {"lost_return", "read_denied"}), result2
    assert len(resumed._services.actions.audit(maximum_records=256)[0].attempts) == 1
    assert all(inv.tool == "read_file" for inv in resumed._observations.values())


@NATIVE
def test_existing_target_never_overwritten(setup):
    (setup[0] / "out.txt").write_bytes(b"original")
    host = setup[3]()
    result = run(host)
    assert not result["ok"] and (setup[0] / "out.txt").read_bytes() == b"original"
    assert not host._services.actions.list_actions()


@pytest.mark.skipif(os.name == "nt", reason="native POSIX refusal")
def test_posix_refuses_without_effect(setup):
    host = setup[3]()
    result = run(host)
    assert not result["ok"] and not (setup[0] / "out.txt").exists()
    assert not host._services.actions.list_actions()


def test_duplicate_callers_and_owner_thread(setup):
    host = setup[3]()
    with pytest.raises(WorkspaceHostError, match="workspace_host_busy"):
        setup[3]()
    errors = []

    def foreign():
        try:
            host.resume("task_000000000000")
        except WorkspaceHostError as exc:
            errors.append(exc.category)

    thread = threading.Thread(target=foreign)
    thread.start()
    thread.join(5)
    assert errors == ["workspace_ownership_invalid"]


def test_terminal_surface_refuses_noninteractive(monkeypatch):
    monkeypatch.setattr(workflow_cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(WorkspaceHostError, match="workflow_interactive_terminal_required"):
        workflow_cli.TerminalWorkflowHuman().accept_plan({"private": BODY})


@NATIVE
@pytest.mark.parametrize("stage", ["before_dispatch", "authorized", "claimed", "published",
                                  "after_success", "after_read", "after_receipt"])
def test_real_process_death_never_replays_creation(setup, monkeypatch, stage):
    root, private, config, compose = setup
    worker = Path(__file__).parent / "fixtures/task50b_crash_worker.py"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    child = subprocess.run([sys.executable, "-B", str(worker), str(root), str(private), stage],
                           env=env, capture_output=True, timeout=30)
    assert child.returncode == 51, child.stderr.decode(errors="replace")
    monkeypatch.setattr(WriteFileTool, "run", lambda *a: pytest.fail("creation replayed"))
    host = compose(enabled=False, cfg=replace(config, permissions=PermissionConfig(
        level=PermissionLevel.CONFIRM, blocked_capabilities=(Capability.FS_WRITE.value,))))
    assert len(host._storage.records) == 1
    workflow_id = next(iter(host._storage.records))
    result = host.resume(workflow_id)
    applied = stage in {"published", "after_success", "after_read", "after_receipt"}
    assert result["ok"] is applied, result
    assert (root / "out.txt").exists() is applied
    actions = host._services.actions.audit(maximum_records=256)
    assert len(actions) == (0 if stage == "before_dispatch" else 1)
    if actions:
        assert len(actions[0].attempts) == (0 if stage == "authorized" else 1)
    assert all(item.tool == "read_file" for item in host._observations.values())


@NATIVE
@pytest.mark.parametrize("published", [False, True])
def test_uncertain_effect_blocks_final_read_until_reconciliation(setup, monkeypatch, published):
    from test_fs_write import _publication_hook
    from agentcore.fs.guard import WorkspaceGuard

    def uncertain(publish):
        if published:
            publish()
        raise TimeoutError("private-fault-sentinel")

    with monkeypatch.context() as scope:
        _publication_hook(scope, uncertain)
        host = setup[3]()
        result = run(host)
        assert not result["ok"] and result["action"]["state"] == "reconcile_required"
        assert host._steps[-1]["id"] not in host._observations
        workflow_id = result["workflow_id"]
        host.close()
    monkeypatch.setattr(WorkspaceGuard, "create_file", lambda *a: pytest.fail("mutation retried"))
    resumed = setup[3](enabled=False, cfg=replace(setup[2], permissions=PermissionConfig(
        level=PermissionLevel.CONFIRM, blocked_capabilities=(Capability.FS_WRITE.value,))))
    result2 = resumed.resume(workflow_id)
    assert result2["ok"] is published, result2
    assert len(resumed._services.actions.audit(maximum_records=256)[0].attempts) == 1


@NATIVE
@pytest.mark.parametrize("boundary", ["before_tools", "during_approval", "after_creation"])
def test_cancellation_preserves_truth_and_prevents_continuation(setup, monkeypatch, boundary):
    from agentcore.realtime.cancellation import CancellationSource, CancellationReason
    source = CancellationSource()
    human = Human()
    host = setup[3](human=human)
    if boundary == "before_tools":
        source.cancel(CancellationReason.OPERATOR_CANCEL)
    elif boundary == "during_approval":
        def cancelled(preview, prompt):
            source.cancel(CancellationReason.OPERATOR_CANCEL)
            return HumanApprovalDecision.APPROVE
        human.approve_create = cancelled
    else:
        original = host.executor.invoke
        def cancelled(name, *args, **kwargs):
            result = original(name, *args, **kwargs)
            if name == "write_file":
                source.cancel(CancellationReason.OPERATOR_CANCEL)
            return result
        monkeypatch.setattr(host.executor, "invoke", cancelled)
    result = host.run("inspect", WorkflowProfile(("input.txt",), "out.txt"), cancellation=source.token)
    assert not result["ok"] and result["status"].startswith("cancelled"), result
    assert (setup[0] / "out.txt").exists() is (boundary == "after_creation")
    assert host._steps[-1]["id"] not in host._observations
    if boundary == "after_creation":
        assert result["action"]["state"] == "succeeded"


@NATIVE
def test_expired_exact_approval_and_override_cannot_bypass_gate(setup):
    from agentcore.util.clock import FixedClock
    clock = FixedClock()
    human = Human()
    def expired(preview, prompt):
        clock.advance(301)
        return HumanApprovalDecision.APPROVE
    human.approve_create = expired
    cfg = replace(setup[2], permissions=PermissionConfig(level=PermissionLevel.CONFIRM,
        capability_overrides={Capability.FS_WRITE.value: PermissionLevel.OBSERVE}))
    host = setup[3](human=human, cfg=cfg, clock=clock)
    result = host.run("inspect", WorkflowProfile(("input.txt",), "out.txt", max_seconds=600))
    assert not result["ok"] and result["status"] == "action_expired", result
    assert not (setup[0] / "out.txt").exists()


@NATIVE
def test_private_verification_save_failure_retains_created_truth(setup, monkeypatch):
    host = setup[3]()
    original = host._storage._file.replace
    def failure(data):
        if host._record is not None and host._record["phase"] == "verified":
            raise OSError("private-storage-fault")
        return original(data)
    monkeypatch.setattr(host._storage._file, "replace", failure)
    result = run(host)
    assert not result["ok"] and result["status"] == "storage_evidence_failure", result
    assert result["action"]["state"] == "succeeded" and result["verification"] is None
    assert (setup[0] / "out.txt").read_bytes() == BODY.encode()


@pytest.mark.parametrize("corrupt", ["manifest", "key", "plan", "missing_manifest", "unknown_temp"])
def test_corrupt_or_missing_evidence_fails_before_model(setup, corrupt):
    human = Human()
    human.decision = HumanApprovalDecision.DENY
    host = setup[3](human=human)
    run(host)
    directory = host._services.storage.directory
    host.close()
    if corrupt == "manifest":
        target = directory / "workflows/workspace-workflows.json"
        data = json.loads(target.read_text())
        next(iter(data["records"].values()))["dispatched"] = False
        # Always change authenticated bytes even if POSIX already refused before approval.
        data["schema"] = 99
        target.write_text(json.dumps(data))
    elif corrupt == "missing_manifest":
        (directory / "workflows/workspace-workflows.json").unlink()
    elif corrupt == "key":
        (directory / "installation-key.json").unlink()
    elif corrupt == "plan":
        next((directory / "payloads").glob("pap_*.json")).write_text('{"invalid":true}')
    else:
        (directory / "workflows/.pending-unknown").write_bytes(b"inert")
    model = Model()
    with pytest.raises(Exception):
        setup[3](model=model)
    assert not model.calls


@NATIVE
@pytest.mark.parametrize("fault", ["truncated", "metadata", "verifier_exception"])
def test_final_read_must_be_complete_and_trusted(setup, monkeypatch, fault):
    from agentcore.tools.builtin.read_file import ReadFileTool
    from agentcore.tools.base import ToolResult
    original = ReadFileTool.run
    def faulty(tool, args, context):
        result = original(tool, args, context)
        if args["path"] == "out.txt":
            if fault == "verifier_exception":
                return ToolResult.success(object(), **result.metadata)
            return ToolResult.success(result.output, **{**result.metadata,
                "truncated": fault == "truncated", "bytes_read": -1 if fault == "metadata" else result.metadata["bytes_read"]})
        return result
    monkeypatch.setattr(ReadFileTool, "run", faulty)
    host = setup[3]()
    result = run(host)
    assert not result["ok"] and result["action"]["state"] == "succeeded", result
    assert result["verification"] is None


@pytest.mark.parametrize("path", ["../escape.txt", "/absolute.txt", "C:/drive.txt", ".env", "a/../../x"])
def test_profile_paths_refused_before_plan_or_tool(setup, path):
    model = Model()
    host = setup[3](model=model)
    with pytest.raises(Exception):
        host.run("create", WorkflowProfile(("input.txt",), path))
    assert not model.calls and not host._observations


@NATIVE
def test_unsafe_input_link_refused(setup):
    from test_fs_write import _symlink
    root = setup[0]
    original = root / "real.txt"
    (root / "input.txt").rename(original)
    _symlink(root / "input.txt", original)
    host = setup[3]()
    result = run(host)
    assert not result["ok"] and not host._record["dispatched"]
    assert not (root / "out.txt").exists()


def test_permission_revoked_after_acceptance_blocks_write(setup):
    human = Human()
    host = setup[3](human=human)
    def revoked(review):
        host.executor.policy = PermissionPolicy(PermissionConfig(level=PermissionLevel.CONFIRM,
            blocked_capabilities=(Capability.FS_WRITE.value,)))
        return True
    human.accept_plan = revoked
    result = run(host)
    assert not result["ok"] and not host._record["dispatched"]
    assert not host._services.actions.list_actions() and not human.previews


@NATIVE
def test_preview_mutation_cannot_change_approved_bytes(setup):
    human = Human()
    def manipulate(preview, prompt):
        preview["content"] = "different"
        preview["destination"] = "elsewhere.txt"
        return HumanApprovalDecision.APPROVE
    human.approve_create = manipulate
    host = setup[3](human=human)
    assert run(host)["ok"]
    assert (setup[0] / "out.txt").read_bytes() == BODY.encode()
    assert not (setup[0] / "elsewhere.txt").exists()


@NATIVE
@pytest.mark.parametrize("missing", ["action", "approval", "whole_workflow_directory"])
def test_missing_terminal_evidence_never_admits_fresh_work(setup, missing):
    from agentcore.workspace_host.storage import _validate_private
    host = setup[3]()
    result = run(host)
    assert result["ok"]
    directory = host._services.storage.directory
    action_id = result["action"]["action_id"]
    request_id = host._record["request_id"]
    host.close()
    if missing == "action":
        (directory / "actions" / (action_id + ".json")).unlink()
    elif missing == "approval":
        (directory / "workflow-approvals" / (request_id + ".json")).unlink()
    else:
        # Test-controlled exact private directory only, after all handles close.
        target = directory / "workflows"
        _validate_private(target, directory=True)
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
    model = Model()
    with pytest.raises(Exception):
        setup[3](model=model)
    assert not model.calls


def test_cli_public_json_is_categorical_and_respects_private_boundary(setup, monkeypatch, capsys):
    from agentcore import cli
    monkeypatch.setattr(workflow_cli, "load_config", lambda _: setup[2])
    monkeypatch.setattr(workflow_cli, "provider_from_config", lambda _: Model())
    human = Human()
    human.accept = False
    monkeypatch.setattr(workflow_cli, "TerminalWorkflowHuman", lambda: human)
    original = workflow_cli.SupervisedWorkspaceWorkflow
    monkeypatch.setattr(workflow_cli, "SupervisedWorkspaceWorkflow",
        lambda **kw: original(**kw, _host_directory=setup[1]))
    code = cli.main(["workflow", "--enable-write", "--message", "private-objective-sentinel",
                     "--input", "input.txt", "--output", "out.txt", "--json"])
    output = capsys.readouterr()
    public = json.loads(output.out)
    assert code == 1 and public["status"] == "plan_declined"
    assert "private-objective-sentinel" not in output.out and not output.err


def test_protected_file_extension_is_fixed_name_only(tmp_path):
    from agentcore.credentials._protected_file import ProtectedFile
    from agentcore.credentials.errors import CredentialError
    for value in ["../workspace-workflows.json", "arbitrary.json", ".credential-owner"]:
        with pytest.raises(CredentialError):
            ProtectedFile(tmp_path / "private", record_name=value)
    assert not (tmp_path / "private").exists()


def test_plan_review_cannot_manipulate_executable_structure(setup):
    human = Human()
    def manipulated(review):
        review["steps"][-2]["args"]["path"] = "extra.txt"
        review["steps"].append(review["steps"][-2])
        return False
    human.accept_plan = manipulated
    host = setup[3](human=human)
    assert run(host)["status"] == "plan_declined"
    assert not host._observations and not host._storage.records


def test_unaccepted_orphan_plan_blocks_new_objective(setup, monkeypatch):
    host = setup[3]()
    original = host._storage.save
    def lost_checkpoint():
        if host._storage.records:
            raise OSError("acceptance commit interrupted")
        return original()
    monkeypatch.setattr(host._storage, "save", lost_checkpoint)
    with pytest.raises(OSError):
        run(host)
    assert not host._observations
    host.close()
    with pytest.raises(WorkspaceHostError, match="workflow_storage_failed"):
        setup[3]()


def test_inspection_truncation_stops_before_draft_and_approval(setup):
    (setup[0] / "input.txt").write_bytes(b"x" * 70000)
    model, human = Model(), Human()
    host = setup[3](model=model, human=human)
    result = run(host)
    assert not result["ok"] and not host._record["dispatched"]
    assert len(model.calls) == 1 and not human.previews


@NATIVE
@pytest.mark.parametrize("size", [4096, 4097])
def test_exact_draft_byte_limit(setup, size):
    host = setup[3](model=Model(body="x" * size))
    result = run(host)
    assert result["ok"] is (size == 4096), result
    assert (setup[0] / "out.txt").exists() is (size == 4096)


@NATIVE
def test_second_successful_objective_retains_first_identity(setup):
    first = setup[3]()
    first_result = run(first)
    assert first_result["ok"]
    first.close()
    proposal = plan()
    for step in proposal["steps"][-2:]:
        step["tool_args"]["path"] = "second.txt"
    second = setup[3](model=Model(proposal=proposal))
    result = second.run("second objective", WorkflowProfile(("input.txt",), "second.txt"))
    assert result["ok"] and result["workflow_id"] != first_result["workflow_id"]
    assert len(second._storage.records) == 2
    assert len(second._services.actions.audit(maximum_records=256)) == 2
    assert (setup[0] / "out.txt").read_bytes() == BODY.encode()


@NATIVE
def test_revoked_read_prevents_restart_probe(setup, monkeypatch):
    from test_fs_write import _publication_hook
    from agentcore.fs.guard import WorkspaceGuard
    def uncertain(publish):
        publish()
        raise TimeoutError("uncertain")
    with monkeypatch.context() as scope:
        _publication_hook(scope, uncertain)
        host = setup[3]()
        assert run(host)["action"]["state"] == "reconcile_required"
        host.close()
    monkeypatch.setattr(WorkspaceGuard, "observe_create", lambda *a: pytest.fail("unauthorized read probe"))
    config = replace(setup[2], permissions=PermissionConfig(level=PermissionLevel.CONFIRM,
        blocked_capabilities=(Capability.FS_READ.value,)))
    with pytest.raises(WorkspaceHostError, match="workflow_read_denied"):
        setup[3](enabled=False, cfg=config)
