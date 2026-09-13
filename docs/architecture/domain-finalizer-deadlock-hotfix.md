# Domain finalizer deadlock hotfix

This standalone repair starts at main `72c6ee3d2c3530299c04e5088008c5a2caacc327`.
It changes no Browser Read, Gmail, executor, permissions, record schemas, or
effect authority. All implementation is original AgentCore code using Python
standard-library primitives; no external source or runtime dependency is added.

## Individually proven defects

Bounded subprocess probes against unchanged main failed for **each** of
`action_journal/protected_storage.py`, `action_journal/store.py`,
`realtime/store.py`, and `approvals/store.py`. Acquisition GC, release GC,
direct registry-lock GC, and ordered cross-thread lock inversion deadlocked
in every family. Domain allocation failure also left retained owner handles
open in every family: 20 failing-before cases in total. Similarity alone was
not used to establish vulnerability.

## Lifecycle and lock order

The four registries retain their directory keys, shared domain mutexes,
reference counts and OS owner locks. `DomainFinalizers` is a small release
handoff, not a shared store/registry replacement. A finalizer marks its own
instance closed and queues its existing domain lease. It never waits for a
domain mutex or a contended registry lock. A live operation retains its store;
a separate live same-domain store retains a separate counted lease.

The native CPython `SimpleQueue.put` is nonblocking and destructor-reentrant.
This implementation targets the repository's tested CPython 3.11–3.13 runtime;
the equivalent destructor guarantee is not claimed for other interpreters.
See [Python's documented guarantee](https://docs.python.org/3/library/queue.html#queue.SimpleQueue.put).
No GC disabling or background cleanup worker is used in production.

An uncontended handoff drains immediately. A busy registry owner drains after
leaving its outer guard, including exceptional exits. Drainers use a nonblocking
registry try-lock and never acquire domain mutexes. A post-unlock queue recheck
closes the enqueue-at-last-empty-check handoff race. Deferred leases remain
counted until consumed; each queued release is consumed once.

Explicit close retains the original domain-mutex-to-registry serialization and
is deterministic/idempotent. Finalizers no longer introduce the reverse wait.
Each registry uses an RLock for same-thread release reentry; the release handoff,
not the RLock alone, removes the cross-thread inversion.

Owner-handle open, lock, unlock and close remain serialized under each registry
guard. Moving them outside would require additional OPENING waiters and pending
descriptor ownership, increasing the fork-sensitive state space. The smaller
repair preserves the existing atomic same-key owner acquisition. Other threads
cannot acquire during last close. The last domain stays registered at zero
references until its descriptor is closed; reentrant same-key acquisition at
that point fails categorically rather than borrowing closing ownership.
Constructor failures close unpublished handles or release an acquired lease;
they cannot poison the registry or release another live store's lease.

## Fork and process authority

Before-fork hooks acquire each registry guard; a fork from another thread waits
for coherent owner-handle initialization/teardown. Parent hooks release and
drain. Child hooks replace registry locks, queues and thread-local depth, clear
inherited registry entries, and close inherited descriptors **without explicitly
unlocking** their shared OS descriptions. Pending parent releases do not run in
the child. Creator-PID checks reject interrupted constructor publication and
owner unlock in the wrong process. Inherited stores remain unusable.

A same-thread fork inside handle opening can inherit the temporary local
descriptor before publication. The resumed child constructor rejects its
creator PID and closes that local copy before returning authority. Last-close
descriptors remain tracked so the child reset can close them. Real-fork probes
exercise both windows and a fork concurrent with another thread's opening;
existing real-fork tests are retained unchanged.

These are process-owned store lifecycle guarantees, not general containment of
arbitrary host code or a promise of immediate interpreter-shutdown finalization.
Finalizers remain defensive; explicit close is the reliable completion API.
Finalizer exceptions are not logged or retried after a lease is consumed.

## Regression boundaries

The probes use synthetic directories and keys, bounded Events/Barriers, bounded
subprocess timeouts and bounded fork-child cleanup. Only isolated test workers
disable automatic GC to select explicit collection at the vulnerable seams.
Windows runs every non-fork probe; Linux runs those and the real-fork probes.
No production timeout, sleep, finalizer removal, or weakened ownership assertion
is part of the repair. No payload, credential, approval or journal content is
introduced into observability or model state.
