"""Canonical binary encoding for persisted AXON values (ADR-0008)."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable

from .ir import (
    ATOM_RE,
    CTOR_LIST,
    CTOR_OPTION,
    CTOR_RESULT,
    ENCODING_TAG,
    I64_MAX,
    I64_MIN,
    MAX_STRING_BYTES,
    NOMINAL,
    TEXT_PREFIX,
    TYPE_CODE,
    GraphError,
)

GetNode = Callable[[str], dict | None]
MAX_LIST_LENGTH = 65_536
_CODE_TYPE = {code: name for name, code in TYPE_CODE.items()}


def _error(code: str, detail: str) -> GraphError:
    return GraphError(None, code, detail)


def _uleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _sleb(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        done = (value == 0 and not byte & 0x40) or (value == -1 and byte & 0x40)
        out.append(byte if done else byte | 0x80)
        if done:
            return bytes(out)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def byte(self) -> int:
        if self.offset >= len(self.data):
            raise ValueError("truncated value")
        value = self.data[self.offset]
        self.offset += 1
        return value

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise ValueError("truncated value")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def uleb(self) -> int:
        start = self.offset
        value = 0
        shift = 0
        while True:
            byte = self.byte()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
            if shift > 63:
                raise ValueError("uleb128 is too large")
        if self.data[start:self.offset] != _uleb(value):
            raise ValueError("non-canonical uleb128")
        return value

    def sleb(self) -> int:
        start = self.offset
        value = 0
        shift = 0
        while True:
            byte = self.byte()
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                if byte & 0x40:
                    value |= -(1 << shift)
                break
            if shift > 70:
                raise ValueError("sleb128 is too large")
        if not I64_MIN <= value <= I64_MAX or self.data[start:self.offset] != _sleb(value):
            raise ValueError("invalid or non-canonical i64 sleb128")
        return value


def decode_type(type_bytes: bytes) -> str | dict:
    """Decode exactly one canonical ``ir.encode_type`` value."""
    if not isinstance(type_bytes, bytes):
        raise _error("E_KV_TYPE", "type encoding must be bytes")
    reader = _Reader(type_bytes)
    try:
        type_ = _decode_type(reader, 0)
        if reader.offset != len(type_bytes):
            raise ValueError("trailing bytes in type encoding")
        return type_
    except (UnicodeError, ValueError) as exc:
        raise _error("E_KV_TYPE", f"invalid type encoding: {exc}") from None


def _decode_type(reader: _Reader, depth: int):
    if depth > 8:
        raise ValueError("type nesting exceeds 8")
    code = reader.byte()
    if code in _CODE_TYPE:
        return _CODE_TYPE[code]
    if code == NOMINAL:
        if reader.byte() != ENCODING_TAG:
            raise ValueError("unsupported record reference encoding")
        return {"rec": TEXT_PREFIX + reader.take(32).hex()}
    if code == CTOR_OPTION:
        return {"option": _decode_type(reader, depth + 1)}
    if code == CTOR_LIST:
        return {"list": _decode_type(reader, depth + 1)}
    if code == CTOR_RESULT:
        payload = _decode_type(reader, depth + 1)
        if reader.byte() != TYPE_CODE["error"]:
            raise ValueError("result error type must be error")
        return {"result": payload}
    raise ValueError(f"unknown type code {code}")


def encode_value(get: GetNode, type_, value_json) -> bytes:
    """Encode a boundary JSON value according to ``type_``."""
    try:
        return _encode(get, type_, value_json, "value")
    except GraphError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise _error("E_TYPE_MISMATCH", str(exc)) from None


def is_encodable(get: GetNode, type_, value_json) -> str | None:
    """Return the first short reason ``value_json`` does not fit ``type_``.

    This is the non-raising boundary probe used by spec validation.  It mirrors
    ``encode_value`` but keeps record field names in diagnostics so stale specs
    can point at the value that needs updating.
    """
    try:
        return _encodability_reason(get, type_, value_json, None)
    except Exception as exc:  # A validator must never turn a bad value into a crash.
        detail = getattr(exc, "info", {}).get("detail")
        return detail or str(exc) or "value is not encodable"


def _shown(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _expected(label: str | None, expected: str, value) -> str:
    subject = "value" if label is None else label
    return f"{subject} expects {expected}, got {_shown(value)}"


def _type_name(type_) -> str:
    if isinstance(type_, str):
        return type_
    if not isinstance(type_, dict) or len(type_) != 1:
        return "a valid type"
    constructor, payload = next(iter(type_.items()))
    if constructor == "rec":
        return "record"
    if constructor == "result":
        return f"result<{_type_name(payload)},error>"
    return f"{constructor}<{_type_name(payload)}>"


def _encodability_reason(get: GetNode, type_, value, label: str | None) -> str | None:
    expected = _type_name(type_)
    if type_ == "i64":
        if isinstance(value, int) and not isinstance(value, bool) and I64_MIN <= value <= I64_MAX:
            return None
        return _expected(label, expected, value)
    if type_ == "bool":
        return None if isinstance(value, bool) else _expected(label, expected, value)
    if type_ == "unit":
        return None if value is None else _expected(label, expected, value)
    if type_ == "string":
        if (
            isinstance(value, str)
            and len(unicodedata.normalize("NFC", value).encode()) <= MAX_STRING_BYTES
        ):
            return None
        return _expected(label, expected, value)
    if type_ == "atom":
        if (
            isinstance(value, dict)
            and set(value) == {"atom"}
            and isinstance(value["atom"], str)
            and ATOM_RE.fullmatch(value["atom"]) is not None
        ):
            return None
        return _expected(label, expected, value)
    if type_ == "error":
        if not isinstance(value, dict) or set(value) != {"code", "detail"}:
            return _expected(label, expected, value)
        reason = _encodability_reason(get, "atom", value["code"], "field 'code'")
        return reason or _encodability_reason(
            get, {"option": "string"}, value["detail"], "field 'detail'"
        )
    if not isinstance(type_, dict) or len(type_) != 1:
        return _expected(label, expected, value)

    constructor, payload_type = next(iter(type_.items()))
    if constructor == "rec":
        fields = _record_fields(get, type_, "value")
        names = _record_names(get, type_["rec"])
        if names is not None:
            if not isinstance(value, dict):
                return _expected(label, expected, value)
            for name in names:
                if name not in value:
                    return f"missing field {name!r}"
            extra = next((name for name in value if name not in names), None)
            if extra is not None:
                return f"unexpected field {extra!r}"
            if len(names) != len(fields):
                return "record declaration has inconsistent field names"
            for name, field_type in zip(names, fields, strict=True):
                reason = _encodability_reason(
                    get, field_type, value[name], f"field {name!r}"
                )
                if reason is not None:
                    return reason
            return None
        if not isinstance(value, list) or len(value) != len(fields):
            return _expected(label, expected, value)
        for index, (field_type, field) in enumerate(zip(fields, value, strict=True)):
            reason = _encodability_reason(get, field_type, field, f"field {index}")
            if reason is not None:
                return reason
        return None
    if constructor == "list":
        if not isinstance(value, list) or len(value) > MAX_LIST_LENGTH:
            return _expected(label, expected, value)
        for index, item in enumerate(value):
            reason = _encodability_reason(get, payload_type, item, f"item {index}")
            if reason is not None:
                return reason
        return None
    if constructor == "option":
        if value == {"tag": "none"}:
            return None
        if isinstance(value, dict) and set(value) == {"tag", "v"} and value["tag"] == "some":
            return _encodability_reason(get, payload_type, value["v"], "option value")
        return _expected(label, expected, value)
    if constructor == "result":
        if isinstance(value, dict) and set(value) == {"tag", "v"}:
            if value["tag"] == "ok":
                return _encodability_reason(get, payload_type, value["v"], "result value")
            if value["tag"] == "err":
                return _encodability_reason(get, "error", value["v"], "result error")
        return _expected(label, expected, value)
    return _expected(label, expected, value)


def _mismatch(role: str, type_) -> GraphError:
    return _error("E_TYPE_MISMATCH", f"{role} must have type {type_}")


def _string(value, role: str, *, atom: bool = False) -> bytes:
    if not isinstance(value, str):
        raise _mismatch(role, "atom" if atom else "string")
    if atom and ATOM_RE.fullmatch(value) is None:
        raise _mismatch(role, "atom")
    normalized = unicodedata.normalize("NFC", value)
    raw = normalized.encode("utf-8")
    if len(raw) > MAX_STRING_BYTES:
        raise _mismatch(role, "atom" if atom else "string")
    return _uleb(len(raw)) + raw


def _record_fields(get: GetNode, type_, role: str) -> list:
    ref = type_["rec"]
    try:
        declaration = get(ref)
    except (KeyError, LookupError):
        declaration = None
    if not isinstance(declaration, dict) or declaration.get("k") != "rec":
        raise _mismatch(role, type_)
    return declaration["fields"]


def _record_names(get: GetNode, reference: str) -> list[str] | None:
    owner = getattr(get, "__self__", None)
    lookup = getattr(owner, "field_names", None)
    if lookup is None:
        return None
    names = lookup(reference)
    return names if names else None


def _encode(get: GetNode, type_, value, role: str) -> bytes:
    if type_ == "i64":
        if not isinstance(value, int) or isinstance(value, bool) or not I64_MIN <= value <= I64_MAX:
            raise _mismatch(role, type_)
        return _sleb(value)
    if type_ == "bool":
        if not isinstance(value, bool):
            raise _mismatch(role, type_)
        return bytes([value])
    if type_ == "unit":
        if value is not None:
            raise _mismatch(role, type_)
        return b""
    if type_ == "string":
        return _string(value, role)
    if type_ == "atom":
        if not isinstance(value, dict) or set(value) != {"atom"}:
            raise _mismatch(role, type_)
        return _string(value["atom"], role, atom=True)
    if type_ == "error":
        if not isinstance(value, dict) or set(value) != {"code", "detail"}:
            raise _mismatch(role, type_)
        return _encode(get, "atom", value["code"], f"{role}.code") + _encode(
            get, {"option": "string"}, value["detail"], f"{role}.detail"
        )
    if not isinstance(type_, dict) or len(type_) != 1:
        raise _mismatch(role, type_)
    constructor, payload_type = next(iter(type_.items()))
    if constructor == "rec":
        fields = _record_fields(get, type_, role)
        names = _record_names(get, type_["rec"])
        if names is not None:
            if len(names) != len(fields) or not isinstance(value, dict) or set(value) != set(names):
                raise _mismatch(role, type_)
            value = [value[name] for name in names]
        elif not isinstance(value, list) or len(value) != len(fields):
            raise _mismatch(role, type_)
        return b"".join(
            _encode(get, field_type, field, f"{role}[{index}]")
            for index, (field_type, field) in enumerate(zip(fields, value, strict=True))
        )
    if constructor == "list":
        if not isinstance(value, list) or len(value) > MAX_LIST_LENGTH:
            raise _mismatch(role, type_)
        return _uleb(len(value)) + b"".join(
            _encode(get, payload_type, item, f"{role}[{index}]")
            for index, item in enumerate(value)
        )
    if not isinstance(value, dict) or not isinstance(value.get("tag"), str):
        raise _mismatch(role, type_)
    if constructor == "option":
        if value == {"tag": "none"}:
            return b"\x00"
        if set(value) == {"tag", "v"} and value["tag"] == "some":
            return b"\x01" + _encode(get, payload_type, value["v"], f"{role}.v")
    if constructor == "result":
        if set(value) == {"tag", "v"} and value["tag"] in {"ok", "err"}:
            subtype = payload_type if value["tag"] == "ok" else "error"
            tag = b"\x00" if value["tag"] == "ok" else b"\x01"
            return tag + _encode(get, subtype, value["v"], f"{role}.v")
    raise _mismatch(role, type_)


def decode_value(get: GetNode, type_, data: bytes):
    """Decode a complete canonical persisted value, rejecting trailing bytes."""
    if not isinstance(data, bytes):
        raise _error("E_KV_TYPE", "encoded value must be bytes")
    reader = _Reader(data)
    try:
        value = _decode(get, type_, reader)
        if reader.offset != len(data):
            raise ValueError("trailing bytes in encoded value")
        return value
    except GraphError as exc:
        if exc.info["code"] == "E_KV_TYPE":
            raise
        raise _error("E_KV_TYPE", exc.info["detail"]) from None
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise _error("E_KV_TYPE", f"invalid encoded value: {exc}") from None


def _decode_string(reader: _Reader, *, atom: bool = False) -> str:
    size = reader.uleb()
    if size > MAX_STRING_BYTES:
        raise ValueError("string exceeds 1 MiB")
    value = reader.take(size).decode("utf-8")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("string is not NFC")
    if atom and ATOM_RE.fullmatch(value) is None:
        raise ValueError("invalid atom")
    return value


def _decode(get: GetNode, type_, reader: _Reader):
    if type_ == "i64":
        return reader.sleb()
    if type_ == "bool":
        value = reader.byte()
        if value not in (0, 1):
            raise ValueError("invalid bool tag")
        return bool(value)
    if type_ == "unit":
        return None
    if type_ == "string":
        return _decode_string(reader)
    if type_ == "atom":
        return {"atom": _decode_string(reader, atom=True)}
    if type_ == "error":
        return {
            "code": _decode(get, "atom", reader),
            "detail": _decode(get, {"option": "string"}, reader),
        }
    if not isinstance(type_, dict) or len(type_) != 1:
        raise ValueError("invalid value type")
    constructor, payload_type = next(iter(type_.items()))
    if constructor == "rec":
        fields = _record_fields(get, type_, "value")
        values = [_decode(get, field_type, reader) for field_type in fields]
        names = _record_names(get, type_["rec"])
        if names is not None:
            if len(names) != len(fields):
                raise ValueError("record field name count does not match declaration")
            return dict(zip(names, values, strict=True))
        return values
    if constructor == "list":
        length = reader.uleb()
        if length > MAX_LIST_LENGTH:
            raise ValueError("list exceeds 65536 elements")
        return [_decode(get, payload_type, reader) for _ in range(length)]
    tag = reader.byte()
    if tag not in (0, 1):
        raise ValueError(f"invalid {constructor} tag")
    if constructor == "option":
        return {"tag": "none"} if tag == 0 else {
            "tag": "some", "v": _decode(get, payload_type, reader)
        }
    if constructor == "result":
        return {
            "tag": "ok" if tag == 0 else "err",
            "v": _decode(get, payload_type if tag == 0 else "error", reader),
        }
    raise ValueError(f"unknown type constructor {constructor}")
