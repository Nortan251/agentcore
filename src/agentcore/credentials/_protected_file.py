"""One exclusively owned, bounded ciphertext file; no OAuth or token parsing.

The directory is trusted host configuration. Cooperating writers are excluded
by an OS lock; replacement is detected before accepting a read or commit.
DPAPI/ACLs do not defend against a malicious process running as the same user.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import RuntimeOnly


MAX_FILE_BYTES = 4 * 1024 * 1024
RECORD_NAME = "connection.dpapi"
_LOCK_NAME = ".credential-owner"


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _ordinary(info: os.stat_result, *, directory: bool = False) -> bool:
    return (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) and not (
        getattr(info, "st_file_attributes", 0) & 0x400) and (directory or info.st_nlink == 1)


def _windows_private_directory(path: Path) -> None:
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        default_owner = win32security.GetTokenInformation(token, win32security.TokenOwner)
    finally:
        token.Close()
    descriptor = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION)
    owner = descriptor.GetSecurityDescriptorOwner()
    # Windows may create objects with the token's default owner group (e.g.
    # Administrators on an elevated desktop/CI host), rather than TokenUser.
    # Accept only those two token-authorized owners, and make the private
    # directory explicitly user-owned without enabling any extra privilege.
    if owner != user and owner != default_owner:
        raise CredentialError(CredentialFailure.UNAVAILABLE)
    acl = win32security.ACL()
    inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, ntsecuritycon.FILE_ALL_ACCESS, user)
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, ntsecuritycon.FILE_ALL_ACCESS, system)
    security = win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION
    replacement_owner = None
    if owner != user:
        security |= win32security.OWNER_SECURITY_INFORMATION
        replacement_owner = user
    win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                      security, replacement_owner, None, acl, None)


class ProtectedFile(RuntimeOnly):
    """Keep ownership handles open until close; PID fencing precedes all I/O."""

    def __init__(self, directory: Path, *, record_name: str = RECORD_NAME) -> None:
        # The host reuses the same pinned, private atomic-file boundary for
        # its inert continuity record. Never accept a path or the owner file.
        if type(record_name) is not str or record_name not in {
                RECORD_NAME, "gmail-host.json", "workspace-workflows.json"}:
            raise CredentialError(CredentialFailure.INVALID)
        self._record_name = record_name
        self._owner_pid = os.getpid()
        self._closed = False
        self.poisoned = False
        self.directory = Path(os.path.abspath(directory))
        self._handles: list = []
        self._fd: int | None = None
        self._directories: list[tuple[Path, tuple[int, int]]] = []
        self._fingerprint: bytes | None = None
        if os.name == "nt" and (self.directory.drive.startswith("\\\\") or ":" in str(self.directory)[2:]):
            raise CredentialError(CredentialFailure.INVALID)
        try:
            for path in (*reversed(self.directory.parents), self.directory):
                # Inspect/pin each parent before creating the next component;
                # do not mkdir through an unchecked junction or symlink.
                try:
                    path.lstat()
                except FileNotFoundError:
                    path.mkdir(mode=0o700)
                info = path.lstat()
                if not _ordinary(info, directory=True):
                    raise CredentialError(CredentialFailure.STORAGE_CHANGED)
                if os.name == "nt":
                    import ntsecuritycon
                    import win32con
                    import win32file

                    handle = win32file.CreateFile(
                        str(path), ntsecuritycon.FILE_READ_ATTRIBUTES,
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None,
                        win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
                    self._handles.append(handle)
                    if win32file.GetFileInformationByHandle(handle)[0] & 0x400:
                        raise CredentialError(CredentialFailure.STORAGE_CHANGED)
                if _identity(path.lstat()) != _identity(info):
                    raise CredentialError(CredentialFailure.STORAGE_CHANGED)
                self._directories.append((path, _identity(info)))
            self.had_owner = (self.directory / _LOCK_NAME).exists()
            if os.name == "nt":
                _windows_private_directory(self.directory)
                self._acquire_windows()
            else:
                if self.directory.stat().st_uid != os.getuid():
                    raise CredentialError(CredentialFailure.UNAVAILABLE)
                self.directory.chmod(0o700)
                self._acquire_posix()
            existing = self._read()
            self._fingerprint = hashlib.sha256(existing).digest() if existing is not None else None
        except BaseException:
            self.close()
            raise

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._closed or self.poisoned:
            raise CredentialError(CredentialFailure.UNAVAILABLE)

    def _acquire_windows(self) -> None:
        import win32con
        import win32file

        handle = win32file.CreateFile(
            str(self.directory / _LOCK_NAME), win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0, None, win32con.OPEN_ALWAYS, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
        self._handles.append(handle)
        info = (self.directory / _LOCK_NAME).lstat()
        if not _ordinary(info):
            raise CredentialError(CredentialFailure.STORAGE_CHANGED)
        self._lock_identity = _identity(info)

    def _acquire_posix(self) -> None:
        import fcntl

        self._fd = os.open(self.directory / _LOCK_NAME, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if not _ordinary(os.fstat(self._fd)):
            raise CredentialError(CredentialFailure.STORAGE_CHANGED)
        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._lock_identity = _identity(os.fstat(self._fd))

    def _check_paths(self) -> None:
        self._owner()
        for path, identity in self._directories:
            info = path.lstat()
            if not _ordinary(info, directory=True) or _identity(info) != identity:
                raise CredentialError(CredentialFailure.STORAGE_CHANGED)
        lock = (self.directory / _LOCK_NAME).lstat()
        if not _ordinary(lock) or _identity(lock) != self._lock_identity:
            raise CredentialError(CredentialFailure.STORAGE_CHANGED)

    def _read(self) -> bytes | None:
        self._check_paths()
        path = self.directory / self._record_name
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not _ordinary(info) or info.st_size > MAX_FILE_BYTES:
                raise CredentialError(CredentialFailure.CORRUPT)
            data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or not data:
                raise CredentialError(CredentialFailure.CORRUPT)
            if _identity(path.lstat()) != _identity(info):
                raise CredentialError(CredentialFailure.STORAGE_CHANGED)
            return data

    def read_current(self) -> bytes | None:
        data = self._read()
        fingerprint = hashlib.sha256(data).digest() if data is not None else None
        if fingerprint != self._fingerprint:
            self.poisoned = True
            raise CredentialError(CredentialFailure.STORAGE_CHANGED)
        return data

    def replace(self, data: bytes) -> None:
        self._owner()
        if type(data) is not bytes or not 1 <= len(data) <= MAX_FILE_BYTES:
            raise CredentialError(CredentialFailure.CAPACITY)
        self.read_current()
        temporary = self.directory / (".pending-" + secrets.token_hex(16))
        replaced = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._owner()
            self.read_current()
            os.replace(temporary, self.directory / self._record_name)
            replaced = True
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._fingerprint = hashlib.sha256(data).digest()
        except BaseException:
            if replaced:
                # Never continue using an old in-memory snapshot after an
                # ambiguous post-replacement durability failure.
                self.poisoned = True
            raise
        finally:
            if self._owner_pid == os.getpid():
                temporary.unlink(missing_ok=True)

    def close(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)
        if self._closed:
            return
        self._closed = True
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        for handle in reversed(self._handles):
            handle.Close()
        self._handles.clear()
