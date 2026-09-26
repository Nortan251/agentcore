"""Original allowlist projection into inert static HTML, not a web engine.

Offline Chromium is not a filesystem boundary. Remove resource/navigation
markup before giving hostile HTML to it, including meta refresh and file URLs.
Only link href DATA is retained; parent code decides link destinations.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from .errors import BrowserError, BrowserFailure
from .network import canonical_url

_TAGS = frozenset("html head body title div span p h1 h2 h3 h4 h5 h6 pre code blockquote ul ol li dl dt dd table thead tbody tfoot tr th td a br hr strong em b i u s small sup sub section article main header footer nav aside figure figcaption".split())
_VOID = frozenset({"br", "hr"})
# The complete HTML void set governs parser state even for tags we strip.
# A trailing slash does not close any other HTML element in a browser.
_HTML_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_EXCLUDE = frozenset("script style noscript template form input textarea select button iframe object embed svg math".split())
# Chromium hides these native fallback subtrees. Stripping only their wrapper
# would otherwise promote hidden text into model-visible page content.
_HIDDEN_FALLBACK = frozenset({"audio", "video", "datalist", "progress", "meter"})
_CLOSED_BY_DEFAULT = frozenset({"dialog", "details"})

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
    # No CSS identifier decoder in v1. Escapes and controls can disguise a
    # relevant property or declaration boundary, so hide conservatively even
    # when the original spelling might have been an unrelated visible style.
    if "/*" in source or "\\" in source or any(
            ord(char) < 32 and char not in "\t\n\r\f" or ord(char) == 127
            for char in source):
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
    def __init__(self, config, final_url=None):
        super().__init__(convert_charrefs=True)
        self.config = config
        self.final_url = final_url
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
        explicitly_hidden = any(
            name == "hidden"
            or name == "aria-hidden" and (value or "").strip().lower() == "true"
            or name == "style" and _inline_hidden(value or "")
            for name, value in attrs)
        # HTML boolean attributes are true by presence, including open="false".
        # Check the original sequence before converting duplicate attributes.
        native_hidden = (tag in _HIDDEN_FALLBACK
                         or tag in _CLOSED_BY_DEFAULT
                         and not any(name == "open" for name, _ in attrs))
        attrs = dict(attrs)  # retain existing href/data handling
        if self.ignored:
            # Track every non-void descendant. A premature/mismatched end tag
            # must not release a suppressed wrapper and expose later text.
            if tag not in _HTML_VOID:
                self.ignored.append(tag)
            return
        if native_hidden or tag in _EXCLUDE or "contenteditable" in attrs:
            if tag not in _HTML_VOID:
                self.ignored.append(tag)
            return
        if tag not in _TAGS:
            if explicitly_hidden and tag not in _HTML_VOID:
                self.ignored.append(tag)
            return
        safe_attrs = ""
        if tag == "a" and type(attrs.get("href")) is str and len(attrs["href"]) <= self.config.max_url_chars:
            # Keep link proposals as inert data, never navigable browser hrefs.
            href = attrs["href"]
            if self.final_url is not None:
                # urljoin/urlsplit can strip control characters before policy
                # sees them. Reject ambiguous raw markup before resolution.
                if (not href.isascii() or "\\" in href
                        or any(ord(char) <= 32 or ord(char) == 127 for char in href)):
                    href = None
                else:
                    try:
                        href = canonical_url(urljoin(self.final_url, href),
                                             max_chars=self.config.max_url_chars)
                    except (BrowserError, ValueError, UnicodeError):
                        href = None
            if href is not None:
                safe_attrs += ' data-agentcore-href="' + html.escape(href, quote=True) + '"'
        # Preserve explicit invisibility without importing arbitrary CSS URLs,
        # @imports, fonts, animations, custom elements or active attributes.
        if explicitly_hidden:
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
        if tag in _HTML_VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.ignored:
            self._emit(html.escape(data, quote=False))


def static_html(source, config, *, final_url=None):
    if type(source) is not str or len(source) > config.max_body_bytes * 6 + 16:
        raise BrowserError(BrowserFailure.SIZE)
    parser = _StaticHTML(config, final_url=final_url)
    try:
        parser.feed(source)
        parser.close()
        return "".join(parser.parts)
    except BrowserError:
        raise
    except Exception:
        raise BrowserError(BrowserFailure.RESPONSE) from None
