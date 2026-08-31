"""Local human operator commands, never a model/admin shell."""

from __future__ import annotations

import argparse
import sys

from agentcore.credentials import CredentialError
from agentcore.credentials.google_protocol import GOOGLE_GMAIL_SEND_SCOPES
from agentcore.credentials.protected import ConnectionState
from agentcore.action_journal import ActionReconciliationStatus
from agentcore.gmail_host.composition import GmailHostComposition, GmailHostMode
from agentcore.gmail_host.errors import GmailHostError, HostFailure
from agentcore.gmail_host.local import open_local_services
from agentcore.gmail_host.operator import PrivateConsole
from agentcore.gmail_host.storage import GmailHostStorage, default_host_directory
from agentcore.integrations.gmail.mutation_http import GmailMutationHttp


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal errors echo rejected arguments (possibly tokens or
        # message content). Only the closed category may cross this boundary.
        raise GmailHostError(HostFailure.INVALID_INPUT)


def parser() -> argparse.ArgumentParser:
    result = _Parser(
        prog="python -m agentcore.gmail_host", allow_abbrev=False,
        description="Private Gmail operator. DO NOT RUN LIVE SEND BEFORE THIS PR IS AUDITED AND MERGED.",
    )
    commands = result.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for command, help_text in (
        ("init", "Initialize private local storage; no OAuth or Gmail I/O."),
        ("status", "Categorical local connection/profile status; no network."),
        ("connect", "Explicit Desktop OAuth consent for the exact GMAIL_SEND profile."),
        ("prepare-smoke-test", "Privately prepare ONE self-send; never send or approve."),
        ("disconnect", "Locally disconnect; does not revoke consent at Google."),
    ):
        commands.add_parser(command, help=help_text, allow_abbrev=False)
    for command, help_text in (
        ("review", "Review the stored preparation privately; no network."),
        ("action-status", "Inspect categorical ActionJournal truth; no network."),
        ("reconcile", "One read-only recovery probe for an uncertain existing action."),
        ("send-smoke-test", "After audit and merge only: review and approve the exact action."),
    ):
        child = commands.add_parser(command, help=help_text, allow_abbrev=False)
        child.add_argument("action_id", help="Opaque action correlation from prepare-smoke-test.")
        if command == "send-smoke-test":
            child.add_argument("--live-send", action="store_true", help="Explicit live composition; fresh private confirmation still required.")
    return result


def _show_action(host, output, action_id=None) -> None:
    status = host.action_status(action_id)
    output.write(
        f"Action: {status.action_id}\nState: {status.state}\n"
        f"Attempts: {status.attempts}; active unresolved probe counter: {status.probe_count}\n"
        f"Last attempt: {status.last_attempt}; effect: {status.effect}\n"
        f"Attempt correlation: {status.attempt_id}\n"
    )
    if status.state in {"RECONCILE_REQUIRED", "IN_FLIGHT"}:
        output.write("DO NOT SEND AGAIN. Inspect or reconcile this SAME action only.\n")
        if status.disposition == "human_confirmation_required":
            output.write("Human confirmation required; sending remains unavailable.\n")
        else:
            output.write("No conclusive Sent match found; effect remains uncertain. Probe later.\n")
    elif status.state == "SUCCEEDED":
        output.write("Do not send again. Privately verify Sent, Message-ID and absence of duplicates.\n")
    elif status.state == "PREPARED":
        output.write("No journal authorization or physical attempt exists. Stopped before live approval.\n")
    elif status.state in {"FAILED", "CANCELLED"}:
        output.write("Inspect the categorical result. No automatic retry or new message is allowed.\n")
    else:
        output.write("Continuation requires fresh private human review and durable approval.\n")


def main(argv=None, *, operator=None, services_factory=open_local_services, output=None) -> int:
    """Factories/terminal are trusted dependency seams, not command-line flags."""
    output = sys.stdout if output is None else output
    command = None
    try:
        arguments = parser().parse_args(argv)
        command = arguments.command
        if command == "send-smoke-test" and not arguments.live_send:
            output.write("LIVE SEND DISABLED. Deliberate pre-live boundary; no services or network opened.\n")
            output.write("DO NOT RUN LIVE SEND BEFORE THIS PR IS AUDITED AND MERGED.\n")
            return 2
        if command == "init":
            with GmailHostStorage(default_host_directory(), initialize=True):
                output.write("Private host storage initialized. Mutation composition DISABLED.\n")
            output.write("Install host-only oauth-client.json using the operator setup document.\n")
            return 0
        operator = PrivateConsole() if operator is None else operator
        with services_factory() as services:
            services.require_owner()
            if command == "connect":
                operator.require_interactive()
                operator.write_private(
                    "Google consent will open in your system browser for the exact Gmail-send profile.\n"
                    "Read Google's warnings; do not bypass restrictions. Return here for durable status.\n"
                )
                services.connect(operator)
                output.write("CURRENT: identity verified and protected connection durably committed.\n")
                output.write("Mutation composition DISABLED. No Gmail send was invoked.\n")
                return 0
            if command == "disconnect":
                operator.require_interactive()
                services.disconnect()
                output.write("LOCALLY DISCONNECTED. No remote revocation is claimed. Preserve action truth.\n")
                return 0
            mode = {
                "prepare-smoke-test": GmailHostMode.PREPARE,
                "send-smoke-test": GmailHostMode.LIVE_SEND,
                "reconcile": GmailHostMode.RECONCILE,
            }.get(command, GmailHostMode.INSPECT)
            host = GmailHostComposition(services, operator, transport=GmailMutationHttp(), mode=mode)
            if command == "status":
                status = services.google.restore()
                info = status.credential
                output.write(
                    f"Connection state: {status.state.value}\n"
                    f"Connection record exists: {info is not None}; provider: {'google' if info else 'none'}\n"
                    f"Connection correlation: {info.connection_id[:12] if info else 'none'}\n"
                    f"GMAIL_SEND profile available: {bool(info and info.scopes == GOOGLE_GMAIL_SEND_SCOPES and status.state in {ConnectionState.CURRENT, ConnectionState.REFRESH_REQUIRED})}\n"
                    "Mutation composition: DISABLED\n"
                )
                if services.storage.load_slot() is not None:
                    _show_action(host, output)
            elif command == "prepare-smoke-test":
                slot = host.prepare_smoke_test()
                _show_action(host, output, slot.action_id)
                output.write("READY FOR HUMAN-CONTROLLED SMOKE TEST AFTER AUDIT AND MERGE.\n")
            elif command == "review":
                host.review(arguments.action_id)
                _show_action(host, output, arguments.action_id)
            elif command == "action-status":
                _show_action(host, output, arguments.action_id)
            elif command == "send-smoke-test":
                invocation = host.send_smoke_test(arguments.action_id)
                if not invocation.result.ok and host.action_status().state in {"PREPARED", "AUTHORIZED"}:
                    output.write("DENIED: no new attempt was authorized. Inspect this same action.\n")
                _show_action(host, output, arguments.action_id)
                return 0 if invocation.result.ok else 2
            elif command == "reconcile":
                report = host.reconcile(arguments.action_id)
                if report is not None:
                    output.write(f"Reconciliation: {report.status.value.upper()}\n")
                _show_action(host, output, arguments.action_id)
                if report is not None and report.status not in {
                    ActionReconciliationStatus.SETTLED_SUCCEEDED, ActionReconciliationStatus.UNRESOLVED,
                }:
                    return 2
            return 0
    except SystemExit as exc:
        # argparse --help only. Never echo arbitrary provider exit payloads.
        if command is None and exc.code == 0:
            return 0
        output.write("FAILED: host operation interrupted; private details withheld.\n")
    except GmailHostError as exc:
        output.write(f"DENIED: {exc.reason.value}.\n")
        if exc.reason is HostFailure.UNCERTAIN:
            output.write("DO NOT SEND AGAIN. Inspect or reconcile the SAME action only.\n")
    except CredentialError as exc:
        output.write(f"DENIED: {exc.reason.value}.\n")
    except BaseException:
        # No crash traceback, provider text, callback URL or CLI input escapes.
        output.write("FAILED: host operation unavailable or interrupted; private details withheld.\n")
    if command in {"send-smoke-test", "reconcile"}:
        output.write("DO NOT SEND AGAIN. Inspect the original action's durable status before any further step.\n")
    else:
        output.write("Mutation composition remains unavailable; check private host setup.\n")
    return 2
