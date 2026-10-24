"""Spotify account for this companion.

The Car Thing never receives the token. Home Assistant logs the user in, and
this process stores the refresh token. Playback arrives on Spotify's dealer
socket. The shelf uses the same card shape as 0.1.18 and is filled from
spclient.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .art import LIKED_SONGS_ASSET, art_url
from .connect import DESKTOP_CLIENT_ID, Connect
from .connect import SpotifyError as ConnectError

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_ROOT = "https://api.spotify.com/v1"
TOKEN_PATH = os.environ.get("BRIDGETHING_SPOTIFY_TOKEN", "/data/spotify.json")
SKEW_S = 60
MAX_ARTWORK = 256 * 1024
LIKED_COVER = "https://misc.scdn.co/liked-songs/liked-songs-300.jpg"
WEB_PLAYLIST_TTL_S = 60

Opener = Callable[..., Any]
log = logging.getLogger("bridgething.spotify")


def _looks_canonical_username(name: str) -> bool:
    """New Spotify user ids are long lowercase tokens, not display names."""
    return bool(name) and name.isalnum() and name.islower() and len(name) >= 16


class SpotifyError(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


class SpotifySession:
    def __init__(self, path: str = TOKEN_PATH, opener: Opener = urllib.request.urlopen) -> None:
        self.path = path
        self.opener = opener
        self.lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.images: dict[str, str] = {}
        self._art_bytes: dict[str, tuple[bytes, str]] = {}
        self.device_id: str | None = None
        self.volume_percent: int | None = None
        self.muted = False
        self._restore_percent = 50
        self._liked: dict[str, bool] = {}
        self._saved_list: list[str] | None = None
        self._playlist_seeds: dict[str, dict[str, Any]] = {}
        self._web_playlists: list[dict[str, Any]] = []
        self._web_playlists_at = 0.0
        self._played_playlists: dict[str, dict[str, Any]] = {}
        self._load()
        self.connect = Connect(lambda: self.opener, self.bearer, opener_factory=True)
        self.connect.manual = opener is not urllib.request.urlopen
        # Official companion caches the access-point username. A stored display
        # name is not a user id and must not be used for library requests.
        self.connect.username = ""
        stored = self._data.get("username")
        if isinstance(stored, str) and _looks_canonical_username(stored):
            self.connect.username = stored
        elif isinstance(stored, str) and stored:
            self._data.pop("username", None)
            self._save()
        self.connect.device_id = str(self._data.get("connect_device_id") or "")
        self.connect.on_identity = self._remember_identity

    @property
    def logged_in(self) -> bool:
        with self.lock:
            return bool(self._data.get("refresh_token") or self._data.get("access_token"))

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "loggedIn": bool(self._data.get("refresh_token") or self._data.get("access_token")),
                "username": self._data.get("username"),
            }

    def adopt(self, body: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        with self.lock:
            for key in ("access_token", "refresh_token"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    self._data[key] = value
            self._data["client_id"] = DESKTOP_CLIENT_ID
            self._data.pop("client_secret", None)
            expires_at = body.get("expires_at")
            expires_in = body.get("expires_in")
            clock = time.time() if now is None else now
            if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
                self._data["expires_at"] = float(expires_at)
            elif isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                self._data["expires_at"] = clock + float(expires_in)
            username = body.get("username")
            if isinstance(username, str) and _looks_canonical_username(username):
                self._data["username"] = username
                self.connect.username = username
            self._save()
        return self.status()

    def bearer(self, now: float | None = None) -> str | None:
        clock = time.time() if now is None else now
        with self.lock:
            access = self._data.get("access_token")
            expires = float(self._data.get("expires_at") or 0)
            refresh = self._data.get("refresh_token")
        if isinstance(access, str) and access and clock + SKEW_S < expires:
            return access
        if not isinstance(refresh, str) or not refresh:
            return access if isinstance(access, str) and access else None
        payload = self._token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": DESKTOP_CLIENT_ID,
            }
        )
        self._store_token(payload, refresh, clock)
        stored = self._data.get("access_token")
        return stored if isinstance(stored, str) else None

    def browse(self, body: dict[str, Any]) -> dict[str, Any]:
        """Library browse using the official Bridgething Spotify provider's shelf rules."""
        node = body.get("nodeId")
        limit = _limit(body.get("limit"), 20)
        offset = _offset(body.get("offset"))
        if node in (None, "", "root"):
            all_playlists = self._playlist_uris()
            preview = _official_preview(body.get("preview"))
            cards = self._official_playlist_items(all_playlists[:preview])
            recent = self._recent_cards(preview)
            entries = []
            if all_playlists:
                entries.append(_folder("playlists", "Playlists", cards, total=len(all_playlists)))
            if recent:
                entries.append(_folder("recently-played", "Recently played", recent, total=len(recent)))
            return {
                "entries": entries,
                "total": len(entries),
                "hasMore": False,
            }
        if node == "recently-played":
            tracks = self._recent_cards(offset + limit)
            return _page_result(tracks[offset : offset + limit], {"total": len(tracks), "hasMore": False})
        if node == "playlists":
            all_playlists = self._playlist_uris()
            page = all_playlists[offset : offset + limit]
            return _page_result(
                self._official_playlist_items(page),
                {"total": len(all_playlists), "hasMore": offset + len(page) < len(all_playlists)},
            )
        if node == "liked-songs" or _spotify_id(str(node))[0] == "collection":
            return self._saved_tracks(limit, offset)
        kind, item_id = _spotify_id(str(node))
        if kind == "playlist":
            return self._open_playlist(item_id, limit, offset)
        if kind == "album":
            page = self._page(f"/albums/{item_id}/tracks", limit, offset, _MARKET)
            return _page_result(self._apply_saved([_track(item, self.images) for item in page["items"] if isinstance(item, dict)]), page)
        if kind == "show":
            page = self._page(f"/shows/{item_id}/episodes", limit, offset)
            return _page_result([_episode(item, self.images) for item in page["items"]], page)
        if kind == "artist":
            payload = self._api("GET", f"/artists/{item_id}/top-tracks")
            tracks = payload.get("tracks") if isinstance(payload, dict) else []
            entries = self._apply_saved([_track(item, self.images) for item in tracks or [] if isinstance(item, dict)])
            page_items = entries[offset : offset + limit]
            return {
                "entries": [entry for entry in (_browse_entry(item) for item in page_items) if entry],
                "total": len(entries),
                "hasMore": offset + limit < len(entries),
            }
        raise SpotifyError("notFound", str(node))

    def _playlist_shelf(self, limit: int, offset: int) -> dict[str, Any]:
        if offset == 0:
            liked = self._liked_playlist()
            self._shelf_liked = liked is not None
        else:
            liked = None
        shift = 1 if getattr(self, "_shelf_liked", False) else 0
        room = limit - 1 if liked else limit
        page = self._page("/me/playlists", max(room, 1), max(0, offset - shift))
        playlists = [_playlist(item, self.images) for item in page["items"] if isinstance(item, dict) and item.get("uri")]
        if liked:
            playlists = playlists[: max(room, 0)]
        entries = ([liked] if liked else []) + playlists
        total = page.get("total")
        if isinstance(total, int) and shift:
            total += 1
        return _page_result(entries, {"total": total, "hasMore": bool(page.get("hasMore"))})

    def _liked_playlist(self) -> dict[str, Any] | None:
        try:
            page = self._page("/me/tracks", 1, 0, _MARKET)
        except SpotifyError:
            return None
        image = None
        if page["items"]:
            track = _entry_track(page["items"][0])
            album = track.get("album") if isinstance(track, dict) else None
            image = _inline_image(album, self.images) if isinstance(album, dict) else None
        return {
            "type": "playlist",
            "data": {
                "uri": "spotify:collection:tracks",
                "name": "Liked Songs",
                "ownerName": None,
                "trackCount": page.get("total"),
                "artworkId": image,
            },
        }

    def _playlist_tracks(self, playlist_id: str, limit: int, offset: int) -> dict[str, Any]:
        failure: SpotifyError | None = None
        blank: dict[str, Any] | None = None
        for path in (f"/playlists/{playlist_id}/items", f"/playlists/{playlist_id}/tracks"):
            try:
                page = self._page(path, limit, offset, {**_MARKET, "additional_types": "track,episode"})
            except SpotifyError as exc:
                failure = exc
                continue
            parsed = _playlist_entries(page["items"], self.images)
            if parsed or not page.get("total"):
                page["parsed"] = parsed
                return page
            blank = page
            blank["parsed"] = parsed
        embedded = self._embedded_playlist(playlist_id, limit, offset)
        if embedded is not None and (embedded["parsed"] or not embedded["items"]):
            return embedded
        if blank is not None:
            return blank
        if embedded is not None:
            return embedded
        if failure is not None:
            raise failure
        return {"items": [], "total": 0, "hasMore": False, "parsed": []}

    def _embedded_playlist(self, playlist_id: str, limit: int, offset: int) -> dict[str, Any] | None:
        try:
            payload = self._api("GET", f"/playlists/{playlist_id}", query=_MARKET)
        except SpotifyError:
            return None
        if not isinstance(payload, dict):
            return None
        paging = payload.get("items") if isinstance(payload.get("items"), dict) else payload.get("tracks")
        if not isinstance(paging, dict):
            return None
        raw = [item for item in paging.get("items") or [] if isinstance(item, dict)]
        start = paging.get("offset") if isinstance(paging.get("offset"), int) else 0
        local = raw[offset - start : offset - start + limit] if offset >= start else []
        held = start + len(raw)
        return {
            "items": local,
            "total": held,
            "hasMore": offset + len(local) < held,
            "parsed": _playlist_entries(local, self.images),
        }

    def _apply_saved(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending = []
        for entry in entries:
            data = entry.get("data") if isinstance(entry, dict) else None
            track_id = str(data.get("id") or "") if isinstance(data, dict) else ""
            if isinstance(data, dict) and data.get("saved") is not True and track_id.startswith("spotify:track:"):
                pending.append(data)
        kept = [entry for entry in entries if entry]
        if not pending:
            return kept
        try:
            flags = self.contains([str(data.get("id") or "") for data in pending])
        except SpotifyError:
            return kept
        for data, flag in zip(pending, flags):
            data["saved"] = bool(flag)
            track_id = str(data.get("id") or "")
            if track_id:
                self._liked[track_id] = bool(flag)
        return kept

    def _page(self, path: str, limit: int, offset: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit, "offset": offset}
        if extra:
            query.update(extra)
        payload = self._api("GET", path, query=query)
        items = payload.get("items") if isinstance(payload, dict) else []
        total = payload.get("total") if isinstance(payload, dict) else None
        return {
            "items": [item for item in items or [] if isinstance(item, dict)],
            "total": total,
            "hasMore": bool(payload.get("next")) if isinstance(payload, dict) else False,
        }

    def _collection(self, path: str, limit: int) -> list[dict[str, Any]]:
        try:
            return self._page(path, limit, 0)["items"]
        except SpotifyError:
            return []

    def _api(self, method: str, path: str, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
        token = self.bearer()
        if not token:
            raise SpotifyError("unauthorized")
        url = API_ROOT + path
        if query:
            url = url + "?" + urllib.parse.urlencode({key: value for key, value in query.items() if value is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        return self._read(request)

    def search(self, body: dict[str, Any]) -> dict[str, Any]:
        query = str(body.get("query") or "").strip()
        if not query:
            return {"items": [], "kinds": [], "total": 0, "hasMore": False}
        found = self._run(self.connect.search, query, _limit(body.get("limit"), 20))
        items: list[dict[str, Any]] = []
        kinds: list[str] = []
        for item in found:
            converted = _search_card(item, self.images, self.connect.saved)
            if not converted:
                continue
            kind = str(converted.get("type") or "")
            if kind and kind not in kinds:
                kinds.append(kind)
            items.append(converted)
        return {"items": items, "kinds": kinds, "total": len(items), "hasMore": False}

    def resolve(self, uri: str) -> dict[str, Any]:
        kind, item_id = _spotify_id(uri)
        if kind == "playlist":
            page = self._run(self.connect.playlist_page, item_id, 0, 1)
            return {"name": page.get("name"), "artworkId": _remember(page.get("image"), self.images), "subtitle": page.get("owner")}
        if kind == "album":
            album = self._run(self.connect.album, uri)
            return {"name": album.get("name"), "artworkId": _remember(album.get("image"), self.images), "subtitle": None}
        if kind == "artist":
            artist = self._run(self.connect.artist, uri)
            return {"name": artist.get("name"), "artworkId": _remember(artist.get("image"), self.images), "subtitle": "Artist"}
        if kind == "track":
            track = self._run(self.connect.track, uri)
            album = track.get("album") if isinstance(track.get("album"), dict) else {}
            pictures = album.get("images") if isinstance(album.get("images"), list) else []
            url = pictures[0].get("url") if pictures and isinstance(pictures[0], dict) else None
            return {"name": track.get("name"), "artworkId": _remember(url, self.images), "subtitle": _artist_name(track)}
        if kind == "show":
            show = self._run(self.connect.show, uri)
            return {"name": show.get("name"), "artworkId": _remember(show.get("image"), self.images), "subtitle": show.get("publisher")}
        raise SpotifyError("notFound", uri)

    def favorites(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._saved_page(_limit(body.get("limit"), 20), _offset(body.get("offset")))

    def contains(self, uris: list[str]) -> list[bool]:
        canonical = [_track_uri(uri) or "" for uri in uris]
        try:
            flags = self._run(self.connect.collection_contains, canonical)
        except SpotifyError:
            flags = [bool(uri and uri in self.connect.saved) for uri in canonical]
        for uri, flag in zip(canonical, flags):
            if uri and flag:
                self.connect.saved.add(uri)
                self._liked[uri] = True
        return [bool(flag) for flag in flags]

    def set_saved(self, uri: str, liked: bool) -> None:
        track_uri = _track_uri(uri)
        if not track_uri:
            raise SpotifyError("notSupported", uri)
        self._run(self.connect.write_saved, track_uri, liked)
        self._liked[track_uri] = liked
        self._liked[uri] = liked
        if self._saved_list is not None:
            if liked and track_uri not in self._saved_list:
                self._saved_list.insert(0, track_uri)
            elif not liked:
                self._saved_list = [item for item in self._saved_list if item != track_uri]

    def now_playing(self) -> dict[str, Any]:
        """Latest dealer cluster. This does not ask Spotify until the socket is already open."""
        self._ensure_observer()
        self.connect.ensure()
        player = self.connect.player(self.images)
        if self.connect.active_device_id:
            self.device_id = self.connect.active_device_id
        volume = player.get("volume")
        if isinstance(volume, dict) and isinstance(volume.get("level"), (int, float)) and not isinstance(volume.get("level"), bool):
            self.volume_percent = max(0, min(100, int(round(float(volume["level"]) * 100))))
            self.muted = bool(volume.get("muted")) or self.volume_percent == 0
            if self.volume_percent:
                self._restore_percent = self.volume_percent
            if isinstance(player.get("track"), dict):
                snapped = self.volume_state()
                if snapped is not None:
                    player["volume"] = snapped
        track = player.get("track")
        if isinstance(track, dict) and track.get("uri"):
            uri = _track_uri(str(track["uri"])) or str(track["uri"])
            track["uri"] = uri
            track["isLikeSupported"] = True
            if uri in self._liked:
                track["liked"] = self._liked[uri]
            elif track.get("liked") is True:
                self._liked[uri] = True
            else:
                try:
                    flags = self._run(self.connect.collection_contains, [uri])
                    self._liked[uri] = bool(flags and flags[0])
                except SpotifyError:
                    flags = None
            if uri in self._liked:
                track["liked"] = self._liked[uri]
        self._remember_playlist_context(player)
        self._remember_identity()
        return player

    def volume_state(self) -> dict[str, Any] | None:
        if self.volume_percent is None:
            return None
        return {"level": self.volume_percent / 100, "muted": self.muted}

    def volume_command(self, event: str, payload: dict[str, Any]) -> bool:
        if event == "volumeUp":
            self._step_volume(1 / 16)
        elif event == "volumeDown":
            self._step_volume(-1 / 16)
        elif event == "setVolume":
            self.set_volume_level(float(payload.get("level") or 0))
        elif event == "setMute":
            self.set_muted(bool(payload.get("muted")))
        elif event == "muteToggle":
            self.set_muted(not self.muted)
        else:
            return False
        return True

    def set_volume_level(self, level: float) -> None:
        percent = int(round(max(0.0, min(1.0, level)) * 100))
        target = self.device_id or self.connect.active_device_id
        if not target:
            raise SpotifyError("notFound", "no active Spotify device")
        self._ensure_observer()
        self._run(self.connect.set_volume, target, float(percent))
        self.volume_percent = percent
        self.muted = percent == 0
        if percent > 0:
            self._restore_percent = percent

    def set_muted(self, muted: bool) -> None:
        if muted:
            if self.volume_percent:
                self._restore_percent = self.volume_percent
            self.set_volume_level(0)
            self.muted = True
            return
        self.set_volume_level(self._restore_percent / 100)
        self.muted = False

    def _step_volume(self, step: float) -> None:
        current = self.volume_percent if isinstance(self.volume_percent, int) else 50
        if self.muted and step > 0:
            current = self._restore_percent
            self.muted = False
        self.set_volume_level(current / 100 + step)

    def play(self, uri: str, context: dict[str, Any] | None = None) -> None:
        context_uri = _context_uri(context)
        collection = _spotify_id(uri)[0] == "collection" or (context_uri is not None and _spotify_id(context_uri)[0] == "collection")
        if collection:
            self._play_context(self._collection_uri(), uri if uri.startswith("spotify:track:") else None)
            return
        if uri.startswith("spotify:track:") and context_uri and context_uri != uri:
            self._play_context(context_uri, uri)
            return
        self._play_context(uri, None)

    def resume(self) -> None:
        self._dealer("resume")

    def pause(self) -> None:
        self._dealer("pause")

    def next_track(self) -> None:
        self._dealer("skip_next")

    def previous_track(self) -> None:
        self._dealer("skip_prev")

    def seek(self, position_ms: int) -> None:
        self._dealer("seek_to", {"value": int(position_ms)})

    def shuffle(self, on: bool) -> None:
        self._dealer("set_shuffling_context", {"value": bool(on)})

    def repeat(self, mode: str) -> None:
        track = mode == "one"
        context = mode == "all"
        self._dealer("set_repeating_track", {"value": track})
        self._dealer("set_repeating_context", {"value": context})

    def enqueue(self, uri: str) -> None:
        self._dealer("add_to_queue", {"track": {"uri": uri, "provider": "queue"}})

    def command(self, event: str, payload: dict[str, Any]) -> bool:
        """Carry out a screen transport command. False when this event is not a player control."""
        if event == "pause":
            self.pause()
        elif event == "resume" or (event == "play" and not payload.get("uri")):
            self.resume()
        elif event in ("play", "queue") and isinstance(payload.get("uri"), str) and payload.get("uri"):
            context = payload.get("context") if isinstance(payload.get("context"), dict) else None
            if event == "queue":
                self.enqueue(str(payload["uri"]))
            else:
                self.play(str(payload["uri"]), context)
        elif event == "skipNext":
            self.next_track()
        elif event == "skipPrev":
            self.previous_track()
        elif event == "seekTo":
            position = payload.get("positionMs")
            if isinstance(position, bool) or not isinstance(position, (int, float)):
                raise SpotifyError("notSupported", "seek")
            self.seek(int(position))
        elif event == "setShuffle":
            self.shuffle(bool(payload.get("on")))
        elif event == "setRepeat":
            self.repeat(str(payload.get("mode") or "off"))
        else:
            return False
        return True

    def artwork(self, asset_id: str) -> tuple[bytes, str] | None:
        """Bytes for the id the device requested. 0.1.18 ids are spotify/img/300/{file}; the device rewrites 300 to 248 or 96."""
        key = asset_id.rsplit("/", 1)[-1]
        url = self.images.get(key) or art_url(asset_id)
        if url is None and (asset_id == LIKED_SONGS_ASSET or key == "liked-songs-300.jpg"):
            url = LIKED_COVER
        if not isinstance(url, str) or not url.startswith("http"):
            return None
        cached = self._art_bytes.get(url)
        if cached:
            return cached
        request = urllib.request.Request(url, headers={"User-Agent": "Spotify/1.2.52.442"})
        try:
            with self.opener(request, timeout=15) as response:
                payload = response.read(MAX_ARTWORK + 1)
                mime = response.headers.get("Content-Type", "image/jpeg") if hasattr(response, "headers") else "image/jpeg"
        except (OSError, urllib.error.URLError):
            return None
        if not payload or len(payload) > MAX_ARTWORK:
            return None
        found = (payload, mime.split(";")[0])
        self._art_bytes[url] = found
        return found

    def _playlist_uris(self) -> list[str]:
        self._playlist_seeds = {}
        cards = ["spotify:collection:tracks"]
        listed: list[dict[str, Any]] = []
        try:
            listed = self._run(self.connect.rootlist)
        except SpotifyError as exc:
            log.warning("rootlist failed: %s", exc.detail or exc.kind)
        seen = {_playlist_key(str(item.get("uri") or "")) for item in listed if isinstance(item, dict)}
        for item in self._playback_playlist_seeds() + self._web_playlist_seeds():
            uri = str(item.get("uri") or "")
            key = _playlist_key(uri)
            if uri and key not in seen:
                listed.append(item)
                seen.add(key)
        for item in listed:
            uri = str(item.get("uri") or "") if isinstance(item, dict) else ""
            if _spotify_id(uri)[0] == "playlist":
                cards.append(uri)
                self._playlist_seeds[uri] = dict(item)
        return cards

    def _web_playlist_seeds(self) -> list[dict[str, Any]]:
        """Token-scoped Web API list. Fills the shelf when rootlist is empty."""
        now = time.time()
        if self._web_playlists and now - self._web_playlists_at < WEB_PLAYLIST_TTL_S:
            return list(self._web_playlists)
        seeds: list[dict[str, Any]] = []
        offset = 0
        while offset < 200:
            try:
                page = self._page("/me/playlists", 50, offset)
            except SpotifyError as exc:
                log.warning("web playlist list failed: %s", exc.detail or exc.kind)
                break
            for item in page["items"]:
                uri = str(item.get("uri") or "")
                if _spotify_id(uri)[0] != "playlist":
                    continue
                pictures = item.get("images") if isinstance(item.get("images"), list) else []
                url = pictures[0].get("url") if pictures and isinstance(pictures[0], dict) else None
                owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
                tracks = item.get("tracks") if isinstance(item.get("tracks"), dict) else {}
                seeds.append(
                    {
                        "uri": uri,
                        "name": item.get("name") or "",
                        "owner": owner.get("display_name") or "",
                        "total": tracks.get("total"),
                        "image": url,
                    }
                )
            if not page.get("hasMore") or not page["items"]:
                break
            offset += len(page["items"])
        if seeds:
            self._web_playlists = seeds
            self._web_playlists_at = now
        return list(seeds)

    def _playback_playlist_seeds(self) -> list[dict[str, Any]]:
        self._remember_playlist_context(self.connect.player(self.images))
        return list(self._played_playlists.values())

    def _remember_playlist_context(self, player: dict[str, Any] | None) -> None:
        context = player.get("context") if isinstance(player, dict) else None
        if not isinstance(context, dict):
            return
        uri = context.get("uri")
        if not isinstance(uri, str) or _spotify_id(uri)[0] != "playlist":
            return
        name = context.get("name")
        self._played_playlists[uri] = {
            "uri": uri,
            "name": name if isinstance(name, str) else "",
            "owner": "",
            "total": None,
            "image": None,
        }

    def _official_playlist_items(self, uris: list[str]) -> list[dict[str, Any]]:
        """Hydrate playlist cards exactly like the official companion provider."""
        def load(uri: str) -> tuple[str, dict[str, Any] | None]:
            if _spotify_id(uri)[0] == "collection":
                return uri, {
                    "name": "Liked Songs",
                    "image": LIKED_COVER,
                }
            kind, item_id = _spotify_id(uri)
            if kind != "playlist" or not item_id:
                return uri, None
            try:
                face = self._run(self.connect.playlist_face, item_id)
            except SpotifyError as exc:
                log.warning("playlist cover failed: %s", exc.detail or exc.kind)
                face = None
            seed = self._playlist_seeds.get(uri)
            if face is None:
                return uri, seed
            if seed:
                face = {key: face.get(key) or seed.get(key) for key in ("name", "image", "total", "first")}
            return uri, face

        workers = min(8, max(1, len(uris)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            loaded = list(pool.map(load, uris))

        missing = [
            (uri, face.get("first"))
            for uri, face in loaded
            if face and not face.get("image") and isinstance(face.get("first"), str)
        ]
        covers: dict[str, dict[str, Any]] = {}
        if missing:
            try:
                covers = self._run(self.connect.tracks, [track for _uri, track in missing])
            except SpotifyError as exc:
                log.warning("playlist fallback covers failed: %s", exc.detail or exc.kind)

        entries: list[dict[str, Any]] = []
        for uri, face in loaded:
            if not face:
                continue
            reference = face.get("image")
            if not reference:
                first = next((track for playlist, track in missing if playlist == uri), None)
                album = (covers.get(first) or {}).get("album") if first else None
                pictures = album.get("images") if isinstance(album, dict) else None
                reference = pictures[0].get("url") if isinstance(pictures, list) and pictures and isinstance(pictures[0], dict) else None
            artwork = _remember(reference, self.images)
            if isinstance(reference, str) and reference.startswith("http"):
                key = reference.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
                if key:
                    self.images[key] = reference
            liked = _spotify_id(uri)[0] == "collection"
            card_uri = "spotify:collection:tracks" if liked else uri
            owner = None if liked else (self._playlist_seeds.get(uri) or {}).get("owner")
            if isinstance(owner, str) and _looks_canonical_username(owner):
                owner = None
            data: dict[str, Any] = {
                "uri": card_uri,
                "name": str(face.get("name") or ("Liked Songs" if liked else "")),
                "ownerName": owner,
                "trackCount": face.get("total") if isinstance(face.get("total"), int) else None,
            }
            if artwork:
                data["artworkId"] = artwork
            entries.append({"type": "playlist", "data": data})
        return entries

    def _recent_cards(self, limit: int) -> list[dict[str, Any]]:
        try:
            uris = [str(uri) for uri in self._run(self.connect.recent_uris, max(limit, 1)) if str(uri).startswith("spotify:track:")]
        except SpotifyError as exc:
            log.warning("recently played failed: %s", exc.detail or exc.kind)
            uris = []
        if not uris:
            uris = self._web_recent_uris(limit)
        uris = uris[:limit]
        return self._hydrate(uris, self._marks(uris))

    def _web_recent_uris(self, limit: int) -> list[str]:
        try:
            page = self._page("/me/player/recently-played", max(limit, 1), 0)
        except SpotifyError as exc:
            log.warning("web recently played failed: %s", exc.detail or exc.kind)
            return []
        uris: list[str] = []
        for item in page["items"]:
            track = item.get("track") if isinstance(item, dict) else None
            uri = track.get("uri") if isinstance(track, dict) else None
            if isinstance(uri, str) and uri.startswith("spotify:track:"):
                uris.append(uri)
        return uris

    def _open_playlist(self, playlist_id: str, limit: int, offset: int) -> dict[str, Any]:
        try:
            page = self._run(self.connect.playlist_page, playlist_id, offset, limit)
            uris = [str(uri) for uri in page.get("uris") or [] if str(uri).startswith("spotify:")]
            if uris or page.get("total") == 0:
                entries = self._hydrate(uris, self._marks(uris))
                total = page.get("total")
                held = total if isinstance(total, int) else offset + len(entries)
                return _page_result(entries, {"total": held, "hasMore": offset + len(page.get("uris") or []) < held})
        except SpotifyError as exc:
            log.warning("playlist page failed: %s", exc.detail or exc.kind)
        page = self._playlist_tracks(playlist_id, limit, offset)
        return _page_result(self._apply_saved(page.get("parsed") or []), page)

    def _hydrate(self, uris: list[str], saved: set[str]) -> list[dict[str, Any]]:
        metas: dict[str, dict[str, Any]] = {}
        tracks = [uri for uri in uris if uri.startswith("spotify:track:")]
        if tracks:
            try:
                metas = self._run(self.connect.tracks, tracks)
            except SpotifyError as exc:
                log.warning("track metadata failed: %s", exc.detail or exc.kind)
        cards = []
        for uri in uris:
            meta = metas.get(uri)
            if isinstance(meta, dict) and meta.get("name"):
                cards.append(self._card_from_meta(meta, saved=uri in saved))
                continue
            cards.append(
                _track(
                    {"uri": uri, "name": uri.rsplit(":", 1)[-1], "artists": [], "album": {}},
                    self.images,
                    saved=uri in saved,
                )
            )
        return cards

    def _saved_tracks(self, limit: int, offset: int) -> dict[str, Any]:
        """The first screen of Liked Songs. Stop once that window is in hand."""
        try:
            uris = self._saved(need=offset + limit)
        except SpotifyError as exc:
            log.warning("collection saved failed: %s", exc.detail or exc.kind)
            uris = []
        if not uris:
            try:
                fallback = self._page("/me/tracks", limit, offset, _MARKET)
            except SpotifyError:
                fallback = None
            if fallback is not None:
                entries = [
                    _track(track, self.images, saved=True)
                    for item in fallback["items"]
                    if (track := _entry_track(item)) is not None
                ]
                return _page_result(entries, fallback)
        page = uris[offset : offset + limit]
        items = self._hydrate(page, set(page))
        meta: dict[str, Any] = {"hasMore": (not self.connect.saved_loaded) or offset + limit < len(uris)}
        if self.connect.saved_loaded:
            meta["total"] = len(uris)
        return _page_result(items, meta)

    def _saved_page(self, limit: int, offset: int) -> dict[str, Any]:
        result = self._saved_tracks(limit, offset)
        return {"items": [entry["data"] for entry in result["entries"] if entry.get("type") == "item"], "total": result.get("total"), "hasMore": result.get("hasMore")}

    def _track_card(self, uri: str, saved: bool = False) -> dict[str, Any]:
        return self._card_from_meta(self._run(self.connect.track, uri), saved=saved)

    def _card_from_meta(self, meta: dict[str, Any], saved: bool = False) -> dict[str, Any]:
        album = meta.get("album") if isinstance(meta.get("album"), dict) else {}
        return _track(
            {
                "uri": meta.get("uri") or "",
                "name": meta.get("name") or "",
                "duration_ms": meta.get("duration_ms") if isinstance(meta.get("duration_ms"), int) and meta.get("duration_ms") > 0 else None,
                "artists": meta.get("artists") or [],
                "album": {"uri": album.get("uri") or "", "name": album.get("name") or "", "images": album.get("images") or []},
            },
            self.images,
            saved=saved,
        )

    def _marks(self, uris: list[str]) -> set[str]:
        known = {uri for uri in uris if uri in self.connect.saved or self._liked.get(uri)}
        pending = [uri for uri in uris if uri.startswith("spotify:track:") and uri not in known]
        if not pending:
            return known
        try:
            flags = self._run(self.connect.collection_contains, pending)
        except SpotifyError as exc:
            log.warning("liked check failed: %s", exc.detail or exc.kind)
            return known
        for uri, flag in zip(pending, flags):
            self._liked[uri] = bool(flag)
            if flag:
                known.add(uri)
                self.connect.saved.add(uri)
        return known

    def _saved(self, need: int | None = None) -> list[str]:
        if self.connect.saved_loaded and self._saved_list is not None:
            return list(self._saved_list)
        if need is not None and self._saved_list is not None and len(self._saved_list) >= need:
            return list(self._saved_list)
        self._saved_list = list(self._run(self.connect.saved_uris, "track", need))
        return list(self._saved_list)

    def _collection_uri(self) -> str:
        username = self.connect._username()
        return f"spotify:user:{username}:collection"

    def _play_context(self, context_uri: str, skip_to: str | None) -> None:
        self._arm_target()
        self._run(self.connect.play, context_uri, skip_to)

    def _dealer(self, endpoint: str, extra: dict[str, Any] | None = None) -> None:
        self._arm_target()
        self._run(self.connect.command, endpoint, None, extra)

    def _arm_target(self) -> None:
        self._ensure_observer()
        if self.device_id:
            self.connect.active_device_id = self.device_id
        elif self.connect.active_device_id:
            self.device_id = self.connect.active_device_id

    def _ensure_observer(self) -> None:
        if self.connect.device_id:
            return
        self.connect.device_id = os.urandom(20).hex()
        self._remember_identity()

    def _run(self, func: Callable[..., Any], *args: Any) -> Any:
        try:
            result = func(*args)
        except ConnectError as exc:
            raise SpotifyError(exc.kind, exc.detail) from exc
        self._remember_identity()
        return result

    def _remember_identity(self) -> None:
        with self.lock:
            changed = False
            if self.connect.username and self._data.get("username") != self.connect.username:
                self._data["username"] = self.connect.username
                changed = True
            if self.connect.device_id and self._data.get("connect_device_id") != self.connect.device_id:
                self._data["connect_device_id"] = self.connect.device_id
                changed = True
            if changed:
                self._save()

    def _token(self, fields: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode(fields).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = self._read(request)
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SpotifyError("unauthorized", "spotify rejected the refresh token")
        return payload

    def _read(self, request: urllib.request.Request) -> Any:
        try:
            with self.opener(request, timeout=20) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:180]
            if exc.code in (401, 403):
                raise SpotifyError("unauthorized", detail) from exc
            if exc.code == 404:
                raise SpotifyError("notFound", detail) from exc
            raise SpotifyError("notSupported", detail or str(exc.code)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise SpotifyError("notSupported", str(exc)) from exc
        if status >= 400:
            raise SpotifyError("unauthorized" if status in (401, 403) else "notSupported")
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            if status < 400 and not stripped.startswith((b"{", b"[")):
                return {}
            raise SpotifyError("notSupported", "spotify returned invalid json") from exc

    def _store_token(self, payload: dict[str, Any], previous_refresh: str, now: float) -> None:
        with self.lock:
            access = payload.get("access_token")
            if isinstance(access, str) and access:
                self._data["access_token"] = access
            rotated = payload.get("refresh_token")
            if isinstance(rotated, str) and rotated:
                self._data["refresh_token"] = rotated
            elif previous_refresh:
                self._data["refresh_token"] = previous_refresh
            expires_in = payload.get("expires_in")
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                self._data["expires_at"] = now + float(expires_in)
            self._data["client_id"] = DESKTOP_CLIENT_ID
            self._data.pop("client_secret", None)
            self._save()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            self._data = payload

    def _save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle)
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            return


def _limit(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) < 1:
        return default
    return min(50, int(value))


def _offset(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _context_uri(context: dict[str, Any] | None) -> str | None:
    if not isinstance(context, dict):
        return None
    for key in ("contextUri", "uri"):
        value = context.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _track_uri(uri: str) -> str | None:
    kind, item_id = _spotify_id(uri)
    if kind == "track" and item_id:
        return f"spotify:track:{item_id}"
    return None


def _playlist_key(uri: str) -> str:
    kind, item_id = _spotify_id(uri)
    return f"{kind}:{item_id}" if item_id else uri


def _spotify_id(uri: str) -> tuple[str, str]:
    parts = uri.split("?", 1)[0].split(":")
    if len(parts) >= 5 and parts[0] == "spotify" and parts[1] == "user" and parts[3] == "playlist":
        return "playlist", parts[4]
    if len(parts) >= 3 and parts[0] == "spotify" and parts[1] == "collection":
        return "collection", parts[2]
    if ":collection" in uri:
        return "collection", uri
    if len(parts) >= 3 and parts[0] == "spotify":
        return parts[1], parts[2]
    return "", uri


def _artist_name(item: dict[str, Any]) -> str | None:
    artists = item.get("artists") if isinstance(item, dict) else None
    if isinstance(artists, list) and artists and isinstance(artists[0], dict):
        name = artists[0].get("name")
        return name if isinstance(name, str) else None
    return None


def _page_result(entries: list[dict[str, Any]], page: dict[str, Any]) -> dict[str, Any]:
    wrapped = [entry for entry in (_browse_entry(item) for item in entries) if entry]
    return {"entries": wrapped, "total": page.get("total"), "hasMore": bool(page.get("hasMore"))}


def _folder(node_id: str, title: str, children: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    preview = [entry for entry in (_browse_entry(item) for item in children) if entry]
    data: dict[str, Any] = {
        "nodeId": node_id,
        "title": title,
        "total": len(preview) if total is None else total,
    }
    if preview:
        data["previewChildren"] = preview
    return {
        "type": "folder",
        "data": data,
    }


def _browse_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    """A library card is an item around the track, album, or playlist."""
    if not entry:
        return None
    if entry.get("type") in ("folder", "item"):
        return entry
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    if not (data.get("uri") or data.get("id") or data.get("name")):
        return None
    return {"type": "item", "data": entry}


def _remember(url: str | None, images: dict[str, str] | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    return _inline_image({"images": [{"url": url}]}, images)


def _official_preview(value: Any) -> int:
    """Official provider default and cap for root shelf previews."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 14
    return max(0, min(14, int(value)))


_MARKET = {"market": "from_token"}


def _entry_track(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    for key in ("track", "item"):
        value = entry.get(key)
        if not isinstance(value, dict):
            continue
        nested = value.get("track") if isinstance(value.get("track"), dict) else value.get("item")
        if isinstance(nested, dict) and (_is_track(nested) or _is_episode(nested)):
            return nested
        if _is_track(value) or _is_episode(value):
            return value
    if _is_track(entry) or _is_episode(entry):
        return entry
    return None


def _playlist_entries(items: list[dict[str, Any]], images: dict[str, str] | None) -> list[dict[str, Any]]:
    entries = []
    for entry in items:
        media = _entry_track(entry)
        if not isinstance(media, dict):
            continue
        if _is_episode(media):
            entries.append(_episode(media, images))
        elif _is_track(media):
            entries.append(_track(media, images))
    return entries


def _is_track(item: dict[str, Any]) -> bool:
    uri = str(item.get("uri") or "")
    kind = item.get("type")
    return uri.startswith("spotify:track:") or kind == "track"


def _is_episode(item: dict[str, Any]) -> bool:
    uri = str(item.get("uri") or "")
    return uri.startswith("spotify:episode:") or item.get("type") == "episode"


def _track(item: dict[str, Any], images: dict[str, str] | None = None, saved: bool = False) -> dict[str, Any]:
    if not item:
        return {}
    album = item.get("album") if isinstance(item.get("album"), dict) else {}
    artists = [artist for artist in item.get("artists") or [] if isinstance(artist, dict)]
    primary = artists[0] if artists else {}
    image = _inline_image(album, images)
    duration = item.get("duration_ms")
    return {
        "type": "track",
        "data": {
            "id": item.get("uri") or item.get("id") or "",
            "name": item.get("name") or "",
            "album": {"id": album.get("uri") or album.get("id") or "", "name": album.get("name") or "", "artworkId": image},
            "artist": {"id": primary.get("uri") or primary.get("id") or "", "name": primary.get("name") or ""},
            "artists": [{"id": artist.get("uri") or artist.get("id") or "", "name": artist.get("name") or ""} for artist in artists],
            "durationMs": int(duration) if isinstance(duration, int) and duration > 0 else 0,
            "imageId": image or "",
            "saved": bool(saved),
        },
    }


def _playlist(item: dict[str, Any], images: dict[str, str] | None = None) -> dict[str, Any]:
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    tracks = item.get("items") if isinstance(item.get("items"), dict) else item.get("tracks")
    tracks = tracks if isinstance(tracks, dict) else {}
    return {
        "type": "playlist",
        "data": {
            "uri": item.get("uri") or "",
            "name": item.get("name") or "",
            "ownerName": owner.get("display_name"),
            "trackCount": tracks.get("total") if isinstance(tracks.get("total"), int) else None,
            "artworkId": _inline_image(item, images),
        },
    }


def _album(item: dict[str, Any], images: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "album",
        "data": {
            "id": item.get("uri") or item.get("id") or "",
            "name": item.get("name") or "",
            "artworkId": _inline_image(item, images),
        },
    }


def _artist(item: dict[str, Any], images: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "artist",
        "data": {
            "id": item.get("uri") or item.get("id") or "",
            "name": item.get("name") or "",
            "artworkId": _inline_image(item, images),
        },
    }


def _show(item: dict[str, Any], images: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "show",
        "data": {
            "uri": item.get("uri") or "",
            "name": item.get("name") or "",
            "publisher": item.get("publisher"),
            "artworkId": _inline_image(item, images),
        },
    }


def _episode(item: dict[str, Any], images: dict[str, str] | None = None) -> dict[str, Any]:
    show = item.get("show") if isinstance(item.get("show"), dict) else {}
    return {
        "type": "podcastEpisode",
        "data": {
            "uri": item.get("uri") or "",
            "name": item.get("name") or "",
            "showName": show.get("name"),
            "durationMs": item.get("duration_ms"),
            "artworkId": _inline_image(item, images) or _inline_image(show, images),
        },
    }


def _search_card(item: dict[str, Any], images: dict[str, str], saved: set[str]) -> dict[str, Any] | None:
    uri = str(item.get("uri") or "")
    name = item.get("name") or ""
    picture = item.get("image") if isinstance(item.get("image"), str) else ""
    pictures = [{"url": picture}] if picture else []
    if uri.startswith("spotify:track:"):
        track = item.get("track") if isinstance(item.get("track"), dict) else {}
        album = track.get("album") if isinstance(track.get("album"), dict) else {}
        artists = [artist for artist in track.get("artists") or [] if isinstance(artist, dict)]
        seconds = (track.get("duration") or {}).get("seconds") if isinstance(track.get("duration"), dict) else 0
        return _track(
            {
                "uri": uri,
                "name": name,
                "duration_ms": int(seconds or 0) * 1000,
                "artists": artists,
                "album": {"uri": album.get("uri") or "", "name": album.get("name") or "", "images": pictures},
            },
            images,
            saved=uri in saved,
        )
    if uri.startswith("spotify:album:"):
        return _album({"uri": uri, "name": name, "images": pictures}, images)
    if uri.startswith("spotify:artist:"):
        return _artist({"uri": uri, "name": name, "images": pictures}, images)
    if uri.startswith("spotify:playlist:") or ":playlist:" in uri:
        return _playlist({"uri": uri, "name": name, "images": pictures}, images)
    if uri.startswith("spotify:show:"):
        return _show({"uri": uri, "name": name, "images": pictures}, images)
    return None


def _inline_image(item: dict[str, Any], remembered: dict[str, str] | None = None) -> str | None:
    pictures = item.get("images") if isinstance(item, dict) else None
    if not isinstance(pictures, list) or not pictures or not isinstance(pictures[0], dict):
        return None
    url = pictures[0].get("url")
    if not isinstance(url, str) or not url:
        return None
    key = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if not key:
        return None
    if remembered is not None:
        remembered[key] = url
    return f"spotify/img/300/{key}"


def library_error(exc: SpotifyError) -> dict[str, Any]:
    if exc.kind == "notFound":
        return {"type": "notFound", "data": {"uri": exc.detail}}
    if exc.kind == "unauthorized":
        return {"type": "unauthorized"}
    return {"type": "notSupported", "data": {"reason": exc.detail or exc.kind}}


def _stopped_player() -> dict[str, Any]:
    from .playback import idle_playback

    return {
        "track": None,
        "playback": idle_playback(),
        "queue": [],
        "options": {"speed": 1.0},
        "origin": "spotify",
        "picture": None,
    }
