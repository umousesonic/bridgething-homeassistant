import gzip
import json
import struct
import unittest
import uuid

from api.protocol import decode_frames, dumps, encode_frame, encode_json, envelope, normalize_msgpack, unpack_msgpack


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
