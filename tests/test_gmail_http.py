"""Real pinned Gmail transport under deterministic HTTPS socket doubles."""

import json
import logging
import os
import ssl
from urllib.parse import parse_qs, urlsplit

import pytest

from agentcore.credentials import CredentialFailure
from agentcore.integrations.gmail.http import GmailReadHttp, decode_json
from agentcore.integrations.gmail.limits import MAX_JSON_BYTES
from gmail_fakes import MESSAGE_ID, PRIVATE_QUERY, READ, SEARCH, Wire, connected, message, setup, text_part
from google_oauth_fakes import ACCESS, REFRESH


@pytest.fixture
def wired(tmp_path, monkeypatch):
    value = connected(tmp_path / "protected")
    wire = Wire().install(monkeypatch)
    runtime = setup(value, transport=GmailReadHttp())
    runtime.wire = wire
    yield runtime
    value.vault.close()


def test_list_is_one_fixed_me_get_with_encoded_private_query_and_no_fanout(wired, caplog):
    caplog.set_level(logging.DEBUG, logger="agentcore")
    token = "opaque+/=&&userId=other?next#fragment"
    wired.wire.body = {"messages": [{"id": MESSAGE_ID, "threadId": "thread-one", "snippet": "must-not-export"}],
                       "nextPageToken": token, "resultSizeEstimate": 120, "private": "must-not-export"}
    result = wired.executor.invoke(SEARCH, {"q": PRIVATE_QUERY, "max_results": 2, "page_token": token})
    assert result.ok
    assert result.result.output == {
        "messages": [{"id": MESSAGE_ID, "thread_id": "thread-one"}],
        "next_page_token": token, "result_size_estimate": 120}
    assert len(wired.wire.requests) == 1
    method, path, body, headers = wired.wire.requests[0]
    parsed = urlsplit(path)
    assert method == "GET" and body is None and parsed.path == "/gmail/v1/users/me/messages"
    assert not parsed.scheme and not parsed.netloc and not parsed.fragment
    fields = parse_qs(parsed.query)
    assert fields == {"q": [PRIVATE_QUERY], "maxResults": ["2"], "pageToken": [token],
                      "fields": ["messages(id,threadId),nextPageToken,resultSizeEstimate"]}
    assert headers["Authorization"] == "Bearer " + ACCESS
    assert PRIVATE_QUERY not in path and ACCESS not in path
    assert PRIVATE_QUERY not in json.dumps(result.to_dict()) + caplog.text
    assert PRIVATE_QUERY not in repr([record.__dict__ for record in caplog.records])
    assert ACCESS not in json.dumps(result.to_dict()) + caplog.text


def test_read_is_exactly_one_full_message_get_no_attachments_or_thread_calls(wired):
    result = wired.executor.invoke(READ, {"message_id": MESSAGE_ID})
    assert result.ok and result.result.output["body"] == "Synthetic plain body."
    assert len(wired.wire.requests) == 1
    method, path, body, headers = wired.wire.requests[0]
    assert method == "GET" and body is None
    assert urlsplit(path).path == "/gmail/v1/users/me/messages/" + MESSAGE_ID
    assert parse_qs(urlsplit(path).query) == {
        "format": ["full"], "fields": ["id,threadId,labelIds,internalDate,snippet,payload"]}
    assert list(headers.values()).count("Bearer " + ACCESS) == 1
    assert ACCESS not in path
    assert wired.wire.reads == [MAX_JSON_BYTES + 1] and wired.wire.closed == 1


@pytest.mark.parametrize("status, reason", [
    (401, CredentialFailure.READ_AUTHENTICATION), (403, CredentialFailure.READ_PERMISSION),
    (404, CredentialFailure.READ_NOT_FOUND), (429, CredentialFailure.READ_RATE_LIMIT),
    (500, CredentialFailure.PROVIDER), (503, CredentialFailure.PROVIDER),
    (400, CredentialFailure.PROVIDER), (301, CredentialFailure.PROVIDER),
    (302, CredentialFailure.PROVIDER), (303, CredentialFailure.PROVIDER),
    (307, CredentialFailure.PROVIDER), (308, CredentialFailure.PROVIDER),
])
def test_errors_are_categorical_no_body_read_retry_redirect_or_reconsent(wired, caplog, status, reason):
    caplog.set_level(logging.DEBUG, logger="agentcore")
    wired.wire.status = status
    wired.wire.body = {"error": ACCESS + REFRESH + PRIVATE_QUERY}
    wired.wire.headers["Location"] = "https://attacker.example.test/" + ACCESS
    before = wired.google.vault._file.read_current()
    result = wired.executor.invoke(READ, {"message_id": MESSAGE_ID})
    assert not result.ok and result.result.metadata["kind"] == reason.value
    assert len(wired.wire.requests) == 1 and not wired.wire.reads
    assert wired.google.vault._file.read_current() == before
    assert len(wired.google.transport.exchanges) == 1 and not wired.google.transport.refreshes
    public = json.dumps(result.to_dict()) + caplog.text + repr([r.__dict__ for r in caplog.records])
    for private in (ACCESS, REFRESH, PRIVATE_QUERY, "attacker.example.test"):
        assert private not in public
    assert all(record.exc_info is None for record in caplog.records)


def test_tls_verification_keylog_proxy_and_debug_rules(wired, monkeypatch, tmp_path, capsys):
    keylog = tmp_path / "must-not-exist.keys"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    for variable in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(variable, "http://attacker.example.test:1234")
    def forbidden(*args, **kwargs):
        pytest.fail("ambient proxy discovery or TLS key logging hook used")
    monkeypatch.setattr("urllib.request.getproxies", forbidden)
    monkeypatch.setattr(ssl, "create_default_context", forbidden)
    result = wired.executor.invoke(SEARCH)
    assert result.ok and len(wired.wire.requests) == 1
    context = wired.wire.contexts[0]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert context.cert_store_stats()["x509_ca"] > 0
    assert context.keylog_filename is None and not keylog.exists()
    assert os.environ["SSLKEYLOGFILE"] == str(keylog)
    captured = capsys.readouterr()
    assert ACCESS not in captured.out + captured.err


@pytest.mark.parametrize("content_type", [
    "", "text/html", "application/octet-stream", "application/problem+json",
    "application/json; charset=iso-8859-1", "application/json, application/json",
    "application/json; unreviewed=parameter", "application/json; charset=utf-8; charset=utf-8",
])
def test_only_strict_json_content_type_is_accepted(wired, content_type):
    wired.wire.headers["Content-Type"] = content_type
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value
    assert not wired.wire.reads


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "identity,gzip"])
def test_compressed_responses_are_not_decompressed(wired, encoding):
    wired.wire.headers["Content-Encoding"] = encoding
    assert not wired.executor.invoke(SEARCH).ok
    assert not wired.wire.reads


@pytest.mark.parametrize("length, reason", [
    (str(MAX_JSON_BYTES + 1), CredentialFailure.READ_TOO_LARGE),
    ("-1", CredentialFailure.READ_RESPONSE), ("1, 2", CredentialFailure.READ_RESPONSE),
    ("9" * 21, CredentialFailure.READ_RESPONSE),
])
def test_invalid_or_oversized_content_length_fails_before_body_read(wired, length, reason):
    wired.wire.headers["Content-Length"] = length
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == reason.value
    assert not wired.wire.reads


def test_unadvertised_oversized_body_stops_after_limit_plus_one(wired):
    wired.wire.body = b" " * (MAX_JSON_BYTES + 500)
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_TOO_LARGE.value
    assert wired.wire.reads == [MAX_JSON_BYTES + 1]


@pytest.mark.parametrize("raw", [
    b'{"messages":[],"messages":[]}', b"[]", b"null", b'{"x":NaN}', b'{"x":1e9999}',
    b'{"x":922337203685477580700000}', b'{"x":"\\ud800"}', b"\xff",
    b'{"x":' + b"[" * 33 + b"]" * 33 + b"}",
    b'{"x":[' + b"0," * 16_384 + b"0]}",
], ids=["duplicate", "array", "null", "nonfinite", "float", "integer", "surrogate", "utf8", "depth", "nodes"])
def test_json_is_bounded_and_strict_without_provider_error_text(wired, raw):
    wired.wire.body = raw
    result = wired.executor.invoke(SEARCH)
    assert result.result.metadata["kind"] == CredentialFailure.READ_RESPONSE.value


def test_json_nesting_scan_understands_quoted_braces_and_escapes():
    value = {"q": 'literal {"nested":"\\\\"} ' * 100}
    assert decode_json(json.dumps(value).encode()) == value


def test_transport_exception_text_notes_and_chains_are_discarded(wired, caplog):
    error = OSError(ACCESS + PRIVATE_QUERY)
    error.add_note(REFRESH)
    wired.wire.error = error
    result = wired.executor.invoke(SEARCH)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.PROVIDER.value
    assert len(wired.wire.requests) == 1
    assert ACCESS not in json.dumps(result.to_dict()) + caplog.text
    assert PRIVATE_QUERY not in caplog.text and REFRESH not in caplog.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_transport_has_no_mutation_or_arbitrary_request_surface(wired, method):
    transport = wired.transport
    with pytest.raises(TypeError):
        transport.list_messages(None, {}, method=method)
    with pytest.raises(TypeError):
        transport.read_message(None, MESSAGE_ID, method=method)
    for option in ("host", "url", "path", "userId", "headers", "format"):
        with pytest.raises(TypeError):
            transport._get(None, **{option: "attacker-selected"})
    assert not hasattr(transport, "request") and not hasattr(transport, "post")
    assert not wired.wire.requests


def test_token_cannot_be_reflected_into_query_or_provider_result(wired):
    result = wired.executor.invoke(SEARCH, {"q": ACCESS})
    assert not result.ok and not wired.wire.requests
    wired.wire.body = message(text_part(ACCESS))
    result = wired.executor.invoke(READ, {"message_id": MESSAGE_ID})
    assert result.result.metadata["kind"] == CredentialFailure.UNSAFE_RESULT.value
    assert ACCESS not in json.dumps(result.to_dict())
