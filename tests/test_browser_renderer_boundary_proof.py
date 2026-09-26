"""Opt-in real Chromium proof before replacing Browser Read's renderer."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urljoin

import pytest

from agentcore.browser.playwright_backend import _CSP, _DOCUMENT_URL

pytestmark = pytest.mark.skipif(os.environ.get("AGENTCORE_BROWSER_INTEGRATION") != "1",
                               reason="explicit real Chromium integration gate")

_EXPECTED_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; "
                 "media-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; "
                 "child-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'; "
                 "style-src 'unsafe-inline'; sandbox")


def _render_offline(browser, source):
    context = browser.new_context(java_script_enabled=False, service_workers="block",
                                  accept_downloads=False, offline=True)
    requests = []
    served = [False]

    def route_document(route):
        request = route.request
        requests.append(request.url)
        try:
            initial_document = (not served[0] and request.url == _DOCUMENT_URL
                                and request.method == "GET" and request.is_navigation_request()
                                and request.frame == page.main_frame)
        except Exception:
            initial_document = False
        if initial_document:
            served[0] = True
            route.fulfill(status=200, body=source, headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Security-Policy": _CSP,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            })
        else:
            route.abort("blockedbyclient")

    context.route("**/*", route_document)
    context.route_web_socket("**/*", lambda socket: socket.close())
    page = context.new_page()
    response = page.goto(_DOCUMENT_URL, wait_until="load")
    assert response.status == 200
    assert response.headers["content-security-policy"] == _CSP
    return context, page, requests, served


def _visible_text(page):
    # Phase-one prototype: Chromium's own innerText handles details/frameset
    # visibility that a text-node rectangle walk does not.
    return page.evaluate("""limit => {
        const hidden = [];
        for (const element of document.querySelectorAll('form,input,textarea,select,[contenteditable]')) {
            hidden.push([element, element.style.getPropertyValue('display'),
                         element.style.getPropertyPriority('display')]);
            element.style.setProperty('display', 'none', 'important');
        }
        try {
            return (document.body || document.documentElement).innerText.slice(0, limit);
        } finally {
            for (const [element, value, priority] of hidden) {
                if (value) element.style.setProperty('display', value, priority);
                else element.style.removeProperty('display');
            }
        }
    }""", 8000)


def _assert_only_inert_child_frames(page):
    assert all(frame.url in ("", "about:blank", "chrome-error://chromewebdata/")
               for frame in page.frames if frame != page.main_frame)


def test_offline_route_fulfills_one_synthetic_document_without_network():
    from playwright.sync_api import sync_playwright

    source = ('<style>.hidden { display:none }</style>'
              '<p class="hidden">HIDDEN_CSS</p><p>VISIBLE</p>')
    assert _CSP == _EXPECTED_CSP
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            context, page, requests, served = _render_offline(browser, source)
            text = page.locator("body").inner_text()
            assert "VISIBLE" in text and "HIDDEN_CSS" not in text
            assert served == [True]
            assert requests == [_DOCUMENT_URL]
            assert page.url == _DOCUMENT_URL
            with pytest.raises(Exception):
                page.reload(wait_until="load", timeout=3000)
            assert requests == [_DOCUMENT_URL, _DOCUMENT_URL]
            assert served == [True]
            context.close()
        finally:
            browser.close()


def test_original_document_retains_native_visibility_and_disables_script():
    from playwright.sync_api import sync_playwright

    cases = [
        ('<style>.hide {display:none}</style><p class="hide">SECRET</p><p>VISIBLE</p>', False),
        ('<frameset><noframes>SECRET</noframes></frameset>', False),
        ('<div popover>SECRET</div><p>VISIBLE</p>', False),
        ('<dialog>SECRET</dialog><p>VISIBLE</p>', False),
        ('<details><summary>SUMMARY</summary>SECRET</details><p>VISIBLE</p>', False),
        ('<p>VISIBLE</p><script>window.__agentcore_script_ran = true</script>', True),
        ('<p>VISIBLE</p><img src=x onerror="window.__agentcore_script_ran = true">', True),
    ]
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            for source, check_script in cases:
                context, page, requests, served = _render_offline(browser, source)
                try:
                    text = _visible_text(page)
                    assert "SECRET" not in text, source
                    if "VISIBLE" in source:
                        assert "VISIBLE" in text, source
                    if check_script:
                        assert page.evaluate("window.__agentcore_script_ran") is None
                    assert requests == [_DOCUMENT_URL]
                    assert served == [True]
                    assert len(page.frames) == 1
                finally:
                    context.close()
        finally:
            browser.close()


@pytest.mark.parametrize("resource", [
    '<iframe src="{url}"></iframe>',
    '<object data="{url}"></object>',
    '<embed src="{url}">',
    '<img src="{url}">',
    '<link rel="stylesheet" href="{url}">',
    '<style>@import "{url}";</style>',
    '<style>body {{ background:url("{url}") }}</style>',
])
def test_local_file_resources_cannot_enter_document(tmp_path, resource):
    from playwright.sync_api import sync_playwright

    sentinel = "AGENTCORE_LOCAL_FILE_SENTINEL_7f2f0d"
    stylesheet = resource.startswith("<link") or resource.startswith("<style")
    local_file = tmp_path / ("private.css" if stylesheet else "private.html")
    local_file.write_text((f"body {{ display:none }} /* {sentinel} */" if stylesheet
                           else f"<p>{sentinel}</p>"), encoding="utf-8")
    source = resource.format(url=local_file.as_uri()) + "<p>VISIBLE</p>"
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            context, page, requests, served = _render_offline(browser, source)
            try:
                assert sentinel not in _visible_text(page)
                assert "VISIBLE" in _visible_text(page)
                assert page.evaluate("getComputedStyle(document.body).display") != "none"
                if resource.startswith("<img"):
                    assert page.locator("img").evaluate("image => image.naturalWidth") == 0
                _assert_only_inert_child_frames(page)
                assert page.url == _DOCUMENT_URL
                assert requests == [_DOCUMENT_URL]
                assert served == [True]
            finally:
                context.close()
        finally:
            browser.close()


@pytest.mark.parametrize("scheme", ["data:text/html,SECRET", "blob:https://agentcore.invalid/SECRET",
                                     "javascript:document.write('SECRET')"])
def test_non_network_frame_schemes_cannot_create_active_document(scheme):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            context, page, requests, served = _render_offline(
                browser, f'<iframe src="{scheme}"></iframe><p>VISIBLE</p>')
            try:
                _assert_only_inert_child_frames(page)
                assert "SECRET" not in _visible_text(page)
                assert requests == [_DOCUMENT_URL]
                assert served == [True]
            finally:
                context.close()
        finally:
            browser.close()


def test_meta_refresh_cannot_navigate_or_obtain_network():
    from playwright.sync_api import sync_playwright

    requests_to_fixture = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_to_fixture.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NETWORK_SECRET")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        destination = f"http://127.0.0.1:{server.server_port}/escape"
        source = (f'<meta http-equiv="refresh" content="0;url={destination}">'
                  f'<img src="{destination}"><iframe src="{destination}"></iframe>'
                  '<p>VISIBLE</p>')
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                context, page, requests, served = _render_offline(browser, source)
                try:
                    page.wait_for_timeout(200)
                    assert page.url == _DOCUMENT_URL
                    assert "VISIBLE" in _visible_text(page)
                    assert "NETWORK_SECRET" not in _visible_text(page)
                    _assert_only_inert_child_frames(page)
                    assert not requests_to_fixture
                    assert served == [True]
                    assert all(url in (_DOCUMENT_URL, destination) for url in requests)
                finally:
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("destination", [
    "#changed", _DOCUMENT_URL + "#changed", _DOCUMENT_URL,
    "data:text/html,SECRET", "javascript:document.write('SECRET')",
    "file:///C:/Windows/win.ini",
])
def test_meta_refresh_cannot_navigate_to_same_document_or_other_scheme(destination):
    from playwright.sync_api import sync_playwright

    source = f'<meta http-equiv="refresh" content="0;url={destination}"><p>VISIBLE</p>'
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            context, page, requests, served = _render_offline(browser, source)
            try:
                page.wait_for_timeout(200)
                assert page.url == _DOCUMENT_URL
                assert "VISIBLE" in _visible_text(page)
                assert served == [True]
                assert requests == [_DOCUMENT_URL]
            finally:
                context.close()
        finally:
            browser.close()


def test_hostile_base_does_not_change_raw_link_resolution_authority():
    from playwright.sync_api import sync_playwright

    trusted_final_url = "https://public.example/final/page"
    source = ('<base href="https://attacker.example/">'
              '<a href="../relative">LINK</a>')
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            context, page, requests, served = _render_offline(browser, source)
            try:
                href = page.locator("a").get_attribute("href")
                assert href == "../relative"
                assert urljoin(trusted_final_url, href) == "https://public.example/relative"
                assert served == [True]
                assert requests == [_DOCUMENT_URL]
            finally:
                context.close()
        finally:
            browser.close()


def test_active_markup_and_external_resources_never_reach_local_fixture():
    from playwright.sync_api import sync_playwright

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            hits.append(("POST", self.path))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/escape"
        source = (f'<link rel="stylesheet" href="{url}">'
                  f'<style>@import "{url}"; @font-face {{font-family:x;src:url("{url}")}}'
                  f' body {{ background-image:url("{url}") }}</style>'
                  f'<img src="{url}" srcset="{url} 2x">'
                  f'<video src="{url}"><source src="{url}"></video>'
                  f'<audio src="{url}"></audio><object data="{url}"></object>'
                  f'<embed src="{url}"><iframe src="{url}"></iframe>'
                  f'<form action="{url}" method="post"><button>submit</button></form>'
                  f'<a href="{url}" ping="{url}" download>download</a>'
                  f'<script>fetch("{url}");new Worker("{url}");'
                  f'new WebSocket("ws://127.0.0.1:{server.server_port}/escape");'
                  f'window.open("{url}");navigator.serviceWorker.register("{url}")</script>'
                  '<p>VISIBLE</p>')
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                context, page, requests, served = _render_offline(browser, source)
                try:
                    page.wait_for_timeout(200)
                    assert "VISIBLE" in _visible_text(page)
                    assert page.url == _DOCUMENT_URL
                    _assert_only_inert_child_frames(page)
                    assert served == [True]
                    assert not hits
                    assert all(request in (_DOCUMENT_URL, url) for request in requests)
                finally:
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
