"""Opt-in client for the separate, installed local desktop service.

The adapter transports a closed protocol over private child-process stdio.
Only the service knows how to launch an application; no model-supplied command,
path, environment, or executable crosses this boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading

from agentcore.action_journal.adapters import ActionAdapterSpec
from agentcore.action_journal.models import ActionJournalEligibility
from agentcore.desktop.service import service_environment
from agentcore.integrations.base import Integration, IntegrationStatus
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolResult, ToolSpec


PLANNED_CAPABILITIES = frozenset({
    Capability.SCREEN_OBSERVE, Capability.INPUT_CONTROL, Capability.APP_CONTROL,
})

STATUS_TEXT = {
    "ready": "The local desktop service is ready.",
    "launch_accepted": "Windows accepted the Notepad launch. A visible window is not verified.",
    "unsupported_os": "Opening Notepad requires Windows 11.",
    "desktop_unavailable": "An active, unlocked Windows desktop is required.",
    "service_unavailable": "The local desktop service is unavailable.",
    "launch_failed": "Windows could not start Notepad successfully.",
    "launch_unknown": "Notepad may have opened; the launch could not be confirmed. Check the desktop before trying again.",
}


class OpenNotepadTool(Tool):
    spec = ToolSpec(
        name="open_notepad",
        description="Open Notepad on the local Windows 11 desktop. No arguments. "
                    "Report launch acceptance only; a visible window is not verified.",
        capabilities=frozenset({Capability.APP_CONTROL, Capability.PROCESS_SPAWN}),
        read_only=False,
    )

    def __init__(self, adapter: ComputerControlAdapter) -> None:
        self._adapter = adapter

    def run(self, args, context):
        return self._adapter.open_notepad()


class NotepadActionAdapter:
    # Opening an app is explicitly ephemeral in the ActionJournal contract.
    spec = ActionAdapterSpec(
        tool_name="open_notepad", tool_version="1.0.0",
        action_kind="desktop.open_notepad",
        eligibility=ActionJournalEligibility.EPHEMERAL_CONTROL,
    )


class ComputerControlAdapter(Integration):
    """Disabled by default; trusted host composition must opt in explicitly."""

    def __init__(self, settings: dict | None = None) -> None:
        super().__init__("computer_control", settings)
        self._tool = OpenNotepadTool(self)
        self._launch_lock = threading.Lock()
        self._uncertain = False
        self.detail = STATUS_TEXT["service_unavailable"]

    def _request(self, operation: str) -> str:
        if operation not in {"health", "open_notepad"}:
            raise ValueError("unsupported desktop operation")
        if sys.platform != "win32":
            return "unsupported_os"
        unknown = "launch_unknown" if operation == "open_notepad" else "service_unavailable"
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve().parents[1] / "desktop" / "service.py")],
                input=json.dumps({"v": 1, "operation": operation}).encode("ascii"),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, check=False, shell=False, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=service_environment(),
            )
        except OSError:
            return unknown
        except subprocess.TimeoutExpired:
            # Killing a timed-out service cannot undo a launch it already made.
            return unknown
        try:
            payload = json.loads(completed.stdout) if len(completed.stdout) <= 256 else None
            allowed = ({"ready", "unsupported_os", "desktop_unavailable", "service_unavailable"}
                       if operation == "health" else set(STATUS_TEXT) - {"ready"})
            if completed.returncode != 0 or type(payload) is not dict or set(payload) != {"v", "status"}:
                return unknown
            if type(payload["v"]) is not int or payload["v"] != 1 or payload["status"] not in allowed:
                return unknown
            return payload["status"]
        except (ValueError, TypeError):
            return unknown

    def connect(self) -> IntegrationStatus:
        category = self._request("health") if self.settings.get("enabled") is True else "service_unavailable"
        self.detail = STATUS_TEXT[category]
        self.status = IntegrationStatus.CONNECTED if category == "ready" else IntegrationStatus.UNAVAILABLE
        return self.status

    def provided_tools(self):
        return (self._tool,) if self.status is IntegrationStatus.CONNECTED else ()

    def open_notepad(self) -> ToolResult:
        with self._launch_lock:
            if self._uncertain:
                category = "launch_unknown"
            elif self.status is not IntegrationStatus.CONNECTED:
                category = "service_unavailable"
            else:
                category = self._request("open_notepad")
            self._uncertain = self._uncertain or category == "launch_unknown"
            if category == "launch_accepted":
                return ToolResult.success(STATUS_TEXT[category], kind=category)
            return ToolResult.failure(STATUS_TEXT[category], kind=category, retryable=False)
