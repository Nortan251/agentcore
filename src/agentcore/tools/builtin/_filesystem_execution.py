"""Executor-issued authority for exact reviewed filesystem mutations."""

import os
import threading

_ISSUER = object()


class _FilesystemMutationExecution:
    """One-use, exact-tool/workspace, thread/PID-local context; never persisted."""

    def __init__(self, issuer, tool, payload, authorize, validate):
        if issuer is not _ISSUER:
            raise ValueError("executor filesystem context required")
        self.tool, self.payload = tool, payload
        self.authorize, self.validate = authorize, validate
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.used, self.active, self.entered = False, True, False

    def __repr__(self):
        return "<executor filesystem mutation context>"

    def __reduce__(self):
        raise TypeError("filesystem context cannot be serialized")
