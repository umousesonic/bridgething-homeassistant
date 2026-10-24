"""Just enough protobuf to read and write the Spotify messages this companion uses."""

from __future__ import annotations

from typing import Any


class Keep:
    """A zero or empty value that still has to be written. Spotify's handshake requires these."""

    def __init__(self, value: Any) -> None:
        self.value = value


def encode(fields: list[tuple[int, Any]]) -> bytes:
    """Encode (field number, value) pairs. Zero, empty, and false values are left out."""
    out = bytearray()
    for field, value in fields:
        forced = isinstance(value, Keep)
        if forced:
            value = value.value
        if not forced and (value is None or value is False or value == 0 or value == "" or value == b"" or value == []):
            continue
        if isinstance(value, list) and (_is_message(value) or (forced and not value)):
            _write(out, field, value)
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, Keep):
                item = item.value
            _write(out, field, item)
    return bytes(out)


def _is_message(value: list[Any]) -> bool:
    """A list of (field, value) pairs is one nested message. Any other list is repeated."""
    return bool(value) and all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], int) for item in value)


def decode(payload: bytes) -> list[tuple[int, int, Any]]:
    """Return (field number, wire type, value) in the order they appear."""
    fields: list[tuple[int, int, Any]] = []
    offset = 0
    size = len(payload)
    while offset < size:
        key, offset = _varint(payload, offset)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _varint(payload, offset)
        elif wire == 1:
            value = payload[offset : offset + 8]
            offset += 8
        elif wire == 2:
            length, offset = _varint(payload, offset)
            value = payload[offset : offset + length]
            offset += length
        elif wire == 5:
            value = payload[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.append((field, wire, value))
    return fields


def message(payload: bytes, schema: dict[int, tuple]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for field, _wire, value in decode(payload):
        spec = schema.get(field)
        if spec is None:
            continue
        name = spec[0]
        kind = spec[1]
        if kind == "map":
            entry = message(value, {1: ("key", spec[2]), 2: ("value", spec[3], *spec[4:])})
            found.setdefault(name, {})[entry.get("key")] = entry.get("value")
            continue
        parsed = _cast(value, spec)
        if kind.startswith("repeat"):
            found.setdefault(name, []).append(parsed)
        else:
            found[name] = parsed
    return found


def zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _cast(value: Any, spec: tuple) -> Any:
    kind = spec[1]
    if kind in ("varint", "repeat-varint"):
        return value
    if kind == "bool":
        return bool(value)
    if kind in ("sint", "repeat-sint"):
        return zigzag(value)
    if kind in ("string", "repeat-string"):
        return value.decode("utf-8", "replace")
    if kind == "bytes":
        return value
    if kind in ("message", "repeat-message"):
        return message(value, spec[2])
    raise ValueError(f"unsupported protobuf kind {kind}")


def _write(out: bytearray, field: int, value: Any) -> None:
    if isinstance(value, bool):
        _key(out, field, 0)
        _put_varint(out, int(value))
    elif isinstance(value, int):
        _key(out, field, 0)
        _put_varint(out, value)
    elif isinstance(value, str):
        _key(out, field, 2)
        _put_bytes(out, value.encode("utf-8"))
    elif isinstance(value, (bytes, bytearray)):
        _key(out, field, 2)
        _put_bytes(out, bytes(value))
    elif isinstance(value, list):
        _key(out, field, 2)
        _put_bytes(out, encode(value))
    else:
        raise TypeError(f"cannot encode {type(value).__name__}")


def _key(out: bytearray, field: int, wire: int) -> None:
    _put_varint(out, (field << 3) | wire)


def _put_varint(out: bytearray, value: int) -> None:
    value &= (1 << 64) - 1
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _put_bytes(out: bytearray, payload: bytes) -> None:
    _put_varint(out, len(payload))
    out += payload


def _varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(payload):
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated protobuf varint")
