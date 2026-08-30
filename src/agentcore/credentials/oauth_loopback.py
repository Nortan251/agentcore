"""Bounded, one-shot desktop OAuth callback on an OS-assigned loopback port.

No web framework, query logging, callback echo, redirect forwarding, or manual
copy/paste flow. This listener is used only by trusted host authentication.
"""

from __future__ import annotations

import hmac
import math
import os
import re
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlsplit

from agentcore.credentials.errors import CredentialError, CredentialFailure
from agentcore.credentials.models import RuntimeOnly, SecretMaterial
from agentcore.credentials.runtime import _metadata_call
from agentcore.util.clock import Clock, SystemClock


class LoopbackCallback(RuntimeOnly):
    """Only 127.0.0.1; the caller cannot supply an address or redirect URI."""

    def __init__(self, state: SecretMaterial, *, timeout: float = 180,
                 clock: Clock | None = None) -> None:
        self._owner_pid = os.getpid()
        if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
            raise CredentialError(CredentialFailure.INVALID)
        self._clock = clock or SystemClock()
        self._deadline = self._clock.monotonic() + timeout
        self._owner()
        if not math.isfinite(self._deadline):
            raise CredentialError(CredentialFailure.INVALID)
        self._hard_deadline = time.monotonic() + timeout
        self._state = SecretMaterial(state._read())
        self._lock = threading.Lock()
        self._used = False
        self._waiting = False
        self._closed = False
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(4)
            self._authority = "127.0.0.1:" + str(self._socket.getsockname()[1])
            self.redirect_uri = "http://" + self._authority + "/"
        except BaseException:
            self._socket.close()
            raise

    def _owner(self) -> None:
        if self._owner_pid != os.getpid():
            raise CredentialError(CredentialFailure.OWNERSHIP)

    @contextmanager
    def _locked(self):
        self._owner()
        self._lock.acquire()
        try:
            yield
        finally:
            self._owner()
            self._lock.release()

    def _remaining(self) -> float:
        self._owner()
        remaining = min(self._deadline - self._clock.monotonic(), self._hard_deadline - time.monotonic())
        self._owner()
        if not math.isfinite(remaining) or remaining <= 0:
            raise CredentialError(CredentialFailure.OAUTH_TIMEOUT)
        return remaining

    def _accept(self, target: str, host: str) -> SecretMaterial:
        """Validate the exact binding before consuming any authorization code."""
        self._owner()
        with self._locked():
            self._owner()
            if self._used or self._closed:
                raise CredentialError(CredentialFailure.OAUTH_REPLAY)
            self._remaining()
            if type(target) is not str or type(host) is not str or host != self._authority or len(target) > 8192:
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            if not target.isascii() or any(ord(ch) <= 32 or ord(ch) == 127 for ch in target) or re.search(r"%(?![0-9a-fA-F]{2})", target):
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.path != "/" or parsed.fragment or "#" in target:
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            fields = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                               max_num_fields=12, encoding="utf-8", errors="strict")
            values = dict(fields)
            allowed = {"state", "code", "scope", "authuser", "prompt", "iss", "error", "error_description", "error_uri"}
            if len(values) != len(fields) or not set(values) <= allowed:
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            state = values.get("state", "")
            if not state.isascii() or not hmac.compare_digest(state, self._state._read()):
                raise CredentialError(CredentialFailure.OAUTH_STATE)
            if "iss" in values and values["iss"] != "https://accounts.google.com":
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            if ("code" in values) == ("error" in values):
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            if "error" in values:
                if not values["error"]:
                    raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
                self._used = True
                raise CredentialError(CredentialFailure.OAUTH_DENIED)
            code = values["code"]
            if "error_description" in values or "error_uri" in values or not 1 <= len(code) <= 4096 or (
                not code.isascii() or any(ord(ch) <= 32 or ord(ch) >= 127 for ch in code)
            ):
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            self._used = True
            return SecretMaterial(code)

    def _request(self, connection: socket.socket) -> tuple[str, str]:
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            connection.settimeout(min(1.0, self._remaining()))
            chunk = connection.recv(min(2048, 16_385 - len(raw)))
            if not chunk:
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            raw.extend(chunk)
            if len(raw) > 16_384:
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
        header, remainder = bytes(raw).split(b"\r\n\r\n", 1)
        if remainder:
            raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
        lines = header.decode("ascii").split("\r\n")
        first = lines[0].split(" ")
        if len(first) != 3 or first[0] != "GET" or first[2] not in ("HTTP/1.0", "HTTP/1.1") or len(lines) > 65:
            raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or not re.fullmatch(r"[A-Za-z0-9-]+", name) or name.lower() in headers:
                raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
            headers[name.lower()] = value.strip()
        if "content-length" in headers or "transfer-encoding" in headers:
            raise CredentialError(CredentialFailure.OAUTH_CALLBACK)
        return first[1], headers.get("host", "")

    @staticmethod
    def _reply(connection: socket.socket, accepted: bool) -> None:
        # Callback acceptance is not a successful token exchange or connection.
        body = (b"Authorization response received. Return to AgentCore for the connection result."
                if accepted else b"Authorization response rejected.")
        status = b"200 OK" if accepted else b"400 Bad Request"
        response = (b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    b"Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
                    b"Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
                    b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        try:
            connection.sendall(response)
        except OSError:
            pass  # Browser delivery is not token-exchange truth.

    def receive(self) -> SecretMaterial:
        self._owner()
        with self._locked():
            self._owner()
            if self._waiting or self._used or self._closed:
                raise CredentialError(CredentialFailure.OAUTH_REPLAY)
            self._waiting = True
        return _metadata_call(self._receive)

    def _receive(self) -> SecretMaterial:
        try:
            timed_out = False
            for _ in range(32):
                self._socket.settimeout(self._remaining())
                try:
                    connection, peer = self._socket.accept()
                except TimeoutError:
                    timed_out = True
                    break
                self._owner()
                with connection:
                    if peer[0] != "127.0.0.1":
                        continue
                    denial = False
                    code = None
                    try:
                        target, host = self._request(connection)
                        code = self._accept(target, host)
                    except CredentialError as exc:
                        denial = exc.reason is CredentialFailure.OAUTH_DENIED
                    except (ValueError, UnicodeError, OSError):
                        pass
                    self._reply(connection, code is not None)
                    if denial:
                        raise CredentialError(CredentialFailure.OAUTH_DENIED)
                    if code is not None:
                        return code
            raise CredentialError(CredentialFailure.OAUTH_TIMEOUT if timed_out else CredentialFailure.OAUTH_CALLBACK)
        finally:
            self.close()

    def close(self) -> None:
        self._owner()
        with self._locked():
            self._owner()
            if not self._closed:
                self._closed = True
                self._socket.close()
                self._state._discard()

    def __enter__(self) -> LoopbackCallback:
        self._owner()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
