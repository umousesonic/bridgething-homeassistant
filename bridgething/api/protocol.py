"""Gateway frame codec and envelope helpers.

The network gateway uses a 16-byte header. The local client socket on port 8891
does not: one JSON object is one WebSocket text frame.
"""

from __future__ import annotations

import gzip
import json
import struct
import uuid
from typing import Any

MAGIC = 0xDEAD
VERSION = 2
ENCODING_JSON = 1
ENCODING_MSGPACK = 0
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_PAYLOAD = 16 * 1024 * 1024
AUTO_GZIP_THRESHOLD = 16 * 1024 - 128

UUID_KEYS = {
    "id",
    "requestId",
    "tunnelId",
    "connectionId",
    "streamId",
    "transferId",
}


class BridgeJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, bytes):
            return list(o)
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


def dumps(value: Any) -> str:
    return json.dumps(value, cls=BridgeJSONEncoder, separators=(",", ":"))


def encode_frame(payload: bytes, *, compression: int = 0, encoding: int = ENCODING_JSON) -> bytes:
    header = struct.pack(">HBBBBHQ", MAGIC, VERSION, compression, encoding, 0, 0, len(payload))
    return header + payload


def encode_json(message: dict[str, Any]) -> bytes:
    return encode_frame(dumps(message).encode("utf-8"))


def encode_msgpack(message: dict[str, Any]) -> bytes:
    payload = pack_msgpack(message)
    if len(payload) > AUTO_GZIP_THRESHOLD:
        compressed = gzip.compress(payload, compresslevel=1, mtime=0)
        if len(compressed) < len(payload):
            return encode_frame(compressed, compression=1, encoding=ENCODING_MSGPACK)
    return encode_frame(payload, encoding=ENCODING_MSGPACK)


def encode_wire(message: dict[str, Any]) -> bytes:
    """Compatibility wire format used by 0.1.18 and accepted by old and current Bridgething."""
    return encode_json(message)


def pack_msgpack(value: Any, key: str | None = None) -> bytes:
    if value is None:
        return b"\xc0"
    if value is False:
        return b"\xc2"
    if value is True:
        return b"\xc3"
    if isinstance(value, bytes):
        return _pack_bin(value)
    if isinstance(value, str):
        if key in UUID_KEYS:
            try:
                return _pack_bin(uuid.UUID(value).bytes)
            except ValueError:
                pass
        return _pack_str(value)
    if isinstance(value, int):
        return _pack_int(value)
    if isinstance(value, float):
        return b"\xcb" + struct.pack(">d", value)
    if isinstance(value, list):
        return _pack_array([pack_msgpack(item) for item in value])
    if isinstance(value, dict):
        packed = [pack_msgpack(str(item_key)) + pack_msgpack(item, str(item_key)) for item_key, item in value.items()]
        return _pack_map(packed)
    if isinstance(value, uuid.UUID):
        return _pack_str(str(value))
    raise TypeError(f"cannot pack {type(value).__name__}")


def _pack_str(value: str) -> bytes:
    raw = value.encode("utf-8")
    size = len(raw)
    if size <= 31:
        return bytes([0xA0 | size]) + raw
    if size <= 255:
        return bytes([0xD9, size]) + raw
    if size <= 65535:
        return b"\xda" + struct.pack(">H", size) + raw
    return b"\xdb" + struct.pack(">I", size) + raw


def _pack_bin(value: bytes) -> bytes:
    size = len(value)
    if size <= 255:
        return bytes([0xC4, size]) + value
    if size <= 65535:
        return b"\xc5" + struct.pack(">H", size) + value
    return b"\xc6" + struct.pack(">I", size) + value


def _pack_int(value: int) -> bytes:
    if 0 <= value <= 127:
        return bytes([value])
    if -32 <= value < 0:
        return bytes([value & 0xFF])
    if 0 <= value <= 0xFF:
        return bytes([0xCC, value])
    if 0 <= value <= 0xFFFF:
        return b"\xcd" + struct.pack(">H", value)
    if 0 <= value <= 0xFFFFFFFF:
        return b"\xce" + struct.pack(">I", value)
    if value >= 0:
        return b"\xcf" + struct.pack(">Q", value)
    if value >= -128:
        return b"\xd0" + struct.pack(">b", value)
    if value >= -32768:
        return b"\xd1" + struct.pack(">h", value)
    if value >= -2147483648:
        return b"\xd2" + struct.pack(">i", value)
    return b"\xd3" + struct.pack(">q", value)


def _pack_array(items: list[bytes]) -> bytes:
    size = len(items)
    body = b"".join(items)
    if size <= 15:
        return bytes([0x90 | size]) + body
    if size <= 65535:
        return b"\xdc" + struct.pack(">H", size) + body
    return b"\xdd" + struct.pack(">I", size) + body


def _pack_map(items: list[bytes]) -> bytes:
    size = len(items)
    body = b"".join(items)
    if size <= 15:
        return bytes([0x80 | size]) + body
    if size <= 65535:
        return b"\xde" + struct.pack(">H", size) + body
    return b"\xdf" + struct.pack(">I", size) + body


def decode_frames(buffer: bytearray) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while len(buffer) >= 16:
        magic, version, compression, encoding, _priority, _reserved, length = struct.unpack(
            ">HBBBBHQ", buffer[:16]
        )
        if magic != MAGIC or version != VERSION:
            raise ValueError(f"bad gateway frame magic={magic:#x} version={version}")
        if length > MAX_PAYLOAD:
            raise ValueError("gateway frame is too large")
        total = 16 + length
        if len(buffer) < total:
            break
        payload = bytes(buffer[16:total])
        del buffer[:total]
        if compression == 1:
            payload = gzip.decompress(payload)
        if encoding == ENCODING_JSON:
            messages.append(json.loads(payload.decode("utf-8")))
        elif encoding == ENCODING_MSGPACK:
            messages.append(normalize_msgpack(unpack_msgpack(payload)))
        else:
            raise ValueError(f"unsupported frame encoding {encoding}")
    return messages


def unpack_msgpack(payload: bytes) -> Any:
    value, offset = _read_msgpack(payload, 0)
    if offset != len(payload):
        raise ValueError("msgpack frame has trailing bytes")
    return value


def normalize_msgpack(value: Any, key: str | None = None) -> Any:
    if isinstance(value, bytes):
        if key in UUID_KEYS and len(value) == 16:
            return str(uuid.UUID(bytes=value))
        return value
    if isinstance(value, list):
        return [normalize_msgpack(item) for item in value]
    if isinstance(value, dict):
        return {normalize_msgpack(item_key): normalize_msgpack(item, str(item_key)) for item_key, item in value.items()}
    return value


def _read_msgpack(payload: bytes, offset: int) -> tuple[Any, int]:
    marker = payload[offset]
    offset += 1
    if marker <= 0x7F:
        return marker, offset
    if 0x80 <= marker <= 0x8F:
        return _read_map(payload, offset, marker & 0x0F)
    if 0x90 <= marker <= 0x9F:
        return _read_array(payload, offset, marker & 0x0F)
    if 0xA0 <= marker <= 0xBF:
        return _read_str(payload, offset, marker & 0x1F)
    if marker == 0xC0:
        return None, offset
    if marker == 0xC2:
        return False, offset
    if marker == 0xC3:
        return True, offset
    if marker == 0xC4:
        size = payload[offset]
        return payload[offset + 1 : offset + 1 + size], offset + 1 + size
    if marker == 0xC5:
        size = struct.unpack_from(">H", payload, offset)[0]
        start = offset + 2
        return payload[start : start + size], start + size
    if marker == 0xC6:
        size = struct.unpack_from(">I", payload, offset)[0]
        start = offset + 4
        return payload[start : start + size], start + size
    if marker == 0xCA:
        return struct.unpack_from(">f", payload, offset)[0], offset + 4
    if marker == 0xCB:
        return struct.unpack_from(">d", payload, offset)[0], offset + 8
    if marker in (0xC7, 0xC8, 0xC9):
        length_fmt = {0xC7: ">B", 0xC8: ">H", 0xC9: ">I"}[marker]
        length_size = struct.calcsize(length_fmt)
        size = struct.unpack_from(length_fmt, payload, offset)[0]
        start = offset + length_size + 1
        return payload[start : start + size], start + size
    if marker in (0xD4, 0xD5, 0xD6, 0xD7, 0xD8):
        size = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}[marker]
        start = offset + 1
        return payload[start : start + size], start + size
    if marker in (0xCC, 0xCD, 0xCE, 0xCF):
        fmt = {0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q"}[marker]
        size = struct.calcsize(fmt)
        return struct.unpack_from(fmt, payload, offset)[0], offset + size
    if marker in (0xD0, 0xD1, 0xD2, 0xD3):
        fmt = {0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}[marker]
        size = struct.calcsize(fmt)
        return struct.unpack_from(fmt, payload, offset)[0], offset + size
    if marker == 0xD9:
        size = payload[offset]
        return _read_str(payload, offset + 1, size)
    if marker == 0xDA:
        size = struct.unpack_from(">H", payload, offset)[0]
        return _read_str(payload, offset + 2, size)
    if marker == 0xDB:
        size = struct.unpack_from(">I", payload, offset)[0]
        return _read_str(payload, offset + 4, size)
    if marker == 0xDC:
        size = struct.unpack_from(">H", payload, offset)[0]
        return _read_array(payload, offset + 2, size)
    if marker == 0xDD:
        size = struct.unpack_from(">I", payload, offset)[0]
        return _read_array(payload, offset + 4, size)
    if marker == 0xDE:
        size = struct.unpack_from(">H", payload, offset)[0]
        return _read_map(payload, offset + 2, size)
    if marker == 0xDF:
        size = struct.unpack_from(">I", payload, offset)[0]
        return _read_map(payload, offset + 4, size)
    if marker >= 0xE0:
        return marker - 256, offset
    raise ValueError(f"unsupported msgpack marker {marker:#x}")


def _read_str(payload: bytes, offset: int, size: int) -> tuple[str, int]:
    end = offset + size
    return payload[offset:end].decode("utf-8"), end


def _read_array(payload: bytes, offset: int, size: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    for _ in range(size):
        item, offset = _read_msgpack(payload, offset)
        items.append(item)
    return items, offset


def _read_map(payload: bytes, offset: int, size: int) -> tuple[dict[Any, Any], int]:
    mapping: dict[Any, Any] = {}
    for _ in range(size):
        key, offset = _read_msgpack(payload, offset)
        value, offset = _read_msgpack(payload, offset)
        mapping[key] = value
    return mapping, offset


def envelope(
    kind: str,
    surface: str,
    event: str,
    body: Any = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    inner: dict[str, Any] = {"event": event}
    if body is not None:
        inner["data"] = body
    meta: dict[str, Any] = {"kind": kind}
    if request_id is not None:
        meta["data"] = {"requestId": request_id}
    return {"id": str(uuid.uuid4()), "meta": meta, "data": {"type": surface, "data": inner}}


def error_response(request_id: str, error_type: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"type": error_type}
    if detail is not None:
        error["data"] = detail
    return {
        "id": str(uuid.uuid4()),
        "meta": {"kind": "response", "data": {"requestId": request_id}},
        "data": {"type": "error", "data": error},
    }


def request_id_of(message: dict[str, Any]) -> str:
    return str(message.get("id") or "")


def kind_of(message: dict[str, Any]) -> str:
    meta = message.get("meta") or {}
    return str(meta.get("kind") or "")


def response_request_id(message: dict[str, Any]) -> str | None:
    meta = message.get("meta") or {}
    data = meta.get("data") or {}
    if isinstance(data, dict) and data.get("requestId"):
        return str(data["requestId"])
    return None


def outer_type(message: dict[str, Any]) -> str:
    data = message.get("data") or {}
    if isinstance(data, dict):
        return str(data.get("type") or "")
    return ""


def inner_event(message: dict[str, Any]) -> tuple[str, Any]:
    data = message.get("data") or {}
    inner = data.get("data") if isinstance(data, dict) else None
    if isinstance(inner, dict) and "event" in inner:
        return str(inner.get("event") or ""), inner.get("data")
    return "", inner


def as_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, list):
        return bytes(int(item) & 0xFF for item in value)
    raise TypeError(f"cannot read bytes from {type(value).__name__}")
