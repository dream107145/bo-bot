"""The hand-rolled websocket framing.

Hand-rolled protocol code is exactly where silent corruption lives, so the
frame codec is pinned against the RFC's own vectors and against the three
length encodings, the masking rule, and the cases this server refuses.
"""
from __future__ import annotations

import struct

import pytest

from troll_poly_bot.web import ws


def _reader(data: bytes):
    """A file-like read that also returns SHORT reads, as a real socket does."""
    buf = bytearray(data)

    def read(n: int) -> bytes:
        take = min(n, 3, len(buf))          # never more than 3 bytes at a time
        out = bytes(buf[:take])
        del buf[:take]
        return out
    return read


# ───────────────────────────────── handshake ─────────────────────────────────


def test_accept_key_matches_the_rfc_vector():
    """RFC 6455 section 1.3."""
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_accept_key_tolerates_surrounding_whitespace():
    assert ws.accept_key("  dGhlIHNhbXBsZSBub25jZQ==\r\n") == \
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_handshake_response_is_a_101_with_the_accept_header():
    out = ws.handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
    assert out.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert b"Upgrade: websocket\r\n" in out
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in out
    assert out.endswith(b"\r\n\r\n")


# ────────────────────────────────── framing ──────────────────────────────────


@pytest.mark.parametrize("size", [0, 1, 125, 126, 127, 65535, 65536])
def test_round_trip_at_every_length_boundary(size):
    """7-bit, 16-bit and 64-bit length encodings, and the bytes either side."""
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    frame = ws.encode_frame(payload, ws.OP_TEXT, mask=True)
    opcode, got = ws.read_frame(_reader(frame))
    assert opcode == ws.OP_TEXT
    assert got == payload


def test_large_server_frame_uses_the_64_bit_length():
    """State pushes routinely exceed 64 KiB; only CLIENT frames are capped."""
    frame = ws.encode_frame(b"y" * 200_000)
    assert frame[1] & 0x7F == 127
    assert struct.unpack("!Q", frame[2:10])[0] == 200_000
    assert frame[10:] == b"y" * 200_000


def test_client_frames_are_capped_but_server_frames_are_not():
    assert ws.MAX_CLIENT_FRAME == 64 * 1024


def test_server_frames_are_not_masked():
    """RFC 6455 5.1: a server must not mask."""
    frame = ws.encode_frame(b"hello")
    assert frame[1] & 0x80 == 0
    assert frame[2:] == b"hello"


def test_short_reads_are_reassembled():
    """A socket may hand back 3 bytes of a 300 byte frame; that is not an error."""
    payload = b"x" * 300
    opcode, got = ws.read_frame(_reader(ws.encode_frame(payload, mask=True)))
    assert got == payload


def test_unmasked_client_frame_is_refused():
    """RFC 6455 5.1: the server MUST close on one."""
    with pytest.raises(ws.WSError, match="not masked"):
        ws.read_frame(_reader(ws.encode_frame(b"hi", mask=False)))


def test_fragmented_frames_are_refused_not_mishandled():
    fin_cleared = bytearray(ws.encode_frame(b"hi", mask=True))
    fin_cleared[0] &= 0x7F
    with pytest.raises(ws.WSError, match="fragmented"):
        ws.read_frame(_reader(bytes(fin_cleared)))


def test_oversized_client_frame_is_refused_before_reading_it():
    """The length is declared before the body; do not allocate on a lie."""
    header = struct.pack("!BBQ", 0x80 | ws.OP_TEXT, 127 | 0x80, 1 << 40)
    with pytest.raises(ws.WSError, match="too large"):
        ws.read_frame(_reader(header + b"\x00" * 4))


def test_truncated_frame_raises_rather_than_returning_short():
    frame = ws.encode_frame(b"hello world", mask=True)[:-4]
    with pytest.raises(ws.WSError, match="closed mid-frame"):
        ws.read_frame(_reader(frame))


def test_control_opcodes_survive_the_round_trip():
    for op in (ws.OP_CLOSE, ws.OP_PING, ws.OP_PONG):
        opcode, payload = ws.read_frame(_reader(ws.encode_frame(b"", op, mask=True)))
        assert opcode == op and payload == b""


def test_close_frame_carries_the_status_code():
    opcode, payload = ws.read_frame(
        _reader(ws.encode_frame(ws.close_frame()[2:], ws.OP_CLOSE, mask=True)))
    assert opcode == ws.OP_CLOSE
    assert struct.unpack("!H", payload[:2])[0] == 1000


def test_utf8_payloads_are_byte_exact():
    payload = '{"chart":"btc-updown-5m-1","note":"é ∆ 🙂"}'.encode()
    _, got = ws.read_frame(_reader(ws.encode_frame(payload, mask=True)))
    assert got.decode() == payload.decode()
