"""A minimal RFC 6455 server, sized for one job: pushing state to the console.

Hand-rolled rather than pulled in as a dependency because the dashboard runs on
``http.server`` and must stay on ONE port -- the box it is deployed to has a
single hole in the firewall, and a second listener would need a second one. The
``websockets`` package in this project is a *client*, used for the venue feeds;
its server is asyncio, and this server is a thread per connection.

Scope, deliberately small:
  * text and binary frames, close, ping, pong
  * the three payload-length encodings
  * client->server unmasking (browsers always mask)
  * continuation frames are REJECTED, not reassembled: the only thing the page
    ever sends is a one-line JSON subscription, and silently mishandling a
    fragment is worse than refusing it

Nothing here is a general-purpose websocket library. It is the subset the
dashboard needs, with the parts it does not need closed off rather than faked.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct

#: RFC 6455 section 1.3. Constant, not a secret.
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: A client frame larger than this is a bug or an attack; the page sends ~50 B.
MAX_CLIENT_FRAME = 64 * 1024


class WSError(Exception):
    """Protocol violation or a closed connection. Always fatal to the socket."""


def accept_key(client_key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1(client_key.strip().encode() + GUID).digest()
    return base64.b64encode(digest).decode()


def handshake_response(client_key: str) -> bytes:
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept_key(client_key).encode() + b"\r\n"
        b"\r\n"
    )


def encode_frame(payload: bytes, opcode: int = OP_TEXT, mask: bool = False) -> bytes:
    """Build a single final frame. Servers send unmasked; `mask` is for tests."""
    if len(payload) < 126:
        header = struct.pack("!BB", 0x80 | opcode, len(payload) | (0x80 if mask else 0))
    elif len(payload) < (1 << 16):
        header = struct.pack("!BBH", 0x80 | opcode,
                             126 | (0x80 if mask else 0), len(payload))
    else:
        header = struct.pack("!BBQ", 0x80 | opcode,
                             127 | (0x80 if mask else 0), len(payload))
    if not mask:
        return header + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return header + key + masked


def _read_exactly(readfn, n: int) -> bytes:
    """`readfn` may return short reads; a websocket frame cannot tolerate that."""
    buf = b""
    while len(buf) < n:
        chunk = readfn(n - len(buf))
        if not chunk:
            raise WSError("connection closed mid-frame")
        buf += chunk
    return buf


def read_frame(readfn) -> tuple[int, bytes]:
    """Read one client frame. Returns (opcode, payload). Raises WSError."""
    b0, b1 = _read_exactly(readfn, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if opcode == OP_CONT or not fin:
        # see the module docstring: refused rather than half-supported
        raise WSError("fragmented frames are not supported")
    if length == 126:
        (length,) = struct.unpack("!H", _read_exactly(readfn, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exactly(readfn, 8))
    if length > MAX_CLIENT_FRAME:
        raise WSError(f"client frame too large: {length}")
    if not masked:
        # RFC 6455 5.1: a server MUST close on an unmasked client frame
        raise WSError("client frame was not masked")

    key = _read_exactly(readfn, 4)
    payload = _read_exactly(readfn, length) if length else b""
    return opcode, bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def close_frame(code: int = 1000, reason: str = "") -> bytes:
    return encode_frame(struct.pack("!H", code) + reason.encode(), OP_CLOSE)
