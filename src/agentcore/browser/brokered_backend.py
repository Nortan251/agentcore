"""Brokered read-only web navigation; Chromium renders inert HTML offline."""

from urllib.parse import urljoin

from .backend import PageLink, PageLinks, PageMetadata
from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure
from .fetch import SafeFetchBroker
from .network import canonical_url
from .playwright_backend import PlaywrightBackend


class BrokeredBrowserBackend:
    network_isolated = True

    def __init__(self, config: BrowserConfig, *, _broker=None, _renderer=None):
        config.validate()
        self._config = config
        self._broker = _broker if _broker is not None else SafeFetchBroker(config)
        self._renderer = _renderer if _renderer is not None else PlaywrightBackend(config)
        self._final_url = "about:blank"
        self._has_document = False
        self._closed = False

    def start(self):
        if self._closed:
            raise BrowserError(BrowserFailure.CLOSED)
        # Fetch validation precedes renderer startup. No Chromium navigation.

    def open_url(self, url):
        self.start()
        document = self._broker.fetch(canonical_url(url))
        final_url = canonical_url(document.final_url)
        self._renderer.start()
        self._renderer.render_document(document.html)
        self._final_url = final_url
        self._has_document = True
        return self.current_page()

    def current_page(self):
        self.start()
        if not self._has_document:
            return PageMetadata("about:blank", "", "blank")
        page = self._renderer.current_page()
        return PageMetadata(self._final_url, page.title, "ready")

    def extract_text(self):
        self.start()
        if not self._has_document:
            raise BrowserError(BrowserFailure.CLOSED)
        return self._renderer.extract_text()

    def list_links(self):
        self.start()
        if not self._has_document:
            raise BrowserError(BrowserFailure.CLOSED)
        links = []
        seen = set()
        cursor = 0
        done = False
        # Only usable canonical links count toward the quota. Candidate IPC
        # stays bounded at 64 entries; the post-parse DOM cap bounds scans.
        while not done and len(links) <= self._config.max_links:
            batch, next_cursor, done = self._renderer.link_batch(cursor, 64)
            if (type(next_cursor) is not int or next_cursor < cursor
                    or (next_cursor == cursor and not done)
                    or next_cursor > self._config.max_dom_nodes):
                raise BrowserError(BrowserFailure.PROVIDER)
            cursor = next_cursor
            for item in batch:
                href = item.url
                # urljoin/urlsplit can strip controls before policy sees them.
                if (type(href) is not str or len(href) > self._config.max_url_chars
                        or not href.isascii() or "\\" in href
                        or any(ord(char) <= 32 or ord(char) == 127 for char in href)):
                    continue
                try:
                    url = canonical_url(urljoin(self._final_url, href),
                                        max_chars=self._config.max_url_chars)
                except (BrowserError, ValueError, UnicodeError):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                links.append(PageLink(url, item.text))
                if len(links) > self._config.max_links:
                    break
        return PageLinks(tuple(links), len(links) > self._config.max_links)

    def close(self):
        self._closed = True
        self._has_document = False
        self._final_url = "about:blank"
        self._renderer.close()
