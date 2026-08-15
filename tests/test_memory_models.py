"""Memory record: validation, lifecycle, serialisation and the taxonomy.

The record is the contract every future memory layer builds on, so its
invariants are tested harder than its conveniences.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agentcore.memory.errors import MemoryValidationError
from agentcore.memory.models import (
    SCHEMA_VERSION,
    MemoryProvenance,
    MemoryRecord,
    MemoryScope,
    MemoryTrust,
    MemoryType,
    new_memory,
    normalise_tags,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# Taxonomy
# ===========================================================================

def test_type_vocabulary_is_small_and_stable():
    assert {t.value for t in MemoryType} == {
        "fact", "preference", "workflow", "decision", "episodic"}


def test_scope_vocabulary():
    assert {s.value for s in MemoryScope} == {"global", "project", "session"}


def test_scope_specificity_orders_narrow_over_broad():
    assert MemoryScope.SESSION.specificity > MemoryScope.PROJECT.specificity
    assert MemoryScope.PROJECT.specificity > MemoryScope.GLOBAL.specificity


def test_trust_vocabulary_and_ranking():
    assert {t.value for t in MemoryTrust} == {
        "user_provided", "verified", "inferred", "uncertain"}
    assert MemoryTrust.USER_PROVIDED.rank > MemoryTrust.VERIFIED.rank
    assert MemoryTrust.VERIFIED.rank > MemoryTrust.INFERRED.rank
    assert MemoryTrust.INFERRED.rank > MemoryTrust.UNCERTAIN.rank


def test_enums_stringify_to_their_value():
    """Matches the project's existing enum convention."""
    assert str(MemoryType.FACT) == "fact"
    assert str(MemoryScope.GLOBAL) == "global"
    assert str(MemoryTrust.VERIFIED) == "verified"


def test_enums_subclass_str_like_the_rest_of_the_project():
    assert isinstance(MemoryType.FACT, str)


# ===========================================================================
# Construction and validation
# ===========================================================================

def test_valid_record():
    record = new_memory("Preferred project language is Python",
                        type=MemoryType.PREFERENCE, tags=["language"])
    assert record.id.startswith("mem_")
    assert record.revision == 1
    assert record.schema_version == SCHEMA_VERSION
    assert record.validate() is record


def test_string_values_accepted_for_enums():
    record = new_memory("x", type="preference", scope="project",
                        scope_key="acme", trust="user_provided")
    assert record.type is MemoryType.PREFERENCE
    assert record.scope is MemoryScope.PROJECT
    assert record.trust is MemoryTrust.USER_PROVIDED


def test_unknown_type_rejected():
    with pytest.raises(MemoryValidationError) as exc:
        new_memory("x", type="telepathy")
    assert "unknown memory type" in str(exc.value)
    assert "fact" in exc.value.details["allowed"]


def test_unknown_scope_and_trust_rejected():
    with pytest.raises(MemoryValidationError):
        new_memory("x", scope="galaxy")
    with pytest.raises(MemoryValidationError):
        new_memory("x", trust="absolute")


@pytest.mark.parametrize("content", ["", "   ", "\n\t"])
def test_empty_content_rejected(content):
    with pytest.raises(MemoryValidationError):
        new_memory(content)


def test_non_string_content_rejected():
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content=None).validate()


def test_blank_id_rejected():
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content="x", id="  ").validate()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b"])
def test_path_like_id_rejected(bad):
    """Ids become filenames; traversal is refused at the model layer."""
    with pytest.raises(MemoryValidationError) as exc:
        MemoryRecord(content="x", id=bad).validate()
    assert "path separators" in str(exc.value)


def test_content_length_limit_is_opt_in():
    record = new_memory("y" * 50)
    record.validate()                              # no limit -> fine
    with pytest.raises(MemoryValidationError) as exc:
        record.validate(max_content_chars=10)
    assert exc.value.details["maximum"] == 10


def test_global_memory_must_not_carry_a_scope_key():
    with pytest.raises(MemoryValidationError) as exc:
        new_memory("x", scope=MemoryScope.GLOBAL, scope_key="acme")
    assert "must not carry a scope_key" in str(exc.value)


@pytest.mark.parametrize("scope", [MemoryScope.PROJECT, MemoryScope.SESSION])
def test_narrow_scope_requires_a_scope_key(scope):
    with pytest.raises(MemoryValidationError) as exc:
        new_memory("x", scope=scope)
    assert "requires a scope_key" in str(exc.value)


def test_revision_must_be_positive():
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content="x", revision=0).validate()


def test_updated_before_created_rejected():
    with pytest.raises(MemoryValidationError) as exc:
        MemoryRecord(content="x", created_at=NOW,
                     updated_at=NOW - timedelta(hours=1)).validate()
    assert "precedes created_at" in str(exc.value)


def test_non_datetime_timestamps_rejected():
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content="x", created_at="yesterday").validate()
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content="x", expires_at="soon").validate()


def test_bad_tags_rejected():
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content="x", tags=("ok", 5)).validate()


def test_bad_provenance_rejected():
    with pytest.raises(MemoryValidationError):
        MemoryRecord(content="x", provenance={"run_id": "r"}).validate()


# ===========================================================================
# Tags
# ===========================================================================

def test_tags_normalised_lowercase_sorted_and_deduplicated():
    record = new_memory("x", tags=["Python", "python", "  Web  ", ""])
    assert record.tags == ("python", "web")


def test_normalise_tags_collapses_inner_whitespace():
    assert normalise_tags(["Machine   Learning"]) == ("machine learning",)


# ===========================================================================
# Lifecycle
# ===========================================================================

def test_evolve_preserves_identity_and_bumps_revision():
    original = new_memory("old value", tags=["a"], now=NOW)
    updated = original.evolve(content="new value", now=NOW + timedelta(days=1))

    assert updated.id == original.id
    assert updated.created_at == original.created_at
    assert updated.revision == 2
    assert updated.updated_at > original.updated_at
    assert updated.content == "new value"
    assert original.content == "old value"        # frozen: original intact


def test_evolve_preserves_provenance_by_default():
    provenance = MemoryProvenance(operator_supplied=True, run_id="run_1",
                                  source="operator")
    record = new_memory("v", provenance=provenance)
    assert record.evolve(content="w").provenance == provenance


def test_evolve_normalises_new_tags():
    assert new_memory("v").evolve(tags=["B", "a"]).tags == ("a", "b")


@pytest.mark.parametrize("locked", ["id", "created_at", "revision"])
def test_evolve_refuses_to_rewrite_identity(locked):
    record = new_memory("v")
    with pytest.raises(MemoryValidationError) as exc:
        record.evolve(**{locked: "tampered"})
    assert "cannot be changed" in str(exc.value)


def test_record_is_frozen():
    record = new_memory("v")
    with pytest.raises(Exception):
        record.content = "mutated"


# ===========================================================================
# Expiry
# ===========================================================================

def test_no_expiry_means_never_expired():
    assert new_memory("v").is_expired() is False


def test_expiry_is_deterministic():
    record = new_memory("v", expires_at=NOW)
    assert record.is_expired(now=NOW - timedelta(seconds=1)) is False
    assert record.is_expired(now=NOW) is False           # strictly past
    assert record.is_expired(now=NOW + timedelta(seconds=1)) is True


def test_naive_timestamps_compare_without_raising():
    naive = datetime(2026, 3, 1, 12, 0)
    record = MemoryRecord(content="v", expires_at=naive)
    assert record.is_expired(now=NOW + timedelta(days=1)) is True


# ===========================================================================
# Serialisation
# ===========================================================================

def test_round_trip_preserves_every_field():
    record = new_memory(
        "Preferred project language is Python",
        type=MemoryType.PREFERENCE, scope=MemoryScope.PROJECT,
        scope_key="acme", tags=["language", "python"],
        trust=MemoryTrust.USER_PROVIDED,
        provenance=MemoryProvenance(operator_supplied=True, run_id="run_7",
                                    task_id="task_1", subtask_id="sub_2",
                                    source="operator", note="stated in chat"),
        expires_at=NOW + timedelta(days=30), now=NOW)

    restored = MemoryRecord.from_dict(record.to_dict())
    assert restored == record


def test_serialised_form_is_json_safe():
    payload = new_memory("v", expires_at=NOW).to_dict()
    assert json.loads(json.dumps(payload))["expires_at"] == NOW.isoformat()


def test_timestamps_serialise_as_iso_strings():
    payload = new_memory("v", now=NOW).to_dict()
    assert payload["created_at"] == NOW.isoformat()
    assert payload["updated_at"] == NOW.isoformat()


@pytest.mark.parametrize("missing", ["id", "content", "created_at"])
def test_from_dict_requires_core_fields(missing):
    payload = new_memory("v").to_dict()
    payload.pop(missing)
    with pytest.raises(MemoryValidationError) as exc:
        MemoryRecord.from_dict(payload)
    assert missing in exc.value.details["fields"]


def test_from_dict_never_invents_a_created_timestamp():
    """Unlike AgentState, a missing origin time is an error, not a default."""
    payload = new_memory("v").to_dict()
    payload["created_at"] = ""
    with pytest.raises(MemoryValidationError):
        MemoryRecord.from_dict(payload)


def test_from_dict_defaults_updated_to_created():
    payload = new_memory("v", now=NOW).to_dict()
    del payload["updated_at"]
    assert MemoryRecord.from_dict(payload).updated_at == NOW


def test_from_dict_rejects_non_mapping():
    with pytest.raises(MemoryValidationError):
        MemoryRecord.from_dict(["not", "a", "mapping"])


def test_from_dict_rejects_bad_timestamp():
    payload = new_memory("v").to_dict()
    payload["created_at"] = "not-a-date"
    with pytest.raises(MemoryValidationError):
        MemoryRecord.from_dict(payload)


def test_from_dict_rejects_unknown_enum_values():
    payload = new_memory("v").to_dict()
    payload["type"] = "nonsense"
    with pytest.raises(MemoryValidationError):
        MemoryRecord.from_dict(payload)


def test_from_dict_revalidates_invariants():
    payload = new_memory("v").to_dict()
    payload["scope"], payload["scope_key"] = "project", None
    with pytest.raises(MemoryValidationError):
        MemoryRecord.from_dict(payload)


def test_schema_version_persisted_and_restored():
    assert MemoryRecord.from_dict(
        new_memory("v").to_dict()).schema_version == SCHEMA_VERSION


# ===========================================================================
# Provenance
# ===========================================================================

def test_provenance_defaults_to_agent_inferred():
    provenance = new_memory("v").provenance
    assert provenance.operator_supplied is False
    assert provenance.run_id is None


def test_provenance_round_trip():
    provenance = MemoryProvenance(operator_supplied=True, run_id="run_1",
                                  task_id="t", subtask_id="s",
                                  source="operator", note="n")
    assert MemoryProvenance.from_dict(provenance.to_dict()) == provenance


def test_provenance_from_none_is_empty():
    assert MemoryProvenance.from_dict(None) == MemoryProvenance()


def test_provenance_rejects_non_mapping():
    with pytest.raises(MemoryValidationError):
        MemoryProvenance.from_dict("operator")


def test_trust_and_operator_supplied_are_independent():
    """An operator can record something they are themselves unsure about."""
    record = new_memory("maybe true", trust=MemoryTrust.UNCERTAIN,
                        provenance=MemoryProvenance(operator_supplied=True))
    assert record.provenance.operator_supplied is True
    assert record.trust is MemoryTrust.UNCERTAIN


# ===========================================================================
# describe(): the log-safe view
# ===========================================================================

def test_describe_omits_content():
    record = new_memory("a very private fact", tags=["t"])
    described = record.describe()
    assert "a very private fact" not in json.dumps(described)
    assert described["content_chars"] == len("a very private fact")
    assert described["memory_id"] == record.id
    assert described["type"] == "fact"
