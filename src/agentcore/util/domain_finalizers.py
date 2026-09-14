"""Destructor-safe release handoff, not a store/domain registry abstraction.

CPython's native SimpleQueue.put is nonblocking and reentrant in destructors.
Finalizers never wait on another domain's mutex or a contended registry lock.
The registry owner drains handoffs after leaving its critical section.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from _queue import Empty, SimpleQueue
import threading
from typing import Callable, Generic, Iterator, TypeVar


_Domain = TypeVar('_Domain')


class DomainFinalizers(Generic[_Domain]):
    def __init__(self, lock: threading.RLock, release: Callable[[_Domain], None]) -> None:
        self.lock = lock
        self._release = release
        self._owner_pid = os.getpid()
        self._pending: SimpleQueue[_Domain] = SimpleQueue()
        self._local = threading.local()

    @contextmanager
    def guard(self) -> Iterator[None]:
        """Keep same-key transitions atomic; flush only outside the outer guard."""
        local = self._local
        depth = getattr(local, 'depth', 0)
        try:
            with self.lock:
                local.depth = depth + 1
                try:
                    yield
                finally:
                    local.depth = depth
        finally:
            # Also flush after failed construction/close. An inherited guard
            # may finish after fork; drain() rejects its parent's queue.
            self.drain()

    def release_later(self, domain: _Domain) -> None:
        if self._owner_pid != os.getpid():
            return
        self._pending.put(domain)
        self.drain()

    def drain(self) -> None:
        if self._owner_pid != os.getpid() or getattr(self._local, 'depth', 0):
            return
        # Check again AFTER unlocking: a producer can enqueue between the final
        # empty get and unlock, fail its try-lock, and rely on this owner to drain.
        while not self._pending.empty():
            if not self.lock.acquire(blocking=False):
                return
            local = self._local
            local.depth = 1
            try:
                while True:
                    try:
                        domain = self._pending.get_nowait()
                    except Empty:
                        break
                    try:
                        self._release(domain)
                    except BaseException:
                        # Same defensive semantics as __del__: no exception or
                        # retry can double-release a lease removed by its owner.
                        pass
            finally:
                local.depth = 0
                self.lock.release()

    def before_fork(self) -> None:
        # A fork from another thread must wait until descriptor initialization /
        # last close has completed and the active registry is a coherent snapshot.
        self.lock.acquire()

    def after_fork_parent(self) -> None:
        self.lock.release()
        self.drain()
