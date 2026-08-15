"""`agentcore memory conflicts`: the operator surface for durable conflicts,
the Agent seam, and the full cross-process acceptance scenario.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agentcore.cli import main, render_conflicts
from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.memory import JsonFileMemoryStore, new_memory
from agentcore.memory.capture import CaptureAction
from agentcore.memory.conflicts import (
    CONFLICTS_SUBDIR,
    ConflictStatus,
    InMemoryConflictStore,
    JsonFileConflictStore,
)
from agentcore.memory.resolve import ConflictResolution, ResolutionStatus
from agentcore.models.messages import Message
from agentcore.permissions.levels import PermissionLevel
from agentcore.recovery.config import RecoveryConfig

SECRET = "sk-cli-conflicts-must-not-leak"


class FakeModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, prompt, *, system=None, **options):
        self.calls.append({"prompt": prompt, "system": system})
        output = self.outputs.pop(0) if len(self.outputs) > 1 \
            else self.outputs[0]
        return output if isinstance(output, str) else json.dumps(output)

    @property
    def last_prompt(self):
        return self.calls[-1]["prompt"]


def plan_payload(objective="goal"):
    return {"objective": objective, "steps": [
        {"id": "s1", "description": "inspect the workspace",
         "tool_name": "workspace_info", "tool_args": {}, "depends_on": []}]}


def config(tmp_path, *, memory=True, planning=False):
    return AgentConfig(
        workspace_dir=str(tmp_path), model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=1), memory=MemoryConfig(),
        features=FeatureFlags(memory=memory, planning=planning),
    ).validate()


def cli(tmp_path, capsys, *argv, memory=True, expect=0):
    env = {"AGENTCORE_WORKSPACE_DIR": str(tmp_path),
           "AGENTCORE_FEATURES__MEMORY": "true" if memory else "false"}
    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        code = main(list(argv))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    captured = capsys.readouterr()
    assert code == expect, captured.out + captured.err
    return captured


def conflict_store(tmp_path):
    return JsonFileConflictStore(tmp_path / "memory" / CONFLICTS_SUBDIR)


def seed_conflict(tmp_path):
    """Create the classic Python -> Rust contradiction on disk."""
    agent = Agent(config(tmp_path))
    original = agent.remember_from(Message.user(
        "Remember that I prefer Python for my projects.")).stored[0]
    result = agent.remember_from(Message.user(
        "Remember that I've switched to Rust for new projects."))
    assert result.decisions[0].action is CaptureAction.POSSIBLE_CONFLICT
    return original, result.conflicts_recorded[0]


# ===========================================================================
# CLI: listing
# ===========================================================================

def test_conflicts_listing(tmp_path, capsys):
    original, conflict = seed_conflict(tmp_path)
    out = cli(tmp_path, capsys, "memory", "conflicts").out
    assert conflict.id in out
    assert "[pending]" in out
    assert "I've switched to Rust for new projects" in out
    assert original.id in out
    assert "I prefer Python for my projects" in out


def test_conflicts_listing_json(tmp_path, capsys):
    original, conflict = seed_conflict(tmp_path)
    [row] = json.loads(cli(tmp_path, capsys, "memory", "conflicts",
                           "--json").out)
    assert row["id"] == conflict.id
    assert row["status"] == "pending"
    assert row["trust"] == "user_provided"
    assert row["recorded_by"] == "user"
    assert row["related"][0]["id"] == original.id
    assert row["related"][0]["retired"] is False


def test_empty_conflicts(tmp_path, capsys):
    JsonFileMemoryStore(tmp_path / "memory")
    out = cli(tmp_path, capsys, "memory", "conflicts").out
    assert "(no unresolved memory conflicts)" in out


def test_conflicts_json_empty(tmp_path, capsys):
    JsonFileMemoryStore(tmp_path / "memory")
    assert json.loads(cli(tmp_path, capsys, "memory", "conflicts",
                          "--json").out) == []


def test_conflicts_requires_memory_enabled(tmp_path, capsys):
    seed_conflict(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "conflicts", memory=False,
                   expect=2)
    assert "memory is disabled" in captured.err


def test_render_conflicts_empty():
    assert render_conflicts([]) == "(no unresolved memory conflicts)"


def test_conflicts_limit(tmp_path, capsys):
    store = conflict_store(tmp_path)
    JsonFileMemoryStore(tmp_path / "memory")
    from agentcore.memory.conflicts import PendingConflict

    for index in range(4):
        store.record(PendingConflict(candidate_content=f"candidate {index}",
                                     id=f"conf_{index}"))
    rows = json.loads(cli(tmp_path, capsys, "memory", "conflicts",
                          "--limit", "2", "--json").out)
    assert len(rows) == 2


# ===========================================================================
# CLI: resolving
# ===========================================================================

def test_supersede_requires_confirmation(tmp_path, capsys):
    original, conflict = seed_conflict(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "conflicts", "--resolve",
                   conflict.id, "--supersede", expect=2)
    assert "--yes" in captured.err
    assert conflict_store(tmp_path).get(conflict.id).is_pending is True
    assert JsonFileMemoryStore(tmp_path / "memory").get(
        original.id).is_expired() is False


def test_resolve_requires_a_choice(tmp_path, capsys):
    _, conflict = seed_conflict(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "conflicts", "--resolve",
                   conflict.id, expect=2)
    assert "--supersede" in captured.err
    assert conflict_store(tmp_path).get(conflict.id).is_pending is True


def test_supersede_via_cli(tmp_path, capsys):
    original, conflict = seed_conflict(tmp_path)
    out = cli(tmp_path, capsys, "memory", "conflicts", "--resolve",
              conflict.id, "--supersede", "--yes", "--operator", "alice").out
    assert "Resolved conflict" in out
    assert original.id in out

    memory = JsonFileMemoryStore(tmp_path / "memory")
    assert [r.content for r in memory.all_records()] == \
        ["I've switched to Rust for new projects"]
    settled = conflict_store(tmp_path).get(conflict.id)
    assert settled.status is ConflictStatus.RESOLVED
    assert settled.resolved_by == "alice"


def test_keep_existing_via_cli(tmp_path, capsys):
    original, conflict = seed_conflict(tmp_path)
    cli(tmp_path, capsys, "memory", "conflicts", "--resolve", conflict.id,
        "--keep-existing")
    memory = JsonFileMemoryStore(tmp_path / "memory")
    assert [r.content for r in memory.all_records()] == \
        ["I prefer Python for my projects"]
    assert memory.get(original.id).is_expired() is False
    assert conflict_store(tmp_path).get(conflict.id).resolution == \
        "keep_existing"


def test_discard_via_cli(tmp_path, capsys):
    original, conflict = seed_conflict(tmp_path)
    cli(tmp_path, capsys, "memory", "conflicts", "--resolve", conflict.id,
        "--discard")
    memory = JsonFileMemoryStore(tmp_path / "memory")
    assert memory.count(include_expired=True) == 1
    assert memory.get(original.id).content == original.content
    assert conflict_store(tmp_path).get(conflict.id).resolution == \
        "discard_candidate"


def test_keep_existing_needs_no_confirmation(tmp_path, capsys):
    """Only the destructive choice is gated."""
    _, conflict = seed_conflict(tmp_path)
    cli(tmp_path, capsys, "memory", "conflicts", "--resolve", conflict.id,
        "--keep-existing", expect=0)


def test_repeat_resolution_is_refused(tmp_path, capsys):
    _, conflict = seed_conflict(tmp_path)
    cli(tmp_path, capsys, "memory", "conflicts", "--resolve", conflict.id,
        "--discard")
    captured = cli(tmp_path, capsys, "memory", "conflicts", "--resolve",
                   conflict.id, "--discard", expect=2)
    assert "already resolved" in captured.err


def test_unknown_conflict_id(tmp_path, capsys):
    seed_conflict(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "conflicts", "--resolve",
                   "conf_nope", "--discard", expect=2)
    assert "no conflict with id" in captured.err
    assert "Traceback" not in captured.err


def test_resolution_json(tmp_path, capsys):
    _, conflict = seed_conflict(tmp_path)
    payload = json.loads(cli(tmp_path, capsys, "memory", "conflicts",
                             "--resolve", conflict.id, "--supersede", "--yes",
                             "--json").out)
    assert payload["status"] == "superseded"
    assert payload["resolution"] == "supersede"


def test_resolved_conflicts_hidden_until_all(tmp_path, capsys):
    _, conflict = seed_conflict(tmp_path)
    cli(tmp_path, capsys, "memory", "conflicts", "--resolve", conflict.id,
        "--discard")
    assert "(no unresolved memory conflicts)" in \
        cli(tmp_path, capsys, "memory", "conflicts").out
    listing = cli(tmp_path, capsys, "memory", "conflicts", "--all").out
    assert conflict.id in listing and "[resolved]" in listing


def test_existing_memory_commands_still_work(tmp_path, capsys):
    seed_conflict(tmp_path)
    out = cli(tmp_path, capsys, "memory", "list").out
    assert "I prefer Python for my projects" in out


# ===========================================================================
# Agent seam
# ===========================================================================

def test_agent_builds_a_conflict_store(tmp_path):
    agent = Agent(config(tmp_path))
    assert isinstance(agent.memory_conflicts, JsonFileConflictStore)
    assert agent.memory_capture.conflicts is agent.memory_conflicts
    assert agent.memory_resolver.conflicts is agent.memory_conflicts


def test_no_conflict_store_without_memory(tmp_path):
    agent = Agent(config(tmp_path, memory=False))
    assert agent.memory_conflicts is None
    assert agent.pending_memory_conflicts() == []


def test_conflict_store_exists_even_with_capture_disabled(tmp_path):
    cfg = AgentConfig(
        workspace_dir=str(tmp_path), logging=LoggingConfig(level="CRITICAL"),
        memory=MemoryConfig(capture_enabled=False),
        features=FeatureFlags(memory=True)).validate()
    agent = Agent(cfg)
    assert agent.memory_capture is None
    assert agent.memory_conflicts is not None


def test_injected_conflict_store_wins(tmp_path):
    injected = InMemoryConflictStore()
    agent = Agent(config(tmp_path),
                  AgentDependencies(memory_conflicts=injected))
    assert agent.memory_conflicts is injected
    assert agent.memory_capture.conflicts is injected


def test_agent_lists_and_resolves(tmp_path):
    original, conflict = seed_conflict(tmp_path)
    agent = Agent(config(tmp_path))
    pending = agent.pending_memory_conflicts()
    assert [c.id for c in pending] == [conflict.id]

    outcome = agent.resolve_memory_conflict(
        conflict.id, ConflictResolution.SUPERSEDE, operator="alice")
    assert outcome.status is ResolutionStatus.SUPERSEDED
    assert agent.pending_memory_conflicts() == []


def test_agent_resolution_without_memory_fails(tmp_path):
    agent = Agent(config(tmp_path, memory=False))
    outcome = agent.resolve_memory_conflict("conf_1",
                                            ConflictResolution.SUPERSEDE)
    assert outcome.status is ResolutionStatus.FAILED


def test_agent_never_resolves_a_conflict_implicitly(tmp_path):
    """Detect automatically; resolve explicitly."""
    seed_conflict(tmp_path)
    agent = Agent(config(tmp_path))
    agent.remember_from(Message.user("Remember that I use tabs."))
    assert len(agent.pending_memory_conflicts()) == 1


# ===========================================================================
# END-TO-END ACCEPTANCE, across real processes
# ===========================================================================

def _run(tmp_path, body: str) -> dict:
    script = (
        "import json\n"
        "from agentcore.config.schema import (AgentConfig, FeatureFlags,\n"
        "    LoggingConfig, MemoryConfig, ModelConfig, PermissionConfig)\n"
        "from agentcore.core.agent import Agent\n"
        "from agentcore.memory.resolve import ConflictResolution\n"
        "from agentcore.models.messages import Message\n"
        "from agentcore.permissions.levels import PermissionLevel\n"
        f"cfg = AgentConfig(workspace_dir={str(tmp_path)!r},\n"
        "    model=ModelConfig(),\n"
        "    permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),\n"
        "    logging=LoggingConfig(level='CRITICAL'), memory=MemoryConfig(),\n"
        "    features=FeatureFlags(memory=True)).validate()\n"
        "agent = Agent(cfg)\n"
        + body
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_end_to_end_conflict_survives_restart(tmp_path):
    # --- Process A: detect the contradiction ------------------------------
    process_a = _run(tmp_path, '''
first = agent.remember_from(Message.user(
    "Remember that I prefer Python for my projects."))
second = agent.remember_from(Message.user(
    "Remember that I've switched to Rust for new projects."))
print(json.dumps({
    "actions": [d.action.value for d in second.decisions],
    "conflicts": len(second.conflicts_recorded),
    "stored_by_second_turn": len(second.stored),
    "live": [r.content for r in agent.memory.all_records()],
    "original_id": first.stored[0].id,
}))
''')
    assert process_a["actions"] == ["possible_conflict"]
    assert process_a["conflicts"] == 1
    assert process_a["stored_by_second_turn"] == 0
    # Python remains active; Rust is not yet authoritative.
    assert process_a["live"] == ["I prefer Python for my projects"]

    # --- Process B: the conflict is still there ---------------------------
    process_b = _run(tmp_path, '''
pending = agent.pending_memory_conflicts()
c = pending[0]
print(json.dumps({
    "count": len(pending),
    "id": c.id,
    "status": c.status.value,
    "candidate": c.candidate_content,
    "trust": c.candidate_trust.value,
    "operator_supplied": c.candidate_provenance.operator_supplied,
    "run_id": c.candidate_provenance.run_id,
    "related": [agent.memory.get(i).content for i in c.related_ids],
    "live": [r.content for r in agent.memory.all_records()],
}))
''')
    assert process_b["count"] == 1
    assert process_b["status"] == "pending"
    assert process_b["candidate"] == "I've switched to Rust for new projects"
    assert process_b["trust"] == "user_provided"
    assert process_b["operator_supplied"] is True
    assert process_b["run_id"]                       # provenance survived
    assert process_b["related"] == ["I prefer Python for my projects"]
    assert process_b["live"] == ["I prefer Python for my projects"]
    conflict_id = process_b["id"]

    # --- Process C: resolve by superseding --------------------------------
    process_c = _run(tmp_path, f'''
outcome = agent.resolve_memory_conflict(
    {conflict_id!r}, ConflictResolution.SUPERSEDE, operator="alice")
repeat = agent.resolve_memory_conflict(
    {conflict_id!r}, ConflictResolution.SUPERSEDE)
print(json.dumps({{
    "status": outcome.status.value,
    "affected": list(outcome.affected_ids),
    "repeat_status": repeat.status.value,
    "live": [r.content for r in agent.memory.all_records()],
}}))
''')
    assert process_c["status"] == "superseded"
    assert process_c["affected"] == [process_a["original_id"]]
    assert process_c["repeat_status"] == "failed"     # resolved exactly once
    assert process_c["live"] == ["I've switched to Rust for new projects"]

    # --- Process D: audit survives, Rust is current -----------------------
    process_d = _run(tmp_path, f'''
settled = agent.memory_conflicts.get({conflict_id!r})
retired = agent.memory.get({process_a["original_id"]!r})
print(json.dumps({{
    "pending": len(agent.pending_memory_conflicts()),
    "status": settled.status.value,
    "resolution": settled.resolution,
    "resolved_by": settled.resolved_by,
    "affected": list(settled.resolution_affected_ids),
    "candidate_preserved": settled.candidate_content,
    "retired_still_readable": retired.content,
    "retired_is_expired": retired.is_expired(),
    "live": [r.content for r in agent.memory.all_records()],
}}))
''')
    assert process_d["pending"] == 0
    assert process_d["status"] == "resolved"
    assert process_d["resolution"] == "supersede"
    assert process_d["resolved_by"] == "alice"
    assert process_d["affected"] == [process_a["original_id"]]
    assert process_d["candidate_preserved"] == \
        "I've switched to Rust for new projects"
    # The retired memory is still auditable, just no longer current.
    assert process_d["retired_still_readable"] == \
        "I prefer Python for my projects"
    assert process_d["retired_is_expired"] is True
    assert process_d["live"] == ["I've switched to Rust for new projects"]


def test_planner_sees_the_resolved_truth_after_restart(tmp_path):
    _, conflict = seed_conflict(tmp_path)
    Agent(config(tmp_path)).resolve_memory_conflict(
        conflict.id, ConflictResolution.SUPERSEDE)

    model = FakeModel(plan_payload("check language"))
    fresh = Agent(config(tmp_path, planning=True),
                  AgentDependencies(model=model))
    fresh.planner.plan("What language do I prefer for projects?")
    prompt = model.last_prompt
    assert "I've switched to Rust for new projects" in prompt
    assert "I prefer Python for my projects" not in prompt


def test_no_transcript_reaches_the_conflict_file(tmp_path):
    agent = Agent(config(tmp_path))
    agent.remember_from(Message.user(
        "Remember that I prefer Python for my projects."))
    agent.remember_from(Message.user(
        "Remember that I've switched to Rust for new projects. Also my "
        "neighbour's dog barked all night and I am furious."))

    blob = json.dumps([c.to_dict()
                       for c in conflict_store(tmp_path).list_all()])
    assert "neighbour" not in blob and "furious" not in blob
    assert "I've switched to Rust for new projects" in blob
