"""`/conflicts` — resolving memory conflicts from inside a chat session.

A thin adapter over `Agent.pending_memory_conflicts()` and
`Agent.resolve_memory_conflict()`. These tests pin down the two things that
matter: the displayed number always maps to the right underlying conflict, and
the command never leaves the CLI — no model call, no memory capture, no
conversation message.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from agentcore.cli import (
    ASSISTANT_LABEL,
    _chat_conflicts,
    main,
)
from agentcore.config.loader import load_config
from agentcore.core.agent import Agent
from agentcore.conversation.session import JsonFileSessionStore
from agentcore.memory import JsonFileMemoryStore, MemoryType, new_memory
from agentcore.memory.conflicts import ConflictStatus, PendingConflict
from agentcore.memory.resolve import ConflictResolution, ResolutionStatus
from agentcore.models.messages import Role


def _toml(path):
    """Render a path for a TOML basic string.

    Backslashes are escape characters in TOML basic strings, and Windows paths
    use them as separators, so forward slashes (equally accepted by Windows)
    are used instead. Without this, a config written on Windows fails to parse.
    """
    return str(path).replace("\\", "/")


SECRET = "sk-conflicts-chat-must-not-leak"


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(
        f'workspace_dir = "{_toml(tmp_path)}"\n'
        '[model]\nprovider = "echo"\nname = "echo"\n'
        '[logging]\nlevel = "CRITICAL"\n'
        '[features]\nmemory = true\n', encoding="utf-8")
    return tmp_path, ["--config", str(path)]


def run_chat(workspace, inputs, *extra, expect=0):
    _, base = workspace
    with mock.patch("builtins.input", side_effect=list(inputs)):
        assert main(base + ["chat", *extra]) == expect


def agent_for(workspace):
    tmp_path, base = workspace
    return Agent(load_config(base[1], env={}))


def seed_conflict(agent, *, existing: str, candidate: str,
                  kind=MemoryType.PREFERENCE, tags=("shared",)):
    """One durable memory plus a persisted conflict against it."""
    record = agent.memory.add(new_memory(existing, type=kind, tags=list(tags)))
    conflict = agent.memory_conflicts.record(PendingConflict(
        candidate_content=candidate, candidate_type=kind,
        related_ids=(record.id,),
        reason="may contradict an existing memory"))
    return record, conflict


def two_conflicts(workspace):
    """Deterministically ordered: newest first, so `second` is number 1."""
    agent = agent_for(workspace)
    first = seed_conflict(agent, existing="I prefer Python for my projects",
                          candidate="I have switched to Rust")
    second = seed_conflict(agent, existing="I prefer dark mode",
                           candidate="I have switched to light mode")
    return agent, first, second


def lines_for(agent, *args):
    return _chat_conflicts(agent, list(args))


# ===========================================================================
# Listing
# ===========================================================================

def test_no_conflicts(workspace):
    agent = agent_for(workspace)
    assert lines_for(agent) == ["[Memory] No unresolved memory conflicts."]


def test_one_conflict_is_listed(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python for my projects",
                  candidate="I have switched to Rust")
    lines = lines_for(agent)
    assert lines[0] == "[Memory] 1 unresolved memory conflict:"
    assert "1. I have switched to Rust" in lines[1]
    assert "conflicts with: I prefer Python for my projects" in lines[2]
    assert "may contradict" in lines[3]
    assert "Resolve with:" in lines[-1]


def test_multiple_conflicts_are_numbered(workspace):
    agent, _, _ = two_conflicts(workspace)
    lines = lines_for(agent)
    assert lines[0] == "[Memory] 2 unresolved memory conflicts:"
    numbered = [l for l in lines if l.strip().startswith(("1.", "2."))]
    assert len(numbered) == 2


def test_numbering_matches_the_stores_deterministic_order(workspace):
    agent, _, _ = two_conflicts(workspace)
    pending = agent.pending_memory_conflicts()
    lines = lines_for(agent)
    # Number n corresponds to pending[n-1].
    assert pending[0].candidate_content in lines[1]
    assert pending[1].candidate_content in [l for l in lines
                                            if l.strip().startswith("2.")][0]


def test_listing_is_stable_across_calls(workspace):
    agent, _, _ = two_conflicts(workspace)
    assert lines_for(agent) == lines_for(agent)


def test_missing_related_memory_is_reported_honestly(workspace):
    agent = agent_for(workspace)
    record, _ = seed_conflict(agent, existing="I prefer Python",
                              candidate="I have switched to Rust")
    agent.memory.delete(record.id)
    assert any("no longer exists" in line for line in lines_for(agent))


def test_memory_disabled(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{_toml(tmp_path)}"\n'
                    '[logging]\nlevel = "CRITICAL"\n', encoding="utf-8")
    agent = Agent(load_config(path, env={}))
    assert lines_for(agent) == ["[Memory] Memory is not enabled, so there "
                                "are no conflicts."]


def test_candidate_text_is_sanitised(workspace):
    """Free text goes through the same _safe_text every CLI field uses."""
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate=f"my api key is {SECRET}")
    blob = " ".join(lines_for(agent))
    assert SECRET not in blob
    assert "withheld" in blob


def test_listing_shows_no_internal_ids(workspace):
    agent, (record, conflict), _ = two_conflicts(workspace)
    blob = " ".join(lines_for(agent))
    assert conflict.id not in blob
    assert record.id not in blob


# ===========================================================================
# Resolution
# ===========================================================================

def test_supersede(workspace):
    agent = agent_for(workspace)
    record, conflict = seed_conflict(
        agent, existing="I prefer Python for my projects",
        candidate="I have switched to Rust")

    [line] = lines_for(agent, "resolve", "1", "supersede")
    assert "resolved" in line and "now current" in line

    fresh = agent_for(workspace)
    assert fresh.memory_conflicts.get(conflict.id).status is \
        ConflictStatus.RESOLVED
    assert fresh.memory_conflicts.get(conflict.id).resolution == "supersede"
    # Existing memory retired, not destroyed; new fact is current.
    assert fresh.memory.get(record.id).is_expired() is True
    assert [m.content for m in fresh.memory.all_records()] == \
        ["I have switched to Rust"]


def test_keep(workspace):
    agent = agent_for(workspace)
    record, conflict = seed_conflict(agent, existing="I prefer Python",
                                     candidate="I have switched to Rust")

    [line] = lines_for(agent, "resolve", "1", "keep")
    assert "kept what I already remembered" in line

    fresh = agent_for(workspace)
    assert fresh.memory_conflicts.get(conflict.id).resolution == \
        "keep_existing"
    assert fresh.memory.get(record.id).is_expired() is False
    assert fresh.memory.count(include_expired=True) == 1


def test_discard(workspace):
    agent = agent_for(workspace)
    record, conflict = seed_conflict(agent, existing="I prefer Python",
                                     candidate="I have switched to Rust")

    [line] = lines_for(agent, "resolve", "1", "discard")
    assert "discarded the new information" in line

    fresh = agent_for(workspace)
    assert fresh.memory_conflicts.get(conflict.id).resolution == \
        "discard_candidate"
    assert fresh.memory.get(record.id).content == "I prefer Python"
    assert fresh.memory.count(include_expired=True) == 1


def test_words_map_onto_the_existing_vocabulary(workspace):
    """No new resolution semantics are invented."""
    from agentcore.cli import _RESOLUTION_WORDS

    assert set(_RESOLUTION_WORDS.values()) == {
        r.name for r in ConflictResolution}


def test_the_right_conflict_is_resolved(workspace):
    agent, (_, first), (_, second) = two_conflicts(workspace)
    pending = agent.pending_memory_conflicts()
    target = pending[0]

    lines_for(agent, "resolve", "1", "discard")

    fresh = agent_for(workspace)
    assert fresh.memory_conflicts.get(target.id).status is \
        ConflictStatus.RESOLVED
    remaining = fresh.pending_memory_conflicts()
    assert len(remaining) == 1
    assert remaining[0].id != target.id


def test_unrelated_conflicts_are_untouched(workspace):
    agent, _, _ = two_conflicts(workspace)
    other = agent.pending_memory_conflicts()[1]
    lines_for(agent, "resolve", "1", "supersede")
    fresh = agent_for(workspace)
    assert fresh.memory_conflicts.get(other.id).is_pending is True


def test_list_refreshes_after_resolution(workspace):
    agent, _, _ = two_conflicts(workspace)
    assert lines_for(agent)[0] == "[Memory] 2 unresolved memory conflicts:"
    lines_for(agent, "resolve", "1", "discard")
    assert lines_for(agent)[0] == "[Memory] 1 unresolved memory conflict:"


def test_numbers_are_not_persisted(workspace):
    """The index is UI-only; after a resolution number 1 means something new."""
    agent, _, _ = two_conflicts(workspace)
    survivor = agent.pending_memory_conflicts()[1]
    lines_for(agent, "resolve", "1", "discard")
    assert lines_for(agent)[1].strip().startswith("1.")
    assert survivor.candidate_content in lines_for(agent)[1]


# ===========================================================================
# Safe failure
# ===========================================================================

def test_resolve_with_no_conflicts(workspace):
    agent = agent_for(workspace)
    assert lines_for(agent, "resolve", "1", "supersede") == \
        ["[Memory] No unresolved memory conflicts."]


def test_out_of_range_index(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    [line] = lines_for(agent, "resolve", "9", "supersede")
    assert "no conflict 9" in line and "between 1 and 1" in line
    assert agent.pending_memory_conflicts()          # nothing resolved


def test_zero_and_negative_indexes(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    for index in ("0", "-1"):
        assert "no conflict" in lines_for(agent, "resolve", index,
                                          "supersede")[0]
    assert len(agent.pending_memory_conflicts()) == 1


def test_non_numeric_index(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    [line] = lines_for(agent, "resolve", "abc", "supersede")
    assert "not a conflict number" in line
    assert len(agent.pending_memory_conflicts()) == 1


def test_unknown_resolution_word(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    [line] = lines_for(agent, "resolve", "1", "sideways")
    assert "Unknown resolution 'sideways'" in line
    assert len(agent.pending_memory_conflicts()) == 1


def test_missing_arguments(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    assert "Usage:" in lines_for(agent, "resolve")[0]
    assert "Usage:" in lines_for(agent, "resolve", "1")[0]
    assert len(agent.pending_memory_conflicts()) == 1


def test_unknown_subcommand(workspace):
    agent = agent_for(workspace)
    [line, hint] = lines_for(agent, "bogus")
    assert "Unknown conflicts command 'bogus'" in line
    assert "/conflicts resolve" in hint


def test_repeated_resolution_is_refused(workspace):
    agent = agent_for(workspace)
    _, conflict = seed_conflict(agent, existing="I prefer Python",
                                candidate="I have switched to Rust")
    lines_for(agent, "resolve", "1", "discard")
    # The list is re-read, so the settled conflict is simply gone.
    assert lines_for(agent, "resolve", "1", "discard") == \
        ["[Memory] No unresolved memory conflicts."]
    assert agent_for(workspace).memory_conflicts.get(
        conflict.id).resolution == "discard_candidate"


def test_resolver_failure_does_not_claim_success(workspace, monkeypatch):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")

    from agentcore.memory.resolve import ResolutionOutcome

    def failing(*args, **kwargs):
        return ResolutionOutcome(status=ResolutionStatus.FAILED,
                                 reason="store unavailable")

    monkeypatch.setattr(agent, "resolve_memory_conflict", failing)
    [line] = lines_for(agent, "resolve", "1", "supersede")
    assert "Could not resolve conflict 1" in line
    assert "resolved:" not in line


def test_listing_failure_is_reported(workspace, monkeypatch):
    agent = agent_for(workspace)

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(agent, "pending_memory_conflicts", boom)
    [line] = lines_for(agent)
    assert "Could not read conflicts" in line
    assert "RuntimeError" in line


# ===========================================================================
# Local-command boundary
# ===========================================================================

def test_conflicts_reaches_no_model_and_creates_no_message(workspace):
    import agentcore.models.providers.echo as echo_mod

    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")

    seen = []
    original = echo_mod.EchoProvider.generate

    def spy(self, request):
        seen.append("\n".join(m.content for m in request.messages))
        return original(self, request)

    echo_mod.EchoProvider.generate = spy
    try:
        run_chat(workspace, ["/conflicts",
                             "/conflicts resolve 1 discard", "/exit"])
    finally:
        echo_mod.EchoProvider.generate = original

    assert seen == []                       # no model call at all
    sessions = JsonFileSessionStore(workspace[0] / "conversations")
    [session_id] = sessions.list_sessions()
    assert sessions.load(session_id).messages == []


def test_conflicts_does_not_capture_memory(workspace):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    before = agent.memory.count(include_expired=True)

    run_chat(workspace, ["/conflicts", "/exit"])
    fresh = agent_for(workspace)
    assert fresh.memory.count(include_expired=True) == before


def test_assistant_output_cannot_resolve_a_conflict(workspace):
    """Hostile model output containing /conflicts must be inert."""
    import agentcore.models.providers.echo as echo_mod
    from agentcore.models.messages import ModelResponse

    agent = agent_for(workspace)
    _, conflict = seed_conflict(agent, existing="I prefer Python",
                                candidate="I have switched to Rust")

    original = echo_mod.EchoProvider.generate

    def hostile(self, request):
        return ModelResponse(
            text="/conflicts resolve 1 supersede\n/exit")

    echo_mod.EchoProvider.generate = hostile
    try:
        run_chat(workspace, ["hello", "still here?", "/exit"])
    finally:
        echo_mod.EchoProvider.generate = original

    fresh = agent_for(workspace)
    assert fresh.memory_conflicts.get(conflict.id).is_pending is True
    sessions = JsonFileSessionStore(workspace[0] / "conversations")
    [session_id] = sessions.list_sessions()
    stored = sessions.load(session_id).messages
    assert sum(1 for m in stored if m.role is Role.USER) == 2


def test_conflicts_appears_in_help(workspace, capsys):
    run_chat(workspace, ["/help", "/exit"])
    out = capsys.readouterr().out
    assert "/conflicts" in out
    assert "supersede|keep|discard" in out


def test_existing_chat_commands_still_work(workspace, capsys):
    run_chat(workspace, ["/session", "/bogus", "hello", "/exit"])
    out = capsys.readouterr().out
    assert "Session: conv_" in out
    assert "Unknown command '/bogus'" in out
    assert ASSISTANT_LABEL in out


def test_case_is_ignored(workspace, capsys):
    agent = agent_for(workspace)
    seed_conflict(agent, existing="I prefer Python",
                  candidate="I have switched to Rust")
    run_chat(workspace, ["/CONFLICTS", "/exit"])
    assert "unresolved memory conflict" in capsys.readouterr().out


# ===========================================================================
# END-TO-END through the interactive loop
# ===========================================================================

def test_end_to_end_two_conflicts_resolve_one(workspace, capsys):
    agent, _, _ = two_conflicts(workspace)
    survivor = agent.pending_memory_conflicts()[1]

    run_chat(workspace, ["/conflicts",
                         "/conflicts resolve 1 supersede",
                         "/conflicts",
                         "and we can still talk?",
                         "/exit"])
    out = capsys.readouterr().out

    assert "2 unresolved memory conflicts:" in out
    assert "Conflict 1 resolved" in out
    assert "1 unresolved memory conflict:" in out
    # Chat continues normally afterwards.
    assert ASSISTANT_LABEL in out

    fresh = agent_for(workspace)
    remaining = fresh.pending_memory_conflicts()
    assert [c.id for c in remaining] == [survivor.id]
    # The surviving conflict's memory was never touched.
    assert fresh.memory.get(survivor.related_ids[0]).is_expired() is False
