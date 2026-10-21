"""One-request local service, launched by the adapter over private stdio.

This file also runs in Python isolated mode without importing AgentCore's
conversation/model runtime. It has no listener, shell, configurable program,
file argument, elevation, or cross-session launch mechanism.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def service_environment():
    """Do not pass model credentials or Python startup hooks to child processes."""
    allowed = {"systemroot", "windir", "temp", "tmp", "userprofile", "localappdata",
               "appdata", "programdata", "programfiles", "programfiles(x86)",
               "commonprogramfiles", "commonprogramfiles(x86)"}
    return {key: value for key, value in os.environ.items() if key.lower() in allowed}


def _desktop_available():
    import win32con
    import win32service
    import win32ts

    session = win32ts.ProcessIdToSessionId(os.getpid())
    if session == 0 or win32ts.WTSQuerySessionInformation(
            win32ts.WTS_CURRENT_SERVER_HANDLE, session, win32ts.WTSConnectState) != win32ts.WTSActive:
        return False
    station = win32service.GetProcessWindowStation()
    if win32service.GetUserObjectInformation(station, win32con.UOI_NAME).lower() != "winsta0":
        return False
    desktop = win32service.OpenInputDesktop(0, False, win32con.DESKTOP_READOBJECTS)
    try:
        return win32service.GetUserObjectInformation(desktop, win32con.UOI_NAME).lower() == "default"
    finally:
        desktop.CloseDesktop()


def availability():
    if sys.platform != "win32":
        return "unsupported_os"
    version = sys.getwindowsversion()
    major, _, build = version.platform_version
    if major != 10 or build < 22000 or version.product_type != 1:
        return "unsupported_os"
    try:
        return "ready" if _desktop_available() else "desktop_unavailable"
    except ImportError:
        return "service_unavailable"
    except Exception:
        return "desktop_unavailable"


def _launch_notepad():
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32process

    directory = win32api.GetSystemDirectory()
    startup = win32process.STARTUPINFO()
    startup.lpDesktop = "winsta0\\default"
    startup.dwFlags = win32con.STARTF_USESHOWWINDOW
    startup.wShowWindow = win32con.SW_SHOWNORMAL
    try:
        process, thread, _, _ = win32process.CreateProcess(
            str(Path(directory) / "notepad.exe"), None, None, None, False,
            win32con.CREATE_UNICODE_ENVIRONMENT, service_environment(), directory, startup,
        )
    except (OSError, pywintypes.error):
        return "launch_failed"
    try:
        try:
            # Modern Notepad may hand off to another process and exit successfully.
            # shortcut: process acceptance is not window visibility, verify live on the user's desktop.
            waited = win32event.WaitForSingleObject(process, 1000)
            if waited not in (win32con.WAIT_OBJECT_0, win32con.WAIT_TIMEOUT):
                return "launch_unknown"
            if waited == win32con.WAIT_OBJECT_0 and win32process.GetExitCodeProcess(process) != 0:
                return "launch_unknown"
            return "launch_accepted"
        finally:
            try:
                thread.Close()
            finally:
                process.Close()
    except Exception:
        return "launch_unknown"


def handle_request(request):
    if (type(request) is not dict or set(request) != {"v", "operation"}
            or type(request["v"]) is not int or request["v"] != 1
            or request["operation"] not in ("health", "open_notepad")):
        return "invalid_request"
    status = availability()
    if status != "ready" or request["operation"] == "health":
        return status
    try:
        return _launch_notepad()
    except ImportError:
        return "service_unavailable"
    except OSError:
        return "launch_failed"


def main():
    try:
        raw = sys.stdin.buffer.read(257)
        status = handle_request(json.loads(raw)) if len(raw) <= 256 else "invalid_request"
    except (ValueError, TypeError):
        status = "invalid_request"
    except Exception:
        # Unexpected failure may follow physical entry; never infer non-application.
        status = "launch_unknown"
    print(json.dumps({"v": 1, "status": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
