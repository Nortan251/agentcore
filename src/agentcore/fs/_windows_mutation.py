"""Small NT handle-relative boundary. Never resolve a model path via Win32 DOS names.

NtCreateFile opens a single component relative to an inspected directory handle.
NtSetInformationFile publishes with ReplaceIfExists=False and a pinned parent.
Unsupported native/filesystem operations fail closed; there is no path fallback.
"""

import ctypes as c
from ctypes import wintypes as w

import win32api
import win32con
import win32file


class _Unicode(c.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]


class _Attributes(c.Structure):
    _fields_ = [("Length", w.ULONG), ("RootDirectory", w.HANDLE),
                ("ObjectName", c.POINTER(_Unicode)), ("Attributes", w.ULONG),
                ("SecurityDescriptor", w.LPVOID), ("SecurityQualityOfService", w.LPVOID)]


class _IO(c.Structure):
    _fields_ = [("Status", c.c_void_p), ("Information", c.c_size_t)]


class _Rename(c.Structure):
    _fields_ = [("ReplaceIfExists", c.c_ubyte), ("RootDirectory", w.HANDLE),
                ("FileNameLength", w.ULONG), ("FileName", w.WCHAR * 1)]


_nt = c.WinDLL("ntdll")
_open = _nt.NtCreateFile
_open.argtypes = (c.POINTER(w.HANDLE), w.ULONG, c.POINTER(_Attributes), c.POINTER(_IO),
                  w.LPVOID, w.ULONG, w.ULONG, w.ULONG, w.ULONG, w.LPVOID, w.ULONG)
_open.restype = w.LONG
_set = _nt.NtSetInformationFile
_set.argtypes = (w.HANDLE, c.POINTER(_IO), w.LPVOID, w.ULONG, w.ULONG)
_set.restype = w.LONG
_error = _nt.RtlNtStatusToDosError
_error.argtypes, _error.restype = (w.LONG,), w.ULONG


def _check(status):
    if status < 0:
        raise c.WinError(_error(status))


def root(path):
    return win32file.CreateFile(str(path), win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None, win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)


def open_child(parent, name, *, directory=False, create=False, read=False):
    text = c.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le", "strict"))
    unicode = _Unicode(length, length + 2, c.cast(text, w.LPWSTR))
    attributes = _Attributes(c.sizeof(_Attributes), int(parent), c.pointer(unicode), 0x40, None, None)
    handle, io = w.HANDLE(), _IO()
    access = 0x00100000 | 0x80  # SYNCHRONIZE | FILE_READ_ATTRIBUTES
    if directory or read:
        access |= win32con.GENERIC_READ
    if create:
        access |= win32con.GENERIC_READ | win32con.GENERIC_WRITE | 0x10000  # DELETE for rename
    # Directory handles exclude delete sharing. Inspection handles permit
    # cooperating access; the newly created staging handle excludes writers.
    share = 3 if directory else (1 if create or read else 7)
    options = 0x00200000 | 0x20 | (1 if directory else 0)  # OPEN_REPARSE | SYNCHRONOUS | DIRECTORY
    _check(_open(c.byref(handle), access, c.byref(attributes), c.byref(io), None,
                 0x80, share, 2 if create else 1, options, None, 0))
    return handle.value


def close(handle):
    win32api.CloseHandle(handle)


def facts(handle):
    info = win32file.GetFileInformationByHandle(handle)
    return info[0], (info[4], info[8], info[9]), (info[5] << 32) | info[6], info[7]


def publish(handle, parent, name):
    encoded = name.encode("utf-16-le", "strict")
    buffer = c.create_string_buffer(c.sizeof(_Rename) + len(encoded))
    value = _Rename.from_buffer(buffer)
    value.ReplaceIfExists, value.RootDirectory, value.FileNameLength = 0, int(parent), len(encoded)
    c.memmove(c.addressof(buffer) + _Rename.FileName.offset, encoded, len(encoded))
    io = _IO()
    _check(_set(int(handle), c.byref(io), buffer, len(buffer), 10))  # FileRenameInformation


def discard(handle):
    # Delete the owned staging object by handle, never by a re-resolved name.
    delete, io = c.c_ubyte(1), _IO()
    _check(_set(int(handle), c.byref(io), c.byref(delete), c.sizeof(delete), 13))


def has_staging_name(handle, name):
    # Private kernel observation only. Never expose this absolute host path.
    return win32file.GetFinalPathNameByHandle(handle, 0).rsplit("\\", 1)[-1] == name


def write(handle, data):
    _, count = win32file.WriteFile(handle, data)
    if count != len(data):
        raise OSError("short staging write")


def flush(handle):
    win32file.FlushFileBuffers(handle)


def read(handle, size):
    return win32file.ReadFile(handle, size)[1]


def regular(handle):
    return win32file.GetFileType(handle) == win32file.FILE_TYPE_DISK
