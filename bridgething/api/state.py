"""Shared view of the Car Thing for the sockets and the HTTP API."""

from __future__ import annotations

import json
import threading
from typing import Any

from .playback import wire_player_state
from .protocol import dumps


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.playback_event = threading.Event()
        self.playback_generation = 0
        self._wire = ""
        self._picture: str | None = None
        self._data: dict[str, Any] = {
            "ncm": False,
            "addresses": [],
            "gatewayHost": None,
            "gatewayConnected": False,
            "clientConnected": False,
            "device": {},
            "hardware": {},
            "webapps": [],
            "activeWebapp": None,
            "activeWebappName": None,
            "diagnostics": {},
            "bluetoothDevices": [],
            "bluetoothSelection": None,
            "bluetoothConnected": None,
            "discoverable": False,
            "bluetoothAlias": "",
            "launcherGesture": None,
            "companionLaunch": None,
            "nickname": None,
            "playback": None,
            "sourceEntity": "",
            "logLine": None,
            "navigateUrl": "",
            "profile": None,
        }

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            self._data.update(kwargs)

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return self._data.get(key, default)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self._data, default=str))

    def apply_meta(self, meta: dict[str, Any]) -> None:
        if not isinstance(meta, dict):
            return
        device = dict(self.get("device") or {})
        device.update({key: value for key, value in meta.items() if value is not None})
        changes: dict[str, Any] = {"device": device}
        if "nickname" in meta:
            changes["nickname"] = meta.get("nickname")
        if meta.get("launcherGesture"):
            changes["launcherGesture"] = meta.get("launcherGesture")
        if "companionLaunch" in meta:
            changes["companionLaunch"] = meta.get("companionLaunch")
        self.update(**changes)

    def set_source(self, entity_id: str) -> None:
        entity_id = (entity_id or "").strip()
        self.update(sourceEntity=entity_id)
        if not entity_id:
            from .playback import player_state_from_ha

            self.set_playback(player_state_from_ha("", None))

    def set_playback(self, player: dict[str, Any]) -> int:
        wire = dumps(wire_player_state(player))
        picture = player.get("picture")
        entity = str(player.get("entityId") or "")
        with self.lock:
            same = (
                wire == self._wire
                and picture == self._picture
                and entity == str(self._data.get("sourceEntity") or "")
            )
            self._data["playback"] = player
            if entity:
                self._data["sourceEntity"] = entity
            if same:
                return self.playback_generation
            self._wire = wire
            self._picture = picture
            self.playback_generation += 1
            generation = self.playback_generation
        self.playback_event.set()
        return generation

    def playback_view(self) -> tuple[int, dict[str, Any] | None, str]:
        with self.lock:
            playback = self._data.get("playback")
            if isinstance(playback, dict):
                playback = json.loads(json.dumps(playback, default=str))
            else:
                playback = None
            return self.playback_generation, playback, str(self._data.get("sourceEntity") or "")
