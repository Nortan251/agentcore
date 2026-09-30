"""Permanent real-Chromium acceptance corpus for original-source rendering."""

import os

import pytest

from agentcore.browser.config import BrowserConfig
from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.browser.playwright_backend import PlaywrightBackend, _DOCUMENT_URL

pytestmark = pytest.mark.skipif(os.environ.get("AGENTCORE_BROWSER_INTEGRATION") != "1",
                               reason="explicit real Chromium integration gate")


# Browser Read returns bounded Chromium innerText, not a pixel-visibility
# judgment. Slot fallback, opacity-zero, form, and editable text may appear;
# the other native-state expectations below remain unchanged.
_CASES = [
    ("hidden wrapper", '<div hidden>SECRET</div><p>VISIBLE</p>', False),
    ("duplicate hidden first", '<div style="display:none" style="display:block">SECRET</div><p>VISIBLE</p>', False),
    ("duplicate visible first", '<div style="display:block" style="display:none">SECRET</div><p>VISIBLE</p>', True),
    ("escaped CSS property", r'<p style="\64 isplay:none">SECRET</p><p>VISIBLE</p>', False),
    ("script nonvoid solidus", '<script/>SECRET', False),
    ("style nonvoid solidus", '<style/>SECRET', False),
    ("textarea nonvoid solidus", '<textarea/>SECRET', False),
    ("div nonvoid solidus", '<div hidden/>SECRET', False),
    ("custom hidden wrapper", '<x-panel hidden><p>SECRET</p></x-panel><p>VISIBLE</p>', False),
    ("dialog closed", '<dialog>SECRET</dialog><p>VISIBLE</p>', False),
    ("dialog open", '<dialog open="false">SECRET</dialog><p>VISIBLE</p>', True),
    ("details closed", '<details><summary>Heading</summary>SECRET</details><p>VISIBLE</p>', False),
    ("details open", '<details open>SECRET</details><p>VISIBLE</p>', True),
    ("audio fallback", '<audio>SECRET</audio><p>VISIBLE</p>', False),
    ("video fallback", '<video>SECRET</video><p>VISIBLE</p>', False),
    ("datalist", '<datalist><option>SECRET</option></datalist><p>VISIBLE</p>', False),
    ("progress", '<progress>SECRET</progress><p>VISIBLE</p>', False),
    ("meter", '<meter>SECRET</meter><p>VISIBLE</p>', False),
    ("canvas fallback", '<canvas>SECRET</canvas><p>VISIBLE</p>', True),
    ("ruby base", '<ruby>SECRET<rt>annotation</rt></ruby><p>VISIBLE</p>', True),
    ("ruby rp", '<ruby>base<rp>SECRET</rp><rt>annotation</rt></ruby><p>VISIBLE</p>', False),
    ("ruby rt", '<ruby>base<rt>SECRET</rt></ruby><p>VISIBLE</p>', True),
    ("noembed", '<noembed>SECRET</noembed><p>VISIBLE</p>', False),
    ("noframes", '<noframes>SECRET</noframes><p>VISIBLE</p>', False),
    ("slot", '<slot>SECRET</slot><p>VISIBLE</p>', True),
    ("popover", '<div popover>SECRET</div><p>VISIBLE</p>', False),
    ("popover empty", '<div popover="">SECRET</div><p>VISIBLE</p>', False),
    ("popover auto", '<div popover="auto">SECRET</div><p>VISIBLE</p>', False),
    ("popover manual", '<div popover="manual">SECRET</div><p>VISIBLE</p>', False),
    ("popover hint", '<div popover="hint">SECRET</div><p>VISIBLE</p>', False),
    ("popover unknown", '<div popover="nonsense">SECRET</div><p>VISIBLE</p>', False),
    ("frameset", '<frameset><noframes>SECRET</noframes></frameset>', False),
    ("hidden until found", '<p hidden="until-found">SECRET</p><p>VISIBLE</p>', False),
    ("aria hidden true", '<p aria-hidden="true">SECRET</p><p>VISIBLE</p>', True),
    ("aria hidden false", '<p aria-hidden="false">SECRET</p><p>VISIBLE</p>', True),
    ("inert", '<p inert>SECRET</p><p>VISIBLE</p>', True),
    ("display none", '<p style="display:none">SECRET</p><p>VISIBLE</p>', False),
    ("display block", '<p style="display:block">SECRET</p><p>VISIBLE</p>', True),
    ("visibility hidden", '<p style="visibility:hidden">SECRET</p><p>VISIBLE</p>', False),
    ("visibility collapse", '<p style="visibility:collapse">SECRET</p><p>VISIBLE</p>', False),
    ("visibility visible", '<p style="visibility:visible">SECRET</p><p>VISIBLE</p>', True),
    ("opacity zero", '<p style="opacity:0">SECRET</p><p>VISIBLE</p>', True),
    ("opacity one", '<p style="opacity:1">SECRET</p><p>VISIBLE</p>', True),
    ("content visibility hidden", '<p style="content-visibility:hidden">SECRET</p><p>VISIBLE</p>', False),
    ("content visibility visible", '<p style="content-visibility:visible">SECRET</p><p>VISIBLE</p>', True),
    ("content visibility auto", '<p style="content-visibility:auto">SECRET</p><p>VISIBLE</p>', True),
    ("form exclusion", '<form>SECRET</form><p>VISIBLE</p>', True),
    ("editable exclusion", '<div contenteditable>SECRET</div><p>VISIBLE</p>', True),
]


def test_original_source_native_visibility_corpus():
    backend = PlaywrightBackend(BrowserConfig())
    try:
        backend.start()
        for name, source, expected_visible in _CASES:
            backend.render_document(source)
            text = backend.extract_text().text
            assert ("SECRET" in text) is expected_visible, (name, text)
            if "<p>VISIBLE</p>" in source:
                assert "VISIBLE" in text, (name, text)
            assert backend.current_page().url == _DOCUMENT_URL
    finally:
        backend.close()


def test_parsed_dom_limit_is_categorical():
    backend = PlaywrightBackend(BrowserConfig(max_dom_nodes=20))
    try:
        backend.start()
        with pytest.raises(BrowserError) as error:
            backend.render_document('<p>node</p>' * 25)
        assert error.value.failure is BrowserFailure.SIZE
    finally:
        backend.close()


def test_text_size_bound_and_repeat_fresh_document():
    backend = PlaywrightBackend(BrowserConfig(max_text_chars=48))
    try:
        backend.start()
        backend.render_document('<p>FIRST</p>')
        assert "FIRST" in backend.extract_text().text
        backend.render_document('<p>SECOND</p>' + '<p>long text</p>' * 40)
        page = backend.extract_text()
        assert "FIRST" not in page.text and "SECOND" in page.text
        assert len(page.text) <= 48 and page.truncated
    finally:
        backend.close()
