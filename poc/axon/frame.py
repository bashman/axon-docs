"""Frame v0: Intent -> compact binary frame -> nodes."""

import hashlib

MAGIC = b"AX"
VERSION = 1

# IR v0 opcodes (each one would be a single token in the model vocabulary)
OP_WRITE = 0x01  # write(fd, bytes) -> sys_write
OP_EXIT = 0x02  # exit(code) -> sys_exit


def uleb(n: int) -> bytes:
    """Encode an unsigned integer as LEB128."""
    if n < 0:
        raise ValueError("uleb requires a non-negative integer")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_uleb(buf: bytes, i: int) -> tuple[int, int]:
    """Decode a LEB128 varint at offset i; return (value, next offset)."""
    n = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i
        if shift > 63:
            raise ValueError("varint too long")


def encode(intents) -> bytes:
    """Emit a frame.

    'len' is derived from the buffer: a redundant field is a hallucination vector.
    """
    out = bytearray(MAGIC + bytes([VERSION]))
    for it in intents:
        if it["action"] == "SYS_WRITE":
            data = bytes(it["buffer"])
            if "len" in it and it["len"] != len(data):
                raise ValueError(f"len={it['len']} does not match buffer ({len(data)})")
            out += bytes([OP_WRITE]) + uleb(it["target"]) + uleb(len(data)) + data
        elif it["action"] == "SYS_EXIT":
            out += bytes([OP_EXIT]) + uleb(it.get("code", 0))
        else:
            raise ValueError(f"unknown action: {it['action']}")
    return bytes(out)


def decode(frame: bytes) -> list[tuple]:
    """Decode a frame into nodes: ('write', fd, bytes) | ('exit', code)."""
    if frame[:2] != MAGIC:
        raise ValueError("invalid magic")
    if len(frame) < 3 or frame[2] != VERSION:
        raise ValueError("unsupported version")
    i, nodes = 3, []
    while i < len(frame):
        op = frame[i]
        i += 1
        if op == OP_WRITE:
            fd, i = read_uleb(frame, i)
            n, i = read_uleb(frame, i)
            if i + n > len(frame):
                raise ValueError("buffer outside the frame")
            nodes.append(("write", fd, frame[i : i + n]))
            i += n
        elif op == OP_EXIT:
            code, i = read_uleb(frame, i)
            nodes.append(("exit", code))
        else:
            raise ValueError(f"invalid opcode 0x{op:02x} at offset {i - 1}")
    return nodes


def frame_hash(frame: bytes) -> bytes:
    """SHA-256 of a frame; used as the AOT seal (32 bytes)."""
    return hashlib.sha256(frame).digest()
