"""Deterministic vault, identity, lifecycle, and ownership boundary tests."""

from __future__ import annotations

import copy
import json
import os
import pickle
import signal
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest

from agentcore.credentials import (
    AccountIdentity,
    CredentialError,
    CredentialFailure,
    CredentialRuntime,
    CredentialState,
    InMemoryCredentialVault,
    SecretMaterial,
)
from agentcore.util.clock import FixedClock


SECRET = "credential-vault-sentinel-44-0-never-public"
ROTATED = "credential-rotated-sentinel-44-0-never-public"
SCOPES = frozenset({"profile.read", "items.read"})
NOW = datetime(2026, 8, 30, tzinfo=timezone.utc)
IDENTITY = AccountIdentity("test", "https://issuer.test/tenant-one", "subject-one")


def register(vault, identity=IDENTITY, *, material=SECRET, **kwargs):
    return vault.register(
        identity, SecretMaterial(material), scopes=kwargs.pop("scopes", SCOPES), **kwargs,
    )


@pytest.fixture
def owned_vault():
    clock = FixedClock(NOW)
    vault = InMemoryCredentialVault(clock=clock)
    # Unit-level conformance to the private backend seam. Production issues
    # this identity only through CredentialRuntime attached to ToolExecutor.
    authority = object()
    vault._bind_runtime(authority)

    @contextmanager
    def lease(info, scopes=SCOPES):
        with vault._lease(info, scopes, authority=authority) as access:
            yield access

    return vault, lease, clock


def assert_reason(error, reason):
    assert error.value.reason is reason
    public = json.dumps(error.value.to_dict()) + str(error.value) + repr(error.value)
    assert SECRET not in public and ROTATED not in public


def test_registration_and_resolution_return_only_exact_owned_secret(owned_vault):
    vault, lease, _ = owned_vault
    info = register(vault)
    assert info.reference.startswith("crd_")
    assert info.connection_id.startswith("con_")
    assert info.account_id.startswith("acc_")
    assert info.generation == 1 and info.state is CredentialState.ACTIVE
    assert vault.describe(info.reference) is info
    assert SECRET not in json.dumps(info.to_dict()) + repr(info) + repr(vault)
    assert "subject-one" not in json.dumps(info.to_dict())
    with lease(info) as access:
        assert access.reveal() == SECRET
        assert access.info == info
        assert SECRET not in str(access) + repr(access)
    with pytest.raises(CredentialError) as error:
        access.reveal()
    assert_reason(error, CredentialFailure.AUTHORITY)


def test_unknown_reference_and_inert_snapshot_cannot_resolve(owned_vault):
    vault, lease, _ = owned_vault
    info = register(vault)
    unknown = "crd_" + "f" * 32
    with pytest.raises(CredentialError) as error:
        vault.describe(unknown)
    assert_reason(error, CredentialFailure.NOT_FOUND)
    with pytest.raises(CredentialError) as error:
        with lease(replace(info, reference=unknown)):
            pytest.fail("unknown credential resolved")
    assert_reason(error, CredentialFailure.NOT_FOUND)
    with pytest.raises(CredentialError) as error:
        with vault._lease(info, SCOPES, authority=info):
            pytest.fail("metadata granted secret access")
    assert_reason(error, CredentialFailure.AUTHORITY)


@pytest.mark.parametrize("change", [
    {"account_id": "acc_" + "e" * 32},
    {"connection_id": "con_" + "e" * 32},
    {"provider": "another"},
    {"generation": 100},
    {"scopes": frozenset({"profile.read"})},
])
def test_metadata_substitution_is_not_authority(owned_vault, change):
    vault, lease, _ = owned_vault
    info = register(vault)
    with pytest.raises(CredentialError) as error:
        with lease(replace(info, **change)):
            pytest.fail("substituted credential binding resolved")
    assert_reason(error, CredentialFailure.IDENTITY_MISMATCH)


def test_identity_is_provider_issuer_subject_not_email_or_display_name(owned_vault):
    vault, _, _ = owned_vault
    first = register(vault)
    second = register(vault, replace(IDENTITY, issuer="https://issuer.test/tenant-two"))
    third = register(vault, replace(IDENTITY, provider="another"))
    assert len({first.account_id, second.account_id, third.account_id}) == 3
    with pytest.raises(CredentialError) as error:
        register(vault)
    assert_reason(error, CredentialFailure.ALREADY_CONNECTED)


def test_revoke_then_reconnect_retains_account_but_never_revives_old_reference(owned_vault):
    vault, lease, _ = owned_vault
    first = register(vault)
    assert vault.revoke(first.reference).state is CredentialState.REVOKED
    assert vault.revoke(first.reference).state is CredentialState.REVOKED
    second = register(vault, material=ROTATED)
    assert second.account_id == first.account_id
    assert second.connection_id != first.connection_id
    assert second.reference != first.reference
    with pytest.raises(CredentialError) as error:
        with lease(first):
            pytest.fail("revoked snapshot became current")
    assert_reason(error, CredentialFailure.REVOKED)
    with lease(second) as access:
        assert access.reveal() == ROTATED


def test_rotation_requires_current_reference_preserves_identity_and_drops_old_material(owned_vault):
    vault, lease, _ = owned_vault
    first = register(vault)
    second = vault.rotate(first.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES)
    assert second.account_id == first.account_id
    assert second.connection_id == first.connection_id
    assert second.generation == 2 and second.reference != first.reference
    assert vault.describe(first.reference).state is CredentialState.REPLACED
    assert vault._records[first.reference].material is None
    with pytest.raises(CredentialError) as error:
        with lease(first):
            pytest.fail("replaced credential resolved")
    assert_reason(error, CredentialFailure.STALE)
    with pytest.raises(CredentialError) as error:
        vault.rotate(first.reference, IDENTITY, SecretMaterial(SECRET), scopes=SCOPES)
    assert_reason(error, CredentialFailure.STALE)
    with pytest.raises(CredentialError) as error:
        vault.revoke(first.reference)
    assert_reason(error, CredentialFailure.STALE)
    with lease(second) as access:
        assert access.reveal() == ROTATED


@pytest.mark.parametrize("identity", [
    replace(IDENTITY, provider="another"),
    replace(IDENTITY, issuer="https://other.test/tenant-one"),
    replace(IDENTITY, subject="subject-two"),
])
def test_refresh_cannot_silently_change_identity(owned_vault, identity):
    vault, lease, _ = owned_vault
    first = register(vault)
    with pytest.raises(CredentialError) as error:
        vault.rotate(first.reference, identity, SecretMaterial(ROTATED), scopes=SCOPES)
    assert_reason(error, CredentialFailure.IDENTITY_MISMATCH)
    with lease(first) as access:
        assert access.reveal() == SECRET


def test_scope_reduction_is_explicit_and_scope_expansion_requires_new_consent(owned_vault):
    vault, lease, _ = owned_vault
    first = register(vault)
    with pytest.raises(CredentialError) as error:
        vault.rotate(first.reference, IDENTITY, SecretMaterial(ROTATED),
                     scopes=SCOPES | {"items.write"})
    assert_reason(error, CredentialFailure.SCOPE_MISMATCH)
    second = vault.rotate(first.reference, IDENTITY, SecretMaterial(ROTATED),
                          scopes=frozenset({"profile.read"}))
    with pytest.raises(CredentialError) as error:
        with lease(second):
            pytest.fail("missing scope was inferred from old grants")
    assert_reason(error, CredentialFailure.SCOPE_MISMATCH)
    with lease(second, frozenset({"profile.read"})) as access:
        assert access.reveal() == ROTATED


def test_expired_credentials_fail_closed_but_verified_rotation_can_replace_them(owned_vault):
    vault, lease, clock = owned_vault
    first = register(vault, expires_at=NOW + timedelta(seconds=1))
    clock.advance(1)
    with pytest.raises(CredentialError) as error:
        with lease(first):
            pytest.fail("expired token resolved")
    assert_reason(error, CredentialFailure.EXPIRED)
    second = vault.rotate(first.reference, IDENTITY, SecretMaterial(ROTATED),
                          scopes=SCOPES, expires_at=NOW + timedelta(seconds=10))
    with lease(second) as access:
        assert access.reveal() == ROTATED
    vault.revoke(second.reference)
    with pytest.raises(CredentialError) as error:
        vault.rotate(second.reference, IDENTITY, SecretMaterial(SECRET), scopes=SCOPES)
    assert_reason(error, CredentialFailure.REVOKED)


@dataclass
class DebugEnvelope:
    value: object


@pytest.mark.parametrize("operation", [copy.copy, copy.deepcopy, pickle.dumps,
                                      lambda value: asdict(DebugEnvelope(value))])
def test_secret_and_runtime_objects_cannot_be_serialized_or_copied(operation):
    vault = InMemoryCredentialVault()
    runtime = CredentialRuntime(vault)
    for value in (SecretMaterial(SECRET), vault, runtime):
        with pytest.raises(TypeError) as error:
            operation(value)
        assert SECRET not in str(error.value) + repr(value) + str(value)
        assert SECRET not in json.dumps({"value": value}, default=str)


def test_errors_never_echo_invalid_input_or_unknown_secret_shaped_references(owned_vault):
    vault, _, _ = owned_vault
    for value in (SECRET, "", object(), 123):
        with pytest.raises(CredentialError) as error:
            vault.describe(value)
        assert_reason(error, CredentialFailure.INVALID)
    assert CredentialError(SECRET).reason is CredentialFailure.INVALID
    for value in ("", object(), "x" * 16385):
        with pytest.raises(CredentialError):
            SecretMaterial(value)
    with pytest.raises(TypeError):
        vars(SecretMaterial(SECRET))


def test_active_lease_blocks_successful_revoke_rotate_close_and_second_use(owned_vault):
    vault, lease, _ = owned_vault
    first = register(vault)
    with lease(first) as access:
        for operation in (
            lambda: vault.revoke(first.reference),
            lambda: vault.rotate(first.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES),
            vault.close,
        ):
            with pytest.raises(CredentialError) as error:
                operation()
            assert_reason(error, CredentialFailure.BUSY)
        with pytest.raises(CredentialError) as error:
            with lease(first):
                pytest.fail("overlapping connection use was admitted")
        assert_reason(error, CredentialFailure.BUSY)
        assert vault.describe(first.reference) == first
        assert access.reveal() == SECRET
    assert vault.revoke(first.reference).state is CredentialState.REVOKED
    vault.close()
    with pytest.raises(CredentialError) as error:
        vault.describe(first.reference)
    assert_reason(error, CredentialFailure.UNAVAILABLE)


def test_provider_lease_holds_no_vault_metadata_lock(owned_vault):
    vault, lease, _ = owned_vault
    first = register(vault)
    completed = threading.Event()
    errors = []

    def unrelated_registration():
        try:
            register(vault, replace(IDENTITY, subject="unrelated"))
            assert vault.describe(first.reference) == first
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    with lease(first):
        worker = threading.Thread(target=unrelated_registration, daemon=True)
        worker.start()
        assert completed.wait(2), "vault metadata lock held across provider lifetime"
    worker.join(2)
    assert errors == []


def test_concurrent_rotation_is_compare_and_swap_on_the_exact_reference(owned_vault):
    vault, lease, _ = owned_vault
    first = register(vault)
    gate = threading.Barrier(3)
    results = []

    def rotate():
        gate.wait(2)
        try:
            results.append(vault.rotate(first.reference, IDENTITY, SecretMaterial(ROTATED), scopes=SCOPES))
        except CredentialError as exc:
            results.append(exc.reason)

    workers = [threading.Thread(target=rotate) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.wait(2)
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert results.count(CredentialFailure.STALE) == 1
    winner = next(result for result in results if not isinstance(result, CredentialFailure))
    with lease(winner) as access:
        assert access.reveal() == ROTATED


def test_live_lease_cannot_be_transferred_to_another_thread(owned_vault):
    vault, lease, _ = owned_vault
    info = register(vault)
    errors = []
    with lease(info) as access:
        def worker():
            try:
                access.reveal()
            except CredentialError as exc:
                errors.append(exc.reason)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        assert errors == [CredentialFailure.OWNERSHIP]


def test_pid_fences_precede_every_vault_lock_and_secret_read(owned_vault, monkeypatch):
    vault, lease, _ = owned_vault
    info = register(vault)
    material = SecretMaterial(SECRET)

    class PoisonLock:
        def __enter__(self):
            pytest.fail("inherited lock was touched")
        def __exit__(self, *args):
            pytest.fail("inherited lock cleanup was touched")

    vault._lock = PoisonLock()
    pid = os.getpid()
    monkeypatch.setattr(os, "getpid", lambda: pid + 1)
    for operation in (
        lambda: vault.describe(info.reference),
        lambda: vault.revoke(info.reference),
        lambda: register(vault),
        lambda: vault.rotate(info.reference, IDENTITY, material, scopes=SCOPES),
        lambda: vault._bind_runtime(object()),
        vault.close,
        material._read,
        material._discard,
    ):
        with pytest.raises(CredentialError) as error:
            operation()
        assert_reason(error, CredentialFailure.OWNERSHIP)
    with pytest.raises(CredentialError) as error:
        with lease(info):
            pytest.fail("child resolved a credential")
    assert_reason(error, CredentialFailure.OWNERSHIP)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_real_fork_rejects_inherited_vault_and_lease_cleanup_before_held_lock(owned_vault):
    import select

    vault, lease, _ = owned_vault
    info = register(vault)
    active = lease(info)
    access = active.__enter__()
    locked, release = threading.Event(), threading.Event()

    def hold_lock():
        with vault._lock:
            locked.set()
            release.wait(10)

    worker = threading.Thread(target=hold_lock, daemon=True)
    worker.start()
    assert locked.wait(2)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        failures = 0
        for operation in (
            lambda: vault.describe(info.reference),
            lambda: vault.revoke(info.reference),
            lambda: active.__exit__(None, None, None),
            access.reveal,
        ):
            try:
                operation()
            except CredentialError as exc:
                failures += exc.reason is CredentialFailure.OWNERSHIP
        fresh = InMemoryCredentialVault(clock=FixedClock(NOW))
        register(fresh)
        os.write(write_fd, str(failures).encode("ascii"))
        os._exit(0)
    os.close(write_fd)
    reaped = False
    try:
        assert select.select([read_fd], [], [], 5)[0], "child touched inherited mutex"
        assert os.read(read_fd, 16) == b"4"
        assert os.waitpid(child, 0)[1] == 0
        reaped = True
    finally:
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(read_fd)
        release.set()
        worker.join(2)
        active.__exit__(None, None, None)
