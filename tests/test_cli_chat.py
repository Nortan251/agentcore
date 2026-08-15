"""`agentcore chat`: the interactive surface over ConversationRuntime.

The CLI owns only input, output and the local ``/`` commands. These tests pin
down that boundary, and above all that the interface never claims a memory
effect the runtime did not report.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from unittest import mock

import pytest

from agentcore.cli import (
    ASSISTANT_LABEL,
    CHAT_HELP,
    COMMAND_PREFIX,
    _memory_notices,
    _render_turn,
    _turn_payload,
    main,
)
from agentcore.config.schema import LoggingConfig
from agentcore.conversation.runtime import TurnResult
from agentcore.conversation.session import ConversationSession, JsonFileSessionStore
from agentcore.memory import JsonFileMemoryStore, new_memory
from agentcore.memory.resolve import ResolutionOutcome, ResolutionStatus
from agentcore.models.messages import Message, Role
from agentcore.observability.logging import configure_logging

SECRET = "sk-chat-tests-must-not-leak-42"


@pytest.fixture
def workspace(tmp_path):
    """A config file wiring the deterministic echo provider to a workspace."""
    path = tmp_path / "agent.toml"
    path.write_text(
        f'workspace_dir = "{tmp_path}"\n'
        '[model]\nprovider = "echo"\nname = "echo"\n'
        '[logging]\nlevel = "CRITICAL"\n'
        '[features]\nmemory = true\n', encoding="utf-8")
    return tmp_path, ["--config", str(path)]


def run_chat(workspace, inputs, *extra, expect=0):
    """Drive the interactive loop with scripted stdin."""
    tmp_path, base = workspace
    with mock.patch("builtins.input", side_effect=list(inputs)):
        code = main(base + ["chat", *extra])
    assert code == expect
    return code


def sessions_of(tmp_path):
    return JsonFileSessionStore(tmp_path / "conversations")


def only_session(tmp_path):
    ids = sessions_of(tmp_path).list_sessions()
    assert len(ids) == 1, ids
    return ids[0]


def turn(**kwargs):
    session = kwargs.pop("session", None) or ConversationSession()
    return TurnResult(session=session, **kwargs)


# ===========================================================================
# Notices — driven only by TurnResult
# ===========================================================================

def test_no_notice_when_nothing_happened():
    assert _memory_notices(turn(reply=Message.assistant("hi"))) == []


def test_created_notice():
    notices = _memory_notices(turn(memories_created=[object()]))
    assert notices == ["[Memory] Remembered something new."]


def test_created_notice_pluralises():
    notices = _memory_notices(turn(memories_created=[object(), object()]))
    assert "2 new things" in notices[0]


def test_updated_notice():
    notices = _memory_notices(turn(memories_updated=[object()]))
    assert notices == ["[Memory] Updated something I already knew."]


def test_conflict_notice_says_nothing_was_overwritten():
    [notice] = _memory_notices(turn(conflicts=[object()]))
    assert "did not overwrite" in notice
    assert "agentcore memory conflicts" in notice


def test_memory_error_notice_does_not_claim_success():
    [notice] = _memory_notices(turn(memory_error="disk full"))
    assert "could not save" in notice
    assert "Remembered" not in notice


def test_memory_error_never_reports_the_technical_reason():
    """Honesty, not debug spam: no exception text reaches the user."""
    [notice] = _memory_notices(turn(memory_error="RuntimeError: disk full"))
    assert "RuntimeError" not in notice


@pytest.mark.parametrize("status,fragment", [
    (ResolutionStatus.DELETED, "Forgotten"),
    (ResolutionStatus.SUPERSEDED, "no longer true"),
    (ResolutionStatus.UNRESOLVED, "not sure which memory"),
    (ResolutionStatus.FAILED, "could not apply"),
])
def test_correction_notices(status, fragment):
    outcome = ResolutionOutcome(status=status, reason="")
    [notice] = _memory_notices(turn(correction=outcome))
    assert fragment in notice


def test_unresolved_correction_states_nothing_was_deleted():
    outcome = ResolutionOutcome(status=ResolutionStatus.UNRESOLVED, reason="")
    [notice] = _memory_notices(turn(correction=outcome))
    assert "did not delete anything" in notice


def test_notices_never_expose_internals():
    """No ids, no candidate text, no counts of internal records."""
    record = new_memory("I prefer Python for my projects", tags=["language"])
    notices = _memory_notices(turn(memories_created=[record],
                                   conflicts=[object()]))
    blob = " ".join(notices)
    assert record.id not in blob
    assert "I prefer Python" not in blob


def test_render_turn_shows_the_reply():
    lines = _render_turn(turn(reply=Message.assistant("Hello!")))
    assert lines[0] == f"{ASSISTANT_LABEL}Hello!"


def test_render_turn_shows_an_error_instead_of_a_reply():
    lines = _render_turn(turn(error="the model failed"))
    assert lines == ["[Error] the model failed"]
    assert not any(ASSISTANT_LABEL in line for line in lines)


# ===========================================================================
# Startup and sessions
# ===========================================================================

def test_new_session_displays_its_id(workspace, capsys):
    run_chat(workspace, ["/exit"])
    out = capsys.readouterr().out
    session_id = only_session(workspace[0])
    assert f"Session: {session_id}" in out


def test_resumed_session_is_labelled(workspace, capsys):
    run_chat(workspace, ["hello", "/exit"])
    session_id = only_session(workspace[0])
    capsys.readouterr()

    run_chat(workspace, ["/exit"], "--session", session_id)
    assert f"Resumed session: {session_id}" in capsys.readouterr().out


def test_unknown_session_is_refused(workspace, capsys):
    tmp_path, base = workspace
    assert main(base + ["chat", "--session", "conv_nope"]) == 2
    captured = capsys.readouterr()
    assert "no conversation with id 'conv_nope'" in captured.err
    assert "Traceback" not in captured.err
    # Nothing was fabricated.
    assert sessions_of(tmp_path).list_sessions() == []


def test_unknown_session_can_be_forced(workspace, capsys):
    run_chat(workspace, ["/exit"], "--session", "conv_mine", "--new")
    assert "Session: conv_mine" in capsys.readouterr().out


def test_session_survives_and_accumulates(workspace, capsys):
    run_chat(workspace, ["first question", "/exit"])
    session_id = only_session(workspace[0])
    assert len(sessions_of(workspace[0]).load(session_id).messages) == 2

    run_chat(workspace, ["second question", "/exit"], "--session", session_id)
    reloaded = sessions_of(workspace[0]).load(session_id)
    assert len(reloaded.messages) == 4
    assert reloaded.messages[2].content == "second question"


def test_one_agent_and_session_for_the_whole_loop(workspace, capsys):
    run_chat(workspace, ["one", "two", "three", "/exit"])
    assert len(sessions_of(workspace[0]).list_sessions()) == 1


# ===========================================================================
# Local commands
# ===========================================================================

@pytest.mark.parametrize("command", ["/exit", "/quit"])
def test_exit_commands(workspace, capsys, command):
    run_chat(workspace, [command])
    assert "Goodbye." in capsys.readouterr().out


def test_help_command(workspace, capsys):
    run_chat(workspace, ["/help", "/exit"])
    assert CHAT_HELP.splitlines()[0] in capsys.readouterr().out


def test_session_command(workspace, capsys):
    run_chat(workspace, ["/session", "/exit"])
    session_id = only_session(workspace[0])
    assert capsys.readouterr().out.count(f"Session: {session_id}") >= 2


def test_unknown_command_is_reported_not_sent(workspace, capsys):
    run_chat(workspace, ["/bogus", "/exit"])
    assert "Unknown command '/bogus'" in capsys.readouterr().out


def test_local_commands_are_not_persisted(workspace, capsys):
    run_chat(workspace, ["/help", "/session", "/bogus", "/exit"])
    session_id = only_session(workspace[0])
    stored = [m.content for m in sessions_of(workspace[0]).load(session_id
                                                                ).messages]
    assert stored == []


def test_local_commands_never_reach_the_model(workspace, capsys):
    import agentcore.models.providers.echo as echo_mod

    seen = []
    original = echo_mod.EchoProvider.generate

    def spy(self, request):
        seen.append("\n".join(m.content for m in request.messages))
        return original(self, request)

    echo_mod.EchoProvider.generate = spy
    try:
        run_chat(workspace, ["/help", "/session", "/bogus", "a real message",
                             "/exit"])
    finally:
        echo_mod.EchoProvider.generate = original

    # More than one call is legitimate: memory capture also consults the
    # model to extract candidates. What matters is that no local command
    # reached it in *any* call.
    assert seen, "the real message should have produced a model call"
    joined = "\n".join(seen)
    assert "a real message" in joined
    for command in ("/help", "/session", "/bogus", "/exit"):
        assert command not in joined


def test_command_prefix_is_the_only_escape():
    assert COMMAND_PREFIX == "/"


# ===========================================================================
# Blank input, EOF, interrupt
# ===========================================================================

def test_blank_input_is_ignored(workspace, capsys):
    run_chat(workspace, ["", "   ", "\t", "/exit"])
    session_id = only_session(workspace[0])
    assert sessions_of(workspace[0]).load(session_id).messages == []


def test_eof_exits_cleanly(workspace, capsys):
    tmp_path, base = workspace
    with mock.patch("builtins.input", side_effect=EOFError):
        assert main(base + ["chat"]) == 0
    assert "Goodbye." in capsys.readouterr().out


def test_keyboard_interrupt_exits_cleanly(workspace, capsys):
    tmp_path, base = workspace
    with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
        assert main(base + ["chat"]) == 0
    assert "Goodbye." in capsys.readouterr().out


def test_interrupt_preserves_earlier_turns(workspace, capsys):
    tmp_path, base = workspace
    with mock.patch("builtins.input",
                    side_effect=["a real question", KeyboardInterrupt]):
        assert main(base + ["chat"]) == 0
    session_id = only_session(tmp_path)
    assert len(sessions_of(tmp_path).load(session_id).messages) == 2


# ===========================================================================
# Conversation and memory behaviour
# ===========================================================================

def test_assistant_reply_is_printed(workspace, capsys):
    run_chat(workspace, ["hello there", "/exit"])
    assert ASSISTANT_LABEL in capsys.readouterr().out


def test_explicit_remember_reports_success(workspace, capsys):
    run_chat(workspace, ["Remember that I prefer Python for my projects.",
                         "/exit"])
    out = capsys.readouterr().out
    assert "[Memory] Remembered something new." in out
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    assert [m.content for m in memory.all_records()] == \
        ["I prefer Python for my projects"]


def test_temporary_statement_gets_no_memory_notice(workspace, capsys):
    run_chat(workspace, ["I am tired today.", "/exit"])
    out = capsys.readouterr().out
    assert "[Memory]" not in out
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    assert memory.count() == 0


def test_secret_is_neither_stored_nor_announced(workspace, capsys):
    run_chat(workspace, [f"Remember my API key is {SECRET}.", "/exit"])
    out = capsys.readouterr().out
    assert "[Memory] Remembered" not in out
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    assert memory.count() == 0


def test_conflict_is_reported_without_overwriting(workspace, capsys):
    run_chat(workspace, ["Remember that I prefer Python for my projects.",
                         "/exit"])
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    original = memory.all_records()[0]
    capsys.readouterr()

    session_id = only_session(workspace[0])
    run_chat(workspace, ["I have switched to Rust for new projects.", "/exit"],
             "--session", session_id)
    out = capsys.readouterr().out
    assert "conflicts with something I remembered earlier" in out

    reloaded = JsonFileMemoryStore(workspace[0] / "memory")
    assert reloaded.get(original.id).content == original.content
    assert reloaded.get(original.id).revision == 1


def test_ambiguous_forget_reports_a_refusal(workspace, capsys):
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    memory.add(new_memory("I prefer Python", tags=["language"]))
    memory.add(new_memory("My timezone is Europe/London", tags=["timezone"]))

    run_chat(workspace, ["Forget that.", "/exit"])
    assert "not sure which memory you meant" in capsys.readouterr().out
    assert JsonFileMemoryStore(workspace[0] / "memory").count() == 2


def test_targeted_forget_reports_deletion(workspace, capsys):
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    memory.add(new_memory("My timezone is Europe/London", tags=["timezone"]))

    run_chat(workspace, ["Forget my timezone.", "/exit"])
    assert "[Memory] Forgotten." in capsys.readouterr().out
    assert JsonFileMemoryStore(workspace[0] / "memory").count() == 0


def test_memory_disabled_still_converses(tmp_path, capsys):
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{tmp_path}"\n'
                    '[model]\nprovider = "echo"\nname = "echo"\n'
                    '[logging]\nlevel = "CRITICAL"\n'
                    '[features]\nmemory = false\n', encoding="utf-8")
    with mock.patch("builtins.input", side_effect=["Remember that I prefer "
                                                   "Python.", "/exit"]):
        assert main(["--config", str(path), "chat"]) == 0
    out = capsys.readouterr().out
    assert ASSISTANT_LABEL in out
    assert "[Memory]" not in out


# ===========================================================================
# Error handling
# ===========================================================================

def test_model_failure_is_reported_without_a_fabricated_reply(tmp_path,
                                                              capsys):
    """provider = "none" leaves the agent with no model at all."""
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{tmp_path}"\n'
                    '[logging]\nlevel = "CRITICAL"\n'
                    '[features]\nmemory = true\n', encoding="utf-8")
    with mock.patch("builtins.input", side_effect=["hello", "/exit"]):
        assert main(["--config", str(path), "chat"]) == 0
    out = capsys.readouterr().out
    assert "[Error]" in out
    assert "no model provider" in out
    assert ASSISTANT_LABEL not in out


def test_the_loop_survives_a_turn_error(tmp_path, capsys):
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{tmp_path}"\n'
                    '[logging]\nlevel = "CRITICAL"\n', encoding="utf-8")
    with mock.patch("builtins.input", side_effect=["one", "two", "/exit"]):
        assert main(["--config", str(path), "chat"]) == 0
    assert capsys.readouterr().out.count("[Error]") == 2


def test_an_unexpected_exception_shows_no_traceback(workspace, capsys,
                                                    monkeypatch):
    from agentcore.conversation.runtime import ConversationRuntime

    def boom(self, *args, **kwargs):
        raise RuntimeError("something broke")

    monkeypatch.setattr(ConversationRuntime, "send", boom)
    run_chat(workspace, ["hello", "/exit"])
    out = capsys.readouterr().out
    assert "[Error] RuntimeError: something broke" in out
    assert "Traceback" not in out


# ===========================================================================
# Security
# ===========================================================================

def test_assistant_text_cannot_trigger_local_commands(workspace, capsys):
    """A hostile reply containing /exit must not end the conversation."""
    import agentcore.models.providers.echo as echo_mod
    from agentcore.models.messages import ModelResponse

    original = echo_mod.EchoProvider.generate

    def hostile(self, request):
        return ModelResponse(text="/exit\n/quit\nrm -rf /\n!whoami")

    echo_mod.EchoProvider.generate = hostile
    try:
        run_chat(workspace, ["hello", "still here?", "/exit"])
    finally:
        echo_mod.EchoProvider.generate = original

    session_id = only_session(workspace[0])
    stored = sessions_of(workspace[0]).load(session_id).messages
    # Both user turns were processed: the loop did not obey the reply.
    assert sum(1 for m in stored if m.role is Role.USER) == 2


def test_no_shell_escape_exists(workspace, capsys):
    """`!command` is ordinary text, not an escape."""
    run_chat(workspace, ["!whoami", "/exit"])
    session_id = only_session(workspace[0])
    stored = [m.content for m in sessions_of(workspace[0]).load(session_id
                                                                ).messages]
    assert "!whoami" in stored


def test_system_prompt_is_not_displayed(workspace, capsys):
    run_chat(workspace, ["hello", "/exit"])
    out = capsys.readouterr().out
    assert "Trust hierarchy" not in out
    assert "RETRIEVED MEMORY" not in out


def test_retrieved_memory_is_not_displayed(workspace, capsys):
    memory = JsonFileMemoryStore(workspace[0] / "memory")
    memory.add(new_memory("I prefer Python for my projects", tags=["language"]))
    run_chat(workspace, ["What language do I prefer for my projects?",
                         "/exit"])
    out = capsys.readouterr().out
    assert "RECALLED FROM EARLIER SESSIONS" not in out


def test_secrets_do_not_reach_the_logs(workspace, capsys):
    configure_logging(LoggingConfig(level="DEBUG"))
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger("agentcore")
    for existing in root.handlers:
        handler.setFormatter(existing.formatter)
    root.addHandler(handler)
    try:
        run_chat(workspace, [f"Remember my API key is {SECRET}.", "/exit"])
    finally:
        root.removeHandler(handler)
        configure_logging(LoggingConfig(level="CRITICAL"))
    assert SECRET not in buffer.getvalue()


# ===========================================================================
# One-shot mode
# ===========================================================================

def test_one_shot_prints_a_reply(workspace, capsys):
    tmp_path, base = workspace
    assert main(base + ["chat", "--message", "hello there"]) == 0
    assert ASSISTANT_LABEL in capsys.readouterr().out


def test_one_shot_persists_the_session(workspace, capsys):
    tmp_path, base = workspace
    main(base + ["chat", "--message", "hello there"])
    session_id = only_session(tmp_path)
    assert len(sessions_of(tmp_path).load(session_id).messages) == 2


def test_one_shot_continues_an_existing_session(workspace, capsys):
    tmp_path, base = workspace
    main(base + ["chat", "--message", "first"])
    session_id = only_session(tmp_path)
    main(base + ["chat", "--session", session_id, "--message", "second"])
    assert len(sessions_of(tmp_path).load(session_id).messages) == 4


def test_one_shot_captures_memory(workspace, capsys):
    tmp_path, base = workspace
    main(base + ["chat", "--message",
                 "Remember that I prefer Python for my projects."])
    assert "[Memory] Remembered something new." in capsys.readouterr().out
    assert JsonFileMemoryStore(tmp_path / "memory").count() == 1


def test_one_shot_empty_message_is_refused(workspace, capsys):
    tmp_path, base = workspace
    assert main(base + ["chat", "--message", "   "]) == 2
    assert "was empty" in capsys.readouterr().err


def test_one_shot_json(workspace, capsys):
    tmp_path, base = workspace
    assert main(base + ["chat", "--message", "hello", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["reply"]
    assert payload["session_id"].startswith("conv_")
    assert payload["notices"] == []


def test_one_shot_json_reports_memory(workspace, capsys):
    tmp_path, base = workspace
    main(base + ["chat", "--message",
                 "Remember that I prefer Python for my projects.",
                 "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["memories_created"] == 1
    assert payload["notices"] == ["[Memory] Remembered something new."]


def test_one_shot_json_on_failure(tmp_path, capsys):
    path = tmp_path / "agent.toml"
    path.write_text(f'workspace_dir = "{tmp_path}"\n'
                    '[logging]\nlevel = "CRITICAL"\n', encoding="utf-8")
    assert main(["--config", str(path), "chat", "--message", "hi",
                 "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reply"] is None
    assert payload["error"]


def test_json_requires_a_message(workspace, capsys):
    tmp_path, base = workspace
    assert main(base + ["chat", "--json"]) == 2
    assert "--json requires --message" in capsys.readouterr().err


def test_turn_payload_shape():
    payload = _turn_payload(turn(reply=Message.assistant("hi")))
    assert set(payload) == {"session_id", "ok", "reply", "error",
                            "memories_created", "memories_updated",
                            "memories_used", "conflicts", "memory_error",
                            "correction", "notices"}


# ===========================================================================
# Cross-process persistence
# ===========================================================================

def test_conversation_survives_a_real_process_restart(workspace):
    tmp_path, base = workspace
    config_path = base[1]

    first = subprocess.run(
        [sys.executable, "-m", "agentcore.cli", "--config", config_path,
         "chat", "--message", "Remember that I prefer Python for my projects."],
        capture_output=True, text=True, check=True)
    assert "[Memory] Remembered something new." in first.stdout

    session_id = only_session(tmp_path)
    second = subprocess.run(
        [sys.executable, "-m", "agentcore.cli", "--config", config_path,
         "chat", "--session", session_id, "--message",
         "What language do I usually prefer for my projects?", "--json"],
        capture_output=True, text=True, check=True)
    payload = json.loads(second.stdout)
    assert payload["session_id"] == session_id
    # The memory persisted by the first process was retrieved by the second.
    assert payload["memories_used"] >= 1

    reloaded = sessions_of(tmp_path).load(session_id)
    assert len(reloaded.messages) == 4
