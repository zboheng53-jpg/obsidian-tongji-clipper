"""Chrome/Edge Native Messaging protocol framing with partial read and size limit protection."""

from __future__ import annotations

import io
import json
import struct
import sys
from typing import Any, BinaryIO, Dict, Optional

# Native Messaging limits: Chrome host output strictly limited to 1 MiB (1,048,576 bytes)
MAX_INCOMING_MESSAGE_SIZE = 4 * 1024 * 1024  # 4 MiB
MAX_OUTGOING_MESSAGE_SIZE = 1 * 1024 * 1024  # 1 MiB strictly per Chromium spec


def read_exact_bytes(stream: BinaryIO, num_bytes: int) -> Optional[bytes]:
    """
    Reads exactly num_bytes from stream, handling partial reads across pipe chunks.
    Returns None if EOF is encountered before any bytes are read.
    Raises EOFError if EOF is encountered after partial bytes.
    """
    buffer = bytearray()
    while len(buffer) < num_bytes:
        chunk = stream.read(num_bytes - len(buffer))
        if not chunk:
            if not buffer:
                return None
            raise EOFError(f"Unexpected EOF while reading message: expected {num_bytes} bytes, got {len(buffer)}")
        buffer.extend(chunk)
    return bytes(buffer)


def read_message(stream: Optional[BinaryIO] = None) -> Optional[Dict[str, Any]]:
    """
    Reads a 32-bit length-prefixed JSON message from binary input stream (defaults to sys.stdin.buffer).
    Returns None on clean EOF.
    """
    if stream is None:
        stream = sys.stdin.buffer

    # Read 4-byte little-endian length prefix
    length_bytes = read_exact_bytes(stream, 4)
    if length_bytes is None:
        return None

    (msg_length,) = struct.unpack("<I", length_bytes)

    if msg_length > MAX_INCOMING_MESSAGE_SIZE:
        raise ValueError(
            f"Incoming message size {msg_length} bytes exceeds maximum allowed limit {MAX_INCOMING_MESSAGE_SIZE} bytes"
        )

    raw_data = read_exact_bytes(stream, msg_length)
    if raw_data is None:
        raise EOFError(f"Expected {msg_length} bytes of payload but stream closed prematurely")

    return json.loads(raw_data.decode("utf-8"))


def send_message(data: Dict[str, Any], stream: Optional[BinaryIO] = None) -> None:
    """
    Sends a 32-bit length-prefixed JSON message to binary output stream (defaults to sys.stdout.buffer).
    Guarantees stdout receives ONLY valid protocol bytes and does not exceed 1 MiB.
    """
    if stream is None:
        stream = sys.stdout.buffer

    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    msg_length = len(encoded)

    if msg_length > MAX_OUTGOING_MESSAGE_SIZE:
        raise ValueError(
            f"Outgoing message size {msg_length} bytes exceeds Chromium Native Messaging limit {MAX_OUTGOING_MESSAGE_SIZE} bytes (1 MiB)"
        )

    length_prefix = struct.pack("<I", msg_length)
    stream.write(length_prefix + encoded)
    stream.flush()
