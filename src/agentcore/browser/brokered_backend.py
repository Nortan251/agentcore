"""Brokered read-only web navigation; Chromium renders inert HTML offline."""

from .backend import PageMetadata
from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure
from .fetch import SafeFetchBroker
from .network import canonical_url
from .playwright_backend import PlaywrightBackend
from .render import static_html


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
        source = static_html(document.html, self._config)
        self._renderer.start()
        self._renderer.render_document(source)
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
        return self._renderer.list_links()

    def close(self):
        self._closed = True
        self._has_document = False
        self._final_url = "about:blank"
        self._renderer.close()
