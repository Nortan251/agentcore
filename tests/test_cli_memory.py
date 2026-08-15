"""`agentcore memory`: the operator surface for inspecting and correcting
persistent memory, plus the Agent-level correction seam and the full
end-to-end correction/forget lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from agentcore.cli import main, render_memories
from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.memory import (
    InMemoryMemoryStore,
    JsonFileMemoryStore,
    MemoryProvenance,
    MemoryTrust,
    MemoryType,
    new_memory,
)
from agentcore.memory.capture import CaptureAction
from agentcore.memory.resolve import (
    ConflictResolution,
    MemoryResolver,
    ResolutionStatus,
)
from agentcore.models.messages import Message
from agentcore.permissions.levels import PermissionLevel
from agentcore.recovery.config import RecoveryConfig

SECRET = "sk-cli-memory-must-not-leak-99"


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
    import os

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


def store_for(tmp_path):
    return JsonFileMemoryStore(tmp_path / "memory")


def seed(tmp_path):
    store = store_for(tmp_path)
    python = store.add(new_memory(
        "I prefer Python for my projects", type=MemoryType.PREFERENCE,
        trust=MemoryTrust.USER_PROVIDED, tags=["language"],
        provenance=MemoryProvenance(operator_supplied=True, run_id="run_1",
                                    source="explicit_request")))
    project = store.add(new_memory("EcoClean Connect is my main project",
                                   tags=["project"]))
    return store, python, project


def digest(tmp_path):
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest()
            for p in (tmp_path / "memory").iterdir()}


# ===========================================================================
# list / show
# ===========================================================================

def test_list_shows_stored_memories(tmp_path, capsys):
    seed(tmp_path)
    out = cli(tmp_path, capsys, "memory", "list").out
    assert "I prefer Python for my projects" in out
    assert "EcoClean Connect is my main project" in out
    assert "MEMORY ID" in out and "TRUST" in out


def test_list_json(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    rows = json.loads(cli(tmp_path, capsys, "memory", "list", "--json").out)
    row = next(r for r in rows if r["id"] == python.id)
    assert row["trust"] == "user_provided"
    assert row["recorded_by"] == "user"
    assert row["revision"] == 1
    assert row["retired"] is False


def test_list_query_filters(tmp_path, capsys):
    seed(tmp_path)
    out = cli(tmp_path, capsys, "memory", "list", "--query", "Python").out
    assert "I prefer Python" in out
    assert "EcoClean" not in out


def test_list_empty(tmp_path, capsys):
    store_for(tmp_path)
    assert "(no memories stored)" in cli(tmp_path, capsys,
                                         "memory", "list").out


def test_list_limit(tmp_path, capsys):
    store = store_for(tmp_path)
    for i in range(5):
        store.add(new_memory(f"durable fact number {i}"))
    out = cli(tmp_path, capsys, "memory", "list", "--limit", "2").out
    assert len([l for l in out.splitlines() if l.startswith("mem_")]) == 2
    assert "showing 2 of 5" in out


def test_show_one_memory(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    out = cli(tmp_path, capsys, "memory", "show", python.id).out
    assert python.id in out
    assert "user_provided" in out
    assert "I prefer Python for my projects" in out
    assert "run_1" in out


def test_show_unknown_id(tmp_path, capsys):
    seed(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "show", "mem_nope", expect=2)
    assert "no memory with id" in captured.err
    assert "Traceback" not in captured.err


def test_memory_disabled_is_refused(tmp_path, capsys):
    seed(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "list", memory=False, expect=2)
    assert "memory is disabled" in captured.err


def test_render_memories_empty():
    assert render_memories([]) == "(no memories stored)"


# ===========================================================================
# forget
# ===========================================================================

def test_forget_requires_confirmation(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    before = digest(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "forget", python.id, expect=2)
    assert "--yes" in captured.err
    assert digest(tmp_path) == before          # nothing touched


def test_forget_with_confirmation(tmp_path, capsys):
    store, python, project = seed(tmp_path)
    out = cli(tmp_path, capsys, "memory", "forget", python.id, "--yes").out
    assert f"Deleted memory {python.id}" in out
    assert store_for(tmp_path).count(include_expired=True) == 1
    assert store_for(tmp_path).get(project.id).content == project.content


def test_forget_unknown_id(tmp_path, capsys):
    seed(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "forget", "mem_nope", "--yes",
                   expect=2)
    assert "no memory with id" in captured.err
    assert store_for(tmp_path).count() == 2


def test_forget_json(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    payload = json.loads(cli(tmp_path, capsys, "memory", "forget", python.id,
                             "--yes", "--json").out)
    assert payload["status"] == "deleted"
    assert payload["affected"] == [python.id]


# ===========================================================================
# correct / retire
# ===========================================================================

def test_correct_updates_in_place(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    out = cli(tmp_path, capsys, "memory", "correct", python.id,
              "I prefer Rust for my projects").out
    assert "revision:  1 -> 2" in out

    updated = store_for(tmp_path).get(python.id)
    assert updated.content == "I prefer Rust for my projects"
    assert updated.created_at == python.created_at
    assert updated.provenance.run_id == "run_1"
    assert store_for(tmp_path).count() == 2       # no duplicate


def test_correct_refuses_credentials(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    captured = cli(tmp_path, capsys, "memory", "correct", python.id,
                   f"my api key is {SECRET}", expect=2)
    assert "credential" in captured.err
    assert store_for(tmp_path).get(python.id).content == python.content


def test_correct_refuses_empty_content(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    cli(tmp_path, capsys, "memory", "correct", python.id, "   ", expect=2)
    assert store_for(tmp_path).get(python.id).revision == 1


def test_retire_keeps_the_record_but_hides_it(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    out = cli(tmp_path, capsys, "memory", "retire", python.id).out
    assert "Retired memory" in out

    store = store_for(tmp_path)
    assert python.id not in {r.id for r in store.all_records()}
    assert store.get(python.id).content == python.content
    assert store.search("Python").hits == ()


def test_retired_memory_shown_only_with_all(tmp_path, capsys):
    _, python, _ = seed(tmp_path)
    cli(tmp_path, capsys, "memory", "retire", python.id)
    assert python.id not in cli(tmp_path, capsys, "memory", "list").out
    listing = cli(tmp_path, capsys, "memory", "list", "--all").out
    assert python.id in listing and "(retired)" in listing


# ===========================================================================
# Agent seam
# ===========================================================================

def test_agent_builds_a_resolver(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    assert isinstance(agent.memory_resolver, MemoryResolver)


def test_no_resolver_without_memory(tmp_path):
    agent = Agent(config(tmp_path, memory=False))
    assert agent.memory_resolver is None
    outcome = agent.correct_memory(Message.user("Forget that."))
    assert outcome.status is ResolutionStatus.FAILED


def test_resolver_exists_even_when_capture_is_disabled(tmp_path):
    """An operator must be able to correct memory regardless of capture."""
    cfg = AgentConfig(
        workspace_dir=str(tmp_path), logging=LoggingConfig(level="CRITICAL"),
        memory=MemoryConfig(capture_enabled=False),
        features=FeatureFlags(memory=True)).validate()
    agent = Agent(cfg)
    assert agent.memory_capture is None
    assert agent.memory_resolver is not None


def test_injected_resolver_wins(tmp_path):
    injected = MemoryResolver(InMemoryMemoryStore())
    agent = Agent(config(tmp_path), AgentDependencies(memory_resolver=injected))
    assert agent.memory_resolver is injected


def test_agent_correct_memory_forgets_a_targeted_fact(tmp_path):
    agent = Agent(config(tmp_path))
    agent.memory.add(new_memory("My timezone is Europe/London",
                                tags=["timezone"]))
    outcome = agent.correct_memory(Message.user("Forget my timezone."))
    assert outcome.status is ResolutionStatus.DELETED
    assert agent.memory.count() == 0


def test_agent_correct_memory_is_safe_when_ambiguous(tmp_path):
    agent = Agent(config(tmp_path))
    agent.memory.add(new_memory("I prefer Python", tags=["language"]))
    agent.memory.add(new_memory("EcoClean Connect is my project",
                                tags=["project"]))
    outcome = agent.correct_memory(Message.user("Forget that."))
    assert outcome.status is ResolutionStatus.UNRESOLVED
    assert agent.memory.count() == 2


def test_agent_ignores_a_non_correction(tmp_path):
    agent = Agent(config(tmp_path))
    outcome = agent.correct_memory(Message.user("I prefer Python."))
    assert outcome.status is ResolutionStatus.NO_ACTION


def test_agent_resolve_memory_applies_a_decision(tmp_path):
    agent = Agent(config(tmp_path))
    result = agent.remember_from(user_turn := Message.user(
        "Remember that I prefer Python for my projects."))
    assert result.stored

    preview = agent.memory_capture.preview(Message.user(
        "Remember that I've switched to Rust for new projects."))
    decision = preview.decisions[0]
    assert decision.action is CaptureAction.POSSIBLE_CONFLICT

    pending = agent.resolve_memory(decision)
    assert pending.status is ResolutionStatus.PENDING_CONFLICT

    applied = agent.resolve_memory(
        decision, conflict=ConflictResolution.SUPERSEDE)
    assert applied.status is ResolutionStatus.SUPERSEDED


# ===========================================================================
# END-TO-END ACCEPTANCE
# ===========================================================================

def test_end_to_end_correction_and_forget(tmp_path):
    """Capture -> conflict -> explicit supersede -> new session retrieval ->
    forget -> ambiguous forget."""
    agent = Agent(config(tmp_path, planning=True),
                  AgentDependencies(model=FakeModel(plan_payload())))

    # --- initial memory ---------------------------------------------------
    first = agent.remember_from(
        Message.user("Remember that I prefer Python for my projects."))
    original = first.stored[0]
    assert original.content == "I prefer Python for my projects"

    # --- conflicting statement: detected, nothing destroyed ---------------
    preview = agent.memory_capture.preview(Message.user(
        "Remember that I've switched to Rust for new projects."))
    decision = preview.decisions[0]
    assert decision.action is CaptureAction.POSSIBLE_CONFLICT
    assert decision.related_ids == (original.id,)

    pending = agent.resolve_memory(decision)
    assert pending.status is ResolutionStatus.PENDING_CONFLICT
    assert agent.memory.get(original.id).content == original.content
    assert agent.memory.get(original.id).revision == 1

    # --- explicit confirmation: supersede ---------------------------------
    applied = agent.resolve_memory(decision,
                                   conflict=ConflictResolution.SUPERSEDE)
    assert applied.status is ResolutionStatus.SUPERSEDED
    assert applied.record.trust is MemoryTrust.USER_PROVIDED
    assert applied.record.provenance.operator_supplied is True
    # Old record retired but intact and auditable.
    retired = agent.memory.get(original.id)
    assert retired.content == original.content
    assert retired.created_at == original.created_at
    assert retired.revision == 2

    # --- new session: Rust is authoritative, Python is not ----------------
    model = FakeModel(plan_payload("check language"))
    session_two = Agent(config(tmp_path, planning=True),
                        AgentDependencies(model=model))
    session_two.planner.plan("What language do I prefer for projects?")
    prompt = model.last_prompt
    assert "RETRIEVED MEMORY" in prompt
    assert "I've switched to Rust for new projects" in prompt
    assert "I prefer Python for my projects" not in prompt

    # --- a subject that shares no words cannot be targeted ----------------
    # Retrieval is literal (no embeddings yet), so "language preference" does
    # not match "I've switched to Rust". Refusing is the correct, safe answer.
    vague = session_two.correct_memory(
        Message.user("Forget my language preference."))
    assert vague.status is ResolutionStatus.UNRESOLVED
    assert vague.changed is False
    # One live record (Rust) plus the retired Python one, untouched.
    assert session_two.memory.count(include_expired=False) == 1
    assert session_two.memory.count(include_expired=True) == 2

    # --- targeted forget, using words the memory actually contains --------
    forget = session_two.correct_memory(
        Message.user("Forget what I told you about Rust."))
    assert forget.status is ResolutionStatus.DELETED
    assert forget.affected_ids == (applied.record.id,)

    third = FakeModel(plan_payload("check again"))
    session_three = Agent(config(tmp_path, planning=True),
                          AgentDependencies(model=third))
    session_three.planner.plan("What language do I prefer for projects?")
    assert "Rust" not in third.last_prompt

    # --- ambiguous forget changes nothing ---------------------------------
    store = session_three.memory
    store.add(new_memory("EcoClean Connect is my main project",
                         tags=["project"]))
    store.add(new_memory("My timezone is Europe/London", tags=["timezone"]))
    before = store.count(include_expired=True)

    ambiguous = session_three.correct_memory(Message.user("Forget that."))
    assert ambiguous.status is ResolutionStatus.UNRESOLVED
    assert ambiguous.changed is False
    assert len(ambiguous.candidate_ids) >= 2
    assert store.count(include_expired=True) == before


def test_corrections_survive_a_separate_process(tmp_path):
    agent = Agent(config(tmp_path))
    record = agent.memory.add(new_memory("I prefer Python for my projects",
                                         type=MemoryType.PREFERENCE))
    MemoryResolver(agent.memory).forget(record.id, reason="test")

    script = f'''
import json
from agentcore.memory import JsonFileMemoryStore
store = JsonFileMemoryStore({str(tmp_path / "memory")!r})
print(json.dumps({{"count": store.count(include_expired=True),
                   "exists": store.exists({record.id!r})}}))
'''
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {
        "count": 0, "exists": False}


def test_supersede_survives_a_separate_process(tmp_path):
    agent = Agent(config(tmp_path))
    agent.remember_from(
        Message.user("Remember that I prefer Python for my projects."))
    preview = agent.memory_capture.preview(Message.user(
        "Remember that I've switched to Rust for new projects."))
    agent.resolve_memory(preview.decisions[0],
                         conflict=ConflictResolution.SUPERSEDE)

    script = f'''
import json
from agentcore.memory import JsonFileMemoryStore
store = JsonFileMemoryStore({str(tmp_path / "memory")!r})
print(json.dumps({{"live": [r.content for r in store.all_records()],
                   "total": store.count(include_expired=True)}}))
'''
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True)
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["live"] == ["I've switched to Rust for new projects"]
    assert payload["total"] == 2       # the retired one is still on disk


def test_memory_listing_withholds_credential_prose(tmp_path, capsys):
    """`_safe_text` alone misses "my api key is sk-...": the keyword list is
    identifier-shaped, and remembered text is prose. The memory layer's
    stronger detector is applied first."""
    store = store_for(tmp_path)
    store.add(new_memory("my api key is sk-leaky-abcdef1234567890"))

    out = cli(tmp_path, capsys, "memory", "list").out
    assert "sk-leaky-abcdef1234567890" not in out
    assert "withheld" in out

    payload = json.loads(cli(tmp_path, capsys, "memory", "list", "--json").out)
    assert "sk-leaky-abcdef1234567890" not in json.dumps(payload)


def test_memory_show_withholds_credential_prose(tmp_path, capsys):
    store = store_for(tmp_path)
    record = store.add(new_memory("my password is hunter2 for the portal"))
    out = cli(tmp_path, capsys, "memory", "show", record.id).out
    assert "hunter2" not in out
    assert "withheld" in out
