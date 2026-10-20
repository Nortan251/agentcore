"""Workspace-only workflow, token persistence, capacity and failure latching."""

from dataclasses import replace
import json
import os
import sys

import pytest

from agentcore.action_journal import ActionState
from agentcore.fs.guard import WorkspaceGuard
from agentcore.models.messages import ModelResponse, Role
from agentcore.tools.builtin.read_file_for_update import UPDATE_RESULT_PREFIX
from agentcore.workspace_host.errors import WorkspaceHostError
from test_fs_replace import native_security, publication_hook
from test_workspace_host import setup, Model, calls, invoke, _private_fixture_json


def update():
    return calls(("read_file_for_update", {"path": "README.txt"}))


def get_revision(request):
    message = [m for m in request.messages if m.role is Role.TOOL and m.name == "read_file_for_update"][-1]
    assert message.content.startswith(UPDATE_RESULT_PREFIX)
    return json.loads(message.content[len(UPDATE_RESULT_PREFIX):])["revision"]


def skip_unavailable(available):
    if os.name != "nt":
        pytest.skip("replacement requires native Windows handles; POSIX refusal tested separately")
    if not available:
        pytest.skip("current Windows token cannot snapshot full security descriptor")


def test_model_update_then_replace_complete_file_and_expire_saved_tool_token(setup, capsys, monkeypatch, native_security):
    skip_unavailable(native_security)
    root, private, _, compose = setup
    revisions = []
    def replace_request(request):
        revision = get_revision(request); revisions.append(revision)
        return calls(("replace_file", {"path": "README.txt", "revision": revision, "content": "NEW_PRIVATE_BODY"}))
    model = Model(update(), replace_request, ModelResponse(text="edited"))
    code, payload = invoke(capsys, monkeypatch, model)
    assert code == 0 and payload["ok"] and payload["tools_executed"] == 2
    assert root.joinpath("README.txt").read_bytes() == b"NEW_PRIVATE_BODY"
    assert len(payload["actions"]) == 1 and payload["actions"][0]["state"] == "succeeded"
    public = json.dumps(payload)
    assert revisions[0] not in public and "README.txt" not in public and "NEW_PRIVATE_BODY" not in public
    with compose() as fresh:
        session = fresh._services.sessions.load(payload["session_id"])
        message = [m for m in session.messages if m.name == "read_file_for_update"][-1]
        body = json.loads(message.content[len(UPDATE_RESULT_PREFIX):])
        assert body == {"content": "Project description.\n", "revision": "<expired-revision>"}
        assert not fresh._revisions._snapshots
        old_model = Model(calls(("replace_file", {"path": "README.txt", "revision": revisions[0], "content": "replay"})),
                          ModelResponse(text="done"))
    # A resumed invocation never restores registry entries from history.
    code, next_payload = invoke(capsys, monkeypatch, old_model, session=payload["session_id"])
    assert code == 1 and not next_payload["actions"] and root.joinpath("README.txt").read_bytes() == b"NEW_PRIVATE_BODY"


@pytest.mark.parametrize("first_read", [False, True])
def test_path_and_normal_read_do_not_authorize_replacement(setup, capsys, monkeypatch, first_read):
    root, _, _, _ = setup
    responses = [calls(("read_file", {"path": "README.txt"}))] if first_read else []
    model = Model(*responses, calls(("replace_file", {"path": "README.txt", "revision": "rev_" + "a" * 64,
                                    "content": "untrusted"}),
                                   ("write_file", {"path": "notes/later.txt", "content": "masked"})),
                  ModelResponse(text="All succeeded."))
    code, payload = invoke(capsys, monkeypatch, model)
    assert code == 1 and not payload["ok"] and not payload["actions"]
    assert root.joinpath("README.txt").read_text() == "Project description.\n"
    assert not (root / "notes/later.txt").exists()


def test_stale_revision_latches_cli_failure_preserving_earlier_effects(setup, capsys, monkeypatch, native_security):
    skip_unavailable(native_security)
    root, _, _, _ = setup
    def stale(request):
        revision = get_revision(request)
        root.joinpath("README.txt").write_bytes(b"new external state")
        return calls(("replace_file", {"path": "README.txt", "revision": revision, "content": "stale overwrite"}))
    model = Model(calls(("create_directory", {"path": "made"}),
                        ("write_file", {"path": "made/kept", "content": "kept"}),
                        ("read_file_for_update", {"path": "README.txt"})), stale, ModelResponse(text="succeeded"))
    code, payload = invoke(capsys, monkeypatch, model)
    assert code == 1 and len(payload["actions"]) == 2
    assert all(a["state"] == "succeeded" for a in payload["actions"])
    assert (root / "made/kept").read_bytes() == b"kept"
    assert (root / "README.txt").read_bytes() == b"new external state"


@pytest.mark.parametrize("state", ["new", "old"])
@pytest.mark.parametrize("identical_content", [False, True])
def test_recovery_precedes_model_and_never_retries_uncertain_replace(setup, monkeypatch, native_security, state,
                                                                  identical_content):
    skip_unavailable(native_security)
    root, _, _, compose = setup
    content = (root / "README.txt").read_text(encoding="utf-8") if identical_content else "NEW"
    def replacement_request(request):
        return calls(("replace_file", {"path": "README.txt", "revision": get_revision(request), "content": content}))
    old_mode = (root / "README.txt").stat().st_mode & 0o777
    def uncertain(publish):
        if state == "new":
            publish()
            if os.name == "posix":
                os.chmod(root / "README.txt", old_mode)
        raise TimeoutError()
    publication_hook(monkeypatch, uncertain)
    with compose(Model(update(), replacement_request, ModelResponse(text="done"))) as h:
        h.conversation().send("edit")
        action = h.action_truth()[0]["action_id"]
        assert h._services.actions.load(action).state is ActionState.RECONCILE_REQUIRED
    monkeypatch.setattr(WorkspaceGuard, "replace_file", lambda *a: pytest.fail("recovery retried replacement"))
    model = Model()
    with pytest.raises(WorkspaceHostError, match="workspace_action_requires_operator"):
        compose(model)
    assert not model.requests


@pytest.mark.parametrize("existing", [255, 256])
def test_replace_capacity_and_update_read_no_action_admission(setup, capsys, monkeypatch, native_security, existing):
    skip_unavailable(native_security)
    root, _, _, compose = setup
    from test_task_43_2a_action_journal_core import _new_record, _id, NOW
    with compose() as h:
        slot = h._services.storage.directory
    for number in range(1, existing + 1):
        record = _new_record(number).transition(ActionState.CANCELLED,
            transition_id=_id("atx", number + 1000), at=NOW)
        _private_fixture_json(slot / "actions" / f"{record.action_id}.json", record.to_dict())
    def replacement_request(request):
        return calls(("replace_file", {"path": "README.txt", "revision": get_revision(request), "content": "NEW"}))
    model = Model(update(), replacement_request, ModelResponse(text="done"))
    capsys.readouterr()
    code, payload = invoke(capsys, monkeypatch, model)
    assert (code == 0) is (existing == 255)
    assert payload["tools_executed"] == (2 if existing == 255 else 1)
    assert len(payload["actions"]) == 256 - existing
    assert root.joinpath("README.txt").read_bytes() == (b"NEW" if existing == 255 else b"Project description.\n")


def test_closed_host_clears_registry_and_update_read_bound_is_not_global_change(setup):
    _, _, config, compose = setup
    h = compose(config=replace(config, conversation=replace(config.conversation, max_tool_result_chars=177)))
    assert h._revisions.capacity == config.conversation.max_tool_rounds * config.conversation.max_tool_calls_per_round
    assert h._replace_foundation.read_tool._max_result_chars == 177
    assert config.conversation.max_tool_result_chars == 2000
    h.close()
    assert not h._revisions._snapshots


def test_historical_authorized_replace_never_executes_on_startup(setup, monkeypatch, native_security):
    skip_unavailable(native_security)
    root, _, _, compose = setup
    def replacement_request(request):
        return calls(("replace_file", {"path": "README.txt", "revision": get_revision(request), "content": "NEW"}))
    with compose(Model(update(), replacement_request, ModelResponse(text="done"))) as h:
        def no_claim(*args, **kwargs):
            raise OSError("attempt claim interrupted before effect")
        monkeypatch.setattr(h._services.actions, "claim_attempt", no_claim)
        h.conversation().send("edit")
        records = h._services.actions.audit(maximum_records=256)
        assert len(records) == 1 and records[0].state is ActionState.AUTHORIZED
        action = records[0].action_id
    monkeypatch.setattr(WorkspaceGuard, "replace_file", lambda *a: pytest.fail("authorized history executed"))
    model = Model(calls(("inspect", {"path": "."})), ModelResponse(text="read"))
    with compose(model) as h:
        assert h._services.actions.load(action).state is ActionState.AUTHORIZED
        assert not model.requests
        assert h.conversation().send("inspect only").ok
    assert root.joinpath("README.txt").read_bytes() == b"Project description.\n"


def test_supported_catalogue_cannot_be_changed(setup):
    _, _, _, compose = setup
    with compose() as h:
        assert set(h.registry.names()) == {"inspect", "list_directory", "read_file", "read_file_for_update",
                                           "create_directory", "write_file", "replace_file"}
        assert len(h._adapters) == 3


def test_unsupported_update_read_preserves_inspection_and_reports_cli_failure(setup, capsys, monkeypatch):
    root, _, _, _ = setup
    model = Model(calls(("read_file", {"path": "README.txt"}),
                        ("read_file_for_update", {"path": "README.txt"})),
                  ModelResponse(text="Everything succeeded."))
    monkeypatch.setattr(sys, "platform", "linux")
    code, payload = invoke(capsys, monkeypatch, model)
    assert code == 1 and payload["error"] == "workspace_tool_failed" and not payload["ok"]
    assert payload["tools_executed"] == 1 and not payload["actions"]
    assert root.joinpath("README.txt").read_bytes() == b"Project description.\n"
    assert not (root / "later").exists() and not list(root.rglob(".agentcore-stage-*"))
