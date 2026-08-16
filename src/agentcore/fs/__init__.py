"""Filesystem safety foundation.

Security primitives for workspace-scoped filesystem access. This package
contains **no tools** — it is the layer a future ``read_file`` /
``list_directory`` is built on, so containment, symlink policy,
sensitive-path policy, special-file handling and byte bounds exist in exactly
one place.

Lazy exports (PEP 562) mirror ``recovery``/``memory``/``conversation``:
``fs.config`` must stay a leaf importing only :mod:`agentcore.errors` so that
``config.schema`` can re-export :class:`FilesystemConfig` without a cycle.
"""

from __future__ import annotations

from typing import Any

_EXPORTS = {
    "FilesystemConfig": ("agentcore.fs.config", "FilesystemConfig"),
    "WorkspaceGuard": ("agentcore.fs.guard", "WorkspaceGuard"),
    "SafePath": ("agentcore.fs.guard", "SafePath"),
    "FileFacts": ("agentcore.fs.paths", "FileFacts"),
    "PathKind": ("agentcore.fs.paths", "PathKind"),
    "PathRejectedError": ("agentcore.fs.paths", "PathRejectedError"),
    "RejectionReason": ("agentcore.fs.paths", "RejectionReason"),
    "WorkspaceMismatchError": ("agentcore.fs.paths", "WorkspaceMismatchError"),
    "canonical_workspace": ("agentcore.fs.paths", "canonical_workspace"),
    "workspace_identity": ("agentcore.fs.paths", "workspace_identity"),
    "classify": ("agentcore.fs.paths", "classify"),
    "SensitivePathPolicy": ("agentcore.fs.sensitive", "SensitivePathPolicy"),
    "DEFAULT_SENSITIVE_POLICY": ("agentcore.fs.sensitive",
                                 "DEFAULT_SENSITIVE_POLICY"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}") from None
    from importlib import import_module
    return getattr(import_module(module_name), attr)


def __dir__() -> list[str]:
    return __all__
