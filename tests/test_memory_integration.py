"""Memory configuration, the feature flag, dependency injection, and the
end-to-end durability scenario.

The governing rule tested here: with ``features.memory`` off — the default —
the agent behaves exactly as it did before this layer existed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from agentcore.config.loader import load_config
from agentcore.config.schema import (
    AgentConfig,
    FeatureFlags,
    LoggingConfig,
    MemoryConfig,
)
from agentcore.core.agent import Agent, AgentDependencies, build_agent
from agentcore.errors import ConfigError
from agentcore.memory import (
    JsonFileMemoryStore,
    MemoryProvenance,
    MemoryQuery,
    MemoryScope,
    MemoryTrust,
    MemoryType,
    new_memory,
)
from agentcore.memory.store import InMemoryMemoryStore
from agentcore.util.clock import utc_now


def _toml(path):
    """Render a path for a TOML basic string.

    Backslashes are escape characters in TOML basic strings, and Windows paths
    use them as separators, so forward slashes (equally accepted by Windows)
    are used instead. Without this, a config written on Windows fails to parse.
    """
    return str(path).replace("\\", "/")


QUIET = LoggingConfig(level="CRITICAL")


def config(tmp_path, *, memory=False, **memory_kwargs) -> AgentConfig:
    return AgentConfig(
        workspace_dir=str(tmp_path), logging=QUIET,
        memory=MemoryConfig(**memory_kwargs),
        features=FeatureFlags(memory=memory),
    ).validate()


# ===========================================================================
# Configuration
# ===========================================================================

def test_memory_config_defaults():
    cfg = MemoryConfig()
    assert cfg.directory is None
    assert cfg.max_records == 1000
    assert cfg.max_search_results == 20
    assert cfg.max_content_chars == 4000


def test_agent_config_exposes_memory_and_validates_it():
    assert isinstance(AgentConfig().memory, MemoryConfig)
    with pytest.raises(ConfigError):
        AgentConfig(memory=MemoryConfig(max_records=0)).validate()


@pytest.mark.parametrize("field,value", [
    ("max_records", 0), ("max_search_results", 0), ("max_content_chars", 0)])
def test_invalid_limits_rejected(field, value):
    with pytest.raises(ConfigError) as exc:
        MemoryConfig(**{field: value}).validate()
    assert field in str(exc.value)


def test_memory_config_loads_from_toml(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        "[memory]\n"
        f'directory = "{_toml(tmp_path / "mem")}"\n'
        "max_records = 50\n"
        "max_search_results = 5\n"
        "max_content_chars = 100\n"
        "\n[features]\nmemory = true\n", encoding="utf-8")
    cfg = load_config(path, env={})
    assert cfg.memory.max_records == 50
    assert cfg.memory.max_search_results == 5
    assert cfg.memory.max_content_chars == 100
    assert cfg.features.memory is True


def test_memory_config_env_override_uses_existing_convention():
    cfg = load_config(env={"AGENTCORE_MEMORY__MAX_RECORDS": "3",
                           "AGENTCORE_FEATURES__MEMORY": "true"})
    assert cfg.memory.max_records == 3
    assert cfg.features.memory is True


def test_no_duplicate_enable_flag():
    """features.memory is the single switch; MemoryConfig must not add one."""
    assert not hasattr(MemoryConfig(), "enabled")


def test_example_config_documents_the_memory_section():
    from pathlib import Path

    text = Path("agent.example.toml").read_text(encoding="utf-8")
    assert "[memory]" in text
    assert "max_records" in text


# ===========================================================================
# Feature flag
# ===========================================================================

def test_memory_disabled_by_default(tmp_path):
    agent = Agent(config(tmp_path))
    assert agent.memory is None
    assert agent.memory_info() is None
    assert agent.status()["memory"] is None


def test_disabled_memory_writes_nothing_to_disk(tmp_path):
    agent = Agent(config(tmp_path))
    agent.execute("do something")
    agent.shutdown()
    assert not (tmp_path / "memory").exists()


def test_enabling_the_flag_builds_a_store(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    assert agent.memory is not None
    assert isinstance(agent.memory, JsonFileMemoryStore)
    assert agent.memory.directory == tmp_path / "memory"


def test_enabled_store_starts_empty(tmp_path):
    """Enabling memory must not itself record anything."""
    agent = Agent(config(tmp_path, memory=True))
    agent.execute("do something")
    agent.shutdown()
    assert agent.memory.count() == 0


def test_enabled_store_honours_configured_limits(tmp_path):
    agent = Agent(config(tmp_path, memory=True, max_records=7,
                         max_search_results=2))
    assert agent.memory.config.max_records == 7
    assert agent.memory.config.max_search_results == 2


def test_memory_info_reports_shape_only(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    agent.memory.add(new_memory("a private fact"))
    info = agent.memory_info()
    assert info["backend"] == "JsonFileMemoryStore"
    assert info["records"] == 1
    assert "a private fact" not in json.dumps(info)


def test_status_includes_memory_without_content(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    agent.memory.add(new_memory("secret preference"))
    assert "secret preference" not in json.dumps(agent.status(), default=str)
    assert agent.status()["memory"]["records"] == 1


def test_broken_memory_directory_does_not_stop_startup(tmp_path):
    """Memory is optional; a bad path must not break existing behaviour."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    agent = Agent(config(tmp_path, memory=True, directory=str(blocker / "mem")))
    assert agent.memory is None
    assert agent.execute("still works").status is not None


# ===========================================================================
# Dependency injection
# ===========================================================================

def test_injected_store_is_used(tmp_path):
    injected = InMemoryMemoryStore()
    agent = Agent(config(tmp_path), AgentDependencies(memory=injected))
    assert agent.memory is injected


def test_injection_wins_over_the_disabled_flag(tmp_path):
    """Explicit injection always wins, mirroring planner/verifier/recovery."""
    injected = InMemoryMemoryStore()
    agent = Agent(config(tmp_path, memory=False),
                  AgentDependencies(memory=injected))
    assert agent.memory is injected


def test_injection_wins_over_the_enabled_flag(tmp_path):
    injected = InMemoryMemoryStore()
    agent = Agent(config(tmp_path, memory=True),
                  AgentDependencies(memory=injected))
    assert agent.memory is injected


def test_build_agent_accepts_memory(tmp_path):
    injected = InMemoryMemoryStore()
    assert build_agent(config(tmp_path), memory=injected).memory is injected


def test_a_custom_backend_satisfies_the_seam(tmp_path):
    """Agent depends on the interface, never on a concrete backend."""

    class RecordingStore:
        def __init__(self):
            self.calls = []

        def remember(self, key, value, *, tags=()):
            self.calls.append(("remember", key))

        def recall(self, key):
            self.calls.append(("recall", key))
            return None

        def search(self, query, *, limit=5):
            self.calls.append(("search", query))
            return []

    custom = RecordingStore()
    agent = Agent(config(tmp_path), AgentDependencies(memory=custom))
    agent.memory.remember("editor", "vim")
    agent.memory.search("editor")
    assert custom.calls == [("remember", "editor"), ("search", "editor")]


def test_injected_memory_does_not_alter_the_execution_pipeline(tmp_path):
    """The core loop must behave identically with and without memory."""
    without = Agent(config(tmp_path / "a"))
    plain = without.execute("write something")
    without.shutdown()

    with_memory = Agent(config(tmp_path / "b"),
                        AgentDependencies(memory=InMemoryMemoryStore()))
    memorised = with_memory.execute("write something")
    with_memory.shutdown()

    assert plain.status == memorised.status
    assert plain.executed == memorised.executed
    assert plain.failed == memorised.failed
    # And nothing was written to memory as a side effect of running a task.
    assert with_memory.memory.count() == 0


def test_execution_never_writes_memory_automatically(tmp_path):
    agent = Agent(config(tmp_path, memory=True))
    agent.execute("a task that mentions an api_key and a password")
    agent.shutdown()
    assert agent.memory.count() == 0
    assert agent.memory.all_records() == []


# ===========================================================================
# END-TO-END ACCEPTANCE
# ===========================================================================

def _in_fresh_process(directory, body: str) -> dict:
    """Run ``body`` in a genuinely separate interpreter and return its JSON."""
    script = (
        "import json\n"
        "from agentcore.memory import (JsonFileMemoryStore, MemoryQuery,\n"
        "    MemoryScope, MemoryTrust, MemoryType, new_memory)\n"
        f"store = JsonFileMemoryStore({str(directory)!r})\n"
        + body
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_end_to_end_memory_lifecycle_across_processes(tmp_path):
    """Create -> persist -> fresh process -> search -> update -> reload ->
    verify -> delete -> verify gone. Every step in a real new interpreter."""
    directory = tmp_path / "memory"

    # 1-2. Create and persist.
    store = JsonFileMemoryStore(directory)
    record = store.add(new_memory(
        "Preferred project language is Python",
        type=MemoryType.PREFERENCE, scope=MemoryScope.PROJECT,
        scope_key="acme", tags=["language", "python"],
        trust=MemoryTrust.USER_PROVIDED,
        provenance=MemoryProvenance(operator_supplied=True, run_id="run_42",
                                    task_id="task_1", source="operator",
                                    note="stated during setup")))

    # 3-5. Fresh process: retrieve by deterministic search; check provenance.
    found = _in_fresh_process(directory, '''
hit = store.search("Preferred project language").hits[0]
print(json.dumps({
    "id": hit.record.id, "content": hit.record.content,
    "match": hit.match.value, "reason": hit.reason,
    "type": hit.record.type.value, "scope": hit.record.scope.value,
    "scope_key": hit.record.scope_key, "trust": hit.record.trust.value,
    "tags": list(hit.record.tags), "revision": hit.record.revision,
    "operator_supplied": hit.record.provenance.operator_supplied,
    "run_id": hit.record.provenance.run_id,
    "task_id": hit.record.provenance.task_id,
    "source": hit.record.provenance.source,
    "note": hit.record.provenance.note,
}))
''')
    assert found["id"] == record.id
    assert found["content"] == "Preferred project language is Python"
    assert found["match"] == "substring"
    # The reason quotes the caller's query verbatim (matching is
    # case-insensitive; the explanation is not rewritten).
    assert found["reason"] == "content contains 'Preferred project language'"
    assert found["type"] == "preference"
    assert found["scope"] == "project" and found["scope_key"] == "acme"
    assert found["trust"] == "user_provided"
    assert found["tags"] == ["language", "python"]
    assert found["revision"] == 1
    assert found["operator_supplied"] is True
    assert found["run_id"] == "run_42" and found["task_id"] == "task_1"
    assert found["source"] == "operator"
    assert found["note"] == "stated during setup"

    # 6-7. Update in another fresh process, and persist.
    updated = _in_fresh_process(directory, f'''
rec = store.update({record.id!r}, content="Preferred project language is Rust")
print(json.dumps({{"id": rec.id, "content": rec.content,
                   "revision": rec.revision,
                   "created_at": rec.created_at.isoformat(),
                   "operator_supplied": rec.provenance.operator_supplied,
                   "run_id": rec.provenance.run_id}}))
''')
    assert updated["revision"] == 2
    assert updated["created_at"] == record.created_at.isoformat()
    assert updated["operator_supplied"] is True     # provenance survived
    assert updated["run_id"] == "run_42"

    # 8-9. Reload from yet another process and verify the updated value.
    reloaded = _in_fresh_process(directory, '''
hits = store.search("Preferred project language").hits
old = store.search("is Python").hits
print(json.dumps({"content": hits[0].record.content,
                  "revision": hits[0].record.revision,
                  "count": len(hits), "old_value_hits": len(old)}))
''')
    assert reloaded["content"] == "Preferred project language is Rust"
    assert reloaded["revision"] == 2
    assert reloaded["count"] == 1
    assert reloaded["old_value_hits"] == 0          # not duplicated

    # 10-11. Delete in a fresh process; confirm retrieval no longer returns it.
    deleted = _in_fresh_process(directory, f'''
store.delete({record.id!r})
print(json.dumps({{"hits": len(store.search("Preferred project language")),
                   "exists": store.exists({record.id!r}),
                   "count": store.count()}}))
''')
    assert deleted == {"hits": 0, "exists": False, "count": 0}

    # And the original process agrees: the file is genuinely gone.
    assert not (directory / f"{record.id}.json").exists()
    assert JsonFileMemoryStore(directory).count() == 0


def test_agent_receives_memory_and_reads_what_a_previous_agent_wrote(tmp_path):
    """The injected seam works end to end across two Agent instances."""
    first = Agent(config(tmp_path, memory=True))
    first.memory.add(new_memory("Preferred project language is Python",
                                type=MemoryType.PREFERENCE,
                                tags=["language"],
                                trust=MemoryTrust.USER_PROVIDED))
    first.shutdown()

    second = Agent(config(tmp_path, memory=True))
    hit = second.memory.search("language").hits[0]
    assert hit.record.content == "Preferred project language is Python"
    assert hit.match.value == "tag"
    assert hit.record.trust is MemoryTrust.USER_PROVIDED
