"""Polls the companion app and pushes the chosen media player to it."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .config_flow import _spotify_user_id
from .const import CONF_SOURCE, CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


class BridgethingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """State of one Car Thing, plus the media-player subscription that feeds its screen."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.entry = entry
        self._unsub = None
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=5))

    @property
    def url(self) -> str:
        stored = self.entry.options.get(CONF_URL) or self.entry.data[CONF_URL]
        return str(stored).rstrip("/")

    def source_entity(self) -> str:
        return str(self.entry.options.get(CONF_SOURCE) or self.entry.data.get(CONF_SOURCE) or "")

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.hass.async_add_executor_job(self._get, "/state")
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    def _get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(self.url + path)
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise UpdateFailed("the companion returned an unexpected payload")
        return payload

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")
            raise HomeAssistantError(detail or err.reason) from err
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}

    async def async_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result = await self.hass.async_add_executor_job(self._post, path, body)
        await self.async_request_refresh()
        return result

    async def async_push_spotify(self) -> None:
        """Give the companion the Spotify login. The companion refreshes it, and the Car Thing never sees it."""
        token = self.entry.data.get("token")
        if not isinstance(token, dict) or not token.get("access_token"):
            return
        username = self.entry.data.get("username")
        if not isinstance(username, str) or not username:
            try:
                username = await self.hass.async_add_executor_job(
                    _spotify_user_id, token.get("access_token") or ""
                )
            except Exception:
                username = None
        body = {
            "access_token": token.get("access_token") or "",
            "refresh_token": token.get("refresh_token") or "",
            "expires_at": token.get("expires_at") or 0,
            "client_id": "65b708073fc0480ea92a077233ca87bd",
        }
        if isinstance(username, str) and username:
            body["username"] = username
        try:
            await self.hass.async_add_executor_job(self._post, "/spotify/credentials", body)
        except Exception:
            _LOGGER.exception("Could not give the companion its Spotify token")

    async def async_watch_source(self) -> None:
        self.async_unwatch()
        entity_id = self.source_entity()
        await self.async_push_spotify()
        await self.hass.async_add_executor_job(self._post, "/playback/source", {"entity_id": entity_id})
        if not entity_id:
            return
        self._unsub = async_track_state_change_event(self.hass, [entity_id], self._on_player)
        state = self.hass.states.get(entity_id)
        if state is not None:
            await self._push_player(state)

    @callback
    def async_unwatch(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _on_player(self, event) -> None:
        new_state = event.data.get("new_state")
        if new_state is not None:
            self.hass.async_create_task(self._push_player(new_state))

    async def _push_player(self, state: State) -> None:
        try:
            await self.hass.async_add_executor_job(
                self._post,
                "/playback/state",
                {
                    "entity_id": state.entity_id,
                    "state": state.state,
                    "attributes": _jsonable(dict(state.attributes)),
                },
            )
        except Exception:
            _LOGGER.exception("Could not push %s to the Car Thing", state.entity_id)
