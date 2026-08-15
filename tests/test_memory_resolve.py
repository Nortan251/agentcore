"""Memory resolution: applying decisions, superseding, and safe forgetting.

The single most important property under test: **no durable memory is
destroyed or overwritten without an explicit instruction**, and an
unidentifiable target changes nothing at all.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from agentcore.config.schema import LoggingConfig
from agentcore.memory import (
    InMemoryMemoryStore,
    MemoryProvenance,
    MemoryQuery,
    MemoryScope,
    MemoryTrust,
    MemoryType,
    new_memory,
)
from agentcore.memory.capture import (
    CaptureAction,
    CaptureDecision,
    CaptureSource,
    MemoryCapture,
    MemoryCandidate,
)
from agentcore.memory.resolve import (
    ConflictResolution,
    CorrectionKind,
    CorrectionRequest,
    MemoryResolver,
    ResolutionOutcome,
    ResolutionStatus,
    parse_correction,
)
from agentcore.models.messages import Message
from agentcore.observability.logging import configure_logging

SECRET = "sk-resolve-must-never-leak-1234"


@pytest.fixture
def store():
    return InMemoryMemoryStore()


@pytest.fixture
def resolver(store):
    return MemoryResolver(store)


@pytest.fixture
def caplog_json():
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


def user(text):
    return Message.user(text)


def candidate(content, *, source=CaptureSource.EXPLICIT_REQUEST, **kwargs):
    return MemoryCandidate(content=content, source=source, **kwargs)


def decision(action, cand, *, related=()):
    return CaptureDecision(cand, action, "test", related_ids=tuple(related))


def seed_preference(store, content="I prefer Python for my projects"):
    return store.add(new_memory(
        content, type=MemoryType.PREFERENCE, trust=MemoryTrust.USER_PROVIDED,
        tags=["language"],
        provenance=MemoryProvenance(operator_supplied=True, run_id="run_1",
                                    source="explicit_request", note="orig")))


# ===========================================================================
# CREATE
# ===========================================================================

def test_create_stores_the_candidate(store, resolver):
    outcome = resolver.apply(decision(CaptureAction.CREATE,
                                      candidate("I prefer Python")))
    assert outcome.status is ResolutionStatus.CREATED
    assert outcome.changed is True
    assert store.count() == 1
    assert outcome.record.content == "I prefer Python"
    assert outcome.affected_ids == (outcome.record.id,)


def test_create_carries_trust_and_provenance(store, resolver):
    outcome = resolver.apply(decision(CaptureAction.CREATE,
                                      candidate("I prefer Python")))
    assert outcome.record.trust is MemoryTrust.USER_PROVIDED
    assert outcome.record.provenance.operator_supplied is True
    assert outcome.record.provenance.source == "explicit_request"


def test_create_failure_is_reported_not_raised(resolver):
    class Failing(InMemoryMemoryStore):
        def add(self, record, *, replace=False):
            raise RuntimeError("disk full")

    outcome = MemoryResolver(Failing()).apply(
        decision(CaptureAction.CREATE, candidate("x")))
    assert outcome.status is ResolutionStatus.FAILED
    assert outcome.changed is False


# ===========================================================================
# REJECT
# ===========================================================================

def test_reject_writes_nothing(store, resolver):
    outcome = resolver.apply(decision(CaptureAction.REJECT,
                                      candidate("I prefer Python")))
    assert outcome.status is ResolutionStatus.NO_ACTION
    assert outcome.changed is False
    assert store.count() == 0


# ===========================================================================
# UPDATE
# ===========================================================================

def test_update_does_not_create_a_duplicate(store, resolver):
    existing = store.add(new_memory("I prefer Python",
                                    trust=MemoryTrust.INFERRED))
    resolver.apply(decision(CaptureAction.UPDATE, candidate("I prefer Python"),
                            related=[existing.id]))
    assert store.count() == 1


def test_update_preserves_id_and_created_at(store, resolver):
    existing = store.add(new_memory("I prefer Python",
                                    trust=MemoryTrust.INFERRED))
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE, candidate("I prefer Python"),
        related=[existing.id]))
    assert outcome.record.id == existing.id
    assert outcome.record.created_at == existing.created_at


def test_update_increments_revision(store, resolver):
    existing = store.add(new_memory("I prefer Python",
                                    trust=MemoryTrust.INFERRED))
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE, candidate("I prefer Python"),
        related=[existing.id]))
    assert outcome.status is ResolutionStatus.UPDATED
    assert outcome.record.revision == existing.revision + 1


def test_update_raises_trust_from_a_user_source(store, resolver):
    existing = store.add(new_memory("I prefer Python",
                                    trust=MemoryTrust.INFERRED))
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE, candidate("I prefer Python"),
        related=[existing.id]))
    assert outcome.record.trust is MemoryTrust.USER_PROVIDED


def test_update_never_lowers_trust(store, resolver):
    existing = seed_preference(store, "I prefer Python")
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE,
        candidate("I prefer Python", source=CaptureSource.USER_STATEMENT),
        related=[existing.id]))
    assert store.get(existing.id).trust is MemoryTrust.USER_PROVIDED
    assert outcome.status is ResolutionStatus.NO_ACTION


def test_inference_never_promotes_an_existing_memory(store, resolver):
    """A model guess must not elevate a record to user authority."""
    existing = store.add(new_memory("I prefer Python",
                                    trust=MemoryTrust.INFERRED))
    resolver.apply(decision(
        CaptureAction.UPDATE,
        candidate("I prefer Python", source=CaptureSource.ASSISTANT_INFERENCE),
        related=[existing.id]))
    assert store.get(existing.id).trust is MemoryTrust.INFERRED


def test_update_keeps_the_original_run_provenance(store, resolver):
    existing = store.add(new_memory(
        "I prefer Python", trust=MemoryTrust.INFERRED,
        provenance=MemoryProvenance(run_id="run_original", source="inference")))
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE,
        candidate("I prefer Python", run_id="run_new"),
        related=[existing.id]))
    assert outcome.record.provenance.run_id == "run_original"
    assert outcome.record.provenance.operator_supplied is True


def test_update_merges_tags(store, resolver):
    existing = store.add(new_memory("I prefer Python", tags=["language"],
                                    trust=MemoryTrust.INFERRED))
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE,
        candidate("I prefer Python", tags=("backend",)),
        related=[existing.id]))
    assert set(outcome.record.tags) == {"language", "backend"}


def test_update_with_nothing_to_change_is_a_no_op(store, resolver):
    existing = seed_preference(store)
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE,
        candidate(existing.content, tags=("language",)),
        related=[existing.id]))
    assert outcome.status is ResolutionStatus.NO_ACTION
    assert store.get(existing.id).revision == existing.revision


def test_update_falls_back_to_create_when_the_target_vanished(store, resolver):
    outcome = resolver.apply(decision(
        CaptureAction.UPDATE, candidate("I prefer Python"),
        related=["mem_gone"]))
    assert outcome.status is ResolutionStatus.CREATED
    assert store.count() == 1


# ===========================================================================
# POSSIBLE_CONFLICT
# ===========================================================================

def conflict_decision(store):
    existing = seed_preference(store)
    return existing, decision(
        CaptureAction.POSSIBLE_CONFLICT,
        candidate("I've switched to Rust for new projects",
                  type=MemoryType.DECISION),
        related=[existing.id])


def test_conflict_without_a_choice_writes_nothing(store, resolver):
    existing, dec = conflict_decision(store)
    outcome = resolver.apply(dec)
    assert outcome.status is ResolutionStatus.PENDING_CONFLICT
    assert outcome.changed is False
    assert outcome.needs_operator is True
    assert outcome.candidate_ids == (existing.id,)
    assert store.count() == 1
    assert store.get(existing.id).content == existing.content
    assert store.get(existing.id).revision == 1


def test_keep_existing_changes_nothing(store, resolver):
    existing, dec = conflict_decision(store)
    outcome = resolver.apply(dec, conflict=ConflictResolution.KEEP_EXISTING)
    assert outcome.status is ResolutionStatus.NO_ACTION
    assert outcome.changed is False
    assert store.count() == 1
    assert store.get(existing.id).revision == 1


def test_discard_candidate_changes_nothing(store, resolver):
    existing, dec = conflict_decision(store)
    outcome = resolver.apply(dec,
                             conflict=ConflictResolution.DISCARD_CANDIDATE)
    assert outcome.status is ResolutionStatus.NO_ACTION
    assert store.count() == 1
    assert store.get(existing.id).content == existing.content


def test_supersede_stores_the_new_truth(store, resolver):
    existing, dec = conflict_decision(store)
    outcome = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    assert outcome.status is ResolutionStatus.SUPERSEDED
    assert outcome.record.content == "I've switched to Rust for new projects"
    assert outcome.affected_ids == (existing.id,)


def test_supersede_retires_rather_than_deletes(store, resolver):
    existing, dec = conflict_decision(store)
    resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    # Still addressable by id, with history and origin intact.
    retired = store.get(existing.id)
    assert retired.content == existing.content
    assert retired.created_at == existing.created_at
    assert retired.provenance.run_id == "run_1"
    assert retired.provenance.operator_supplied is True
    assert retired.revision == existing.revision + 1


def test_superseded_memory_leaves_retrieval(store, resolver):
    existing, dec = conflict_decision(store)
    resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)

    live = [h.record.content for h in store.search("prefer")]
    assert existing.content not in live
    assert [r.id for r in store.all_records()] != []
    assert existing.id not in {r.id for r in store.all_records()}
    assert existing.id in {r.id for r in store.all_records(include_expired=True)}


def test_superseded_memory_is_not_equally_authoritative(store, resolver):
    _, dec = conflict_decision(store)
    resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    contents = [h.record.content for h in store.search("projects")]
    assert contents == ["I've switched to Rust for new projects"]


def test_supersede_keeps_user_provenance_on_the_new_record(store, resolver):
    _, dec = conflict_decision(store)
    outcome = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    assert outcome.record.trust is MemoryTrust.USER_PROVIDED
    assert outcome.record.provenance.operator_supplied is True


def test_supersede_with_several_related_records(store, resolver):
    first = seed_preference(store, "I prefer Python")
    second = seed_preference(store, "I always use Python at work")
    dec = decision(CaptureAction.POSSIBLE_CONFLICT,
                   candidate("I've switched to Rust"),
                   related=[first.id, second.id])
    outcome = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    assert set(outcome.affected_ids) == {first.id, second.id}
    assert store.count(include_expired=False) == 1


def test_supersede_tolerates_a_missing_related_record(store, resolver):
    existing = seed_preference(store)
    dec = decision(CaptureAction.POSSIBLE_CONFLICT,
                   candidate("I've switched to Rust"),
                   related=[existing.id, "mem_gone"])
    outcome = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    assert outcome.affected_ids == (existing.id,)


# ===========================================================================
# Correction language
# ===========================================================================

@pytest.mark.parametrize("text,kind,subject", [
    ("Forget that.", CorrectionKind.FORGET, ""),
    ("forget it", CorrectionKind.FORGET, ""),
    ("Forget my language preference.", CorrectionKind.FORGET,
     "language preference"),
    ("Forget what I told you about Python.", CorrectionKind.FORGET, "Python"),
    ("Forget about my timezone.", CorrectionKind.FORGET, "timezone"),
    ("Delete that memory.", CorrectionKind.FORGET, ""),
    ("Delete the memory about Python.", CorrectionKind.FORGET, "Python"),
    ("Don't remember that anymore.", CorrectionKind.FORGET, ""),
    ("Do not remember my timezone.", CorrectionKind.FORGET, "timezone"),
    ("Stop remembering my timezone.", CorrectionKind.FORGET, "timezone"),
    ("That's no longer true.", CorrectionKind.INVALIDATE, ""),
    ("That is no longer true about Python.", CorrectionKind.INVALIDATE,
     "Python"),
    ("That's wrong.", CorrectionKind.INVALIDATE, ""),
    ("That's outdated about my timezone.", CorrectionKind.INVALIDATE,
     "timezone"),
])
def test_correction_phrasings(text, kind, subject):
    request = parse_correction(text)
    assert request is not None, text
    assert request.kind is kind
    assert request.subject == subject


@pytest.mark.parametrize("text", [
    "I prefer Python.", "Remember that I prefer Python.",
    "What did I tell you?", "", "   ",
])
def test_non_corrections_are_not_matched(text):
    assert parse_correction(text) is None


def test_anaphora_is_not_treated_as_a_subject():
    assert parse_correction("Forget everything.").subject == ""
    assert parse_correction("Forget what I said.").subject == ""


def test_correction_records_origin():
    request = parse_correction("Forget that.", source_turn_id="turn_3",
                               run_id="run_9")
    assert request.source_turn_id == "turn_3" and request.run_id == "run_9"


def test_correction_describe_omits_the_subject_text():
    described = parse_correction("Forget my secret project name.").describe()
    assert "secret project name" not in json.dumps(described)
    assert described["targeted"] is True


# ===========================================================================
# Forgetting: targeting safety
# ===========================================================================

def seed_three(store):
    return (
        store.add(new_memory("I prefer Python for my projects",
                             type=MemoryType.PREFERENCE, tags=["language"])),
        store.add(new_memory("EcoClean Connect is my main project",
                             tags=["project"])),
        store.add(new_memory("My timezone is Europe/London",
                             tags=["timezone"])),
    )


def test_bare_forget_deletes_nothing(store, resolver):
    seed_three(store)
    outcome = resolver.resolve_correction(parse_correction("Forget that."))
    assert outcome.status is ResolutionStatus.UNRESOLVED
    assert outcome.changed is False
    assert outcome.needs_operator is True
    assert store.count() == 3


def test_bare_forget_offers_candidates(store, resolver):
    seed_three(store)
    outcome = resolver.resolve_correction(parse_correction("Forget that."))
    assert len(outcome.candidate_ids) == 3


def test_targeted_forget_deletes_exactly_one(store, resolver):
    _, _, timezone = seed_three(store)
    outcome = resolver.resolve_correction(
        parse_correction("Forget my timezone."))
    assert outcome.status is ResolutionStatus.DELETED
    assert outcome.affected_ids == (timezone.id,)
    assert store.count() == 2


def test_targeted_forget_leaves_unrelated_memories(store, resolver):
    python, project, _ = seed_three(store)
    resolver.resolve_correction(parse_correction("Forget my timezone."))
    assert store.get(python.id).content == python.content
    assert store.get(project.id).content == project.content


def test_ambiguous_subject_deletes_nothing(store, resolver):
    seed_three(store)
    outcome = resolver.resolve_correction(
        parse_correction("Forget my project."))
    assert outcome.status is ResolutionStatus.UNRESOLVED
    assert len(outcome.candidate_ids) > 1
    assert store.count() == 3


def test_unmatched_subject_deletes_nothing(store, resolver):
    seed_three(store)
    outcome = resolver.resolve_correction(
        parse_correction("Forget my shoe size."))
    assert outcome.status is ResolutionStatus.UNRESOLVED
    assert outcome.candidate_ids == ()
    assert store.count() == 3


def test_explicit_id_overrides_ambiguity(store, resolver):
    python, _, _ = seed_three(store)
    outcome = resolver.resolve_correction(parse_correction("Forget that."),
                                          memory_id=python.id)
    assert outcome.status is ResolutionStatus.DELETED
    assert store.count() == 2


def test_unknown_id_fails_without_changing_anything(store, resolver):
    seed_three(store)
    outcome = resolver.resolve_correction(parse_correction("Forget that."),
                                          memory_id="mem_nope")
    assert outcome.status is ResolutionStatus.FAILED
    assert store.count() == 3


def test_forget_by_id_directly(store, resolver):
    python, _, _ = seed_three(store)
    outcome = resolver.forget(python.id, reason="operator cleanup")
    assert outcome.status is ResolutionStatus.DELETED
    assert store.count() == 2


def test_forget_unknown_id_is_reported(store, resolver):
    outcome = resolver.forget("mem_nope")
    assert outcome.status is ResolutionStatus.FAILED
    assert outcome.changed is False


# ===========================================================================
# Invalidation
# ===========================================================================

def test_invalidate_retires_without_deleting(store, resolver):
    python, _, _ = seed_three(store)
    outcome = resolver.resolve_correction(
        parse_correction("That's no longer true."), memory_id=python.id)
    assert outcome.status is ResolutionStatus.SUPERSEDED
    # Gone from retrieval, still present for audit.
    assert python.id not in {r.id for r in store.all_records()}
    assert store.get(python.id).content == python.content
    assert store.count(include_expired=True) == 3


def test_invalidated_memory_is_not_retrieved(store, resolver):
    python, _, _ = seed_three(store)
    resolver.resolve_correction(parse_correction("That's no longer true."),
                                memory_id=python.id)
    assert [h.record.id for h in store.search("Python")] == []


# ===========================================================================
# No store / robustness
# ===========================================================================

def test_resolver_without_a_store_fails_cleanly():
    resolver = MemoryResolver(None)
    assert resolver.apply(decision(CaptureAction.CREATE,
                                   candidate("x"))).status \
        is ResolutionStatus.FAILED
    assert resolver.forget("mem_1").status is ResolutionStatus.FAILED
    assert resolver.resolve_correction(
        CorrectionRequest(kind=CorrectionKind.FORGET)).status \
        is ResolutionStatus.FAILED


def test_store_without_search_cannot_target_by_subject():
    class NoSearch:
        def get(self, memory_id):
            raise KeyError(memory_id)

        def all_records(self, **kwargs):
            return []

    outcome = MemoryResolver(NoSearch()).resolve_correction(
        parse_correction("Forget my timezone."))
    assert outcome.status is ResolutionStatus.UNRESOLVED


def test_outcome_changed_only_for_writes():
    for status in ResolutionStatus:
        outcome = ResolutionOutcome(status=status, reason="")
        expected = status in (ResolutionStatus.CREATED,
                              ResolutionStatus.UPDATED,
                              ResolutionStatus.SUPERSEDED,
                              ResolutionStatus.DELETED)
        assert outcome.changed is expected


# ===========================================================================
# Audit and secrecy
# ===========================================================================

def test_content_is_never_logged(store, resolver, caplog_json):
    record = store.add(new_memory(f"my note about {SECRET}"))
    resolver.forget(record.id, reason="cleanup")
    logs = caplog_json.getvalue()
    assert SECRET not in logs
    assert "memory.forgotten" in logs


def test_deletion_is_audited_with_shape_only(store, resolver, caplog_json):
    record = store.add(new_memory("I prefer Python",
                                  type=MemoryType.PREFERENCE))
    resolver.forget(record.id, reason="operator cleanup")
    entry = next(json.loads(line) for line in
                 caplog_json.getvalue().strip().splitlines()
                 if json.loads(line).get("event") == "memory.forgotten")
    assert entry["memory_id"] == record.id
    assert entry["type"] == "preference"
    assert "content" not in entry
    assert entry["reason"] == "operator cleanup"


def test_supersede_is_audited(store, resolver, caplog_json):
    _, dec = conflict_decision(store)
    resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    logs = caplog_json.getvalue()
    assert "memory.superseded" in logs
    assert "memory.conflict_resolved" in logs


def test_pending_conflict_is_audited(store, resolver, caplog_json):
    _, dec = conflict_decision(store)
    resolver.apply(dec)
    assert "memory.conflict_pending" in caplog_json.getvalue()


def test_unresolved_forget_is_audited(store, resolver, caplog_json):
    seed_three(store)
    resolver.resolve_correction(parse_correction("Forget that."))
    assert "memory.forget_unresolved" in caplog_json.getvalue()


def test_outcome_describe_omits_content(store, resolver):
    outcome = resolver.apply(decision(
        CaptureAction.CREATE, candidate("a private durable fact")))
    assert "a private durable fact" not in json.dumps(outcome.describe())
    assert outcome.describe()["status"] == "created"


# ===========================================================================
# Capture -> resolve, together
# ===========================================================================

def test_capture_conflict_flows_into_resolution(store):
    capture = MemoryCapture(store)
    resolver = MemoryResolver(store)

    first = capture.capture(user("Remember that I prefer Python for my "
                                 "projects."))
    original = first.stored[0]

    second = capture.preview(
        user("Remember that I've switched to Rust for all new projects."))
    dec = second.decisions[0]
    assert dec.action is CaptureAction.POSSIBLE_CONFLICT

    pending = resolver.apply(dec)
    assert pending.status is ResolutionStatus.PENDING_CONFLICT
    assert store.get(original.id).content == original.content

    applied = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    assert applied.status is ResolutionStatus.SUPERSEDED
    live = [h.record.content for h in store.search("projects")]
    assert live == ["I've switched to Rust for all new projects"]


def test_supersede_inherits_tags_so_the_new_fact_stays_findable(store,
                                                                resolver):
    """Correcting a memory must not make the corrected fact unsearchable."""
    existing = store.add(new_memory(
        "I prefer Python for my projects", type=MemoryType.PREFERENCE,
        tags=["language", "tooling"]))
    dec = decision(CaptureAction.POSSIBLE_CONFLICT,
                   candidate("I've switched to Rust for new projects"),
                   related=[existing.id])
    outcome = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)

    assert set(outcome.record.tags) == {"language", "tooling"}
    assert outcome.record.revision == 1        # still a brand-new record
    found = [h.record.content for h in store.search("language")]
    assert found == ["I've switched to Rust for new projects"]


def test_supersede_keeps_its_own_tags_too(store, resolver):
    existing = store.add(new_memory("I prefer Python", tags=["language"]))
    dec = decision(CaptureAction.POSSIBLE_CONFLICT,
                   candidate("I've switched to Rust", tags=("rust",)),
                   related=[existing.id])
    outcome = resolver.apply(dec, conflict=ConflictResolution.SUPERSEDE)
    assert set(outcome.record.tags) == {"language", "rust"}
