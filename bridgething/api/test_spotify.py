import base64
import hashlib
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from api.connect import DESKTOP_CLIENT_ID, SpotifyError as ConnectError, _read_welcome, _solve_login5_hashcash, _trailing_zeros
from api.gateway import GatewaySession
from api.ha import HomeAssistant
from api.pb import Keep, decode, encode, message
from api.server import Companion, route_post
from api.shannon import Shannon
from api.art import THUMB_EDGE, with_edge
from api.protocol import as_bytes, decode_frames, encode_json
from api.spotify import CAPTURE_BACKOFF_S, SpotifySession, _track
from api.state import AppState

TRACK_URI = "spotify:track:abc"
IMAGE = bytes.fromhex("c0ffee")


def _track_proto(name: str = "Song", uri: str = TRACK_URI) -> bytes:
    return encode(
        [
            (2, name),
            (3, [(2, "Album"), (3, [[(2, "Ada")]]), (9, [[(1, IMAGE)]])]),
            (4, [[(2, "Ada")]]),
            (7, 1000 << 1),
            (36, uri),
        ]
    )


def _collection_proto(uris: tuple[str, ...] = (TRACK_URI,)) -> bytes:
    return encode([(1, [[(1, uri)] for uri in uris])])


def _rootlist_proto(uri: str = "spotify:playlist:p") -> bytes:
    return encode(
        [
            (
                5,
                [
                    (3, [[(1, uri)]]),
                    (4, [[(2, [(1, "Morning"), (13, [[(2, "https://i.scdn.co/image/c0ffee")]])]), (3, 1), (5, "ada")]]),
                ],
            )
        ]
    )


def _recent_proto() -> bytes:
    return encode([(1, [[(1, "spotify:playlist:p"), (3, TRACK_URI)]])])


def _playlist_proto() -> bytes:
    return encode(
        [
            (2, 1),
            (3, [(1, "Morning"), (13, [[(1, "default"), (2, "https://i.scdn.co/image/c0ffee")]])]),
            (5, [(3, [[(1, TRACK_URI)]])]),
        ]
    )


def _extended_track(name: str = "Song", uri: str = TRACK_URI) -> bytes:
    entity = [(2, uri), (3, [(2, _track_proto(name, uri))])]
    return encode([(2, [[(3, [entity])]])])


def _sparse_cluster_proto() -> bytes:
    return encode([(1, [(3, [(7, [(1, TRACK_URI)]), (10, 4000), (12, True)])])])


def _cluster_proto() -> bytes:
    track = [
        (1, TRACK_URI),
        (
            3,
            [
                [(1, "title"), (2, "Song")],
                [(1, "artist_name"), (2, "Ada")],
                [(1, "artist_uri"), (2, "spotify:artist:a")],
                [(1, "album_title"), (2, "Album")],
                [(1, "album_uri"), (2, "spotify:album:b")],
                [(1, "duration"), (2, "180000")],
                [(1, "image_url"), (2, "https://i.scdn.co/image/cover123")],
                [(1, "collection.in_collection"), (2, "true")],
            ],
        ),
        (8, "spotify:album:b"),
        (10, "spotify:artist:a"),
    ]
    player = [
        (2, "spotify:album:b"),
        (7, track),
        (10, 1500),
        (11, 180000),
        (13, True),
        (16, [(1, True), (2, True)]),
        (21, [[(1, "context_description"), (2, "Album")]]),
    ]
    device = [(1, "phone"), (2, [(2, 26214), (3, "Phone"), (10, "phone")])]
    return encode([(1, [(2, "phone"), (3, player), (4, [device])])])


class _Response:
    def __init__(self, payload: dict | list, status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _Raw:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._raw = payload
        self.status = status
        self.headers = {}

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> "_Raw":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _Bytes:
    def __init__(self, payload: bytes) -> None:
        self._raw = payload
        self.status = 200
        self.headers = {"Content-Type": "image/jpeg"}

    def read(self, _limit: int = -1) -> bytes:
        return self._raw

    def __enter__(self) -> "_Bytes":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _logged_in(session: SpotifySession, username: str = "ada") -> None:
    session._data = {"access_token": "access-1", "expires_at": time.time() + 3600}
    session.connect.username = username


def _web_track() -> dict:
    return {
        "uri": TRACK_URI,
        "name": "Song",
        "type": "track",
        "duration_ms": 1000,
        "artists": [{"uri": "spotify:artist:a", "name": "Ada"}],
        "album": {"uri": "spotify:album:b", "name": "Album", "images": [{"url": "https://i.scdn.co/image/c0ffee"}]},
    }


def _web_page(items: list) -> dict:
    return {"items": items, "total": len(items), "next": None}


def _library_opener():
    def opener(request, timeout: float = 20):
        path = urllib.parse.urlparse(request.full_url).path
        if path == "/v1/me":
            return _Response({"id": "ada", "display_name": "displayname"})
        if path.endswith("/me/playlists"):
            return _Response(
                _web_page(
                    [
                        {
                            "uri": "spotify:playlist:p",
                            "name": "Morning",
                            "owner": {"display_name": "Ada"},
                            "tracks": {"total": 1},
                            "images": [{"url": "https://i.scdn.co/image/c0ffee"}],
                        }
                    ]
                )
            )
        if path.endswith("/me/player/recently-played"):
            return _Response(_web_page([{"track": _web_track()}]))
        if path.endswith("/me/tracks"):
            return _Response(_web_page([{"track": _web_track()}]))
        if "/playlists/" in path:
            return _Response(_web_page([{"item": _web_track()}]))
        if path.endswith("/rootlist"):
            return _Raw(_rootlist_proto())
        if path.endswith("/recently-played"):
            return _Raw(_recent_proto())
        if path.endswith("/extended-metadata/v0/extended-metadata"):
            return _Raw(_extended_track())
        if "/metadata/4/" in path:
            raise AssertionError(path)
        if path.endswith("/image/c0ffee"):
            return _Bytes(IMAGE)
        if path.endswith("/liked-songs-300.jpg"):
            return _Bytes(b"\xff\xd8liked")
        if path.endswith("/collection/v2/contains"):
            return _Raw(encode([(1, 1)]))
        if path.endswith("/collection/v2/paging"):
            accept = request.headers.get("Accept")
            if accept != "application/vnd.collection-v2.spotify.proto":
                raise AssertionError(accept)
            return _Raw(_collection_proto())
        if "/playlist/v2/playlist/" in path:
            return _Raw(_playlist_proto())
        raise AssertionError(path)

    return opener


class SpotifyTokenTests(unittest.TestCase):
    def test_shannon_round_trip_matches_the_access_point_packet_split(self) -> None:
        payload = b"hello"
        frame = bytearray(bytes([0xAB]) + len(payload).to_bytes(2, "big") + payload)
        send = Shannon(b"0123456789abcdef0123456789abcdef")
        send.nonce_u32(0)
        send.encrypt(frame)
        mac = send.finish(4)
        recv = Shannon(b"0123456789abcdef0123456789abcdef")
        recv.nonce_u32(0)
        header = frame[:3]
        body = frame[3:]
        recv.decrypt(header)
        recv.decrypt(body)
        recv.check_mac(mac)
        self.assertEqual(bytes(header), bytes([0xAB, 0, 5]))
        self.assertEqual(bytes(body), payload)

    def test_protobuf_keeps_a_zero_and_nests_a_message(self) -> None:
        raw = encode([(1, [(2, "hi"), (3, Keep(0))])])
        from api.pb import message

        parsed = message(raw, {1: ("inner", "message", {2: ("name", "string"), 3: ("flag", "varint")})})
        self.assertEqual(parsed["inner"]["name"], "hi")
        self.assertEqual(parsed["inner"]["flag"], 0)

    def test_refresh_token_is_stored_and_rotated_without_leaving_the_companion(self) -> None:
        seen: dict[str, str] = {}

        def opener(request, timeout: float = 20):
            if request.data:
                seen["body"] = request.data.decode()
            if "/api/token" in request.full_url:
                return _Response({"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600})
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "spotify.json")
            session = SpotifySession(path, opener=opener)
            status = session.adopt(
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_at": time.time() + 3600,
                    "client_id": "client",
                    "client_secret": "secret",
                }
            )
            self.assertEqual(status, {"loggedIn": True, "username": None})
            self.assertNotIn("access_token", status)
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved["refresh_token"], "refresh-1")
            self.assertNotIn("client_secret", saved)
            self.assertEqual(saved["client_id"], DESKTOP_CLIENT_ID)

            session._data["expires_at"] = time.time() - 10
            self.assertEqual(session.bearer(), "access-2")
            saved = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(saved["refresh_token"], "refresh-2")
            self.assertEqual(saved["access_token"], "access-2")
            self.assertIn(f"client_id={DESKTOP_CLIENT_ID}", seen["body"])
            self.assertNotIn("client_secret", seen["body"])

    def test_welcome_keeps_the_reusable_credential(self) -> None:
        payload = encode([(0x0A, "canonicaluserid123"), (0x1E, 1), (0x28, b"reusable")])
        found = _read_welcome(payload)
        self.assertEqual(found["username"], "canonicaluserid123")
        self.assertEqual(found["auth_data"], b"reusable")
        self.assertEqual(found["auth_type"], 1)

    def test_login5_hashcash_meets_the_requested_width(self) -> None:
        suffix, elapsed = _solve_login5_hashcash(b"context", b"prefix", 8)
        check = int.from_bytes(hashlib.sha1(b"prefix" + suffix).digest()[12:20], "big", signed=True)
        self.assertGreaterEqual(_trailing_zeros(check), 8)
        self.assertGreaterEqual(elapsed, 0)

    def test_stored_credential_mints_an_access_token_without_the_refresh_grant(self) -> None:
        seen: list[bytes] = []

        def opener(request, timeout: float = 20):
            url = request.full_url
            if "clienttoken.spotify.com" in url:
                return _Raw(encode([(1, 1), (2, [(1, "client-tok"), (3, 7200)])]))
            if "login5.spotify.com" in url:
                seen.append(request.data)
                self.assertEqual(request.get_header("Client-token"), "client-tok")
                return _Raw(encode([(1, [(1, "canonicaluserid123"), (2, "access-from-blob"), (3, b"new-blob"), (4, 3600)])]))
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            session.connect.manual = False
            session.connect.device_id = "device-1"
            session.connect.username = "canonicaluserid123"
            session._data = {
                "access_token": "expired",
                "refresh_token": "refresh-original",
                "expires_at": time.time() - 10,
                "username": "canonicaluserid123",
                "auth_data": base64.b64encode(b"stored-blob").decode("ascii"),
                "connect_device_id": "device-1",
            }
            self.assertEqual(session.bearer(), "access-from-blob")
            saved = json.loads(Path(session.path).read_text(encoding="utf-8"))
            self.assertEqual(saved["refresh_token"], "refresh-original")
            self.assertEqual(saved["access_token"], "access-from-blob")
            self.assertEqual(base64.b64decode(saved["auth_data"]), b"new-blob")
            stored = message(next(value for field, _wire, value in decode(seen[0]) if field == 100), {1: ("username", "string"), 2: ("data", "bytes")})
            self.assertEqual(stored["username"], "canonicaluserid123")
            self.assertEqual(stored["data"], b"stored-blob")

    def test_rejected_stored_login_falls_back_to_the_refresh_token(self) -> None:
        def opener(request, timeout: float = 20):
            url = request.full_url
            if "clienttoken.spotify.com" in url:
                return _Raw(encode([(1, 1), (2, [(1, "client-tok"), (3, 7200)])]))
            if "login5.spotify.com" in url:
                return _Raw(encode([(2, 1)]))
            if "/api/token" in url:
                return _Response({"access_token": "access-from-refresh", "expires_in": 3600})
            if "apresolve" in url:
                return _Response({"accesspoint": []})
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            session.connect.manual = False
            session.connect.device_id = "device-1"
            session._data = {
                "access_token": "expired",
                "refresh_token": "refresh-1",
                "expires_at": time.time() - 10,
                "username": "canonicaluserid123",
                "auth_data": base64.b64encode(b"stored-blob").decode("ascii"),
                "connect_device_id": "device-1",
            }
            self.assertEqual(session.bearer(), "access-from-refresh")
            saved = json.loads(Path(session.path).read_text(encoding="utf-8"))
            self.assertNotIn("auth_data", saved)
            self.assertEqual(saved["refresh_token"], "refresh-1")

    def test_reusable_login_backs_off_after_a_failure(self) -> None:
        calls = {"n": 0}

        def opener(request, timeout: float = 20):
            if "apresolve.spotify.com" in request.full_url:
                calls["n"] += 1
                raise ConnectionRefusedError(111, "Connection refused")
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            session.connect.manual = False
            session._data = {"access_token": "access-1", "expires_at": time.time() + 3600}
            self.assertEqual(session.bearer(), "access-1")
            self.assertEqual(session.bearer(), "access-1")
            self.assertEqual(calls["n"], 1)
            self.assertGreater(session._capture_after, time.time())
            session._capture_fails = len(CAPTURE_BACKOFF_S)
            session._capture_after = time.time() - 1
            self.assertEqual(session.bearer(), "access-1")
            self.assertEqual(calls["n"], 2)
            self.assertGreaterEqual(session._capture_after - time.time(), CAPTURE_BACKOFF_S[-1] - 1)

    def test_web_requests_wait_in_line_and_retry_a_rate_limit(self) -> None:
        import io

        clock = [0.0]
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock[0] += seconds

        calls = {"n": 0}

        def opener(request, timeout: float = 20):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "2"},
                    io.BytesIO(b'{"error":{"status":429,"message":"API rate limit exceeded"}}'),
                )
            return _Response({"items": [], "total": 0, "next": None})

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            session.connect.manual = False
            session._data = {
                "access_token": "access-1",
                "expires_at": time.time() + 3600,
                "username": "canonicaluserid123",
                "auth_data": base64.b64encode(b"blob").decode("ascii"),
            }
            session._web_gate.clock = lambda: clock[0]
            session._web_gate.sleep = sleep
            session._page("/me/playlists", 1, 0)
            session._page("/me/playlists", 1, 0)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept[0], 2.0)
        self.assertGreaterEqual(slept[-1], 0.35)

    def test_stale_home_assistant_login_does_not_replace_a_live_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=lambda request, timeout=20: (_ for _ in ()).throw(AssertionError(request.full_url)))
            session._data = {
                "access_token": "live",
                "refresh_token": "refresh-rotated",
                "expires_at": time.time() + 1800,
                "username": "canonicaluserid123",
                "auth_data": base64.b64encode(b"reusable").decode("ascii"),
            }
            session.connect.username = "canonicaluserid123"
            session.adopt(
                {
                    "access_token": "stale",
                    "refresh_token": "refresh-original",
                    "expires_at": time.time() - 10,
                }
            )
            saved = json.loads(Path(session.path).read_text(encoding="utf-8"))
            self.assertEqual(saved["access_token"], "live")
            self.assertEqual(saved["refresh_token"], "refresh-rotated")
            self.assertEqual(base64.b64decode(saved["auth_data"]), b"reusable")

    def test_credentials_route_hides_the_token_from_home_assistant_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=lambda request, timeout=20: (_ for _ in ()).throw(AssertionError(request.full_url)))
            companion = Companion(AppState(), HomeAssistant(token=""), session)
            status, payload = route_post(
                companion,
                "/spotify/credentials",
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_at": time.time() + 3600,
                    "client_id": "client",
                    "client_secret": "secret",
                },
            )
            encoded = json.dumps(payload)
            self.assertEqual(status, 200)
            self.assertIsNone(payload["spotify"]["username"])
            self.assertNotIn("access-1", encoded)
            self.assertNotIn("refresh-1", encoded)
            self.assertNotIn("secret", encoded)

    def test_stale_display_username_is_replaced_by_canonical_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spotify.json"
            path.write_text(
                json.dumps(
                    {
                        "access_token": "access-1",
                        "expires_at": time.time() + 3600,
                        "username": "display-name",
                    }
                ),
                encoding="utf-8",
            )
            session = SpotifySession(str(path), opener=_library_opener())
            self.assertIsNone(session.status()["username"])
            self.assertEqual(session.connect.username, "")
            session.connect.manual = False
            with patch("api.spotify._ap_login", side_effect=ConnectError("notSupported", "no access point")):
                with patch("api.connect._ap_username", return_value="canonical-user-id"):
                    session.connect._username()
            self.assertEqual(session.connect.username, "canonical-user-id")
            self.assertEqual(session.status()["username"], "canonical-user-id")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["username"], "canonical-user-id")

    def test_adopt_ignores_a_display_username(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=_library_opener())
            session.adopt(
                {
                    "access_token": "access-1",
                    "expires_at": time.time() + 3600,
                    "username": "displayname",
                }
            )
            self.assertIsNone(session.status()["username"])
            self.assertEqual(session.connect.username, "")
            session.adopt(
                {
                    "access_token": "access-1",
                    "expires_at": time.time() + 3600,
                    "username": "canonicaluserid123",
                }
            )
            self.assertEqual(session.status()["username"], "canonicaluserid123")
            self.assertEqual(session.connect.username, "canonicaluserid123")

    def test_display_username_is_not_used_for_rootlist_or_collection(self) -> None:
        seen: list[str] = []
        inner = _library_opener()

        def opener(request, timeout: float = 20):
            path = urllib.parse.urlparse(request.full_url).path
            seen.append(path)
            if path == "/v1/me":
                return _Response({"id": "canonical-user-id", "display_name": "displayname"})
            return inner(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "spotify.json"
            path.write_text(
                json.dumps(
                    {
                        "access_token": "access-1",
                        "expires_at": time.time() + 3600,
                        "username": "displayname",
                    }
                ),
                encoding="utf-8",
            )
            session = SpotifySession(str(path), opener=opener)
            session.connect.rootlist()
            self.assertEqual(session._collection_uri(), "spotify:user:canonical-user-id:collection")
            self.assertTrue(any(path.endswith("/user/canonical-user-id/rootlist") for path in seen))
            self.assertFalse(any("/user/displayname/" in path for path in seen))
            self.assertEqual(session.status()["username"], "canonical-user-id")

    def test_empty_rootlist_still_lists_web_playlists(self) -> None:
        inner = _library_opener()

        def opener(request, timeout: float = 20):
            path = urllib.parse.urlparse(request.full_url).path
            if path.endswith("/rootlist"):
                return _Raw(encode([]))
            return inner(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            result = session.browse({"nodeId": None, "limit": 10, "offset": 0})
            shelf = result["entries"][0]["data"]["previewChildren"]
            self.assertEqual(shelf[0]["data"]["data"]["uri"], "spotify:collection:tracks")
            self.assertIsNone(shelf[0]["data"]["data"]["ownerName"])
            self.assertEqual(shelf[1]["data"]["data"]["name"], "Morning")
            self.assertEqual(shelf[1]["data"]["data"]["uri"], "spotify:playlist:p")
            self.assertGreaterEqual(result["entries"][0]["data"]["total"], 2)

    def test_playing_playlist_is_on_the_shelf_when_lists_are_empty(self) -> None:
        def opener(request, timeout: float = 20):
            path = urllib.parse.urlparse(request.full_url).path
            if path.endswith("/rootlist"):
                return _Raw(encode([]))
            if path.endswith("/me/playlists"):
                return _Response(_web_page([]))
            if path.endswith("/recently-played") or path.endswith("/me/player/recently-played"):
                return _Response(_web_page([])) if path.startswith("/v1") or "/me/" in path else _Raw(encode([]))
            if path.endswith("/liked-songs-300.jpg"):
                return _Bytes(b"\xff\xd8liked")
            if "/playlist/v2/playlist/" in path:
                raise OSError("playlist header unavailable")
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            session.connect.ingest(
                encode(
                    [
                        (
                            1,
                            [
                                (2, "phone"),
                                (
                                    3,
                                    [
                                        (2, "spotify:playlist:chill"),
                                        (
                                            7,
                                            [
                                                (1, TRACK_URI),
                                                (
                                                    3,
                                                    [
                                                        [(1, "title"), (2, "Song")],
                                                        [(1, "artist_name"), (2, "Ada")],
                                                        [(1, "album_title"), (2, "Album")],
                                                        [(1, "duration"), (2, "180000")],
                                                    ],
                                                ),
                                            ],
                                        ),
                                        (11, 180000),
                                        (13, True),
                                        (21, [[(1, "context_description"), (2, "Chill Synth")]]),
                                    ],
                                ),
                            ],
                        )
                    ]
                )
            )
            result = session.browse({"nodeId": None, "limit": 10, "offset": 0})
            shelf = result["entries"][0]["data"]["previewChildren"]
            self.assertEqual(shelf[0]["data"]["data"]["uri"], "spotify:collection:tracks")
            self.assertEqual(shelf[1]["data"]["data"]["uri"], "spotify:playlist:chill")
            self.assertEqual(shelf[1]["data"]["data"]["name"], "Chill Synth")

    def test_library_track_keeps_the_fields_the_shelf_decodes(self) -> None:
        card = _track({"uri": "spotify:track:x", "name": "Untitled", "artists": [], "album": {}}, {})
        self.assertEqual(card["data"]["durationMs"], 0)
        self.assertEqual(card["data"]["imageId"], "")
        self.assertIsNone(card["data"]["album"]["artworkId"])
        self.assertEqual(card["data"]["album"]["name"], "")
        self.assertEqual(card["data"]["artist"]["name"], "")

    def test_library_browse_uses_the_stored_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=_library_opener())
            _logged_in(session)
            result = session.browse({"nodeId": None, "limit": 10, "offset": 0})
            self.assertEqual([item["data"]["nodeId"] for item in result["entries"]], ["playlists", "recently-played"])
            shelf = result["entries"][0]["data"]["previewChildren"]
            self.assertEqual(shelf[0]["type"], "item")
            self.assertEqual(shelf[0]["data"]["type"], "playlist")
            self.assertEqual(shelf[0]["data"]["data"]["uri"], "spotify:collection:tracks")
            self.assertEqual(shelf[0]["data"]["data"]["name"], "Liked Songs")
            self.assertEqual(shelf[0]["data"]["data"]["artworkId"], "spotify/img/300/liked-songs-300.jpg")
            self.assertIsNone(shelf[0]["data"]["data"]["ownerName"])
            self.assertIsNone(shelf[0]["data"]["data"]["trackCount"])
            self.assertEqual(shelf[1]["data"]["data"]["name"], "Morning")
            self.assertEqual(shelf[1]["data"]["data"]["uri"], "spotify:playlist:p")
            self.assertEqual(shelf[1]["data"]["data"]["artworkId"], "spotify/img/300/c0ffee")
            self.assertEqual(shelf[1]["data"]["data"]["ownerName"], "ada")
            self.assertEqual(shelf[1]["data"]["data"]["trackCount"], 1)
            self.assertEqual(result["entries"][0]["data"]["total"], 2)
            opened = session.browse({"nodeId": "recently-played", "limit": 10, "offset": 0})
            card = opened["entries"][0]
            self.assertEqual(card["type"], "item")
            self.assertEqual(card["data"]["type"], "track")
            preview = card["data"]["data"]
            self.assertEqual(preview["name"], "Song")
            self.assertEqual(preview["id"], TRACK_URI)
            self.assertEqual(preview["imageId"], "spotify/img/300/c0ffee")
            self.assertIsInstance(preview["durationMs"], int)
            self.assertTrue(preview["saved"])
            narrow = session.browse({"nodeId": None, "limit": 0, "offset": 0, "preview": 0})
            self.assertNotIn("previewChildren", narrow["entries"][0]["data"])
            self.assertGreaterEqual(narrow["entries"][0]["data"]["total"], 2)
            self.assertEqual(opened["entries"][0]["type"], "item")
            self.assertEqual(opened["entries"][0]["data"]["data"]["id"], TRACK_URI)
            playlist = session.browse({"nodeId": "spotify:playlist:p", "limit": 10, "offset": 0})
            self.assertEqual(playlist["entries"][0]["data"]["data"]["name"], "Song")
            self.assertEqual(playlist["entries"][0]["data"]["data"]["durationMs"], 1000)
            liked = session.browse({"nodeId": "spotify:collection:tracks", "limit": 10, "offset": 0})
            self.assertEqual(liked["entries"][0]["data"]["data"]["name"], "Song")
            self.assertTrue(liked["entries"][0]["data"]["data"]["saved"])
            legacy = session.browse({"nodeId": "spotify:user:ada:playlist:p", "limit": 10, "offset": 0})
            self.assertEqual(legacy["entries"][0]["data"]["data"]["name"], "Song")
            self.assertEqual(session.images["c0ffee"], "https://i.scdn.co/image/c0ffee")
            thumb = with_edge(shelf[1]["data"]["data"]["artworkId"], THUMB_EDGE)
            self.assertEqual(thumb, "spotify/img/96/c0ffee")
            self.assertEqual(session.artwork(thumb), (IMAGE, "image/jpeg"))
            self.assertEqual(session.artwork("spotify/img/248/c0ffee"), (IMAGE, "image/jpeg"))
            self.assertEqual(session.artwork("builtin/img/liked-songs"), (b"\xff\xd8liked", "image/jpeg"))

    def test_gateway_answers_library_from_the_companion_token(self) -> None:
        class Stub:
            logged_in = True

            def browse(self, _body: dict) -> dict:
                return {"entries": [], "total": 0, "hasMore": False}

        session = GatewaySession(AppState(), HomeAssistant(token=""), Stub())
        session.handle(
            {
                "id": "browse-1",
                "meta": {"kind": "request"},
                "data": {"type": "library", "data": {"event": "browse", "data": {"nodeId": None, "limit": 10, "offset": 0}}},
            }
        )
        reply = session.sent[-1]
        self.assertEqual(reply["data"]["data"]["event"], "browseReply")
        self.assertEqual(reply["meta"]["data"]["requestId"], "browse-1")

    def test_library_without_a_token_is_unauthorized(self) -> None:
        session = GatewaySession(AppState(), HomeAssistant(token=""))
        session.handle(
            {
                "id": "browse-2",
                "meta": {"kind": "request"},
                "data": {"type": "library", "data": {"event": "browse", "data": {}}},
            }
        )
        error = session.sent[-1]["data"]["data"]["data"]["error"]
        self.assertEqual(error["type"], "unauthorized")

    def test_volume_buttons_reach_spotify(self) -> None:
        calls: list[tuple[str, str]] = []

        def opener(request, timeout: float = 20):
            parsed = urllib.parse.urlparse(request.full_url)
            calls.append((request.get_method(), parsed.path))
            return _Raw(b"")

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            session.volume_percent = 40
            session.device_id = "phone"
            gateway = GatewaySession(AppState(), HomeAssistant(token=""), session)
            gateway.handle({"id": "vol", "meta": {"kind": "command"}, "data": {"type": "audio", "data": {"event": "volumeUp"}}})
            self.assertTrue(any(method == "PUT" and "/connect/volume/from/" in path and path.endswith("/to/phone") for method, path in calls))
            changed = gateway.sent[-1]
            self.assertEqual(changed["data"]["type"], "audio")
            self.assertEqual(changed["data"]["data"]["event"], "volumeChanged")
            self.assertEqual(changed["data"]["data"]["data"]["level"], 0.46)
            self.assertFalse(changed["data"]["data"]["data"]["muted"])

    def test_no_spotify_player_is_a_paused_snapshot_without_a_track(self) -> None:
        def opener(request, timeout: float = 20):
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            player = session.now_playing()
        self.assertIsNone(player["track"])
        self.assertEqual(player["playback"]["state"], "paused")
        self.assertEqual(player["playback"]["shuffleMode"], "off")
        self.assertFalse(player["playback"]["setElapsedTimeAvailable"])
        self.assertNotIn("volume", player)

    def test_playlist_page_lists_its_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=_library_opener())
            _logged_in(session)
            playlist = session.browse({"nodeId": "spotify:playlist:owned", "limit": 10, "offset": 0})
            self.assertEqual(playlist["entries"][0]["data"]["data"]["name"], "Song")
            self.assertFalse(playlist["hasMore"])

    def test_shelf_keeps_rootlist_cards_when_playlist_hydration_fails(self) -> None:
        base = _library_opener()

        def opener(request, timeout: float = 20):
            if "/playlist/v2/playlist/" in urllib.parse.urlparse(request.full_url).path:
                raise OSError("playlist header unavailable")
            return base(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            shelf = session.browse({"nodeId": None, "preview": 14})["entries"][0]["data"]["previewChildren"]
            self.assertEqual([item["data"]["data"]["name"] for item in shelf], ["Liked Songs", "Morning"])

    def test_shelf_accepts_legacy_user_playlist_uris(self) -> None:
        base = _library_opener()

        def opener(request, timeout: float = 20):
            if urllib.parse.urlparse(request.full_url).path.endswith("/rootlist"):
                return _Raw(_rootlist_proto("spotify:user:ada:playlist:p"))
            return base(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            shelf = session.browse({"nodeId": None, "preview": 14})["entries"][0]["data"]["previewChildren"]
            self.assertEqual(shelf[1]["data"]["data"]["uri"], "spotify:user:ada:playlist:p")

    def test_liked_songs_falls_back_when_collection_paging_is_empty(self) -> None:
        base = _library_opener()

        def opener(request, timeout: float = 20):
            if urllib.parse.urlparse(request.full_url).path.endswith("/collection/v2/paging"):
                return _Raw(_collection_proto(()))
            return base(request, timeout)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            liked = session.browse({"nodeId": "spotify:user:ada:collection", "limit": 10, "offset": 0})
            self.assertEqual(liked["entries"][0]["data"]["data"]["name"], "Song")
            self.assertTrue(liked["entries"][0]["data"]["data"]["saved"])

    def test_playing_a_playlist_track_starts_that_playlist(self) -> None:
        seen: dict[str, object] = {}

        def opener(request, timeout: float = 20):
            seen["body"] = json.loads(request.data.decode())
            seen["path"] = urllib.parse.urlparse(request.full_url).path
            return _Raw(b"", 204)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            session.device_id = "phone"
            self.assertTrue(
                session.command(
                    "play",
                    {"uri": TRACK_URI, "context": {"contextUri": "spotify:playlist:morning"}},
                )
            )
            command = seen["body"]["command"]
            self.assertEqual(command["endpoint"], "play")
            self.assertEqual(command["context"]["uri"], "spotify:playlist:morning")
            self.assertEqual(command["prepare_play_options"]["skip_to"]["track_uri"], TRACK_URI)
            self.assertIn("/player/command/from/", seen["path"])
            self.assertTrue(str(seen["path"]).endswith("/to/phone"))

    def test_heart_saves_through_the_collection_and_updates_the_screen(self) -> None:
        calls: list[tuple[str, str]] = []

        def opener(request, timeout: float = 20):
            path = urllib.parse.urlparse(request.full_url).path
            calls.append((request.get_method(), path))
            if path.endswith("/collection/v2/contains") or path.endswith("/collection/v2/write"):
                return _Raw(b"")
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            self.assertEqual(session.contains([TRACK_URI]), [False])
            session.set_saved(TRACK_URI, True)
            session.set_saved(TRACK_URI, False)
            self.assertTrue(any(method == "POST" and path.endswith("/collection/v2/contains") for method, path in calls))
            self.assertFalse(any(path.endswith("/collection/v2/paging") for _method, path in calls))
            self.assertEqual(sum(path.endswith("/collection/v2/write") for _method, path in calls), 2)
            self.assertFalse(session._liked[TRACK_URI])

    def test_play_accepts_spotify_empty_success_body(self) -> None:
        def opener(request, timeout: float = 20):
            if "/player/command/" in request.full_url:
                return _Raw(b"\n", 204)
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            session.device_id = "phone"
            self.assertTrue(session.command("resume", {}))

    def test_currently_playing_cover_is_fetched_and_transport_reaches_spotify(self) -> None:
        bodies: list[dict] = []
        paths: list[str] = []

        def opener(request, timeout: float = 20):
            parsed = urllib.parse.urlparse(request.full_url)
            paths.append(parsed.path)
            if request.data:
                bodies.append(json.loads(request.data.decode()))
                return _Raw(b"")
            if parsed.path.endswith("/cover123"):
                return _Bytes(b"jpeg-bytes")
            raise AssertionError(request.full_url)

        with tempfile.TemporaryDirectory() as directory:
            session = SpotifySession(str(Path(directory) / "spotify.json"), opener=opener)
            _logged_in(session)
            session.connect.ingest(_cluster_proto())
            state = session.now_playing()
            self.assertEqual(state["track"]["title"], "Song")
            self.assertEqual(state["track"]["artworkId"], "spotify/img/300/cover123")
            self.assertEqual(state["track"]["persistentId"], TRACK_URI)
            self.assertTrue(state["track"]["liked"])
            self.assertTrue(state["track"]["isLikeSupported"])
            self.assertEqual(state["volume"], {"level": 0.4, "muted": False})
            self.assertTrue(state["playback"]["setElapsedTimeAvailable"])
            self.assertEqual(state["picture"], "https://i.scdn.co/image/cover123")
            self.assertEqual(state["playback"]["positionMs"], 1500)
            self.assertEqual(state["track"]["durationMs"], 180000)
            session.connect.ingest(_sparse_cluster_proto())
            later = session.now_playing()
            self.assertEqual(later["track"]["title"], "Song")
            self.assertEqual(later["track"]["artworkId"], "spotify/img/300/cover123")
            self.assertEqual(later["track"]["durationMs"], 180000)
            self.assertEqual(later["volume"], {"level": 0.4, "muted": False})
            session._liked[TRACK_URI] = False
            self.assertFalse(session.now_playing()["track"]["liked"])
            self.assertEqual(state["playback"]["repeat"], "all")
            self.assertEqual(state["context"]["uri"], "spotify:album:b")
            cover = session.artwork(with_edge(state["track"]["artworkId"], THUMB_EDGE))
            self.assertEqual(cover, (b"jpeg-bytes", "image/jpeg"))
            self.assertTrue(session.command("pause", {}))
            self.assertTrue(session.command("skipNext", {}))
            self.assertTrue(session.command("seekTo", {"positionMs": 2500}))
            self.assertEqual([body["command"]["endpoint"] for body in bodies], ["pause", "skip_next", "seek_to"])
            self.assertEqual(bodies[2]["command"]["value"], 2500)
            self.assertTrue(all("/player/command/from/" in path and path.endswith("/to/phone") for path in paths if "/player/command/" in path))

        class Playing:
            logged_in = True

            def command(self, event: str, payload: dict) -> bool:
                self.event = event
                self.payload = payload
                return True

            def now_playing(self) -> dict:
                return state

            def artwork(self, asset_id: str):
                if asset_id.endswith("cover123"):
                    return b"jpeg-bytes", "image/jpeg"
                return None

        gateway = GatewaySession(AppState(), HomeAssistant(token=""), Playing())
        gateway.handle({"id": "pause", "meta": {"kind": "command"}, "data": {"type": "player", "data": {"event": "pause"}}})
        self.assertFalse(any(item["data"]["data"]["event"] == "errorEvent" for item in gateway.sent))
        snapshot = next(item for item in gateway.sent if item["data"]["type"] == "player" and item["data"]["data"]["event"] == "snapshot")
        self.assertEqual(snapshot["data"]["data"]["data"]["track"]["artworkId"], "spotify/img/300/cover123")
        self.assertEqual(snapshot["data"]["data"]["data"]["context"]["uri"], "spotify:album:b")
        gateway.handle(
            {
                "id": "art",
                "meta": {"kind": "request"},
                "data": {"type": "asset", "data": {"event": "request", "data": {"id": "spotify/img/96/cover123"}}},
            }
        )
        got = gateway.sent[-1]
        self.assertEqual(got["data"]["data"]["event"], "got")
        self.assertEqual(got["data"]["data"]["data"]["body"], {"type": "inline", "data": b"jpeg-bytes"})
        self.assertNotIn("bytes", got["data"]["data"]["data"])
        wire = encode_json(got)
        self.assertEqual(wire[4], 1)
        decoded = decode_frames(bytearray(wire))[0]
        self.assertEqual(as_bytes(decoded["data"]["data"]["data"]["body"]["data"]), b"jpeg-bytes")
