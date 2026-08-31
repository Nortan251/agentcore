"""Real read HTTP parsing over memory sockets, with every stream read observed."""

import http.client
import json
import logging
import socket
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agentcore.config.schema import AgentConfig, ConversationConfig, LoggingConfig
from agentcore.conversation.session import JsonFileSessionStore
from agentcore.core.agent import Agent, AgentDependencies
from agentcore.credentials import CredentialError, CredentialFailure, CredentialRuntime
from agentcore.integrations.gmail import GmailReadonlyTools
from agentcore.integrations.gmail.http import GmailReadHttp
from agentcore.integrations.gmail.limits import MAX_JSON_BYTES
from agentcore.models.messages import ModelResponse, ToolCall
from agentcore.observability.logging import configure_logging
from agentcore.state.store import JsonFileStateStore
from gmail_fakes import MESSAGE_ID, PRIVATE_QUERY, READ, SEARCH, THREAD_ID, connected, message, setup
from gmail_mutation_http_fakes import HttpBoundary, Reply, _Stream, no_network
from google_oauth_fakes import ACCESS, REFRESH


CHUNKED = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
PRIVATE = "private provider bytes"


class ObservedStream(_Stream):
    def __init__(self, value):
        super().__init__(value)
        self.line_sizes = []
        self.line_bytes = 0
        self.body_bytes = 0

    def read(self, *args):
        data = super().read(*args)
        self.body_bytes += len(data)
        return data

    def readline(self, size=-1):
        self.line_sizes.append(size)
        line = super().readline(size)
        self.line_bytes += len(line)
        return line


class ReadBoundary(HttpBoundary):
    def reply(self, request):
        assert request.method == "GET"
        path = urlsplit(request.path).path
        if path == "/gmail/v1/users/me/messages":
            return self.search
        assert path == "/gmail/v1/users/me/messages/" + MESSAGE_ID
        return self.candidates[MESSAGE_ID]


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setattr("gmail_mutation_http_fakes._Stream", ObservedStream)
    boundary = ReadBoundary()
    boundary.install(monkeypatch)
    boundary.search = Reply({"messages": []})
    boundary.candidates[MESSAGE_ID] = Reply(message())
    return boundary


@pytest.fixture
def google(tmp_path):
    value = connected(tmp_path / "protected")
    try:
        yield value
    finally:
        value.vault.close()


@pytest.fixture
def wired(google, boundary):
    runtime = setup(google, transport=GmailReadHttp())
    runtime.wire = boundary
    return runtime


def assert_bounded(wired):
    assert len(wired.wire.requests) == 1
    request = wired.wire.requests[0]
    assert request.closed and request.stream.closed
    assert all(0 <= size <= MAX_JSON_BYTES + 1 for size in request.stream.read_sizes), request.stream.read_sizes
    assert request.stream.body_bytes <= MAX_JSON_BYTES + 1 + 2 * 66
    assert request.stream.line_sizes
    assert all(0 <= size <= 4097 for size in request.stream.line_sizes)
    assert request.stream.line_bytes <= 16_385  # includes at most one detection byte
    assert len(request.stream.line_sizes) <= 67  # includes at most one detection line
    return request


def test_negative_chunk_length_never_reaches_an_unbounded_stream_read(wired, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    wired.wire.search = Reply(wire=CHUNKED + b"-1\r\n" + (PRIVATE + ACCESS + REFRESH).encode() + b"\r\n0\r\n\r\n")
    result = wired.executor.invoke(SEARCH)
    request = assert_bounded(wired)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert not request.stream.read_sizes
    captured = capsys.readouterr()
    public = json.dumps(result.to_dict()) + caplog.text + repr([r.__dict__ for r in caplog.records])
    public += captured.out + captured.err
    for private in (PRIVATE, ACCESS, REFRESH):
        assert private not in public
    assert all(record.exc_info is None for record in caplog.records)


def chunked(body, *, trailer=b"", prefix=b""):
    return Reply(wire=prefix + CHUNKED + f"{len(body):X}\r\n".encode() + body + b"\r\n0\r\n" + trailer + b"\r\n")


@pytest.mark.parametrize("operation", [SEARCH, READ])
@pytest.mark.parametrize("framing", ["length", "chunked", "interim", "eof"])
def test_valid_responses_keep_exact_public_data_and_fixed_get_semantics(wired, operation, framing):
    payload = ({"messages": [{"id": MESSAGE_ID, "threadId": THREAD_ID}],
                "nextPageToken": "opaque-next+/=", "resultSizeEstimate": 3} if operation == SEARCH else message())
    raw = json.dumps(payload, separators=(",", ":")).encode()
    reply = Reply(raw=raw)
    if framing in ("chunked", "interim"):
        prefix = b"HTTP/1.1 100 Continue\r\nX-Safe: interim\r\n\r\n" if framing == "interim" else b""
        reply = chunked(raw, trailer=b"X-Safe: inert\r\n", prefix=prefix)
    elif framing == "eof":
        reply = Reply(raw=raw, headers=[("Content-Type", "application/json")])
    if operation == SEARCH:
        wired.wire.search = reply
        args = {"q": PRIVATE_QUERY, "max_results": 2, "page_token": "opaque+/=&&userId=other?next#fragment"}
        expected = {"messages": [{"id": MESSAGE_ID, "thread_id": THREAD_ID}],
                    "next_page_token": "opaque-next+/=", "result_size_estimate": 3}
    else:
        wired.wire.candidates[MESSAGE_ID] = reply
        args = {"message_id": MESSAGE_ID}
        expected = {
            "id": MESSAGE_ID, "thread_id": THREAD_ID, "label_ids": ["INBOX", "UNREAD"],
            "internal_date": "1788080400000", "snippet": "Synthetic preview.",
            "headers": {"from": "Synthetic Sender <sender@example.test>",
                        "to": "Synthetic Recipient <recipient@example.test>", "cc": "copy@example.test",
                        "reply_to": "reply@example.test", "subject": "Synthetic test message",
                        "date": "Sun, 30 Aug 2026 09:00:00 +0000", "message_id": "<synthetic-message@example.test>"},
            "body": "Synthetic plain body.", "body_source": "plain", "omitted_body_parts": 0,
            "attachments": [], "attachments_omitted": 0, "truncated": False, "truncation": [],
        }
    result = wired.executor.invoke(operation, args)
    assert result.ok and result.result.output == expected
    request = assert_bounded(wired)
    assert request.method == "GET" and request.body == b""
    assert request.headers == {"host": "gmail.googleapis.com", "authorization": "Bearer " + ACCESS,
                               "accept": "application/json", "accept-encoding": "identity"}
    assert request.bytes.count(ACCESS.encode()) == 1
    assert ACCESS not in request.path and REFRESH.encode() not in request.bytes
    assert not wired.wire.posts
    parsed = urlsplit(request.path)
    assert not parsed.scheme and not parsed.netloc and not parsed.fragment
    assert parsed.path == "/gmail/v1/users/me/messages" + ("/" + MESSAGE_ID if operation == READ else "")
    assert parse_qs(parsed.query) == ({
        "q": [PRIVATE_QUERY], "maxResults": ["2"], "pageToken": [args["page_token"]],
        "fields": ["messages(id,threadId),nextPageToken,resultSizeEstimate"],
    } if operation == SEARCH else {"format": ["full"], "fields": ["id,threadId,labelIds,internalDate,snippet,payload"]})


@pytest.mark.parametrize("framing", ["length", "chunked", "eof"])
def test_exact_json_byte_limit_still_succeeds(wired, framing):
    body = b"{}" + b" " * (MAX_JSON_BYTES - 2)
    wired.wire.search = chunked(body) if framing == "chunked" else Reply(
        raw=body, headers=None if framing == "length" else [("Content-Type", "application/json")])
    result = wired.executor.invoke(SEARCH)
    assert result.ok and result.result.output == {"messages": []}
    assert_bounded(wired)


@pytest.mark.parametrize("framing", ["length", "chunked", "multiple_chunks", "eof"])
def test_decoded_body_over_limit_is_rejected_without_unbounded_reads(wired, framing):
    body = b" " * MAX_JSON_BYTES + b"{}"
    if framing == "chunked":
        reply = chunked(body)
    elif framing == "multiple_chunks":
        reply = Reply(wire=CHUNKED + b"2\r\n{}\r\n" + f"{MAX_JSON_BYTES:x}\r\n".encode() + body)
    else:
        reply = Reply(raw=body, headers=None if framing == "length" else [("Content-Type", "application/json")])
    wired.wire.search = reply
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_TOO_LARGE.value
    assert_bounded(wired)


@pytest.mark.parametrize("headers, reason", [
    ([("Content-Length", "-1")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "+2")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "2.0")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "0x2")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "2, 2")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "9" * 21)], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "9" * 20)], CredentialFailure.READ_TOO_LARGE),
    ([("Content-Length", str(MAX_JSON_BYTES + 1))], CredentialFailure.READ_TOO_LARGE),
    ([("Content-Length", "2"), ("content-length", "2")], CredentialFailure.READ_RESPONSE),
    ([("Content-Type", "application/json")], CredentialFailure.READ_RESPONSE),
    ([("Content-Encoding", "identity"), ("Content-Encoding", "identity")], CredentialFailure.READ_RESPONSE),
    ([("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")], CredentialFailure.READ_RESPONSE),
    ([("Content-Length", "2"), ("Transfer-Encoding", "chunked")], CredentialFailure.READ_RESPONSE),
    ([("Transfer-Encoding", "gzip")], CredentialFailure.READ_RESPONSE),
    ([("Transfer-Encoding", "gzip, chunked")], CredentialFailure.READ_RESPONSE),
    ([("Transfer-Encoding", "chunked, chunked")], CredentialFailure.READ_RESPONSE),
    ([("Transfer-Encoding", "chunked ")], CredentialFailure.READ_RESPONSE),
    ([("Content-Encoding", "gzip")], CredentialFailure.READ_RESPONSE),
    ([("Content-Encoding", "br")], CredentialFailure.READ_RESPONSE),
], ids=["negative-length", "signed-length", "decimal-length", "hex-length", "empty-length", "joined-length",
        "length-digits", "huge-length", "oversize-length", "duplicate-length", "duplicate-type", "duplicate-encoding",
        "duplicate-transfer", "conflict", "unsupported-transfer", "multiple-transfers", "repeated-transfer",
        "transfer-whitespace", "gzip", "brotli"])
def test_framing_fields_are_strict_and_rejected_before_body_reads(wired, headers, reason):
    wired.wire.search = Reply(raw=(PRIVATE + ACCESS).encode(), headers=[("Content-Type", "application/json"), *headers])
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == reason.value
    assert not assert_bounded(wired).stream.read_sizes


def test_content_length_must_match_actual_returned_bytes(wired):
    wired.wire.search = Reply(raw=b"{}", headers=[("Content-Type", "application/json"), ("Content-Length", "3")])
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert assert_bounded(wired).stream.read_sizes == [3]


@pytest.mark.parametrize("framing", [
    b"-1\r\n", b"+2\r\n", b" 2\r\n", b"2 \r\n", b"0x2\r\n", b"1_0\r\n", b"z\r\n", b"\r\n",
    b"2;extension=value\r\n", b"1" * 9 + b"\r\n", b"0" * 4096 + b"2\r\n",
    b"2\n", b"2\r\n{}XX0\r\n\r\n", b"2\r\n{}\r", b"2\r\n{}\r\n", b"ff\r\nshort",
], ids=["negative", "signed", "leading-space", "trailing-space", "hex-prefix", "separator", "nonhex", "empty",
        "extension", "excess-digits", "overlong-line", "bare-lf", "wrong-delimiter", "short-delimiter",
        "missing-last-chunk", "truncated-body"])
def test_malformed_chunks_fail_safely(wired, framing):
    wired.wire.search = Reply(wire=CHUNKED + framing)
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.output is None
    assert result.result.metadata["kind"] in {CredentialFailure.READ_RESPONSE.value, CredentialFailure.PROVIDER.value}
    assert_bounded(wired)


def test_huge_unsigned_chunk_is_rejected_before_any_body_read(wired):
    wired.wire.search = Reply(wire=CHUNKED + b"ffffffff\r\n" + PRIVATE.encode())
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_TOO_LARGE.value
    assert not assert_bounded(wired).stream.read_sizes


@pytest.mark.parametrize("prefix, headers", [
    (b"", b"X-Long: " + b"x" * 4096 + b"\r\n"),
    (b"", (b"X-Safe: " + b"x" * 3500 + b"\r\n") * 5),
    (b"", b"X-Safe: x\r\n" * 67),
    (b"HTTP/1.1 100 Continue\r\nX-Safe: x\r\n\r\n" * 23, b""),
    ((b"HTTP/1.1 100 Continue\r\nX-Safe: " + b"x" * 3500 + b"\r\n\r\n") * 5, b""),
    (b"HTTP/1.1 200 " + b"x" * 4096 + b"\r\n", b""),
    (b"", b"Missing-Colon\r\n"), (b"", b"Bad Name: value\r\n"),
    (b"", b"X-Bad : value\r\n"), (b"", b" folded-value\r\n"),
    (b"", b"X-Bad: value\n"), (b"", b"X-Bad: value\x00\r\n"),
    (b"HTTP/1.1 100 Continue\r\nMissing-Colon\r\n\r\n", b""),
], ids=["line", "total", "count", "interim-count", "interim-total", "status-line", "missing-colon",
        "bad-name", "space-before-colon", "folding", "bare-lf", "control", "malformed-interim"])
def test_status_and_headers_share_strict_framing_limits(wired, prefix, headers):
    wired.wire.search = Reply(wire=prefix + b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + headers + b"\r\n{}")
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert not assert_bounded(wired).stream.read_sizes


@pytest.mark.parametrize("trailer", [
    b"", b"\n", b"X-Safe: value\r\n", b"Missing-Colon\r\n\r\n", b"X-Bad: value\n\r\n",
    b" folded-value\r\n\r\n", b"X-Bad: value\x00\r\n\r\n", b"Content-Length: 2\r\n\r\n",
    b"Transfer-Encoding: chunked\r\n\r\n", b"Content-Type: application/json\r\n\r\n",
    b"Content-Encoding: identity\r\n\r\n", b"X-Long: " + b"x" * 4096 + b"\r\n\r\n",
    b"X-Safe: value\r\n" * 67 + b"\r\n", (b"X-Safe: " + b"x" * 3500 + b"\r\n") * 5 + b"\r\n",
], ids=["missing", "bare-lf", "missing-terminator", "missing-colon", "bad-line-ending", "folding", "control",
        "length", "transfer", "type", "encoding", "long-line", "count", "total"])
def test_malformed_or_excessive_trailers_are_rejected(wired, trailer):
    wired.wire.search = Reply(wire=CHUNKED + b"2\r\n{}\r\n0\r\n" + trailer)
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert_bounded(wired)


def test_headers_and_trailers_cannot_each_spend_a_separate_byte_budget(wired):
    field = b"X-Safe: " + b"x" * 1000 + b"\r\n"
    header = CHUNKED[:-2] + field * 12 + b"\r\n"
    wired.wire.search = Reply(wire=header + b"2\r\n{}\r\n0\r\n" + field * 5 + b"\r\n")
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert_bounded(wired)


def test_many_tiny_chunks_cannot_exhaust_unbounded_framing_work(wired):
    wired.wire.search = Reply(wire=CHUNKED + b"1\r\n \r\n" * 100 + b"2\r\n{}\r\n0\r\n\r\n")
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert_bounded(wired)


@pytest.mark.parametrize("status, reason", [
    (301, CredentialFailure.PROVIDER), (302, CredentialFailure.PROVIDER), (303, CredentialFailure.PROVIDER),
    (307, CredentialFailure.PROVIDER), (308, CredentialFailure.PROVIDER),
    (401, CredentialFailure.READ_AUTHENTICATION), (403, CredentialFailure.READ_PERMISSION),
    (404, CredentialFailure.READ_NOT_FOUND), (429, CredentialFailure.READ_RATE_LIMIT),
    (500, CredentialFailure.PROVIDER), (503, CredentialFailure.PROVIDER),
])
def test_redirect_and_provider_error_bodies_remain_unread(wired, status, reason):
    wired.wire.search = Reply(status=status, raw=(PRIVATE + ACCESS + REFRESH).encode(), headers=[
        ("Location", "https://attacker.invalid/" + ACCESS), ("Retry-After", "0"), ("Transfer-Encoding", "chunked"),
    ])
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == reason.value
    assert not assert_bounded(wired).stream.read_sizes
    assert PRIVATE not in json.dumps(result.to_dict())


@pytest.mark.parametrize("case", ["negative_chunk", "truncated_chunk", "malformed_header", "malformed_json"])
def test_private_framing_failure_is_withheld_from_exceptions_model_session_state_and_logs(
        google, boundary, tmp_path, monkeypatch, caplog, capsys, case):
    private = (PRIVATE + ACCESS + REFRESH).encode()
    reply = {
        "negative_chunk": Reply(wire=CHUNKED + b"-1\r\n" + private + b"\r\n0\r\n\r\n"),
        "truncated_chunk": Reply(wire=CHUNKED + b"fff\r\n" + private),
        "malformed_header": Reply(wire=b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Bad: " + private + b"\x00\r\n\r\n{}"),
        "malformed_json": Reply(raw=b'{"private":"' + private),
    }[case]
    boundary.candidates[MESSAGE_ID] = reply
    errors, original = [], GmailReadHttp._get

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except CredentialError as error:
            errors.append(error)
            raise

    monkeypatch.setattr(GmailReadHttp, "_get", observe)
    monkeypatch.setattr(http.client.HTTPConnection, "debuglevel", 99)
    model = SimpleNamespace(requests=[], responses=[
        ModelResponse(text="", tool_calls=[ToolCall(id="synthetic-read", name=READ, arguments={"message_id": MESSAGE_ID})]),
        ModelResponse(text="The selected message could not be read."),
    ])

    def generate(request):
        model.requests.append(request)
        return model.responses.pop(0)

    model.generate = generate
    log_path = tmp_path / "framing.log"
    sessions, states = JsonFileSessionStore(tmp_path / "sessions"), JsonFileStateStore(tmp_path / "states")
    runtime = CredentialRuntime(google.vault)
    gmail = GmailReadonlyTools(transport=GmailReadHttp(), allow_message_content=True)
    gmail.bind(runtime, google.info.reference)
    before = google.vault._file.read_current()
    try:
        agent = Agent(AgentConfig(workspace_dir=str(tmp_path), conversation=ConversationConfig(tools_enabled=True),
                                  logging=LoggingConfig(level="DEBUG", file_path=str(log_path))),
                      AgentDependencies(tools=list(gmail.tools), model=model, credential_runtime=runtime,
                                        allow_authenticated_reads=True, state_store=states))
        result = agent.conversation(sessions=sessions).send("Read the selected synthetic message.")
        agent.checkpoint()
        assert result.ok and len(model.requests) == 2
        assert len(errors) == 1
        error = errors[0]
        assert error.__context__ is None and error.__cause__ is None and not getattr(error, "__notes__", [])
        assert_bounded(SimpleNamespace(wire=boundary))
        assert google.vault._file.read_current() == before
        assert not google.transport.refreshes
        for handler in logging.getLogger("agentcore").handlers:
            handler.flush()
        public = json.dumps([agent.state.to_dict(), [request.to_dict() for request in model.requests]], default=str)
        public += "".join(path.read_text(encoding="utf-8") for directory in (tmp_path / "sessions", tmp_path / "states")
                          for path in directory.glob("*.json"))
        captured = capsys.readouterr()
        public += log_path.read_text(encoding="utf-8") + caplog.text + captured.out + captured.err
        public += repr(error) + str(error) + repr(vars(error)) + repr([record.__dict__ for record in caplog.records])
        for value in (PRIVATE, ACCESS, REFRESH):
            assert value not in public
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        configure_logging(LoggingConfig(level="CRITICAL"))


def test_framing_tests_cannot_use_real_dns_or_sockets():
    with pytest.raises(AssertionError, match="cannot use DNS or real sockets"):
        socket.getaddrinfo("gmail.googleapis.com", 443)
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="cannot use DNS or real sockets"):
            connection.connect(("192.0.2.1", 443))
