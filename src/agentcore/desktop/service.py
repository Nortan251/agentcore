"""One-request local service, launched by the adapter over private stdio.

This file also runs in Python isolated mode without importing AgentCore's
conversation/model runtime. It has no listener, shell, configurable program,
file argument, elevation, or cross-session launch mechanism.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
import sys


APP_NAMES = {
    "notepad.exe": "Notepad",
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "code.exe": "Visual Studio Code",
    "explorer.exe": "File Explorer",
    "windowsterminal.exe": "Windows Terminal",
}
OBSERVATION_STATUSES = frozenset({
    "observed", "unknown", "inaccessible", "desktop_unavailable",
    "unsupported_os", "service_unavailable",
})
UNKNOWN_REASONS = frozenset({
    "no_foreground_window", "window_process_unavailable", "executable_not_allowlisted",
    "shared_application_host", "console_host", "image_query_invalid", "explorer_shell",
    "foreground_changed", "foreground_process_exited",
})


def decode_message(raw):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate protocol key")
        return result
    if type(raw) is not bytes or len(raw) > 256:
        raise ValueError("invalid protocol size")
    return json.loads(raw, object_pairs_hook=unique_object)


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


def _process_identity(process):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x800)
    query = kernel.QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                      ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buffer))
    if not query(int(process), 0, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not 0 < size.value < len(buffer):
        return None, "image_query_invalid"
    # This is an executable-name label, never publisher authentication or authority.
    name = PureWindowsPath(buffer.value).name.casefold()
    app = APP_NAMES.get(name)
    reason = {"applicationframehost.exe": "shared_application_host",
              "openconsole.exe": "console_host", "conhost.exe": "console_host"}.get(name, "executable_not_allowlisted")
    return app, None if app else reason


def _foreground_app():
    import win32api
    import win32con
    import win32gui
    import win32process
    import win32security
    import win32ts

    window = win32gui.GetForegroundWindow()
    if not window:
        return {"status": "unknown", "app": None, "reason": "no_foreground_window"}
    thread, pid = win32process.GetWindowThreadProcessId(window)
    if not thread or not pid:
        return {"status": "unknown", "app": None, "reason": "window_process_unavailable"}
    # Query only this process, with no memory-read, control or privilege rights.
    process = win32api.OpenProcess(0x1000, False, pid)
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        try:
            if win32security.GetTokenInformation(token, win32security.TokenSessionId) != win32ts.ProcessIdToSessionId(os.getpid()):
                return {"status": "inaccessible", "app": None}
        finally:
            token.Close()
        app, reason = _process_identity(process)
        if app == "File Explorer" and win32gui.GetClassName(window) not in {"CabinetWClass", "ExploreWClass"}:
            app = None
            reason = "explorer_shell"
        # Refuse a changed/locked desktop or a focus change instead of guessing.
        if not _desktop_available():
            return {"status": "desktop_unavailable", "app": None}
        current_thread, current_pid = win32process.GetWindowThreadProcessId(window)
        if (win32gui.GetForegroundWindow() != window
                or current_thread != thread or current_pid != pid):
            return {"status": "unknown", "app": None, "reason": "foreground_changed"}
        if win32process.GetExitCodeProcess(process) != 259:
            return {"status": "unknown", "app": None, "reason": "foreground_process_exited"}
    finally:
        process.Close()
    return {"status": "observed", "app": app} if app else {"status": "unknown", "app": None, "reason": reason}


def handle_request(request):
    if (type(request) is not dict or set(request) != {"v", "operation"}
            or type(request["v"]) is not int or request["v"] != 1
            or type(request["operation"]) is not str
            or request["operation"] not in ("health", "open_notepad", "get_foreground_app")):
        return "invalid_request"
    status = availability()
    if request["operation"] == "get_foreground_app":
        if status != "ready":
            return {"status": status, "app": None}
        try:
            return _foreground_app()
        except ImportError:
            return {"status": "service_unavailable", "app": None}
        except Exception:
            # Native errors may contain private process information; never render them.
            return {"status": "inaccessible", "app": None}
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
        status = handle_request(decode_message(raw))
    except (ValueError, TypeError):
        status = "invalid_request"
    except Exception:
        # Unexpected failure may follow physical entry; never infer non-application.
        status = "launch_unknown"
    print(json.dumps({"v": 1, **status} if type(status) is dict else {"v": 1, "status": status}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
