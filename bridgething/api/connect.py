"""Spotify Connect session.

Playback arrives on the dealer socket as a cluster. Library pages come from
spclient when the screen asks. Nothing here polls the public Web API.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .pb import Keep, decode, encode, message
from .shannon import Shannon
from .ws import connect as open_websocket

DESKTOP_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
ANDROID_CLIENT_ID = "9a8d2f0ce77a4e248bb71fefcb557637"
CLIENT_VERSION = "9.1.52.1394"
CLIENT_TOKEN_VERSION = "1.2.52.442"
SPCLIENT_FALLBACK = "https://guc3-spclient.spotify.com"
COLLECTION_TYPE = "application/vnd.collection-v2.spotify.proto"
_TRACK_V4 = 10
_ALBUM_V4 = 9
_ARTIST_V4 = 8
_SHOW_V4 = 11
_PLAYLIST_URI = re.compile(rb"spotify:(?:user:[^:\x00]+:)?playlist:[A-Za-z0-9]+")

_SERVER_KEY = bytes([
    0xAC, 0xE0, 0x46, 0x0B, 0xFF, 0xC2, 0x30, 0xAF, 0xF4, 0x6B, 0xFE, 0xC3, 0xBF, 0xBF, 0x86, 0x3D,
    0xA1, 0x91, 0xC6, 0xCC, 0x33, 0x6C, 0x93, 0xA1, 0x4F, 0xB3, 0xB0, 0x16, 0x12, 0xAC, 0xAC, 0x6A,
    0xF1, 0x80, 0xE7, 0xF6, 0x14, 0xD9, 0x42, 0x9D, 0xBE, 0x2E, 0x34, 0x66, 0x43, 0xE3, 0x62, 0xD2,
    0x32, 0x7A, 0x1A, 0x0D, 0x92, 0x3B, 0xAE, 0xDD, 0x14, 0x02, 0xB1, 0x81, 0x55, 0x05, 0x61, 0x04,
    0xD5, 0x2C, 0x96, 0xA4, 0x4C, 0x1E, 0xCC, 0x02, 0x4A, 0xD4, 0xB2, 0x0C, 0x00, 0x1F, 0x17, 0xED,
    0xC2, 0x2F, 0xC4, 0x35, 0x21, 0xC8, 0xF0, 0xCB, 0xAE, 0xD2, 0xAD, 0xD7, 0x2B, 0x0F, 0x9D, 0xB3,
    0xC5, 0x32, 0x1A, 0x2A, 0xFE, 0x59, 0xF3, 0x5A, 0x0D, 0xAC, 0x68, 0xF1, 0xFA, 0x62, 0x1E, 0xFB,
    0x2C, 0x8D, 0x0C, 0xB7, 0x39, 0x2D, 0x92, 0x47, 0xE3, 0xD7, 0x35, 0x1A, 0x6D, 0xBD, 0x24, 0xC2,
    0xAE, 0x25, 0x5B, 0x88, 0xFF, 0xAB, 0x73, 0x29, 0x8A, 0x0B, 0xCC, 0xCD, 0x0C, 0x58, 0x67, 0x31,
    0x89, 0xE8, 0xBD, 0x34, 0x80, 0x78, 0x4A, 0x5F, 0xC9, 0x6B, 0x89, 0x9D, 0x95, 0x6B, 0xFC, 0x86,
    0xD7, 0x4F, 0x33, 0xA6, 0x78, 0x17, 0x96, 0xC9, 0xC3, 0x2D, 0x0D, 0x32, 0xA5, 0xAB, 0xCD, 0x05,
    0x27, 0xE2, 0xF7, 0x10, 0xA3, 0x96, 0x13, 0xC4, 0x2F, 0x99, 0xC0, 0x27, 0xBF, 0xED, 0x04, 0x9C,
    0x3C, 0x27, 0x58, 0x04, 0xB6, 0xB2, 0x19, 0xF9, 0xC1, 0x2F, 0x02, 0xE9, 0x48, 0x63, 0xEC, 0xA1,
    0xB6, 0x42, 0xA0, 0x9D, 0x48, 0x25, 0xF8, 0xB3, 0x9D, 0xD0, 0xE8, 0x6A, 0xF9, 0x48, 0x4D, 0xA1,
    0xC2, 0xBA, 0x86, 0x30, 0x42, 0xEA, 0x9D, 0xB3, 0x08, 0x6C, 0x19, 0x0E, 0x48, 0xB3, 0x9D, 0x66,
    0xEB, 0x00, 0x06, 0xA2, 0x5A, 0xEE, 0xA1, 0x1B, 0x13, 0x87, 0x3C, 0xD7, 0x19, 0xE6, 0x55, 0xBD,
])
_DH_PRIME = int.from_bytes(bytes([
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xC9, 0x0F, 0xDA, 0xA2, 0x21, 0x68, 0xC2, 0x34,
    0xC4, 0xC6, 0x62, 0x8B, 0x80, 0xDC, 0x1C, 0xD1, 0x29, 0x02, 0x4E, 0x08, 0x8A, 0x67, 0xCC, 0x74,
    0x02, 0x0B, 0xBE, 0xA6, 0x3B, 0x13, 0x9B, 0x22, 0x51, 0x4A, 0x08, 0x79, 0x8E, 0x34, 0x04, 0xDD,
    0xEF, 0x95, 0x19, 0xB3, 0xCD, 0x3A, 0x43, 0x1B, 0x30, 0x2B, 0x0A, 0x6D, 0xF2, 0x5F, 0x14, 0x37,
    0x4F, 0xE1, 0x35, 0x6D, 0x6D, 0x51, 0xC2, 0x45, 0xE4, 0x85, 0xB5, 0x76, 0x62, 0x5E, 0x7E, 0xC6,
    0xF4, 0x4C, 0x42, 0xE9, 0xA6, 0x3A, 0x36, 0x20, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
]), "big")
_SHA1_DIGESTINFO = bytes([
    0x30, 0x21, 0x30, 0x09, 0x06, 0x05, 0x2B, 0x0E, 0x03, 0x02, 0x1A, 0x05, 0x00, 0x04, 0x14,
])

_RESTRICTIONS = {
    3: ("disallow_seeking_reasons", "repeat-string"),
    6: ("disallow_skipping_prev_reasons", "repeat-string"),
    7: ("disallow_skipping_next_reasons", "repeat-string"),
    8: ("disallow_toggling_repeat_context_reasons", "repeat-string"),
    9: ("disallow_toggling_repeat_track_reasons", "repeat-string"),
    10: ("disallow_toggling_shuffle_reasons", "repeat-string"),
    24: ("disallow_add_to_queue_reasons", "repeat-string"),
}
_OPTIONS = {
    1: ("shuffling_context", "bool"),
    2: ("repeating_context", "bool"),
    3: ("repeating_track", "bool"),
}
_TRACK = {
    1: ("uri", "string"),
    2: ("uid", "string"),
    3: ("metadata", "map", "string", "string"),
    6: ("provider", "string"),
    8: ("album_uri", "string"),
    10: ("artist_uri", "string"),
}
_PLAYER = {
    1: ("timestamp", "varint"),
    2: ("context_uri", "string"),
    7: ("track", "message", _TRACK),
    10: ("position_as_of_timestamp", "varint"),
    11: ("duration", "varint"),
    12: ("is_playing", "bool"),
    13: ("is_paused", "bool"),
    16: ("options", "message", _OPTIONS),
    17: ("restrictions", "message", _RESTRICTIONS),
    19: ("prev_tracks", "repeat-message", _TRACK),
    20: ("next_tracks", "repeat-message", _TRACK),
    21: ("context_metadata", "map", "string", "string"),
    24: ("queue_revision", "string"),
}
_DEVICE_INFO = {
    2: ("volume", "varint"),
    3: ("name", "string"),
    7: ("device_type", "varint"),
    10: ("device_id", "string"),
}
_CLUSTER = {
    2: ("active_device_id", "string"),
    3: ("player_state", "message", _PLAYER),
    4: ("device", "map", "string", "message", _DEVICE_INFO),
}
_CLUSTER_UPDATE = {1: ("cluster", "message", _CLUSTER)}
_IMAGE = {1: ("file_id", "bytes"), 3: ("width", "sint"), 4: ("height", "sint")}
_IMAGE_GROUP = {1: ("image", "repeat-message", _IMAGE)}
_ARTIST = {2: ("name", "string")}
_ALBUM = {
    2: ("name", "string"),
    3: ("artist", "repeat-message", _ARTIST),
    9: ("cover", "repeat-message", _IMAGE),
    11: ("disc", "repeat-message", {3: ("track", "repeat-message", {})}),
    17: ("cover_group", "message", _IMAGE_GROUP),
}
_META_TRACK = {
    2: ("name", "string"),
    3: ("album", "message", _ALBUM),
    4: ("artist", "repeat-message", _ARTIST),
    7: ("duration", "sint"),
    36: ("canonical_uri", "string"),
}
_ALBUM[11] = ("disc", "repeat-message", {1: ("number", "sint"), 3: ("track", "repeat-message", _META_TRACK)})
_TOP = {2: ("track", "repeat-message", _META_TRACK)}
_META_ARTIST = {2: ("name", "string"), 4: ("top_track", "repeat-message", _TOP), 11: ("portrait", "repeat-message", _IMAGE)}
_EPISODE = {2: ("name", "string"), 7: ("duration", "sint")}
_SHOW = {2: ("name", "string"), 66: ("publisher", "string"), 69: ("cover_image", "message", _IMAGE_GROUP), 70: ("episode", "repeat-message", _EPISODE)}
_LIST_ATTR = {
    1: ("name", "string"),
    2: ("description", "string"),
    3: ("picture", "bytes"),
    12: ("format", "repeat-message", {1: ("key", "string"), 2: ("value", "string")}),
    13: ("picture_size", "repeat-message", {1: ("target", "string"), 2: ("url", "string")}),
}
_ITEM = {1: ("uri", "string")}
_META_ITEM = {2: ("attributes", "message", _LIST_ATTR), 3: ("length", "varint"), 5: ("owner_username", "string")}
_LIST_ITEMS = {3: ("items", "repeat-message", _ITEM), 4: ("meta_items", "repeat-message", _META_ITEM)}
_SELECTED = {
    2: ("length", "varint"),
    3: ("attributes", "message", _LIST_ATTR),
    5: ("contents", "message", _LIST_ITEMS),
    16: ("owner_username", "string"),
}
_COLLECTION_ITEM = {1: ("uri", "string"), 3: ("is_removed", "bool")}
_PAGE = {1: ("items", "repeat-message", _COLLECTION_ITEM), 2: ("next_page_token", "string")}
_RECENT = {1: ("contexts", "repeat-message", {1: ("context", "string"), 3: ("track", "string")})}
_ANY = {1: ("type_url", "string"), 2: ("value", "bytes")}
_EXT_ENTITY = {2: ("entity_uri", "string"), 3: ("extension", "message", _ANY)}
_EXT_ARRAY = {3: ("extension_data", "repeat-message", _EXT_ENTITY)}
_EXTENDED = {2: ("extended_metadata", "repeat-message", _EXT_ARRAY)}
_TOKEN_RESPONSE = {
    1: ("response_type", "varint"),
    2: ("granted", "message", {1: ("token", "string"), 3: ("refresh_after_seconds", "varint")}),
    3: (
        "challenges",
        "message",
        {
            1: ("state", "string"),
            2: (
                "challenge",
                "repeat-message",
                {1: ("type", "varint"), 4: ("hashcash", "message", {1: ("length", "varint"), 2: ("prefix", "string")})},
            ),
        },
    ),
}
_SEARCH_ITEM = {
    1: ("uri", "string"),
    2: ("name", "string"),
    3: ("image", "string"),
    5: ("track", "message", {3: ("album", "message", {1: ("uri", "string"), 2: ("name", "string")}), 4: ("artists", "repeat-message", {1: ("uri", "string"), 2: ("name", "string")}), 18: ("duration", "message", {1: ("seconds", "varint")})}),
    6: ("album", "message", {1: ("artist_name", "string")}),
    14: ("section", "message", {2: ("entries", "repeat-message", {2: ("item", "message", {1: ("entity", "message", {})})})}),
}

Opener = Callable[..., Any]
log = logging.getLogger("bridgething.connect")


class SpotifyError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


class Connect:
    def __init__(
        self,
        opener: Opener | Callable[[], Opener],
        bearer: Callable[[], str | None],
        username: str | None = None,
        device_id: str | None = None,
        *,
        opener_factory: bool = False,
    ) -> None:
        self._opener = opener
        self._factory = opener_factory
        self._bearer = bearer
        self.username = username or ""
        self.device_id = device_id or ""
        self.manual = False
        self.active_device_id = ""
        self.saved: set[str] = set()
        self.saved_loaded = False
        self._saved_order: list[str] = []
        self._details: dict[str, dict[str, Any]] = {}
        self._detailing: set[str] = set()
        self._lists: dict[str, dict[str, Any]] = {}
        self._client_token = ""
        self._client_until = 0.0
        self._client_failed = 0.0
        self.on_identity: Callable[[], None] | None = None
        self._cluster: dict[str, Any] | None = None
        self._connection_id = ""
        self._spclient = ""
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _http(self) -> Opener:
        if self._factory:
            return self._opener()  # type: ignore[operator]
        return self._opener  # type: ignore[return-value]

    def ensure(self) -> None:
        if self.manual or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="spotify-connect", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def player(self, images: dict[str, str]) -> dict[str, Any]:
        self.ensure()
        with self._lock:
            cluster = self._cluster
            active = self.active_device_id
            details = dict(self._details)
        if not cluster:
            return _idle_player()
        state = cluster.get("player_state") or {}
        track = _provided(state.get("track") or {}, images)
        if not track.get("uri"):
            return _idle_player()
        uri = str(track["uri"])
        detail = details.get(uri)
        if detail:
            track = _overlay_detail(track, detail, images)
        if _sparse(track):
            self._schedule_detail(uri)
        else:
            with self._lock:
                self._details.setdefault(uri, _detail_from_provided(track))
        player = _playing_player(state, track, images)
        volume = _volume(cluster, active)
        if volume is not None:
            player["volume"] = volume
        return player

    def command(self, endpoint: str, target: str | None = None, extra: dict[str, Any] | None = None) -> None:
        device = target or self.active_device_id
        if not device:
            raise SpotifyError("notFound", "no active Spotify device")
        body = {"endpoint": endpoint}
        if extra:
            body.update(extra)
        self._request(
            "POST",
            f"/connect-state/v1/player/command/from/{self.device_id}/to/{device}",
            json_body={"command": body},
            connection=True,
        )

    def play(self, context_uri: str, skip_to: str | None = None) -> None:
        options: dict[str, Any] = {"license": "premium"}
        if skip_to:
            options["skip_to"] = {"track_uri": skip_to}
        self.command(
            "play",
            extra={
                "context": {"uri": context_uri, "url": f"context://{context_uri}", "metadata": {}},
                "play_origin": {"feature_identifier": "harmony", "feature_version": CLIENT_VERSION, "referrer_identifier": "home"},
                "prepare_play_options": options,
                "play_options": {"reason": "interactive", "operation": "replace", "trigger": "immediately"},
            },
        )

    def set_volume(self, target: str, percent: float) -> None:
        raw = max(0, min(65535, int(round(percent / 100 * 65535))))
        body = encode([
            (1, Keep(raw)),
            (3, [(1, [os.urandom(8).hex()])]),
            (4, "wlan"),
        ])
        self._request(
            "PUT",
            f"/connect-state/v1/connect/volume/from/{self.device_id}/to/{target}",
            payload=body,
            accept="application/protobuf",
            connection=True,
        )

    def rootlist(self) -> list[dict[str, Any]]:
        return self._rootlist_for(self._username())

    def _rootlist_for(self, user: str) -> list[dict[str, Any]]:
        path = f"/playlist/v2/user/{urllib.parse.quote(user)}/rootlist"
        query = {"decorate": "revision,attributes,length,owner,timestamp", "from": "0", "length": "500"}
        try:
            _status, body, _headers = self._request("GET", path, query=query, accept="application/x-protobuf")
        except SpotifyError as exc:
            log.warning("rootlist for %s failed: %s", user, exc.detail or exc.kind)
            return []
        if not body:
            log.warning("rootlist for %s returned an empty body", user)
            try:
                _status, body, _headers = self._request(
                    "GET",
                    path,
                    query=query,
                    accept="application/x-protobuf",
                    connection=True,
                )
            except SpotifyError as exc:
                log.warning("rootlist retry for %s failed: %s", user, exc.detail or exc.kind)
                return []
        cards = _cards_from_rootlist(body)
        if not cards:
            cards = [{"uri": uri, "name": "", "owner": "", "total": None, "image": None} for uri in _playlist_uris_in(body)]
        if not cards:
            log.warning("rootlist for %s returned no playlists", user)
        return cards

    def playlist_face(self, playlist_id: str) -> dict[str, Any]:
        """Name and cover from the playlist header. The rootlist omits pictures."""
        with self._lock:
            cached = self._lists.get(playlist_id)
        if cached is not None:
            first = next((str(uri) for uri in cached.get("uris") or [] if str(uri).startswith("spotify:track:")), None)
            return {
                "name": cached.get("name") or "",
                "image": cached.get("image"),
                "total": cached.get("total"),
                "first": first,
            }
        selected = self._proto(
            "GET",
            f"/playlist/v2/playlist/{playlist_id}",
            _SELECTED,
            query={"from": "0", "length": "1"},
        )
        attributes = selected.get("attributes") or {}
        length = selected.get("length")
        contents = selected.get("contents") or {}
        first = next(
            (
                str(item.get("uri"))
                for item in contents.get("items") or []
                if isinstance(item, dict) and str(item.get("uri") or "").startswith("spotify:track:")
            ),
            None,
        )
        return {
            "name": attributes.get("name") or "",
            "image": _picture(attributes),
            "total": length if isinstance(length, int) else None,
            "first": first,
        }

    def playlist_page(self, playlist_id: str, offset: int, limit: int) -> dict[str, Any]:
        with self._lock:
            cached = self._lists.get(playlist_id)
        if cached is None:
            selected = self._proto("GET", f"/playlist/v2/playlist/{playlist_id}", _SELECTED)
            contents = selected.get("contents") or {}
            uris = [str(item.get("uri") or "") for item in contents.get("items") or [] if item.get("uri")]
            if not uris:
                log.warning("playlist %s returned no tracks", playlist_id)
            attributes = selected.get("attributes") or {}
            length = selected.get("length")
            cached = {
                "name": attributes.get("name") or "",
                "owner": selected.get("owner_username") or "",
                "total": length if isinstance(length, int) else len(uris),
                "uris": uris,
                "image": _picture(attributes),
            }
            with self._lock:
                self._lists[playlist_id] = cached
        page = dict(cached)
        page["uris"] = list(cached["uris"][offset : offset + limit])
        return page

    def saved_uris(self, kind: str | None = None, need: int | None = None) -> list[str]:
        if kind in (None, "track") and self.saved_loaded and self._saved_order:
            return list(self._saved_order)
        user = self._username()
        found: list[str] = []
        token = ""
        complete = True
        while True:
            ask = 1000 if need is None else min(1000, max(50, need))
            page = self._proto(
                "POST",
                "/collection/v2/paging",
                _PAGE,
                payload=encode([(1, user), (2, "collection"), (3, token), (4, ask)]),
                content_type=COLLECTION_TYPE,
            )
            for item in page.get("items") or []:
                uri = str(item.get("uri") or "")
                item_kind = uri.split(":")[1] if uri.count(":") >= 2 else ""
                if item.get("is_removed") or not uri:
                    continue
                if kind is None or item_kind == kind:
                    found.append(uri)
                    if need is not None and len(found) >= need:
                        break
            if need is not None and len(found) >= need:
                complete = False
                break
            nxt = page.get("next_page_token") or ""
            if not nxt or nxt == token:
                break
            token = nxt
        if kind in (None, "track"):
            tracks = [uri for uri in found if uri.startswith("spotify:track:")]
            if complete:
                self.saved = set(tracks)
                self._saved_order = tracks
                self.saved_loaded = True
            else:
                self.saved.update(tracks)
        return found

    def collection_contains(self, uris: list[str]) -> list[bool]:
        """Whether each uri is in Liked Songs. One call, not a scan of the whole library."""
        wanted = [uri for uri in uris if uri.startswith("spotify:")]
        if not wanted:
            return [False for _uri in uris]
        user = self._username()
        items = [[(1, uri)] for uri in wanted]
        _status, body, _headers = self._request(
            "POST",
            "/collection/v2/contains",
            payload=encode([(1, user), (2, "collection"), (3, items)]),
            content_type=COLLECTION_TYPE,
            accept=COLLECTION_TYPE,
        )
        flags = _packed_bools(body, 1)
        found = {uri: flags[index] if index < len(flags) else False for index, uri in enumerate(wanted)}
        return [found.get(uri, False) for uri in uris]

    def write_saved(self, uri: str, liked: bool) -> None:
        user = self._username()
        was_loaded = self.saved_loaded
        item = [(1, uri), (3, True)] if not liked else [(1, uri), (2, int(time.time()))]
        self._request(
            "POST",
            "/collection/v2/write",
            payload=encode([(1, user), (2, "collection"), (3, [item])]),
            content_type=COLLECTION_TYPE,
            accept=COLLECTION_TYPE,
        )
        if liked:
            self.saved.add(uri)
            if was_loaded and uri not in self._saved_order:
                self._saved_order.insert(0, uri)
        else:
            self.saved.discard(uri)
            if was_loaded:
                self._saved_order = [item for item in self._saved_order if item != uri]
        self.saved_loaded = was_loaded

    def recent_uris(self, limit: int) -> list[str]:
        user = self._username()
        page = self._proto(
            "GET",
            f"/recently-played/v3/user/{urllib.parse.quote(user)}/recently-played",
            _RECENT,
            query={"limit": str(limit), "filter": "default,collection-new-episodes"},
        )
        found = []
        for item in page.get("contexts") or []:
            track = str(item.get("track") or "")
            context = str(item.get("context") or "")
            uri = track or (context if context.startswith("spotify:track:") else "")
            if uri:
                found.append(uri)
        return found

    def track(self, uri: str) -> dict[str, Any]:
        found = self.tracks([uri])
        item = found.get(uri)
        if not item:
            raise SpotifyError("notFound", uri)
        return item

    def tracks(self, uris: list[str]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for uri, parsed in self._extension([uri for uri in uris if uri.startswith("spotify:track:")], _TRACK_V4, _META_TRACK).items():
            item = _meta_track(uri, parsed)
            found[uri] = item
            canonical = str(item.get("uri") or "")
            if canonical:
                found[canonical] = item
        return found

    def album(self, uri: str) -> dict[str, Any]:
        parsed = self._extension([uri], _ALBUM_V4, _ALBUM).get(uri) or {}
        tracks = []
        for disc in parsed.get("disc") or []:
            tracks.extend(_meta_track(item.get("canonical_uri") or "", item) for item in disc.get("track") or [])
        return {"name": parsed.get("name") or "", "image": _image_id(parsed.get("cover") or (parsed.get("cover_group") or {}).get("image")), "tracks": tracks}

    def artist(self, uri: str) -> dict[str, Any]:
        parsed = self._extension([uri], _ARTIST_V4, _META_ARTIST).get(uri) or {}
        return {"name": parsed.get("name") or "", "image": _image_id(parsed.get("portrait"))}

    def artist_top(self, uri: str) -> list[dict[str, Any]]:
        parsed = self._extension([uri], _ARTIST_V4, _META_ARTIST).get(uri) or {}
        groups = parsed.get("top_track") or []
        tracks = groups[0].get("track") if groups else []
        return [_meta_track(item.get("canonical_uri") or "", item) for item in tracks or []]

    def show(self, uri: str) -> dict[str, Any]:
        parsed = self._extension([uri], _SHOW_V4, _SHOW).get(uri) or {}
        parsed["image"] = _image_id(parsed.get("cover_image"))
        return parsed

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        parsed = self._proto(
            "GET",
            "/searchview/v3/search",
            {1: ("items", "repeat-message", _SEARCH_ITEM)},
            query={
                "request_id": os.urandom(8).hex(),
                "query": query,
                "locale": "en_US",
                "entity_types": "album,artist,playlist,track,show,audio_episode",
                "timestamp": str(int(time.time() * 1000)),
                "limit": str(limit),
                "page_token": "",
            },
        )
        return list(parsed.get("items") or [])[:limit]

    def ingest(self, payload: bytes) -> None:
        update = message(payload, _CLUSTER_UPDATE)
        cluster = update.get("cluster")
        if isinstance(cluster, dict):
            self._remember(cluster)

    def _remember(self, cluster: dict[str, Any]) -> None:
        with self._lock:
            if self._cluster:
                cluster = _merge_cluster(self._cluster, cluster)
            self._cluster = cluster
            active = str(cluster.get("active_device_id") or "")
            if active:
                self.active_device_id = active

    def _schedule_detail(self, uri: str) -> None:
        if self.manual or not uri or uri in self._details or uri in self._detailing:
            return
        self._detailing.add(uri)
        threading.Thread(target=self._fill_detail, args=(uri,), name="spotify-track", daemon=True).start()

    def _fill_detail(self, uri: str) -> None:
        detail = None
        try:
            detail = self.track(uri)
        except SpotifyError as exc:
            log.info("track details unavailable for %s: %s", uri, exc.detail or exc.kind)
        if detail:
            with self._lock:
                self._details[uri] = detail
        self._detailing.discard(uri)

    def _username(self) -> str:
        if self.username:
            return self.username
        token = self._bearer()
        if not token:
            raise SpotifyError("unauthorized")
        if self.manual:
            try:
                self.username = _profile_username(self._http(), token)
            except SpotifyError:
                raise SpotifyError("unauthorized", "spotify username is not known yet")
            self._identity()
            return self.username
        if not self.device_id:
            self.device_id = os.urandom(20).hex()
            self._identity()
        try:
            self.username = _ap_username(self._http(), token, self.device_id)
        except Exception as exc:
            log.warning("spotify access point login failed: %s", exc)
        if not self.username:
            self.username = _profile_username(self._http(), token)
        self._identity()
        return self.username

    def _run(self) -> None:
        delay = 1
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1
            except Exception:
                log.warning("spotify connect dropped", exc_info=True)
                if self._stop.wait(delay):
                    return
                delay = min(delay * 2, 30)

    def _session(self) -> None:
        token = self._bearer()
        if not token:
            raise SpotifyError("unauthorized")
        if not self.device_id:
            self.device_id = os.urandom(20).hex()
            self._identity()
        if not self.username:
            try:
                self.username = _ap_username(self._http(), token, self.device_id)
            except Exception as exc:
                log.warning("spotify username unavailable: %s", exc)
            if not self.username:
                try:
                    self.username = _profile_username(self._http(), token)
                except SpotifyError as exc:
                    log.warning("spotify profile unavailable: %s", exc.detail or exc.kind)
            if self.username:
                self._identity()
        host = _resolve(self._http(), "dealer")
        ws = open_websocket(host, 443, timeout=20, tls=True, path="/?access_token=" + urllib.parse.quote(token, safe=""))
        try:
            opcode, payload = ws.recv()
            if opcode != 1:
                raise SpotifyError("notSupported", "dealer hello was not text")
            hello = json.loads(payload.decode("utf-8"))
            self._connection_id = str(((hello.get("headers") or {}).get("Spotify-Connection-Id")) or "")
            if not self._connection_id:
                raise SpotifyError("notSupported", "dealer did not send a connection id")
            self._register()
            while not self._stop.is_set():
                try:
                    opcode, payload = ws.recv()
                except TimeoutError:
                    ws.send_text('{"type":"ping"}')
                    continue
                if opcode != 1:
                    continue
                self._on_frame(ws, payload.decode("utf-8", "replace"))
        finally:
            ws.close()

    def _on_frame(self, ws: Any, text: str) -> None:
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            return
        kind = frame.get("type")
        if kind == "ping":
            ws.send_text('{"type":"pong"}')
            return
        if kind == "request" and frame.get("key"):
            ws.send_text(json.dumps({"type": "reply", "key": frame["key"], "payload": {"success": True}}))
            return
        if kind != "message":
            return
        uri = str(frame.get("uri") or "")
        if uri.startswith("hm://collection/"):
            self.saved.clear()
            self.saved_loaded = False
            return
        if "connect-state/v1/cluster" not in uri:
            return
        gzipped = (frame.get("headers") or {}).get("Transfer-Encoding") == "gzip"
        for encoded in frame.get("payloads") or []:
            if not isinstance(encoded, str):
                continue
            try:
                raw = base64.b64decode(encoded)
                if gzipped:
                    raw = gzip.decompress(raw)
                self.ingest(raw)
                return
            except (ValueError, OSError, EOFError):
                continue

    def _register(self) -> None:
        capabilities = [
            (7, True),
            (9, ["audio/track"]),
            (12, True),
            (22, True),
        ]
        info = [
            (3, "Home Assistant"),
            (4, capabilities),
            (6, "bridgething-sfp/0.1"),
            (7, 102),
            (10, self.device_id),
            (13, ANDROID_CLIENT_ID),
        ]
        body = encode([
            (2, [(1, info)]),
            (3, 2),
            (5, 3),
            (12, int(time.time() * 1000)),
        ])
        self._request("PUT", f"/connect-state/v1/devices/{self.device_id}", payload=body, accept="application/protobuf", connection=True)

    def _identity(self) -> None:
        if self.on_identity is not None:
            self.on_identity()

    def _extension(self, uris: list[str], kind: int, schema: dict[int, tuple]) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        unique = [uri for uri in dict.fromkeys(uris) if uri]
        for start in range(0, len(unique), 20):
            chunk = unique[start : start + 20]
            body = encode([(2, [[(1, uri), (2, [[(1, kind)]])] for uri in chunk])])
            parsed = self._proto(
                "POST",
                "/extended-metadata/v0/extended-metadata",
                _EXTENDED,
                payload=body,
                content_type="application/x-protobuf",
            )
            for group in parsed.get("extended_metadata") or []:
                for entry in group.get("extension_data") or []:
                    raw = (entry.get("extension") or {}).get("value")
                    uri = str(entry.get("entity_uri") or "")
                    if not isinstance(raw, (bytes, bytearray)) or not raw:
                        continue
                    try:
                        found[uri] = message(raw, schema)
                    except ValueError:
                        log.warning("extended metadata for %s was not readable", uri)
        if unique and not found:
            log.warning("extended metadata returned nothing for %s", ", ".join(unique[:3]))
        return found

    def _proto(self, method: str, path: str, schema: dict[int, tuple], query: dict[str, str] | None = None, payload: bytes | None = None, content_type: str | None = None) -> dict[str, Any]:
        accept = content_type or "application/x-protobuf"
        _status, body, _headers = self._request(
            method,
            path,
            query=query,
            payload=payload,
            accept=accept,
            content_type=content_type or ("application/x-protobuf" if payload else None),
        )
        if not body:
            log.warning("spotify %s %s returned an empty body", method, path)
            return {}
        try:
            return message(body, schema)
        except ValueError as exc:
            raise SpotifyError("notSupported", f"{path} was not a protobuf") from exc

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: bytes | None = None,
        json_body: dict[str, Any] | None = None,
        accept: str | None = None,
        content_type: str | None = None,
        connection: bool = False,
    ) -> tuple[int, bytes, Any]:
        token = self._bearer()
        if not token:
            raise SpotifyError("unauthorized")
        url = self._endpoint(path)
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else payload
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"Spotify/{CLIENT_VERSION} Linux/Bridgething",
        }
        if connection:
            headers["App-Platform"] = "Android"
            headers["Spotify-App-Version"] = CLIENT_VERSION
            if self._connection_id:
                headers["X-Spotify-Connection-Id"] = self._connection_id
        client = self._client_header()
        if client:
            headers["Client-Token"] = client
        if accept:
            headers["Accept"] = accept
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._http()(request, timeout=20) as response:
                body = response.read()
                status = getattr(response, "status", 200)
                response_headers = getattr(response, "headers", {})
                body = _ungzip(body, response_headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise SpotifyError("unauthorized", detail[:180]) from exc
            if exc.code == 404:
                raise SpotifyError("notFound", detail[:180]) from exc
            raise SpotifyError("notSupported", detail[:180] or str(exc.code)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise SpotifyError("notSupported", str(exc)) from exc
        return status, body, response_headers

    def _endpoint(self, path: str) -> str:
        if not self._spclient:
            if self.manual:
                self._spclient = SPCLIENT_FALLBACK
            else:
                try:
                    self._spclient = "https://" + _resolve(self._http(), "spclient")
                except SpotifyError:
                    self._spclient = SPCLIENT_FALLBACK
        return self._spclient + path

    def _client_header(self) -> str:
        if self.manual:
            return ""
        now = time.time()
        if self._client_token and now < self._client_until:
            return self._client_token
        if now < self._client_failed:
            return ""
        if not self.device_id:
            self.device_id = os.urandom(20).hex()
        try:
            token, ttl = _mint_client_token(self._http(), self.device_id)
        except SpotifyError as exc:
            log.warning("spotify client token unavailable: %s", exc.detail or exc.kind)
            self._client_failed = now + 60
            return ""
        self._client_token = token
        self._client_until = now + ttl
        return token


def _idle_player() -> dict[str, Any]:
    from .playback import idle_playback

    return {
        "track": None,
        "playback": idle_playback(),
        "queue": [],
        "options": {"speed": 1.0},
        "origin": "spotify",
        "picture": None,
    }


def _playing_player(state: dict[str, Any], track: dict[str, Any], images: dict[str, str]) -> dict[str, Any]:
    options = state.get("options") or {}
    restrictions = state.get("restrictions") or {}
    repeat = "one" if options.get("repeating_track") else "all" if options.get("repeating_context") else "off"
    shuffled = bool(options.get("shuffling_context"))
    context_uri = str(state.get("context_uri") or "")
    context_name = ((state.get("context_metadata") or {}).get("context_description")) or track.get("album") or ""
    queue = [_queue_from_provided(item, images) for item in [state.get("track") or {}, *(state.get("next_tracks") or [])]]
    player = {
        "track": {
            "uri": track["uri"],
            "persistentId": track["uri"],
            "title": track.get("name") or "",
            "artist": track.get("artist") or "",
            "artistUri": track.get("artist_uri") or "",
            "album": track.get("album") or "",
            "albumUri": track.get("album_uri") or "",
            "durationMs": _duration(track, state),
            "artworkId": track.get("artwork_id"),
            "liked": bool(track.get("saved")),
            "isLikeSupported": True,
            "mediaTypes": ["music"],
        },
        "playback": {
            "state": "playing" if state.get("is_playing") and not state.get("is_paused") else "paused",
            "positionMs": _position(state, _duration(track, state)),
            "positionAgeMs": 0,
            "shuffle": shuffled,
            "shuffleMode": "songs" if shuffled else "off",
            "repeat": repeat,
            "setElapsedTimeAvailable": not restrictions.get("disallow_seeking_reasons"),
        },
        "queue": [item for item in queue if item.get("uri")],
        "options": {"speed": 1.0},
        "picture": track.get("picture"),
        "origin": "spotify",
    }
    player["track"] = {key: value for key, value in player["track"].items() if value is not None}
    if context_uri:
        player["context"] = {"uri": context_uri, "name": context_name}
    return player


def _duration(track: dict[str, Any], state: dict[str, Any]) -> int | None:
    for value in (track.get("duration_ms"), state.get("duration")):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _detail_from_provided(track: dict[str, Any]) -> dict[str, Any]:
    picture = track.get("picture")
    return {
        "name": track.get("name") or "",
        "duration_ms": track.get("duration_ms") or 0,
        "artists": [{"name": track.get("artist") or ""}],
        "album": {
            "name": track.get("album") or "",
            "images": [{"url": picture}] if isinstance(picture, str) and picture else [],
        },
    }


def _sparse(track: dict[str, Any]) -> bool:
    return not track.get("name") or not track.get("artwork_id") or not track.get("duration_ms")


def _overlay_detail(track: dict[str, Any], detail: dict[str, Any], images: dict[str, str]) -> dict[str, Any]:
    album = detail.get("album") if isinstance(detail.get("album"), dict) else {}
    if not track.get("name"):
        track["name"] = detail.get("name") or ""
    if not track.get("artist"):
        names = [artist.get("name") for artist in detail.get("artists") or [] if isinstance(artist, dict) and artist.get("name")]
        track["artist"] = ", ".join(str(name) for name in names if name)
    if not track.get("album"):
        track["album"] = album.get("name") or ""
    if not track.get("duration_ms"):
        duration = detail.get("duration_ms") or 0
        if isinstance(duration, int) and duration > 0:
            track["duration_ms"] = duration
    if not track.get("picture"):
        pictures = album.get("images") if isinstance(album.get("images"), list) else []
        url = pictures[0].get("url") if pictures and isinstance(pictures[0], dict) else None
        if isinstance(url, str) and url:
            picture, artwork = _remember_image(url, images)
            track["picture"] = picture
            track["artwork_id"] = artwork
    return track


def _merge_cluster(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """A later cluster often carries only the playhead. Keep the title, cover, and duration already seen."""
    merged = dict(current)
    old_state = previous.get("player_state") if isinstance(previous.get("player_state"), dict) else {}
    new_state = dict(current.get("player_state") or {})
    old_track = old_state.get("track") if isinstance(old_state.get("track"), dict) else {}
    new_track = dict(new_state.get("track") or {})
    old_uri = str(old_track.get("uri") or "")
    new_uri = str(new_track.get("uri") or "")
    if new_uri and new_uri == old_uri:
        old_meta = old_track.get("metadata") if isinstance(old_track.get("metadata"), dict) else {}
        new_meta = new_track.get("metadata") if isinstance(new_track.get("metadata"), dict) else {}
        meta = dict(old_meta)
        meta.update({key: value for key, value in new_meta.items() if value})
        if meta:
            new_track["metadata"] = meta
        if not new_state.get("duration") and old_state.get("duration"):
            new_state["duration"] = old_state.get("duration")
        new_state["track"] = new_track
    if not merged.get("device") and previous.get("device"):
        merged["device"] = previous.get("device")
    if not merged.get("active_device_id"):
        merged["active_device_id"] = previous.get("active_device_id") or ""
    merged["player_state"] = new_state
    return merged


def _position(state: dict[str, Any], limit: int | None = None) -> int:
    base = int(state.get("position_as_of_timestamp") or 0)
    duration = int(state.get("duration") or 0)
    cap = duration if duration > 0 else (limit or 0)
    timestamp = int(state.get("timestamp") or 0)
    if state.get("is_playing") and not state.get("is_paused") and timestamp:
        elapsed = max(0, int(time.time() * 1000) - timestamp)
        live = base + elapsed
        return min(live, cap) if cap else live
    return min(base, cap) if cap else base


def _provided(track: dict[str, Any], images: dict[str, str]) -> dict[str, Any]:
    meta = track.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    names = [meta.get("artist_name")] if meta.get("artist_name") else []
    index = 1
    while meta.get(f"artist_name:{index}"):
        names.append(meta.get(f"artist_name:{index}"))
        index += 1
    picture, artwork = _cdn(meta, images)
    duration = meta.get("duration")
    return {
        "uri": track.get("uri") or "",
        "name": meta.get("title") or "",
        "artist": ", ".join(str(name) for name in names if name),
        "artist_uri": meta.get("artist_uri") or track.get("artist_uri") or "",
        "album": meta.get("album_title") or "",
        "album_uri": track.get("album_uri") or meta.get("album_uri") or "",
        "duration_ms": int(duration) if isinstance(duration, str) and duration.isdigit() else None,
        "artwork_id": artwork,
        "picture": picture,
        "saved": meta.get("collection.in_collection") == "true",
    }


def _queue_from_provided(track: dict[str, Any], images: dict[str, str]) -> dict[str, Any]:
    item = _provided(track, images)
    entry = {
        "uri": item.get("uri") or "",
        "persistentId": item.get("uri") or "",
        "title": item.get("name") or "",
        "artist": item.get("artist") or "",
        "artistUri": item.get("artist_uri") or "",
        "album": item.get("album") or "",
        "albumUri": item.get("album_uri") or "",
        "artworkId": item.get("artwork_id"),
        "durationMs": item.get("duration_ms"),
        "queued": track.get("provider") == "queue",
    }
    return {key: value for key, value in entry.items() if value is not None}


def _cdn(meta: dict[str, Any], images: dict[str, str]) -> tuple[str | None, str | None]:
    for key in ("image_xlarge_url", "image_large_url", "image_url", "image_small_url"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return _remember_image(value, images)
    return None, None


def _ungzip(body: bytes, headers: Any) -> bytes:
    encoding = ""
    if hasattr(headers, "get"):
        encoding = str(headers.get("Content-Encoding") or "")
    if "gzip" in encoding.lower() or body[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(body)
        except OSError:
            return body
    return body


def _mint_client_token(opener: Opener, device_id: str) -> tuple[str, int]:
    linux = [(1, "Linux"), (2, "6.6.0"), (3, "bridgething"), (4, "x86_64")]
    body = encode([(1, 1), (2, [(1, CLIENT_TOKEN_VERSION), (2, DESKTOP_CLIENT_ID), (3, [(1, [(5, linux)]), (2, device_id)])])])
    parsed = _client_token_call(opener, body)
    if parsed.get("response_type") == 2:
        parsed = _client_token_call(opener, _hashcash_answer(parsed.get("challenges") or {}))
    granted = parsed.get("granted") or {}
    token = granted.get("token")
    if not isinstance(token, str) or not token:
        raise SpotifyError("unauthorized", "spotify did not grant a client token")
    refresh = granted.get("refresh_after_seconds")
    return token, int(refresh) if isinstance(refresh, int) and refresh > 0 else 7200


def _client_token_call(opener: Opener, body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://clienttoken.spotify.com/v1/clienttoken",
        data=body,
        method="POST",
        headers={"Accept": "application/x-protobuf", "Content-Type": "application/x-protobuf"},
    )
    try:
        with opener(request, timeout=15) as response:
            payload = _ungzip(response.read(), getattr(response, "headers", {}))
    except urllib.error.HTTPError as exc:
        raise SpotifyError("unauthorized", exc.read().decode("utf-8", "replace")[:180]) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise SpotifyError("notSupported", str(exc)) from exc
    try:
        return message(payload, _TOKEN_RESPONSE)
    except ValueError as exc:
        raise SpotifyError("notSupported", "client token response was not a protobuf") from exc


def _hashcash_answer(challenges: dict[str, Any]) -> bytes:
    state = str(challenges.get("state") or "")
    for challenge in challenges.get("challenge") or []:
        if challenge.get("type") != 3:
            continue
        params = challenge.get("hashcash") or {}
        try:
            prefix = bytes.fromhex(str(params.get("prefix") or ""))
        except ValueError as exc:
            raise SpotifyError("notSupported", "spotify client token challenge was not hex") from exc
        suffix = _solve_hashcash(prefix, int(params.get("length") or 0))
        answer = [(1, 3), (4, [(1, suffix)])]
        return encode([(1, 2), (3, [(1, state), (2, [answer])])])
    raise SpotifyError("notSupported", "spotify client token challenge is not hashcash")


def _solve_hashcash(prefix: bytes, bits: int) -> str:
    width = max(0, min(bits, 20))
    suffix = bytearray(16)
    for _ in range(1 << min(width + 4, 22)):
        if _leading_zeros(hashlib.sha1(prefix + suffix).digest()) >= width:
            return suffix.hex().upper()
        for index in range(15, -1, -1):
            suffix[index] = (suffix[index] + 1) & 0xFF
            if suffix[index]:
                break
    raise SpotifyError("notSupported", "spotify client token challenge was not solved")


def _leading_zeros(digest: bytes) -> int:
    count = 0
    for byte in digest:
        if byte == 0:
            count += 8
            continue
        count += 8 - byte.bit_length()
        break
    return count


def _remember_image(url: str, images: dict[str, str]) -> tuple[str | None, str | None]:
    if url.startswith("spotify:image:"):
        url = "https://i.scdn.co/image/" + url.rsplit(":", 1)[-1]
    if not url.startswith("http"):
        file_id = url.strip()
        if not file_id or "/" in file_id or " " in file_id:
            return None, None
        url = "https://i.scdn.co/image/" + file_id
    key = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if not key:
        return None, None
    images[key] = url
    return url, f"spotify/img/300/{key}"


def _volume(cluster: dict[str, Any], active: str) -> dict[str, Any] | None:
    info = (cluster.get("device") or {}).get(active) if active else None
    if not isinstance(info, dict) or not isinstance(info.get("volume"), int):
        return None
    level = max(0.0, min(1.0, info["volume"] / 65535))
    return {"level": level, "muted": level == 0}


def _picture(attributes: dict[str, Any]) -> str | None:
    sizes = attributes.get("picture_size") or []
    for size in sizes:
        url = size.get("url") if isinstance(size, dict) else None
        if isinstance(url, str) and url:
            return url
    picture = attributes.get("picture")
    if isinstance(picture, (bytes, bytearray)) and picture:
        return "https://i.scdn.co/image/" + bytes(picture).hex()
    for item in attributes.get("format") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value:
            continue
        if value.startswith("http"):
            return value
        if len(value) == 40 and all(char in "0123456789abcdefABCDEF" for char in value):
            return "https://i.scdn.co/image/" + value.lower()
    return None


def _packed_bools(payload: bytes, field: int) -> list[bool]:
    found: list[bool] = []
    if not payload:
        return found
    for number, wire, value in decode(payload):
        if number != field:
            continue
        if wire == 0:
            found.append(bool(value))
        elif wire == 2 and isinstance(value, (bytes, bytearray)):
            offset = 0
            raw = bytes(value)
            while offset < len(raw):
                bit, offset = _varint_at(raw, offset)
                found.append(bool(bit))
    return found


def _varint_at(payload: bytes, offset: int) -> tuple[int, int]:
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


def _image_id(images: Any) -> str | None:
    if isinstance(images, dict):
        images = images.get("image")
    if not isinstance(images, list):
        return None
    best = None
    best_width = -1
    for image in images:
        file_id = image.get("file_id") if isinstance(image, dict) else None
        if not isinstance(file_id, (bytes, bytearray)) or not file_id:
            continue
        width = image.get("width") if isinstance(image.get("width"), int) else 0
        if best is None or width > best_width:
            best = bytes(file_id).hex()
            best_width = width
    return "https://i.scdn.co/image/" + best if best else None


def _meta_track(uri: str, parsed: dict[str, Any]) -> dict[str, Any]:
    album = parsed.get("album") if isinstance(parsed.get("album"), dict) else {}
    artists = parsed.get("artist") or album.get("artist") or []
    image = _image_id(album.get("cover")) or _image_id(album.get("cover_group"))
    return {
        "uri": parsed.get("canonical_uri") or uri,
        "name": parsed.get("name") or "",
        "duration_ms": parsed.get("duration") or 0,
        "artists": [{"name": artist.get("name") or "", "uri": ""} for artist in artists if isinstance(artist, dict)],
        "album": {"name": album.get("name") or "", "uri": "", "images": [{"url": image}] if image else []},
    }


def _profile_username(opener: Opener, token: str) -> str:
    request = urllib.request.Request(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with opener(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError) as exc:
        raise SpotifyError("unauthorized", str(exc)) from exc
    name = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name:
        raise SpotifyError("unauthorized", "spotify did not return a username")
    return name


def _cards_from_rootlist(body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        selected = message(body, _SELECTED)
    except ValueError:
        return []
    contents = selected.get("contents") or {}
    items = contents.get("items") or []
    metas = contents.get("meta_items") or []
    cards = []
    for index, item in enumerate(items):
        uri = str(item.get("uri") or "")
        if ":playlist:" not in uri:
            continue
        meta = metas[index] if index < len(metas) else {}
        attributes = meta.get("attributes") or {}
        cards.append(
            {
                "uri": uri,
                "name": attributes.get("name") or "",
                "owner": meta.get("owner_username") or "",
                "total": meta.get("length"),
                "image": _picture(attributes),
            }
        )
    return cards


def _playlist_uris_in(body: bytes) -> list[str]:
    found: list[str] = []
    for match in _PLAYLIST_URI.finditer(body or b""):
        uri = match.group().decode("ascii")
        if uri not in found:
            found.append(uri)
    return found


def _resolve(opener: Opener, kind: str) -> str:
    request = urllib.request.Request(f"https://apresolve.spotify.com/?type={kind}")
    try:
        with opener(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError) as exc:
        raise SpotifyError("notSupported", str(exc)) from exc
    values = payload.get(kind) if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values or not isinstance(values[0], str):
        raise SpotifyError("notSupported", f"no {kind} host")
    return values[0].split(":", 1)[0]


def _ap_username(opener: Opener, token: str, device_id: str) -> str:
    return str(_ap_login(opener, token, device_id)["username"])


def _ap_login(
    opener: Opener,
    token: str,
    device_id: str,
    username: str = "",
    auth_data: bytes = b"",
    auth_type: int = 3,
) -> dict[str, Any]:
    """Log in at the access point and keep the reusable credential from the welcome."""
    try:
        host, port = _resolve_accesspoint(opener)
        sock = socket.create_connection((host, port), timeout=10)
    except SpotifyError:
        raise
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeError) as exc:
        raise SpotifyError("notSupported", str(exc)) from exc
    try:
        return _ap_handshake(sock, token, device_id, username, auth_data, auth_type)
    finally:
        sock.close()


def _resolve_accesspoint(opener: Opener) -> tuple[str, int]:
    request = urllib.request.Request("https://apresolve.spotify.com/?type=accesspoint")
    with opener(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    entry = (payload.get("accesspoint") or [""])[0]
    host, _, port = str(entry).rpartition(":")
    if not host or not port.isdigit():
        raise SpotifyError("notSupported", "no access point")
    return host, int(port)


def _ap_handshake(
    sock: socket.socket,
    token: str,
    device_id: str,
    username: str = "",
    auth_data: bytes = b"",
    auth_type: int = 3,
) -> dict[str, Any]:
    private = int.from_bytes(os.urandom(95), "little")
    public = pow(2, private, _DH_PRIME).to_bytes((pow(2, private, _DH_PRIME).bit_length() + 7) // 8, "big")
    hello = encode([
        (0x0A, [(0x0A, Keep(0)), (0x14, [Keep(0)]), (0x1E, Keep(8)), (0x28, 124200290)]),
        (0x1E, [Keep(0)]),
        (0x32, [(0x0A, [(0x0A, public), (0x14, Keep(1))])]),
        (0x3C, os.urandom(16)),
        (0x46, bytes([0x1E])),
    ])
    acc = bytearray(b"\x00\x04")
    acc += (2 + 4 + len(hello)).to_bytes(4, "big")
    acc += hello
    sock.sendall(acc)
    length = int.from_bytes(_read_exact(sock, 4), "big")
    body = _read_exact(sock, length - 4)
    acc += length.to_bytes(4, "big") + body
    remote, signature = _access_point_keys(body)
    if not remote or not signature:
        raise SpotifyError("unauthorized", "spotify access point did not send a signature")
    if not _verify_server(remote, signature):
        raise SpotifyError("unauthorized", "spotify access point signature did not match")
    shared = pow(int.from_bytes(remote, "big"), private, _DH_PRIME)
    shared_bytes = shared.to_bytes((shared.bit_length() + 7) // 8 or 1, "big")
    mac, send_key, recv_key = _keys(shared_bytes, bytes(acc))
    response = encode([
        (0x0A, [(0x0A, [(0x0A, mac)])]),
        (0x14, [Keep([])]),
        (0x1E, [Keep([])]),
    ])
    packet = (4 + len(response)).to_bytes(4, "big") + response
    sock.sendall(packet)
    send = Shannon(send_key)
    recv = Shannon(recv_key)
    material = auth_data if auth_data else token.encode("utf-8")
    credentials: list[tuple[int, Any]] = [(0x14, Keep(auth_type)), (0x1E, material)]
    if username:
        credentials.insert(0, (0x0A, username))
    login = encode([
        (0x0A, credentials),
        (0x32, [(0x0A, Keep(2)), (0x3C, Keep(5)), (0x5A, "bridgething-sfp"), (0x64, device_id)]),
        (0x46, "bridgething-sfp 0.1"),
    ])
    _send_packet(sock, send, 0xAB, login)
    command, payload = _recv_packet(sock, recv)
    if command == 0xAC:
        return _read_welcome(payload)
    raise SpotifyError("unauthorized", f"spotify access point rejected the token ({command:#x})")


def _read_welcome(payload: bytes) -> dict[str, Any]:
    """Username plus the reusable credential Spotify expects the client to keep."""
    welcome = message(
        payload,
        {
            0x0A: ("canonical_username", "string"),
            0x1E: ("reusable_auth_credentials_type", "varint"),
            0x28: ("reusable_auth_credentials", "bytes"),
        },
    )
    name = welcome.get("canonical_username")
    if not isinstance(name, str) or not name:
        raise SpotifyError("unauthorized", "spotify did not return a username")
    blob = welcome.get("reusable_auth_credentials") or b""
    kind = welcome.get("reusable_auth_credentials_type")
    return {
        "username": name,
        "auth_data": bytes(blob) if isinstance(blob, (bytes, bytearray)) else b"",
        "auth_type": kind if isinstance(kind, int) else 1,
    }


_LOGIN5_OK = {
    1: ("username", "string"),
    2: ("access_token", "string"),
    3: ("stored_credential", "bytes"),
    4: ("expires_in", "varint"),
}
_LOGIN5_RESPONSE = {
    1: ("ok", "message", _LOGIN5_OK),
    2: ("error", "varint"),
    3: (
        "challenges",
        "message",
        {
            1: (
                "challenge",
                "repeat-message",
                {1: ("hashcash", "message", {1: ("prefix", "bytes"), 2: ("length", "varint")}), 2: ("code", "message", {})},
            )
        },
    ),
    5: ("login_context", "bytes"),
}
_LOGIN5_URL = "https://login5.spotify.com/v3/login"


def mint_access_token(opener: Opener, client_token: str, device_id: str, username: str, auth_data: bytes) -> dict[str, Any]:
    """Mint a Web API access token from the reusable credential. The OAuth refresh token is not used."""
    if not client_token:
        raise SpotifyError("notSupported", "spotify client token unavailable")
    if not username or not auth_data:
        raise SpotifyError("unauthorized", "spotify reusable login is missing")
    context = b""
    solutions: list[Any] | None = None
    for _ in range(3):
        fields: list[tuple[int, Any]] = [
            (1, [(1, DESKTOP_CLIENT_ID), (2, device_id)]),
            (100, [(1, username), (2, auth_data)]),
        ]
        if context:
            fields.append((2, context))
        if solutions:
            fields.append((3, [(1, solutions)]))
        parsed = _login5_call(opener, client_token, encode(fields))
        ok = parsed.get("ok") if isinstance(parsed.get("ok"), dict) else {}
        token = ok.get("access_token")
        if isinstance(token, str) and token:
            blob = ok.get("stored_credential")
            expires = ok.get("expires_in")
            return {
                "username": ok.get("username") or username,
                "access_token": token,
                "auth_data": bytes(blob) if isinstance(blob, (bytes, bytearray)) and blob else auth_data,
                "expires_in": int(expires) if isinstance(expires, int) and expires > 0 else 3600,
            }
        error = parsed.get("error")
        if isinstance(error, int) and not ok:
            if error in (1, 2, 5):
                raise SpotifyError("unauthorized", f"spotify stored login was rejected ({error})")
            raise SpotifyError("notSupported", f"spotify stored login failed ({error})")
        challenges = (parsed.get("challenges") or {}).get("challenge") or []
        raw_context = parsed.get("login_context") or b""
        context = bytes(raw_context) if isinstance(raw_context, (bytes, bytearray)) else b""
        solutions = _login5_solutions(challenges, context)
    raise SpotifyError("notSupported", "spotify stored login was not solved")


def _login5_solutions(challenges: list[Any], context: bytes) -> list[Any]:
    solutions = []
    for challenge in challenges:
        if not isinstance(challenge, dict):
            continue
        if challenge.get("code") and not challenge.get("hashcash"):
            raise SpotifyError("notSupported", "spotify asked for a code challenge")
        cash = challenge.get("hashcash")
        if not isinstance(cash, dict):
            continue
        prefix = cash.get("prefix") or b""
        if not isinstance(prefix, (bytes, bytearray)):
            prefix = b""
        suffix, elapsed = _solve_login5_hashcash(context, bytes(prefix), int(cash.get("length") or 0))
        solutions.append([(1, [(1, suffix), (2, [(1, elapsed)])])])
    if not solutions:
        raise SpotifyError("notSupported", "spotify stored login returned no token")
    return solutions


def _login5_call(opener: Opener, client_token: str, body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        _LOGIN5_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/x-protobuf",
            "Content-Type": "application/x-protobuf",
            "client-token": client_token,
        },
    )
    try:
        with opener(request, timeout=20) as response:
            payload = _ungzip(response.read(), getattr(response, "headers", {}))
    except urllib.error.HTTPError as exc:
        payload = _ungzip(exc.read(), {})
        if not payload:
            detail = "spotify stored login failed"
            raise SpotifyError("unauthorized" if exc.code in (401, 403) else "notSupported", detail) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise SpotifyError("notSupported", str(exc)) from exc
    try:
        return message(payload, _LOGIN5_RESPONSE)
    except ValueError as exc:
        raise SpotifyError("notSupported", "spotify stored login response was not a protobuf") from exc


def _solve_login5_hashcash(context: bytes, prefix: bytes, length: int) -> tuple[bytes, int]:
    """Same hashcash login5 uses: SHA1(prefix + suffix) trailing zeros, suffix derived from the context."""
    width = max(0, min(int(length), 32))
    target = int.from_bytes(hashlib.sha1(context).digest()[12:20], "big", signed=True)
    started = time.perf_counter()
    counter = 0
    while time.perf_counter() - started < 5:
        suffix = _i64(target + counter).to_bytes(8, "big", signed=True) + _i64(counter).to_bytes(8, "big", signed=True)
        check = int.from_bytes(hashlib.sha1(prefix + suffix).digest()[12:20], "big", signed=True)
        if _trailing_zeros(check) >= width:
            return suffix, max(0, int(time.perf_counter() - started))
        counter += 1
    raise SpotifyError("notSupported", "spotify login challenge was not solved")


def _i64(value: int) -> int:
    value %= 1 << 64
    if value >= 1 << 63:
        value -= 1 << 64
    return value


def _trailing_zeros(value: int) -> int:
    bits = value & ((1 << 64) - 1)
    if bits == 0:
        return 64
    count = 0
    while bits & 1 == 0:
        bits >>= 1
        count += 1
    return count


def _send_packet(sock: socket.socket, cipher: Shannon, command: int, payload: bytes) -> None:
    cipher.nonce_u32(0)
    buf = bytearray(bytes([command]) + len(payload).to_bytes(2, "big") + payload)
    cipher.encrypt(buf)
    buf += cipher.finish(4)
    sock.sendall(buf)


def _recv_packet(sock: socket.socket, cipher: Shannon) -> tuple[int, bytes]:
    cipher.nonce_u32(0)
    header = bytearray(_read_exact(sock, 3))
    cipher.decrypt(header)
    size = int.from_bytes(header[1:3], "big")
    payload = bytearray(_read_exact(sock, size + 4))
    mac = bytes(payload[size:])
    del payload[size:]
    cipher.decrypt(payload)
    cipher.check_mac(mac)
    return header[0], bytes(payload)


def _keys(shared: bytes, packets: bytes) -> tuple[bytes, bytes, bytes]:
    folded = bytearray()
    for index in range(1, 6):
        digest = hmac.new(shared, packets + bytes([index]), hashlib.sha1).digest()
        folded += digest
    challenge = hmac.new(folded[:0x14], packets, hashlib.sha1).digest()
    return challenge, bytes(folded[0x14:0x34]), bytes(folded[0x34:0x54])


def _access_point_keys(payload: bytes) -> tuple[bytes, bytes]:
    """Server public key and signature from the access-point challenge.

    The key sits inside the Diffie-Hellman message, which is inside the login
    crypto union, which is inside the challenge.
    """
    diffie = {
        0x0A: ("gs", "bytes"),
        0x1E: ("gs_signature", "bytes"),
    }
    parsed = message(
        payload,
        {
            0x0A: (
                "challenge",
                "message",
                {0x0A: ("login_crypto", "message", {0x0A: ("diffie_hellman", "message", diffie)})},
            )
        },
    )
    found = (((parsed.get("challenge") or {}).get("login_crypto") or {}).get("diffie_hellman")) or {}
    remote = found.get("gs") or b""
    signature = found.get("gs_signature") or b""
    return (
        bytes(remote) if isinstance(remote, (bytes, bytearray)) else b"",
        bytes(signature) if isinstance(signature, (bytes, bytearray)) else b"",
    )


def _verify_server(remote: bytes, signature: bytes) -> bool:
    if not remote or not signature:
        return False
    signed = pow(int.from_bytes(signature, "big"), 65537, int.from_bytes(_SERVER_KEY, "big"))
    encoded = signed.to_bytes(256, "big")
    digest = hashlib.sha1(remote).digest()
    expect = bytearray(b"\x00\x01")
    expect += b"\xff" * (256 - 3 - len(_SHA1_DIGESTINFO) - len(digest))
    expect += b"\x00" + _SHA1_DIGESTINFO + digest
    return encoded == expect


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise SpotifyError("notSupported", "spotify access point closed")
        chunks += part
    return bytes(chunks)
