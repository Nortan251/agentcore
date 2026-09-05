"""Durable protection, identity continuity, atomic replacement and ownership."""

import copy
import json
import os
import pickle
import secrets
import signal
import subprocess
import sys
import threading
import traceback
from dataclasses import asdict, dataclass
from datetime import timedelta

import pytest

from agentcore.credentials import (
    CredentialError, CredentialFailure, CredentialRuntime, CredentialState,
    GoogleConnectionService, GoogleOAuthConfig, ProtectedCredentialVault, SecretMaterial,
    WindowsDpapiProtector,
)
from agentcore.credentials._protected_file import MAX_FILE_BYTES, RECORD_NAME, ProtectedFile
from agentcore.credentials.protected import ConnectionState
from google_oauth_fakes import ACCESS, CLIENT_ID, CLIENT_SECRET, REFRESH, SUBJECT, TestProtector, bundle


@pytest.fixture
def connected(tmp_path):
    value = bundle(tmp_path / "credentials")
    value.info = value.service.connect()
    yield value
    value.vault.close()


def test_protected_roundtrip_reopen_refresh_without_id_token_keeps_identity_and_generations(connected, tmp_path):
    original = connected.info
    assert connected.vault.status().state is ConnectionState.CURRENT
    assert connected.vault._data.identity.subject == SUBJECT
    connected.vault.close()
    reopened = bundle(tmp_path / "credentials", protector=connected.protector)
    try:
        assert reopened.service.restore().state is ConnectionState.REFRESH_REQUIRED
        assert reopened.vault.describe(original.reference) == original
        assert reopened.vault._data.identity.subject == SUBJECT
        identity = reopened.vault._data.identity
        del reopened.transport.refreshed["id_token"]
        reopened.transport.refreshed["expires_in"] = 1800
        refreshed = reopened.service.refresh(original.reference)
        assert reopened.verifier.calls == 0
        assert reopened.vault._data.identity == identity
        assert refreshed.connection_id == original.connection_id
        assert refreshed.account_id == original.account_id
        assert refreshed.generation == original.generation + 1
        assert refreshed.reference != original.reference
        assert refreshed.scopes == original.scopes
        assert refreshed.expires_at == reopened.clock.now() + timedelta(seconds=1800)
        assert reopened.vault.describe(original.reference).state is CredentialState.REPLACED
        assert reopened.transport.refreshes == [REFRESH]
    finally:
        reopened.vault.close()


def test_all_private_record_fields_are_protected_on_disk(connected, tmp_path):
    encoded = (tmp_path / "credentials" / RECORD_NAME).read_bytes()
    for value in (ACCESS, REFRESH, CLIENT_SECRET, SUBJECT, CLIENT_ID, connected.info.reference,
                  connected.info.account_id, connected.info.connection_id):
        assert value.encode() not in encoded
    assert not list((tmp_path / "credentials").glob(".pending-*"))


def test_empty_domain_can_reopen_but_missing_established_record_cannot_reset_identity(tmp_path):
    protector = TestProtector()
    value = bundle(tmp_path / "credentials", protector=protector)
    value.vault.close()
    value = bundle(tmp_path / "credentials", protector=protector)
    assert value.service.restore().state is ConnectionState.DISCONNECTED
    value.service.connect()
    value.vault.close()
    (tmp_path / "credentials" / RECORD_NAME).unlink()
    with pytest.raises(CredentialError) as error:
        bundle(tmp_path / "credentials", protector=protector)
    assert error.value.reason is CredentialFailure.CORRUPT
    assert not (tmp_path / "credentials" / RECORD_NAME).exists()


@pytest.mark.parametrize("cut", ["before_replace", "after_replace"])
def test_process_crash_during_refresh_preserves_a_complete_protected_generation(connected, tmp_path, cut):
    connected.vault.close()
    code = "\n".join([
        "import os, sys",
        "from pathlib import Path",
        "sys.path.insert(0, 'tests')",
        "from google_oauth_fakes import bundle, TestProtector, REFRESH",
        "b = bundle(Path(sys.argv[1]), protector=TestProtector(sys.argv[2].encode()))",
        "b.transport.refreshed['refresh_token'] = 'rotated-' + REFRESH",
        "original = b.vault._file.replace",
        "def crash(data):",
        "    if sys.argv[3] == 'after_replace':",
        "        original(data)",
        "    os._exit(71)",
        "b.vault._file.replace = crash",
        "b.service.refresh(b.service.restore().credential.reference)",
    ])
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "credentials"),
                             connected.protector.key.decode(), cut], capture_output=True, text=True, timeout=10)
    assert result.returncode == 71, result.stderr
    reopened = bundle(tmp_path / "credentials", protector=connected.protector)
    try:
        info = reopened.service.restore().credential
        assert info.generation == (2 if cut == "after_replace" else 1)
        assert info.connection_id == connected.info.connection_id
        reopened.service.refresh(info.reference)
        assert reopened.transport.refreshes == [("rotated-" if cut == "after_replace" else "") + REFRESH]
    finally:
        reopened.vault.close()


def test_capacity_failure_precedes_remote_refresh_and_keeps_tombstones(connected, monkeypatch):
    monkeypatch.setattr("agentcore.credentials.protected.MAX_CREDENTIAL_RECORDS", 2)
    refreshed = connected.service.refresh(connected.info.reference)
    with pytest.raises(CredentialError) as error:
        connected.service.refresh(refreshed.reference)
    assert error.value.reason is CredentialFailure.CAPACITY
    assert len(connected.transport.refreshes) == 1
    assert connected.vault.describe(connected.info.reference).state is CredentialState.REPLACED


def test_ciphertext_cannot_be_transplanted_to_a_different_store_domain(connected, tmp_path):
    connected.vault.close()
    other = tmp_path / "other"
    other.mkdir()
    (other / RECORD_NAME).write_bytes((tmp_path / "credentials" / RECORD_NAME).read_bytes())
    with pytest.raises(CredentialError):
        bundle(other, protector=connected.protector)


def test_hardlinked_protected_record_is_rejected(connected, tmp_path):
    connected.vault.close()
    os.link(tmp_path / "credentials" / RECORD_NAME, tmp_path / "alias")
    with pytest.raises(CredentialError):
        bundle(tmp_path / "credentials", protector=connected.protector)


def test_symlinked_storage_parent_is_rejected_before_creating_child(tmp_path):
    real, alias = tmp_path / "real", tmp_path / "alias"
    real.mkdir()
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable; junction separately covered")
        raise
    with pytest.raises(CredentialError):
        bundle(alias / "must-not-create")
    assert not (real / "must-not-create").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_junction_storage_parent_is_rejected_before_creating_child(tmp_path):
    real, alias = tmp_path / "real", tmp_path / "alias"
    real.mkdir()
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(real)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    with pytest.raises(CredentialError):
        bundle(alias / "must-not-create")
    assert not (real / "must-not-create").exists()


@pytest.mark.parametrize("damage", ["bitflip", "truncate", "plaintext", "future_schema", "duplicate", "identity", "generation", "oversize", "empty"])
def test_corrupt_or_unbounded_records_fail_closed_without_reset(connected, tmp_path, damage):
    path = tmp_path / "credentials" / RECORD_NAME
    data = path.read_bytes()
    purpose = connected.vault._purpose
    connected.vault.close()
    if damage == "bitflip":
        data = data[:30] + bytes([data[30] ^ 1]) + data[31:]
    elif damage == "truncate":
        data = data[:20]
    elif damage == "plaintext":
        data = json.dumps({"refresh": REFRESH}).encode()
    elif damage == "oversize":
        data = b"x" * (MAX_FILE_BYTES + 1)
    elif damage == "empty":
        data = b""
    else:
        plain = json.loads(connected.protector.unprotect(data, purpose=purpose))
        if damage == "future_schema":
            plain["schema"] = 2
        elif damage == "identity":
            plain["records"][0]["provider"] = "another-provider"
        elif damage == "generation":
            plain["records"][0]["generation"] = 2
        encoded = json.dumps(plain).encode()
        if damage == "duplicate":
            encoded = b'{"schema":1,' + encoded[1:]
        data = connected.protector.protect(encoded, purpose=purpose)
    path.write_bytes(data)
    with pytest.raises(CredentialError):
        bundle(tmp_path / "credentials", protector=connected.protector)
    assert path.read_bytes() == data


def test_reopen_rejects_a_different_oauth_client(connected, tmp_path):
    connected.vault.close()
    with ProtectedCredentialVault(tmp_path / "credentials", protector=connected.protector, test_only=True) as vault:
        with pytest.raises(CredentialError) as error:
            GoogleConnectionService(vault, GoogleOAuthConfig("other-client.apps.googleusercontent.com"))
        assert error.value.reason is CredentialFailure.IDENTITY_MISMATCH


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_interrupted_load_sanitizes_errors_and_releases_exclusive_owner(connected, tmp_path, monkeypatch, failure):
    connected.vault.close()
    opened = []
    original_init = ProtectedFile.__init__

    def capture(store, *args, **kwargs):
        original_init(store, *args, **kwargs)
        opened.append(store)

    def interrupted(*args, **kwargs):
        raise failure(REFRESH)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ProtectedFile, "__init__", capture)
            patch.setattr(connected.protector, "unprotect", interrupted)
            with pytest.raises(failure) as error:
                bundle(tmp_path / "credentials", protector=connected.protector)
            assert REFRESH not in "".join(traceback.format_exception(error.value))
            assert error.value.__context__ is None
        reopened = bundle(tmp_path / "credentials", protector=connected.protector)
        try:
            assert reopened.service.restore().credential == connected.info
        finally:
            reopened.vault.close()
    finally:
        for store in opened:
            store.close()


def test_second_writer_is_excluded_in_same_and_other_process(connected, tmp_path):
    with pytest.raises(CredentialError):
        bundle(tmp_path / "credentials", protector=connected.protector)
    code = "\n".join([
        "from pathlib import Path",
        "from agentcore.credentials._protected_file import ProtectedFile",
        "import sys",
        "try:",
        "    f = ProtectedFile(Path(sys.argv[1]))",
        "except Exception:",
        "    print('excluded')",
        "else:",
        "    f.close()",
        "    raise SystemExit(2)",
    ])
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "credentials")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "excluded"


def test_failed_initial_commit_never_reports_connected(tmp_path, monkeypatch):
    value = bundle(tmp_path / "credentials")
    original = (tmp_path / "credentials" / RECORD_NAME).read_bytes()
    def fail(_):
        raise OSError(REFRESH)
    monkeypatch.setattr(value.vault._file, "replace", fail)
    try:
        with pytest.raises(CredentialError) as error:
            value.service.connect()
        assert error.value.reason is CredentialFailure.COMMIT_FAILED
        assert value.service.restore().state is ConnectionState.DISCONNECTED
        assert value.vault._data.refresh is None
        assert REFRESH not in "".join(traceback.format_exception(error.value))
        assert error.value.__context__ is None
        assert (tmp_path / "credentials" / RECORD_NAME).read_bytes() == original
    finally:
        value.vault.close()


def test_rotation_replace_failure_preserves_old_protected_material(connected, tmp_path, monkeypatch):
    path = tmp_path / "credentials" / RECORD_NAME
    before = path.read_bytes()
    connected.transport.refreshed["refresh_token"] = "rotated-" + REFRESH
    def fail(*_):
        raise OSError("test-only simulated replace failure")
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(CredentialError) as error:
            connected.service.refresh(connected.info.reference)
        assert error.value.reason is CredentialFailure.COMMIT_FAILED
    assert path.read_bytes() == before
    assert connected.vault.describe(connected.info.reference) == connected.info
    assert connected.vault.status().state is ConnectionState.CURRENT
    assert connected.vault._data.refresh._read() == REFRESH
    assert not list(path.parent.glob(".pending-*"))


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
def test_exception_after_replacement_never_rolls_back_the_durable_rotation(connected, tmp_path, monkeypatch, failure):
    connected.transport.refreshed["refresh_token"] = "rotated-" + REFRESH
    original = connected.vault._file.replace

    def interrupted(data):
        original(data)
        raise failure(REFRESH)

    with monkeypatch.context() as patch:
        patch.setattr(connected.vault._file, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt if failure is KeyboardInterrupt else CredentialError) as error:
            connected.service.refresh(connected.info.reference)
        assert REFRESH not in "".join(traceback.format_exception(error.value))
        assert error.value.__context__ is None
        with pytest.raises(CredentialError) as unavailable:
            connected.service.restore()
        assert unavailable.value.reason is CredentialFailure.UNAVAILABLE
    connected.vault.close()
    reopened = bundle(tmp_path / "credentials", protector=connected.protector)
    try:
        status = reopened.service.restore()
        assert status.credential.generation == 2
        assert status.credential.connection_id == connected.info.connection_id
        assert status.state is ConnectionState.REFRESH_REQUIRED
        reopened.service.refresh(status.credential.reference)
        assert reopened.transport.refreshes == ["rotated-" + REFRESH]
    finally:
        reopened.vault.close()


def test_committed_rotation_reopens_with_only_replacement_refresh(connected, tmp_path):
    connected.transport.refreshed["refresh_token"] = "rotated-" + REFRESH
    replacement = connected.service.refresh(connected.info.reference)
    connected.vault.close()
    reopened = bundle(tmp_path / "credentials", protector=connected.protector)
    try:
        assert reopened.vault.describe(replacement.reference) == replacement
        reopened.service.refresh(replacement.reference)
        assert reopened.transport.refreshes == ["rotated-" + REFRESH]
    finally:
        reopened.vault.close()


def test_interrupted_access_publication_cannot_pair_new_metadata_with_old_material(connected, monkeypatch):
    original = connected.vault._commit
    calls = 0

    def interrupted(candidate):
        nonlocal calls
        original(candidate)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt(REFRESH)

    with monkeypatch.context() as patch:
        patch.setattr(connected.vault, "_commit", interrupted)
        with pytest.raises(KeyboardInterrupt):
            connected.service.refresh(connected.info.reference)
    status = connected.service.restore()
    assert status.credential.generation == 2
    assert status.state is not ConnectionState.CURRENT
    assert connected.vault._access is None


def test_external_file_replacement_during_refresh_cannot_publish(connected, tmp_path):
    path = tmp_path / "credentials" / RECORD_NAME
    connected.transport.on_refresh = lambda: path.write_bytes(b"untrusted replacement")
    with pytest.raises(CredentialError):
        connected.service.refresh(connected.info.reference)
    assert path.read_bytes() == b"untrusted replacement"
    with pytest.raises(CredentialError):
        connected.vault.describe(connected.info.reference)


def test_disconnect_persists_tombstone_and_reconnect_keeps_account_not_connection(connected, tmp_path):
    original = connected.info
    status = connected.service.disconnect(original.reference)
    assert status.state is ConnectionState.DISCONNECTED and not status.can_refresh
    assert connected.vault._data.refresh is None
    connected.vault.close()
    reopened = bundle(tmp_path / "credentials", protector=connected.protector)
    try:
        assert reopened.service.restore().state is ConnectionState.DISCONNECTED
        with pytest.raises(CredentialError):
            reopened.service.refresh(original.reference)
        replacement = reopened.service.connect()
        assert replacement.account_id == original.account_id
        assert replacement.connection_id != original.connection_id
        assert reopened.vault.describe(original.reference).state is CredentialState.REVOKED
        with pytest.raises(CredentialError):
            reopened.service.disconnect(original.reference)
        assert reopened.vault.describe(replacement.reference) == replacement
    finally:
        reopened.vault.close()


@dataclass
class Envelope:
    value: object


@pytest.mark.parametrize("operation", [repr, str, copy.copy, copy.deepcopy, pickle.dumps, lambda value: asdict(Envelope(value))])
def test_runtime_objects_do_not_serialize_secret_material(connected, operation):
    values = (connected.vault, connected.service, connected.config, connected.vault._data,
              connected.vault._data.refresh, SecretMaterial(REFRESH))
    for value in values:
        if operation in (str, repr):
            rendered = operation(value)
            assert not any(secret in rendered for secret in (REFRESH, ACCESS, CLIENT_SECRET))
        else:
            with pytest.raises(TypeError):
                operation(value)
    assert REFRESH not in repr(asdict(connected.vault.status()))


def test_inert_metadata_cannot_resolve_access_or_refresh_material(connected):
    with pytest.raises(CredentialError) as error:
        with connected.vault._lease(connected.info, connected.info.scopes, authority=object()):
            pytest.fail("forged authority admitted")
    assert error.value.reason is CredentialFailure.AUTHORITY
    with pytest.raises(CredentialError):
        connected.vault._begin("refresh", connected.info.reference, authority=object())


def test_test_protector_requires_explicit_nonproduction_opt_in(tmp_path):
    with pytest.raises(CredentialError) as error:
        ProtectedCredentialVault(tmp_path / "credentials", protector=TestProtector())
    assert error.value.reason is CredentialFailure.UNSUPPORTED


@pytest.mark.skipif(os.name == "nt", reason="Non-Windows production must fail closed")
def test_non_windows_production_has_no_plaintext_fallback(tmp_path):
    with pytest.raises(CredentialError) as error:
        ProtectedCredentialVault(tmp_path / "credentials")
    assert error.value.reason is CredentialFailure.UNSUPPORTED
    assert not (tmp_path / "credentials").exists()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows current-user DPAPI integration")
def test_windows_dpapi_roundtrip_corruption_purpose_and_durable_restart(tmp_path):
    protector = WindowsDpapiProtector()
    synthetic = secrets.token_bytes(96)
    blob = protector.protect(synthetic, purpose=b"synthetic-test-purpose")
    assert synthetic not in blob
    assert protector.unprotect(blob, purpose=b"synthetic-test-purpose") == synthetic
    for invalid, purpose in ((blob[:-1], b"synthetic-test-purpose"), (blob, b"other-domain")):
        with pytest.raises(CredentialError):
            protector.unprotect(invalid, purpose=purpose)
    value = bundle(tmp_path / "dpapi", production=True)
    value.transport.initial["refresh_token"] = secrets.token_urlsafe(48)
    try:
        info = value.service.connect()
        expected = value.transport.initial["refresh_token"]
        assert expected.encode() not in (tmp_path / "dpapi" / RECORD_NAME).read_bytes()
    finally:
        value.vault.close()
    restored = bundle(tmp_path / "dpapi", production=True)
    try:
        assert restored.vault.describe(info.reference) == info
        restored.service.refresh(info.reference)
        assert restored.transport.refreshes == [expected]
    finally:
        restored.vault.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration")
def test_windows_directory_dacl_is_protected_and_restricts_access(connected, tmp_path):
    import win32api
    import win32con
    import win32security
    directory = tmp_path / "credentials"
    sd = win32security.GetFileSecurity(str(directory), win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION)
    assert sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
    acl = sd.GetSecurityDescriptorDacl()
    assert acl.GetAceCount() == 2
    allowed = {win32security.ConvertSidToStringSid(acl.GetAce(i)[2]) for i in range(acl.GetAceCount())}
    assert allowed == {win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner()), "S-1-5-18"}
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        assert sd.GetSecurityDescriptorOwner() == win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


@pytest.mark.skipif(os.name != "nt", reason="Windows token default-owner security rules")
@pytest.mark.parametrize("owner_kind", ["user", "default_group", "unrelated"])
def test_windows_private_directory_handles_token_default_group_owner(tmp_path, monkeypatch, owner_kind):
    from types import SimpleNamespace
    import win32api
    import win32con
    import win32security
    from agentcore.credentials._protected_file import _windows_private_directory

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    default = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    unrelated = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    owner = {"user": user, "default_group": default, "unrelated": unrelated}[owner_kind]

    def information(token, kind):
        if kind == win32security.TokenUser:
            return user, 0
        assert kind == win32security.TokenOwner
        return default

    applied = []
    monkeypatch.setattr(win32security, "GetTokenInformation", information)
    monkeypatch.setattr(win32security, "GetFileSecurity", lambda *args:
                        SimpleNamespace(GetSecurityDescriptorOwner=lambda: owner))
    monkeypatch.setattr(win32security, "SetNamedSecurityInfo", lambda *args: applied.append(args))
    if owner_kind == "unrelated":
        with pytest.raises(CredentialError):
            _windows_private_directory(tmp_path)
        assert not applied
        return
    _windows_private_directory(tmp_path)
    assert len(applied) == 1
    arguments = applied[0]
    assert arguments[2] & win32security.PROTECTED_DACL_SECURITY_INFORMATION
    if owner_kind == "default_group":
        assert arguments[2] & win32security.OWNER_SECURITY_INFORMATION
        assert arguments[3] == user
    else:
        assert arguments[3] is None
    acl = arguments[5]
    assert acl.GetAceCount() == 2
    assert {win32security.ConvertSidToStringSid(acl.GetAce(i)[2]) for i in range(2)} == {
        win32security.ConvertSidToStringSid(user), "S-1-5-18"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_portable_test_store_uses_private_files_and_directory(connected, tmp_path):
    assert (tmp_path / "credentials").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "credentials" / RECORD_NAME).stat().st_mode & 0o777 == 0o600


def test_pid_fences_precede_inherited_locks_clock_and_files(connected, monkeypatch):
    class Poison:
        def __enter__(self):
            pytest.fail("inherited lock used")
        def now(self):
            pytest.fail("inherited clock used")
    connected.vault._lock = Poison()
    connected.vault._clock = Poison()
    original = connected.vault._owner_pid
    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: original + 1)
        for action in (connected.vault.status, connected.vault.close, connected.service.restore,
                       connected.service.connect, lambda: connected.service.refresh(connected.info.reference),
                       lambda: connected.service.disconnect(connected.info.reference), connected.vault._file.close):
            with pytest.raises(CredentialError) as error:
                action()
            assert error.value.reason is CredentialFailure.OWNERSHIP
    connected.vault._lock = threading.RLock()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork with a held metadata lock")
def test_real_fork_rejects_inherited_store_service_runtime_before_lock(connected):
    import select

    runtime = CredentialRuntime(connected.vault)
    held, release = threading.Event(), threading.Event()
    def holder():
        with connected.vault._lock, runtime._lock:
            held.set()
            assert release.wait(10)
    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(2)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        rejected = 0
        for action in (connected.vault.status, connected.vault.close, connected.service.restore,
                       lambda: connected.service.refresh(connected.info.reference), runtime.close):
            try:
                action()
            except CredentialError as error:
                rejected += error.reason is CredentialFailure.OWNERSHIP
        os.write(write_fd, str(rejected).encode())
        os._exit(0)
    os.close(write_fd)
    reaped = False
    try:
        assert select.select([read_fd], [], [], 5)[0], "child touched an inherited lock"
        assert os.read(read_fd, 16) == b"5"
        assert os.waitpid(child, 0)[1] == 0
        reaped = True
    finally:
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(read_fd)
        release.set()
        thread.join(2)
        runtime.close()
