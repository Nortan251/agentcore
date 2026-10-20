"""Opaque in-memory current-host snapshots; never reconstructed from history."""

from copy import deepcopy
import os
import secrets
import threading

from agentcore.fs.mutation import relative_path
from agentcore.fs.replacement import RevisionRejectedError


class FileRevisionRegistry:
    def __init__(self, guard, *, capacity=96, active=lambda: True):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("invalid revision capacity")
        self.guard, self.capacity, self._active = guard, capacity, active
        self._owner = os.getpid(), threading.get_ident()
        self._snapshots = {}
        self._closed = False

    def _check(self):
        if self._owner != (os.getpid(), threading.get_ident()) or self._closed or not self._active():
            raise RevisionRejectedError("invalid_revision")

    def candidate(self):
        self._check()
        if len(self._snapshots) >= self.capacity:
            raise RevisionRejectedError("revision_capacity_exhausted")
        return "rev_" + secrets.token_hex(32)

    def issue(self, revision, snapshot):
        self._check()
        if len(self._snapshots) >= self.capacity or revision in self._snapshots:
            raise RevisionRejectedError("revision_capacity_exhausted")
        self._snapshots[revision] = deepcopy(snapshot)

    def consume(self, revision, requested):
        self._check()
        # Pop first: even a path-mismatched use burns this one-use token. Path
        # normalization is lexical and occurs before any filesystem I/O.
        snapshot = self._snapshots.pop(revision, None)
        if snapshot is None or snapshot["path"] != relative_path(self.guard, requested).as_posix():
            raise RevisionRejectedError("invalid_revision")
        return snapshot

    def close(self):
        if self._owner != (os.getpid(), threading.get_ident()):
            raise RevisionRejectedError("invalid_revision")
        self._closed = True
        self._snapshots.clear()

    def __repr__(self):
        return "<ephemeral file revisions>"

    def __reduce__(self):
        raise TypeError("file revisions cannot be serialized")
