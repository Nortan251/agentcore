"""Minimal CLI for inspecting and smoke-testing the foundation.

    agentcore status
    agentcore tools
    agentcore run "objective"
    agentcore doctor [--json]
    agentcore runs
    agentcore history <run_id>
    agentcore memory list | show | correct | retire | forget
    agentcore chat [--session ID] [--message TEXT]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentcore import __version__
from agentcore.config.loader import load_config
from agentcore.config.schema import AgentConfig
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.errors import AgentError
from agentcore.fs.paths import WorkspaceMismatchError
from agentcore.integrations.base import IntegrationRegistry, IntegrationStatus
from agentcore.integrations.computer_control import ComputerControlAdapter
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger, redact
from agentcore.state.store import JsonFileStateStore

#: Subdirectory of the workspace where runs are persisted.
RUNS_SUBDIR = "runs"

#: Cap on any single rendered value, so history stays readable.
MAX_FIELD = 160

#: Cap on the objective column in the run listing.
MAX_OBJECTIVE = 56

#: Rows shown by `agentcore runs` unless --limit or --all says otherwise.
DEFAULT_RUN_LIMIT = 20

#: Printed by `agentcore inbox` when nothing awaits a human. Empty is a
#: successful, normal result — never an error.
INBOX_EMPTY = "No runs require operator attention."

#: Status shown for a run whose state file could not be read. It is not a real
#: outcome, so it is deliberately excluded from --status matching.
UNREADABLE_STATUS = "UNREADABLE"


def valid_statuses() -> frozenset[str]:
    """Every status `_run_status` can report, derived — never hard-coded.

    `_final_outcome` emits either one of its own verdict words or a
    ``TaskStatus`` name, so the vocabulary is the union of the two. Deriving it
    means a new task status is filterable the moment it exists.
    """
    from agentcore.tasks.models import TaskStatus

    derived = {status.value.upper() for status in TaskStatus}
    # Verdicts `_final_outcome` produces that are not TaskStatus members.
    derived.update({"COMPLETED", "FAILED", "BLOCKED", "ESCALATED", "UNKNOWN"})
    derived.add(UNREADABLE_STATUS)
    return frozenset(derived)


def _status_argument(value: str) -> str:
    """Validate --status against the derived vocabulary (case-insensitive)."""
    candidate = str(value).strip().upper()
    if candidate not in valid_statuses():
        raise argparse.ArgumentTypeError(
            f"unknown status '{value}'. Valid statuses: "
            + ", ".join(sorted(valid_statuses())))
    return candidate


def _limit_argument(value: str) -> int:
    """Validate --limit as a positive integer."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--limit must be an integer, got '{value}'") from None
    if number < 1:
        raise argparse.ArgumentTypeError(
            f"--limit must be at least 1, got {number}")
    return number

#: Markers that make a free-text field look like it carries a credential.
#: Derived from the logging redaction key list so there is one source of
#: truth — ``redact()`` itself only masks *mapping keys*, which cannot help a
#: plain string such as an objective.
def _secret_markers() -> tuple[str, ...]:
    from agentcore.config.schema import LoggingConfig

    return tuple(k.lower() for k in LoggingConfig().redact_keys)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentcore",
                                     description="Agent foundation CLI")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", help="path to a TOML config file")
    parser.add_argument("--runs-dir",
                        help="directory holding persisted run state "
                             "(default: <workspace_dir>/runs)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show agent status")
    sub.add_parser("tools", help="list registered tools")
    doctor = sub.add_parser("doctor", help="check wiring and integrations")
    doctor.add_argument("--json", action="store_true", dest="as_json",
                        help="emit bounded, versioned diagnostic checks as JSON")
    runs = sub.add_parser("runs", help="summarise persisted runs")
    runs.add_argument("--json", action="store_true", dest="as_json",
                      help="emit the run summaries as JSON")
    runs.add_argument("--status", type=_status_argument, metavar="STATUS",
                      help="only show runs with this status "
                           f"({', '.join(sorted(valid_statuses()))}); "
                           "case-insensitive. Unreadable runs never match.")
    runs.add_argument("--decision", type=_decision_argument,
                      metavar="DECISION",
                      help="only show runs with this operator decision "
                           f"({DECISION_APPROVED}, {DECISION_REJECTED}, "
                           f"{PENDING_DECISION} = awaiting a human, "
                           f"{NO_DECISION_ALIAS} = no decision recorded); "
                           "case-insensitive")
    # --limit and --all both control how many rows are shown, so asking for
    # both is contradictory rather than a preference; argparse rejects it.
    size = runs.add_mutually_exclusive_group()
    size.add_argument("--limit", type=_limit_argument, metavar="N",
                      help=f"show at most N runs (default {DEFAULT_RUN_LIMIT})")
    size.add_argument("--all", action="store_true", dest="show_all",
                      help="show every matching run, ignoring the default cap")
    inbox = sub.add_parser(
        "inbox",
        help="show runs with unresolved pending actions "
             "(i.e. runs awaiting an operator decision), or drill into one "
             "run with `inbox RUN_ID`")
    inbox.add_argument("run_id", nargs="?",
                       help="show only this run, with its action details "
                            "(implies --action-details)")
    inbox.add_argument("--json", action="store_true", dest="as_json",
                       help="emit the run summaries as JSON")
    inbox.add_argument("--action-details", action="store_true",
                       dest="action_details",
                       help="also show each unresolved action's id, tool, "
                            "description and reason, so it can be approved "
                            "or rejected without a second command")
    # Same size options, same semantics as `runs`.
    inbox_size = inbox.add_mutually_exclusive_group()
    inbox_size.add_argument("--limit", type=_limit_argument, metavar="N",
                            help="show at most N runs "
                                 f"(default {DEFAULT_RUN_LIMIT}); "
                                 "not valid with RUN_ID")
    inbox_size.add_argument("--all", action="store_true", dest="show_all",
                            help="show every waiting run, ignoring the "
                                 "default cap; not valid with RUN_ID")
    memory = sub.add_parser(
        "memory", help="inspect and correct persistent memory")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)

    mem_list = memory_sub.add_parser("list", help="list stored memories")
    mem_list.add_argument("--query", metavar="TEXT",
                          help="only show memories matching this text")
    mem_list.add_argument("--all", action="store_true", dest="show_all",
                          help="include retired/expired memories")
    mem_list.add_argument("--limit", type=_limit_argument, metavar="N",
                          help=f"show at most N (default {DEFAULT_RUN_LIMIT})")
    mem_list.add_argument("--json", action="store_true", dest="as_json",
                          help="emit the memories as JSON")

    mem_show = memory_sub.add_parser("show", help="show one memory in full")
    mem_show.add_argument("memory_id")
    mem_show.add_argument("--json", action="store_true", dest="as_json",
                          help="emit the memory as JSON")

    mem_forget = memory_sub.add_parser(
        "forget", help="delete one memory, by id")
    mem_forget.add_argument("memory_id")
    mem_forget.add_argument("--reason", default="", metavar="TEXT",
                            help="why it is being deleted (recorded in logs)")
    mem_forget.add_argument("--yes", action="store_true", dest="confirmed",
                            help="confirm the deletion (required)")
    mem_forget.add_argument("--json", action="store_true", dest="as_json",
                            help="emit the result as JSON")

    mem_correct = memory_sub.add_parser(
        "correct", help="replace a memory's content, preserving its history")
    mem_correct.add_argument("memory_id")
    mem_correct.add_argument("content", help="the corrected statement")
    mem_correct.add_argument("--json", action="store_true", dest="as_json",
                             help="emit the result as JSON")

    mem_conflicts = memory_sub.add_parser(
        "conflicts",
        help="list unresolved memory conflicts, or resolve one")
    mem_conflicts.add_argument("--all", action="store_true", dest="show_all",
                               help="include already-resolved conflicts")
    mem_conflicts.add_argument("--limit", type=_limit_argument, metavar="N",
                               help="show at most N "
                                    f"(default {DEFAULT_RUN_LIMIT})")
    mem_conflicts.add_argument("--resolve", metavar="CONFLICT_ID",
                               dest="resolve_id",
                               help="settle this conflict (requires one of "
                                    "--supersede/--keep-existing/--discard)")
    choice = mem_conflicts.add_mutually_exclusive_group()
    choice.add_argument("--supersede", action="store_true",
                        help="the candidate is current; retire the related "
                             "memories")
    choice.add_argument("--keep-existing", action="store_true",
                        dest="keep_existing",
                        help="the stored memory stands; drop the candidate")
    choice.add_argument("--discard", action="store_true",
                        help="the candidate was wrong; change nothing")
    mem_conflicts.add_argument("--operator", default="operator",
                               metavar="NAME",
                               help="who is resolving it (default: operator)")
    mem_conflicts.add_argument("--yes", action="store_true", dest="confirmed",
                               help="confirm --supersede (required: it "
                                    "retires existing memories)")
    mem_conflicts.add_argument("--json", action="store_true", dest="as_json",
                               help="emit the result as JSON")

    mem_retire = memory_sub.add_parser(
        "retire", help="mark a memory no longer true, keeping it for audit")
    mem_retire.add_argument("memory_id")
    mem_retire.add_argument("--json", action="store_true", dest="as_json",
                            help="emit the result as JSON")

    chat = sub.add_parser(
        "chat", help="talk to the assistant, with persistent memory")
    chat.add_argument("--session", metavar="ID", dest="session_id",
                      help="continue an existing conversation")
    chat.add_argument("--message", metavar="TEXT",
                      help="send one message and exit, instead of an "
                           "interactive session")
    chat.add_argument("--new", action="store_true", dest="force_new",
                      help="with --session, start a new conversation under "
                           "that id instead of requiring it to exist")
    chat.add_argument("--json", action="store_true", dest="as_json",
                      help="emit the turn as JSON (requires --message)")
    chat.add_argument("--enable-desktop", action="store_true",
                      help="enable configured Windows 11 Notepad and foreground observation tools for this chat")
    chat.add_argument("--desktop-evidence", type=Path, metavar="FILE",
                      help="save closed Notepad evidence for one interactive CONFIRM turn (no --json)")

    workspace = sub.add_parser("workspace", help="one explicitly enabled create-only workspace turn")
    workspace.add_argument("--enable-write", action="store_true",
                           help="opt in for this invocation; requires AUTONOMOUS FS_READ + FS_WRITE")
    workspace.add_argument("--message", required=True, metavar="TEXT")
    workspace.add_argument("--session", dest="session_id", metavar="ID")
    workspace.add_argument("--json", action="store_true", dest="as_json")

    workflow = sub.add_parser("workflow", help="bounded supervised workspace inspect/create/verify")
    workflow.add_argument("--enable-write", action="store_true", help="current CONFIRM write opt-in")
    workflow.add_argument("--message", metavar="TEXT")
    workflow.add_argument("--input", action="append", default=[], dest="inputs", metavar="PATH")
    workflow.add_argument("--output", metavar="PATH")
    workflow.add_argument("--max-bytes", type=int, default=4096)
    workflow.add_argument("--max-seconds", type=int, default=120)
    workflow.add_argument("--resume", metavar="WORKFLOW_ID", help="reconcile and explicitly verify; never write")
    workflow.add_argument("--json", action="store_true", dest="as_json")

    browser = sub.add_parser("browser", help="read explicitly granted web pages")
    browser.add_argument("--allow-url", action="append", required=True,
                         metavar="URL", help="grant one exact URL for this turn; repeatable")
    browser.add_argument("--session", metavar="ID", dest="session_id",
                         help="resume a browser-only conversation")
    browser.add_argument("--json", action="store_true", dest="as_json",
                         help="emit a bounded browser turn summary")
    browser.add_argument("prompt", help="model input; never navigation authority")

    run = sub.add_parser("run", help="plan and run an objective, "
                                     "or continue a persisted run")
    run.add_argument("objective", nargs="?",
                     help="the objective to plan and execute "
                          "(omit when using --resume)")
    run.add_argument("--resume", metavar="RUN_ID", dest="resume_run_id",
                     help="continue a previously persisted run instead of "
                          "starting a new one")
    run.add_argument("--json", action="store_true", dest="as_json",
                     help="emit the result as JSON")
    approve = sub.add_parser(
        "approve",
        help="record an operator decision approving a pending action, "
             "making an escalated run resumable")
    approve.add_argument("run_id")
    approve.add_argument("--action-id", dest="action_id", metavar="ID",
                         help="which pending action to approve (required "
                              "when a run has more than one)")
    approve.add_argument("--operator", default="operator", metavar="NAME",
                         help="who is recording the decision "
                              "(default: operator)")
    approve.add_argument("--note", default="", metavar="TEXT",
                         help="optional reason recorded with the decision")
    approve.add_argument("--json", action="store_true", dest="as_json",
                         help="emit the decision as JSON")
    reject = sub.add_parser(
        "reject",
        help="record an operator decision refusing a pending action, "
             "closing the run so it cannot be resumed")
    reject.add_argument("run_id")
    reject.add_argument("--action-id", dest="action_id", metavar="ID",
                        help="which pending action to reject (required "
                             "when a run has more than one)")
    reject.add_argument("--operator", default="operator", metavar="NAME",
                        help="who is recording the decision "
                             "(default: operator)")
    reject.add_argument("--reason", default="", metavar="TEXT", dest="note",
                        help="why the action was refused "
                             "(default: operator rejected action)")
    reject.add_argument("--json", action="store_true", dest="as_json",
                        help="emit the decision as JSON")
    history = sub.add_parser("history",
                             help="show the timeline of a persisted run")
    history.add_argument("run_id")
    history.add_argument("--json", action="store_true", dest="as_json",
                         help="emit the timeline as JSON")
    return parser


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _quiet_logging() -> None:
    """Silence framework logging for read-only report commands.

    Loading state emits informational log lines; these commands write a report
    to stdout, so the noise would corrupt both the listing and ``--json``.
    """
    from agentcore.config.schema import LoggingConfig
    from agentcore.observability.logging import configure_logging

    configure_logging(LoggingConfig(level="CRITICAL"))


def _runs_dir(config: AgentConfig, override: str | None) -> Path:
    """Where persisted runs live. Explicit flag wins over the workspace."""
    if override:
        return Path(override)
    return Path(config.workspace_dir) / RUNS_SUBDIR


# ---------------------------------------------------------------------------
# History rendering (read-only)
# ---------------------------------------------------------------------------

def _clip(value: Any, limit: int = MAX_FIELD) -> str:
    """Collapse whitespace and truncate; never returns None."""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _safe_args(args: Any) -> str:
    """Render tool arguments through the project's existing redaction.

    Raw arguments can hold credentials, so they always pass through
    ``observability.logging.redact`` — the same rules logging uses — before
    being shown.
    """
    if not isinstance(args, Mapping) or not args:
        return ""
    try:
        return _clip(json.dumps(redact(dict(args)), default=str, sort_keys=True))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return "<unrenderable arguments>"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class _Event:
    """One timeline entry, ordered by its recorded timestamp when present."""

    __slots__ = ("at", "order", "kind", "title", "lines")

    def __init__(self, at: datetime | None, order: int, kind: str,
                 title: str, lines: list[str]) -> None:
        self.at = at
        self.order = order
        self.kind = kind
        self.title = title
        self.lines = lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat() if self.at else None,
            "title": self.title,
            "details": list(self.lines),
        }


def _history_events(state: Any) -> list[_Event]:
    """Events recorded on the append-only ``AgentState.history`` log."""
    events: list[_Event] = []
    for index, entry in enumerate(getattr(state, "history", []) or []):
        kind = getattr(entry, "kind", "") or "event"
        data = getattr(entry, "data", {}) or {}
        summary = _clip(getattr(entry, "summary", ""))
        lines: list[str] = []
        title = summary

        if kind == "transition":
            title = f"phase {data.get('from')} -> {data.get('to')}"
            note = _clip(data.get("note"))
            if note:
                lines.append(f"note: {note}")
        elif kind == "tool_call":
            tool = data.get("tool")
            result = data.get("result") if isinstance(
                data.get("result"), Mapping) else {}
            ok = result.get("ok")
            title = f"tool '{tool}' -> {'ok' if ok else 'failed'}"
            args = _safe_args(data.get("args"))
            if args:
                lines.append(f"args: {args}")
            if ok and result.get("output") is not None:
                lines.append(f"output: {_clip(result.get('output'))}")
            if not ok and result.get("error"):
                lines.append(f"error: {_clip(result.get('error'))}")
            duration = data.get("duration_ms")
            if duration is not None:
                lines.append(f"duration_ms: {duration}")
        elif kind == "verification":
            status = str(data.get("status", "")).upper()
            title = f"verification {status or 'UNKNOWN'}"
            if data.get("checked_by"):
                lines.append(f"checked by: {_clip(data.get('checked_by'))}")
            if data.get("detail"):
                lines.append(f"reason: {_clip(data.get('detail'))}")
        elif kind == "error":
            title = f"error {_clip(data.get('type'))}"
            if data.get("message"):
                lines.append(f"message: {_clip(data.get('message'))}")
        elif kind == "pending_action":
            title = "escalation / pending action"
            lines.append(f"needs: {summary}")
            if data.get("reason"):
                lines.append(f"reason: {_clip(data.get('reason'))}")
        elif kind == "operator_decision":
            decision = str(data.get("decision", "")).upper() or "UNKNOWN"
            title = f"operator decision: {decision}"
            if data.get("operator"):
                lines.append(f"by: {_safe_text(data.get('operator'), 32, label='operator')}")
            if data.get("action_id"):
                lines.append(f"action: {data['action_id']}")
            if data.get("note"):
                # Operator free text: sanitise like any other supplied string.
                lines.append(f"reason: {_safe_text(data['note'], MAX_FIELD, label='reason')}")

        subtask_id = getattr(entry, "subtask_id", None)
        if subtask_id and kind in {"tool_call", "verification", "error"}:
            lines.append(f"subtask: {subtask_id}")

        events.append(_Event(_parse_time(
            getattr(entry, "at", None).isoformat()
            if isinstance(getattr(entry, "at", None), datetime) else None),
            index, kind, title, lines))
    return events


def _recovery_events(state: Any, base: int) -> list[_Event]:
    """Recovery decisions, read from the persisted recovery history."""
    events: list[_Event] = []
    try:
        from agentcore.recovery.history import RecoveryHistory

        records = RecoveryHistory.load(state).to_dict()
    except Exception:
        return events

    order = base
    for subtask_id, record in (records or {}).items():
        if not isinstance(record, Mapping):
            continue
        for decision in record.get("decisions", []) or []:
            if not isinstance(decision, Mapping):
                continue
            action = str(decision.get("action", "")).upper() or "UNKNOWN"
            lines = [f"category: {decision.get('category')}",
                     f"retry safety: {decision.get('safety')}"]
            if decision.get("attempt"):
                lines.append(f"attempt: {decision.get('attempt')}")
            if decision.get("reason"):
                lines.append(f"reason: {_clip(decision.get('reason'))}")
            lines.append(f"subtask: {subtask_id}")
            events.append(_Event(_parse_time(decision.get("at")), order,
                                 "recovery", f"recovery decision: {action}",
                                 lines))
            order += 1
    return events


def _replan_events(state: Any, base: int) -> list[_Event]:
    """Replan cycles, including proposals refused before execution."""
    events: list[_Event] = []
    try:
        from agentcore.core.replan import ReplanLedger

        ledger = ReplanLedger.load(state)
    except Exception:
        return events

    for offset, record in enumerate(ledger.records):
        outcome = str(getattr(record, "outcome", "")) or "unknown"
        attempt = getattr(record, "attempt", 0)
        lines: list[str] = []
        if getattr(record, "signature", ""):
            lines.append(f"proposed strategy: {_clip(record.signature)}")

        if outcome.startswith("rejected"):
            kind = "rejected_proposal"
            label = ("duplicate of an already-failed strategy"
                     if outcome == "rejected_duplicate" else "invalid proposal")
            title = f"rejected proposal #{attempt} ({label})"
            # The single most important fact about a rejection.
            lines.append("EXECUTED: NO — refused before execution")
        elif outcome == "error":
            kind = "replan"
            title = f"replan #{attempt} failed"
        else:
            kind = "replan"
            title = f"replan #{attempt} accepted"
            if getattr(record, "new_task_id", None):
                lines.append(f"new plan: {record.new_task_id}")

        if getattr(record, "reason", ""):
            lines.append(f"replan reason: {_clip(record.reason)}")
        if getattr(record, "detail", ""):
            key = "rejection reason" if outcome.startswith("rejected") \
                else "detail"
            lines.append(f"{key}: {_clip(record.detail)}")

        events.append(_Event(_parse_time(getattr(record, "at", None)),
                             base + offset, kind, title, lines))
    return events


def build_timeline(state: Any) -> list[_Event]:
    """Merge every persisted event source into one chronological timeline.

    Ordering uses the timestamps already stored on each record. Entries with
    no usable timestamp keep their recorded insertion order and sort after
    timestamped ones, rather than being given an invented time.
    """
    events = _history_events(state)
    events += _recovery_events(state, len(events))
    events += _replan_events(state, len(events) * 2)
    events.sort(key=lambda e: (e.at is None, e.at or datetime.min, e.order))
    return events


def _plan_summary(state: Any) -> list[str]:
    """Describe the plan currently attached to the run."""
    task = getattr(state, "current_task", None)
    if task is None:
        return ["(no plan recorded)"]
    lines = [f"task: {task.id} ({task.status})"]
    for index, sub in enumerate(task.subtasks, start=1):
        bits = [f"  {index}. {_clip(sub.description)}"]
        bits.append(f"tool={sub.tool_name or '(none)'}")
        bits.append(f"status={sub.status}")
        verification = getattr(sub, "verification", None)
        if verification is not None and str(verification.status) != "unverified":
            bits.append(f"verification={str(verification.status).upper()}")
        lines.append(" ".join(bits))
    return lines


def _run_header(state: Any) -> dict[str, Any]:
    task = getattr(state, "current_task", None)
    return {
        "run_id": getattr(state, "run_id", ""),
        "objective": task.objective if task else "(none recorded)",
        "task_status": str(task.status) if task else "(none)",
        "phase": str(getattr(state, "phase", "")),
        "started": _clip(getattr(state, "created_at", "")),
        "last_updated": _clip(getattr(state, "updated_at", "")),
        "replans": _replan_count(state),
        "errors": len(getattr(state, "errors", []) or []),
        "pending_actions": len(getattr(state, "pending_actions", []) or []),
    }


def _replan_count(state: Any) -> int:
    try:
        from agentcore.core.replan import ReplanLedger

        return ReplanLedger.load(state).count
    except Exception:  # pragma: no cover - defensive
        return 0


def _final_outcome(state: Any) -> str:
    """Honest one-line verdict for the run."""
    task = getattr(state, "current_task", None)
    phase = str(getattr(state, "phase", ""))
    pending = getattr(state, "pending_actions", []) or []
    status = str(task.status) if task else "unknown"

    if status == "completed":
        return "COMPLETED — the task finished and its work was accepted"
    if pending:
        return (f"ESCALATED — {len(pending)} pending action(s) need a human; "
                f"task status '{status}'")
    if status == "failed":
        return "FAILED — the task did not succeed"
    if status == "blocked":
        return "BLOCKED — the run stopped awaiting a decision or new approach"
    return f"{status.upper()} — phase '{phase}'"


def render_history(state: Any) -> str:
    """Human-readable run history. Purely a view; never mutates state."""
    header = _run_header(state)
    out: list[str] = [
        f"Run:        {header['run_id']}",
        f"Objective:  {header['objective']}",
        f"Status:     {header['task_status']}   (phase: {header['phase']})",
        f"Started:    {header['started']}",
        f"Updated:    {header['last_updated']}",
        f"Replans:    {header['replans']}",
        "",
        "PLAN",
    ]
    out.extend(f"  {line}" for line in _plan_summary(state))
    out.append("")
    out.append("TIMELINE")

    events = build_timeline(state)
    if not events:
        out.append("  (no events recorded)")
    undated = any(event.at is None for event in events)
    for index, event in enumerate(events, start=1):
        stamp = event.at.isoformat() if event.at else "(no timestamp)"
        out.append(f"  {index:02d}. [{event.kind}] {event.title}")
        out.append(f"      at: {stamp}")
        out.extend(f"      {line}" for line in event.lines)
    if undated:
        out.append("")
        out.append("  note: entries marked '(no timestamp)' were not "
                   "timestamped when recorded; they appear in recorded order.")

    out.append("")
    out.append("FINAL")
    out.append(f"  {_final_outcome(state)}")
    return "\n".join(out)


def history_payload(state: Any) -> dict[str, Any]:
    """Machine-readable equivalent of :func:`render_history`."""
    return {
        **_run_header(state),
        "plan": _plan_summary(state),
        "timeline": [event.to_dict() for event in build_timeline(state)],
        "final": _final_outcome(state),
    }


#: Shown in place of a field a pending action does not actually carry. Never
#: a guess — the absence of a description is reported, not invented.
NOT_AVAILABLE = "(not available)"

#: Shown for an action whose id was not persisted. ``PendingAction.from_dict``
#: mints a fresh id for such a record on *every* load, so the id seen here
#: would not resolve in a later ``approve``/``reject`` process. Displaying it
#: would be a fabrication, so it is withheld and named as unusable.
NO_ACTION_ID = "(no id recorded)"

#: Cap on the free-text action fields in the expanded inbox listing.
MAX_ACTION_TEXT = 100


def _action_id(action: Any) -> str:
    """The action's id rendered exactly as ``--action-id`` must receive it.

    Deliberately *not* passed through :func:`_safe_text`: an id is an opaque
    generated token (``act_<hex>``), not free text, and truncating or
    withholding it would break the copy-into-``--action-id`` contract. It is
    only coerced to ``str``, because a hand-written legacy document can carry
    a non-string id.
    """
    return str(getattr(action, "id", "") or "")


def _action_details(state: Any, raw_actions: Any) -> list[dict[str, Any]]:
    """Safe, display-ready details for each unresolved action of one run.

    Reads the live ``state.pending_actions`` collection — the same and only
    source ``approve``/``reject`` resolve against — so what is shown is exactly
    what can be acted on. Order is that collection's own append order, which
    ``to_dict``/``from_dict`` round-trip preserves.

    ``raw_actions`` is the *unparsed* list from the state document, used solely
    to tell a persisted id from one ``PendingAction.from_dict`` invented for a
    record that had none. Such an id differs on every load, so it is reported
    as :data:`NO_ACTION_ID` rather than printed as if it were usable — the same
    care ``_run_summary`` already takes with invented timestamps.

    ``tool_args`` is never read here: raw arguments can carry credentials, and
    an operator deciding whether to allow an action needs to know *what* is
    proposed, not the values it would run with. ``history`` shows them redacted.
    """
    raw = raw_actions if isinstance(raw_actions, list) else []
    details: list[dict[str, Any]] = []

    for index, action in enumerate(getattr(state, "pending_actions", []) or []):
        entry = raw[index] if index < len(raw) and isinstance(
            raw[index], Mapping) else {}
        persisted_id = entry.get("id") if entry else None
        identifier = _action_id(action)
        details.append({
            # Verbatim so it can be pasted straight into --action-id, or None
            # when the stored record genuinely had none.
            "id": identifier if (persisted_id or not entry) else None,
            "tool_name": _safe_text(action.tool_name, MAX_ACTION_TEXT,
                                    label="tool name")
            if getattr(action, "tool_name", None) else None,
            "description": _safe_text(getattr(action, "description", ""),
                                      MAX_ACTION_TEXT, label="description")
            or None,
            "reason": _safe_text(getattr(action, "reason", ""),
                                 MAX_ACTION_TEXT, label="reason") or None,
            "subtask_id": _safe_text(getattr(action, "subtask_id", ""), 48,
                                     label="subtask id") or None,
        })
    return details


def render_action_details(actions: Sequence[Mapping[str, Any]],
                          indent: str = "    ") -> list[str]:
    """Indented block describing each unresolved action beneath its run.

    A concise operational summary — enough to decide and to copy an id — not a
    replay of the run. Prompts, model output, tool arguments and unrelated
    history stay in ``agentcore history``, which remains authoritative.
    """
    lines: list[str] = []
    for position, action in enumerate(actions, start=1):
        identifier = action.get("id") or NO_ACTION_ID
        lines.append(f"{indent}ACTION {position}: {identifier}")
        lines.append(f"{indent}    Tool:        "
                     f"{action.get('tool_name') or NOT_AVAILABLE}")
        lines.append(f"{indent}    Description: "
                     f"{action.get('description') or NOT_AVAILABLE}")
        lines.append(f"{indent}    Reason:      "
                     f"{action.get('reason') or NOT_AVAILABLE}")
        if action.get("subtask_id"):
            lines.append(f"{indent}    Subtask:     {action['subtask_id']}")
        if not action.get("id"):
            lines.append(f"{indent}    note: no id was persisted for this "
                         f"action; inspect it with `agentcore history`.")
    return lines


def _resolve_pending_action(state: Any, action_id: str | None, *,
                            verb: str = "approve"):
    """Pick the pending action to decide on, or explain why we cannot.

    Returns ``(action, error_lines)`` — exactly one is meaningful. Multiple
    pending actions can coexist, so an ambiguous request is refused and the
    choices are listed rather than guessing (or deciding on all of them).

    An action that has already been approved or rejected is no longer pending,
    so it simply is not found here — which is what makes repeated decisions
    fail cleanly without touching history.
    """
    pending = list(getattr(state, "pending_actions", []) or [])
    if not pending:
        return None, [f"this run has no pending action to {verb}"]

    if action_id:
        for action in pending:
            # A legacy document can carry a non-string id, so compare on the
            # rendered form — the same one `inbox --action-details` prints.
            if _action_id(action) == action_id:
                return action, []
        return None, [
            f"run has no pending action with id '{action_id}'",
            "pending actions: " + ", ".join(_action_id(a) for a in pending),
        ]

    if len(pending) > 1:
        lines = [f"run has {len(pending)} pending actions; "
                 "choose one with --action-id:"]
        lines += [f"  {_action_id(a)}  {_safe_text(a.description, 70)}"
                  for a in pending]
        return None, lines

    return pending[0], []


def _make_resumable(state: Any) -> bool:
    """Move an escalated run back to a state the runner will accept.

    Uses the existing transition API rather than assigning statuses directly,
    so the state machine still validates every step. ``FAILED -> PENDING`` and
    ``ERROR -> RECOVERING`` are already legal transitions — approval does not
    widen the state machine, it just takes a path that already exists.

    Returns True when the run is resumable afterwards.
    """
    from agentcore.state.models import AgentPhase
    from agentcore.tasks.models import TaskStatus, can_transition

    task = getattr(state, "current_task", None)
    if task is None:
        return False

    # Re-open the specific subtask that was blocked, then the task itself.
    for subtask in task.subtasks:
        if subtask.status is TaskStatus.FAILED and can_transition(
                subtask.status, TaskStatus.PENDING):
            subtask.transition_to(TaskStatus.PENDING)

    if task.status.is_terminal:
        if not can_transition(task.status, TaskStatus.PENDING):
            return False
        task.transition_to(TaskStatus.PENDING)

    # Phase: ERROR -> RECOVERING marks that a human intervened.
    if state.phase is AgentPhase.ERROR:
        state.transition_phase(AgentPhase.RECOVERING,
                               note="operator approved a pending action")
    return not task.status.is_terminal


def _make_terminal(state: Any) -> bool:
    """Close a run so the rejected action can never be continued.

    Uses only transitions the state machine already permits — rejection does
    not widen it. The two escalation shapes differ:

    * a ``FAILED`` task (permission-denied escalation) is already terminal and
      ``FAILED -> CANCELLED`` is *not* legal, so it is left as it stands;
      ``run --resume`` already refuses a terminal task.
    * a ``BLOCKED`` task is still resumable, so it is moved to ``CANCELLED`` —
      the only status with no outgoing transitions, which is exactly what
      "a human refused continuation" means.

    Returns True when the run is terminal (i.e. resume is now impossible).
    """
    from agentcore.tasks.models import TaskStatus, can_transition

    task = getattr(state, "current_task", None)
    if task is None:
        return False

    if not task.status.is_terminal and can_transition(task.status,
                                                      TaskStatus.CANCELLED):
        # Close the specific subtasks that were awaiting a decision first.
        for subtask in task.subtasks:
            if not subtask.status.is_terminal and can_transition(
                    subtask.status, TaskStatus.CANCELLED):
                subtask.transition_to(TaskStatus.CANCELLED)
        task.transition_to(TaskStatus.CANCELLED)

    return task.status.is_terminal


def _command_decide(args: argparse.Namespace, config: AgentConfig, *,
                    decision: str) -> int:
    """Record an operator decision about a pending action.

    Shared by ``approve`` and ``reject`` so the two stay symmetrical: identical
    preflight, identical action resolution, identical audit record, identical
    output shape. Only the resulting state transition differs — approval
    reopens the run, rejection closes it.

    Neither ever executes the action or touches the permission policy.
    """
    approving = decision == "APPROVED"
    verb = "approve" if approving else "reject"
    run_id = args.run_id
    directory = _runs_dir(config, args.runs_dir)
    if not directory.exists():
        print(f"no run directory at {directory}", file=sys.stderr)
        return 2

    store = JsonFileStateStore(directory)
    _quiet_logging()

    # --- preflight: strictly read-only -----------------------------------
    # No Agent is constructed and nothing is written until every check passes.
    try:
        state = store.load(run_id)
    except AgentError as exc:
        print(f"cannot {verb} run '{run_id}': {exc}", file=sys.stderr)
        available = store.list_runs()
        if available:
            print("available runs: " + ", ".join(available[:20]),
                  file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback
        print(f"cannot {verb} run '{run_id}': {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if getattr(state, "current_task", None) is None:
        print(f"run '{run_id}' has no task to {verb} against", file=sys.stderr)
        return 2

    action, problems = _resolve_pending_action(state, args.action_id,
                                               verb=verb)
    if action is None:
        print(f"cannot {verb} run '{run_id}':", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        if "no pending action" in " ".join(problems):
            print(f"inspect it with: agentcore history {run_id}",
                  file=sys.stderr)
        return 2

    # --- decision accepted: record, clear, transition --------------------
    note = getattr(args, "note", "") or ""
    if not approving and not note:
        # An honest default: state that a human refused, and claim nothing
        # more specific than that.
        note = "operator rejected action"
    # Free text from the operator, so sanitise and bound it like any other.
    note = _safe_text(note, MAX_FIELD, label="reason")

    state.record_decision(action, decision=decision,
                          operator=args.operator, note=note)
    state.clear_pending_action(action.id)
    if approving:
        resumable = _make_resumable(state)
    else:
        resumable = not _make_terminal(state)
    store.save(state)

    get_logger("cli").info(
        Events.APPROVAL_GRANTED if approving else Events.APPROVAL_REJECTED,
        run_id=run_id, action_id=action.id, operator=args.operator)

    payload = {
        "run_id": run_id,
        "action_id": action.id,
        # Description is operator-facing text; sanitise it like any free text.
        "action": _safe_text(action.description, MAX_FIELD,
                             label="action description"),
        "decision": decision,
        "operator": args.operator,
        "reason": note,
        "resumable": resumable,
        "task_status": str(state.current_task.status),
        "phase": str(state.phase),
        "remaining_pending_actions": len(state.pending_actions),
    }
    if args.as_json:
        _emit(payload)
        return 0

    lines = [
        f"Run:      {payload['run_id']}",
        f"Action:   {payload['action_id']}  {payload['action']}",
        f"Decision: {payload['decision']} (recorded by "
        f"{payload['operator']})",
    ]
    if not approving:
        lines.append(f"Reason:   {payload['reason']}")
    lines.append(
        f"State:    {'RESUMABLE' if resumable else 'TERMINAL'} "
        f"(task {payload['task_status']}, phase {payload['phase']})")
    if payload["remaining_pending_actions"]:
        lines.append(f"Note:     {payload['remaining_pending_actions']} "
                     f"pending action(s) still awaiting a decision")
    if resumable:
        lines.append(f"Next:     agentcore run --resume {run_id}")
    else:
        lines.append("Resume:   NOT ALLOWED")
    print("\n".join(lines))
    return 0


def _command_approve(args: argparse.Namespace, config: AgentConfig) -> int:
    """Record an operator approval and make an escalated run resumable.

    This command never executes the approved action. It records the human
    decision, clears the pending action and hands the run back to the normal
    engine via ``agentcore run --resume``, where the permission policy still
    evaluates the action on its own terms.
    """
    return _command_decide(args, config, decision="APPROVED")


def _command_reject(args: argparse.Namespace, config: AgentConfig) -> int:
    """Record an operator refusal and close the run.

    The mirror image of approval: the action is cleared and audited, but the
    run is left terminal so ``run --resume`` cannot continue it. The rejected
    action is never executed.
    """
    return _command_decide(args, config, decision="REJECTED")


def _command_history(args: argparse.Namespace, config: AgentConfig) -> int:
    directory = _runs_dir(config, args.runs_dir)
    if not directory.exists():
        print(f"no run directory at {directory}", file=sys.stderr)
        return 2

    store = JsonFileStateStore(directory)
    try:
        _quiet_logging()
        state = store.load(args.run_id)
    except AgentError as exc:
        print(f"cannot read run '{args.run_id}': {exc}", file=sys.stderr)
        available = store.list_runs()
        if available:
            print("available runs: " + ", ".join(available[:20]),
                  file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback
        print(f"cannot read run '{args.run_id}': {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if args.as_json:
        _emit(history_payload(state))
    else:
        print(render_history(state))
    return 0


def _safe_text(value: Any, limit: int, *, label: str = "objective") -> str:
    """Render a free-text field for display: redacted, then truncated.

    ``redact()`` masks mapping *keys*, so it cannot sanitise a bare string like
    an objective. Anything mentioning a credential-shaped word is therefore
    withheld wholesale rather than partially printed — the conservative choice.
    Truncation is display-only and deterministic; persisted text is untouched.

    ``label`` only names the field in the withheld message, so operators are
    told which value was suppressed.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if any(marker in text.lower() for marker in _secret_markers()):
        return f"<{label} withheld: may contain sensitive values>"
    return text[:limit - 1] + "…" if len(text) > limit else text


def _run_status(state: Any) -> str:
    """Most meaningful status for a one-line summary.

    Reuses the vocabulary :func:`_final_outcome` already established — the
    leading word of its verdict — so the listing and the history report can
    never disagree about a run.
    """
    return _final_outcome(state).split(" ", 1)[0].split("—")[0].strip()


#: Shown when a run has never received an operator decision. A decision is
#: established *only* by an ``operator_decision`` history entry — never
#: inferred from a task status such as CANCELLED or FAILED.
NO_DECISION = "-"

#: Marker for a run still awaiting an operator decision.
PENDING_DECISION = "PENDING"

#: The decisions the CLI can record. `approve` and `reject` use these exact
#: constants, so the filter vocabulary cannot drift from what is written.
DECISION_APPROVED = "APPROVED"
DECISION_REJECTED = "REJECTED"

#: User-facing alias for "no operator decision". A bare ``-`` is awkward on a
#: command line because argparse reads a leading dash as an option, so ``NONE``
#: is the documented spelling; both are accepted and mean the same thing.
NO_DECISION_ALIAS = "NONE"


def valid_decisions() -> frozenset[str]:
    """Every value ``--decision`` accepts.

    Derived from the labels :func:`_decision_label` can actually produce — the
    two recorded decisions plus the two synthetic states — so adding a decision
    to the approve/reject commands makes it filterable automatically.
    """
    return frozenset({DECISION_APPROVED, DECISION_REJECTED,
                      PENDING_DECISION, NO_DECISION_ALIAS, NO_DECISION})


def _decision_argument(value: str) -> str:
    """Validate --decision against the derived vocabulary (case-insensitive).

    ``NONE`` and ``-`` both normalise to the internal no-decision marker so the
    filter compares directly against what the listing displays.
    """
    candidate = str(value).strip().upper()
    if candidate not in valid_decisions():
        raise argparse.ArgumentTypeError(
            f"unknown decision '{value}'. Valid decisions: "
            + ", ".join(sorted(valid_decisions())))
    return NO_DECISION if candidate == NO_DECISION_ALIAS else candidate


def _latest_decision(state: Any) -> dict[str, Any]:
    """Summarise the operator decision for a run, if one was ever recorded.

    Rule for multiple decisions: a run may accumulate several
    ``operator_decision`` entries (one per pending action). History is
    append-only and chronological, so the **most recent** entry is reported —
    an approval followed later by a rejection shows REJECTED. The underlying
    history is never modified, and ``history`` still lists every decision.

    Returns ``decision``/``decision_at``/``decision_operator``. The operator's
    free-text ``note`` is deliberately excluded: it can contain anything and
    belongs in ``history``, not a one-line summary.
    """
    latest = None
    for entry in getattr(state, "history", []) or []:
        if getattr(entry, "kind", "") == "operator_decision":
            latest = entry          # append-only, so the last wins

    if latest is None:
        return {"decision": None, "decision_at": None,
                "decision_operator": None}

    data = getattr(latest, "data", {}) or {}
    decision = data.get("decision")
    at = getattr(latest, "at", None)
    return {
        "decision": str(decision).upper() if decision else None,
        "decision_at": at.isoformat() if hasattr(at, "isoformat") else None,
        "decision_operator": _safe_text(data.get("operator"), 32,
                                        label="operator"),
    }


def _run_summary(store: JsonFileStateStore, run_id: str, *,
                 with_actions: bool = False) -> dict[str, Any]:
    """Summarise one persisted run. Never raises: a broken run is reported.

    ``with_actions`` adds an ``actions`` list of safe pending-action details.
    It is opt-in so the default summary — and therefore ``runs --json`` and
    ``inbox --json`` — keeps exactly the keys it had before.
    """
    try:
        state = store.load(run_id)
    except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
        return {"run_id": run_id, "status": "UNREADABLE", "objective": "",
                "replans": None, "timestamp": None, "sort_key": None,
                # An unreadable run has no trustworthy decision either.
                "decision": None, "decision_at": None,
                "decision_operator": None, "pending_actions": None,
                # An unparseable state has no trustworthy actions either.
                **({"actions": []} if with_actions else {}),
                "error": f"{type(exc).__name__}: {exc}"}

    # `AgentState.from_dict` substitutes "now" for a missing timestamp, so the
    # raw document decides whether one was genuinely persisted. Never show an
    # invented time.
    persisted_ts: str | None = None
    raw_actions: Any = None
    try:
        raw = json.loads((store.directory / f"{run_id}.json")
                         .read_text(encoding="utf-8"))
        if isinstance(raw, Mapping):
            candidate = raw.get("updated_at") or raw.get("created_at")
            if isinstance(candidate, str) and candidate:
                persisted_ts = candidate
            # Same single read serves the action details: no second file open.
            raw_actions = raw.get("pending_actions")
    except Exception:  # pragma: no cover - state already loaded successfully
        persisted_ts = None

    task = getattr(state, "current_task", None)
    return {
        "run_id": getattr(state, "run_id", run_id),
        "status": _run_status(state),
        # Sanitised here rather than at render time, so *every* consumer —
        # human listing and --json alike — gets the safe form.
        "objective": _safe_text(task.objective if task else "", MAX_OBJECTIVE),
        "replans": _replan_count(state),
        "timestamp": persisted_ts,
        "sort_key": _parse_time(persisted_ts),
        # Pending actions and decisions are separate facts: a run can await a
        # decision, have received one, both, or neither.
        "pending_actions": len(getattr(state, "pending_actions", []) or []),
        **_latest_decision(state),
        **({"actions": _action_details(state, raw_actions)}
           if with_actions else {}),
        "error": None,
    }


def _decision_label(summary: Mapping[str, Any]) -> str:
    """One short column value describing operator involvement.

    Precedence: a recorded decision is a fact and always wins. Only when no
    decision exists does an outstanding pending action surface as PENDING, so
    "awaiting a human" is never confused with "a human decided".
    """
    decision = summary.get("decision")
    if decision:
        return str(decision)
    if summary.get("pending_actions"):
        return PENDING_DECISION
    return NO_DECISION


def _collect_summaries(store: JsonFileStateStore, *,
                       with_actions: bool = False) -> list[dict[str, Any]]:
    """All runs, newest first; undated and unreadable runs sort last by id.

    Each run is still loaded exactly once, whether or not action details are
    requested — ``with_actions`` only changes what that single load reports.
    """
    summaries = [_run_summary(store, run_id, with_actions=with_actions)
                 for run_id in store.list_runs()]
    summaries.sort(key=lambda s: (
        s["sort_key"] is None,                       # dated runs first
        -(s["sort_key"].timestamp() if s["sort_key"] else 0),  # newest first
        s["run_id"],                                 # stable tiebreak
    ))
    return summaries


def render_runs(summaries: Sequence[Mapping[str, Any]], *,
                show_pending: bool = False,
                show_actions: bool = False,
                empty: str = "(no runs found)") -> str:
    """Fixed-width listing, readable in a normal terminal.

    ``show_pending`` adds a PENDING column holding the number of unresolved
    operator actions; ``inbox`` uses it so the operator can see how many
    decisions a run is waiting on. ``show_actions`` additionally prints each
    unresolved action in an indented block under its own run row, from the
    ``actions`` key the summary carries when collected with details.

    Everything else — sanitisation, the decision label, timestamps, the
    unreadable note — is shared with ``runs`` so the two listings can never
    disagree about the same run. Both flags default off, so the existing
    ``runs`` output is unchanged.
    """
    if not summaries:
        return empty

    id_width = max(len("RUN ID"),
                   max(len(str(s["run_id"])) for s in summaries))
    pending_head = f"{'PENDING':>7}  " if show_pending else ""
    rows = [f"{'RUN ID':<{id_width}}  {'STATUS':<10}  {'DECISION':<9}  "
            f"{pending_head}"
            f"{'REPLANS':>7}  {'TIMESTAMP':<19}  OBJECTIVE"]
    for summary in summaries:
        replans = summary.get("replans")
        stamp = summary.get("timestamp")
        # Trim ISO microseconds/offset to minutes for a compact column.
        shown = str(stamp)[:16].replace("T", " ") if stamp \
            else "(no timestamp)"
        objective = str(summary.get("objective") or "")  # already sanitised
        if summary.get("error"):
            objective = f"<unreadable: {_clip(summary['error'], 60)}>"
        pending_cell = ""
        if show_pending:
            count = summary.get("pending_actions")
            pending_cell = f"{('-' if count is None else count):>7}  "
        rows.append(
            f"{summary['run_id']:<{id_width}}  "
            f"{str(summary.get('status', '')):<10}  "
            f"{_decision_label(summary):<9}  "
            f"{pending_cell}"
            f"{('-' if replans is None else replans):>7}  "
            f"{shown:<19}  {objective}")
        if show_actions:
            # Directly beneath its own row, so an action is never ambiguous
            # about which run it belongs to.
            rows.extend(render_action_details(summary.get("actions") or []))

    unreadable = sum(1 for s in summaries if s.get("error"))
    if unreadable:
        rows.append("")
        rows.append(f"note: {unreadable} run(s) could not be read and are "
                    f"listed as UNREADABLE.")
    return "\n".join(rows)


def filter_summaries(summaries: Sequence[Mapping[str, Any]],
                     status: str | None,
                     *, decision: str | None = None) -> list[dict[str, Any]]:
    """Keep only runs matching ``status`` and/or ``decision``.

    Order is preserved, and both filters are independent — supplying both
    requires a run to satisfy *both*, neither reinterpreting the other.

    Matching uses the same fields the listing displays (``status`` and the
    label from :func:`_decision_label`), so ``runs --status X``,
    ``runs --decision Y`` and ``history`` can never disagree.

    An unreadable run is deliberately excluded from every status filter: its
    state could not be parsed, so it has no trustworthy status to match on. It
    still appears in an unfiltered listing. For decisions it carries no
    decision at all, so it matches only the explicit no-decision filter.
    """
    kept = [dict(summary) for summary in summaries]

    if status is not None:
        wanted = status.strip().upper()
        kept = [s for s in kept
                if str(s.get("status", "")).upper() == wanted
                and not (s.get("error") and wanted != UNREADABLE_STATUS)]

    if decision is not None:
        target = decision.strip().upper()
        if target == NO_DECISION_ALIAS:
            target = NO_DECISION
        kept = [s for s in kept if _decision_label(s) == target]

    return kept


def needs_attention(summary: Mapping[str, Any]) -> bool:
    """True when a run is waiting on a human right now.

    The single authoritative criterion is ``pending_actions > 0`` — the count
    of *currently unresolved* actions, which ``approve``/``reject`` shrink by
    clearing the action. It is deliberately NOT derived from the decision
    label, the status, escalation or failure:

    * ``_decision_label`` reports the *latest recorded decision*, which wins
      over pendingness, so a run with one action approved and another still
      unresolved shows APPROVED and would be missed;
    * a status of ESCALATED/BLOCKED/FAILED describes how the run ended, not
      whether anything is still awaiting a decision.

    An unreadable run has ``pending_actions is None`` — its state could not be
    parsed, so there is no honest way to claim it needs attention. It is
    excluded (and reported separately) rather than guessed at.
    """
    count = summary.get("pending_actions")
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def filter_attention(summaries: Sequence[Mapping[str, Any]],
                     ) -> list[dict[str, Any]]:
    """Keep only runs with unresolved pending actions, preserving order."""
    return [dict(s) for s in summaries if needs_attention(s)]


def _run_result(agent: Any, report: Any, *, resumed: bool) -> dict[str, Any]:
    """Uniform result payload for both a fresh run and a resumed one."""
    return {
        "run_id": agent.state.run_id,
        "resumed": resumed,
        "report": report.to_dict(),
        "state": agent.state.summary(),
        "final": _final_outcome(agent.state),
    }


def _render_run_result(payload: Mapping[str, Any]) -> str:
    """Concise human summary. Never dumps state, prompts or credentials."""
    report = payload.get("report") or {}
    state = payload.get("state") or {}
    lines = [
        f"Run:        {payload.get('run_id')}"
        + ("  (resumed)" if payload.get("resumed") else ""),
        f"Objective:  {_safe_text(state.get('objective'), MAX_FIELD)}",
        f"Status:     {report.get('status')}",
        f"Subtasks:   {state.get('subtasks_completed')}/"
        f"{state.get('subtasks_total')} completed",
        f"Executed:   {report.get('executed')}   "
        f"failed: {report.get('failed')}   retried: {report.get('retried')}",
        f"Replans:    {payload.get('state', {}).get('replans', '')}"
        if "replans" in state else
        f"Replan requested: {bool(report.get('replan_requested'))}",
        f"Escalated:  {bool(report.get('escalated'))}",
        "",
        f"FINAL: {payload.get('final')}",
    ]
    if payload.get("resumed"):
        lines.append(f"Inspect with: agentcore history {payload.get('run_id')}")
    return "\n".join(line for line in lines if line is not None)


def _command_resume(args: argparse.Namespace, config: AgentConfig) -> int:
    """Continue a persisted run using the existing Agent machinery.

    The CLI is a thin adapter: it loads state through the same store and calls
    ``Agent.resume()`` then ``Agent.run_task()``. Planning, verification,
    permissions, recovery and replanning are entirely the engine's business.
    """
    run_id = args.resume_run_id
    directory = _runs_dir(config, args.runs_dir)
    if not directory.exists():
        print(f"no run directory at {directory}", file=sys.stderr)
        return 2

    store = JsonFileStateStore(directory)
    _quiet_logging()

    # --- preflight: everything here is read-only -------------------------
    # An Agent is only constructed once the run is known to be resumable, so a
    # bad run id can never start a model or touch state.
    try:
        state = store.load(run_id)
    except AgentError as exc:
        print(f"cannot resume run '{run_id}': {exc}", file=sys.stderr)
        available = store.list_runs()
        if available:
            print("available runs: " + ", ".join(available[:20]),
                  file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback
        print(f"cannot resume run '{run_id}': {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    task = getattr(state, "current_task", None)
    if task is None:
        print(f"run '{run_id}' has no task to continue", file=sys.stderr)
        return 2
    if task.status.is_terminal:
        # Matches the engine's own contract: Agent.run_task refuses a finished
        # task. Reported here so the state file is never touched.
        print(f"run '{run_id}' has already finished "
              f"(status '{task.status}') and cannot be resumed.",
              file=sys.stderr)
        print(f"inspect it with: agentcore history {run_id}", file=sys.stderr)
        return 2

    # --- resume through the existing engine ------------------------------
    # Constructing an Agent reconfigures logging from the run's config, which
    # would otherwise print records onto stdout and corrupt the report (and
    # --json). Re-quieten immediately afterwards.
    agent = Agent(config, AgentDependencies(state_store=store))
    _quiet_logging()
    try:
        agent.resume(run_id)
    except WorkspaceMismatchError as exc:
        # A resumed autonomous run must not execute against a different
        # project directory. Nothing has run at this point.
        print(f"cannot resume run '{run_id}': {exc}", file=sys.stderr)
        print(f"  run workspace:     {exc.details.get('run_workspace')}",
              file=sys.stderr)
        print(f"  current workspace: {exc.details.get('current_workspace')}",
              file=sys.stderr)
        print("re-run from the original workspace, or set workspace_dir "
              "to match.", file=sys.stderr)
        return 2
    try:
        report = agent.run_task()
    except AgentError as exc:
        print(f"resume failed for run '{run_id}': {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - keep tracebacks out of the CLI
        print(f"resume failed for run '{run_id}': "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = _run_result(agent, report, resumed=True)
    if args.as_json:
        _emit(payload)
    else:
        print(_render_run_result(payload))
    agent.shutdown()
    return 0


def _command_runs(args: argparse.Namespace, config: AgentConfig) -> int:
    directory = _runs_dir(config, args.runs_dir)
    if not directory.exists():
        if args.as_json:
            _emit([])
        else:
            print("(no runs found)")
        return 0

    _quiet_logging()
    # discover/load -> safe summaries -> sort  (all done by _collect_summaries)
    summaries = _collect_summaries(JsonFileStateStore(directory))
    total = len(summaries)

    # -> filter (status, then decision) -> limit. Filtering after sorting keeps
    # the newest-first ordering intact, so a limit always yields the newest
    # matches; both filters must be satisfied when both are supplied.
    matching = filter_summaries(summaries, getattr(args, "status", None),
                                decision=getattr(args, "decision", None))
    matched = len(matching)

    limit = None if getattr(args, "show_all", False) else (
        args.limit or DEFAULT_RUN_LIMIT)
    shown = matching if limit is None else matching[:limit]

    if args.as_json:
        # Same sanitised representation as the human listing; sort_key is
        # internal bookkeeping and never leaves the process.
        _emit([{k: v for k, v in s.items() if k != "sort_key"} for s in shown])
        return 0

    filters = []
    if getattr(args, "status", None):
        filters.append(f"status {args.status}")
    if getattr(args, "decision", None):
        shown_decision = (NO_DECISION_ALIAS if args.decision == NO_DECISION
                          else args.decision)
        filters.append(f"decision {shown_decision}")
    if not shown and filters:
        print(f"(no runs with {' and '.join(filters)}; "
              f"{total} run(s) total)")
        return 0

    print(render_runs(shown))
    if limit is not None and matched > len(shown):
        print()
        print(f"note: showing {len(shown)} of {matched} matching run(s); "
              f"use --limit N or --all to see more.")
    return 0


#: Printed by `agentcore inbox RUN_ID` for a run that exists and is readable
#: but has nothing awaiting a human. Deliberately distinct from
#: :data:`INBOX_EMPTY`, which describes the whole inbox rather than one run.
INBOX_RUN_CLEAR = "Run {run_id} has no pending actions."


def _command_inbox_run(args: argparse.Namespace, config: AgentConfig,
                       run_id: str) -> int:
    """Drill into one run: its summary plus every unresolved action.

    Loads exactly the requested state file. ``_run_summary`` already takes a
    run id and performs a single ``store.load()`` — verified: it never calls
    ``list_runs``. So this is a targeted lookup, not "load everything and
    filter", and its cost does not grow with the number of persisted runs.

    Action details are implied, because inspecting one named run is *only*
    ever asked in order to act on it. Everything else — sanitisation, the
    summary schema, the renderers — is shared with the full inbox, so the two
    views can never disagree about the same run.
    """
    directory = _runs_dir(config, args.runs_dir)
    if not directory.exists():
        print(f"no run directory at {directory}", file=sys.stderr)
        return 2

    store = JsonFileStateStore(directory)
    _quiet_logging()

    # `_run_summary` never raises: an unreadable or absent run comes back
    # flagged, so one code path covers "no such run", "corrupt state" and an
    # invalid id like '../escape' (which the store rejects outright).
    summary = _run_summary(store, run_id, with_actions=True)

    if summary.get("error"):
        print(f"cannot read run '{run_id}': {summary['error']}",
              file=sys.stderr)
        # A concise pointer rather than a dump of every id in the directory.
        print("list waiting runs with: agentcore inbox", file=sys.stderr)
        return 2

    if not needs_attention(summary):
        # A valid run with nothing outstanding is normal, not an error — the
        # same stance the empty inbox takes. JSON stays a list so the schema
        # is identical whether or not the run qualifies; it is simply empty,
        # which is exactly what `inbox --json` would say about this run.
        if args.as_json:
            _emit([])
        else:
            print(INBOX_RUN_CLEAR.format(run_id=summary["run_id"]))
        return 0

    if args.as_json:
        # Exactly the objects `inbox --json --action-details` emits, as a
        # one-element list: same schema, same sanitisation, no second format.
        _emit([{k: v for k, v in summary.items() if k != "sort_key"}])
        return 0

    print("1 run awaiting an operator decision:")
    print()
    print(render_runs([summary], show_pending=True, show_actions=True,
                      empty=INBOX_EMPTY))
    print()
    print(f"Decide with: agentcore approve|reject {summary['run_id']} "
          "--action-id <ACTION ID>")
    return 0


def _command_inbox(args: argparse.Namespace, config: AgentConfig) -> int:
    """Show every run that currently needs an operator decision.

    Deliberately a thin composition of the existing read pipeline:
    ``_collect_summaries`` (discover -> load once -> sanitise -> sort newest
    first) -> :func:`filter_attention` -> ``render_runs``. No second read path,
    no second pending-action calculation and no second serialisation format.
    Strictly read-only: no Agent, model or tool is constructed.

    ``--action-details`` only enriches that same single pass: the summaries
    gain an ``actions`` list, which the renderer prints beneath each run and
    ``--json`` includes as one extra key. Run selection, ordering, limits and
    every other column are untouched by the flag.
    """
    run_id = getattr(args, "run_id", None)
    if run_id:
        # Selecting one run makes "how many runs to show" meaningless. Saying
        # so beats silently ignoring a flag the operator deliberately typed.
        if getattr(args, "limit", None) is not None or \
                getattr(args, "show_all", False):
            flag = "--all" if getattr(args, "show_all", False) else "--limit"
            print(f"error: {flag} cannot be combined with a run id; "
                  f"`inbox {run_id}` already selects exactly one run",
                  file=sys.stderr)
            return 2
        return _command_inbox_run(args, config, run_id)

    directory = _runs_dir(config, args.runs_dir)
    if not directory.exists():
        if args.as_json:
            _emit([])
        else:
            print(INBOX_EMPTY)
        return 0

    _quiet_logging()
    with_actions = bool(getattr(args, "action_details", False))
    summaries = _collect_summaries(JsonFileStateStore(directory),
                                   with_actions=with_actions)
    matching = filter_attention(summaries)
    matched = len(matching)

    limit = None if getattr(args, "show_all", False) else (
        args.limit or DEFAULT_RUN_LIMIT)
    shown = matching if limit is None else matching[:limit]

    if args.as_json:
        # Byte-for-byte the same sanitised objects `runs --json` emits, in the
        # same order, so human and JSON output are the same logical set.
        _emit([{k: v for k, v in s.items() if k != "sort_key"} for s in shown])
        return 0

    # An unreadable run cannot prove it needs attention, so it is excluded —
    # but silently dropping it would be dishonest, so it is counted.
    unreadable = sum(1 for s in summaries if s.get("error"))

    if not shown:
        print(INBOX_EMPTY)
        if unreadable:
            print(f"note: {unreadable} unreadable run(s) were skipped; "
                  "see `agentcore runs`.")
        return 0

    print(f"{len(shown)} run(s) awaiting an operator decision:")
    print()
    print(render_runs(shown, show_pending=True, show_actions=with_actions,
                      empty=INBOX_EMPTY))
    if with_actions:
        print()
        print("Decide with: agentcore approve|reject <run_id> "
              "--action-id <ACTION ID>")
    if unreadable:
        print()
        print(f"note: {unreadable} unreadable run(s) were skipped; "
              "see `agentcore runs`.")
    if limit is not None and matched > len(shown):
        print()
        print(f"note: showing {len(shown)} of {matched} matching run(s); "
              f"use --limit N or --all to see more.")
    return 0


# ===========================================================================
# memory
# ===========================================================================

#: Cap on remembered content shown in the memory listing.
MAX_MEMORY_CONTENT = 60


def _memory_store(config: AgentConfig):
    """Open the configured memory store, or explain why it is unavailable.

    Returns ``(store, error)`` — exactly one is meaningful. Reuses
    ``build_memory_store`` so the CLI and the Agent resolve the same
    directory; no file is ever opened directly here.
    """
    if not config.features.memory:
        return None, ("memory is disabled; enable features.memory in your "
                      "config to use this command")
    from agentcore.memory.store import build_memory_store

    try:
        return build_memory_store(config), None
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback
        return None, f"cannot open memory: {type(exc).__name__}: {exc}"


def _memory_summary(record: Any) -> dict[str, Any]:
    """Sanitised, bounded projection of one record for operator output."""
    provenance = getattr(record, "provenance", None)
    return {
        "id": record.id,
        "type": record.type.value,
        "scope": record.scope.value,
        "scope_key": record.scope_key,
        "trust": record.trust.value,
        "revision": record.revision,
        "tags": list(record.tags),
        # Remembered text is user content: sanitise it exactly like every
        # other free-text field the CLI prints.
        "content": _safe_memory_text(record.content, MAX_FIELD, label="memory"),
        "recorded_by": ("user" if getattr(provenance, "operator_supplied",
                                          False) else "agent"),
        "source": getattr(provenance, "source", None),
        "run_id": getattr(provenance, "run_id", None),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "retired": record.is_expired(),
    }


def render_memories(rows: Sequence[Mapping[str, Any]]) -> str:
    """Fixed-width memory listing, matching the runs/inbox conventions."""
    if not rows:
        return "(no memories stored)"
    id_width = max(len("MEMORY ID"), max(len(str(r["id"])) for r in rows))
    lines = [f"{'MEMORY ID':<{id_width}}  {'TYPE':<10}  {'SCOPE':<8}  "
             f"{'TRUST':<13}  {'REV':>3}  {'BY':<5}  CONTENT"]
    for row in rows:
        content = _clip(row.get("content"), MAX_MEMORY_CONTENT)
        if row.get("retired"):
            content = f"(retired) {content}"
        lines.append(
            f"{row['id']:<{id_width}}  {row['type']:<10}  {row['scope']:<8}  "
            f"{row['trust']:<13}  {row['revision']:>3}  "
            f"{row['recorded_by']:<5}  {content}")
    return "\n".join(lines)


def _command_memory(args: argparse.Namespace, config: AgentConfig) -> int:
    """Operator surface over persistent memory: inspect and correct safely."""
    store, error = _memory_store(config)
    if store is None:
        print(error, file=sys.stderr)
        return 2
    _quiet_logging()

    command = args.memory_command
    if command == "list":
        return _memory_list(args, store)
    if command == "show":
        return _memory_show(args, store)
    if command == "conflicts":
        return _memory_conflicts(args, config, store)

    # Every mutating command resolves the record first, so an unknown id can
    # never produce a partial change.
    record = _memory_get(store, args.memory_id)
    if record is None:
        print(f"no memory with id '{args.memory_id}'", file=sys.stderr)
        print("list them with: agentcore memory list", file=sys.stderr)
        return 2

    from agentcore.memory.resolve import MemoryResolver

    resolver = MemoryResolver(store)
    if command == "forget":
        return _memory_forget(args, resolver, record)
    if command == "correct":
        return _memory_correct(args, store, record)
    if command == "retire":
        return _memory_retire(args, resolver, record)
    print(f"unknown memory command '{command}'", file=sys.stderr)
    return 2


def _memory_get(store: Any, memory_id: str) -> Any:
    try:
        return store.get(memory_id)
    except Exception:  # noqa: BLE001 - missing, unreadable or invalid id
        return None


def _memory_list(args: argparse.Namespace, store: Any) -> int:
    include_expired = bool(getattr(args, "show_all", False))
    query = getattr(args, "query", None)
    if query:
        from agentcore.memory.query import MemoryQuery

        result = store.search(MemoryQuery(text=query,
                                          include_expired=include_expired))
        records = [hit.record for hit in result]
    else:
        records = store.all_records(include_expired=include_expired)

    limit = args.limit or DEFAULT_RUN_LIMIT
    rows = [_memory_summary(r) for r in records[:limit]]
    if args.as_json:
        _emit(rows)
        return 0
    if not rows:
        print("(no matching memories)" if query else "(no memories stored)")
        return 0
    print(render_memories(rows))
    if len(records) > len(rows):
        print()
        print(f"note: showing {len(rows)} of {len(records)}; use --limit N "
              "to see more.")
    return 0


def _memory_show(args: argparse.Namespace, store: Any) -> int:
    record = _memory_get(store, args.memory_id)
    if record is None:
        print(f"no memory with id '{args.memory_id}'", file=sys.stderr)
        return 2
    row = _memory_summary(record)
    if args.as_json:
        _emit(row)
        return 0
    print("\n".join([
        f"Memory:    {row['id']}",
        f"Type:      {row['type']}   scope: {row['scope']}"
        + (f":{row['scope_key']}" if row['scope_key'] else ""),
        f"Trust:     {row['trust']}   recorded by: {row['recorded_by']}",
        f"Revision:  {row['revision']}"
        + ("   (retired — not returned by retrieval)"
           if row["retired"] else ""),
        f"Source:    {row['source'] or '(none)'}"
        + (f"   run: {row['run_id']}" if row["run_id"] else ""),
        f"Created:   {row['created_at']}",
        f"Updated:   {row['updated_at']}",
        f"Tags:      {', '.join(row['tags']) if row['tags'] else '(none)'}",
        "",
        f"Content:   {row['content']}",
    ]))
    return 0


def _memory_forget(args: argparse.Namespace, resolver: Any, record: Any) -> int:
    """Delete one memory. Confirmation is required: this is irreversible."""
    if not getattr(args, "confirmed", False):
        print(f"refusing to delete '{record.id}' without --yes",
              file=sys.stderr)
        print(f"inspect it first with: agentcore memory show {record.id}",
              file=sys.stderr)
        return 2
    outcome = resolver.forget(record.id,
                              reason=getattr(args, "reason", "") or
                              "deleted by operator")
    return _emit_outcome(args, outcome, f"Deleted memory {record.id}")


def _memory_correct(args: argparse.Namespace, store: Any, record: Any) -> int:
    """Replace a memory's content, keeping its id, origin and history."""
    content = _normalise_cli_text(args.content)
    if not content:
        print("the corrected content must not be empty", file=sys.stderr)
        return 2

    from agentcore.memory.capture import looks_like_secret

    if looks_like_secret(content, redact_keys=config_redact_keys()):
        print("refusing to store content that looks like a credential",
              file=sys.stderr)
        return 2
    try:
        updated = store.update(record.id, content=content)
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback
        print(f"cannot correct '{record.id}': {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    if args.as_json:
        _emit(_memory_summary(updated))
        return 0
    print(f"Corrected memory {updated.id}")
    print(f"  revision:  {record.revision} -> {updated.revision}")
    print(f"  created:   {updated.created_at.isoformat()}  (unchanged)")
    print(f"  content:   "
          f"{_safe_memory_text(updated.content, MAX_FIELD, label='memory')}")
    return 0


def _memory_retire(args: argparse.Namespace, resolver: Any, record: Any) -> int:
    """Mark a memory no longer true without destroying it."""
    from agentcore.memory.resolve import CorrectionKind, CorrectionRequest

    outcome = resolver.resolve_correction(
        CorrectionRequest(kind=CorrectionKind.INVALIDATE),
        memory_id=record.id)
    return _emit_outcome(
        args, outcome,
        f"Retired memory {record.id} — kept for audit, no longer retrieved")


def _conflict_summary(conflict: Any, store: Any) -> dict[str, Any]:
    """Sanitised projection of one conflict for operator output.

    Includes a short summary of each related memory so the contradiction is
    understandable without opening a second command — and without exposing any
    conversation.
    """
    related = []
    for memory_id in conflict.related_ids:
        existing = _memory_get(store, memory_id)
        related.append({
            "id": memory_id,
            "content": _safe_memory_text(existing.content, MAX_FIELD,
                                         label="memory")
            if existing is not None else "(memory no longer exists)",
            "retired": bool(existing.is_expired()) if existing is not None
            else None,
        })
    return {
        "id": conflict.id,
        "status": conflict.status.value,
        "candidate": _safe_memory_text(conflict.candidate_content, MAX_FIELD,
                                       label="candidate"),
        "type": conflict.candidate_type.value,
        "scope": conflict.candidate_scope.value,
        "trust": conflict.candidate_trust.value,
        "recorded_by": ("user" if conflict.candidate_provenance
                        .operator_supplied else "agent"),
        "reason": _safe_text(conflict.reason, MAX_FIELD, label="reason"),
        "related": related,
        "created_at": conflict.created_at.isoformat(),
        "resolution": conflict.resolution,
        "resolved_by": conflict.resolved_by,
        "resolved_at": conflict.resolved_at.isoformat()
        if conflict.resolved_at else None,
    }


def render_conflicts(rows: Sequence[Mapping[str, Any]]) -> str:
    """Readable conflict listing, matching the memory/runs conventions."""
    if not rows:
        return "(no unresolved memory conflicts)"
    lines: list[str] = []
    for row in rows:
        header = f"{row['id']}  [{row['status']}]"
        if row.get("resolution"):
            header += f" -> {row['resolution']}"
            if row.get("resolved_by"):
                header += f" by {row['resolved_by']}"
        lines.append(header)
        lines.append(f"  candidate:  {_clip(row['candidate'], 100)}")
        lines.append(f"  classified: {row['type']} / {row['scope']} / "
                     f"trust {row['trust']} / recorded by {row['recorded_by']}")
        if row.get("reason"):
            lines.append(f"  reason:     {_clip(row['reason'], 100)}")
        for existing in row["related"]:
            marker = " (retired)" if existing.get("retired") else ""
            lines.append(f"  conflicts with {existing['id']}{marker}: "
                         f"{_clip(existing['content'], 80)}")
        lines.append(f"  detected:   {str(row['created_at'])[:19]}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _memory_conflicts(args: argparse.Namespace, config: AgentConfig,
                      store: Any) -> int:
    """List unresolved memory conflicts, or settle one explicitly."""
    from agentcore.memory.conflicts import build_conflict_store

    try:
        conflicts = build_conflict_store(config)
    except Exception as exc:  # noqa: BLE001 - never show a raw traceback
        print(f"cannot open conflicts: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if getattr(args, "resolve_id", None):
        return _memory_resolve_conflict(args, store, conflicts)

    limit = args.limit or DEFAULT_RUN_LIMIT
    found = (conflicts.list_all(limit=limit) if getattr(args, "show_all", False)
             else conflicts.list_pending(limit=limit))
    rows = [_conflict_summary(c, store) for c in found]
    if args.as_json:
        _emit(rows)
        return 0
    if not rows:
        print("(no unresolved memory conflicts)")
        return 0
    print(render_conflicts(rows))
    return 0


def _memory_resolve_conflict(args: argparse.Namespace, store: Any,
                             conflicts: Any) -> int:
    """Apply an explicit resolution to one persisted conflict."""
    from agentcore.memory.resolve import ConflictResolution, MemoryResolver

    if args.supersede:
        choice = ConflictResolution.SUPERSEDE
    elif args.keep_existing:
        choice = ConflictResolution.KEEP_EXISTING
    elif args.discard:
        choice = ConflictResolution.DISCARD_CANDIDATE
    else:
        print("choose how to resolve it: --supersede, --keep-existing "
              "or --discard", file=sys.stderr)
        return 2

    # Superseding retires existing memories, so it is confirmed like every
    # other destructive memory operation.
    if choice is ConflictResolution.SUPERSEDE and \
            not getattr(args, "confirmed", False):
        print(f"refusing to supersede without --yes: this retires the "
              f"memories related to '{args.resolve_id}'", file=sys.stderr)
        print("inspect it first with: agentcore memory conflicts",
              file=sys.stderr)
        return 2

    resolver = MemoryResolver(store, conflicts=conflicts)
    outcome = resolver.resolve_conflict(args.resolve_id, choice,
                                        operator=args.operator)
    payload = {"conflict_id": args.resolve_id, "resolution": choice.value,
               "status": outcome.status.value, "reason": outcome.reason,
               "affected": list(outcome.affected_ids)}
    if args.as_json:
        _emit(payload)
        return 2 if outcome.status.value == "failed" else 0
    if outcome.status.value == "failed":
        print(f"cannot resolve: {outcome.reason}", file=sys.stderr)
        return 2
    print(f"Resolved conflict {args.resolve_id} as {choice.value}")
    print(f"  {outcome.reason}")
    if outcome.affected_ids:
        print(f"  affected memories: {', '.join(outcome.affected_ids)}")
    return 0


def _emit_outcome(args: argparse.Namespace, outcome: Any, success: str) -> int:
    payload = {"status": outcome.status.value, "reason": outcome.reason,
               "affected": list(outcome.affected_ids),
               "changed": outcome.changed}
    if args.as_json:
        _emit(payload)
        return 0 if outcome.changed else 2
    if outcome.changed:
        print(success)
        return 0
    print(f"nothing changed: {outcome.reason}", file=sys.stderr)
    return 2


def _normalise_cli_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def config_redact_keys() -> tuple[str, ...]:
    return _secret_markers()


# ===========================================================================
# chat
# ===========================================================================

#: How the two speakers are labelled in the transcript.
USER_PROMPT = "You > "
ASSISTANT_LABEL = "Jarvis > "

#: Prefix for commands the CLI handles itself. Everything without it is a
#: message for the model — there is deliberately no shell escape.
COMMAND_PREFIX = "/"

CHAT_HELP = """\
Commands (handled locally, never sent to the assistant):
  /help          show this help
  /session       show the current session id
  /conflicts     list unresolved memory conflicts
  /conflicts resolve <n> supersede|keep|discard
                 settle conflict <n> from that list
  /exit, /quit   end the conversation

Anything else is sent to the assistant. Your conversation is saved after each
turn; resume it later with:  agentcore chat --session <id>\
"""


def _memory_notices(result: Any) -> list[str]:
    """Honest, non-technical notes about what memory actually did.

    Driven entirely by ``TurnResult``, so the interface can never claim more
    than happened: a notice appears only when the runtime reports the effect.
    Deliberately says nothing about *what* was remembered — no ids, no
    candidate text, no counts of internal records — because the point is
    honesty, not debug output.
    """
    notices: list[str] = []
    created = len(getattr(result, "memories_created", []) or [])
    updated = len(getattr(result, "memories_updated", []) or [])

    if created:
        notices.append("[Memory] Remembered something new."
                       if created == 1 else
                       f"[Memory] Remembered {created} new things.")
    if updated:
        notices.append("[Memory] Updated something I already knew.")

    if getattr(result, "has_conflict", False):
        notices.append(
            "[Memory] That conflicts with something I remembered earlier, so "
            "I kept the old information and did not overwrite it. Review it "
            "with: agentcore memory conflicts")

    correction = getattr(result, "correction", None)
    if correction is not None:
        notices.extend(_correction_notices(correction))

    if getattr(result, "memory_error", None):
        # Never silently swallow a failed write: the reply still happened,
        # but the user must not believe anything was saved.
        notices.append("[Memory] I could not save that, but the conversation "
                       "continued.")
    return notices


def _correction_notices(correction: Any) -> list[str]:
    """Report a "forget that" outcome, including refusals."""
    status = getattr(getattr(correction, "status", None), "value", "")
    if status == "deleted":
        return ["[Memory] Forgotten."]
    if status == "superseded":
        return ["[Memory] Marked as no longer true."]
    if status == "unresolved":
        # Ambiguity is a refusal, and saying so is the whole point.
        return ["[Memory] I was not sure which memory you meant, so I did not "
                "delete anything. Tell me more specifically what to forget."]
    if status == "failed":
        return ["[Memory] I could not apply that correction."]
    return []


def _render_turn(result: Any) -> list[str]:
    """The assistant's reply plus any memory notices, ready to print."""
    lines: list[str] = []
    if getattr(result, "ok", False) and result.reply is not None:
        lines.append(f"{ASSISTANT_LABEL}{result.reply.content}")
    elif getattr(result, "error", None):
        # A structured failure, not a traceback, and never a fabricated reply.
        lines.append(f"[Error] {result.error}")
    lines.extend(_chat_notices(result))
    return lines


def _chat_notices(result: Any) -> list[str]:
    from agentcore.desktop.chat import desktop_notices

    return _memory_notices(result) + desktop_notices(result)


def _turn_payload(result: Any) -> dict[str, Any]:
    """Machine-readable turn summary. Same fields the human view is built on."""
    from agentcore.desktop.chat import desktop_observations, desktop_tool_calls

    payload = {
        "session_id": result.session.id,
        "ok": bool(result.ok),
        "reply": result.reply.content if result.reply else None,
        "error": result.error,
        "memories_created": len(result.memories_created),
        "memories_updated": len(result.memories_updated),
        "memories_used": result.memories_used,
        "conflicts": len(result.conflicts),
        "memory_error": result.memory_error,
        "correction": getattr(getattr(result, "correction", None), "status",
                              None) and result.correction.status.value,
        "notices": _chat_notices(result),
    }
    observations = desktop_observations(result)
    if observations:
        payload["desktop_observations"] = observations
    calls = desktop_tool_calls(result)
    if calls:
        payload["desktop_tool_calls"] = calls
    if getattr(result, "error_category", None):
        payload["error_category"] = result.error_category
    return payload


def _chat_session(runtime: Any, args: argparse.Namespace) -> tuple[Any, str]:
    """Resolve the session to converse in, or explain why we cannot.

    ``runtime.start()`` deliberately falls back to a fresh session for an
    unknown id, which is right for a library but wrong for an operator who
    typed an id: silently opening a *different* empty conversation would look
    like data loss. So existence is checked here first.

    Returns ``(session, banner)``; ``session`` is None when the id is unknown.
    """
    session_id = getattr(args, "session_id", None)
    if not session_id:
        session = runtime.start()
        # Reserve the id immediately. The runtime only saves after a turn —
        # correct for a library — but the CLI *shows* the id and invites the
        # user to resume it, so an id that refers to nothing on disk would be
        # a broken promise. Saving an empty session costs one small file.
        runtime.save(session)
        return session, f"Session: {session.id}"

    store = runtime.sessions
    known = False
    if store is not None:
        try:
            known = store.exists(session_id)
        except Exception:  # noqa: BLE001 - an invalid id is simply unknown
            known = False

    if known:
        session = runtime.start(session_id=session_id)
        return session, f"Resumed session: {session.id}"
    if getattr(args, "force_new", False):
        session = runtime.start(session_id=session_id)
        runtime.save(session)          # reserve it, as above
        return session, f"Session: {session.id}"
    return None, session_id


def _command_chat(args: argparse.Namespace, config: AgentConfig) -> int:
    """Talk to the assistant. A thin surface over ConversationRuntime.

    The CLI owns exactly three things: reading input, printing output, and the
    local ``/`` commands. Memory retrieval, capture, correction, conflict
    detection and persistence all belong to the runtime and are not
    reimplemented here.
    """
    if getattr(args, "desktop_evidence", None):
        from agentcore.permissions.levels import PermissionLevel

        if (not args.enable_desktop or args.as_json or not args.message or args.session_id
                or not sys.stdin.isatty() or config.permissions.level is not PermissionLevel.CONFIRM
                or config.permissions.capability_overrides or tuple(config.tools.enabled) != ("open_notepad",)
                or config.conversation.max_tool_rounds != 1 or config.conversation.max_tool_calls_per_round != 1):
            print("--desktop-evidence requires a fresh interactive --enable-desktop --message turn, "
                  "CONFIRM without overrides, only open_notepad, one tool round/call, and no --json.", file=sys.stderr)
            return 2
    if getattr(args, "as_json", False) and not getattr(args, "message", None):
        print("--json requires --message: an interactive session cannot emit "
              "a single JSON document", file=sys.stderr)
        return 2

    deps = AgentDependencies(state_store=JsonFileStateStore(_runs_dir(config, args.runs_dir)))
    desktop = None
    if args.enable_desktop:
        from agentcore.action_journal.adapters import ActionAdapterRegistry
        from agentcore.desktop.chat import ConsoleNotepadApproval
        from agentcore.integrations.computer_control import NotepadActionAdapter

        if not config.conversation.tools_enabled:
            print("--enable-desktop requires conversation.tools_enabled = true", file=sys.stderr)
            return 2
        desktop = ComputerControlAdapter({"enabled": True})
        if desktop.connect() is not IntegrationStatus.CONNECTED:
            print("[Desktop] " + desktop.detail, file=sys.stderr)
            return 2
        deps.action_adapters = ActionAdapterRegistry()
        deps.action_adapters.register(desktop.provided_tools()[0], NotepadActionAdapter())
        if not args.as_json and sys.stdin.isatty():
            deps.approval_gate = ConsoleNotepadApproval()

    agent = Agent(config, deps)
    # Constructing an Agent reconfigures logging; keep it off the transcript.
    _quiet_logging()
    tools = None
    if desktop is not None:
        from agentcore.desktop.chat import NotepadConversationRunner

        if desktop.register_into(agent.registry) == 0:
            agent.shutdown()
            print("[Desktop] Desktop tools are disabled by tool configuration.", file=sys.stderr)
            return 2
        tools = NotepadConversationRunner(
            agent.executor, agent.registry, notepad_tool=desktop.provided_tools()[0],
            max_result_chars=config.conversation.max_tool_result_chars,
            max_calls_per_round=config.conversation.max_tool_calls_per_round,
        )
    system_prompt = None
    if desktop is not None and "get_foreground_app" in agent.registry:
        from agentcore.conversation.runtime import SYSTEM_PROMPT
        from agentcore.desktop.chat import FOREGROUND_RESULT_INSTRUCTIONS

        # Keep result semantics after the final round stops offering tools.
        system_prompt = SYSTEM_PROMPT + "\n\n" + FOREGROUND_RESULT_INSTRUCTIONS
    runtime = agent.conversation(tools=tools, system_prompt=system_prompt)

    session, banner = _chat_session(runtime, args)
    if session is None:
        print(f"no conversation with id '{banner}'", file=sys.stderr)
        print("start a new one with: agentcore chat", file=sys.stderr)
        print(f"or force it with:     agentcore chat --session {banner} --new",
              file=sys.stderr)
        return 2

    try:
        if getattr(args, "message", None):
            return _chat_once(runtime, session, args, approval=deps.approval_gate)
        return _chat_loop(agent, runtime, session, banner)
    finally:
        agent.shutdown()
        if desktop is not None:
            desktop.disconnect()


def _chat_once(runtime: Any, session: Any, args: argparse.Namespace, *, approval=None) -> int:
    """Send one message and exit. Same runtime, same session, same memory."""
    text = _normalise_cli_text(args.message)
    if not text:
        print("nothing to send: --message was empty", file=sys.stderr)
        return 2

    receipt = None
    if getattr(args, "desktop_evidence", None):
        try:
            receipt = args.desktop_evidence.open("x", encoding="utf-8")
        except OSError:
            print("[Desktop evidence] Could not create a new receipt; no conversation turn was sent.", file=sys.stderr)
            return 2
    receipt_failed = False
    try:
        result = runtime.send(text, session=session)
        if receipt is not None:
            from agentcore.desktop.chat import notepad_evidence

            try:
                json.dump(notepad_evidence(result, approval), receipt, ensure_ascii=True, indent=2)
                receipt.flush()
            except OSError:
                receipt_failed = True
    finally:
        if receipt is not None:
            try:
                receipt.close()
            except OSError:
                receipt_failed = True
        if receipt_failed:
            print("[Desktop evidence] Receipt write failed; the action may have executed. Do not retry automatically.", file=sys.stderr)
    if receipt_failed:
        return 1
    if getattr(args, "as_json", False):
        _emit(_turn_payload(result))
        return 0 if result.ok else 1
    for line in _render_turn(result):
        print(line)
    return 0 if result.ok else 1


#: The three words `/conflicts resolve` accepts, mapped onto the existing
#: ConflictResolution vocabulary. No new resolution semantics are invented —
#: these are only shorter spellings for the interactive surface.
_RESOLUTION_WORDS: dict[str, str] = {
    "supersede": "SUPERSEDE",
    "keep": "KEEP_EXISTING",
    "discard": "DISCARD_CANDIDATE",
}


def _safe_memory_text(value: Any, limit: int, *, label: str) -> str:
    """Sanitise remembered free text for display.

    ``_safe_text`` withholds on the *logging* redact keys, which are
    identifier-shaped ("api_key"). Remembered text is prose, so it can carry a
    credential the keyword list misses — "my api key is sk-..." passes
    ``_safe_text`` untouched. The memory layer already owns a stronger
    detector for exactly this (keywords *and* value shapes), so it is applied
    first and the whole value withheld on a hit.
    """
    from agentcore.memory.capture import looks_like_secret

    text = " ".join(str(value or "").split())
    if text and looks_like_secret(text, redact_keys=_secret_markers()):
        return f"<{label} withheld: may contain sensitive values>"
    return _safe_text(text, limit, label=label)


def _chat_conflict_line(index: int, conflict: Any) -> str:
    """One numbered conflict, summarised safely for a chat transcript.

    Content is bounded and sanitised, so a credential-shaped candidate is
    withheld rather than printed. The index is presentational only.
    """
    candidate = _safe_memory_text(conflict.candidate_content,
                                  MAX_MEMORY_CONTENT, label="candidate")
    return f"  {index}. {candidate}"


def _chat_conflict_detail(conflict: Any, agent: Any) -> list[str]:
    """The related memory and reason, indented under a conflict."""
    lines: list[str] = []
    for memory_id in conflict.related_ids:
        existing = None
        if agent.memory is not None:
            try:
                existing = agent.memory.get(memory_id)
            except Exception:  # noqa: BLE001 - the memory may be gone
                existing = None
        summary = (_safe_memory_text(existing.content, MAX_MEMORY_CONTENT,
                                     label="memory")
                   if existing is not None else "(memory no longer exists)")
        lines.append(f"     conflicts with: {summary}")
    if conflict.reason:
        lines.append(f"     reason: {_clip(conflict.reason, 90)}")
    return lines


def _chat_conflicts(agent: Any, args: Sequence[str]) -> list[str]:
    """Handle `/conflicts` and `/conflicts resolve <n> <word>`.

    A thin adapter: listing calls ``Agent.pending_memory_conflicts()`` and
    resolution calls ``Agent.resolve_memory_conflict()``. No conflict is
    stored, ordered or settled here.

    The displayed number is a **UI index into the list just fetched**, never
    persisted and never sent anywhere. It is mapped back to the real conflict
    id immediately before delegating, and the list is re-read on every
    command so the numbering can never go stale.
    """
    if agent.memory_resolver is None:
        return ["[Memory] Memory is not enabled, so there are no conflicts."]

    try:
        pending = agent.pending_memory_conflicts()
    except Exception as exc:  # noqa: BLE001 - never break the chat loop
        return [f"[Memory] Could not read conflicts: {type(exc).__name__}"]

    if not args:
        return _chat_conflict_list(pending, agent)

    if args[0] != "resolve":
        return [f"[Memory] Unknown conflicts command '{args[0]}'.",
                "         Try /conflicts or "
                "/conflicts resolve <n> supersede|keep|discard"]
    return _chat_conflict_resolve(agent, pending, args[1:])


def _chat_conflict_list(pending: Sequence[Any], agent: Any) -> list[str]:
    if not pending:
        return ["[Memory] No unresolved memory conflicts."]

    label = "conflict" if len(pending) == 1 else "conflicts"
    lines = [f"[Memory] {len(pending)} unresolved memory {label}:"]
    for index, conflict in enumerate(pending, start=1):
        lines.append(_chat_conflict_line(index, conflict))
        lines.extend(_chat_conflict_detail(conflict, agent))
    lines.append("  Resolve with: /conflicts resolve <n> "
                 "supersede|keep|discard")
    return lines


def _chat_conflict_resolve(agent: Any, pending: Sequence[Any],
                           args: Sequence[str]) -> list[str]:
    """Settle one conflict by its displayed number."""
    if len(args) < 2:
        return ["[Memory] Usage: /conflicts resolve <n> "
                "supersede|keep|discard"]

    raw_index, raw_choice = args[0], args[1]
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        return [f"[Memory] '{raw_index}' is not a conflict number. "
                "Run /conflicts to see the list."]

    if not pending:
        return ["[Memory] No unresolved memory conflicts."]
    if not 1 <= index <= len(pending):
        return [f"[Memory] There is no conflict {index}. "
                f"Choose between 1 and {len(pending)}."]

    name = _RESOLUTION_WORDS.get(raw_choice)
    if name is None:
        return [f"[Memory] Unknown resolution '{raw_choice}'. "
                "Use supersede, keep or discard."]

    from agentcore.memory.resolve import ConflictResolution

    # The UI index is mapped to the real id here and nowhere else.
    conflict = pending[index - 1]
    outcome = agent.resolve_memory_conflict(
        conflict.id, ConflictResolution[name], operator="chat")

    status = getattr(getattr(outcome, "status", None), "value", "unknown")
    if not getattr(outcome, "changed", False) and status == "failed":
        # Never claim success the resolver did not report.
        return [f"[Memory] Could not resolve conflict {index}: "
                f"{_clip(outcome.reason, 120)}"]
    return _chat_resolution_notice(index, name, outcome)


def _chat_resolution_notice(index: int, name: str, outcome: Any) -> list[str]:
    """Describe what the resolver actually did, in the user's terms."""
    if name == "SUPERSEDE":
        retired = len(getattr(outcome, "affected_ids", ()) or ())
        detail = (f" The previous information was retired "
                  f"({retired} memor{'y' if retired == 1 else 'ies'}).")
        return [f"[Memory] Conflict {index} resolved: the new information is "
                f"now current.{detail if retired else ''}"]
    if name == "KEEP_EXISTING":
        return [f"[Memory] Conflict {index} resolved: kept what I already "
                "remembered; the new information was discarded."]
    return [f"[Memory] Conflict {index} resolved: discarded the new "
            "information. Nothing else changed."]


def _chat_loop(agent: Any, runtime: Any, session: Any,
               banner: str) -> int:
    """The interactive loop. One Agent, one runtime, one session throughout."""
    print(banner)
    print("Type /help for commands, /exit to leave.")
    print()

    while True:
        try:
            raw = input(USER_PROMPT)
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D / Ctrl-C: leave quietly. Every turn was already saved, so
            # nothing is lost and nothing needs unwinding.
            print()
            print("Goodbye.")
            return 0

        text = raw.strip()
        if not text:
            continue          # never send an empty turn to the model

        if text.startswith(COMMAND_PREFIX):
            command = text[len(COMMAND_PREFIX):].strip().lower()
            # /conflicts takes arguments, so it is matched on its first word
            # before the exact-match commands below.
            if command.split(" ")[0] == "conflicts":
                for line in _chat_conflicts(agent, command.split()[1:]):
                    print(line)
                continue
            if command in ("exit", "quit"):
                print("Goodbye.")
                return 0
            if command == "help":
                print(CHAT_HELP)
                continue
            if command == "session":
                print(f"Session: {session.id}")
                continue
            print(f"Unknown command '{text}'. Try /help.")
            continue

        try:
            result = runtime.send(text, session=session)
        except Exception as exc:  # noqa: BLE001 - never show a raw traceback
            print(f"[Error] {type(exc).__name__}: {exc}")
            continue

        for line in _render_turn(result):
            print(line)
        print()


def _doctor_integrations(agent: Agent) -> list[dict[str, Any]]:
    integrations = IntegrationRegistry()
    integrations.add(ComputerControlAdapter())
    integrations.connect_all(agent.registry)
    return integrations.health()


def _emit_doctor(outcomes: dict[str, str], code: int | None = None) -> int:
    # Static messages exclude private values even when an exception contains them.
    messages = {
        "ok": "Check passed.",
        "not_configured": "No model is configured.",
        "unavailable": "Component is unavailable.",
        "error": "Check failed; inspect configuration locally.",
        "not_checked": "Check was not run.",
    }
    ok = all(value in {"ok", "not_configured"} for value in outcomes.values())
    _emit({"schema_version": 1, "ok": ok, "checks": [
        {"name": name, "outcome": outcome, "message": messages[outcome]}
        for name, outcome in outcomes.items()
    ]})
    return int(not ok) if code is None else code


def _command_doctor_json(args: argparse.Namespace) -> int:
    from dataclasses import replace
    from agentcore.config.schema import LoggingConfig

    outcomes = dict.fromkeys(
        ("configuration", "runtime", "computer_control", "model", "providers"),
        "not_checked")
    _quiet_logging()
    try:
        config = load_config(args.config)
    except Exception:
        outcomes["configuration"] = "error"
        return _emit_doctor(outcomes, 2)
    outcomes["configuration"] = "ok"
    try:
        # Agent configures logging itself; do not open a configured log file or
        # print private provider/configuration fields during diagnostics.
        agent = Agent(replace(config, logging=LoggingConfig(level="CRITICAL")))
    except Exception:
        outcomes["runtime"] = "error"
        return _emit_doctor(outcomes)
    outcomes["runtime"] = "ok"
    try:
        try:
            health = _doctor_integrations(agent)
            if type(health) is not list or len(health) != 1 or type(health[0]) is not dict or (
                    health[0].get("name") != "computer_control"):
                outcomes["computer_control"] = "error"
            else:
                outcomes["computer_control"] = {
                    "connected": "ok", "unavailable": "unavailable",
                    "disconnected": "unavailable", "error": "error",
                }.get(health[0].get("status"), "error")
        except Exception:
            outcomes["computer_control"] = "error"
        try:
            model = agent.model_info()
            if model is None:
                outcomes["model"] = ("not_configured" if
                    (config.model.provider or "").strip().lower() in {"", "none"} else "unavailable")
            else:
                outcomes["model"] = "ok" if type(model) is dict and (
                    type(model.get("provider")) is str and bool(model["provider"]) and
                    type(model.get("model")) is str and bool(model["model"])) else "error"
        except Exception:
            outcomes["model"] = "error"
        try:
            from agentcore.models.factory import available_providers

            providers = available_providers()
            outcomes["providers"] = "ok" if type(providers) is list and providers and all(
                type(name) is str and bool(name) for name in providers) else "error"
        except Exception:
            outcomes["providers"] = "error"
    finally:
        try:
            agent.shutdown()
        except Exception:
            outcomes["runtime"] = "error"
    return _emit_doctor(outcomes)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor" and args.as_json:
        import logging

        previous_disable = logging.root.manager.disable
        logging.disable(sys.maxsize)  # Suppress critical and custom levels too.
        try:
            return _command_doctor_json(args)
        finally:
            logging.disable(previous_disable)
    if args.command == "workflow":
        import logging

        from agentcore.workspace_host.workflow_cli import command_workflow

        previous_disable = logging.root.manager.disable
        logging.disable(sys.maxsize)
        try:
            return command_workflow(args)
        finally:
            logging.disable(previous_disable)
    if args.command == "workspace":
        from agentcore.workspace_host.cli import command_workspace

        _quiet_logging()
        return command_workspace(args)
    if args.command == "browser":
        # Keep even configuration errors inside the browser's categorical output
        # boundary, before ordinary Agent construction or its logging setup.
        from agentcore.browser.cli import command_browser

        _quiet_logging()
        return command_browser(args)
    try:
        config = load_config(args.config)
    except AgentError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    # Read-only commands must not construct an Agent: inspecting a finished run
    # should never start a model, register tools, or touch live state.
    if args.command == "history":
        return _command_history(args, config)
    if args.command == "approve":
        return _command_approve(args, config)
    if args.command == "reject":
        return _command_reject(args, config)
    if args.command == "runs":
        return _command_runs(args, config)
    if args.command == "inbox":
        return _command_inbox(args, config)
    if args.command == "memory":
        return _command_memory(args, config)
    if args.command == "chat":
        from agentcore.conversation.errors import SessionDomainError
        try:
            return _command_chat(args, config)
        except SessionDomainError:
            if args.as_json:
                _emit({"ok": False, "error": "conversation_session_domain_mismatch"})
            else:
                print("Conversation session belongs to a different session domain.", file=sys.stderr)
            return 2

    if args.command == "run":
        # An objective starts something new; --resume continues something that
        # exists. Supplying both is contradictory, and neither leaves nothing
        # to do, so both combinations are rejected before any work begins.
        if args.resume_run_id and args.objective:
            print("error: give either an objective or --resume RUN_ID, "
                  "not both", file=sys.stderr)
            return 2
        if not args.resume_run_id and not args.objective:
            print("error: provide an objective, or --resume RUN_ID to "
                  "continue an existing run", file=sys.stderr)
            return 2
        if args.resume_run_id:
            return _command_resume(args, config)

        # Persist the run so `agentcore history <run_id>` can inspect it.
        store = JsonFileStateStore(_runs_dir(config, args.runs_dir))
        agent = Agent(config, AgentDependencies(state_store=store))
        # `run` prints a report to stdout, so keep framework logs off it.
        _quiet_logging()
    else:
        agent = Agent(config)

    if args.command == "status":
        _emit(agent.status())
    elif args.command == "tools":
        _emit(agent.available_tools())
    elif args.command == "run":
        report = agent.execute(args.objective)
        payload = _run_result(agent, report, resumed=False)
        if args.as_json:
            _emit(payload)
        else:
            print(_render_run_result(payload))
    elif args.command == "doctor":
        health = _doctor_integrations(agent)
        from agentcore.models.factory import available_providers

        _emit({
            "version": __version__,
            "environment": config.environment,
            "permission_level": str(agent.policy.level),
            "tools": agent.registry.names(),
            "features_enabled": list(config.features.enabled_flags()),
            "integrations": health,
            "model": agent.model_info(),
            "available_providers": available_providers(),
        })
    agent.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
