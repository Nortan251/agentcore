"""Isolated, bounded probes; never operate on real credentials or accounts."""
from __future__ import annotations

import faulthandler
import gc
import importlib
import json
import os
from pathlib import Path
import select
import sys
import threading


def run(family, scenario, directory):
    names = {
        'protected': ('agentcore.action_journal.protected_storage', 'JsonFileEvidenceStore'),
        'action': ('agentcore.action_journal.store', 'JsonFileActionStore'),
        'realtime': ('agentcore.realtime.store', 'JsonFileTurnStore'),
        'approval': ('agentcore.approvals.store', 'JsonFileApprovalStore'),
    }
    module_name, class_name = names[family]
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    def make(path):
        if family == 'protected':
            return cls(path, module.StaticInstallationKeyProvider(bytes(range(32))))
        return cls(path)
    def inner(store):
        return store._store if family == 'protected' else store
    def cycle(store):
        obj = inner(store)
        obj._probe_cycle = obj
    directory = Path(directory)
    gc.disable()  # Only the isolated probe selects exact GC timing.
    faulthandler.dump_traceback_later(3)
    victim = make(directory / 'victim')
    domain = inner(victim)._domain
    real_open = Path.open
    real_unlock = module._unlock_owner_handle
    unlocks = []
    def unlock(handle, **kwargs):
        unlocks.append(handle)
        return real_unlock(handle, **kwargs)
    module._unlock_owner_handle = unlock

    if scenario in {'acquire_gc', 'release_gc', 'locked_gc', 'shared_gc', 'inversion'}:
        survivor = make(directory / 'victim') if scenario in {'shared_gc', 'inversion'} else None
        cycle(victim)
        del victim
        if scenario == 'locked_gc':
            with module._DOMAINS_LOCK:
                gc.collect()
        elif scenario == 'release_gc':
            other = make(directory / 'other')
            def collect_unlock(handle, **kwargs):
                gc.collect()
                return unlock(handle, **kwargs)
            module._unlock_owner_handle = collect_unlock
            other.close()
        else:
            entered, closing = threading.Event(), threading.Event()
            errors = []
            def concurrent_close():
                try:
                    with domain.mutex:
                        entered.set()
                        assert closing.wait(2)
                        survivor.close()
                except BaseException as exc:
                    errors.append(type(exc).__name__)
            thread = None
            if scenario == 'inversion':
                thread = threading.Thread(target=concurrent_close, daemon=True)
                thread.start()
                assert entered.wait(2)
            def collecting_open(path, *args, **kwargs):
                handle = real_open(path, *args, **kwargs)
                if path.parent == directory / 'other':
                    closing.set()
                    gc.collect()
                return handle
            Path.open = collecting_open
            other = make(directory / 'other')
            Path.open = real_open
            other.close()
            if thread:
                thread.join(2)
                assert not thread.is_alive() and not errors
            if survivor and scenario == 'shared_gc':
                assert inner(survivor)._domain is domain
                assert domain.references == 1 and not domain.handle.closed
                assert domain.handle not in unlocks
                survivor.close()
        assert domain.handle.closed and domain.key not in module._DOMAINS
        assert unlocks.count(domain.handle) == 1
    elif scenario in {'concurrent', 'last_close'}:
        victim.close()
        gate, opened = threading.Event(), threading.Event()
        barrier = threading.Barrier(3)
        stores, errors, handles = [], [], []
        def opening(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path.parent == directory / 'same':
                handles.append(handle)
                opened.set()
                assert gate.wait(2)
            return handle
        Path.open = opening
        def constructor():
            try:
                barrier.wait(2)
                stores.append(make(directory / 'same'))
            except BaseException as exc:
                errors.append(type(exc).__name__)
        threads = [threading.Thread(target=constructor, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(2)
        assert opened.wait(2)
        gate.set()
        for thread in threads:
            thread.join(2)
        assert not any(t.is_alive() for t in threads) and not errors
        assert len(stores) == 2 and len(handles) == 1
        same = inner(stores[0])._domain
        assert same is inner(stores[1])._domain and same.references == 2
        close_barrier = threading.Barrier(3)
        def closer(store):
            close_barrier.wait(2)
            store.close()
        threads = [threading.Thread(target=closer, args=(s,), daemon=True) for s in stores]
        for thread in threads:
            thread.start()
        close_barrier.wait(2)
        for thread in threads:
            thread.join(2)
        assert not any(t.is_alive() for t in threads)
        assert same.references == 0 and same.handle.closed
        assert same.key not in module._DOMAINS and unlocks.count(same.handle) == 1
        for store in stores:
            store.close()
            inner(store).__del__()
        assert unlocks.count(same.handle) == 1
    elif scenario in {'constructor_failure', 'failed_acquire_gc'}:
        if scenario == 'failed_acquire_gc':
            cycle(victim)
            del victim
        else:
            victim.close()
        real_domain = module._StoreDomain
        handles = []
        def opening(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            handles.append(handle)  # Retain it: implicit GC is not cleanup proof.
            return handle
        Path.open = opening
        def fail(*args, **kwargs):
            if scenario == 'failed_acquire_gc':
                gc.collect()
            raise RuntimeError('synthetic domain allocation failure')
        module._StoreDomain = fail
        try:
            make(directory / 'failed')
        except RuntimeError:
            pass
        else:
            raise AssertionError('constructor must fail')
        assert handles[-1].closed and not module._DOMAINS
        module._StoreDomain = real_domain
        good = make(directory / 'failed')
        good.close()
    elif scenario in {'instance_failure', 'instance_failure_shared'}:
        shared = scenario == 'instance_failure_shared'
        if not shared:
            victim.close()
        instance_cls = type(inner(victim))
        real_setattr = instance_cls.__setattr__
        handles = []
        def opening(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            handles.append(handle)
            return handle
        Path.open = opening
        def fail_assignment(self, name, value):
            if name == '_domain':
                raise RuntimeError('synthetic instance publication failure')
            real_setattr(self, name, value)
        instance_cls.__setattr__ = fail_assignment
        try:
            make(directory / ('victim' if shared else 'failed'))
        except RuntimeError:
            pass
        else:
            raise AssertionError('instance construction must fail')
        finally:
            instance_cls.__setattr__ = real_setattr
        if shared:
            assert domain.references == 1 and not domain.handle.closed
            assert module._DOMAINS[domain.key] is domain
            assert domain.handle not in unlocks
            victim.close()
        else:
            assert handles[-1].closed and not module._DOMAINS
        good = make(directory / 'failed')
        good.close()
    elif scenario == 'closing_reentry':
        def reenter(handle, **kwargs):
            assert module._DOMAINS[domain.key] is domain
            assert domain.references == 0
            try:
                make(directory / 'victim')
            except Exception as exc:
                assert 'OwnershipError' in type(exc).__name__
            else:
                raise AssertionError('cannot acquire closing ownership')
            return unlock(handle, **kwargs)
        module._unlock_owner_handle = reenter
        victim.close()
        module._unlock_owner_handle = unlock
        assert domain.handle.closed and unlocks.count(domain.handle) == 1
        good = make(directory / 'victim')
        good.close()
    elif scenario == 'pid_change_in_open':
        victim.close()
        initial_getpid = os.getpid
        initial_pid = initial_getpid()
        handles = []
        def changed_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            handles.append(handle)
            os.getpid = lambda: initial_pid + 1
            module._after_fork_in_child()
            return handle
        Path.open = changed_open
        try:
            make(directory / 'changed')
        except Exception as exc:
            assert 'OwnershipError' in type(exc).__name__
        else:
            raise AssertionError('must reject changed process')
        finally:
            os.getpid = initial_getpid
        assert handles[-1].closed and not module._DOMAINS
    elif scenario == 'fork_in_open':
        cycle(victim)
        del victim
        read_fd, write_fd = os.pipe()
        initial_pid = os.getpid()
        child_pid = None
        handles = []
        def fork_open(path, *args, **kwargs):
            nonlocal child_pid
            handle = real_open(path, *args, **kwargs)
            handles.append(handle)
            gc.collect()  # A deferred release must survive only in the parent.
            child_pid = os.fork()
            return handle
        Path.open = fork_open
        try:
            store = make(directory / 'forked')
        except Exception as exc:
            if os.getpid() != initial_pid:
                ok = 'OwnershipError' in type(exc).__name__ and handles[-1].closed and not module._DOMAINS
                os.write(write_fd, b'PASS' if ok else b'FAIL')
                os._exit(0)
            raise
        if os.getpid() != initial_pid:
            os.write(write_fd, b'FAIL:published inherited construction')
            os._exit(0)
        Path.open = real_open
        try:
            assert select.select([read_fd], [], [], 2)[0]
            assert os.read(read_fd, 128) == b'PASS'
        finally:
            reap(child_pid)
            os.close(read_fd)
            os.close(write_fd)
            store.close()
    elif scenario == 'fork_in_unlock':
        read_fd, write_fd = os.pipe()
        initial_pid = os.getpid()
        child_pid = None
        def fork_unlock(handle, **kwargs):
            nonlocal child_pid
            child_pid = os.fork()
            if child_pid:
                assert select.select([read_fd], [], [], 2)[0]
                assert os.read(read_fd, 128) == b'PASS'
            return unlock(handle, **kwargs)
        module._unlock_owner_handle = fork_unlock
        victim.close()
        module._unlock_owner_handle = unlock
        if os.getpid() != initial_pid:
            ok = domain.handle.closed and not module._DOMAINS
            try:
                make(directory / 'victim')
            except Exception as exc:
                ok = ok and 'OwnershipError' in type(exc).__name__
            else:
                ok = False
            os.write(write_fd, b'PASS' if ok else b'FAIL')
            os._exit(0)
        reap(child_pid)
        os.close(read_fd)
        os.close(write_fd)
        assert domain.handle.closed and unlocks.count(domain.handle) == 1
        good = make(directory / 'victim')
        good.close()
    elif scenario == 'fork_during_open_thread':
        victim.close()
        gate, opened = threading.Event(), threading.Event()
        handles, stores, errors = [], [], []
        def blocking_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            handles.append(handle)
            opened.set()
            assert gate.wait(2)
            return handle
        Path.open = blocking_open
        def constructor():
            try:
                stores.append(make(directory / 'pending'))
            except BaseException as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=constructor, daemon=True)
        thread.start()
        assert opened.wait(2)
        # Before hooks run in reverse registration order: release the fixture
        # gate before the production hook waits for coherent publication.
        os.register_at_fork(before=gate.set)
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if not child_pid:
            ok = handles[-1].closed and not module._DOMAINS
            Path.open = real_open
            try:
                make(directory / 'pending')
            except Exception as exc:
                ok = ok and 'OwnershipError' in type(exc).__name__
            else:
                ok = False
            os.write(write_fd, b'PASS' if ok else b'FAIL')
            os._exit(0)
        Path.open = real_open
        try:
            assert select.select([read_fd], [], [], 2)[0]
            assert os.read(read_fd, 128) == b'PASS'
        finally:
            reap(child_pid)
            os.close(read_fd)
            os.close(write_fd)
        thread.join(2)
        assert not thread.is_alive() and not errors and len(stores) == 1
        stores[0].close()
    else:
        raise AssertionError(scenario)
    faulthandler.cancel_dump_traceback_later()
    return {'family': family, 'scenario': scenario, 'ok': True}


def reap(pid):
    """Always kill/reap the owned fixture child without a blocking waitpid."""
    import time
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return
        select.select([], [], [], .01)
    raise AssertionError('fixture child did not exit')


if __name__ == '__main__':
    print(json.dumps(run(*sys.argv[1:])), flush=True)
