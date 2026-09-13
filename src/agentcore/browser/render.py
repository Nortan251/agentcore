"""Original allowlist projection into inert static HTML, not a web engine.

Offline Chromium is not a filesystem boundary. Remove resource/navigation
markup before giving hostile HTML to it, including meta refresh and file URLs.
Only link href DATA is retained; parent code decides link destinations.
"""

import html
import re
from html.parser import HTMLParser

from .errors import BrowserError, BrowserFailure

_TAGS = frozenset("html head body title div span p h1 h2 h3 h4 h5 h6 pre code blockquote ul ol li dl dt dd table thead tbody tfoot tr th td a br hr strong em b i u s small sup sub section article main header footer nav aside figure figcaption".split())
_VOID = frozenset({"br", "hr"})
_EXCLUDE = frozenset("script style noscript template form input textarea select button iframe object embed svg math".split())

_COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)
_IMPORTANT = re.compile(r"\s*!\s*important\s*$", re.IGNORECASE)
_OPACITY = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?%?")
_VISIBLE_DISPLAY = frozenset("block inline inline-block flex inline-flex grid inline-grid table table-row table-cell contents list-item flow-root".split())


def _inline_hidden(style):
    """Small conservative subset, never replay original CSS into Chromium.

    Any hiding declaration wins, even if later overridden (including important
    priority). Unknown/ambiguous relevant values hide too; no CSS cascade claim.
    Unrelated declarations have no influence and are discarded.
    """
    source = _COMMENTS.sub("", style)
    if "/*" in source:  # unterminated comment makes visibility ambiguous
        return True
    for declaration in source.split(";"):
        property_name, separator, value = declaration.partition(":")
        name = property_name.strip().lower()
        if name not in {"display", "visibility", "opacity"}:
            continue
        if not separator:
            return True
        value = _IMPORTANT.sub("", value).strip().lower()
        if name == "display":
            if value not in _VISIBLE_DISPLAY:
                return True
        elif name == "visibility":
            if value != "visible":
                return True
        else:
            if not _OPACITY.fullmatch(value):
                return True
            # All signed/decimal/exponent/percentage zero spellings, and CSS
            # clamped negative opacity, hide. Non-finite/ambiguous values hide.
            numeric = float(value.removesuffix("%"))
            if not 0 < numeric < float("inf"):
                return True
    return False


class _StaticHTML(HTMLParser):
    def __init__(self, config):
        super().__init__(convert_charrefs=True)
        self.config = config
        self.parts = []
        self.ignored = []
        self.nodes = 0
        self.chars = 0

    def _emit(self, value):
        self.chars += len(value)
        if self.chars > self.config.max_body_bytes * 6 + 1024:
            raise BrowserError(BrowserFailure.SIZE)
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > self.config.max_dom_nodes:
            raise BrowserError(BrowserFailure.SIZE)
        attrs = dict(attrs)
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.append(tag)
            return
        if tag in _EXCLUDE or "contenteditable" in attrs:
            if tag not in {"input", "embed"}:
                self.ignored.append(tag)
            return
        if tag not in _TAGS:
            return
        safe_attrs = ""
        if tag == "a" and type(attrs.get("href")) is str and len(attrs["href"]) <= self.config.max_url_chars:
            # Keep destinations as inert data, not navigable browser hrefs
            # (including file/javascript URLs or speculative anchor fetches).
            safe_attrs += ' data-agentcore-href="' + html.escape(attrs["href"], quote=True) + '"'
        # Preserve explicit invisibility without importing arbitrary CSS URLs,
        # @imports, fonts, animations, custom elements or active attributes.
        if ("hidden" in attrs or attrs.get("aria-hidden") == "true"
                or _inline_hidden(attrs.get("style") or "")):
            safe_attrs += ' style="display:none"'
        self._emit("<" + tag + safe_attrs + ">")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in _TAGS and tag not in _VOID:
            self._emit("</" + tag + ">")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.ignored:
            self._emit(html.escape(data, quote=False))


def static_html(source, config):
    if type(source) is not str or len(source) > config.max_body_bytes * 6 + 16:
        raise BrowserError(BrowserFailure.SIZE)
    parser = _StaticHTML(config)
    try:
        parser.feed(source)
        parser.close()
        return "".join(parser.parts)
    except BrowserError:
        raise
    except Exception:
        raise BrowserError(BrowserFailure.RESPONSE) from None
