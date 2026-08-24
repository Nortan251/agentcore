"""Strict, bounded Server-Sent Events parsing for model streams.

Only the OpenAI-compatible subset is accepted: blank-line framed events,
comments, and one or more ``data`` fields.  The parser consumes bounded byte
chunks and enforces the raw-frame ceiling as each byte arrives; it never uses
``read()`` without a size, ``readline()``, or a raw-frame history.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from agentcore.models.errors import ModelResponseError
from agentcore.models.streaming import MAX_RAW_SSE_FRAME_BYTES


_SSE_READ_CHUNK_BYTES = 4096


class _BoundedByteStream(Protocol):
    def read(self, size: int) -> bytes: ...


def iter_sse_data(
    stream: _BoundedByteStream,
    *,
    max_frame_bytes: int = MAX_RAW_SSE_FRAME_BYTES,
    read_size: int = _SSE_READ_CHUNK_BYTES,
) -> Iterator[str]:
    """Yield decoded ``data`` payloads from a bounded SSE byte stream.

    Line endings accepted by the SSE standard (LF, CRLF, and CR) are handled
    incrementally, including when split across reads.  Multiple ``data`` lines
    are joined with ``"\n"``.  Unknown fields, invalid UTF-8, partial frames,
    oversized frames, and invalid reader output fail categorically without
    copying raw content into an exception.
    """
    if isinstance(max_frame_bytes, bool) or \
            not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
        raise ModelResponseError("SSE frame bound is invalid")
    if isinstance(read_size, bool) or \
            not isinstance(read_size, int) or read_size <= 0:
        raise ModelResponseError("SSE read size is invalid")

    line = bytearray()
    data_lines: list[str] = []
    frame_bytes = 0
    pending_cr = False

    def count_byte() -> None:
        nonlocal frame_bytes
        frame_bytes += 1
        if frame_bytes > max_frame_bytes:
            raise ModelResponseError("provider SSE frame exceeded the byte limit")

    def finish_line() -> str | None:
        """Accept one complete line; return a completed event if any."""
        nonlocal frame_bytes
        raw_line = bytes(line)
        line.clear()
        try:
            decoded = raw_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ModelResponseError(
                "provider SSE stream contained invalid UTF-8") from None

        if decoded == "":
            event = "\n".join(data_lines) if data_lines else None
            data_lines.clear()
            frame_bytes = 0
            return event

        if decoded.startswith(":"):
            return None

        field, separator, value = decoded.partition(":")
        if field != "data":
            raise ModelResponseError(
                "provider SSE stream contained an unsupported field")
        if not separator:
            value = ""
        elif value.startswith(" "):
            # SSE removes at most one optional leading U+0020.
            value = value[1:]
        data_lines.append(value)
        return None

    while True:
        chunk = stream.read(read_size)
        if not isinstance(chunk, bytes) or len(chunk) > read_size:
            raise ModelResponseError(
                "provider SSE reader returned an invalid byte chunk")
        if not chunk:
            break

        for byte in chunk:
            if pending_cr:
                if byte == 0x0A:  # complete CRLF
                    count_byte()
                    pending_cr = False
                    event = finish_line()
                    if event is not None:
                        yield event
                    continue
                pending_cr = False
                event = finish_line()  # previous CR was a complete line end
                if event is not None:
                    yield event

            if byte == 0x0D:  # CR; wait one byte to distinguish CRLF
                count_byte()
                pending_cr = True
            elif byte == 0x0A:  # bare LF
                count_byte()
                event = finish_line()
                if event is not None:
                    yield event
            else:
                count_byte()
                line.append(byte)

    if pending_cr:
        pending_cr = False
        event = finish_line()
        if event is not None:
            yield event

    # A frame is valid only after its blank-line delimiter.  EOF on a data or
    # comment line is malformed even before the provider-level [DONE] check.
    if line or data_lines or frame_bytes:
        raise ModelResponseError(
            "provider SSE stream ended inside an event frame")


__all__ = ["iter_sse_data"]
