"""Synthetic v1 compatibility corpus: real parser, fake transport, no network.

Failure stages are TEST-ONLY assertions against the actual innermost raise
site. Public BrowserError strings remain categorical, with no new disclosure.
"""

import functools
import inspect
import io
import socket

import pytest

from agentcore.browser import fetch
from agentcore.browser.config import BrowserConfig
from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.browser.network import canonical_url
from agentcore.browser.playwright_backend import _check_render_source


CONFIG = BrowserConfig()
FETCH_DOCUMENT = fetch._fetch_document
BODY_MAX = CONFIG.max_body_bytes
LINE_MAX = 8192  # Includes name, colon, whitespace and CRLF, not just value.
HEADER_MAX = fetch._MAX_HEADER_BYTES  # Includes status line and final CRLF.
BODY = b"<p>Readable static text</p>"
TYPE = ("Content-Type", "text/html; charset=utf-8")

# Stage -> production function and guard immediately preceding its rejection.
# This deliberately fails if a fixture starts failing at a different guard.
SITES = {
    "status": (fetch._exchange, "if status != 200:"),
    "header-line": (fetch._line, "if not line.endswith"),
    "aggregate-headers": (fetch._line, "if not line.endswith"),
    "header-count": (fetch._headers, "if count > 100"),
    "duplicate-header": (fetch._headers, "if key in headers:"),
    "header-syntax": (fetch._headers, "if not _TOKEN.fullmatch"),
    "status-line": (fetch._headers, "if match is None:"),
    "content-type": (fetch._exchange, "if not content_type:"),
    "charset": (fetch._exchange, "if charset not in _CHARSETS:"),
    "encoding": (fetch._body, 'if headers.get("content-encoding"'),
    "framing-conflict": (fetch._body, "if transfer is not None and"),
    "declared-body-size": (fetch._body, "if length is not None and int(length) > cap:"),
    "eof-body-size": (fetch._body, "if len(data) > cap:"),
    "chunk-body-size": (fetch._body, "if total > cap:"),
    "chunk-syntax": (fetch._body, "if not re.fullmatch(rb"),
    "truncated-body": (fetch._exact, "if len(data) != size:"),
    "chunk-terminator": (fetch._body, "if _exact(stream, 2)"),
    "trailer": (fetch._body, "if _line(stream, 8192)"),
    "redirect": (fetch._fetch_document, "if hop == config.max_redirects"),
    "downgrade": (fetch._fetch_document, "if target.secure and"),
    "private-dns": (fetch._resolve, "if not addresses or"),
    "renderer-source": (_check_render_source, "if oversized:"),
    "localhost": (canonical_url, "if (len(host) > 253"),
    "numeric": (canonical_url, "if not public_address(str(address))"),
}


def assert_site(error, stage):
    function, guard = SITES[stage]
    lines, start = inspect.getsourcelines(function)
    guard_index = next(i for i, line in enumerate(lines) if line.strip().startswith(guard))
    raise_index = next(i for i in range(guard_index + 1, len(lines))
                       if lines[i].strip().startswith(("raise BrowserError", "raise deny")))
    traceback = error.__traceback__
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback.tb_frame.f_code is function.__code__
    assert traceback.tb_lineno == start + raise_index
    assert str(error) == error.failure.value


def wire(body=BODY, *, status=200, headers=None):
    fields = [TYPE, ("Content-Length", str(len(body)))] if headers is None else headers
    return (f"HTTP/1.1 {status} Fixture\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in fields)
            + "\r\n").encode("ascii") + body


def html_body(size, *, anchors=0):
    prefix = (b"<!doctype html><html><head><title>Static fixture</title></head><body>"
              b"<h1>Readable static page</h1>"
              + b"".join(f'<a href="/topic/{i}">Topic {i}</a>'.encode()
                         for i in range(anchors)) + b"<p>")
    suffix = b"</p></body></html>"
    remaining = size - len(prefix) - len(suffix)
    assert remaining >= 0
    text = (b"Static reference text. " * (remaining // 23 + 1))[:remaining]
    result = prefix + text + suffix
    assert len(result) == size
    return result


def large_header(name, length):
    overhead = len(name) + 4  # ': ' and CRLF
    seed = "default-src 'self'; https://static.example.test "
    value_size = length - overhead
    assert value_size >= 0
    return name, (seed * (value_size // len(seed) + 1))[:value_size]


def aggregate_wire(size):
    # Exact wire budget, including the status line and blank terminator.
    fields = [TYPE, ("Content-Length", str(len(BODY)))]
    remaining = size - (len(wire(headers=fields)) - len(BODY))
    index = 0
    while remaining:
        name = f"X-Policy-{index}"
        length = min(7000, remaining)
        assert length >= len(name) + 4
        fields.append(large_header(name, length))
        remaining -= length
        index += 1
    result = wire(headers=fields)
    assert len(result) - len(BODY) == size
    return result


def modern_page(kind):
    if kind == "wikipedia":
        body = html_body(1_782_579, anchors=400)
        headers = [TYPE, ("Content-Length", str(len(body))),
                   large_header("Content-Security-Policy", 20000)]
        headers += [(f"X-Normal-{i}", "ordinary") for i in range(30)]
        return wire(body, headers=headers)
    if kind == "python":
        return wire(html_body(64 * 1024, anchors=60))
    if kind == "search":
        return wire(b"<title>Search fallback</title><p>Enable JavaScript to search.</p>"
                    + b"<script>document.body.innerText='SCRIPT_EXECUTED';</script>" * 80)
    if kind == "search-202":
        return wire(b"<p>Search is being prepared.</p>", status=202)
    raise AssertionError("unknown synthetic page class")


class Transport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.visited = []
        self.streams = []
        self.body_reads = []
        self.requests = []
        self.closed = 0

    def connect(self, target, _timeout):
        self.visited.append(target.url)
        data = next(self.responses)
        owner = self

        class Stream(io.BytesIO):
            def read(self, size=-1):
                owner.body_reads.append(size)
                return super().read(size)

        stream = Stream(data)
        self.streams.append(stream)

        class Socket:
            def sendall(self, request):
                owner.requests.append(request)

            def makefile(self, _mode):
                return stream

            def close(self):
                owner.closed += 1

        return Socket()

    def run(self, config=CONFIG, *, resolver=lambda *_: ("8.8.8.8",)):
        return FETCH_DOCUMENT(
            "https://example.com/start", "GET", config, resolver=resolver,
            exchange=lambda target, method, settings: fetch._exchange(
                target, method, settings, connector=self.connect))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("compatibility corpus attempted real network access")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "socket", denied)


# Lazily generated data avoids committing or retaining multi-MiB fixtures.
CASES = []


def case(name, make, stage=None, failure=BrowserFailure.RESPONSE, *, body_read=False):
    CASES.append(pytest.param(make, stage, failure, body_read, id=name))


for status in (200, 202, 204, 206, 301, 302, 303, 307, 308, 400, 404, 429, 500):
    redirect = status in fetch._REDIRECTS
    case(f"status-{status}", lambda status=status: [wire(status=status,
         headers=[TYPE, ("Location", "/final"), ("Content-Length", str(len(BODY)))]), wire()],
         None if status == 200 or redirect else "status", body_read=status == 200 or redirect)

for name in ("Content-Security-Policy", "Permissions-Policy", "Link", "Report-To", "Reporting-Endpoints"):
    for size in (LINE_MAX - 1, LINE_MAX, LINE_MAX + 1, 20000):
        case(f"line-{name}-{size}", lambda name=name, size=size: [wire(headers=[
             TYPE, ("Content-Length", str(len(BODY))), large_header(name, size)])],
             "header-line" if size > LINE_MAX else None, body_read=size <= LINE_MAX)

for size in (HEADER_MAX - 1, HEADER_MAX, HEADER_MAX + 1, 65536):
    case(f"aggregate-{size}", lambda size=size: [aggregate_wire(size)],
         "aggregate-headers" if size > HEADER_MAX else None, body_read=size <= HEADER_MAX)

for count in (99, 100, 101):
    case(f"fields-{count}", lambda count=count: [wire(headers=[TYPE,
         ("Content-Length", str(len(BODY)))] + [(f"X-{i}", "v") for i in range(count - 2)])],
         "header-count" if count > 100 else None, body_read=count <= 100)

for framing in ("length", "chunked", "eof"):
    for size in (64 * 1024, 128 * 1024, 255 * 1024, BODY_MAX - 1, BODY_MAX,
                 BODY_MAX + 1, 512 * 1024, 1024 * 1024, 1_782_579):
        def make(size=size, framing=framing):
            body = html_body(size)
            if framing == "length":
                return [wire(body)]
            if framing == "eof":
                return [wire(body, headers=[TYPE])]
            return [wire(f"{size:x}\r\n".encode() + body + b"\r\n0\r\n\r\n",
                         headers=[TYPE, ("Transfer-Encoding", "chunked")])]
        stage = {"length": "declared-body-size", "eof": "eof-body-size",
                 "chunked": "chunk-body-size"}[framing] if size > BODY_MAX else None
        case(f"body-{framing}-{size}", make, stage, BrowserFailure.SIZE,
             body_read=framing == "eof" or size <= BODY_MAX)

for name, body, headers, stage in (
    ("zero-length", b"", [TYPE, ("Content-Length", "0")], None),
    ("zero-eof", b"", [TYPE], None),
    ("zero-chunked", b"0\r\n\r\n", [TYPE, ("Transfer-Encoding", "chunked")], None),
    ("conflicting-length", BODY, [TYPE, ("Content-Length", "1"), ("Content-Length", "2")], "duplicate-header"),
    ("repeated-length", BODY, [TYPE, ("Content-Length", "1"), ("Content-Length", "1")], "duplicate-header"),
    ("te-and-length", BODY, [TYPE, ("Content-Length", "1"), ("Transfer-Encoding", "chunked")], "framing-conflict"),
    ("malformed-chunk", b"g\r\n", [TYPE, ("Transfer-Encoding", "chunked")], "chunk-syntax"),
    ("truncated-chunk", b"8\r\nx", [TYPE, ("Transfer-Encoding", "chunked")], "truncated-body"),
    ("bad-chunk-terminator", b"1\r\nxXX", [TYPE, ("Transfer-Encoding", "chunked")], "chunk-terminator"),
    ("bad-trailer", b"0\r\nReport-To: ignored\r\n\r\n", [TYPE, ("Transfer-Encoding", "chunked")], "trailer"),
    ("folded-header", BODY, [TYPE, (" X-Folded", "value")], "header-syntax"),
    ("truncated-length", BODY, [TYPE, ("Content-Length", str(len(BODY) + 1))], "truncated-body"),
):
    case(f"framing-{name}", lambda body=body, headers=headers: [wire(body, headers=headers)], stage,
         body_read=name != "zero-chunked" and stage in (None, "truncated-body", "chunk-terminator"))
case("invalid-status-line", lambda: [b"HTTP/2 200 OK\r\n\r\n"], "status-line")

for encoding in ("identity", "gzip", "deflate", "br", "unknown"):
    case(f"encoding-{encoding}", lambda encoding=encoding: [wire(headers=[TYPE,
         ("Content-Length", str(len(BODY))), ("Content-Encoding", encoding)])],
         None if encoding == "identity" else "encoding", body_read=encoding == "identity")

for name, content_type, body, stage in (
    ("html-utf8", "text/html; charset=utf-8", BODY, None),
    ("plain-utf8", "text/plain; charset=utf-8", b"<& readable", None),
    ("ascii", "text/html; charset=us-ascii", BODY, None),
    ("latin1", "text/html; charset=iso-8859-1", b"<p>\xa3</p>", None),
    ("windows1252", "text/html; charset=windows-1252", b"<p>\x80</p>", None),
    ("unsupported", "text/html; charset=utf-7", BODY, "charset"),
    ("missing-charset", "text/html", BODY, None),
    ("json", "application/json", b"{}", "content-type"),
    ("octet-stream", "application/octet-stream", BODY, "content-type"),
    ("malformed", "text/html; charset=", BODY, "content-type"),
    ("missing-type", None, BODY, "content-type"),
    ("invalid-utf8-replaced", "text/html; charset=utf-8", b"<p>\xff</p>", None),
):
    case(f"type-{name}", lambda content_type=content_type, body=body: [wire(body, headers=[
         ("Content-Length", str(len(body)))] + ([] if content_type is None else [("Content-Type", content_type)]))],
         stage, body_read=stage is None)

for kind, stage in (("wikipedia", "header-line"), ("python", None), ("search", None), ("search-202", "status")):
    case(f"modern-{kind}", lambda kind=kind: [modern_page(kind)], stage, body_read=stage is None)
case("modern-wikipedia-ordinary-headers", lambda: [wire(html_body(1_782_579, anchors=400))],
     "declared-body-size", BrowserFailure.SIZE)


@pytest.mark.parametrize("make,stage,failure,body_read", CASES)
def test_response_corpus(make, stage, failure, body_read):
    responses = make()
    transport = Transport(responses)
    if stage is None:
        document = transport.run()
        assert isinstance(document.html, str)
        _check_render_source(CONFIG, document.html)
        if b"<& readable" in responses[0]:
            assert document.html == "<pre>&lt;&amp; readable</pre>"
        if b"<p>\xff</p>" in responses[0]:
            assert document.html == "<p>\ufffd</p>"
        if b"<p>\xa3</p>" in responses[0]:
            assert document.html == "<p>£</p>"
        if b"<p>\x80</p>" in responses[0]:
            assert document.html == "<p>€</p>"
    else:
        with pytest.raises(BrowserError) as caught:
            transport.run()
        assert caught.value.failure is failure
        assert_site(caught.value, stage)
        if stage in ("header-line", "aggregate-headers"):
            # Disambiguate the shared _line guard from its live frame locals.
            traceback = caught.value.__traceback__
            while traceback.tb_next:
                traceback = traceback.tb_next
            remaining = traceback.tb_frame.f_locals["remaining"]
            assert (remaining < LINE_MAX) is (stage == "aggregate-headers")
    assert bool(transport.body_reads) is body_read
    assert transport.closed == len(transport.visited)
    assert all(b"Accept-Encoding: identity\r\n" in request for request in transport.requests)


REDIRECT_CASES = [
    ("relative", "/final", None, None, 2),
    ("absolute", "https://other.example/final", None, None, 2),
    ("fragment", "#section", None, None, 2),
    ("query", "?q=next", None, None, 2),
    ("missing", None, "redirect", BrowserFailure.REDIRECT, 1),
    # urljoin raises internally; _fetch_worker sanitizes to FETCH (below).
    ("malformed", "https://[:::]/", "urljoin", BrowserFailure.FETCH, 1),
    ("downgrade", "http://example.com/final", "downgrade", BrowserFailure.REDIRECT, 1),
    ("private-dns", "https://private.example/", "private-dns", BrowserFailure.DESTINATION, 1),
    ("localhost", "https://localhost/", "localhost", BrowserFailure.DESTINATION, 1),
    ("numeric", "https://127.0.0.1/", "numeric", BrowserFailure.DESTINATION, 1),
]


@pytest.mark.parametrize("name,location,stage,failure,hops", REDIRECT_CASES,
                         ids=[row[0] for row in REDIRECT_CASES])
def test_redirect_corpus(name, location, stage, failure, hops):
    fields = [TYPE] + ([] if location is None else [("Location", location)])
    transport = Transport([wire(status=302, headers=fields), wire()])
    resolved = []

    def resolver(host, _port):
        resolved.append(host)
        return ("10.0.0.1",) if host == "private.example" else ("8.8.8.8",)

    if failure is None:
        document = transport.run(resolver=resolver)
        assert document.final_url == {
            "relative": "https://example.com/final", "absolute": "https://other.example/final",
            "fragment": "https://example.com/start", "query": "https://example.com/start?q=next",
        }[name]
    elif stage == "urljoin":
        with pytest.raises(ValueError):
            transport.run(resolver=resolver)
        assert resolved == ["example.com"]
    else:
        with pytest.raises(BrowserError) as caught:
            transport.run(resolver=resolver)
        assert caught.value.failure is failure
        assert str(caught.value) == failure.value
        if stage:
            assert_site(caught.value, stage)
            if stage in ("localhost", "numeric"):
                assert resolved == ["example.com"]
        else:
            # Canonical URL rejection precedes resolution/dial of next hop.
            assert resolved == ["example.com"]
    assert len(transport.visited) == hops
    assert transport.closed == hops


def test_malformed_location_worker_is_categorical(monkeypatch):
    transport = Transport([wire(status=302, headers=[("Location", "https://[:::]/")])])
    monkeypatch.setattr(fetch, "_fetch_document", lambda *_a, **_k: transport.run())

    class Pipe:
        messages = []

        def send(self, message):
            self.messages.append(message)

        def close(self):
            pass

    pipe = Pipe()
    fetch._fetch_worker(pipe, "https://example.com/?PRIVATE_QUERY", "GET", CONFIG)
    assert pipe.messages == [("error", BrowserFailure.FETCH.value)]
    assert len(transport.visited) == transport.closed == 1


@pytest.mark.parametrize("count", (CONFIG.max_redirects - 1, CONFIG.max_redirects, CONFIG.max_redirects + 1))
def test_redirect_count_boundary(count):
    transport = Transport([wire(status=302, headers=[("Location", f"/hop/{i}")])
                           for i in range(count)] + [wire()])
    if count <= CONFIG.max_redirects:
        transport.run()
        assert len(transport.visited) == count + 1
    else:
        with pytest.raises(BrowserError) as caught:
            transport.run()
        assert caught.value.failure is BrowserFailure.REDIRECT
        assert_site(caught.value, "redirect")
        assert len(transport.visited) == CONFIG.max_redirects + 1


def generated_source(size, multibyte):
    return ("€" * (size // 3) + "x" * (size % 3)) if multibyte else "x" * size


@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("multibyte", (False, True), ids=("ascii", "utf8-bytes"))
def test_renderer_generated_source_boundary(delta, multibyte):
    source = generated_source(CONFIG.render_source_limit + delta, multibyte)
    assert len(source.encode("utf-8")) == CONFIG.render_source_limit + delta
    if delta <= 0:
        _check_render_source(CONFIG, source)
    else:
        with pytest.raises(BrowserError) as caught:
            _check_render_source(CONFIG, source)
        assert caught.value.failure is BrowserFailure.SIZE
        assert_site(caught.value, "renderer-source")


def test_wikipedia_source_also_exceeds_current_renderer_bound():
    source = html_body(1_782_579, anchors=400).decode("ascii")
    with pytest.raises(BrowserError) as caught:
        _check_render_source(CONFIG, source)
    assert caught.value.failure is BrowserFailure.SIZE
    assert_site(caught.value, "renderer-source")


def generated_source_worker(pipe, _url, _method, config, *, delta, multibyte):
    # Real spawned IPC, no DNS/socket calls. Exercise the broker parent bound.
    pipe.send(("ok", ("https://example.com/start",
                      generated_source(config.render_source_limit + delta, multibyte))))
    pipe.close()


@pytest.mark.parametrize("delta", (-1, 0, 1))
@pytest.mark.parametrize("multibyte", (False, True), ids=("ascii", "utf8-bytes"))
def test_broker_generated_source_boundary(delta, multibyte):
    broker = fetch.SafeFetchBroker(CONFIG, _worker_target=functools.partial(
        generated_source_worker, delta=delta, multibyte=multibyte))
    if delta <= 0:
        document = broker.fetch("https://example.com/start")
        assert len(document.html.encode("utf-8")) == CONFIG.render_source_limit + delta
    else:
        with pytest.raises(BrowserError) as caught:
            broker.fetch("https://example.com/start")
        assert caught.value.failure is BrowserFailure.SIZE
        assert str(caught.value) == BrowserFailure.SIZE.value
