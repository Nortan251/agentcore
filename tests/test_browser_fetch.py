import io
import functools
import socket
import ssl
import threading
import time

import pytest

from agentcore.browser.config import BrowserConfig
from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.browser import fetch


def response(body=b"<p>document</p>", *, status="200 OK", headers=None):
    headers = headers if headers is not None else {"Content-Type": "text/html", "Content-Length": str(len(body))}
    return (f"HTTP/1.1 {status}\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n").encode() + body


class Socket:
    def __init__(self, data=None, fail=None):
        self.data = data if data is not None else response()
        self.fail = fail
        self.destination = self.request = self.timeout = None
        self.closed = False

    def connect(self, destination):
        self.destination = destination
        if self.fail == "connect":
            raise socket.timeout("OS_SECRET address=PRIVATE")

    def getpeername(self):
        return self.destination

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, request):
        self.request = request

    def makefile(self, mode):
        if self.fail == "read":
            raise socket.timeout("OS_SECRET query=SECRET")
        return io.BytesIO(self.data)

    def close(self):
        self.closed = True


def document(url="https://example.com/path?secret=QUERY", *, addresses=("8.8.8.8",), data=None, cap=262144):
    sockets = []
    resolved = []
    def resolver(host, port):
        resolved.append((host, port))
        return addresses
    def connector(target, timeout):
        sock = Socket(data)
        sockets.append(sock)
        return fetch._connect(target, timeout, socket_factory=lambda *_: sock,
                              tls_factory=lambda: TLS())
    class TLS:
        def wrap_socket(self, sock, server_hostname):
            sock.sni = server_hostname
            return sock
    config = BrowserConfig(max_body_bytes=cap)
    exchange = lambda target, method, config: fetch._exchange(target, method, config, connector=connector)
    result = fetch._fetch_document(url, "GET", config, resolver=resolver, exchange=exchange)
    return result, sockets, resolved


def test_literal_public_address_no_dns_and_exact_connect():
    result, sockets, resolved = document("https://8.8.8.8/")
    assert resolved == []
    assert sockets[0].destination == ("8.8.8.8", 443)
    assert sockets[0].closed
    assert result.final_url == "https://8.8.8.8/"


def test_public_dns_exact_pin_host_sni_and_no_second_lookup(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kw: pytest.fail("unexpected second DNS"))
    result, sockets, resolved = document()
    assert resolved == [("example.com", 443)]
    assert sockets[0].destination == ("8.8.8.8", 443)
    assert sockets[0].sni == "example.com"
    assert b"Host: example.com\r\n" in sockets[0].request
    assert sockets[0].request.startswith(b"GET /path?secret=QUERY HTTP/1.1\r\n")
    assert b"Accept-Encoding: identity\r\n" in sockets[0].request
    assert "QUERY" not in repr(result)


def test_rebinding_retained_answers_never_resolved_at_dial():
    calls = []
    def resolver(*_):
        calls.append("resolve")
        return ["8.8.8.8"] if len(calls) == 1 else ["127.0.0.1"]
    target = fetch._resolve("https://example.com/", resolver, lambda _: None)
    sock = Socket()
    class TLS:
        def wrap_socket(self, sock, server_hostname):
            return sock
    fetch._connect(target, 1, socket_factory=lambda *_: sock, tls_factory=TLS)
    assert calls == ["resolve"]
    assert target.addresses == ("8.8.8.8",)
    assert sock.destination == ("8.8.8.8", 443)


@pytest.mark.parametrize("addresses", [["8.8.8.8", "127.0.0.1"], ["::1"], [], ["bad"],
    ["2001:4860::8888%lo"], ["192.0.0.9"], ["2001:20::1"], ["192.88.99.1"]])
def test_all_dns_answers_checked_before_connect(addresses):
    with pytest.raises(BrowserError) as error:
        fetch._fetch_document("https://example.com/", "GET", BrowserConfig(), resolver=lambda *_: addresses,
                              exchange=lambda *_: pytest.fail("must not connect"))
    assert error.value.failure is BrowserFailure.DESTINATION


def test_dns_failure_sanitized():
    def resolver(*_):
        raise OSError("SECRET DNS OS address")
    with pytest.raises(BrowserError) as error:
        fetch._resolve("https://example.com/?secret=QUERY", resolver, lambda _: None)
    assert str(error.value) == BrowserFailure.DNS.value


def test_ipv6_numeric_sockaddr_and_original_host_header():
    result, sockets, _ = document("https://[2001:4860:4860::8888]/")
    assert sockets[0].destination == ("2001:4860:4860::8888", 443, 0, 0)
    assert b"Host: [2001:4860:4860::8888]\r\n" in sockets[0].request
    assert sockets[0].sni == "2001:4860:4860::8888"


def test_real_tls_context_keeps_hostname_verification_and_disables_keylogging(monkeypatch, tmp_path):
    path = tmp_path / "keylog"
    monkeypatch.setenv("SSLKEYLOGFILE", str(path))
    context = fetch._tls_context()
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.keylog_filename is None
    assert not path.exists()


def test_peer_address_mismatch_closes_socket():
    target = fetch._resolve("https://example.com/", lambda *_: ["8.8.8.8"], lambda _: None)
    sock = Socket()
    sock.getpeername = lambda: ("127.0.0.1", 443)
    with pytest.raises(BrowserError):
        fetch._connect(target, 1, socket_factory=lambda *_: sock)
    assert sock.closed


def test_redirect_revalidates_resolves_and_pins_each_hop():
    targets = []
    resolved = []
    def resolver(host, port):
        resolved.append(host)
        return ["8.8.8.8"] if host == "example.com" else ["1.1.1.1"]
    def exchange(target, *_):
        targets.append(target)
        return (302, "https://other.com/final", None) if len(targets) == 1 else (200, None, "<p>done</p>")
    result = fetch._fetch_document("https://example.com/", "GET", BrowserConfig(), resolver=resolver, exchange=exchange)
    assert resolved == ["example.com", "other.com"]
    assert [target.addresses for target in targets] == [("8.8.8.8",), ("1.1.1.1",)]
    assert result.final_url == "https://other.com/final"


@pytest.mark.parametrize("location", ["https://localhost/", "https://127.0.0.1/", "https://10.0.0.1/",
    "https://[::1]/", "https://user:pass@example.com/", "file:///secret", "http://example.com/",
    "https://example.com:8443/", "https://0177.0.0.1/", "https://2130706433/"])
def test_redirect_escape_denied_before_second_connect(location):
    calls = []
    def exchange(*_):
        calls.append("connect")
        return (302, location, None)
    with pytest.raises(BrowserError):
        fetch._fetch_document("https://example.com/", "GET", BrowserConfig(),
            resolver=lambda *_: ["8.8.8.8"], exchange=exchange)
    assert calls == ["connect"]


def test_redirect_dns_private_and_count_bound():
    connects = []
    def exchange(*_):
        connects.append(1)
        return 302, "https://private.com/", None
    with pytest.raises(BrowserError):
        fetch._fetch_document("https://example.com/", "GET", BrowserConfig(),
            resolver=lambda host, port: ["127.0.0.1"] if host == "private.com" else ["8.8.8.8"], exchange=exchange)
    assert len(connects) == 1
    connects.clear()
    with pytest.raises(BrowserError) as error:
        fetch._fetch_document("https://example.com/", "GET", BrowserConfig(max_redirects=2),
            resolver=lambda *_: ["8.8.8.8"], exchange=exchange)
    assert error.value.failure is BrowserFailure.REDIRECT
    assert len(connects) == 3


@pytest.mark.parametrize("data", [
    b"HTTP/1.1 200 OK\nContent-Type: text/html\n\nhi",
    b"HTTP/1.1 200 OK\r\n Content-Type: text/html\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Type: text/plain\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: -1\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nX: \x00\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nX: \xff\r\n\r\n",
    b"HTTP/2 200 OK\r\n\r\n", response(status="500 ERROR"), response(headers={}),
    response(headers={"Content-Type": "application/octet-stream"}),
    response(headers={"Content-Type": "application/xhtml+xml"}),
    response(headers={"Content-Type": "text/html; charset=utf-7"}),
    response(headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}),
    response(headers={"Content-Type": "text/html", "Content-Encoding": "br"}),
    b"HTTP/1.1 200 OK\r\n" + b"X: " + b"a" * 8192 + b"\r\n\r\n",
])
def test_strict_headers_status_types_and_no_uncontrolled_decompression(data):
    with pytest.raises(BrowserError):
        document(data=data)


@pytest.mark.parametrize("data", [response(b"x" * 21), response(b"x" * 21, headers={"Content-Type": "text/html"}),
    response(b"15\r\n" + b"x" * 21 + b"\r\n0\r\n\r\n", headers={"Content-Type": "text/html", "Transfer-Encoding": "chunked"})])
def test_body_byte_bound_all_framings(data):
    with pytest.raises(BrowserError) as error:
        document(data=data, cap=20)
    assert error.value.failure is BrowserFailure.SIZE


@pytest.mark.parametrize("body", [b"-1\r\n", b"g\r\n", b"1;extension=x\r\na\r\n0\r\n\r\n",
                                  b"1\r\naXX0\r\n\r\n", b"0\r\nSet-Cookie: SECRET\r\n\r\n"])
def test_malformed_chunk_framing_rejected(body):
    with pytest.raises(BrowserError):
        document(data=response(body, headers={"Content-Type": "text/html", "Transfer-Encoding": "chunked"}))


def test_valid_chunked_and_plaintext_and_no_cookie_persistence():
    result, _, _ = document(data=response(b"2\r\nhi\r\n0\r\n\r\n", headers={"Content-Type": "text/plain", "Transfer-Encoding": "chunked", "Set-Cookie": "SECRET"}))
    assert result.html == "<pre>hi</pre>"
    assert not hasattr(result, "headers") and "SECRET" not in repr(result)


def test_environment_proxies_never_used(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(key, "http://user:SECRET@127.0.0.1:9999")
    _, sockets, _ = document()
    assert sockets[0].destination == ("8.8.8.8", 443)
    assert b"Authorization" not in sockets[0].request
    assert b"Cookie" not in sockets[0].request
    assert b"SECRET" not in sockets[0].request


@pytest.mark.parametrize("mode", ["connect", "read"])
def test_connect_read_timeout_worker_boundary_sanitizes(mode, monkeypatch):
    target = fetch._resolve("http://example.com/?QUERY", lambda *_: ["8.8.8.8"], lambda _: None)
    sock = Socket(fail=mode)
    def operation(*_args, **_kwargs):
        connector = lambda target, timeout: fetch._connect(target, timeout, socket_factory=lambda *_: sock)
        fetch._exchange(target, "GET", BrowserConfig(), connector=connector)
    monkeypatch.setattr(fetch, "_fetch_document", operation)
    class Pipe:
        def __init__(self):
            self.messages = []
        def send(self, message):
            self.messages.append(message)
        def close(self):
            pass
    pipe = Pipe()
    fetch._fetch_worker(pipe, "https://example.com/?QUERY", "GET", BrowserConfig())
    assert pipe.messages == [("error", BrowserFailure.TIMEOUT.value)]
    assert sock.closed


def dns_hung_worker(pipe, url, method, config):
    pipe.send(("stage", "dns"))
    time.sleep(30)


def test_parent_owned_dns_hard_deadline():
    broker = fetch.SafeFetchBroker(BrowserConfig(dns_timeout_seconds=.15, operation_timeout_seconds=5),
                                  _worker_target=dns_hung_worker)
    before = time.monotonic()
    with pytest.raises(BrowserError) as error:
        broker.fetch("https://example.com/")
    assert error.value.failure is BrowserFailure.TIMEOUT
    assert time.monotonic() - before < 3


@pytest.mark.parametrize("url", ["http://localhost/", "file:///secret", "http://127.1/", "https://user:SECRET@example.com/",
                                  "https://example.com:8443/", "http://[::ffff:8.8.8.8]/"])
def test_production_broker_rejects_before_spawn(url, monkeypatch):
    monkeypatch.setattr(fetch.multiprocessing, "get_context", lambda *_: pytest.fail("invalid URL must not spawn"))
    with pytest.raises(BrowserError):
        fetch.SafeFetchBroker(BrowserConfig()).fetch(url)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CONNECT"])
def test_only_get_head_methods_before_spawn(method, monkeypatch):
    monkeypatch.setattr(fetch.multiprocessing, "get_context", lambda *_: pytest.fail("must not spawn"))
    with pytest.raises(BrowserError):
        fetch.SafeFetchBroker(BrowserConfig()).fetch("https://example.com/", method)


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_partial_ipc_frame_cannot_defeat_deadline_and_cleanup_is_categorical(monkeypatch, cleanup_error):
    import threading
    stopped = threading.Event()
    class Pipe:
        def recv(self):
            stopped.wait(10)  # some frame bytes arrived, but recv never completes
            raise EOFError
        def close(self):
            pass
    class Worker:
        pid = None
        def start(self):
            self.pid = 42
        def is_alive(self):
            return not stopped.is_set()
        def join(self, timeout):
            pass
        def terminate(self):
            stopped.set()
        kill = terminate
        def close(self):
            if cleanup_error:
                raise OSError("QUERY_SECRET OS_ADDRESS")
    class Spawn:
        def Pipe(self, **kwargs):
            return Pipe(), Pipe()
        def Process(self, **kwargs):
            return Worker()
    monkeypatch.setattr(fetch.multiprocessing, "get_context", lambda *_: Spawn())
    before = time.monotonic()
    with pytest.raises(BrowserError) as error:
        fetch.SafeFetchBroker(BrowserConfig(operation_timeout_seconds=1)).fetch("https://example.com/?QUERY_SECRET")
    assert error.value.failure is (BrowserFailure.CLEANUP if cleanup_error else BrowserFailure.TIMEOUT)
    assert "SECRET" not in str(error.value) and "ADDRESS" not in str(error.value)
    assert stopped.is_set() and time.monotonic() - before < 3


def completion_worker(pipe, url, method, config, *, case):
    """Real spawn/IPC fixture; no resolver, socket or HTTP operation."""
    try:
        if case in ("progress_ok", "progress_error", "progress_only"):
            pipe.send(("stage", "dns"))
        if case in ("progress_ok", "progress_error"):
            pipe.send(("stage", "connect"))
        if case == "progress_error":
            pipe.send(("error", BrowserFailure.DESTINATION.value))
        elif case == "truncated":
            # Transport frame contains only the pickle protocol prefix. recv()
            # encounters EOF before a complete protocol message can exist.
            pipe.send_bytes(b"\x80\x04")
        elif case == "malformed":
            # Pickle declares an eight-byte payload frame but supplies one.
            pipe.send_bytes(b"\x80\x04\x95\x08\x00\x00\x00\x00\x00\x00\x00N")
        elif case == "never_terminal":
            threading.Event().wait(30)  # parent deadline must terminate us
        elif case != "progress_only":
            pipe.send(("ok", (url, "<p>completed</p>")))
        # No sleep after the terminal send; process exits immediately.
    finally:
        pipe.close()


def completion_broker(monkeypatch, case, *, delay_reader=True, cleanup_error=False):
    """Select a valid scheduling order with join, not timing-only sleeps."""
    spawn = fetch.multiprocessing.get_context("spawn")
    real_thread = threading.Thread
    workers, exited_before_receive = [], []

    class Worker:
        def __init__(self, process):
            self.process = process

        def __getattr__(self, name):
            return getattr(self.process, name)

        def close(self):
            self.process.close()
            if cleanup_error:
                raise OSError("PRIVATE_CLEANUP_SECRET")

    class Spawn:
        def Pipe(self, **kwargs):
            return spawn.Pipe(**kwargs)

        def Process(self, **kwargs):
            worker = Worker(spawn.Process(**kwargs))
            workers.append(worker)
            return worker

    class Reader(real_thread):
        def start(self):
            if delay_reader:
                workers[0].join(timeout=5)
                assert not workers[0].is_alive(), "fixture worker did not exit"
                assert workers[0].exitcode == 0
                exited_before_receive.append(True)
            return super().start()

    monkeypatch.setattr(fetch.multiprocessing, "get_context", lambda _: Spawn())
    monkeypatch.setattr(fetch.threading, "Thread", Reader)
    config = BrowserConfig(operation_timeout_seconds=5 if delay_reader else 1)
    broker = fetch.SafeFetchBroker(config,
        _worker_target=functools.partial(completion_worker, case=case))
    return broker, workers, exited_before_receive


@pytest.mark.parametrize("case,expected", [
    ("progress_ok", None),
    ("progress_error", BrowserFailure.DESTINATION),
    ("progress_only", BrowserFailure.FETCH),
    ("terminal_only", None),
    ("truncated", BrowserFailure.FETCH),
    ("malformed", BrowserFailure.FETCH),
])
def test_ordered_completion_after_confirmed_worker_exit(monkeypatch, case, expected):
    broker, workers, observed_exit = completion_broker(monkeypatch, case)
    if expected is None:
        document = broker.fetch("https://example.com/")
        assert document.final_url == "https://example.com/"
        assert document.html == "<p>completed</p>"
    else:
        with pytest.raises(BrowserError) as error:
            broker.fetch("https://example.com/")
        assert error.value.failure is expected
    assert observed_exit == [True]  # exited before ANY parent receive
    assert workers[0].process._closed


def test_terminal_success_immediately_before_exit(monkeypatch):
    broker, workers, observed_exit = completion_broker(
        monkeypatch, "terminal_only", delay_reader=False)
    assert broker.fetch("https://example.com/").html == "<p>completed</p>"
    assert observed_exit == [] and workers[0].process._closed


def test_live_worker_without_terminal_still_has_hard_deadline(monkeypatch):
    broker, workers, _ = completion_broker(monkeypatch, "never_terminal", delay_reader=False)
    before = time.monotonic()
    with pytest.raises(BrowserError) as error:
        broker.fetch("https://example.com/")
    assert error.value.failure is BrowserFailure.TIMEOUT
    assert time.monotonic() - before < 3
    assert workers[0].process._closed


def test_real_completion_cleanup_uncertainty_overrides_success(monkeypatch):
    broker, workers, observed_exit = completion_broker(
        monkeypatch, "terminal_only", cleanup_error=True)
    with pytest.raises(BrowserError) as error:
        broker.fetch("https://example.com/")
    assert error.value.failure is BrowserFailure.CLEANUP
    assert "PRIVATE_CLEANUP_SECRET" not in str(error.value)
    assert observed_exit == [True] and workers[0].process._closed
