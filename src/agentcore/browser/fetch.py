"""AgentCore-owned resolve/check/PIN broker, independent of Chromium.

No HTTP client/proxy/connection pool may re-resolve the destination. Numeric
sockaddr connect is the only dial seam. No headers, IPs, cookies or provider
exceptions cross the public failure boundary. Tests inject private primitives;
there is no production allow-private flag or model transport configuration.
"""

from dataclasses import dataclass, field
import html
import ipaddress
import multiprocessing
import os
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit

from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure
from .network import canonical_url, public_address, system_resolver

_STATUS = re.compile(rb"HTTP/1\.[01] ([2-5][0-9]{2}) [\x20-\x7e]*\r\n\Z")
_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_CONTENT_TYPE = re.compile(r'(text/html|text/plain)(?:\s*;\s*charset\s*=\s*"?([a-z0-9-]+)"?)?\Z', re.I)
_CHARSETS = {"utf-8": "utf-8", "us-ascii": "ascii", "iso-8859-1": "iso-8859-1", "windows-1252": "cp1252"}
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_HEADER_BYTES = 32768


@dataclass(frozen=True, repr=False)
class FetchedDocument:
    # Host-only ephemeral result. Conversation receives projections, not this.
    final_url: str = field(repr=False)
    html: str = field(repr=False)

    def __repr__(self):
        return "<bounded fetched document>"


@dataclass(frozen=True, repr=False)
class _Target:
    url: str
    host: str
    port: int
    secure: bool
    addresses: tuple[str, ...]


def _resolve(url, resolver, stage):
    normalized = canonical_url(url)
    parts = urlsplit(normalized)
    host = parts.hostname
    port = 443 if parts.scheme == "https" else 80
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        stage("dns")
        try:
            # System resolver returns all A/AAAA answers. Bound hostile fake
            # resolver output too; no filtering out an unsafe answer.
            addresses = tuple(resolver(host, port))
        except Exception:
            raise BrowserError(BrowserFailure.DNS) from None
        if not addresses or len(addresses) > 64 or any(not public_address(ip) for ip in addresses):
            raise BrowserError(BrowserFailure.DESTINATION)
        addresses = tuple(dict.fromkeys(str(ipaddress.ip_address(ip)) for ip in addresses))
    else:
        addresses = (str(address),)
    stage("connect")
    return _Target(normalized, host, port, parts.scheme == "https", addresses)


def _tls_context():
    # create_default_context can inherit SSLKEYLOGFILE. Construct explicitly.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.keylog_filename = None
    context.load_default_certs()
    context.set_alpn_protocols(["http/1.1"])
    return context


def _connect(target, timeout, socket_factory=socket.socket, tls_factory=_tls_context):
    # Select one retained, validated answer. No retries/second DNS selection.
    ip = target.addresses[0]
    address = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket_factory(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        destination = (ip, target.port, 0, 0) if address.version == 6 else (ip, target.port)
        sock.connect(destination)  # numeric sockaddr, never hostname resolution
        if ipaddress.ip_address(sock.getpeername()[0]) != address:
            raise BrowserError(BrowserFailure.FETCH)
        if target.secure:
            sock = tls_factory().wrap_socket(sock, server_hostname=target.host)
        return sock
    except BaseException:
        sock.close()
        raise


def _line(stream, remaining):
    line = stream.readline(min(8192, remaining) + 1)
    if not line.endswith(b"\r\n") or len(line) > min(8192, remaining):
        raise BrowserError(BrowserFailure.RESPONSE)
    return line


def _headers(stream):
    status_line = _line(stream, _MAX_HEADER_BYTES)
    match = _STATUS.fullmatch(status_line)
    if match is None:
        raise BrowserError(BrowserFailure.RESPONSE)
    status = int(match[1])
    remaining = _MAX_HEADER_BYTES - len(status_line)
    headers = {}
    count = 0
    while True:
        line = _line(stream, remaining)
        remaining -= len(line)
        if line == b"\r\n":
            break
        count += 1
        if count > 100 or b":" not in line:
            raise BrowserError(BrowserFailure.RESPONSE)
        name, value = line[:-2].split(b":", 1)
        if not _TOKEN.fullmatch(name) or any(c < 32 and c != 9 or c == 127 for c in value):
            raise BrowserError(BrowserFailure.RESPONSE)
        key = name.decode("ascii").lower()
        # Never store cookie values, even in worker response objects.
        if key == "set-cookie":
            continue
        if key in headers:
            raise BrowserError(BrowserFailure.RESPONSE)
        try:
            headers[key] = value.strip().decode("ascii", errors="strict")
        except UnicodeError:
            raise BrowserError(BrowserFailure.RESPONSE) from None
    return status, headers


def _exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise BrowserError(BrowserFailure.RESPONSE)
    return data


def _body(stream, headers, cap, method):
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise BrowserError(BrowserFailure.RESPONSE)
    length = headers.get("content-length")
    transfer = headers.get("transfer-encoding")
    if transfer is not None and (length is not None or transfer.lower() != "chunked"):
        raise BrowserError(BrowserFailure.RESPONSE)
    if length is not None and not re.fullmatch(r"[0-9]{1,10}", length):
        raise BrowserError(BrowserFailure.RESPONSE)
    if length is not None and int(length) > cap:
        raise BrowserError(BrowserFailure.SIZE)
    if method == "HEAD":
        return b""
    if transfer is not None:
        chunks = []
        total = 0
        while True:
            line = _line(stream, 32)
            if not re.fullmatch(rb"[0-9a-fA-F]{1,8}\r\n", line):
                raise BrowserError(BrowserFailure.RESPONSE)
            size = int(line[:-2], 16)
            if size == 0:
                # Trailer fields are not supported or persisted in v1.
                if _line(stream, 8192) != b"\r\n":
                    raise BrowserError(BrowserFailure.RESPONSE)
                break
            total += size
            if total > cap:
                raise BrowserError(BrowserFailure.SIZE)
            chunks.append(_exact(stream, size))
            if _exact(stream, 2) != b"\r\n":
                raise BrowserError(BrowserFailure.RESPONSE)
        return b"".join(chunks)
    data = _exact(stream, int(length)) if length is not None else stream.read(cap + 1)
    if len(data) > cap:
        raise BrowserError(BrowserFailure.SIZE)
    return data


def _exchange(target, method, config, *, connector=_connect):
    sock = connector(target, config.operation_timeout_seconds)
    try:
        parts = urlsplit(target.url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        host = f"[{target.host}]" if ":" in target.host else target.host
        request = (f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
                   "User-Agent: AgentCore-BrowserRead/1\r\nAccept: text/html, text/plain\r\n"
                   "Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        with sock.makefile("rb") as stream:
            status, headers = _headers(stream)
            if status in _REDIRECTS:
                # No automatic redirect, cookie, authorization or body replay.
                return status, headers.get("location"), None
            if status != 200:
                raise BrowserError(BrowserFailure.RESPONSE)
            content_type = _CONTENT_TYPE.fullmatch(headers.get("content-type", ""))
            if not content_type:
                raise BrowserError(BrowserFailure.RESPONSE)
            charset = (content_type[2] or "utf-8").lower()
            if charset not in _CHARSETS:
                raise BrowserError(BrowserFailure.RESPONSE)
            body = _body(stream, headers, config.max_body_bytes, method)
            text = body.decode(_CHARSETS[charset], errors="replace")
            if content_type[1].lower() == "text/plain":
                # Escaping expands bytes; renderer input has its own bound.
                text = "<pre>" + html.escape(text) + "</pre>"
            return status, None, text
    finally:
        sock.close()


def _fetch_document(url, method, config, *, resolver=system_resolver, exchange=_exchange, stage=lambda _: None):
    if method not in ("GET", "HEAD"):
        raise BrowserError(BrowserFailure.DESTINATION)
    current = canonical_url(url)
    for hop in range(config.max_redirects + 1):
        target = _resolve(current, resolver, stage)
        status, location, content = exchange(target, method, config)
        if status not in _REDIRECTS:
            return FetchedDocument(target.url, content)
        if hop == config.max_redirects or not location or len(location) > config.max_url_chars:
            raise BrowserError(BrowserFailure.REDIRECT)
        next_url = canonical_url(urljoin(target.url, location))
        if target.secure and urlsplit(next_url).scheme != "https":
            raise BrowserError(BrowserFailure.REDIRECT)
        current = next_url  # next iteration repeats resolve/check/PIN in full
    raise BrowserError(BrowserFailure.REDIRECT)


def _fetch_worker(pipe, url, method, config):
    try:
        if multiprocessing.current_process().name != "MainProcess":
            with open(os.devnull, "w") as sink:
                os.dup2(sink.fileno(), 1)
                os.dup2(sink.fileno(), 2)
        document = _fetch_document(url, method, config, stage=lambda stage: pipe.send(("stage", stage)))
        pipe.send(("ok", (document.final_url, document.html)))
    except BrowserError as error:
        pipe.send(("error", error.failure.value))
    except (TimeoutError, socket.timeout):
        pipe.send(("error", BrowserFailure.TIMEOUT.value))
    except Exception:
        pipe.send(("error", BrowserFailure.FETCH.value))
    finally:
        pipe.close()


class SafeFetchBroker:
    def __init__(self, config: BrowserConfig, *, _worker_target=_fetch_worker):
        config.validate()
        self._config = config
        self._worker_target = _worker_target
        self._owner = (os.getpid(), threading.get_ident())

    def fetch(self, url: str, method: str = "GET") -> FetchedDocument:
        if self._owner != (os.getpid(), threading.get_ident()):
            raise BrowserError(BrowserFailure.OWNERSHIP)
        url = canonical_url(url, max_chars=self._config.max_url_chars)
        if method not in ("GET", "HEAD"):
            raise BrowserError(BrowserFailure.DESTINATION)
        worker = parent = child = reader = None
        failure = None
        try:
            deadline = time.monotonic() + self._config.operation_timeout_seconds
            stage_deadline = deadline
            spawn = multiprocessing.get_context("spawn")
            parent, child = spawn.Pipe(duplex=False)
            worker = spawn.Process(target=self._worker_target, args=(child, url, method, self._config), daemon=True)
            worker.start()
            child.close()
            messages = queue.Queue()
            def receive():
                try:
                    while True:
                        messages.put(parent.recv())
                except (EOFError, OSError):
                    messages.put(("error", BrowserFailure.FETCH.value))
            # poll() only promises some bytes, not a complete IPC frame. Never
            # let a partial frame defeat the parent's DNS/operation deadline.
            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            while time.monotonic() < min(deadline, stage_deadline):
                try:
                    kind, data = messages.get(timeout=min(.05, max(0, min(deadline, stage_deadline) - time.monotonic())))
                except queue.Empty:
                    pass
                else:
                    if kind == "stage":
                        stage_deadline = (min(deadline, time.monotonic() + self._config.dns_timeout_seconds)
                                          if data == "dns" else deadline)
                    elif kind == "ok":
                        if len(data[1]) > self._config.max_body_bytes * 6 + 16:
                            raise BrowserError(BrowserFailure.SIZE)
                        return FetchedDocument(*data)
                    else:
                        raise BrowserError(BrowserFailure(data))
                if not worker.is_alive():
                    raise BrowserError(BrowserFailure.FETCH)
            raise BrowserError(BrowserFailure.TIMEOUT)
        except BrowserError:
            raise
        except Exception:
            raise BrowserError(BrowserFailure.FETCH) from None
        finally:
            if worker is not None and worker.pid is not None:
                try:
                    worker.join(timeout=.2)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=1)
                    if worker.is_alive():
                        worker.kill()
                        worker.join(timeout=1)
                    if worker.is_alive():
                        failure = BrowserFailure.CLEANUP
                    else:
                        worker.close()
                except Exception:
                    failure = BrowserFailure.CLEANUP
            for pipe in (parent, child):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        failure = BrowserFailure.CLEANUP
            if reader is not None:
                reader.join(timeout=.2)
                if reader.is_alive():
                    failure = BrowserFailure.CLEANUP
            if failure:
                raise BrowserError(failure) from None
