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
import win32security


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


def open_child(parent, name, *, directory=False, create=False, read=False,
               _update=False, _security=None):
    text = c.create_unicode_buffer(name)
    length = len(name.encode("utf-16-le", "strict"))
    unicode = _Unicode(length, length + 2, c.cast(text, w.LPWSTR))
    security_buffer = c.create_string_buffer(_security) if _security is not None else None
    attributes = _Attributes(c.sizeof(_Attributes), int(parent), c.pointer(unicode), 0x40,
        c.cast(security_buffer, w.LPVOID) if security_buffer is not None else None, None)
    handle, io = w.HANDLE(), _IO()
    access = 0x00100000 | 0x80  # SYNCHRONIZE | FILE_READ_ATTRIBUTES
    if directory or read:
        access |= win32con.GENERIC_READ
    if create and not directory:
        access |= win32con.GENERIC_READ | win32con.GENERIC_WRITE | 0x10000  # DELETE for rename
    if _update:
        # No privilege is enabled here. Missing current ACCESS_SYSTEM_SECURITY
        # fails closed before a snapshot or any staging/content publication.
        access |= 0x01000000 | 0x20000
        if create:
            access |= 0x40000 | 0x80000  # WRITE_DAC | WRITE_OWNER
    # Directory handles exclude delete sharing. Inspection handles permit
    # cooperating access; the newly created staging handle excludes writers.
    share = 3 if directory else (1 if create or read else 7)
    options = 0x00200000 | 0x20 | (1 if directory else 0)  # OPEN_REPARSE | SYNCHRONOUS | DIRECTORY
    try:
        _check(_open(c.byref(handle), access, c.byref(attributes), c.byref(io), None,
                     0x10 if directory else 0x80, share, 2 if create else 1, options, None, 0))
        return handle.value
    except BaseException:
        if handle.value:
            close(handle.value)
        raise


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


def final_name(handle):
    # Private kernel observation only. Never expose this absolute host path.
    return win32file.GetFinalPathNameByHandle(handle, 0).rsplit("\\", 1)[-1]


def has_staging_name(handle, name):
    return final_name(handle) == name


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


_query = _nt.NtQueryInformationFile
_query.argtypes = (w.HANDLE, c.POINTER(_IO), w.LPVOID, w.ULONG, w.ULONG)
_query.restype = w.LONG


def _no_extra_data(handle):
    # FileEaInformation and bounded FileStreamInformation; never silently lose
    # an EA or alternate stream when replacing the object's default stream.
    ea, io = w.ULONG(), _IO()
    _check(_query(int(handle), c.byref(io), c.byref(ea), c.sizeof(ea), 7))
    if ea.value:
        raise ValueError("unsupported_file_metadata")
    buffer = c.create_string_buffer(4096)
    _check(_query(int(handle), c.byref(io), buffer, len(buffer), 22))
    raw = buffer.raw[:io.Information]
    if len(raw) < 24 or int.from_bytes(raw[:4], "little") != 0:
        raise ValueError("unsupported_file_metadata")
    length = int.from_bytes(raw[4:8], "little")
    if length > len(raw) - 24 or raw[24:24 + length].decode("utf-16-le") != "::$DATA":
        raise ValueError("unsupported_file_metadata")


def _update_security(handle):
    # Full descriptor, including the complete SACL, under already-granted OS
    # access. Partial owner/DACL queries are never treated as full snapshots.
    sd = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT, 0x10000)
    raw = bytes(sd)
    if len(raw) > 16384 or sd.GetSecurityDescriptorDacl() is None:
        raise ValueError("unsupported_file_metadata")
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        owners = (win32security.GetTokenInformation(token, win32security.TokenUser)[0],
                  win32security.GetTokenInformation(token, win32security.TokenOwner))
    finally:
        token.Close()
    if sd.GetSecurityDescriptorOwner() not in owners:
        raise ValueError("unsupported_file_metadata")
    return raw.hex()


def open_update_target(parent, name):
    return open_child(parent, name, read=True, _update=True)


def update_facts(handle):
    attrs, identity, size, links = facts(handle)
    # Refuse readonly/system, encrypted, compressed, reparse, sparse/offline,
    # integrity/recall and other unusual state rather than discarding it.
    if not regular(handle) or links != 1 or attrs & ~(0x20 | 0x2 | 0x80 | 0x2000):
        raise ValueError("unsupported_file_metadata")
    _no_extra_data(handle)
    basic, io = _Basic(), _IO()
    _check(_query(int(handle), c.byref(io), c.byref(basic), c.sizeof(basic), 4))
    return {"identity": list(identity), "size": size,
            "metadata": {"attributes": attrs, "security": _update_security(handle)},
            "change": [basic.CreationTime, basic.LastWriteTime, basic.ChangeTime]}


def create_update_stage(parent, name, metadata):
    # Supply the target descriptor at exclusive creation, before content can
    # ever exist under a broader inherited ACL. Then preserve/verify all ACLs.
    opened = open_child(parent, name, create=True, _update=True,
                        _security=bytes.fromhex(metadata["security"]))
    try:
        sd = win32security.SECURITY_DESCRIPTOR(bytes.fromhex(metadata["security"]))
        control = sd.GetSecurityDescriptorControl()[0]
        flags = 1 | 2 | 4 | 8
        flags |= 0x80000000 if control & 0x1000 else 0x20000000
        flags |= 0x40000000 if control & 0x2000 else 0x10000000
        win32security.SetKernelObjectSecurity(opened, flags, sd)
        if _update_security(opened) != metadata["security"]:
            raise ValueError("unsupported_file_metadata")
    except BaseException:
        try:
            discard(opened)
        finally:
            close(opened)
        raise
    return opened


class _Basic(c.Structure):
    _fields_ = [("CreationTime", c.c_longlong), ("LastAccessTime", c.c_longlong),
                ("LastWriteTime", c.c_longlong), ("ChangeTime", c.c_longlong),
                ("FileAttributes", w.ULONG)]


def preserve_update_metadata(handle, metadata):
    value, io = _Basic(0, 0, 0, 0, metadata["attributes"]), _IO()
    _check(_set(int(handle), c.byref(io), c.byref(value), c.sizeof(value), 4))
    if update_facts(handle)["metadata"] != metadata:
        raise ValueError("unsupported_file_metadata")


def publish_update(handle, parent, name):
    encoded = name.encode("utf-16-le", "strict")
    buffer = c.create_string_buffer(c.sizeof(_Rename) + len(encoded))
    value = _Rename.from_buffer(buffer)
    value.ReplaceIfExists, value.RootDirectory, value.FileNameLength = 1, int(parent), len(encoded)
    c.memmove(c.addressof(buffer) + _Rename.FileName.offset, encoded, len(encoded))
    io = _IO()
    _check(_set(int(handle), c.byref(io), buffer, len(buffer), 10))
