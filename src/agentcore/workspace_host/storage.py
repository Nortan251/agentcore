"""Private plaintext host storage, outside the configured workspace.

The slot lock serializes bootstrap, the two independent durable stores,
startup recovery and the complete one-shot conversation/save/close lifetime.
It is never unlinked. ACLs protect against other users, not same-user code or
administrators. Existing unsafe objects are refused, never chmodded into trust.
"""

from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from agentcore.action_journal import (
    JsonFileActionStore, JsonFileInstallationKeyProvider,
    JsonFileProtectedActionPayloadStore, ProtectedPayloadPurpose,
)
from agentcore.action_journal.protected_storage import MAX_PROTECTED_ACTION_PAYLOAD_BYTES
from agentcore.conversation.session import ConversationSession, JsonFileSessionStore, WORKSPACE_SESSIONS_SUBDIR
from agentcore.credentials._protected_file import _ordinary, _windows_private_directory
from agentcore.fs.paths import workspace_identity
from .errors import WorkspaceHostError
from .privacy import HostLogger
from agentcore.tools.builtin.read_file_for_update import UPDATE_RESULT_PREFIX

MAX_HOST_ENTRIES = 4096
_SLOT_LOCK = ".workspace-host.lock"


def default_host_directory():
    if os.name == "nt":
        from win32com.shell import shell, shellcon

        home = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_LOCAL_APPDATA, None, 0))
        return home / "AgentCore" / "WorkspaceFiles"
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "share" / "agentcore" / "workspace-files"


def workspace_slot(root, workspace):
    identity = os.path.normcase(workspace_identity(workspace))
    digest = hashlib.sha256(b"agentcore.workspace-host.v1\0" + identity.encode("utf-8")).hexdigest()
    return root / "workspaces" / digest


def _windows_validate_private(path, *, directory):
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        default_owner = win32security.GetTokenInformation(token, win32security.TokenOwner)
    finally:
        token.Close()
    sd = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION
                                      | win32security.DACL_SECURITY_INFORMATION)
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    acl = sd.GetSecurityDescriptorDacl()
    # Elevated Windows tokens can create regular files owned by their default
    # Administrators group. As in the reviewed Gmail primitive, this is an
    # authorized token owner; administrators are outside the host boundary.
    owners = (user,) if directory else (user, default_owner)
    if sd.GetSecurityDescriptorOwner() not in owners or acl is None or acl.GetAceCount() == 0:
        raise WorkspaceHostError("workspace_storage_unsafe")
    if directory and not sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED:
        raise WorkspaceHostError("workspace_storage_unsafe")
    for i in range(acl.GetAceCount()):
        ace = acl.GetAce(i)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or ace[2] not in (user, system):
            raise WorkspaceHostError("workspace_storage_unsafe")
    # Child files must inherit the same private access control.
    if directory and not all(acl.GetAce(i)[0][1] & 3 == 3 for i in range(acl.GetAceCount())):
        raise WorkspaceHostError("workspace_storage_unsafe")


def _validate_private(path, *, directory=False):
    info = path.lstat()
    if not _ordinary(info, directory=directory):
        raise WorkspaceHostError("workspace_storage_unsafe")
    if os.name == "nt":
        _windows_validate_private(path, directory=directory)
    elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise WorkspaceHostError("workspace_storage_unsafe")
    return info.st_dev, info.st_ino


def _new_private_directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    else:
        if os.name == "nt":
            # Reuse only the reviewed DACL installer; Gmail is unchanged.
            _windows_private_directory(path)
    return _validate_private(path, directory=True)


def _private_lock_file(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    _validate_private(path)


class WorkspaceHostStorage:
    def __init__(self, workspace, *, directory=None):
        self._pid = os.getpid()
        self._closed = False
        self._stack = ExitStack()
        self._directories = []
        root = Path(directory) if directory is not None else default_host_directory()
        if not root.is_absolute() or (os.name == "nt" and (
                root.drive.startswith("\\\\") or ":" in str(root)[2:])):
            raise WorkspaceHostError("workspace_storage_unsafe")
        root = Path(os.path.abspath(root))
        # Containment uses canonical identities, before mkdir/key/lock/store I/O.
        resolved = root.resolve()
        if resolved == workspace or workspace in resolved.parents or resolved in workspace.parents:
            raise WorkspaceHostError("workspace_storage_unsafe")
        self.directory = workspace_slot(root, workspace)
        try:
            for path in (*reversed(root.parents), root):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    identity = _new_private_directory(path)
                else:
                    if not _ordinary(info, directory=True):
                        raise WorkspaceHostError("workspace_storage_unsafe")
                    if path == root:
                        identity = _validate_private(path, directory=True)
                    else:
                        # Public root-owned sticky temp is permitted for tests;
                        # other writable POSIX ancestors can redirect ownership.
                        if os.name != "nt" and info.st_mode & 0o022 and not (
                                info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                            raise WorkspaceHostError("workspace_storage_unsafe")
                        identity = info.st_dev, info.st_ino
                self._pin_directory(path, identity, private=path == root)
            for path in (root / "workspaces", self.directory):
                self._pin_directory(path, _new_private_directory(path), private=True)
            lock = self.directory / _SLOT_LOCK
            _private_lock_file(lock)
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(lock, flags)
            self._stack.callback(os.close, fd)
            if not _ordinary(os.fstat(fd)) or (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != _validate_private(lock):
                raise WorkspaceHostError("workspace_storage_unsafe")
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise WorkspaceHostError("workspace_host_busy") from None
                raise
            self._stack.callback(self._unlock, fd)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
                os.fsync(fd)
            # Missing keys only bootstrap an empty slot. A sessions directory,
            # actions, payloads, temps or any other state forbid regeneration.
            entries = self._entries(self.directory)
            key = self.directory / "installation-key.json"
            self.key_provider = JsonFileInstallationKeyProvider(key)
            if not key.exists():
                if any(entry.name != _SLOT_LOCK for entry in entries):
                    raise WorkspaceHostError("workspace_installation_key_missing")
                self.key_provider.create()
            _validate_private(key)
            self.key_provider.load()
            for name in ("actions", "payloads", WORKSPACE_SESSIONS_SUBDIR):
                path = self.directory / name
                self._pin_directory(path, _new_private_directory(path), private=True)
                if name == WORKSPACE_SESSIONS_SUBDIR:
                    self._recover_session_temporaries(path)
                else:
                    for entry in self._entries(path):
                        _validate_private(entry)
            _private_lock_file(self.directory / "actions" / ".agentcore-action-store.lock")
            _private_lock_file(self.directory / "payloads" / ".agentcore-protected-payload-store.lock")
            self.check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _entries(path, *, maximum=MAX_HOST_ENTRIES):
        from itertools import islice

        with os.scandir(path) as scan:
            entries = [Path(entry.path) for entry in islice(scan, maximum + 1)]
        if len(entries) > maximum:
            raise WorkspaceHostError("workspace_storage_failed")
        return entries

    def _recover_session_temporaries(self, path):
        # JsonFileSessionStore uses mkstemp's default tmp<8 chars>.tmp names.
        # One extra scan slot reserves crash headroom for its atomic save,
        # even when replacing a transcript at the durable-entry limit. These
        # unpublished files are inert: never replay or promote their content.
        # Only this private session directory has this recovery exception;
        # action/protected-payload temporaries still fail closed unchanged.
        entries = self._entries(path, maximum=MAX_HOST_ENTRIES + 1)
        for entry in entries:
            _validate_private(entry)
        temporary = [entry for entry in entries
                     if re.fullmatch(r"tmp[a-z0-9_]{8}\.tmp", entry.name)]
        if len(entries) - len(temporary) > MAX_HOST_ENTRIES:
            raise WorkspaceHostError("workspace_storage_failed")
        self.check()  # Pinned private directory identities before cleanup.
        for entry in temporary:
            entry.unlink()
        self._entries(path)  # Restore the ordinary durable directory bound.

    def _pin_directory(self, path, identity, *, private):
        if os.name == "nt":
            import win32con
            import win32file

            handle = win32file.CreateFile(str(path), win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE, None, win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT, None)
            self._stack.callback(handle.Close)
            if win32file.GetFileInformationByHandle(handle)[0] & 0x400:
                raise WorkspaceHostError("workspace_storage_unsafe")
        else:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self._stack.callback(os.close, fd)
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != identity:
                raise WorkspaceHostError("workspace_storage_unsafe")
        if (path.lstat().st_dev, path.lstat().st_ino) != identity:
            raise WorkspaceHostError("workspace_storage_unsafe")
        self._directories.append((path, identity, private))

    def _owner(self):
        if self._pid != os.getpid():
            raise WorkspaceHostError("workspace_ownership_invalid")
        if self._closed:
            raise WorkspaceHostError("workspace_host_closed")

    def check(self):
        self._owner()
        for path, identity, private in self._directories:
            info = path.lstat()
            if not _ordinary(info, directory=True) or (info.st_dev, info.st_ino) != identity:
                raise WorkspaceHostError("workspace_storage_unsafe")
            if private:
                _validate_private(path, directory=True)

    def _unlock(self, fd):
        if self._pid != os.getpid():
            raise WorkspaceHostError("workspace_ownership_invalid")
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def close(self):
        if self._pid != os.getpid():
            raise WorkspaceHostError("workspace_ownership_invalid")
        if not self._closed:
            self._closed = True
            self._stack.close()


class PrivateSessionStore(JsonFileSessionStore):
    def __init__(self, storage):
        self._storage = storage
        super().__init__(storage.directory / WORKSPACE_SESSIONS_SUBDIR)
        self._log = HostLogger()
        self.bind_domain(domain="workspace_files")

    def _path(self, session_id):
        self._storage.check()
        if type(session_id) is not str or re.fullmatch(r"conv_[0-9a-f]{12}", session_id) is None:
            raise WorkspaceHostError("workspace_session_invalid")
        path = super()._path(session_id)
        if path.exists():
            _validate_private(path)
        return path

    def require_capacity(self, session_id):
        path = self._path(session_id)
        # The enclosing host slot lock serializes admission and every save.
        # Existing transcripts can still be updated at the directory limit.
        if not path.exists() and len(self._storage._entries(self.directory)) >= MAX_HOST_ENTRIES:
            raise WorkspaceHostError("workspace_session_capacity_exhausted")

    def save(self, session, **kwargs):
        self.require_capacity(session.id)  # Before tempfile/publication I/O.
        # Narrow Workspace persistence projection; the current model context
        # and in-memory protocol keep the complete live result.
        projected = session.to_dict()
        for message in projected["messages"]:
            if message["role"] == "tool" and message.get("name") == "read_file_for_update" and (
                    message["content"].startswith(UPDATE_RESULT_PREFIX)):
                try:
                    body = json.loads(message["content"][len(UPDATE_RESULT_PREFIX):])
                    if type(body) is dict and set(body) == {"revision", "content"}:
                        body["revision"] = "<expired-revision>"
                        message["content"] = UPDATE_RESULT_PREFIX + json.dumps(body, sort_keys=True)
                except (ValueError, TypeError):
                    pass
        return super().save(ConversationSession.from_dict(projected), **kwargs)


class WorkspaceHostServices:
    def __init__(self, workspace, *, directory=None):
        self._pid = os.getpid()
        self._stack = ExitStack()
        try:
            self.storage = WorkspaceHostStorage(workspace, directory=directory)
            self._stack.callback(self.storage.close)
            self.payloads = JsonFileProtectedActionPayloadStore(
                self.storage.directory / "payloads", self.storage.key_provider)
            self._stack.callback(self.payloads.close)
            self.actions = JsonFileActionStore(self.storage.directory / "actions")
            self._stack.callback(self.actions.close)
            self.sessions = PrivateSessionStore(self.storage)
            self._audit_payloads()
        except BaseException:
            self.close()
            raise

    def _audit_payloads(self):
        # Validate even orphaned and terminal payloads. Read bounded inert
        # correlation, then authenticate with the existing public load API.
        for path in self.storage._entries(self.storage.directory / "payloads"):
            if path.name == ".agentcore-protected-payload-store.lock":
                continue
            if re.fullmatch(r"pap_[0-9a-f]{12}\.json", path.name) is None:
                raise WorkspaceHostError("workspace_storage_failed")
            _validate_private(path)
            if path.stat().st_size > MAX_PROTECTED_ACTION_PAYLOAD_BYTES:
                raise WorkspaceHostError("workspace_storage_failed")
            with path.open("rb") as handle:
                raw = handle.read(MAX_PROTECTED_ACTION_PAYLOAD_BYTES + 1)
            data = json.loads(raw)
            self.payloads.load(path.stem, action_id=data["action_id"],
                purpose=ProtectedPayloadPurpose.ACTION_INTENT,
                expected_intent_digest=data["intent_digest"])

    def close(self):
        if self._pid != os.getpid():
            raise WorkspaceHostError("workspace_ownership_invalid")
        self._stack.close()
