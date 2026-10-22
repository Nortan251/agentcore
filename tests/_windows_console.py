"""Run offline console regressions in ConPTY, without a desktop input device."""

import ctypes as c
from ctypes import wintypes as w
import subprocess
import threading
import time


def run_console(command, *, ready, answers, timeout=20):
    """Keep real console handles: redirected stdin misses CPython's prompt path."""
    kernel = c.WinDLL("kernel32", use_last_error=True)

    class Coord(c.Structure):
        _fields_ = [("X", w.SHORT), ("Y", w.SHORT)]

    class Startup(c.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                    ("lpTitle", w.LPWSTR)] + [(name, w.DWORD) for name in
                    ("dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")] + [
                    ("wShowWindow", w.WORD), ("cbReserved2", w.WORD), ("lpReserved2", c.c_void_p),
                    ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]

    class StartupEx(c.Structure):
        _fields_ = [("StartupInfo", Startup), ("lpAttributeList", c.c_void_p)]

    class Process(c.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                    ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

    signatures = {
        "CreatePipe": ([c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), c.c_void_p, w.DWORD], w.BOOL),
        "CreatePseudoConsole": ([Coord, w.HANDLE, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)], c.c_long),
        "ClosePseudoConsole": ([w.HANDLE], None),
        "InitializeProcThreadAttributeList": ([c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
        "UpdateProcThreadAttribute": ([c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p], w.BOOL),
        "DeleteProcThreadAttributeList": ([c.c_void_p], None),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                            c.c_void_p, w.LPCWSTR, c.POINTER(StartupEx), c.POINTER(Process)], w.BOOL),
        "ReadFile": ([w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p], w.BOOL),
        "WriteFile": ([w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p], w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result

    def check(ok):
        if not ok:
            raise c.WinError(c.get_last_error())

    input_read, input_write, output_read, output_write, console = (w.HANDLE() for _ in range(5))
    process, attributes, reader = Process(), None, None
    chunks = []
    try:
        check(kernel.CreatePipe(c.byref(input_read), c.byref(input_write), None, 0))
        check(kernel.CreatePipe(c.byref(output_read), c.byref(output_write), None, 0))
        assert kernel.CreatePseudoConsole(Coord(240, 40), input_read, output_write, 0, c.byref(console)) == 0
        size = c.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        attributes = c.create_string_buffer(size.value)
        check(kernel.InitializeProcThreadAttributeList(attributes, 1, 0, c.byref(size)))
        # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE attaches only this isolated terminal.
        check(kernel.UpdateProcThreadAttribute(attributes, 0, 0x00020016, console, c.sizeof(console), None, None))
        startup = StartupEx()
        startup.StartupInfo.cb = c.sizeof(startup)
        # Null explicit handles prevent inheriting pytest's redirected streams.
        startup.StartupInfo.dwFlags = 0x00000100
        startup.lpAttributeList = c.cast(attributes, c.c_void_p)
        check(kernel.CreateProcessW(None, c.create_unicode_buffer(subprocess.list2cmdline(command)),
                                   None, None, False, 0x00080000, None, None, c.byref(startup), c.byref(process)))
        for handle in (input_read, output_write):
            check(kernel.CloseHandle(handle))
            handle.value = None

        def drain():
            buffer, count = c.create_string_buffer(4096), w.DWORD()
            while kernel.ReadFile(output_read, buffer, len(buffer), c.byref(count), None) and count.value:
                chunks.append(buffer.raw[:count.value])

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline, sent = time.monotonic() + timeout, False
        while kernel.WaitForSingleObject(process.hProcess, 0) == 258:
            assert time.monotonic() < deadline, b"".join(chunks).decode("utf-8", errors="replace")
            if not sent and ready.encode() in b"".join(chunks):
                data, count = answers.encode(), w.DWORD()
                check(kernel.WriteFile(input_write, data, len(data), c.byref(count), None))
                assert count.value == len(data)
                sent = True
            time.sleep(0.01)
        code = w.DWORD()
        check(kernel.GetExitCodeProcess(process.hProcess, c.byref(code)))
    finally:
        if process.hProcess and kernel.WaitForSingleObject(process.hProcess, 0) == 258:
            kernel.TerminateProcess(process.hProcess, 1)
            kernel.WaitForSingleObject(process.hProcess, 5000)
        if console:
            kernel.ClosePseudoConsole(console)
        if reader:
            reader.join(5)
        if attributes is not None:
            kernel.DeleteProcThreadAttributeList(attributes)
        for handle in (process.hThread, process.hProcess, input_read, input_write, output_read, output_write):
            if handle:
                kernel.CloseHandle(handle)
    assert sent, b"".join(chunks).decode("utf-8", errors="replace")
    return code.value, b"".join(chunks).decode("utf-8", errors="replace")
