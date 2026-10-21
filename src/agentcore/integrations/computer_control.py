"""Opt-in client for the separate, installed local desktop service.

The adapter transports a closed protocol over private child-process stdio.
Only the service knows how to launch an application; no model-supplied command,
path, environment, or executable crosses this boundary.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
import subprocess
import sys
import threading

from agentcore.action_journal.adapters import ActionAdapterSpec
from agentcore.action_journal.models import ActionJournalEligibility
from agentcore.desktop.service import APP_NAMES, OBSERVATION_STATUSES, UNKNOWN_REASONS, decode_message, service_environment
from agentcore.integrations.base import Integration, IntegrationStatus
from agentcore.permissions.levels import Capability
from agentcore.tools.base import ExternalReadTool, Tool, ToolResult, ToolSpec


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


OBSERVATION_TEXT = {
    "unsupported_os": "Foreground observation requires Windows 11.",
    "desktop_unavailable": "An active, unlocked Windows desktop is required.",
    "service_unavailable": "The foreground observation could not be obtained from the local desktop service.",
    "inaccessible": "The foreground application is inaccessible; its identity could not be observed.",
    "unknown": "The foreground application could not be identified safely.",
}


def _run_service(command, *, input, timeout, **kwargs):
    """Bound captured stdout before decoding, including a nonterminating service."""
    with subprocess.Popen(command, stdin=subprocess.PIPE, **kwargs) as process:
        with ThreadPoolExecutor(max_workers=1) as pool:
            def exchange():
                process.stdin.write(input)
                process.stdin.close()
                output = process.stdout.read(257)
                if len(output) > 256:
                    process.kill()
                return subprocess.CompletedProcess(command, process.wait(), output)
            future = pool.submit(exchange)
            try:
                return future.result(timeout=timeout)
            except TimeoutError:
                raise subprocess.TimeoutExpired("desktop service", timeout) from None
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()


class GetForegroundAppTool(ExternalReadTool):
    spec = ToolSpec(
        name="get_foreground_app",
        description="Observe the foreground application on the local Windows 11 desktop now. "
                    "No arguments. Returns a fixed app label or a truthful unavailable/unknown status.",
        capabilities=frozenset({Capability.SCREEN_OBSERVE}),
        read_only=True,
        tags=("private_arguments",),
    )

    def __init__(self, adapter: ComputerControlAdapter) -> None:
        self._adapter = adapter

    def run(self, args, context):
        return self._adapter.get_foreground_app()


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
        self._foreground_tool = GetForegroundAppTool(self)
        self._launch_lock = threading.Lock()
        self._uncertain = False
        self.detail = STATUS_TEXT["service_unavailable"]

    def _request(self, operation: str) -> str | dict:
        if operation not in {"health", "open_notepad", "get_foreground_app"}:
            raise ValueError("unsupported desktop operation")
        observation = operation == "get_foreground_app"
        if sys.platform != "win32":
            return {"status": "unsupported_os", "app": None} if observation else "unsupported_os"
        unknown = "launch_unknown" if operation == "open_notepad" else "service_unavailable"
        fallback = {"status": unknown, "app": None} if observation else unknown
        try:
            completed = _run_service(
                [sys.executable, "-I", str(Path(__file__).resolve().parents[1] / "desktop" / "service.py")],
                input=json.dumps({"v": 1, "operation": operation}).encode("ascii"),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, shell=False, close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=service_environment(),
            )
        except OSError:
            return fallback
        except subprocess.TimeoutExpired:
            # Killing a timed-out service cannot undo a launch it already made.
            return fallback
        try:
            payload = decode_message(completed.stdout)
            if observation:
                if (completed.returncode != 0 or type(payload) is not dict
                        or set(payload) not in ({"v", "status", "app"}, {"v", "status", "app", "reason"})
                        or type(payload["v"]) is not int or payload["v"] != 1
                        or type(payload["status"]) is not str
                        or payload["status"] not in OBSERVATION_STATUSES):
                    return fallback
                if payload["status"] == "observed":
                    if type(payload["app"]) is not str or payload["app"] not in APP_NAMES.values():
                        return fallback
                elif payload["app"] is not None:
                    return fallback
                result = {"status": payload["status"], "app": payload["app"]}
                if "reason" in payload:
                    if (payload["status"] != "unknown" or type(payload["reason"]) is not str
                            or payload["reason"] not in UNKNOWN_REASONS):
                        return fallback
                    result["reason"] = payload["reason"]
                return result
            allowed = ({"ready", "unsupported_os", "desktop_unavailable", "service_unavailable"}
                       if operation == "health" else set(STATUS_TEXT) - {"ready"})
            if completed.returncode != 0 or type(payload) is not dict or set(payload) != {"v", "status"}:
                return unknown
            if type(payload["v"]) is not int or payload["v"] != 1 or payload["status"] not in allowed:
                return unknown
            return payload["status"]
        except (ValueError, TypeError):
            return fallback

    def connect(self) -> IntegrationStatus:
        category = self._request("health") if self.settings.get("enabled") is True else "service_unavailable"
        self.detail = STATUS_TEXT[category]
        self.status = IntegrationStatus.CONNECTED if category == "ready" else IntegrationStatus.UNAVAILABLE
        return self.status

    def provided_tools(self):
        return (self._tool, self._foreground_tool) if self.status is IntegrationStatus.CONNECTED else ()

    def get_foreground_app(self) -> ToolResult:
        result = (self._request("get_foreground_app")
                  if self.settings.get("enabled") is True and self.status is IntegrationStatus.CONNECTED
                  else {"status": "service_unavailable", "app": None})
        category = result["status"]
        if category in {"observed", "unknown"}:
            metadata = {"kind": category}
            if "reason" in result:
                metadata["observation_reason"] = result["reason"]
            return ToolResult.success({"status": category, "app": result["app"]}, **metadata)
        return ToolResult.failure(OBSERVATION_TEXT[category], kind=category, retryable=False)

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
