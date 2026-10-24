import gzip
import json
import struct
import unittest
import uuid

from api.art import art_url, asset_id_for, with_edge
from api.protocol import as_bytes, decode_frames, dumps, encode_frame, encode_json, encode_msgpack, encode_wire, envelope, normalize_msgpack, unpack_msgpack


class FrameTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        message = envelope("event", "player", "snapshot", {"track": None})
        buffer = bytearray(encode_json(message))
        decoded = decode_frames(buffer)
        self.assertEqual(decoded, [message])
        self.assertEqual(buffer, bytearray())

    def test_two_frames_in_one_buffer(self) -> None:
        first = envelope("event", "time", "snapshot", {"wallClockUnixS": 1})
        second = envelope("response", "system", "keepaliveAck", {"seq": 3}, "req-1")
        buffer = bytearray(encode_json(first) + encode_json(second))
        self.assertEqual(decode_frames(buffer), [first, second])

    def test_partial_frame_waits(self) -> None:
        message = envelope("event", "capabilities", "announce", {"musicProvider": "none"})
        encoded = encode_json(message)
        buffer = bytearray(encoded[:10])
        self.assertEqual(decode_frames(buffer), [])
        buffer += encoded[10:]
        self.assertEqual(decode_frames(buffer), [message])

    def test_gzip_frame(self) -> None:
        message = {"id": "abc", "meta": {"kind": "event"}, "data": {"type": "ack"}}
        payload = gzip.compress(dumps(message).encode("utf-8"))
        buffer = bytearray(encode_frame(payload, compression=1))
        self.assertEqual(decode_frames(buffer), [message])

    def test_msgpack_uuid_bin(self) -> None:
        value = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
        payload = bytes([0x81, 0xA2, 0x69, 0x64, 0xC4, 0x10]) + value.bytes
        self.assertEqual(normalize_msgpack(unpack_msgpack(payload))["id"], str(value))

    def test_msgpack_float64(self) -> None:
        payload = bytes([0x81, 0xA5]) + b"speed" + bytes([0xCB]) + struct.pack(">d", 1.5)
        self.assertEqual(unpack_msgpack(payload), {"speed": 1.5})
        frame = bytearray(encode_frame(payload, encoding=0))
        self.assertEqual(decode_frames(frame), [{"speed": 1.5}])

    def test_client_socket_is_json_without_a_frame_header(self) -> None:
        message = envelope("command", "hardware", "displaySetMode", {"mode": "manual"})
        text = dumps(message)
        self.assertTrue(text.startswith("{"))
        self.assertNotIn("dead", text.lower())
        parsed = json.loads(text)
        self.assertEqual(parsed["data"]["data"]["event"], "displaySetMode")
        self.assertEqual(parsed["data"]["data"]["data"]["mode"], "manual")
        self.assertIsNone(parsed["meta"].get("data"))

    def test_artwork_ids_round_trip_the_edge_the_device_rewrites(self) -> None:
        scdn = asset_id_for("https://i.scdn.co/image/deadbeef")
        self.assertEqual(scdn, "spotify/img/248/ideadbeef")
        self.assertEqual(with_edge(scdn or "", 96), "spotify/img/96/ideadbeef")
        self.assertEqual(art_url("spotify/img/96/ideadbeef"), "https://i.scdn.co/image/deadbeef")
        foreign = "https://example.com/a b/c?x=1&y=Ü"
        encoded = asset_id_for(foreign, 96)
        self.assertTrue(encoded and encoded.startswith("spotify/img/96/u"))
        self.assertEqual(art_url(with_edge(encoded or "", 248)), foreign)
        self.assertIsNone(art_url("spotify/img/248/xdeadbeef"))
        self.assertEqual(with_edge("builtin/img/liked-songs", 96), "builtin/img/liked-songs")

    def test_gateway_output_uses_the_0_1_18_json_compatibility_format(self) -> None:
        message = envelope(
            "response",
            "asset",
            "got",
            {"id": "spotify/img/96/ideadbeef", "mime": "image/jpeg", "body": {"type": "inline", "data": b"\xff\xd8\xff"}},
        )
        frame = encode_wire(message)
        self.assertEqual(frame[4], 1)
        decoded = decode_frames(bytearray(frame))[0]
        self.assertEqual(as_bytes(decoded["data"]["data"]["data"]["body"]["data"]), b"\xff\xd8\xff")
        plain = encode_wire(envelope("event", "player", "snapshot", {"track": None}))
        self.assertEqual(plain[4], 1)
        decoded_plain = decode_frames(bytearray(plain))[0]
        self.assertIsInstance(decoded_plain["id"], str)
        uuid.UUID(decoded_plain["id"])

    def test_optional_official_msgpack_encoder_uses_automatic_gzip(self) -> None:
        message = envelope("event", "test", "large", {"value": "a" * (16 * 1024)})
        frame = encode_msgpack(message)
        self.assertEqual(frame[3], 1)
        self.assertEqual(frame[4], 0)
        self.assertEqual(decode_frames(bytearray(frame)), [message])
