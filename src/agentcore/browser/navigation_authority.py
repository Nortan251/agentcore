"""Ephemeral, host-owned exact-URL grants for Browser Read navigation."""

from contextlib import contextmanager
import os
import threading

from .errors import BrowserError, BrowserFailure
from .network import canonical_url


_MAX_GRANTS = 16


class BrowserNavigationAuthority:
    """One owner's bounded grants for one synchronous conversation turn.

    Model arguments, page links and persisted transcripts never call
    ``for_turn``. Successful URLs may be reused only while that host turn is
    active; close, exceptions and turn completion revoke every grant.
    """

    def __init__(self, *, max_url_chars: int):
        self._owner = (os.getpid(), threading.get_ident())
        self._max_url_chars = max_url_chars
        self._urls = frozenset()
        self._active = False
        self._closed = False

    def __repr__(self):
        return "<browser navigation authority>"

    def __reduce_ex__(self, protocol):
        raise TypeError("browser navigation authority cannot be serialized")

    def _require_owner(self):
        if self._owner != (os.getpid(), threading.get_ident()):
            raise BrowserError(BrowserFailure.OWNERSHIP)

    @contextmanager
    def for_turn(self, urls=()):
        """Accept only an explicit, bounded host sequence; retain no raw URL."""
        self._require_owner()
        if self._closed or self._active:
            raise BrowserError(BrowserFailure.CONTRACT)
        if type(urls) is tuple:
            snapshot = urls
        elif type(urls) is list:
            # List ownership stays with the host. Copy once before checking
            # bounds so later appends cannot enter this turn's authority.
            snapshot = tuple(urls.copy())
        else:
            raise BrowserError(BrowserFailure.CONTRACT)
        if len(snapshot) > _MAX_GRANTS:
            raise BrowserError(BrowserFailure.CONTRACT)
        # Complete validation precedes publication. A bad grant never leaves a
        # partially authorized set, and canonical_url performs no DNS or I/O.
        canonical = frozenset(canonical_url(url, max_chars=self._max_url_chars) for url in snapshot)
        self._urls = canonical
        self._active = True
        try:
            yield
        finally:
            self._urls = frozenset()
            self._active = False

    def require(self, url: str) -> None:
        self._require_owner()
        canonical = canonical_url(url, max_chars=self._max_url_chars)
        if self._closed or not self._active or canonical not in self._urls:
            raise BrowserError(BrowserFailure.NAVIGATION_NOT_AUTHORIZED)

    def close(self) -> None:
        self._require_owner()
        self._urls = frozenset()
        self._closed = True
