"""Host-side browser interface; no Playwright handles cross this boundary."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PageMetadata:
    url: str
    title: str
    state: str = "ready"


@dataclass(frozen=True)
class PageText:
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class PageLink:
    url: str
    text: str


@dataclass(frozen=True)
class PageLinks:
    links: tuple[PageLink, ...]
    truncated: bool = False


class BrowserBackend(Protocol):
    @property
    def network_isolated(self) -> bool: ...
    def start(self) -> None: ...
    def open_url(self, url: str) -> PageMetadata: ...
    def current_page(self) -> PageMetadata: ...
    def extract_text(self) -> PageText: ...
    def list_links(self) -> PageLinks: ...
    def close(self) -> None: ...
