"""Memory capture wired into the Agent: flags, injection, and the full
cross-session scenario from capture through planner retrieval.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    PermissionConfig,
)
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.errors import ConfigError
from agentcore.memory import (
    InMemoryMemoryStore,
    JsonFileMemoryStore,
    MemoryTrust,
    MemoryType,
    new_memory,
)
from agentcore.memory.capture import (
    CaptureAction,
    CapturePolicy,
    CaptureResult,
    MemoryCapture,
)
from agentcore.models.messages import Message
from agentcore.permissions.levels import PermissionLevel
from agentcore.planning.planner import LLMPlanner
from agentcore.recovery.config import RecoveryConfig

SECRET = "sk-endtoend123456789abc"


class FakeModel:
    """Records prompts; returns a scripted plan for planning calls."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, prompt, *, system=None, **options):
        self.calls.append({"prompt": prompt, "system": system})
        output = self.outputs.pop(0) if len(self.outputs) > 1 \
            else self.outputs[0]
        if isinstance(output, Exception):
            raise output
        return output if isinstance(output, str) else json.dumps(output)

    @property
    def last_prompt(self):
        return self.calls[-1]["prompt"]


def plan_payload(objective="goal"):
    return {"objective": objective, "steps": [
        {"id": "s1", "description": "inspect the workspace",
         "tool_name": "workspace_info", "tool_args": {}, "depends_on": []}]}


def config(tmp_path, *, memory=True, planning=True, **memory_kwargs):
    return AgentConfig(
        workspace_dir=str(tmp_path), model=ModelConfig(),
        permissions=PermissionConfig(level=PermissionLevel.AUTONOMOUS),
        logging=LoggingConfig(level="CRITICAL"),
        recovery=RecoveryConfig(max_replans=1),
        memory=MemoryConfig(**memory_kwargs),
        features=FeatureFlags(memory=memory, planning=planning),
    ).validate()


def user(text):
    return Message.user(text)


# ===========================================================================
# Configuration
# ===========================================================================

def test_capture_defaults():
    cfg = MemoryConfig()
    assert cfg.capture_enabled is True
    assert cfg.max_capture_candidates == 5
    assert cfg.max_capture_chars == 500
    assert cfg.capture_inferred is False


def test_no_competing_global_memory_flag():
    """features.memory stays the single subsystem switch."""
    assert not hasattr(MemoryConfig(), "memory_enabled")
    assert not hasattr(MemoryConfig(), "enabled")


@pytest.mark.parametrize("field", ["max_capture_candidates",
                                   "max_capture_chars"])
def test_invalid_capture_bounds_rejected(field):
    with pytest.raises(ConfigError) as exc:
        MemoryConfig(**{field: 0}).validate()
    assert field in str(exc.value)


def test_capture_config_loads_from_env():
    from agentcore.config.loader import load_config

    cfg = load_config(env={"AGENTCORE_MEMORY__CAPTURE_ENABLED": "false",
                           "AGENTCORE_MEMORY__MAX_CAPTURE_CANDIDATES": "2",
                           "AGENTCORE_MEMORY__CAPTURE_INFERRED": "true"})
    assert cfg.memory.capture_enabled is False
    assert cfg.memory.max_capture_candidates == 2
    assert cfg.memory.capture_inferred is True


def test_capture_config_loads_from_toml(tmp_path):
    from agentcore.config.loader import load_config

    path = tmp_path / "agent.toml"
    path.write_text("[memory]\ncapture_enabled = false\n"
                    "max_capture_chars = 80\n", encoding="utf-8")
    cfg = load_config(path, env={})
    assert cfg.memory.capture_enabled is False
    assert cfg.memory.max_capture_chars == 80


def test_example_config_documents_capture():
    from pathlib import Path

    text = Path("agent.example.toml").read_text(encoding="utf-8")
    assert "capture_enabled" in text


# ===========================================================================
# Agent wiring
# ===========================================================================

def test_agent_builds_capture_when_memory_is_on(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    assert isinstance(agent.memory_capture, MemoryCapture)
    assert agent.memory_capture.store is agent.memory


def test_no_capture_without_a_memory_store(tmp_path):
    agent = Agent(config(tmp_path, memory=False))
    assert agent.memory is None
    assert agent.memory_capture is None


def test_capture_can_be_disabled_while_memory_stays_on(tmp_path):
    """A subordinate switch, not a competing one."""
    agent = Agent(config(tmp_path, memory=True, capture_enabled=False))
    assert agent.memory is not None          # retrieval still available
    assert agent.memory_capture is None      # but nothing is captured


def test_disabled_capture_persists_nothing(tmp_path):
    agent = Agent(config(tmp_path, memory=True, capture_enabled=False))
    result = agent.remember_from(user("Remember that I prefer Python."))
    assert isinstance(result, CaptureResult)
    assert result.stored == []
    assert agent.memory.count() == 0


def test_remember_from_without_memory_returns_empty(tmp_path):
    agent = Agent(config(tmp_path, memory=False))
    assert agent.remember_from(user("Remember that I prefer Python.")
                               ).stored == []


def test_remember_from_persists_and_tags_the_run(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    result = agent.remember_from(user("Remember that I prefer Python."))
    assert len(result.stored) == 1
    assert result.stored[0].provenance.run_id == agent.state.run_id


def test_remember_from_can_preview(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    result = agent.remember_from(user("Remember that I prefer Python."),
                                 persist=False)
    assert result.decisions[0].action is CaptureAction.CREATE
    assert agent.memory.count() == 0


def test_agent_passes_configured_bounds(tmp_path):
    agent = Agent(config(tmp_path, memory=True, max_capture_candidates=2,
                         max_capture_chars=40))
    assert agent.memory_capture.extractor.max_candidates == 2
    assert agent.memory_capture.policy.max_content_chars == 40


def test_agent_passes_redact_keys_to_the_policy(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    assert "api_key" in agent.memory_capture.policy.redact_keys


def test_inference_is_disallowed_by_default(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    assert agent.memory_capture.policy.allow_inferred is False


def test_status_reports_capture_availability(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    assert agent.status()["memory"]["capture"] is True
    off = Agent(config(tmp_path, memory=True, capture_enabled=False))
    assert off.status()["memory"]["capture"] is False


# ===========================================================================
# Dependency injection
# ===========================================================================

def test_injected_capture_wins(tmp_path):
    store = InMemoryMemoryStore()
    injected = MemoryCapture(store)
    agent = Agent(config(tmp_path, memory=True),
                  AgentDependencies(memory_capture=injected))
    assert agent.memory_capture is injected


def test_injected_capture_works_even_with_capture_disabled(tmp_path):
    store = InMemoryMemoryStore()
    injected = MemoryCapture(store)
    agent = Agent(config(tmp_path, memory=True, capture_enabled=False),
                  AgentDependencies(memory_capture=injected))
    assert agent.memory_capture is injected


def test_capture_uses_an_injected_custom_store(tmp_path):
    class CustomStore(InMemoryMemoryStore):
        def __init__(self):
            super().__init__()
            self.added = []

        def add(self, record, *, replace=False):
            self.added.append(record.content)
            return super().add(record, replace=replace)

    custom = CustomStore()
    agent = Agent(config(tmp_path, memory=True),
                  AgentDependencies(memory=custom))
    assert agent.memory_capture.store is custom
    agent.remember_from(user("Remember that I prefer Python."))
    assert custom.added == ["I prefer Python"]


def test_custom_policy_can_be_injected():
    store = InMemoryMemoryStore()
    capture = MemoryCapture(store, policy=CapturePolicy(allow_inferred=True))
    assert capture.policy.allow_inferred is True


def test_execution_never_triggers_capture(tmp_path):
    """remember_from is explicit; run_task must not call it implicitly."""
    model = FakeModel(plan_payload("Remember that I prefer Python"))
    agent = Agent(config(tmp_path, memory=True),
                  AgentDependencies(model=model))
    agent.execute("Remember that I prefer Python")
    agent.shutdown()
    assert agent.memory.count() == 0


# ===========================================================================
# END-TO-END ACCEPTANCE
# ===========================================================================

def test_end_to_end_conversational_memory(tmp_path):
    """Capture -> persist -> fresh session -> planner retrieval, plus the
    temporary / secret / conflict cases."""
    # --- Turn 1: an explicit remember request ------------------------------
    agent = Agent(config(tmp_path, memory=True))
    result = agent.remember_from(
        user("Remember that I prefer Python for my projects."))

    assert [d.action for d in result.decisions] == [CaptureAction.CREATE]
    stored = result.stored[0]
    assert stored.content == "I prefer Python for my projects"
    assert stored.trust is MemoryTrust.USER_PROVIDED
    assert stored.provenance.operator_supplied is True
    assert stored.provenance.source == "explicit_request"

    # --- A temporary turn creates nothing ----------------------------------
    before = agent.memory.count()
    tired = agent.remember_from(user("I'm tired today."))
    assert tired.stored == []
    assert agent.memory.count() == before

    # --- Explicit project context ------------------------------------------
    project = agent.remember_from(
        user("Remember that EcoClean Connect is my main project."))
    assert len(project.stored) == 1
    assert "EcoClean Connect" in project.stored[0].content

    # --- A secret is rejected and never written ----------------------------
    secret = agent.remember_from(user(f"Remember my API key is {SECRET}."))
    assert [d.action for d in secret.decisions] == [CaptureAction.REJECT]
    assert secret.stored == []
    on_disk = json.dumps([r.to_dict() for r in agent.memory.all_records()])
    assert SECRET not in on_disk
    assert "api key" not in on_disk.lower()

    # --- Conflict: no silent destruction -----------------------------------
    conflict = agent.remember_from(
        user("Remember that I've switched to Rust for all new projects."))
    assert [d.action for d in conflict.decisions] == \
        [CaptureAction.POSSIBLE_CONFLICT]
    assert conflict.decisions[0].related_ids == (stored.id,)
    assert agent.memory.get(stored.id).content == \
        "I prefer Python for my projects"
    assert agent.memory.get(stored.id).revision == 1

    # --- A fresh session, same store, retrieves it through the planner -----
    model = FakeModel(plan_payload("check my language preference"))
    session_two = Agent(config(tmp_path, memory=True),
                        AgentDependencies(model=model))
    assert isinstance(session_two.planner, LLMPlanner)
    session_two.planner.plan(
        "What language do I usually prefer for projects?")

    prompt = model.last_prompt
    assert "RETRIEVED MEMORY" in prompt
    assert "I prefer Python for my projects" in prompt
    assert "trust=user_provided" in prompt
    assert SECRET not in prompt

    # Exactly two records ever written: the preference and the project.
    assert agent.memory.count() == 2


def test_captured_memory_survives_a_separate_process(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    agent.remember_from(user("Remember that I prefer Python for my projects."))

    script = f'''
import json
from agentcore.memory import JsonFileMemoryStore
store = JsonFileMemoryStore({str(tmp_path / "memory")!r})
hit = store.search("Python").hits[0]
print(json.dumps({{"content": hit.record.content,
                   "trust": hit.record.trust.value,
                   "source": hit.record.provenance.source,
                   "operator": hit.record.provenance.operator_supplied,
                   "count": store.count()}}))
'''
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True)
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload == {"content": "I prefer Python for my projects",
                       "trust": "user_provided",
                       "source": "explicit_request",
                       "operator": True, "count": 1}


def test_no_transcript_reaches_disk(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    agent.remember_from(user(
        "Remember that I prefer Python. Also my neighbour's dog barked all "
        "night and I am furious about the bins."))
    contents = [r.content for r in agent.memory.all_records()]
    assert contents == ["I prefer Python"]
    blob = json.dumps([r.to_dict() for r in agent.memory.all_records()])
    assert "neighbour" not in blob and "bins" not in blob
