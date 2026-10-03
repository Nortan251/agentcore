"""Permanent real-Chromium acceptance corpus for original-source rendering."""

from contextlib import contextmanager
import os
import sys

import pytest

from agentcore.browser.config import BrowserConfig
from agentcore.browser.backend import PageText
from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.browser.playwright_backend import PlaywrightBackend, _DOCUMENT_URL
from test_browser_response_compatibility import CONFIG as LEGACY_CONFIG
from test_browser_playwright_integration import local_server, _assert_owned_processes_terminated

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


@pytest.fixture(scope="function")
def compatibility_renderer():
    # One owner per test, including DOM cases. Pytest completes this teardown
    # before the next test can start an independent renderer.
    backend = PlaywrightBackend(LEGACY_CONFIG)
    try:
        backend.start()
        yield backend
    finally:
        backend.close()


@pytest.mark.parametrize("kind", ("python", "search"))
def test_compatibility_modern_static_projection(compatibility_renderer, kind):
    from test_browser_response_compatibility import Transport, modern_page

    document = Transport([modern_page(kind)]).run()
    backend = compatibility_renderer
    backend.render_document(document.html)
    text = backend.extract_text()
    links = backend.list_links()
    if kind == "python":
        assert "Readable static page" in text.text
        assert len(text.text) == 8000 and text.truncated
        assert len(links.links) == 50 and links.truncated
        assert [link.url for link in links.links] == [f"/topic/{i}" for i in range(50)]
    else:
        assert text.text.strip() == "Enable JavaScript to search."
        assert not text.truncated and not links.links
        assert "SCRIPT_EXECUTED" not in text.text


@pytest.mark.parametrize("size", (64 * 1024, 128 * 1024, 255 * 1024, 262143, 262144))
def test_compatibility_accepted_body_reaches_output_projection(compatibility_renderer, size):
    from test_browser_response_compatibility import Transport, html_body, wire

    document = Transport([wire(html_body(size))]).run()
    compatibility_renderer.render_document(document.html)
    result = compatibility_renderer.extract_text()
    assert "Readable static page" in result.text
    assert len(result.text) == 8000 and result.truncated


@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_compatibility_dom_count_boundary(delta, compatibility_renderer):
    from agentcore.browser.playwright_backend import _check_render_source

    config = LEGACY_CONFIG
    # Document + html + head + body = 4; each empty i adds one element.
    # No whitespace/text/doctype nodes obscure the exact N-1/N/N+1 count.
    source = "<html><head></head><body>" + "<i></i>" * (config.max_dom_nodes + delta - 4) + "</body></html>"
    _check_render_source(config, source)
    assert len(source.encode()) < config.max_body_bytes
    backend = compatibility_renderer
    if delta <= 0:
        backend.render_document(source)
        assert backend.extract_text().text == ""
    else:
        with pytest.raises(BrowserError) as caught:
            backend.render_document(source)
        # Source validated before IPC; this inert, sub-body-cap document
        # can reach SIZE only at the worker's post-parse _DOM_COUNT guard.
        assert caught.value.failure is BrowserFailure.SIZE
        assert str(caught.value) == BrowserFailure.SIZE.value


def test_compatibility_renderer_lifecycle(monkeypatch):
    active = None
    events = []

    class Backend:
        def __init__(self, _config):
            self.started = False

        def start(self):
            nonlocal active
            assert active is None, "compatibility renderer owners overlap"
            active = self
            self.started = True
            events.append("start")

        def render_document(self, _source):
            assert active is self

        def extract_text(self):
            assert active is self
            return PageText("")

        def close(self):
            nonlocal active
            if self.started:
                assert active is self
                active = None
                self.started = False
                events.append("close")

    # Instrument the actual fixture construction seam and execute the actual
    # DOM path, without launching a process or relying on pytest test order.
    monkeypatch.setattr(sys.modules[__name__], "PlaywrightBackend", Backend)
    renderer = contextmanager(compatibility_renderer.__wrapped__)
    for delta in (-1, 0):
        with renderer() as backend:
            test_compatibility_dom_count_boundary(delta, backend)
        assert active is None
    assert events == ["start", "close", "start", "close"]
    # Support both pytest fixture-marker representations and check lifetime.
    marker = getattr(compatibility_renderer, "_fixture_function_marker", None)
    if marker is None:
        marker = compatibility_renderer._pytestfixturefunction
    assert marker.scope == "function"


@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("multibyte", (False, True), ids=("ascii", "utf8-bytes"))
def test_compatibility_renderer_source_boundary(compatibility_renderer, delta, multibyte):
    from test_browser_response_compatibility import generated_source

    config = LEGACY_CONFIG
    source = generated_source(config.render_source_limit + delta, multibyte)
    backend = compatibility_renderer
    if delta <= 0:
        backend.render_document(source)
        result = backend.extract_text()
        assert len(result.text) == config.max_text_chars and result.truncated
    else:
        with pytest.raises(BrowserError) as caught:
            backend.render_document(source)
        assert caught.value.failure is BrowserFailure.SIZE
        assert str(caught.value) == BrowserFailure.SIZE.value


@pytest.mark.parametrize("bound", ("text", "title", "label", "links"))
@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_compatibility_output_boundary(compatibility_renderer, bound, delta):
    backend = compatibility_renderer
    config = LEGACY_CONFIG
    limit = {"text": config.max_text_chars, "title": config.max_title_chars,
             "label": config.max_link_text_chars, "links": config.max_links}[bound]
    size = limit + delta
    source = {
        "text": "<p>" + "T" * size + "</p>",
        "title": "<title>" + "T" * size + "</title>",
        "label": '<a href="/topic">' + "L" * size + "</a>",
        "links": "".join(f'<a href="/topic/{i}">Topic</a>' for i in range(size)),
    }[bound]
    backend.render_document(source)
    if bound == "text":
        result = backend.extract_text()
        assert result.text == "T" * min(size, limit)
        assert result.truncated is (delta > 0)
    elif bound == "title":
        assert backend.current_page().title == "T" * min(size, limit)
    elif bound == "label":
        assert backend.list_links().links[0].text == "L" * min(size, limit)
    else:
        result = backend.list_links()
        assert len(result.links) == min(size, limit)
        assert result.truncated is (delta > 0)


@pytest.mark.parametrize("cut,expected", (
    ("<p>Readable prefix</p><script", "Readable prefix"),
    ("<p>Readable prefix</p><scr", "Readable prefix"),
    ("<p>Readable prefix</p><script>Hidden text", "Readable prefix"),
))
def test_compatibility_truncation_can_change_dom(compatibility_renderer, cut, expected):
    # Evidence only, never a new fetch truncation policy. Cutting inside markup
    # loses elements/text; raw-text script EOF keeps following text hidden.
    complete = '<p>Readable prefix</p><script>Hidden text</script><p>Readable suffix</p>'
    backend = compatibility_renderer
    backend.render_document(complete)
    assert backend.extract_text().text == "Readable prefix\n\nReadable suffix"
    backend.render_document(cut)
    assert backend.extract_text().text == expected


@pytest.fixture(scope="function")
def static_capacity_renderer():
    backend = PlaywrightBackend(BrowserConfig())
    try:
        backend.start()
        yield backend
    finally:
        backend.close()
        _assert_owned_processes_terminated(backend)


@pytest.mark.parametrize("kind", ("wikipedia", "python"))
def test_static_capacity_modern_projection(static_capacity_renderer, kind):
    from test_browser_response_compatibility import Transport, modern_page

    document = Transport([modern_page(kind)]).run(BrowserConfig())
    backend = static_capacity_renderer
    backend.render_document(document.html)
    assert backend.current_page().title == "Static fixture"
    text, links = backend.extract_text(), backend.list_links()
    assert "Readable static page" in text.text
    assert len(text.text) == 8000 and text.truncated
    assert len(links.links) == 50 and links.truncated
    assert [link.url for link in links.links] == [f"/topic/{i}" for i in range(50)]


@pytest.mark.parametrize("delta", (-1, 0))
def test_static_capacity_near_max_html(static_capacity_renderer, delta):
    from test_browser_response_compatibility import Transport, html_body, wire

    config = BrowserConfig()
    body = html_body(config.max_body_bytes + delta, anchors=60)
    document = Transport([wire(body)]).run(config)
    assert document.html.encode("ascii") == body
    backend = static_capacity_renderer
    backend.render_document(document.html)
    text, links = backend.extract_text(), backend.list_links()
    assert len(text.text) == 8000 and text.truncated
    assert len(links.links) == 50 and links.truncated


@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_static_capacity_dom_count_boundary(static_capacity_renderer, delta):
    # Same native parsing/count guard as the historical corpus, new defaults.
    config = BrowserConfig()
    source = "<html><head></head><body>" + "<i></i>" * (config.max_dom_nodes + delta - 4) + "</body></html>"
    assert len(source.encode("utf-8")) < 100_000 < config.max_body_bytes
    if delta <= 0:
        static_capacity_renderer.render_document(source)
        assert static_capacity_renderer.extract_text().text == ""
    else:
        with pytest.raises(BrowserError) as error:
            static_capacity_renderer.render_document(source)
        assert error.value.failure is BrowserFailure.SIZE


def test_static_capacity_plaintext_real_worst_escape(static_capacity_renderer):
    from test_browser_render_source_bound import _plain_document

    config = BrowserConfig()
    document = _plain_document(b"'" * config.max_body_bytes, "utf-8", config)
    assert len(document.html.encode("utf-8")) == 12_582_923
    static_capacity_renderer.render_document(document.html)
    text = static_capacity_renderer.extract_text()
    assert text.text == "'" * 8000 and text.truncated
    assert static_capacity_renderer.list_links().links == ()


def test_static_capacity_large_document_keeps_resources_inert(static_capacity_renderer, local_server):
    from test_browser_response_compatibility import Transport, wire

    port, hits = local_server
    config = BrowserConfig()
    prefix = (f'<html><head><title>INERT</title><link rel="stylesheet" href="http://127.0.0.1:{port}/style">'
              f'<style>@font-face{{font-family:fixture;src:url(http://127.0.0.1:{port}/font)}}'
              'body{font-family:fixture}</style></head><body>'
              f'<script>document.title="SCRIPT_EXECUTED";fetch("http://127.0.0.1:{port}/script");'
              f'new WebSocket("ws://127.0.0.1:{port}/socket");</script>'
              f'<img src="http://127.0.0.1:{port}/image"><iframe src="http://127.0.0.1:{port}/frame"></iframe>'
              f'<video src="http://127.0.0.1:{port}/video"></video><p>').encode("ascii")
    suffix = b"</p></body></html>"
    body = prefix + b"T" * (config.max_body_bytes - len(prefix) - len(suffix)) + suffix
    document = Transport([wire(body)]).run(config)
    static_capacity_renderer.render_document(document.html)
    assert static_capacity_renderer.current_page().title == "INERT"
    text = static_capacity_renderer.extract_text()
    assert text.text == "T" * 8000 and text.truncated
    assert hits == []
