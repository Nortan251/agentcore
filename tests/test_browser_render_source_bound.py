"""Plaintext expansion remains bounded without rejecting accepted raw bodies."""

import io
import functools
import multiprocessing
import os

import pytest

from agentcore.browser.brokered_backend import BrokeredBrowserBackend
from agentcore.browser.config import BrowserConfig
from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.browser.fetch import SafeFetchBroker, _exchange, _fetch_document
from agentcore.browser.playwright_backend import PlaywrightBackend, _check_render_source, _serve


def _plain_document(body: bytes, charset: str, config: BrowserConfig):
    class Socket:
        def __init__(self):
            self.stream = io.BytesIO(
                (f"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset={charset}\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n").encode() + body)

        def sendall(self, _request):
            pass

        def makefile(self, _mode):
            return self.stream

        def close(self):
            pass

    return _fetch_document(
        "https://example.com/plain", "GET", config,
        resolver=lambda *_: ("8.8.8.8",),
        exchange=lambda target, method, settings: _exchange(
            target, method, settings, connector=lambda *_: Socket()))


@pytest.mark.parametrize("body,charset", [
    (b"a" * 32, "utf-8"),
    (b"&<>\"'" * 6, "utf-8"),
    ("é".encode("utf-8") * 16, "utf-8"),
    (b"\xa3" * 32, "iso-8859-1"),
    (b"\x80" * 32, "windows-1252"),
])
def test_accepted_raw_plaintext_can_render_with_expansion(body, charset):
    config = BrowserConfig(enabled=True, max_body_bytes=32)
    document = _plain_document(body, charset, config)
    assert len(document.html) <= config.render_source_limit
    assert len(document.html.encode("utf-8")) <= config.render_source_limit
    if os.environ.get("AGENTCORE_BROWSER_INTEGRATION") != "1":
        return

    class Broker:
        def fetch(self, _url):
            return document

    backend = BrokeredBrowserBackend(config, _broker=Broker())
    try:
        backend.open_url("https://example.com/plain")
        assert backend.extract_text().text.strip() == body.decode(charset)
    finally:
        backend.close()


def test_raw_body_cap_does_not_expand():
    with pytest.raises(BrowserError) as error:
        _plain_document(b"a" * 33, "utf-8", BrowserConfig(max_body_bytes=32))
    assert error.value.failure is BrowserFailure.SIZE


def _oversized_source_worker(pipe, _url, _method, _config):
    pipe.send(("ok", ("https://example.com/plain", "€" * 70)))
    pipe.close()


def test_broker_parent_checks_generated_source_utf8_bytes():
    broker = SafeFetchBroker(BrowserConfig(max_body_bytes=32),
                             _worker_target=_oversized_source_worker)
    with pytest.raises(BrowserError) as error:
        broker.fetch("https://example.com/plain")
    assert error.value.failure is BrowserFailure.SIZE


def test_renderer_parent_rejects_generated_source_before_ipc():
    config = BrowserConfig(max_body_bytes=32)
    assert config.render_source_limit == 208
    backend = PlaywrightBackend(config)
    for source in ("x" * 209, "€" * 70):
        with pytest.raises(BrowserError) as error:
            backend.render_document(source)
        assert error.value.failure is BrowserFailure.SIZE
        assert backend._worker is None


def test_renderer_worker_uses_same_generated_source_bound():
    config = BrowserConfig(max_body_bytes=32)
    for source in ("x" * 209, "€" * 70):
        with pytest.raises(BrowserError) as error:
            _check_render_source(config, source)
        assert error.value.failure is BrowserFailure.SIZE
    _check_render_source(config, "<pre>" + "&amp;" * 32 + "</pre>")


@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("multibyte", (False, True), ids=("ascii", "utf8-bytes"))
def test_static_capacity_source_boundary(delta, multibyte):
    from test_browser_response_compatibility import generated_source

    config = BrowserConfig()
    assert config.render_source_limit == 12_582_928
    source = generated_source(config.render_source_limit + delta, multibyte)
    assert len(source.encode("utf-8")) == config.render_source_limit + delta
    if delta <= 0:
        _check_render_source(config, source)
    else:
        backend = PlaywrightBackend(config)
        with pytest.raises(BrowserError) as error:
            backend.render_document(source)
        assert error.value.failure is BrowserFailure.SIZE
        assert backend._worker is None  # parent rejects before starting/IPC


@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("multibyte", (False, True), ids=("ascii", "utf8-bytes"))
def test_static_capacity_broker_source_boundary(delta, multibyte):
    from test_browser_response_compatibility import generated_source_worker

    config = BrowserConfig()
    broker = SafeFetchBroker(config, _worker_target=functools.partial(
        generated_source_worker, delta=delta, multibyte=multibyte))
    if delta <= 0:
        document = broker.fetch("https://example.com/start")
        assert len(document.html.encode("utf-8")) == config.render_source_limit + delta
    else:
        with pytest.raises(BrowserError) as error:
            broker.fetch("https://example.com/start")
        assert error.value.failure is BrowserFailure.SIZE


def test_static_capacity_plaintext_worst_escape():
    config = BrowserConfig()
    document = _plain_document(b"'" * config.max_body_bytes, "utf-8", config)
    assert document.html == "<pre>" + "&#x27;" * config.max_body_bytes + "</pre>"
    assert len(document.html.encode("utf-8")) == 12_582_923
    assert config.render_source_limit - len(document.html) == 5
    _check_render_source(config, document.html)


@pytest.mark.skipif(os.environ.get("AGENTCORE_BROWSER_INTEGRATION") != "1",
                    reason="explicit real Chromium integration gate")
@pytest.mark.parametrize("body_cap", (32, 2 * 1024 * 1024))
@pytest.mark.parametrize("multibyte", (False, True), ids=("ascii", "utf8-bytes"))
def test_worker_rejects_oversized_source_sent_directly_over_ipc(body_cap, multibyte):
    from test_browser_response_compatibility import generated_source

    config = BrowserConfig(max_body_bytes=body_cap)
    spawn = multiprocessing.get_context("spawn")
    parent, child = spawn.Pipe()
    worker = spawn.Process(target=_serve, args=(child, config),
                           daemon=True)
    try:
        worker.start()
        child.close()
        assert parent.poll(10) and parent.recv() == (True, None)
        parent.send(("render_document", generated_source(config.render_source_limit + 1, multibyte)))
        assert parent.poll(10) and parent.recv() == (False, BrowserFailure.SIZE.value)
    finally:
        parent.close()
        child.close()
        worker.join(timeout=5)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
        assert not worker.is_alive()
        worker.close()
