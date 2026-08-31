"""Memory sockets below real HTTPSConnection/HTTPResponse; all network forbidden."""

import base64
import copy
import http.client
import io
import json
import socket
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import SMTP
from urllib.parse import urlsplit

import pytest

from agentcore.integrations.gmail.mutation_http import GmailMutationHttp
from gmail_send_fakes import ARGS, SENDER, Harness


LIVE_ARGS = {**ARGS, "bcc": []}
SEND_PATH = "/gmail/v1/users/me/messages/send"
PROFILE_PATH = "/gmail/v1/users/me/profile?fields=emailAddress"


def forbid_network(*args, **kwargs):
    raise AssertionError("Automated Gmail tests cannot use DNS or real sockets")


def install_network_guard(setattr_fn):
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "create_connection"):
        setattr_fn(socket, name, forbid_network)
    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        setattr_fn(socket.socket, name, forbid_network)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    install_network_guard(monkeypatch.setattr)


@dataclass
class Reply:
    payload: object = None
    status: int = 200
    headers: object = None
    raw: bytes | None = None
    wire: bytes | None = None
    error: BaseException | None = None

    def bytes(self):
        if self.wire is not None:
            return self.wire
        body = self.raw if self.raw is not None else json.dumps(self.payload, separators=(",", ":")).encode("utf-8")
        headers = self.headers if self.headers is not None else [
            ("Content-Type", "application/json; charset=UTF-8"), ("Content-Length", str(len(body))),
        ]
        return (f"HTTP/1.1 {self.status} Synthetic\r\n".encode() +
                b"".join(f"{key}: {value}\r\n".encode("ascii") for key, value in headers) + b"\r\n" + body)


class WireRequest:
    def __init__(self, connection):
        self.connection, self.chunks, self.closed, self.stream = connection, [], False, None

    @property
    def bytes(self):
        return b"".join(self.chunks)

    @property
    def method(self):
        return self.bytes.split(b" ", 1)[0].decode("ascii")

    @property
    def path(self):
        return self.bytes.split(b" ", 2)[1].decode("ascii")

    @property
    def headers(self):
        lines = self.bytes.split(b"\r\n\r\n", 1)[0].split(b"\r\n")[1:]
        return {name.decode("ascii").lower(): value.strip().decode("ascii")
                for name, value in (line.split(b":", 1) for line in lines)}

    @property
    def body(self):
        return self.bytes.partition(b"\r\n\r\n")[2]


class _Stream(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.body_reads = 0
        self.read_sizes = []

    def read(self, *args):
        self.body_reads += 1
        self.read_sizes.append(args[0] if args else -1)
        return super().read(*args)


class _Socket:
    def __init__(self, boundary, request):
        self.boundary, self.request = boundary, request

    def sendall(self, data):
        self.request.chunks.append(bytes(data))
        if self.boundary.on_write is not None:
            self.boundary.on_write(self.request)

    def makefile(self, mode):
        assert mode == "rb"
        if self.boundary.on_response is not None:
            self.boundary.on_response(self.request)
        reply = self.boundary.reply(self.request)
        if reply.error is not None:
            raise reply.error
        self.request.stream = _Stream(reply.bytes())
        return self.request.stream

    def close(self):
        self.request.closed = True


def full_message(mime, *, message_id="syntheticmessage1", thread_id="syntheticthread1"):
    mail = BytesParser(policy=SMTP).parsebytes(mime)
    body = mail.get_payload(decode=True)
    return {"id": message_id, "threadId": thread_id, "labelIds": ["SENT"], "payload": {
        "mimeType": "text/plain", "filename": "",
        "headers": [{"name": name, "value": value} for name, value in mail.raw_items()],
        "body": {"size": len(body), "data": base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")},
    }}


class HttpBoundary:
    def __init__(self):
        self.requests = []
        self.on_write = self.on_response = None
        self.profile = Reply({"emailAddress": SENDER})
        self.send = Reply({"id": "syntheticmessage1", "threadId": "syntheticthread1", "labelIds": ["SENT"]})
        self.search = Reply({"messages": [{"id": "syntheticmessage1", "threadId": "syntheticthread1"}],
                             "resultSizeEstimate": 1})
        self.candidates = {}
        self.template_mime = None

    def install(self, monkeypatch=None):
        setter = monkeypatch.setattr if monkeypatch is not None else setattr
        install_network_guard(setter)
        boundary = self

        def connect(connection):
            assert connection.host == "gmail.googleapis.com" and connection.port == 443
            assert connection.timeout == 10 and connection.debuglevel == 0
            request = WireRequest(connection)
            boundary.requests.append(request)
            connection.sock = _Socket(boundary, request)

        setter(http.client.HTTPSConnection, "connect", connect)

    @property
    def posts(self):
        return [request for request in self.requests if request.method == "POST"]

    @property
    def mime(self):
        if self.template_mime is not None:
            return self.template_mime
        encoded = json.loads(self.posts[0].body)["raw"]
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))

    def reply(self, request):
        if request.path == PROFILE_PATH and request.method == "GET":
            return self.profile
        if request.path == SEND_PATH and request.method == "POST":
            return self.send
        path = urlsplit(request.path).path
        if path == "/gmail/v1/users/me/messages" and request.method == "GET":
            return self.search
        if path.startswith("/gmail/v1/users/me/messages/") and request.method == "GET":
            message_id = path.rsplit("/", 1)[-1]
            return self.candidates.get(message_id, Reply(full_message(self.mime, message_id=message_id)))
        raise AssertionError("Unexpected Gmail endpoint or method")


class LiveHarness(Harness):
    def __init__(self, directory, **kwargs):
        super().__init__(directory, live_transport=GmailMutationHttp(), **kwargs)

    def prepare(self, args=None):
        return super().prepare(copy.deepcopy(LIVE_ARGS if args is None else args))

    def invoke(self, args=None, **kwargs):
        return super().invoke(copy.deepcopy(LIVE_ARGS if args is None else args), **kwargs)


@pytest.fixture
def boundary(monkeypatch):
    value = HttpBoundary()
    value.install(monkeypatch)
    return value


@pytest.fixture
def live(tmp_path, boundary):
    value = LiveHarness(tmp_path)
    yield value
    value.close()
