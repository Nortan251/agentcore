"""Adversarial bounded MIME extraction through the real executor/provider."""

import base64
import codecs
import json

import pytest

from agentcore.credentials import CredentialFailure
from gmail_fakes import MALICIOUS, MESSAGE_ID, READ, connected, message, multipart, setup, text_part


@pytest.fixture
def reader(tmp_path):
    value = connected(tmp_path / "protected")
    runtime = setup(value)
    yield runtime
    value.vault.close()


def read(runtime, payload):
    runtime.transport.message_result = message(payload)
    return runtime.executor.invoke(READ, {"message_id": MESSAGE_ID})


def test_plain_message_is_detached_allowlisted_and_bounded(reader):
    result = read(reader, text_part("Hello,\r\nThis is a synthetic message.\x00"))
    assert result.ok
    output = result.result.output
    assert set(output) == {"id", "thread_id", "label_ids", "internal_date", "snippet",
                           "headers", "body", "body_source", "omitted_body_parts",
                           "attachments", "attachments_omitted", "truncated", "truncation"}
    assert set(output["headers"]) == {"from", "to", "cc", "reply_to", "subject", "date", "message_id"}
    assert output["body"] == "Hello,\nThis is a synthetic message."
    assert output["body_source"] == "plain" and not output["truncated"]
    assert output["label_ids"] == ["INBOX", "UNREAD"]
    assert not output["attachments"] and output["omitted_body_parts"] == 0
    reader.transport.message_result["payload"]["body"]["data"] = "changed"
    assert output["body"].startswith("Hello")
    serialized = json.dumps(output)
    assert "unnecessary-provider-header" not in serialized and "unnecessary-history-identifier" not in serialized
    assert "withheld-provider-field" not in serialized


@pytest.mark.parametrize("reverse", [False, True])
def test_multipart_alternative_prefers_plain_and_mixed_keeps_bounded_text(reader, reverse):
    alternatives = [text_part("<p>HTML alternative must not win</p>", "text/html"), text_part("Plain alternative")]
    if reverse:
        alternatives.reverse()
    payload = multipart(
        multipart(*alternatives, mime="multipart/alternative"),
        text_part("Second independent text part"),
        text_part(b"binary-secret-bytes", "application/octet-stream", filename="synthetic.bin"),
    )
    result = read(reader, payload)
    assert result.ok
    assert result.result.output["body"] == "Plain alternative\n\nSecond independent text part"
    assert "HTML alternative" not in result.result.output["body"]
    assert result.result.output["attachments"] == [{"filename": "synthetic.bin", "mime_type": "application/octet-stream", "size": 19}]
    assert len(reader.transport.calls) == 1


def test_empty_plain_alternative_does_not_select_html_instead(reader):
    result = read(reader, multipart(text_part(""), text_part("<p>hidden alternative</p>", "text/html"),
                                    mime="multipart/alternative"))
    assert result.ok and result.result.output["body"] == ""
    assert result.result.output["body_source"] == "plain"


def test_html_is_inert_text_with_no_scripts_styles_attributes_or_resources(reader, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("email parser attempted external I/O")
    monkeypatch.setattr("webbrowser.open", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    html = (
        '<head><title>HEAD_SECRET</title><style>@import "https://resource.example.test/style"; STYLE_SECRET</style></head>'
        '<script src="https://resource.example.test/script">SCRIPT_SECRET</script>'
        '<p>Hello &amp; welcome</p><img src="https://resource.example.test/tracker">'
        '<iframe src="https://resource.example.test/frame">FRAME_SECRET</iframe>'
        '<svg><script>SVG_SECRET</script></svg>'
        '<a href="https://resource.example.test/link" onclick="EVENT_SECRET">visible link</a>'
        '<!-- COMMENT_SECRET --><p>' + MALICIOUS + "</p>"
    )
    result = read(reader, text_part(html, "text/html"))
    assert result.ok and result.result.output["body_source"] == "html_to_text"
    body = result.result.output["body"]
    assert "Hello & welcome" in body and "visible link" in body and "EMAIL-INJECTION-SENTINEL" in body
    for withheld in ("HEAD_SECRET", "STYLE_SECRET", "SCRIPT_SECRET", "FRAME_SECRET", "SVG_SECRET",
                     "EVENT_SECRET", "COMMENT_SECRET", "resource.example.test"):
        assert withheld not in body
    assert len(reader.transport.calls) == 1


@pytest.mark.parametrize("encoded", ["a", "a===", "AA== ", "++//", "AB", "AB==", "SGVsbG8===", "%7D", "é"],
                         ids=["mod4", "padding", "space", "not_urlsafe", "pad_bits", "padded_pad_bits", "extra_pad", "escape", "unicode"])
def test_malformed_base64url_is_rejected_categorically(reader, encoded):
    payload = text_part()
    payload["body"]["data"] = encoded
    result = read(reader, payload)
    assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_CONTENT.value
    assert result.result.output is None


@pytest.mark.parametrize("padded", [False, True])
def test_canonical_padded_and_unpadded_base64url_are_supported(reader, padded):
    payload = text_part("ok")
    if padded:
        payload["body"]["data"] = base64.urlsafe_b64encode(b"ok").decode()
    assert read(reader, payload).result.output["body"] == "ok"


@pytest.mark.parametrize("depth, accepted", [(8, True), (9, False)])
def test_mime_depth_is_enforced(reader, depth, accepted):
    payload = text_part("leaf")
    for _ in range(depth):
        payload = multipart(payload)
    result = read(reader, payload)
    assert result.ok is accepted
    if not accepted:
        assert result.result.metadata["kind"] == CredentialFailure.READ_CONTENT.value


@pytest.mark.parametrize("parts, accepted", [(99, True), (100, False)])
def test_mime_part_count_includes_containers(reader, parts, accepted):
    result = read(reader, multipart(*(text_part("x") for _ in range(parts))))
    assert result.ok is accepted
    if not accepted:
        assert result.result.metadata["kind"] == CredentialFailure.READ_CONTENT.value


@pytest.mark.parametrize("damage", ["header_count", "header_name", "header_value", "header_shape",
                                   "control_duplicate", "mime_type", "leaf_children", "part_size", "total_size", "charset"])
def test_unsafe_mime_shapes_and_input_budgets_fail_before_publication(reader, damage):
    payload = text_part("body")
    if damage == "header_count":
        payload["headers"] = [{"name": "X-Test", "value": "value"}] * 200
    elif damage == "header_name":
        payload["headers"] = [{"name": "X" * 129, "value": "value"}]
    elif damage == "header_value":
        payload["headers"] = [{"name": "Subject", "value": "x" * 4097}]
    elif damage == "header_shape":
        payload["headers"] = [42]
    elif damage == "control_duplicate":
        payload["headers"] = [{"name": "Content-Type", "value": "text/plain"}] * 2
    elif damage == "mime_type":
        payload["mimeType"] = "text/plain\r\ninjected"
    elif damage == "leaf_children":
        payload["parts"] = [text_part("child")]
    elif damage == "part_size":
        payload = text_part("x" * 65_537)
    elif damage == "total_size":
        payload = multipart(*(text_part("x" * 60_000) for _ in range(3)))
    else:
        payload["headers"] = [{"name": "Content-Type", "value": "text/plain; charset=zlib"}]
    result = read(reader, payload)
    assert result.result.metadata["kind"] == CredentialFailure.READ_CONTENT.value
    assert result.result.output is None and len(reader.transport.calls) == 1


@pytest.mark.parametrize("html", [False, True])
def test_large_valid_body_is_explicitly_truncated_not_an_unbounded_result(reader, html):
    text = "x" * 12_000
    result = read(reader, text_part("<p>" + text + "</p>" if html else text, "text/html" if html else "text/plain"))
    assert result.ok
    output = result.result.output
    assert len(output["body"]) == 8000 and output["truncated"]
    assert "body" in output["truncation"]
    assert len(json.dumps(output, ensure_ascii=True)) <= 48_000


def test_unicode_json_expansion_obeys_the_runtime_envelope(reader):
    result = read(reader, text_part("\U0001f600" * 8000))
    assert result.ok
    output = result.result.output
    assert output["truncated"] and "output_budget" in output["truncation"]
    assert len(output["body"]) < 8000 and len(json.dumps(output, ensure_ascii=True)) <= 48_000


def test_attachment_data_and_ids_never_become_message_text_or_downloads(reader):
    binary = text_part(b"binary-content-must-not-export", "application/octet-stream", filename="report.bin")
    binary["body"]["attachmentId"] = "opaque-attachment-id-must-not-export"
    binary["body"]["data"] = "ignored-not-decoded-attachment-data"
    attached_text = text_part("attached-text-must-not-export", filename="attached.txt")
    unnamed_attachment = text_part("unnamed-text-must-not-export", headers=[
        {"name": "Content-Disposition", "value": "attachment"}])
    body_on_attachment_api = {"mimeType": "text/plain", "body": {"size": 999, "attachmentId": "never-fetch"}}
    result = read(reader, multipart(text_part("visible body"), binary, attached_text, unnamed_attachment, body_on_attachment_api))
    assert result.ok
    output = result.result.output
    assert output["body"] == "visible body" and output["omitted_body_parts"] == 1
    assert len(output["attachments"]) == 4 and output["attachments"][0]["filename"] == "report.bin"
    public = json.dumps(output)
    for withheld in ("must-not-export", "never-fetch", "ignored-not-decoded-attachment-data"):
        assert withheld not in public
    assert len(reader.transport.calls) == 1 and reader.transport.calls[0][0] == "read"


def test_attached_message_cannot_smuggle_nested_body_into_parent(reader):
    attached = {"mimeType": "message/rfc822", "filename": "forward.eml", "body": {"size": 100},
                "parts": [text_part("nested-attachment-body")]}
    result = read(reader, multipart(text_part("parent body"), attached))
    assert result.ok and result.result.output["body"] == "parent body"
    assert "nested-attachment-body" not in json.dumps(result.result.output)


@pytest.mark.parametrize("complexity", ["deep", "many", "unsupported"])
def test_withheld_attachment_subtrees_do_not_block_visible_parent_text(reader, complexity):
    child = text_part("withheld-nested-content")
    if complexity == "deep":
        for _ in range(9):
            child = multipart(child)
        children = [child]
    elif complexity == "many":
        children = [child] * 101
    else:
        children = [{"mimeType": "unsupported child structure", "body": {"data": "invalid base64"}}]
    attached = {"mimeType": "message/rfc822", "filename": "forward.eml", "body": {"size": 999},
                "parts": children}
    result = read(reader, multipart(text_part("visible parent"), attached))
    assert result.ok and result.result.output["body"] == "visible parent"
    assert result.result.output["attachments"] == [{"filename": "forward.eml", "mime_type": "message/rfc822", "size": 999}]
    assert "withheld-nested-content" not in json.dumps(result.result.output)
    assert len(reader.transport.calls) == 1


@pytest.mark.parametrize("header", [
    {"name": "Content-Disposition", "value": 'inline; filename="synthetic.txt"'},
    {"name": "Content-Type", "value": 'text/plain; name="synthetic.txt"'},
    {"name": "Content-Disposition", "value": "inline; filename*=utf-8''synthetic.txt"},
])
def test_filename_parameters_also_prevent_attached_text_exposure(reader, header):
    result = read(reader, multipart(text_part("parent"), text_part("attached-secret", headers=[header])))
    assert result.ok and result.result.output["body"] == "parent"
    assert len(result.result.output["attachments"]) == 1
    assert "attached-secret" not in json.dumps(result.result.output)


def test_attachment_header_label_and_snippet_output_limits_are_explicit(reader):
    payload = multipart(text_part("body"), *(text_part(b"binary", "image/png", filename="f" * 200) for _ in range(25)))
    raw = message(payload)
    raw["payload"]["headers"].append({"name": "Subject", "value": "s" * 2000})
    raw["snippet"] = "n" * 1024
    raw["labelIds"] = ["label_" + str(i) for i in range(20)]
    reader.transport.message_result = raw
    result = reader.executor.invoke(READ, {"message_id": MESSAGE_ID})
    assert result.ok
    output = result.result.output
    assert len(output["headers"]["subject"]) == 512 and len(output["snippet"]) == 256
    assert len(output["label_ids"]) == 16 and len(output["attachments"]) == 20
    assert output["attachments_omitted"] == 5
    assert {"headers", "snippet", "labels", "attachments", "attachment_metadata"} <= set(output["truncation"])


def test_known_text_charset_and_rfc2047_header_are_decoded_without_resources(reader):
    payload = text_part(b"Caf\xe9", headers=[{"name": "Content-Type", "value": "text/plain; charset=windows-1252"}])
    raw = message(payload)
    raw["payload"]["headers"] = [h for h in raw["payload"]["headers"] if h["name"] != "Subject"]
    raw["payload"]["headers"].append({"name": "Subject", "value": "=?utf-8?b?Q2Fmw6k=?="})
    reader.transport.message_result = raw
    result = reader.executor.invoke(READ, {"message_id": MESSAGE_ID})
    assert result.ok
    assert result.result.output["body"] == "Café" and result.result.output["headers"]["subject"] == "Café"


def test_rfc2231_charset_cannot_invoke_a_provider_selected_codec(reader):
    calls = []
    def decode(value, errors="strict"):
        calls.append(bytes(value))
        return "utf-8", len(value)
    def lookup(name):
        if name == "x_synthetic_gmail_codec":
            return codecs.CodecInfo(name=name, encode=None, decode=decode)
    codecs.register(lookup)
    try:
        payload = text_part("synthetic body", headers=[{
            "name": "Content-Type", "value": "text/plain; charset*=x-synthetic-gmail-codec''utf-8",
        }])
        result = read(reader, payload)
        assert calls == []
        assert not result.ok and result.result.metadata["kind"] == CredentialFailure.READ_CONTENT.value
    finally:
        codecs.unregister(lookup)


@pytest.mark.parametrize("field, value", [("id", "another-message"), ("threadId", "../thread"), ("internalDate", "-1")])
def test_message_identity_and_metadata_are_validated(reader, field, value):
    reader.transport.message_result[field] = value
    result = reader.executor.invoke(READ, {"message_id": MESSAGE_ID})
    assert not result.ok and result.result.output is None
