"""Applying capture decisions: correction, superseding and forgetting.

Capture (:mod:`agentcore.memory.capture`) decides *what a statement means*.
This module decides *nothing* — it only carries out a decision that has
already been made, closing the lifecycle::

    CAPTURE -> DECIDE -> RESOLVE -> PERSIST

The split matters. :class:`~agentcore.memory.capture.CapturePolicy` judges
candidates; :class:`MemoryResolver` performs writes. A resolver that also
judged would be able to talk itself into a destructive action.

Safety rules, in order of importance
------------------------------------
1. **Nothing durable is destroyed without an explicit instruction.** A
   ``POSSIBLE_CONFLICT`` is never auto-applied; a caller must choose
   ``SUPERSEDE``, ``KEEP_EXISTING`` or ``DISCARD_CANDIDATE``.
2. **Superseding retires, it does not erase.** The old record is expired via
   the existing ``expires_at`` field, so it leaves retrieval but keeps its id,
   ``created_at``, provenance and history. Only an explicit forget deletes.
3. **An unresolved target changes nothing.** If a "forget that" cannot be
   pinned to exactly one memory, the result is ``UNRESOLVED`` and the store is
   untouched.
4. **Everything goes through the store interface.** No file is ever touched
   here, so any backend works unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import Enum
from typing import Any, Iterable, Sequence

from agentcore.memory.capture import (
    CaptureAction,
    CaptureDecision,
    CaptureSource,
    MemoryCandidate,
    _CHANGE_MARKERS,
    _normalise,
    first_statement,
    looks_like_secret,
)
from agentcore.memory.errors import MemoryNotFoundError, MemoryValidationError
from agentcore.memory.models import (
    MemoryProvenance,
    MemoryRecord,
    MemoryTrust,
    MemoryType,
)
from agentcore.memory.query import MemoryQuery
from agentcore.models.messages import Message, Role
from agentcore.observability.events import Events
from agentcore.observability.logging import StructuredLogger, get_logger
from agentcore.util.clock import utc_now


@dataclass(frozen=True)
class _StoredCandidate(MemoryCandidate):
    """A candidate rebuilt from a persisted conflict.

    Overrides ``trust()`` and ``provenance()`` to return exactly what was
    recorded when the conflict was detected. Re-deriving them from the source
    would risk silently upgrading model-generated evidence into user-provided
    truth simply because a resolver was invoked later.
    """

    stored_trust: MemoryTrust = MemoryTrust.INFERRED
    stored_provenance: MemoryProvenance = field(
        default_factory=MemoryProvenance)

    def trust(self) -> MemoryTrust:
        return self.stored_trust

    def provenance(self) -> MemoryProvenance:
        return self.stored_provenance


class ConflictResolution(str, Enum):
    """How a caller chose to settle a ``POSSIBLE_CONFLICT``.

    Deliberately three words. Anything richer would invite the resolver to
    interpret intent, which is exactly what it must not do.
    """

    #: The candidate is the current truth; retire the related memories.
    SUPERSEDE = "supersede"
    #: The existing memory stands; the candidate is dropped.
    KEEP_EXISTING = "keep_existing"
    #: The candidate was wrong; drop it and leave everything alone.
    DISCARD_CANDIDATE = "discard_candidate"

    def __str__(self) -> str:
        return self.value


class ResolutionStatus(str, Enum):
    """What the resolver actually did."""

    CREATED = "created"
    UPDATED = "updated"
    SUPERSEDED = "superseded"
    DELETED = "deleted"
    #: A decision that correctly results in no write (REJECT, KEEP_EXISTING,
    #: DISCARD_CANDIDATE, or a duplicate that needs nothing).
    NO_ACTION = "no_action"
    #: A conflict awaiting an explicit choice. Nothing was written.
    PENDING_CONFLICT = "pending_conflict"
    #: A correction whose target could not be identified. Nothing was written.
    UNRESOLVED = "unresolved"
    #: The operation was refused (e.g. unknown id, invalid request).
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ResolutionOutcome:
    """The result of applying one decision. Always explicit about writes."""

    status: ResolutionStatus
    reason: str
    #: The record created or updated, when there is one.
    record: MemoryRecord | None = None
    #: Ids the operation affected (retired, deleted or updated).
    affected_ids: tuple[str, ...] = ()
    #: Ids a caller must choose between, when the outcome is unresolved.
    candidate_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """True only when durable memory was actually modified."""
        return self.status in _WRITING_STATUSES

    @property
    def needs_operator(self) -> bool:
        return self.status in (ResolutionStatus.PENDING_CONFLICT,
                               ResolutionStatus.UNRESOLVED)

    def describe(self) -> dict[str, Any]:
        """Log-safe: ids and shape, never remembered content."""
        payload: dict[str, Any] = {
            "status": self.status.value,
            "affected": list(self.affected_ids),
        }
        if self.candidate_ids:
            payload["candidates"] = list(self.candidate_ids)
        if self.record is not None:
            payload.update({"memory_id": self.record.id,
                            "type": self.record.type.value,
                            "scope": self.record.scope.value,
                            "trust": self.record.trust.value,
                            "revision": self.record.revision})
        return payload


_WRITING_STATUSES = frozenset({
    ResolutionStatus.CREATED, ResolutionStatus.UPDATED,
    ResolutionStatus.SUPERSEDED, ResolutionStatus.DELETED,
})


# ===========================================================================
# Correction language
# ===========================================================================

class CorrectionKind(str, Enum):
    """What a corrective utterance is asking for."""

    #: Remove a memory entirely.
    FORGET = "forget"
    #: Mark a memory as no longer true, without naming a replacement.
    INVALIDATE = "invalidate"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class CorrectionRequest:
    """A structured correction — never a direct delete instruction.

    ``subject`` is what the user pointed at ("my language preference"), or
    empty for a bare "forget that". Empty subjects are what make targeting
    ambiguous, and ambiguity is resolved by *refusing*, never by guessing.
    """

    kind: CorrectionKind
    subject: str = ""
    source_turn_id: str | None = None
    run_id: str | None = None

    @property
    def is_targeted(self) -> bool:
        return bool(self.subject.strip())

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "targeted": self.is_targeted,
                "subject_chars": len(self.subject)}


#: Corrective phrasings. Each captures an optional ``subject`` so a targeted
#: correction can be told from a bare one. Deliberately literal — the model
#: path handles wording these miss, and a missed correction is safe.
_CORRECTION_PATTERNS: tuple[tuple[re.Pattern[str], CorrectionKind], ...] = tuple(
    (re.compile(p, re.I), kind) for p, kind in (
        (r"^\s*(?:please\s+)?forget\s+(?:about\s+)?(?:what\s+i\s+(?:told|said)\s+"
         r"you\s+about\s+)?(?P<subject>.*?)\s*[.!]?\s*$", CorrectionKind.FORGET),
        (r"^\s*(?:please\s+)?delete\s+(?:that\s+|the\s+)?memor(?:y|ies)"
         r"(?:\s+about\s+(?P<subject>.+?))?\s*[.!]?\s*$", CorrectionKind.FORGET),
        (r"^\s*(?:please\s+)?(?:don'?t|do\s+not)\s+remember\s+"
         r"(?P<subject>.*?)(?:\s+anymore|\s+any\s+more)?\s*[.!]?\s*$",
         CorrectionKind.FORGET),
        (r"^\s*(?:please\s+)?stop\s+remembering\s+(?P<subject>.*?)\s*[.!]?\s*$",
         CorrectionKind.FORGET),
        (r"^\s*(?:that'?s|that\s+is|this\s+is)\s+no\s+longer\s+true"
         r"(?:\s+about\s+(?P<subject>.+?))?\s*[.!]?\s*$",
         CorrectionKind.INVALIDATE),
        (r"^\s*(?:that'?s|that\s+is)\s+(?:wrong|incorrect|outdated)"
         r"(?:\s+about\s+(?P<subject>.+?))?\s*[.!]?\s*$",
         CorrectionKind.INVALIDATE),
        (r"^\s*(?:i\s+)?no\s+longer\s+(?P<subject>.+?)\s*[.!]?\s*$",
         CorrectionKind.INVALIDATE),
    ))

#: Words that refer back to something rather than naming it. A subject made
#: only of these is not a target.
_ANAPHORA: frozenset[str] = frozenset({
    "that", "this", "it", "those", "these", "them", "that one", "the last one",
    "the last thing", "what i said", "what i told you", "everything",
    "all of it", "all that", "anything",
})


def parse_correction(text: str, *, source_turn_id: str | None = None,
                     run_id: str | None = None) -> CorrectionRequest | None:
    """Recognise a corrective utterance, or return None.

    Produces a *request*, never an action: the caller must still resolve a
    target, and an unresolvable target deletes nothing.
    """
    candidate = first_statement(_normalise(text))
    if not candidate:
        return None
    for pattern, kind in _CORRECTION_PATTERNS:
        match = pattern.match(candidate)
        if not match:
            continue
        subject = _normalise(match.groupdict().get("subject") or "")
        # Strip a leading possessive so "my language preference" searches well.
        subject = re.sub(r"^(?:my|the|our)\s+", "", subject, flags=re.I).strip()
        if subject.lower() in _ANAPHORA:
            subject = ""
        return CorrectionRequest(kind=kind, subject=subject,
                                 source_turn_id=source_turn_id, run_id=run_id)
    return None


# ===========================================================================
# Resolver
# ===========================================================================

class MemoryResolver:
    """Applies capture decisions and corrections to a memory store.

    Holds no policy of its own and constructs no backend: the store is
    injected, so ``Agent -> MemoryResolver -> MemoryStore`` stays honest and
    any backend works unchanged.
    """

    def __init__(self, store: Any, *, conflicts: Any | None = None,
                 logger: StructuredLogger | None = None) -> None:
        self.store = store
        # Durable pending conflicts. Optional; without it the resolver behaves
        # exactly as before and conflicts live only for this process.
        self.conflicts = conflicts
        self._log = logger or get_logger("memory.resolve")

    # -- capture decisions -------------------------------------------------
    def apply(self, decision: CaptureDecision, *,
              conflict: ConflictResolution | None = None) -> ResolutionOutcome:
        """Carry out ``decision``.

        ``conflict`` is required to settle a ``POSSIBLE_CONFLICT``; without it
        the outcome is ``PENDING_CONFLICT`` and nothing is written. That is the
        whole point: a contradiction is only resolved when somebody says so.
        """
        if self.store is None:
            return self._fail("no memory store is available")

        action = decision.action
        if action is CaptureAction.REJECT:
            return self._outcome(ResolutionStatus.NO_ACTION,
                                 f"candidate was rejected: {decision.reason}")
        if action is CaptureAction.CREATE:
            return self._create(decision.candidate)
        if action is CaptureAction.UPDATE:
            return self._update(decision)
        if action is CaptureAction.POSSIBLE_CONFLICT:
            return self._conflict(decision, conflict)
        return self._fail(f"unknown capture action '{action}'")

    # -- individual actions ------------------------------------------------
    def _create(self, candidate: MemoryCandidate) -> ResolutionOutcome:
        try:
            record = self.store.add(candidate.to_record())
        except Exception as exc:  # noqa: BLE001 - a write failure is reported
            return self._fail(f"could not store the memory: "
                              f"{type(exc).__name__}")
        return self._outcome(ResolutionStatus.CREATED, "new memory stored",
                             record=record, affected_ids=(record.id,))

    def _update(self, decision: CaptureDecision) -> ResolutionOutcome:
        """Refresh an existing memory rather than storing a second copy.

        ``UPDATE`` means the candidate says the same thing as a record that
        already exists, so the useful outcome is to keep one record and let it
        reflect the stronger evidence. The record's id, ``created_at`` and
        original provenance survive because :meth:`MemoryRecord.evolve` will
        not let them change.
        """
        target = self._first_existing(decision.related_ids)
        if target is None:
            # The related record vanished between decision and application;
            # storing the candidate is the honest fallback.
            return self._create(decision.candidate)

        candidate = decision.candidate
        changes: dict[str, Any] = {}
        # Only ever raise trust, and only from a stronger *user* source. An
        # inference must never promote a user-provided fact.
        if candidate.user_supplied and \
                candidate.trust().rank > target.trust.rank:
            changes["trust"] = candidate.trust()
            changes["provenance"] = self._merged_provenance(target, candidate)
        merged_tags = tuple(sorted(set(target.tags) | set(candidate.tags)))
        if merged_tags != target.tags:
            changes["tags"] = merged_tags

        if not changes:
            return self._outcome(
                ResolutionStatus.NO_ACTION,
                "memory already records this; nothing to change",
                record=target, affected_ids=(target.id,))
        try:
            record = self.store.update(target.id, **changes)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not update the memory: "
                              f"{type(exc).__name__}")
        self._log.info(Events.MEMORY_UPDATED, **record.describe(),
                       operation="capture_update")
        return self._outcome(ResolutionStatus.UPDATED,
                             "existing memory refreshed; no duplicate created",
                             record=record, affected_ids=(record.id,))

    def _conflict(self, decision: CaptureDecision,
                  choice: ConflictResolution | None) -> ResolutionOutcome:
        related = tuple(decision.related_ids)
        if choice is None:
            self._log.info(Events.MEMORY_CONFLICT_PENDING,
                           **decision.describe())
            return self._outcome(
                ResolutionStatus.PENDING_CONFLICT,
                "conflicting memory exists; an explicit resolution is "
                "required before anything is changed",
                candidate_ids=related)

        if choice is ConflictResolution.KEEP_EXISTING:
            self._log.info(Events.MEMORY_CONFLICT_RESOLVED,
                           resolution=choice.value, affected=list(related))
            return self._outcome(ResolutionStatus.NO_ACTION,
                                 "existing memory kept; candidate discarded",
                                 affected_ids=related)
        if choice is ConflictResolution.DISCARD_CANDIDATE:
            self._log.info(Events.MEMORY_CONFLICT_RESOLVED,
                           resolution=choice.value, affected=[])
            return self._outcome(ResolutionStatus.NO_ACTION,
                                 "candidate discarded at the caller's request")
        return self._supersede(decision.candidate, related)

    def _supersede(self, candidate: MemoryCandidate,
                   related: Sequence[str]) -> ResolutionOutcome:
        """Store the new truth and retire the old records.

        Retirement uses the existing ``expires_at`` field rather than deletion:
        an expired record leaves search and ``all_records()`` — so retrieval
        can never present it as current — while keeping its id, origin and
        revision history for audit. Nothing is destroyed, and no parallel
        version-control system is invented.

        The replacement inherits the retired records' tags. By choosing
        ``SUPERSEDE`` the caller has asserted these memories are about the
        same subject, so the new fact must stay findable by the same terms —
        otherwise correcting a memory would quietly make it unsearchable.
        """
        retiring = [record for record in
                    (self._get(memory_id) for memory_id in related)
                    if record is not None]
        inherited = {tag for record in retiring for tag in record.tags}

        record = candidate.to_record()
        if inherited - set(record.tags):
            record = record.evolve(
                tags=tuple(sorted(set(record.tags) | inherited)))
            # evolve() bumps the revision; a brand-new record starts at 1.
            record = replace(record, revision=1)
        try:
            record = self.store.add(record)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not store the replacement memory: "
                              f"{type(exc).__name__}")

        retired: list[str] = []
        # A moment in the past, so the record is expired immediately and
        # deterministically rather than at some future boundary.
        retired_at = utc_now() - timedelta(seconds=1)
        for existing in retiring:
            try:
                self.store.update(existing.id, expires_at=retired_at)
            except Exception:  # noqa: BLE001 - one failure must not abort
                continue
            retired.append(existing.id)
            self._log.info(Events.MEMORY_SUPERSEDED, memory_id=existing.id,
                           replaced_by=record.id)

        self._log.info(Events.MEMORY_CONFLICT_RESOLVED,
                       resolution=ConflictResolution.SUPERSEDE.value,
                       affected=retired, memory_id=record.id)
        return self._outcome(
            ResolutionStatus.SUPERSEDED,
            f"new memory stored; {len(retired)} earlier record(s) retired",
            record=record, affected_ids=tuple(retired))

    # -- persisted conflicts ------------------------------------------------
    def list_pending_conflicts(self, *, limit: int | None = None,
                               scope_key: str | None = None) -> list[Any]:
        """Unresolved conflicts, newest first. Empty when none are stored."""
        if self.conflicts is None:
            return []
        try:
            return self.conflicts.list_pending(limit=limit,
                                               scope_key=scope_key)
        except Exception:  # noqa: BLE001 - discovery must never raise
            return []

    def resolve_conflict(self, conflict_id: str,
                         choice: ConflictResolution, *,
                         operator: str | None = None) -> ResolutionOutcome:
        """Settle a *persisted* conflict, then mark it resolved.

        Ordering is deliberate and is the consistency strategy. The memory
        write happens first; the conflict is marked resolved only once that
        write has succeeded. The two possible interruptions are therefore:

        * **crash before the memory write** — nothing happened at all, and the
          conflict is still pending. Safe: retrying repeats the whole thing.
        * **crash between the write and the mark** — memory is correct but the
          conflict still reads pending. Detectable, and never a false claim of
          success. :meth:`incomplete_conflicts` finds exactly these, and
          re-resolving is refused rather than silently duplicating the write.

        The reverse order would be far worse: a conflict marked resolved whose
        memory was never changed is invisible and unrecoverable. Two JSON
        files cannot be written in one transaction, so the design makes the
        surviving inconsistency the detectable, harmless one.
        """
        if self.conflicts is None:
            return self._fail("no conflict store is available")
        if self.store is None:
            return self._fail("no memory store is available")

        try:
            conflict = self.conflicts.get(conflict_id)
        except Exception:  # noqa: BLE001 - unknown or unreadable
            return self._fail(f"no conflict with id '{conflict_id}'")

        if not conflict.is_pending:
            return self._fail(
                f"conflict '{conflict.id}' was already resolved "
                f"({conflict.resolution})")

        outcome = self._apply_conflict_choice(conflict, choice)
        if outcome.status is ResolutionStatus.FAILED:
            # The memory write failed, so the conflict stays pending and can
            # be retried. Nothing is marked.
            return outcome

        try:
            self.conflicts.mark_resolved(
                conflict.id, resolution=choice.value, operator=operator,
                affected_ids=outcome.affected_ids)
        except Exception as exc:  # noqa: BLE001 - report honestly
            # The memory change already happened. Say so rather than pretend.
            self._log.warning(Events.MEMORY_CONFLICT_INCOMPLETE,
                              conflict_id=conflict.id,
                              error=type(exc).__name__)
            return ResolutionOutcome(
                status=outcome.status,
                reason=(f"{outcome.reason}; WARNING: the conflict record could "
                        f"not be marked resolved and still reads pending"),
                record=outcome.record, affected_ids=outcome.affected_ids)
        return outcome

    def _apply_conflict_choice(self, conflict: Any,
                               choice: ConflictResolution
                               ) -> ResolutionOutcome:
        """Perform the memory-side effect for a persisted conflict."""
        if choice is ConflictResolution.KEEP_EXISTING:
            return self._outcome(
                ResolutionStatus.NO_ACTION,
                "existing memory kept; candidate discarded",
                affected_ids=tuple(conflict.related_ids))
        if choice is ConflictResolution.DISCARD_CANDIDATE:
            return self._outcome(ResolutionStatus.NO_ACTION,
                                 "candidate discarded at the caller's request")
        return self._supersede(self._candidate_from(conflict),
                               tuple(conflict.related_ids))

    @staticmethod
    def _candidate_from(conflict: Any) -> MemoryCandidate:
        """Rebuild the candidate a persisted conflict describes.

        Trust is reconstructed from the *recorded* provenance, not re-derived:
        invoking a resolver must never convert model-generated evidence into
        user-provided truth.
        """
        provenance = conflict.candidate_provenance
        source = CaptureSource.ASSISTANT_INFERENCE
        try:
            source = CaptureSource(str(provenance.source))
        except (ValueError, TypeError):
            if provenance.operator_supplied:
                source = CaptureSource.USER_STATEMENT
        return _StoredCandidate(
            content=conflict.candidate_content,
            type=conflict.candidate_type,
            scope=conflict.candidate_scope,
            scope_key=conflict.candidate_scope_key,
            tags=tuple(conflict.candidate_tags),
            source=source,
            reason=conflict.reason,
            run_id=provenance.run_id,
            source_turn_id=provenance.subtask_id,
            stored_trust=conflict.candidate_trust,
            stored_provenance=provenance,
        )

    def incomplete_conflicts(self) -> list[Any]:
        """Pending conflicts whose memory change appears to have happened.

        Detects the one inconsistency :meth:`resolve_conflict` can leave: a
        crash after the memory write but before the record was marked. A
        conflict is suspect when every related memory it names has already
        been retired, which only a supersede does.
        """
        suspect: list[Any] = []
        for conflict in self.list_pending_conflicts():
            related = [self._get(i) for i in conflict.related_ids]
            present = [r for r in related if r is not None]
            if not present:
                continue
            if all(r.is_expired() for r in present):
                suspect.append(conflict)
        return suspect

    # -- corrections -------------------------------------------------------
    def resolve_correction(self, request: CorrectionRequest, *,
                           memory_id: str | None = None,
                           limit: int = 10) -> ResolutionOutcome:
        """Act on a "forget that" / "that's no longer true" request.

        Targeting is deliberately strict:

        * an explicit ``memory_id`` is used directly;
        * otherwise the subject is searched, and the correction proceeds
          **only if exactly one** memory matches;
        * a bare correction with no subject, or an ambiguous one, returns
          ``UNRESOLVED`` with the candidate ids and writes nothing.

        Deleting the closest-looking record would be the single most damaging
        thing this system could do, so it never guesses.
        """
        if self.store is None:
            return self._fail("no memory store is available")

        if memory_id:
            record = self._get(memory_id)
            if record is None:
                return self._fail(f"no memory with id '{memory_id}'")
            return self._apply_correction(request, record)

        if not request.is_targeted:
            self._log.info(Events.MEMORY_FORGET_UNRESOLVED,
                           **request.describe(), reason="no subject given")
            return self._outcome(
                ResolutionStatus.UNRESOLVED,
                "the correction did not say which memory it refers to; "
                "nothing was changed",
                candidate_ids=tuple(r.id for r in self._recent(limit)))

        matches = self._find(request.subject, limit=limit)
        if not matches:
            self._log.info(Events.MEMORY_FORGET_UNRESOLVED,
                           **request.describe(), reason="no match")
            return self._outcome(
                ResolutionStatus.UNRESOLVED,
                "no stored memory matches that subject; nothing was changed")
        if len(matches) > 1:
            self._log.info(Events.MEMORY_FORGET_UNRESOLVED,
                           **request.describe(), reason="ambiguous",
                           matches=len(matches))
            return self._outcome(
                ResolutionStatus.UNRESOLVED,
                f"{len(matches)} memories match that subject; specify which "
                "one before anything is changed",
                candidate_ids=tuple(r.id for r in matches))
        return self._apply_correction(request, matches[0])

    def _apply_correction(self, request: CorrectionRequest,
                          record: MemoryRecord) -> ResolutionOutcome:
        if request.kind is CorrectionKind.FORGET:
            return self.forget(record.id, reason="user asked to forget it")
        # INVALIDATE: the fact is no longer true but the user named no
        # replacement, so the record is retired rather than deleted — its
        # history stays available and retrieval stops returning it.
        try:
            updated = self.store.update(
                record.id, expires_at=utc_now() - timedelta(seconds=1))
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not retire the memory: "
                              f"{type(exc).__name__}")
        self._log.info(Events.MEMORY_SUPERSEDED, memory_id=record.id,
                       replaced_by=None, operation="invalidate")
        return self._outcome(ResolutionStatus.SUPERSEDED,
                             "memory marked no longer true and retired from "
                             "retrieval",
                             record=updated, affected_ids=(record.id,))

    def forget(self, memory_id: str, *, reason: str = "") -> ResolutionOutcome:
        """Delete exactly one memory, by id, through the store."""
        if self.store is None:
            return self._fail("no memory store is available")
        record = self._get(memory_id)
        if record is None:
            return self._fail(f"no memory with id '{memory_id}'")
        try:
            self.store.delete(record.id)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"could not delete the memory: "
                              f"{type(exc).__name__}")
        # Audit what was removed — identity and shape, never the content.
        self._log.info(Events.MEMORY_FORGOTTEN, memory_id=record.id,
                       type=record.type.value, scope=record.scope.value,
                       trust=record.trust.value, revision=record.revision,
                       reason=reason[:120])
        return self._outcome(ResolutionStatus.DELETED,
                             reason or "memory deleted",
                             affected_ids=(record.id,))

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _merged_provenance(target: MemoryRecord,
                           candidate: MemoryCandidate) -> MemoryProvenance:
        """Provenance for an upgraded record.

        The user is now the source, so ``operator_supplied`` becomes true and
        the source names the correction — but the *original* run id is kept,
        so where the memory first came from is never lost.
        """
        original = target.provenance
        fresh = candidate.provenance()
        return MemoryProvenance(
            operator_supplied=fresh.operator_supplied or
            original.operator_supplied,
            run_id=original.run_id or fresh.run_id,
            task_id=original.task_id,
            subtask_id=fresh.subtask_id or original.subtask_id,
            source=fresh.source or original.source,
            note=(fresh.note or original.note)[:160],
        )

    def _get(self, memory_id: str) -> MemoryRecord | None:
        try:
            return self.store.get(memory_id)
        except Exception:  # noqa: BLE001 - missing or unreadable
            return None

    def _first_existing(self, ids: Iterable[str]) -> MemoryRecord | None:
        for memory_id in ids:
            record = self._get(memory_id)
            if record is not None:
                return record
        return None

    def _recent(self, limit: int) -> list[MemoryRecord]:
        try:
            return list(self.store.all_records())[:limit]
        except Exception:  # noqa: BLE001
            return []

    def _find(self, subject: str, *, limit: int) -> list[MemoryRecord]:
        """Deterministic search for a correction subject.

        Reuses the store's existing search — no second retrieval system and no
        fuzzy matching. Results are de-duplicated by id and returned in the
        store's own ranked order.
        """
        found: dict[str, MemoryRecord] = {}
        search = getattr(self.store, "search", None)
        if not callable(search):
            return []
        for query in self._queries(subject):
            try:
                result = search(query, limit=limit)
            except TypeError:
                try:
                    result = search(query)
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue
            for item in list(result or []):
                record = getattr(item, "record", item)
                identifier = getattr(record, "id", None)
                if identifier and hasattr(record, "content"):
                    found[identifier] = record
        return list(found.values())[:limit]

    @staticmethod
    def _queries(subject: str) -> list[str]:
        text = _normalise(subject)
        queries = [text] if text else []
        for word in re.findall(r"[a-zA-Z0-9][\w\-]{3,}", text.lower()):
            if word not in queries:
                queries.append(word)
            if len(queries) >= 6:
                break
        return queries

    def _outcome(self, status: ResolutionStatus, reason: str, **kwargs: Any
                 ) -> ResolutionOutcome:
        outcome = ResolutionOutcome(status=status, reason=reason, **kwargs)
        if outcome.changed:
            self._log.info(Events.MEMORY_RESOLVED, **outcome.describe())
        return outcome

    def _fail(self, reason: str) -> ResolutionOutcome:
        self._log.warning(Events.MEMORY_RESOLVE_FAILED, reason=reason[:160])
        return ResolutionOutcome(status=ResolutionStatus.FAILED, reason=reason)
