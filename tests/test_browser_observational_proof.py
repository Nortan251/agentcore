"""Real offline Chromium regressions for observational Browser Read output."""

import importlib.metadata
import os

import pytest

from test_browser_native_visibility import _CASES
from test_browser_renderer_boundary_proof import _render_offline
from agentcore.browser.config import BrowserConfig
from agentcore.browser.playwright_backend import PlaywrightBackend, _ROLE_LINK, _TEXT


pytestmark = pytest.mark.skipif(os.environ.get("AGENTCORE_BROWSER_INTEGRATION") != "1",
                               reason="explicit real Chromium integration gate")

_RAW_TEXT = "() => (document.body || document.documentElement).innerText || ''"
_RAW_ANCHOR = """(element, limit) => {
    if (!(element instanceof HTMLAnchorElement) || !element.hasAttribute('href')) return null;
    return {url: element.getAttribute('href'), text: (element.innerText || '').slice(0, limit)};
}"""


def _document(browser, source):
    context, page, requests, served = _render_offline(browser, source)
    assert served == [True]
    assert requests == ['https://agentcore.invalid/document']
    return context, page, requests


def _snapshot(page):
    return page.locator('html').evaluate('(element) => element.outerHTML')


def test_root_inner_text_matches_native_corpus_without_dom_mutation():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            for name, source, expected_visible in _CASES:
                context, page, requests = _document(browser, source)
                try:
                    before = _snapshot(page)
                    raw = page.evaluate(_RAW_TEXT)
                    projected = page.evaluate(_TEXT, 8000)
                    assert projected == {'text': raw[:8000], 'truncated': len(raw) > 8000}, name
                    assert ('SECRET' in raw) is expected_visible, (name, raw)
                    assert _snapshot(page) == before, name
                    assert requests == ['https://agentcore.invalid/document'], name
                finally:
                    context.close()
        finally:
            browser.close()
    assert len(_CASES) == 48


def test_raw_inner_text_targeted_matrix():
    from playwright.sync_api import sync_playwright

    cases = [
        ('spaces', '<p>Hello   world</p>'),
        ('tabs newlines', '<p>Hello\t \n world</p>'),
        ('pre', '<pre>Pre   spaced\n line</pre>'),
        ('nested inline', '<p>Hello <span>wide</span> world</p>'),
        ('same-word details', '<details><summary>Echo X</summary>Echo  X</details>'),
        ('same-word display none', '<p style="display:none">Echo  X</p><p>Echo X</p>'),
        ('display none', '<p style="display:none">SECRET</p><p>VISIBLE</p>'),
        ('visibility hidden', '<p style="visibility:hidden">SECRET</p><p>VISIBLE</p>'),
        ('block content visibility hidden',
         '<div style="content-visibility:hidden">SECRET</div><p>VISIBLE</p>'),
        ('display contents', '<div style="display:contents"><span>VISIBLE</span></div>'),
        ('nested display contents',
         '<div style="display:contents"><span style="display:contents">VISIBLE</span></div>'),
        ('inline content visibility hidden',
         '<span style="content-visibility:hidden">SECRET</span><span>VISIBLE</span>'),
        ('opacity zero', '<p style="opacity:0">SECRET</p><p>VISIBLE</p>'),
    ]
    results = {}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            for name, source in cases:
                context, page, requests = _document(browser, source)
                try:
                    before = _snapshot(page)
                    results[name] = page.evaluate(_RAW_TEXT)
                    assert page.evaluate(_TEXT, 8000) == {
                        'text': results[name][:8000], 'truncated': len(results[name]) > 8000}
                    assert _snapshot(page) == before
                    assert requests == ['https://agentcore.invalid/document']
                finally:
                    context.close()
        finally:
            browser.close()
    assert results['spaces'] == 'Hello world'
    assert results['pre'] == 'Pre   spaced\n line'
    assert results['same-word details'] == 'Echo X'
    assert results['same-word display none'] == 'Echo X'
    assert results['display contents'] == 'VISIBLE'
    assert 'SECRET' not in results['display none']
    assert 'SECRET' not in results['visibility hidden']
    assert 'SECRET' not in results['block content visibility hidden']
    assert results['nested display contents'] == 'VISIBLE'
    assert results['inline content visibility hidden'] == 'SECRETVISIBLE'
    assert 'SECRET' in results['opacity zero']


def test_playwright_role_actual_anchor_matrix_without_mutation():
    from playwright.sync_api import sync_playwright

    cases = [
        ('ordinary', '<a href="/x">VISIBLE</a>'),
        ('repeated whitespace', '<a href="/x">Hello   world</a>'),
        ('nested span', '<a href="/x"><span>VISIBLE</span></a>'),
        ('display contents anchor', '<a href="/x" style="display:contents">VISIBLE</a>'),
        ('preformatted label', '<a href="/x" style="white-space:pre">Hello   world</a>'),
        ('display none', '<a href="/x" style="display:none">SECRET</a>'),
        ('visibility hidden', '<a href="/x" style="visibility:hidden">SECRET</a>'),
        ('closed details', '<details><a href="/x">SECRET</a></details>'),
        ('closed dialog', '<dialog><a href="/x">SECRET</a></dialog>'),
        ('closed popover', '<div popover><a href="/x">SECRET</a></div>'),
        ('hidden', '<div hidden><a href="/x">SECRET</a></div>'),
        ('hidden until found', '<div hidden="until-found"><a href="/x">SECRET</a></div>'),
        ('block content visibility hidden',
         '<div style="content-visibility:hidden"><a href="/x">SECRET</a></div>'),
        ('opacity zero anchor', '<a href="/x" style="opacity:0">SECRET</a>'),
        ('opacity zero ancestor', '<div style="opacity:0"><a href="/x">SECRET</a></div>'),
        ('inline content visibility hidden',
         '<a href="/x" style="content-visibility:hidden">SECRET</a>'),
        ('display contents ancestor',
         '<div style="display:contents"><a href="/x">VISIBLE</a></div>'),
        ('form anchor', '<form><a href="/x">FORM</a></form>'),
        ('editable anchor', '<div contenteditable><a href="/x">EDITABLE</a></div>'),
        ('aria hidden', '<a href="/x" aria-hidden="true">VISUAL</a>'),
        ('role button', '<a href="/x" role="button">VISIBLE</a>'),
        ('role spoof', '<div role="link" href="/spoof">SPOOF</div>'),
        ('empty anchor', '<a href="/x"></a>'),
        ('aria label only', '<a href="/x" aria-label="ARIA"></a>'),
        ('image only', '<a href="/x"><img alt="ICON" src="https://external.example/x"></a>'),
        ('same-word hidden',
         '<details><a href="/hidden">ECHO</a></details><a href="/visible">ECHO</a>'),
    ]
    results = {}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            for name, source in cases:
                context, page, requests = _document(browser, source)
                try:
                    before = _snapshot(page)
                    roles = page.get_by_role('link')
                    count = roles.count()
                    assert count <= 3
                    actual = [roles.nth(index).evaluate(_RAW_ANCHOR, 256)
                              for index in range(count)]
                    projected = [roles.nth(index).evaluate(_ROLE_LINK,
                        {'url': 2048, 'text': 256}) for index in range(count)]
                    assert projected == actual, name
                    results[name] = {'role_count': count,
                                     'actual_anchors': [item for item in actual if item is not None]}
                    assert _snapshot(page) == before, name
                    assert requests == ['https://agentcore.invalid/document'], name
                finally:
                    context.close()
        finally:
            browser.close()
    assert importlib.metadata.version('playwright') == '1.62.0'
    link = lambda text: {'role_count': 1, 'actual_anchors': [{'url': '/x', 'text': text}]}
    hidden = {'role_count': 0, 'actual_anchors': []}
    expected = {
        'ordinary': link('VISIBLE'),
        'repeated whitespace': link('Hello world'),
        'nested span': link('VISIBLE'),
        'display contents anchor': link('VISIBLE'),
        'preformatted label': link('Hello   world'),
        'display none': hidden,
        'visibility hidden': hidden,
        'closed details': hidden,
        'closed dialog': hidden,
        'closed popover': hidden,
        'hidden': hidden,
        'hidden until found': hidden,
        'block content visibility hidden': hidden,
        'opacity zero anchor': link('SECRET'),
        'opacity zero ancestor': link('SECRET'),
        'inline content visibility hidden': link('SECRET'),
        'display contents ancestor': link('VISIBLE'),
        'form anchor': link('FORM'),
        'editable anchor': link('EDITABLE'),
        'aria hidden': hidden,
        'role button': hidden,
        'role spoof': {'role_count': 1, 'actual_anchors': []},
        'empty anchor': link(''),
        'aria label only': link(''),
        'image only': link(''),
        'same-word hidden': {'role_count': 1,
                             'actual_anchors': [{'url': '/visible', 'text': 'ECHO'}]},
    }
    assert results == expected


def test_hostile_css_observational_projection_does_not_change_visibility():
    from playwright.sync_api import sync_playwright

    cases = [
        ('text', '<style>#secret{display:none}'
         'body:has(#policy[style*="display: none"]) #secret{display:block}</style>'
         '<div id="policy" style="opacity:0">POLICY</div><p id="secret">SECRET</p>'),
        ('link', '<style>#secret{display:none}'
         'a:has(#policy[style*="display: none"]) #secret{display:inline}</style>'
         '<a href="/x">VISIBLE<span id="policy" style="opacity:0">POLICY</span>'
         '<span id="secret">SECRET</span></a>'),
    ]
    results = {}
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            for name, source in cases:
                context, page, requests = _document(browser, source)
                try:
                    before_dom = _snapshot(page)
                    before_text = page.evaluate(_RAW_TEXT)
                    before_display = page.locator('#secret').evaluate(
                        '(element) => getComputedStyle(element).display')
                    roles = page.get_by_role('link')
                    links = [roles.nth(index).evaluate(_ROLE_LINK, {'url': 2048, 'text': 256})
                             for index in range(roles.count())]
                    extracted = page.evaluate(_TEXT, 8000)
                    after_text = page.evaluate(_RAW_TEXT)
                    after_display = page.locator('#secret').evaluate(
                        '(element) => getComputedStyle(element).display')
                    results[name] = {'before_text': before_text, 'after_text': after_text,
                                     'before_display': before_display,
                                     'after_display': after_display, 'links': links,
                                     'extracted': extracted,
                                     'dom_unchanged': _snapshot(page) == before_dom}
                    assert _snapshot(page) == before_dom
                    assert before_display == after_display == 'none'
                    assert 'SECRET' not in before_text + after_text
                    assert 'SECRET' not in extracted['text']
                    assert all(item is None or 'SECRET' not in item['text'] for item in links)
                    assert requests == ['https://agentcore.invalid/document']
                finally:
                    context.close()
        finally:
            browser.close()
    assert results['text']['before_text'] == results['text']['after_text'] == 'POLICY'
    assert results['link']['links'] == [{'url': '/x', 'text': 'VISIBLEPOLICY'}]


def test_owned_renderer_batches_role_candidates_in_order_and_bounds_labels():
    backend = PlaywrightBackend(BrowserConfig(max_links=70, max_link_text_chars=3))
    source = ('<div role="link" href="/spoof">SPOOF</div>'
              + ''.join(f'<a href="/{index}">LABEL_{index}</a>' for index in range(67)))
    try:
        backend.start()
        backend.render_document(source)
        first, cursor, done = backend.link_batch(0, 64)
        assert cursor == 64 and not done
        assert [(link.url, link.text) for link in first] == [
            (f'/{index}', 'LAB') for index in range(63)]
        second, cursor, done = backend.link_batch(cursor, 64)
        assert cursor == 68 and done
        assert [(link.url, link.text) for link in second] == [
            (f'/{index}', 'LAB') for index in range(63, 67)]
        assert [link.url for link in backend.list_links().links] == [
            f'/{index}' for index in range(67)]
    finally:
        backend.close()
