"""Tests for Chrome Native Messaging protocol framing, partial reads, and size limits."""

import io
import struct
import unittest

from tongji_clipper.native_host.protocol import (
    MAX_INCOMING_MESSAGE_SIZE,
    MAX_OUTGOING_MESSAGE_SIZE,
    read_exact_bytes,
    read_message,
    send_message,
)


class ChunkedReader(io.RawIOBase):
    """Simulates a stream that delivers data in tiny fragments (e.g. 2 bytes at a time)."""

    def __init__(self, data: bytes, chunk_size: int = 2):
        self._data = data
        self._pos = 0
        self._chunk_size = chunk_size

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        limit = min(n if n > 0 else self._chunk_size, self._chunk_size)
        chunk = self._data[self._pos : self._pos + limit]
        self._pos += len(chunk)
        return chunk


class TestNativeMessagingProtocol(unittest.TestCase):

    def test_send_and_read_roundtrip(self):
        buf = io.BytesIO()
        test_payload = {"action": "submit", "id": 12345, "title": "高阶数学导论", "items": [1, 2, 3]}

        send_message(test_payload, stream=buf)
        raw_bytes = buf.getvalue()

        # Check 4-byte header
        (length,) = struct.unpack("<I", raw_bytes[:4])
        self.assertEqual(len(raw_bytes) - 4, length)

        # Read back
        read_buf = io.BytesIO(raw_bytes)
        received = read_message(stream=read_buf)
        self.assertEqual(received, test_payload)

    def test_partial_read_fragmented_stream(self):
        """Verifies that reading from a fragmented pipe correctly reconstructs the message."""
        buf = io.BytesIO()
        payload = {"type": "fragment_test", "data": "A" * 500}
        send_message(payload, stream=buf)
        full_bytes = buf.getvalue()

        # Feed through chunked reader with 3 bytes per read
        chunked = ChunkedReader(full_bytes, chunk_size=3)
        received = read_message(stream=chunked)
        self.assertEqual(received, payload)

    def test_eof_returns_none(self):
        empty_stream = io.BytesIO(b"")
        self.assertIsNone(read_message(stream=empty_stream))

    def test_unexpected_eof_raises_error(self):
        # 4-byte header claims 100 bytes, but only 10 bytes follow
        header = struct.pack("<I", 100)
        incomplete = io.BytesIO(header + b"1234567890")
        with self.assertRaises(EOFError):
            read_message(stream=incomplete)

    def test_incoming_message_size_limit_exceeded(self):
        # Header claims larger than MAX_INCOMING_MESSAGE_SIZE (4 MiB)
        oversized = MAX_INCOMING_MESSAGE_SIZE + 1024
        header = struct.pack("<I", oversized)
        stream = io.BytesIO(header + b"dummy")
        with self.assertRaises(ValueError) as ctx:
            read_message(stream=stream)
        self.assertIn("exceeds maximum allowed limit", str(ctx.exception))

    def test_outgoing_message_size_limit_exceeded(self):
        # Outgoing message exceeds MAX_OUTGOING_MESSAGE_SIZE (1 MiB)
        oversized_data = {"huge": "X" * (MAX_OUTGOING_MESSAGE_SIZE + 100)}
        buf = io.BytesIO()
        with self.assertRaises(ValueError) as ctx:
            send_message(oversized_data, stream=buf)
        self.assertIn("exceeds Chromium Native Messaging limit", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
