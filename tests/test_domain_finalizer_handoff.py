from _queue import Empty, SimpleQueue
import threading

import pytest

from agentcore.util.domain_finalizers import DomainFinalizers


def test_failed_guard_flushes_deferred_release():
    releases = []
    handoff = DomainFinalizers(threading.RLock(), releases.append)
    with pytest.raises(RuntimeError):
        with handoff.guard():
            handoff.release_later('domain')
            assert not releases
            raise RuntimeError('synthetic failed initialization')
    assert releases == ['domain']


def test_enqueue_between_empty_get_and_unlock_cannot_lose_handoff():
    releases = []
    handoff = DomainFinalizers(threading.RLock(), releases.append)
    empty_get, producer_done = threading.Event(), threading.Event()
    queue = SimpleQueue()

    class CoordinatedQueue:
        fired = False
        def put(self, value):
            queue.put(value)
        def empty(self):
            return queue.empty()
        def get_nowait(self):
            try:
                return queue.get_nowait()
            except Empty:
                if not self.fired:
                    self.fired = True
                    empty_get.set()
                    assert producer_done.wait(2)
                raise

    handoff._pending = CoordinatedQueue()
    def producer():
        assert empty_get.wait(2)
        handoff.release_later('raced')
        producer_done.set()
    thread = threading.Thread(target=producer, daemon=True)
    thread.start()
    handoff.release_later('initial')
    thread.join(2)
    assert not thread.is_alive()
    assert releases == ['initial', 'raced'] and queue.empty()
