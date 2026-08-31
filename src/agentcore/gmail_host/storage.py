"""Pinned private local storage; one immutable smoke preparation per domain.

The continuity MAC authenticates inert correlation, never permission. Existing
payload/evidence, approval and credential stores remain authoritative.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path

from agentcore.action_journal import JsonFileInstallationKeyProvider
from agentcore.action_journal.models import require_action_id
from agentcore.credentials import GoogleOAuthConfig, SecretMaterial
from agentcore.credentials._protected_file import ProtectedFile, _ordinary, _windows_private_directory
from agentcore.credentials.models import RuntimeOnly
from agentcore.credentials.protected import strict_json
from agentcore.gmail_host.errors import GmailHostError, HostFailure
from agentcore.integrations.gmail.send import GmailSendPreparation


_DIRECTORIES = ("actions", "approvals", "payloads", "evidence")
_STATE_FILE = "gmail-host.json"
_CLIENT_FILE = "oauth-client.json"
_MAX_STATE = 4096


def _local_directory(directory: Path) -> Path:
    if not Path(directory).is_absolute():
        raise GmailHostError(HostFailure.CONFIGURATION)
    path = Path(os.path.abspath(directory))
    if os.name == "nt" and (path.drive.startswith("\\\\") or ":" in str(path)[2:]):
        raise GmailHostError(HostFailure.CONFIGURATION)
    return path


def default_host_directory() -> Path:
    """Ask Windows, not environment variables or the current working tree."""
    if os.name != "nt":
        raise GmailHostError(HostFailure.CONFIGURATION)
    from win32com.shell import shell, shellcon

    root = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_LOCAL_APPDATA, None, 0))
    root = _local_directory(root / "AgentCore" / "GmailSmokeHost")
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise GmailHostError(HostFailure.CONFIGURATION)
    return root


@dataclass(frozen=True)
class SmokeTestSlot:
    """Content-free host correlation. An action ID is reserved, not authorized."""

    action_id: str
    preparation: GmailSendPreparation

    def validate(self) -> SmokeTestSlot:
        require_action_id(self.action_id)
        handle = self.preparation
        if type(handle) is not GmailSendPreparation:
            raise GmailHostError(HostFailure.STORAGE)
        require_action_id(handle.storage_action_id)
        for value, pattern in (
            (handle.preparation_id, r"gsp_[0-9a-f]{64}"),
            (handle.reference, r"pap_[0-9a-f]{12}"),
            (handle.intent_digest, r"[0-9a-f]{64}"),
        ):
            if type(value) is not str or re.fullmatch(pattern, value) is None:
                raise GmailHostError(HostFailure.STORAGE)
        return self


def _secure_file(path: Path) -> None:
    info = path.lstat()
    if not _ordinary(info):
        raise GmailHostError(HostFailure.STORAGE)
    if os.name == "nt":
        # The existing helper also sets a protected current-user/SYSTEM DACL
        # on regular files. Reject links before changing any access control.
        _windows_private_directory(path)
    else:
        if info.st_uid != os.getuid():
            raise GmailHostError(HostFailure.STORAGE)
        path.chmod(0o600)
    after = path.lstat()
    if not _ordinary(after) or (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
        raise GmailHostError(HostFailure.STORAGE)


def _private_read(path: Path, maximum: int) -> bytes:
    _secure_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not _ordinary(info) or info.st_size > maximum:
            raise GmailHostError(HostFailure.STORAGE)
        raw = handle.read(maximum + 1)
        after = path.lstat()
        if not _ordinary(after) or len(raw) > maximum or (
            info.st_dev, info.st_ino
        ) != (after.st_dev, after.st_ino):
            raise GmailHostError(HostFailure.STORAGE)
        return raw


class GmailHostStorage(RuntimeOnly):
    """Exclusive domain ownership, private DACLs, authenticated atomic slot."""

    def __init__(self, directory: Path, *, initialize: bool = False) -> None:
        self._pid = os.getpid()
        self._stack = ExitStack()
        self.directory = _local_directory(directory)
        if not initialize and not self.directory.is_dir():
            raise GmailHostError(HostFailure.CONFIGURATION)
        try:
            self._file = ProtectedFile(self.directory, record_name=_STATE_FILE)
            self._stack.callback(self._file.close)
            current = self._file.read_current()
            key_path = self.directory / "installation-key.json"
            self.key_provider = JsonFileInstallationKeyProvider(key_path)
            fresh = initialize and not self._file.had_owner and current is None and not key_path.exists()
            if fresh:
                self.key_provider.create()
            _private_read(key_path, 1024)
            self._key = self.key_provider.load().key
            for name in _DIRECTORIES:
                path = self.directory / name
                if not fresh and not path.is_dir():
                    raise GmailHostError(HostFailure.STORAGE)
                guard = ProtectedFile(path)
                self._stack.callback(guard.close)
                # Existing stores are ACL-dependent authenticated plaintext.
                # Bound this installation check; never recurse into links.
                entries = list(islice(path.iterdir(), 4097))
                if len(entries) > 4096:
                    raise GmailHostError(HostFailure.STORAGE)
                for entry in entries:
                    if entry.name != ".credential-owner":
                        _secure_file(entry)
            if fresh:
                self._write(None)
            elif current is None:
                raise GmailHostError(HostFailure.STORAGE)
            else:
                _secure_file(self.directory / _STATE_FILE)
            self.load_slot()
        except BaseException:
            self._stack.close()
            raise

    def _owner(self) -> None:
        if self._pid != os.getpid():
            raise GmailHostError(HostFailure.OWNERSHIP)
        self._file._check_paths()

    @staticmethod
    def _encoded(value: dict) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    def _mac(self, value: dict) -> str:
        return hmac.new(self._key, b"agentcore.gmail.host-slot.v1\0" + self._encoded(value), hashlib.sha256).hexdigest()

    def _write(self, slot: SmokeTestSlot | None) -> None:
        value = {"schema_version": 1, "slot": asdict(slot.validate()) if slot else None}
        self._file.replace(self._encoded({**value, "mac": self._mac(value)}))

    def load_slot(self) -> SmokeTestSlot | None:
        self._owner()
        raw = self._file.read_current()
        if raw is None or len(raw) > _MAX_STATE:
            raise GmailHostError(HostFailure.STORAGE)
        value = strict_json(raw)
        if set(value) != {"schema_version", "slot", "mac"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise GmailHostError(HostFailure.STORAGE)
        mac = value.pop("mac")
        if type(mac) is not str or not hmac.compare_digest(self._mac(value), mac):
            raise GmailHostError(HostFailure.STORAGE)
        data = value["slot"]
        if data is None:
            return None
        if type(data) is not dict or set(data) != {"action_id", "preparation"}:
            raise GmailHostError(HostFailure.STORAGE)
        handle = data["preparation"]
        if type(handle) is not dict or set(handle) != {
            "preparation_id", "storage_action_id", "reference", "intent_digest",
        }:
            raise GmailHostError(HostFailure.STORAGE)
        return SmokeTestSlot(data["action_id"], GmailSendPreparation(**handle)).validate()

    def save_once(self, slot: SmokeTestSlot) -> None:
        self._owner()
        if self.load_slot() is not None:
            raise GmailHostError(HostFailure.EXISTING)
        self._write(slot.validate())
        if self.load_slot() != slot:
            raise GmailHostError(HostFailure.STORAGE)

    def oauth_config(self) -> GoogleOAuthConfig:
        self._owner()
        # This deliberately small host format accepts no token, account, scope,
        # path, URL or permission options. Nothing is read from a repository.
        raw = None
        try:
            raw = _private_read(self.directory / _CLIENT_FILE, 16_384)
        except FileNotFoundError:
            pass
        if raw is None:
            raise GmailHostError(HostFailure.CONFIGURATION)
        value = strict_json(raw)
        if not {"client_id"} <= set(value) <= {"client_id", "client_secret"}:
            raise GmailHostError(HostFailure.CONFIGURATION)
        client_id = value["client_id"]
        if type(client_id) is not str or not client_id.endswith(".apps.googleusercontent.com"):
            raise GmailHostError(HostFailure.CONFIGURATION)
        secret = SecretMaterial(value["client_secret"]) if "client_secret" in value else None
        try:
            return GoogleOAuthConfig(client_id, client_secret=secret)
        finally:
            if secret is not None:
                secret._discard()

    def close(self) -> None:
        if self._pid != os.getpid():
            raise GmailHostError(HostFailure.OWNERSHIP)
        self._stack.close()

    def __enter__(self) -> GmailHostStorage:
        self._owner()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
