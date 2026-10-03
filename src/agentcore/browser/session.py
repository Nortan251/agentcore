"""One explicit owner, lazy start, categorical cleanup, no durable state."""

import os
import threading
from urllib.parse import urljoin
from agentcore.realtime.errors import CancellationRequested

from .backend import BrowserBackend, PageLinks, PageMetadata, PageText
from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure
from .navigation_authority import BrowserNavigationAuthority
from .network import canonical_url, display_url


def safe_text(value: str, limit: int) -> str:
    return "".join(c for c in value[:limit] if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))


class BrowserSession:
    def __init__(self, backend: BrowserBackend | None = None, *, config: BrowserConfig | None = None,
                 navigation_authority: BrowserNavigationAuthority | None = None):
        self.config = config or BrowserConfig()
        self.config.validate()
        if backend is None:
            from .playwright_backend import PlaywrightBackend
            backend = PlaywrightBackend(self.config)
        self._backend = backend
        self._navigation_authority = (navigation_authority if navigation_authority is not None
                                      else BrowserNavigationAuthority(max_url_chars=self.config.max_url_chars))
        self._owner = (os.getpid(), threading.get_ident())
        self._started = False
        self._closed = False
        self._cleanup_failed = False
        self._url = "about:blank"

    def _require_owner(self) -> None:
        if self._owner != (os.getpid(), threading.get_ident()):
            raise BrowserError(BrowserFailure.OWNERSHIP)

    @property
    def available(self) -> bool:
        self._require_owner()
        return self.config.enabled and not self._closed and self._backend.network_isolated

    def _ready(self) -> None:
        self._require_owner()
        if self._closed:
            raise BrowserError(BrowserFailure.CLOSED)
        if not self.config.enabled:
            raise BrowserError(BrowserFailure.DISABLED)
        if not self._backend.network_isolated:
            raise BrowserError(BrowserFailure.NETWORK_BOUNDARY)
        if not self._started:
            # Mark ownership before launch so partial launch failure is closed.
            self._started = True
            try:
                self._backend.start()
            except CancellationRequested:
                self.close()
                raise
            except Exception as error:
                failure = error.failure if isinstance(error, BrowserError) else BrowserFailure.LAUNCH
                self.close()
                raise BrowserError(failure) from None
            except BaseException:
                self.close()
                raise

    def _call(self, method: str, *args):
        self._ready()
        try:
            return getattr(self._backend, method)(*args)
        except CancellationRequested:
            self.close()
            raise
        except Exception as error:
            failure = error.failure if isinstance(error, BrowserError) else BrowserFailure.PROVIDER
            self.close()
            raise BrowserError(failure) from None
        except BaseException:
            self.close()
            raise

    def _metadata(self, page: PageMetadata) -> dict:
        self._url = page.url
        return {"url": display_url(page.url, max_chars=self.config.max_url_chars),
                "title": safe_text(page.title, self.config.max_title_chars),
                "state": page.state if page.state in ("ready", "loading", "blank") else "unknown",
                "trust": "UNTRUSTED_DATA"}

    def open_url(self, url: str) -> dict:
        self._require_owner()
        # Preserve the existing unavailable/closed categories without starting
        # a provider. They never imply permission to navigate.
        if self._closed:
            raise BrowserError(BrowserFailure.CLOSED)
        if not self.config.enabled:
            raise BrowserError(BrowserFailure.DISABLED)
        if not self._backend.network_isolated:
            raise BrowserError(BrowserFailure.NETWORK_BOUNDARY)
        # Static validation never performs DNS, launches a provider or logs URL.
        normalized = canonical_url(url, max_chars=self.config.max_url_chars)
        # The model's URL is only a proposal. This check precedes backend
        # startup, DNS, broker worker creation and rendering.
        self._navigation_authority.require(normalized)
        return self._metadata(self._call("open_url", normalized))

    def current_page(self) -> dict:
        return self._metadata(self._call("current_page"))

    def extract_text(self) -> dict:
        page: PageText = self._call("extract_text")
        return {"text": safe_text(page.text, self.config.max_text_chars),
                "truncated": page.truncated or len(page.text) > self.config.max_text_chars,
                "trust": "UNTRUSTED_DATA"}

    def list_links(self) -> dict:
        page: PageLinks = self._call("list_links")
        links = []
        seen = set()
        # Preserve first accessibility-exposed candidate order; never sort by hostile text.
        for link in page.links[:self.config.max_links + 1]:
            try:
                url = canonical_url(urljoin(self._url, link.url), max_chars=self.config.max_url_chars)
            except BrowserError:
                continue
            if url in seen:
                continue
            seen.add(url)
            links.append({"url": url, "text": safe_text(link.text, self.config.max_link_text_chars)})
        return {"links": links[:self.config.max_links],
                "truncated": page.truncated or len(page.links) > self.config.max_links,
                "trust": "UNTRUSTED_DATA"}

    def close(self) -> None:
        self._require_owner()
        if self._cleanup_failed:
            raise BrowserError(BrowserFailure.CLEANUP)
        if self._closed:
            return
        self._closed = True
        self._url = "about:blank"
        if self._started:
            try:
                self._backend.close()
            except Exception:
                self._cleanup_failed = True
                raise BrowserError(BrowserFailure.CLEANUP) from None

    def __enter__(self):
        self._require_owner()
        return self

    def __exit__(self, *_):
        self.close()
