"""Memory persistence and retrieval: CRUD, ranking, conflicts, safety.

Both backends run through the same rule tests, because the whole point of
sharing ``_BaseMemoryStore`` is that behaviour cannot diverge between them.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agentcore.config.schema import LoggingConfig
from agentcore.core.interfaces import MemoryStore as MemoryStoreProtocol
from agentcore.memory.config import MemoryConfig
from agentcore.memory.errors import (
    MemoryConflictError,
    MemoryLimitError,
    MemoryNotFoundError,
    MemoryValidationError,
)
from agentcore.memory.models import (
    MemoryProvenance,
    MemoryRecord,
    MemoryScope,
    MemoryTrust,
    MemoryType,
    new_memory,
)
from agentcore.memory.query import (
    MatchKind,
    MemoryHit,
    MemoryQuery,
    MemorySearchResult,
    search_records,
)
from agentcore.memory.store import (
    InMemoryMemoryStore,
    JsonFileMemoryStore,
    MemoryBackend,
    build_memory_store,
)
from agentcore.observability.logging import configure_logging

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SECRET = "hunter2-must-never-be-logged"


@pytest.fixture(params=["memory", "json"])
def store(request, tmp_path):
    """Every rule test runs against both backends."""
    if request.param == "memory":
        return InMemoryMemoryStore()
    return JsonFileMemoryStore(tmp_path / "memory")


@pytest.fixture
def caplog_json():
    """Capture agentcore's structured log output as raw text."""
    configure_logging(LoggingConfig(level="DEBUG"))
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger("agentcore")
    for existing in root.handlers:
        handler.setFormatter(existing.formatter)
    root.addHandler(handler)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        configure_logging(LoggingConfig(level="CRITICAL"))


# ===========================================================================
# Protocol conformance
# ===========================================================================

def test_backends_satisfy_the_existing_memorystore_protocol(store):
    """The seam declared in core.interfaces must work unchanged."""
    assert isinstance(store, MemoryStoreProtocol)


def test_backends_satisfy_the_richer_backend_protocol(store):
    assert isinstance(store, MemoryBackend)


def test_protocol_remember_recall_search(store):
    store.remember("editor", "vim")
    assert store.recall("editor").content == "vim"
    assert [r.content for r in store.search_text("vim")] == ["vim"]


def test_protocol_remember_uses_the_key_as_a_tag_not_the_id(store):
    """Repeated keys must not collide, so the key is a retrieval handle."""
    store.remember("editor", "vim")
    store.remember("editor", "emacs")
    assert store.count() == 2
    assert {h.record.content for h in store.search("editor")} == {"vim", "emacs"}


def test_protocol_recall_returns_none_when_absent(store):
    assert store.recall("nothing") is None


# ===========================================================================
# Create
# ===========================================================================

def test_add_and_get(store):
    record = store.add(new_memory("Preferred project language is Python"))
    assert store.get(record.id) == record


def test_add_validates(store):
    with pytest.raises(MemoryValidationError):
        store.add(MemoryRecord(content=""))


def test_add_enforces_configured_content_limit(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "m", MemoryConfig(max_content_chars=10))
    with pytest.raises(MemoryValidationError):
        store.add(new_memory("x" * 11))


def test_add_enforces_the_record_ceiling(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "m", MemoryConfig(max_records=2))
    store.add(new_memory("one"))
    store.add(new_memory("two"))
    with pytest.raises(MemoryLimitError) as exc:
        store.add(new_memory("three"))
    assert exc.value.details["maximum"] == 2


def test_ceiling_does_not_block_overwriting_an_existing_record(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "m", MemoryConfig(max_records=1))
    record = store.add(new_memory("one"))
    store.add(record.evolve(content="one revised"), replace=True)
    assert store.get(record.id).content == "one revised"


def test_get_missing_raises(store):
    with pytest.raises(MemoryNotFoundError):
        store.get("mem_nope")


def test_exists(store):
    record = store.add(new_memory("v"))
    assert store.exists(record.id) is True
    assert store.exists("mem_nope") is False


# ===========================================================================
# Duplicate and conflict behaviour
# ===========================================================================

def test_same_id_conflicts_rather_than_overwriting(store):
    record = store.add(new_memory("original"))
    clashing = MemoryRecord(content="different fact", id=record.id)
    with pytest.raises(MemoryConflictError) as exc:
        store.add(clashing)
    assert exc.value.details["memory_id"] == record.id
    assert store.get(record.id).content == "original"     # untouched


def test_same_id_overwrites_only_when_explicitly_asked(store):
    record = store.add(new_memory("original"))
    store.add(MemoryRecord(content="replacement", id=record.id), replace=True)
    assert store.get(record.id).content == "replacement"


def test_identical_content_with_different_ids_is_kept_separately(store):
    """They may differ in scope/trust/provenance; merging would lose that."""
    first = store.add(new_memory("same words", trust=MemoryTrust.USER_PROVIDED))
    second = store.add(new_memory("same words", trust=MemoryTrust.UNCERTAIN))
    assert first.id != second.id
    assert store.count() == 2


def test_find_duplicates_reports_without_resolving(store):
    a = store.add(new_memory("Same  Words"))
    b = store.add(new_memory("same words"))
    store.add(new_memory("something else"))
    duplicates = store.find_duplicates()
    assert duplicates == {"same words": sorted([a.id, b.id])}
    assert store.count() == 3                     # nothing was merged


def test_conflicting_facts_in_one_scope_are_both_retained(store):
    """Contradiction needs judgement this layer refuses to fake."""
    store.add(new_memory("the port is 8080", tags=["port"]))
    store.add(new_memory("the port is 9090", tags=["port"]))
    contents = {h.record.content for h in store.search("port")}
    assert contents == {"the port is 8080", "the port is 9090"}


# ===========================================================================
# Update
# ===========================================================================

def test_update_preserves_identity_and_provenance(store):
    provenance = MemoryProvenance(operator_supplied=True, run_id="run_1")
    record = store.add(new_memory("old", provenance=provenance))
    updated = store.update(record.id, content="new")

    assert updated.id == record.id
    assert updated.created_at == record.created_at
    assert updated.provenance == provenance
    assert updated.revision == 2
    assert store.get(record.id).content == "new"


def test_update_can_change_trust_and_tags(store):
    record = store.add(new_memory("v", trust=MemoryTrust.INFERRED))
    updated = store.update(record.id, trust=MemoryTrust.VERIFIED,
                           tags=["Checked"])
    assert updated.trust is MemoryTrust.VERIFIED
    assert updated.tags == ("checked",)


def test_update_can_replace_provenance_explicitly(store):
    record = store.add(new_memory("v"))
    new_provenance = MemoryProvenance(operator_supplied=True, source="operator")
    assert store.update(record.id,
                        provenance=new_provenance).provenance == new_provenance


def test_update_missing_raises(store):
    with pytest.raises(MemoryNotFoundError):
        store.update("mem_nope", content="x")


def test_update_revalidates(store):
    record = store.add(new_memory("v"))
    with pytest.raises(MemoryValidationError):
        store.update(record.id, content="")


def test_repeated_updates_increment_revision(store):
    record = store.add(new_memory("v0"))
    for expected in (2, 3, 4):
        assert store.update(record.id, content=f"v{expected}").revision == expected


# ===========================================================================
# Delete and expiry
# ===========================================================================

def test_delete(store):
    record = store.add(new_memory("v"))
    store.delete(record.id)
    assert store.exists(record.id) is False
    with pytest.raises(MemoryNotFoundError):
        store.get(record.id)


def test_delete_missing_raises_unless_permitted(store):
    with pytest.raises(MemoryNotFoundError):
        store.delete("mem_nope")
    store.delete("mem_nope", missing_ok=True)     # explicit opt-in


def test_expired_records_are_hidden_but_not_deleted(store):
    live = store.add(new_memory("live"))
    stale = store.add(new_memory("stale", expires_at=NOW))
    ids = {r.id for r in store.all_records()}
    assert live.id in ids
    # `stale` expired relative to real now (NOW is in the past by test time).
    assert stale.id not in ids
    assert stale.id in {r.id for r in store.all_records(include_expired=True)}


def test_purge_expired_is_deterministic_and_reported(store):
    stale = store.add(new_memory("stale", expires_at=NOW))
    live = store.add(new_memory("live"))
    assert store.purge_expired() == [stale.id]
    assert store.exists(stale.id) is False
    assert store.exists(live.id) is True
    assert store.purge_expired() == []            # idempotent


def test_purge_respects_an_injected_clock(store):
    future = datetime.now(timezone.utc) + timedelta(days=1)
    record = store.add(new_memory("expires tomorrow", expires_at=future))
    assert store.purge_expired() == []
    assert store.purge_expired(now=future + timedelta(seconds=1)) == [record.id]


# ===========================================================================
# Deterministic retrieval
# ===========================================================================

def seed_ranking(store):
    """Weakest scope/trust on the tag match, so match strength must dominate."""
    tag_hit = store.add(new_memory("something unrelated entirely",
                                   tags=["python"], scope=MemoryScope.GLOBAL,
                                   trust=MemoryTrust.UNCERTAIN))
    exact = store.add(new_memory("python", scope=MemoryScope.SESSION,
                                 scope_key="s1",
                                 trust=MemoryTrust.USER_PROVIDED))
    substring = store.add(new_memory("we use python daily",
                                     scope=MemoryScope.SESSION, scope_key="s1",
                                     trust=MemoryTrust.USER_PROVIDED))
    return tag_hit, exact, substring


def test_match_strength_dominates_scope_and_trust(store):
    tag_hit, exact, substring = seed_ranking(store)
    hits = store.search("python").hits
    assert [h.match for h in hits] == [MatchKind.TAG, MatchKind.EXACT,
                                       MatchKind.SUBSTRING]
    assert [h.id for h in hits] == [tag_hit.id, exact.id, substring.id]


def test_scope_specificity_breaks_ties_within_a_match_kind(store):
    broad = store.add(new_memory("python is nice", scope=MemoryScope.GLOBAL))
    narrow = store.add(new_memory("python is nice here",
                                  scope=MemoryScope.PROJECT, scope_key="acme"))
    assert [h.id for h in store.search("python")] == [narrow.id, broad.id]


def test_trust_breaks_ties_within_a_scope(store):
    weak = store.add(new_memory("python weak claim",
                                trust=MemoryTrust.UNCERTAIN))
    strong = store.add(new_memory("python strong claim",
                                  trust=MemoryTrust.USER_PROVIDED))
    assert [h.id for h in store.search("python")] == [strong.id, weak.id]


def test_recency_breaks_remaining_ties(store):
    older = store.add(new_memory("python one", now=NOW))
    newer = store.add(new_memory("python two", now=NOW + timedelta(days=1)))
    assert [h.id for h in store.search("python")] == [newer.id, older.id]


def test_id_is_the_final_stable_tiebreak():
    """Fully identical rank -> ascending id, so order never wobbles."""
    same = dict(scope=MemoryScope.GLOBAL, trust=MemoryTrust.INFERRED, now=NOW)
    records = [MemoryRecord(content="python fact", id=f"mem_{i}",
                            created_at=NOW, updated_at=NOW)
               for i in ("c", "a", "b")]
    result = search_records(records, MemoryQuery(text="python"))
    assert [h.id for h in result] == ["mem_a", "mem_b", "mem_c"]


def test_search_is_deterministic_across_calls(store):
    seed_ranking(store)
    assert [h.id for h in store.search("python")] == \
        [h.id for h in store.search("python")]


def test_search_is_case_insensitive(store):
    store.add(new_memory("Preferred Language Is Python"))
    assert len(store.search("preferred language")) == 1
    assert len(store.search("PYTHON")) == 1


def test_no_match_returns_empty_result(store):
    store.add(new_memory("something"))
    result = store.search("absent")
    assert list(result) == [] and not result and result.total_matched == 0


def test_reasons_explain_the_match(store):
    seed_ranking(store)
    reasons = {h.match: h.reason for h in store.search("python")}
    assert reasons[MatchKind.TAG] == "tag matches 'python'"
    assert reasons[MatchKind.EXACT] == "content exactly matches 'python'"
    assert reasons[MatchKind.SUBSTRING] == "content contains 'python'"


# ===========================================================================
# Filters
# ===========================================================================

def test_empty_query_lists_everything_live(store):
    store.add(new_memory("a"))
    store.add(new_memory("b"))
    result = store.search()
    assert len(result) == 2
    assert all(h.match is MatchKind.FILTER for h in result)


def test_filter_by_type(store):
    preference = store.add(new_memory("likes python",
                                      type=MemoryType.PREFERENCE))
    store.add(new_memory("port is 8080", type=MemoryType.FACT))
    result = store.search(MemoryQuery(types=(MemoryType.PREFERENCE,)))
    assert [h.id for h in result] == [preference.id]


def test_filter_by_scope(store):
    project = store.add(new_memory("p", scope=MemoryScope.PROJECT,
                                   scope_key="acme"))
    store.add(new_memory("g"))
    result = store.search(MemoryQuery(scopes=(MemoryScope.PROJECT,)))
    assert [h.id for h in result] == [project.id]


def test_scope_key_includes_global_by_default(store):
    project = store.add(new_memory("acme fact", scope=MemoryScope.PROJECT,
                                   scope_key="acme"))
    everywhere = store.add(new_memory("universal fact"))
    store.add(new_memory("other fact", scope=MemoryScope.PROJECT,
                         scope_key="other"))
    ids = {h.id for h in store.search(MemoryQuery(scope_key="acme"))}
    assert ids == {project.id, everywhere.id}


def test_scope_key_can_exclude_global(store):
    project = store.add(new_memory("acme fact", scope=MemoryScope.PROJECT,
                                   scope_key="acme"))
    store.add(new_memory("universal fact"))
    result = store.search(MemoryQuery(scope_key="acme", include_global=False))
    assert [h.id for h in result] == [project.id]


def test_filter_by_tags_requires_all(store):
    both = store.add(new_memory("has both", tags=["a", "b"]))
    store.add(new_memory("has one", tags=["a"]))
    assert [h.id for h in store.search(MemoryQuery(tags=("a", "b")))] == [both.id]


def test_tag_filter_is_normalised(store):
    record = store.add(new_memory("v", tags=["Python"]))
    assert [h.id for h in store.search(MemoryQuery(tags=("PYTHON",)))] == \
        [record.id]


def test_filters_combine_with_text(store):
    wanted = store.add(new_memory("python preference", type=MemoryType.PREFERENCE))
    store.add(new_memory("python fact", type=MemoryType.FACT))
    result = store.search(MemoryQuery(text="python",
                                      types=(MemoryType.PREFERENCE,)))
    assert [h.id for h in result] == [wanted.id]


def test_expired_excluded_from_search_but_reachable_on_request(store):
    stale = store.add(new_memory("stale python", expires_at=NOW))
    assert list(store.search("python")) == []
    result = store.search(MemoryQuery(text="python", include_expired=True))
    assert [h.id for h in result] == [stale.id]


# ===========================================================================
# Limits and truncation
# ===========================================================================

def test_query_limit_truncates_and_reports(store):
    for i in range(5):
        store.add(new_memory(f"python {i}"))
    result = store.search(MemoryQuery(text="python", limit=2))
    assert len(result) == 2
    assert result.total_matched == 5
    assert result.truncated is True


def test_configured_max_search_results_applies_by_default(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "m",
                               MemoryConfig(max_search_results=3))
    for i in range(5):
        store.add(new_memory(f"python {i}"))
    result = store.search("python")
    assert len(result) == 3 and result.total_matched == 5


def test_untruncated_result_says_so(store):
    store.add(new_memory("python"))
    result = store.search("python")
    assert result.truncated is False and result.total_matched == 1


def test_search_rejects_query_object_plus_kwargs(store):
    with pytest.raises(MemoryValidationError):
        store.search(MemoryQuery(text="x"), types=(MemoryType.FACT,))


def test_result_helpers(store):
    record = store.add(new_memory("python"))
    result = store.search("python")
    assert result.records == (record,)
    assert result.to_dict()["hits"][0]["record"]["id"] == record.id
    assert result.to_dict()["total_matched"] == 1


# ===========================================================================
# Persistence
# ===========================================================================

def test_json_backend_writes_one_readable_file_per_record(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "memory")
    record = store.add(new_memory("Preferred project language is Python"))
    path = tmp_path / "memory" / f"{record.id}.json"
    assert path.exists()
    assert json.loads(path.read_text())["content"] == \
        "Preferred project language is Python"


def test_json_backend_reloads_from_a_new_instance(tmp_path):
    first = JsonFileMemoryStore(tmp_path / "memory")
    record = first.add(new_memory("durable", tags=["keep"]))

    second = JsonFileMemoryStore(tmp_path / "memory")
    assert second.get(record.id) == record


def test_json_backend_leaves_no_temp_files(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "memory")
    store.add(new_memory("v"))
    assert list((tmp_path / "memory").glob("*.tmp")) == []


def test_json_backend_write_is_atomic_on_failure(tmp_path, monkeypatch):
    """A failed replace must not leave a partial file or a stray temp file."""
    store = JsonFileMemoryStore(tmp_path / "memory")
    record = store.add(new_memory("original"))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("agentcore.memory.store.os.replace", boom)
    with pytest.raises(OSError):
        store.update(record.id, content="replacement")

    monkeypatch.undo()
    assert store.get(record.id).content == "original"
    assert list((tmp_path / "memory").glob("*.tmp")) == []


def test_corrupt_file_is_skipped_not_fatal(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "memory")
    good = store.add(new_memory("good record"))
    (tmp_path / "memory" / "mem_broken.json").write_text("{not json",
                                                         encoding="utf-8")
    assert [r.id for r in store.all_records()] == [good.id]
    assert len(store.search("good")) == 1


def test_corrupt_record_reports_missing_rather_than_crashing(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "memory")
    (tmp_path / "memory" / "mem_broken.json").write_text("{not json",
                                                         encoding="utf-8")
    with pytest.raises(MemoryNotFoundError):
        store.get("mem_broken")


def test_json_backend_rejects_path_like_ids(tmp_path):
    store = JsonFileMemoryStore(tmp_path / "memory")
    with pytest.raises(MemoryValidationError):
        store.get("../escape")


def test_in_memory_backend_snapshots_rather_than_aliasing():
    """Matches InMemoryStateStore's documented behaviour."""
    store = InMemoryMemoryStore()
    record = store.add(new_memory("v"))
    assert store.get(record.id) is not record
    assert store.get(record.id) == record


def test_in_memory_backend_seeds_from_records():
    seed = new_memory("seeded")
    store = InMemoryMemoryStore(records=[seed])
    assert store.get(seed.id) == seed


def test_all_records_newest_first(store):
    old = store.add(new_memory("old", now=NOW))
    new = store.add(new_memory("new", now=NOW + timedelta(days=1)))
    assert [r.id for r in store.all_records()] == [new.id, old.id]


# ===========================================================================
# Cross-process persistence
# ===========================================================================

def test_survives_a_genuinely_separate_process(tmp_path):
    directory = tmp_path / "memory"
    store = JsonFileMemoryStore(directory)
    record = store.add(new_memory(
        "Preferred project language is Python",
        type=MemoryType.PREFERENCE, scope=MemoryScope.PROJECT,
        scope_key="acme", tags=["language"], trust=MemoryTrust.USER_PROVIDED,
        provenance=MemoryProvenance(operator_supplied=True, run_id="run_9",
                                    source="operator")))

    script = f'''
import json
from agentcore.memory import JsonFileMemoryStore, MemoryQuery
store = JsonFileMemoryStore({str(directory)!r})
hit = store.search("Preferred project language").hits[0]
print(json.dumps({{
    "id": hit.record.id,
    "content": hit.record.content,
    "type": hit.record.type.value,
    "scope": hit.record.scope.value,
    "scope_key": hit.record.scope_key,
    "trust": hit.record.trust.value,
    "operator_supplied": hit.record.provenance.operator_supplied,
    "run_id": hit.record.provenance.run_id,
}}))
'''
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, check=True)
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload == {
        "id": record.id,
        "content": "Preferred project language is Python",
        "type": "preference", "scope": "project", "scope_key": "acme",
        "trust": "user_provided", "operator_supplied": True,
        "run_id": "run_9",
    }


# ===========================================================================
# Security: logging and what is never stored
# ===========================================================================

def test_content_never_reaches_the_logs(tmp_path, caplog_json):
    store = JsonFileMemoryStore(tmp_path / "memory")
    record = store.add(new_memory(f"the api_key is {SECRET}", tags=["creds"]))
    store.update(record.id, content=f"rotated to {SECRET}")
    store.delete(record.id)

    logs = caplog_json.getvalue()
    assert SECRET not in logs
    assert "api_key is" not in logs
    assert "memory.created" in logs and "memory.deleted" in logs


def test_query_text_never_reaches_the_logs(tmp_path, caplog_json):
    """A search term can itself be private."""
    store = JsonFileMemoryStore(tmp_path / "memory")
    store.search(SECRET)
    store.search(MemoryQuery(text=SECRET))
    logs = caplog_json.getvalue()
    assert SECRET not in logs
    assert "memory.searched" in logs
    assert '"has_text": true' in logs


def test_logs_carry_shape_not_content(tmp_path, caplog_json):
    store = JsonFileMemoryStore(tmp_path / "memory")
    store.add(new_memory("a private fact", tags=["t"],
                         type=MemoryType.PREFERENCE))
    entry = next(json.loads(line) for line in
                 caplog_json.getvalue().strip().splitlines()
                 if json.loads(line).get("event") == "memory.created")
    assert entry["type"] == "preference"
    assert entry["content_chars"] == len("a private fact")
    assert "content" not in entry
    assert "a private fact" not in json.dumps(entry)


def test_missing_lookup_is_logged_without_inventing_a_record(tmp_path,
                                                             caplog_json):
    store = JsonFileMemoryStore(tmp_path / "memory")
    with pytest.raises(MemoryNotFoundError):
        store.get("mem_absent")
    assert "memory.missed" in caplog_json.getvalue()


def test_nothing_is_stored_without_an_explicit_call(tmp_path):
    """Construction alone must never create records."""
    store = JsonFileMemoryStore(tmp_path / "memory")
    store.search("anything")
    store.all_records()
    store.find_duplicates()
    assert store.count() == 0
    assert list((tmp_path / "memory").glob("*.json")) == []


def test_record_has_no_field_for_tool_arguments():
    """Structurally impossible to auto-persist tool args or env values."""
    fields = set(new_memory("v").to_dict())
    assert "tool_args" not in fields
    assert "environment" not in fields
    assert fields == {"id", "content", "type", "scope", "scope_key", "tags",
                      "trust", "provenance", "created_at", "updated_at",
                      "revision", "expires_at", "schema_version"}


def test_provenance_has_no_transcript_field():
    assert set(MemoryProvenance().to_dict()) == {
        "operator_supplied", "run_id", "task_id", "subtask_id", "source",
        "note"}


# ===========================================================================
# build_memory_store
# ===========================================================================

def test_build_defaults_to_workspace_memory_directory(tmp_path):
    from agentcore.config.schema import AgentConfig

    config = AgentConfig(workspace_dir=str(tmp_path))
    store = build_memory_store(config)
    assert store.directory == tmp_path / "memory"


def test_build_honours_an_explicit_directory(tmp_path):
    from agentcore.config.schema import AgentConfig

    config = AgentConfig(workspace_dir=str(tmp_path),
                         memory=MemoryConfig(directory=str(tmp_path / "custom")))
    assert build_memory_store(config).directory == tmp_path / "custom"


def test_build_accepts_a_bare_memory_config(tmp_path):
    store = build_memory_store(MemoryConfig(directory=str(tmp_path / "bare")))
    assert store.directory == tmp_path / "bare"


def test_build_passes_limits_through(tmp_path):
    store = build_memory_store(MemoryConfig(directory=str(tmp_path / "d"),
                                            max_records=5))
    assert store.config.max_records == 5
