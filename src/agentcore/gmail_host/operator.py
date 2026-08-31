"""Private terminal input/output, separate from logging and ordinary results."""

from __future__ import annotations

import os
import sys
from typing import Protocol

from agentcore.credentials.models import RuntimeOnly
from agentcore.gmail_host.errors import GmailHostError, HostFailure


class PrivateOperator(Protocol):
    """Injected only by trusted host code; never a tool or CLI parameter."""

    def require_interactive(self) -> None: ...
    def write_private(self, text: str) -> None: ...
    def read_private(self, prompt: str, *, maximum: int) -> str: ...


class PrivateConsole(RuntimeOnly):
    """Windows console only. No pipe, redirected review, echo or input history."""

    def __init__(self) -> None:
        self._pid = os.getpid()

    def require_interactive(self) -> None:
        if self._pid != os.getpid():
            raise GmailHostError(HostFailure.OWNERSHIP)
        if os.name != "nt" or any(
            stream is not original or not stream.isatty()
            for stream, original in (
                (sys.stdin, sys.__stdin__), (sys.stdout, sys.__stdout__),
                (sys.stderr, sys.__stderr__),
            )
        ):
            raise GmailHostError(HostFailure.PRIVATE_TERMINAL)

    def write_private(self, text: str) -> None:
        self.require_interactive()
        sys.stderr.write(text)
        sys.stderr.flush()

    def read_private(self, prompt: str, *, maximum: int) -> str:
        self.require_interactive()
        import msvcrt

        self.write_private(prompt)
        characters: list[str] = []
        while True:
            self.require_interactive()
            character = msvcrt.getwch()
            if character in ("\r", "\n"):
                self.write_private("\n")
                return "".join(characters)
            if character == "\x03":
                raise KeyboardInterrupt()
            if character == "\b":
                if characters:
                    characters.pop()
                continue
            if character in ("\x00", "\xe0"):
                msvcrt.getwch()  # discard the rest of a console function key
                continue
            if ord(character) < 32 or ord(character) == 127 or len(characters) >= maximum:
                raise GmailHostError(HostFailure.INVALID_INPUT)
            characters.append(character)


def show_review(operator: PrivateOperator, slot, review: dict) -> None:
    """Only the foundation's authenticated, detached review reaches this UI."""
    operator.require_interactive()
    message = review["message"]
    operator.write_private(
        "\nBEGIN PRIVATE PREPARED MESSAGE\n"
        "THIS WILL SEND A REAL EMAIL IF EXECUTED\n"
        f"Action: {slot.action_id}\n"
        f"Preparation: {slot.preparation.preparation_id}\n"
        f"Authenticated primary sender: {review['sender']}\n"
        f"To: {', '.join(message['to'])}\n"
        "Cc: NONE\nBcc: NONE\nFormat: plain text; no attachments; no reply/forward\n"
        f"Subject: {message['subject']}\n"
        f"Body (exact normalized text, including final line ending):\n{message['body']}"
        f"Message-ID: {review['message_id']}\n"
        f"Date: {review['date']}\n"
        "END PRIVATE PREPARED MESSAGE\n"
    )
