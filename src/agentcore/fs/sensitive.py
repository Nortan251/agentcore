"""Sensitive-path policy.

Deliberately independent of :mod:`agentcore.permissions`. The permission layer
gates *capabilities* (``FS_READ``) and intentionally does not inspect tool
arguments; ``FS_READ`` is allowed at ``OBSERVE``, so protection for credential
files cannot come from there. This module supplies the argument-level check.

Design rules:

* **Deterministic.** Filename and extension matching only. A model is never
  asked whether a file is sensitive, and file *contents* are never inspected
  to decide — deciding by content would require reading the very bytes the
  policy exists to withhold.
* **Deny by default**, before any bytes are read.
* **Explicit and expandable.** The lists below are the whole policy.

This list is emphatically *not* complete. It covers the common cases; new
entries are expected as real tools land.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath

#: Exact filenames that are sensitive wherever they appear.
SENSITIVE_NAMES: frozenset[str] = frozenset({
    # Environment / dotenv
    ".env",
    # Version-control and network credentials
    ".git-credentials", ".netrc", "_netrc", ".npmrc", ".pypirc",
    # SSH private keys (public ``.pub`` counterparts are not listed)
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity",
    "authorized_keys", "known_hosts",
    # Cloud provider credentials
    "credentials", "config.json", "kubeconfig", ".dockercfg",
    # Generic secret carriers
    "secrets.yaml", "secrets.yml", "secrets.json", "secrets.toml",
    ".htpasswd", ".pgpass", ".my.cnf", ".boto",
    # Browser/session stores
    "cookies.sqlite", "cookies.txt",
})

#: Glob patterns matched against the *filename*.
SENSITIVE_NAME_PATTERNS: tuple[str, ...] = (
    ".env.*",           # .env.local, .env.production
    "*.env",            # prod.env
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "*.ppk", "*.asc", "*.gpg", "*.kdbx",
    "*_rsa", "*_dsa", "*_ecdsa", "*_ed25519",
    "*.crt.key", "*credentials*.json",
)

#: Directory names that are sensitive in their entirety. Matched against any
#: component of the relative path, so ``project/.ssh/notes`` is refused.
SENSITIVE_DIRECTORIES: frozenset[str] = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".gcloud", ".kube", ".docker",
    ".git",  # object store can contain credentials and full history
})


@dataclass(frozen=True)
class SensitivePathPolicy:
    """Decides whether a workspace-relative path is credential-bearing.

    Immutable; extend by constructing one with extra entries rather than
    mutating shared module state::

        policy = SensitivePathPolicy().extend(names={"license.key"})
    """

    names: frozenset[str] = field(default=SENSITIVE_NAMES)
    patterns: tuple[str, ...] = field(default=SENSITIVE_NAME_PATTERNS)
    directories: frozenset[str] = field(default=SENSITIVE_DIRECTORIES)

    def extend(self, *, names: frozenset[str] | set[str] | None = None,
               patterns: tuple[str, ...] = (),
               directories: frozenset[str] | set[str] | None = None,
               ) -> SensitivePathPolicy:
        return SensitivePathPolicy(
            names=self.names | frozenset(names or ()),
            patterns=self.patterns + tuple(patterns),
            directories=self.directories | frozenset(directories or ()),
        )

    def reason(self, relative: PurePosixPath | str) -> str | None:
        """Return why the path is sensitive, or ``None`` if it is not.

        Matching is case-insensitive: filesystems differ, and ``ID_RSA`` is no
        less a private key than ``id_rsa``.
        """
        pure = (relative if isinstance(relative, PurePosixPath)
                else PurePosixPath(str(relative)))
        parts = pure.parts
        if not parts:
            return None

        for part in parts[:-1]:
            if part.lower() in self.directories:
                return f"path is inside a sensitive directory ('{part}')"
        name = parts[-1]
        lowered = name.lower()
        # A sensitive directory named as the final component also counts.
        if lowered in self.directories:
            return f"path is a sensitive directory ('{name}')"
        if lowered in self.names:
            return f"'{name}' is a known credential file"
        for pattern in self.patterns:
            if fnmatch(lowered, pattern):
                return f"'{name}' matches sensitive pattern '{pattern}'"
        return None

    def is_sensitive(self, relative: PurePosixPath | str) -> bool:
        return self.reason(relative) is not None


#: Shared default instance. Callers that need more entries should build their
#: own via :meth:`SensitivePathPolicy.extend`.
DEFAULT_SENSITIVE_POLICY = SensitivePathPolicy()
