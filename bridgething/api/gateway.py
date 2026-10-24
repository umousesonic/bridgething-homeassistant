"""Framed session on port 8892. This process is the Car Thing's companion."""

from __future__ import annotations

import datetime
import logging
import socket
import threading
from typing import Any

from .ha import HomeAssistant
from .version import __version__
from .ncm import CLIENT_PORT, GATEWAY_PORT
from .net_proxy import Tunnel, connect_tunnel, fetch, open_stream
from .playback import (
    app_bundle,
    has_track,
    music_provider,
    player_state_from_ha,
    queue_snapshot,
    transport_service,
    wire_player_state,
)
from .spotify import SpotifyError, SpotifySession, library_error
from .protocol import as_bytes, envelope, error_response, inner_event, kind_of, outer_type, request_id_of, response_request_id
from .state import AppState
from .ws import WebSocket, connect

log = logging.getLogger("bridgething.gateway")
SCOPES = ("nowPlayingMetadata", "nowPlayingPlayback", "volume")


class Pending:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict[str, Any] | None = None


def capabilities(entity_id: str, logged_in: bool = False) -> dict[str, Any]:
    provider = "spotify" if logged_in or music_provider(entity_id) == "spotify" else "none"
    return {
        "gateway": {
            "address": "home-assistant",
            "name": "Home Assistant",
            "osName": "Linux",
            "appName": "bridgething-home-assistant",
            "appVersion": __version__,
            "adapterVersion": __version__,
            "libVersion": "0.1.0",
            "libbridgethingVersion": "0.1.0",
        },
        "uriSchemes": ["spotify"] if provider == "spotify" else [],
        "network": {"kind": "ethernet", "metered": False},
        "available": {
            "geo": False,
            "notifications": False,
            "netFetch": True,
            "netWs": True,
            "audioTts": False,
            "lyrics": False,
            "playbackTargets": False,
            "forward": False,
        },
        "audio": {"earcons": [], "voices": []},
        "musicProvider": provider,
    }


def _favorite_uris(body: dict[str, Any]) -> list[str]:
    raw = body.get("uris")
    if not isinstance(raw, list):
        raw = body.get("items") if isinstance(body.get("items"), list) else []
    found: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            found.append(item)
        elif isinstance(item, dict) and item.get("uri"):
            found.append(str(item["uri"]))
    return found


def time_info() -> dict[str, Any]:
    now = datetime.datetime.now().astimezone()
    offset = now.utcoffset()
    dst = now.dst()
    zone = getattr(now.tzinfo, "key", None)
    return {
        "tzIana": zone,
        "wallClockUnixS": int(now.timestamp()),
        "utcOffsetMinutes": int(offset.total_seconds() // 60) if offset else 0,
        "dstOffsetMinutes": int(dst.total_seconds() // 60) if dst else 0,
    }


class GatewaySession:
    def __init__(self, state: AppState, ha: HomeAssistant, spotify: SpotifySession | None = None, *, inline_net: bool = False) -> None:
        self.state = state
        self.ha = ha
        self.spotify = spotify
        self.inline_net = inline_net
        self.sent: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.waiting: dict[str, Pending] = {}
        self.ws: WebSocket | None = None
        self.claimed = False
        self.announced_provider: str | None = None
        self.sent_generation = -1
        self.tunnels: dict[str, Tunnel] = {}
        self.upstreams: dict[str, WebSocket] = {}
        self.sent_volume: dict[str, Any] | None = None

    def connect(self, host: str) -> None:
        self.close()
        self.ws = connect(host, GATEWAY_PORT)
        self.ws.sock.settimeout(0.35)
        self.claimed = False
        self.announced_provider = None
        self.sent_generation = -1
        self.state.update(gatewayConnected=True, gatewayHost=host)

    def close(self) -> None:
        for tunnel in list(self.tunnels.values()):
            tunnel.closed = True
            try:
                tunnel.sock.close()
            except OSError:
                pass
        self.tunnels.clear()
        for upstream in list(self.upstreams.values()):
            upstream.close()
        self.upstreams.clear()
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        self.state.update(gatewayConnected=False)

    def _off_socket(self, target: Any, *args: Any) -> None:
        """Library and artwork answers can take a moment. The socket still has to read keepalives."""
        if self.ws is None or self.inline_net:
            target(*args)
            return
        threading.Thread(target=target, args=args, daemon=True).start()

    def send(self, message: dict[str, Any]) -> None:
        from .protocol import encode_wire

        payload = encode_wire(message)
        with self.lock:
            self.sent.append(message)
            ws = self.ws
            if ws is not None:
                ws.send(payload)

    def request(self, surface: str, event: str, body: Any = None, timeout: float = 8.0) -> dict[str, Any] | None:
        message = envelope("request", surface, event, body)
        pending = Pending()
        with self.lock:
            self.waiting[message["id"]] = pending
        try:
            self.send(message)
        except OSError:
            with self.lock:
                self.waiting.pop(message["id"], None)
            raise
        if not pending.event.wait(timeout):
            with self.lock:
                self.waiting.pop(message["id"], None)
            raise TimeoutError(event)
        return pending.result

    def handle(self, message: dict[str, Any]) -> None:
        try:
            self._dispatch(message)
        finally:
            self._resolve(message)

    def sync_playback(self, force: bool = False) -> None:
        generation, player, entity = self.state.playback_view()
        if player is None:
            player = player_state_from_ha(entity, None)
        if not force and generation == self.sent_generation:
            return
        self.sent_generation = generation
        logged_in = bool(self.spotify and self.spotify.logged_in)
        provider = "spotify" if logged_in else music_provider(entity)
        if self.announced_provider != provider:
            self.send(envelope("event", "capabilities", "announce", capabilities(entity, logged_in)))
            self.announced_provider = provider
        want = has_track(player)
        bundle = "com.spotify.client" if logged_in else app_bundle(entity)
        if want and not self.claimed:
            for scope in SCOPES:
                self.send(envelope("event", "authority", "claim", {"scope": scope, "appBundle": bundle}))
            self.claimed = True
        elif not want and self.claimed:
            for scope in SCOPES:
                self.send(envelope("event", "authority", "release", {"scope": scope}))
            self.claimed = False
        self.send(envelope("event", "player", "snapshot", wire_player_state(player)))
        if want:
            self.send(envelope("event", "player", "queueChanged", queue_snapshot(player)))
        self._publish_volume(player.get("volume") if isinstance(player, dict) else None)

    def navigate(self, url: str) -> None:
        self.state.update(navigateUrl=url)
        self.send(envelope("command", "chrome", "navigate", {"url": url}))

    def set_nickname(self, nickname: str) -> None:
        self.send(envelope("request", "system", "deviceSetNickname", {"nickname": nickname}))

    def switch_webapp(self, webapp_id: str) -> None:
        self.send(envelope("request", "webapp", "switchTo", {"id": webapp_id}))

    def _resolve(self, message: dict[str, Any]) -> None:
        rid = response_request_id(message)
        if not rid:
            return
        with self.lock:
            pending = self.waiting.pop(rid, None)
        if pending is not None:
            pending.result = message
            pending.event.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        surface = outer_type(message)
        event, body = inner_event(message)
        kind = kind_of(message)
        rid = request_id_of(message)
        if surface == "version" and isinstance(body, dict):
            self.state.apply_meta(body)
            entity = str(self.state.get("sourceEntity") or "")
            logged_in = bool(self.spotify and self.spotify.logged_in)
            self.send(envelope("event", "capabilities", "announce", capabilities(entity, logged_in)))
            self.announced_provider = "spotify" if logged_in else music_provider(entity)
            self.send(envelope("event", "time", "snapshot", time_info()))
            self.sync_playback(force=True)
            self.send(envelope("request", "webapp", "list"))
            self.send(envelope("request", "webapp", "getActive"))
            return
        if surface == "system" and event == "keepalive":
            seq = body.get("seq") if isinstance(body, dict) else None
            self.send(envelope("response", "system", "keepaliveAck", {"seq": seq}, rid))
            return
        if surface == "system" and event in ("deviceNickname", "deviceNicknameChanged") and isinstance(body, dict):
            self.state.update(nickname=body.get("nickname"))
            if isinstance(body.get("nickname"), str):
                self.state.apply_meta({"nickname": body.get("nickname")})
            return
        if surface == "player":
            self._player(event, body, rid)
            return
        if surface == "asset" and event in ("get", "request", "preload"):
            self._off_socket(self._asset_message, event, body if isinstance(body, dict) else {}, rid)
            return
        if surface == "net":
            target = self._net
            args = (event, body if isinstance(body, dict) else {}, rid)
            if self.inline_net:
                target(*args)
            else:
                threading.Thread(target=target, args=args, daemon=True).start()
            return
        if surface == "tunnel":
            target = self._tunnel
            args = (event, body if isinstance(body, dict) else {}, rid)
            if self.inline_net:
                target(*args)
            else:
                threading.Thread(target=target, args=args, daemon=True).start()
            return
        if surface == "webapp":
            self._webapp(event, body)
            return
        if surface == "library":
            self._library(event, body if isinstance(body, dict) else {}, rid, kind)
            return
        if surface == "audio":
            self._audio(event, body if isinstance(body, dict) else {})
            return
        if kind == "request":
            self.send(error_response(rid, "unsupported"))

    def _player(self, event: str, body: Any, rid: str) -> None:
        if event == "snapshotRequest":
            self._refresh_spotify()
            self.sync_playback(force=True)
            self.send(envelope("response", "player", "snapshotAck", {}, rid))
            return
        payload = body if isinstance(body, dict) else {}
        if self._spotify_transport(event, payload):
            return
        entity = str(self.state.get("sourceEntity") or "")
        if not entity:
            self._play_failed("no active transport")
            return
        service = transport_service(event, payload, entity)
        if service is None:
            self._play_failed(event or "unsupported")
            return
        try:
            self.ha.call_service(*service)
        except Exception as exc:  # noqa: BLE001
            self._play_failed(str(exc))
            return

    def _play_failed(self, reason: str) -> None:
        self.send(
            envelope(
                "event",
                "player",
                "errorEvent",
                {"error": {"type": "playFailed", "data": {"reason": reason}}},
            )
        )

    def _asset_message(self, event: str, body: dict[str, Any], rid: str) -> None:
        if event == "preload":
            for asset_id in [str(item) for item in body.get("ids") or [] if item][:64]:
                if self._fetch_asset(asset_id):
                    self.send(envelope("event", "asset", "ready", {"id": asset_id}))
            return
        self._asset(body, rid)

    def _fetch_asset(self, asset_id: str) -> tuple[bytes, str] | None:
        playback = self.state.get("playback") or {}
        track = playback.get("track") if isinstance(playback, dict) else None
        picture = playback.get("picture") if isinstance(playback, dict) else None
        artwork = track.get("artworkId") if isinstance(track, dict) else None
        fetched = self.ha.fetch_picture(picture) if picture and asset_id == artwork else None
        if fetched is None and self.spotify is not None:
            fetched = self.spotify.artwork(asset_id)
        return fetched

    def _asset(self, body: dict[str, Any], rid: str) -> None:
        asset_id = str(body.get("id") or "")
        fetched = self._fetch_asset(asset_id)
        if fetched:
            payload, mime = fetched
            self.send(
                envelope(
                    "response",
                    "asset",
                    "got",
                    {"id": asset_id, "mime": mime, "body": {"type": "inline", "data": payload}},
                    rid,
                )
            )
            return
        self.send(envelope("response", "asset", "notFound", {"id": asset_id}, rid))

    def _spotify_transport(self, event: str, payload: dict[str, Any]) -> bool:
        if self.spotify is None or not self.spotify.logged_in:
            return False
        try:
            handled = self.spotify.command(event, payload)
        except SpotifyError as exc:
            self._play_failed(exc.detail or exc.kind)
            return True
        if not handled:
            return False
        self._refresh_spotify()
        self.sync_playback(force=True)
        return True

    def _refresh_spotify(self) -> None:
        if self.spotify is None or not self.spotify.logged_in:
            return
        try:
            self.state.set_playback(self.spotify.now_playing())
        except SpotifyError as exc:
            log.warning("spotify player unavailable: %s", exc.detail or exc.kind)

    def _publish_volume(self, volume: Any) -> None:
        if not isinstance(volume, dict) or volume == self.sent_volume:
            return
        level = volume.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            return
        state = {"level": float(level), "muted": bool(volume.get("muted"))}
        self.sent_volume = state
        self.send(envelope("event", "audio", "volumeChanged", state))

    def _audio(self, event: str, body: dict[str, Any]) -> None:
        if self.spotify is not None and self.spotify.logged_in:
            try:
                changed = self.spotify.volume_command(event, body)
            except SpotifyError as exc:
                log.warning("spotify volume failed: %s", exc.detail or exc.kind)
                return
            if changed:
                self._publish_volume(self.spotify.volume_state())
            return
        entity = str(self.state.get("sourceEntity") or "")
        if not entity:
            return
        if event == "volumeUp":
            service = ("media_player", "volume_up", {"entity_id": entity})
        elif event == "volumeDown":
            service = ("media_player", "volume_down", {"entity_id": entity})
        elif event == "setVolume":
            service = ("media_player", "volume_set", {"entity_id": entity, "volume_level": float(body.get("level") or 0)})
        elif event == "setMute":
            service = ("media_player", "volume_mute", {"entity_id": entity, "is_volume_muted": bool(body.get("muted"))})
        elif event == "muteToggle":
            service = ("media_player", "volume_mute", {"entity_id": entity, "is_volume_muted": True})
        else:
            return
        try:
            self.ha.call_service(*service)
        except Exception as exc:  # noqa: BLE001
            log.warning("volume command failed: %s", exc)

    def _library(self, event: str, body: dict[str, Any], rid: str, kind: str) -> None:
        if self.spotify is None or not self.spotify.logged_in:
            self._library_error(rid, kind, {"type": "unauthorized"})
            return
        try:
            if event == "browse":
                result = self.spotify.browse(body)
                shelves = []
                for entry in result.get("entries") or []:
                    data = entry.get("data") if isinstance(entry, dict) and isinstance(entry.get("data"), dict) else {}
                    kids = data.get("previewChildren")
                    if isinstance(kids, list):
                        kinds = [
                            str((kid.get("data") or {}).get("type") or kid.get("type") or "?")
                            for kid in kids
                            if isinstance(kid, dict)
                        ]
                        shelves.append(f"{data.get('nodeId')}={len(kids)}[{','.join(kinds)}]")
                from .protocol import dumps

                encoded_size = len(dumps(envelope("response", "library", "browseReply", {"result": result}, rid)))
                log.info(
                    "browse request node=%r limit=%r preview=%r replyBytes=%d shelves=%s",
                    body.get("nodeId"),
                    body.get("limit"),
                    body.get("preview"),
                    encoded_size,
                    " ".join(shelves) or f"page={len(result.get('entries') or [])}",
                )
                self.send(envelope("response", "library", "browseReply", {"result": result}, rid))
            elif event == "search":
                self.send(envelope("response", "library", "searchReply", {"result": self.spotify.search(body)}, rid))
            elif event == "resolveContext":
                self.send(envelope("response", "library", "contextResolveReply", self.spotify.resolve(str(body.get("uri") or "")), rid))
            elif event == "favoritesList":
                self.send(envelope("response", "library", "favoritesListReply", {"page": self.spotify.favorites(body)}, rid))
            elif event == "favoritesContains":
                self.send(envelope("response", "library", "favoritesContainsReply", {"liked": self.spotify.contains(_favorite_uris(body))}, rid))
            elif event == "recommendations":
                self._library_error(rid, kind, {"type": "notSupported", "data": {"reason": "recommendations"}})
            elif event in ("favoritesSet", "favoritesToggle", "favoritesSetMany"):
                self._favorites_write(event, body)
            elif kind == "request":
                self._library_error(rid, kind, {"type": "notSupported", "data": {"reason": event or "unsupported"}})
        except SpotifyError as exc:
            log.warning("library %s failed: %s", event or kind, exc.detail or exc.kind)
            self._library_error(rid, kind, library_error(exc))
        except Exception as exc:  # noqa: BLE001
            log.warning("library %s failed: %s", event or kind, exc)
            self._library_error(rid, kind, {"type": "notSupported", "data": {"reason": "library failed"}})

    def _favorites_write(self, event: str, body: dict[str, Any]) -> None:
        assert self.spotify is not None
        if event == "favoritesSetMany":
            for entry in body.get("entries") or []:
                if isinstance(entry, dict):
                    self._favorites_write("favoritesSet", entry)
            return
        item = body.get("item") if isinstance(body.get("item"), dict) else {}
        uri = str(item.get("uri") or "")
        if not uri:
            return
        if event == "favoritesToggle":
            liked = not self.spotify.contains([uri])[0]
        else:
            liked = bool(body.get("liked"))
        self.spotify.set_saved(uri, liked)
        self.send(envelope("event", "library", "favoriteChanged", {"uri": uri, "liked": liked}))
        self._refresh_spotify()
        self.sync_playback(force=True)

    def _library_error(self, rid: str, kind: str, error: dict[str, Any]) -> None:
        if kind == "request" and rid:
            self.send(envelope("response", "library", "errorReply", {"error": error}, rid))

    def _webapp(self, event: str, body: Any) -> None:
        if not isinstance(body, dict):
            return
        if event == "webapps" and isinstance(body.get("webapps"), list):
            self.state.update(webapps=body["webapps"])
        if event in ("active", "switched", "activeChanged"):
            self.state.update(activeWebapp=body.get("id"), activeWebappName=body.get("name"))

    def _net(self, event: str, body: dict[str, Any], rid: str) -> None:
        try:
            if event == "fetch":
                self._fetch(body, rid)
            elif event == "wsOpen":
                self._ws_open(body, rid)
            elif event == "wsSend":
                self._ws_send(body)
            elif event == "wsClose":
                self._ws_close(body)
            elif event == "streamOpen":
                open_stream(body.get("request") or {}, self._stream_event, str(body.get("streamId") or ""))
            elif event == "streamCancel":
                return
            elif rid:
                self.send(error_response(rid, "unsupported"))
        except Exception as exc:  # noqa: BLE001
            log.warning("net proxy failed: %s", exc)
            if rid:
                self.send(
                    envelope(
                        "response",
                        "net",
                        "fetchErrorReply",
                        {"error": {"type": "requestFailed", "data": {"reason": str(exc)}}},
                        rid,
                    )
                )

    def _fetch(self, body: dict[str, Any], rid: str) -> None:
        request = body.get("request") if isinstance(body.get("request"), dict) else None
        if request is None:
            self.send(
                envelope(
                    "response",
                    "net",
                    "fetchErrorReply",
                    {"error": {"type": "requestFailed", "data": {"reason": "missing request"}}},
                    rid,
                )
            )
            return
        result = fetch(request)
        if not result.get("ok") or result.get("truncated"):
            error = result.get("error") or {"type": "requestFailed", "data": {"reason": "response too large"}}
            self.send(envelope("response", "net", "fetchErrorReply", {"error": error}, rid))
            return
        self.send(
            envelope(
                "response",
                "net",
                "fetchReply",
                {"response": {"status": result["status"], "headers": result["headers"], "body": result["body"]}},
                rid,
            )
        )

    def _stream_event(self, event: str, body: dict[str, Any]) -> None:
        self.send(envelope("event", "net", event, body))

    def _ws_open(self, body: dict[str, Any], rid: str) -> None:
        connection_id = str(body.get("connectionId") or "")
        url = str(body.get("url") or "")
        if not url.startswith(("ws://", "wss://")):
            self.send(
                envelope(
                    "response",
                    "net",
                    "wsErrorReply",
                    {"error": {"type": "connectFailed", "data": {"reason": "only ws and wss are proxied"}}},
                    rid,
                )
            )
            return
        try:
            host, port, secure, path = _split_ws(url)
            header_map = {}
            if isinstance(body.get("headers"), list):
                for item in body["headers"]:
                    if isinstance(item, dict) and item.get("name"):
                        header_map[str(item["name"])] = str(item.get("value") or "")
            protocols = body.get("protocols") if isinstance(body.get("protocols"), list) else None
            upstream = connect(host, port, tls=secure, path=path, headers=header_map, protocols=protocols)
        except Exception as exc:  # noqa: BLE001
            self.send(
                envelope(
                    "response",
                    "net",
                    "wsErrorReply",
                    {"error": {"type": "connectFailed", "data": {"reason": str(exc)}}},
                    rid,
                )
            )
            return
        self.upstreams[connection_id] = upstream
        reply: dict[str, Any] = {}
        if upstream.protocol:
            reply["acceptedProtocol"] = upstream.protocol
        self.send(envelope("response", "net", "wsOpenReply", reply, rid))
        threading.Thread(target=self._ws_read, args=(connection_id, upstream), daemon=True).start()

    def _ws_read(self, connection_id: str, upstream: WebSocket) -> None:
        try:
            while self.upstreams.get(connection_id) is upstream:
                opcode, payload = upstream.recv()
                frame: dict[str, Any]
                if opcode == 1:
                    frame = {"type": "text", "data": payload.decode("utf-8", "replace")}
                else:
                    frame = {"type": "binary", "data": payload}
                self.send(envelope("event", "net", "wsMessage", {"connectionId": connection_id, "frame": frame}))
        except Exception as exc:  # noqa: BLE001
            self.upstreams.pop(connection_id, None)
            self.send(
                envelope(
                    "event",
                    "net",
                    "wsClosed",
                    {"connectionId": connection_id, "code": 1006, "reason": str(exc)},
                )
            )

    def _ws_send(self, body: dict[str, Any]) -> None:
        upstream = self.upstreams.get(str(body.get("connectionId") or ""))
        frame = body.get("frame") if isinstance(body.get("frame"), dict) else {}
        if upstream is None or not isinstance(frame, dict):
            return
        if frame.get("type") == "text":
            upstream.send_text(str(frame.get("data") or ""))
        else:
            upstream.send(as_bytes(frame.get("data")))

    def _ws_close(self, body: dict[str, Any]) -> None:
        connection_id = str(body.get("connectionId") or "")
        upstream = self.upstreams.pop(connection_id, None)
        if upstream is not None:
            upstream.close()
        self.send(
            envelope(
                "event",
                "net",
                "wsClosed",
                {"connectionId": connection_id, "code": int(body.get("code") or 1000), "reason": str(body.get("reason") or "")},
            )
        )

    def _tunnel(self, event: str, body: dict[str, Any], rid: str) -> None:
        tunnel_id = str(body.get("tunnelId") or "")
        if event == "open":
            try:
                sock = connect_tunnel(str(body.get("host") or ""), int(body.get("port") or 0))
            except OSError as exc:
                self.send(
                    envelope(
                        "response",
                        "tunnel",
                        "errorReply",
                        {"error": {"type": "connectFailed", "data": {"reason": str(exc)}}},
                        rid,
                    )
                )
                return
            self.send(envelope("response", "tunnel", "openReply", {}, rid))
            self.tunnels[tunnel_id] = Tunnel(tunnel_id, sock, self._tunnel_event)
            return
        tunnel = self.tunnels.get(tunnel_id)
        if tunnel is None:
            return
        if event == "data":
            tunnel.write(as_bytes(body.get("bytes")))
        elif event == "close":
            self.tunnels.pop(tunnel_id, None)
            tunnel.close(str(body.get("reason") or "closed"))

    def _tunnel_event(self, event: str, body: dict[str, Any]) -> None:
        self.send(envelope("event", "tunnel", event, body))
        if event == "closed":
            self.tunnels.pop(str(body.get("tunnelId") or ""), None)


def _split_ws(url: str) -> tuple[str, int, bool, str]:
    secure = url.startswith("wss://")
    rest = url.split("://", 1)[1]
    slash = rest.find("/")
    hostport = rest if slash < 0 else rest[:slash]
    path = "/" if slash < 0 else rest[slash:]
    if hostport.startswith("["):
        host, _, port_text = hostport[1:].rpartition("]")
        port_text = port_text[1:] if port_text.startswith(":") else ""
    elif ":" in hostport:
        host, port_text = hostport.rsplit(":", 1)
    else:
        host, port_text = hostport, ""
    port = int(port_text) if port_text else (443 if secure else 80)
    return host, port, secure, path


def probe(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def find_gateway(hosts: list[str]) -> str | None:
    for host in hosts:
        if probe(host, GATEWAY_PORT):
            return host
    return None


def client_port_open(host: str) -> bool:
    return probe(host, CLIENT_PORT)
