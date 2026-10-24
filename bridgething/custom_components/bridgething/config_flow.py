"""Config flow for the Car Thing companion.

Home Assistant Core is a container. The app uses the host network so it can
see the USB device, and Core reaches that host through the Docker bridge
gateway, not through the host's loopback.

Spotify login uses Spotify's own desktop client, so the companion can open a
Connect session. The user pastes the redirect address. The Car Thing never
sees the token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow, SOURCE_REAUTH
from homeassistant.core import callback
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
from homeassistant.helpers.selector import selector

from .const import API_PORT, CONF_SOURCE, CONF_URL, DEFAULT_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)
_PING_ERRORS = (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError, ConnectionError)
# Spotify's public desktop client. A dashboard app cannot open a Connect session.
DESKTOP_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
CONNECT_REDIRECT = "http://127.0.0.1:8888/login"
SPOTIFY_SCOPE = " ".join(
    (
        "streaming",
        "user-read-playback-state",
        "user-modify-playback-state",
        "user-read-private",
        "user-library-read",
        "user-library-modify",
        "user-follow-read",
        "user-read-recently-played",
        "playlist-read-private",
        "playlist-read-collaborative",
    )
)


def docker_host_url(port: int = API_PORT) -> str:
    """URL of the host as seen from this container."""
    return f"http://{_default_gateway() or '172.30.32.1'}:{port}"


def _default_gateway() -> str | None:
    try:
        with open("/proc/net/route", encoding="ascii") as handle:
            next(handle, None)
            for line in handle:
                fields = line.split()
                if len(fields) < 3 or fields[1] != "00000000" or len(fields[2]) != 8:
                    continue
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError):
        return None
    return None


def _authorize_url(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    query = urllib.parse.urlencode(
        {
            "client_id": DESKTOP_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": CONNECT_REDIRECT,
            "scope": SPOTIFY_SCOPE,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "show_dialog": "true",
        }
    )
    return "https://accounts.spotify.com/authorize?" + query


def _auth_code(pasted: str) -> str | None:
    text = pasted.strip()
    if not text:
        return None
    if "code=" in text or "error=" in text:
        query = urllib.parse.urlparse(text).query
        if not query and "?" in text:
            query = text.split("?", 1)[1]
        parsed = urllib.parse.parse_qs(query)
        if parsed.get("error"):
            return None
        code = (parsed.get("code") or [None])[0]
        return code if isinstance(code, str) and code else None
    if " " not in text and "://" not in text:
        return text
    return None


def _spotify_user_id(access_token: str) -> str | None:
    """Spotify account id. Official Bridgething never uses display_name."""
    request = urllib.request.Request(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    user_id = payload.get("id") if isinstance(payload, dict) else None
    return user_id if isinstance(user_id, str) and user_id else None


def _exchange_code(verifier: str, code: str) -> dict[str, Any]:
    request = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CONNECT_REDIRECT,
                "client_id": DESKTOP_CLIENT_ID,
                "code_verifier": verifier,
            }
        ).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ConnectionError("spotify rejected the login code")
    return payload


def _ping(url: str) -> None:
    request = urllib.request.Request(url.rstrip("/") + "/state")
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ConnectionError("unexpected response")


def _candidate_urls(preferred: str) -> list[str]:
    urls: list[str] = []
    for url in (preferred, docker_host_url(), DEFAULT_URL, f"http://host.docker.internal:{API_PORT}"):
        cleaned = url.rstrip("/")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    return urls


def _first_reachable(preferred: str) -> str | None:
    for url in _candidate_urls(preferred):
        try:
            _ping(url)
        except _PING_ERRORS:
            continue
        return url
    return None


class BridgethingConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """One config entry is one Car Thing."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, str]:
        return {"scope": SPOTIFY_SCOPE, "show_dialog": "true"}

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            found = await self.hass.async_add_executor_job(_first_reachable, user_input[CONF_URL])
            if found is None:
                errors["base"] = "cannot_connect"
            else:
                self._url = found
                self._source = user_input.get(CONF_SOURCE) or ""
                return await self.async_step_spotify()
        default_url = getattr(self, "_discovered_url", None) or docker_host_url()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=default_url): str,
                    vol.Optional(CONF_SOURCE): selector({"entity": {"domain": "media_player"}}),
                }
            ),
            errors=errors,
        )

    async def async_step_spotify(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="spotify",
            menu_options=["connect", "skip_spotify"],
        )

    async def async_step_connect(self, user_input: dict | None = None) -> ConfigFlowResult:
        verifier = getattr(self, "_pkce", None)
        if not isinstance(verifier, str) or not verifier:
            verifier = secrets.token_urlsafe(64)
            self._pkce = verifier
        errors = {}
        if user_input is not None:
            pasted = str(user_input.get("redirect") or "")
            fallback = str(user_input.get("login_url") or "")
            code = _auth_code(pasted) or _auth_code(fallback)
            if not code:
                errors["base"] = "invalid_auth"
            else:
                try:
                    token = await self.hass.async_add_executor_job(_exchange_code, verifier, code)
                except _PING_ERRORS:
                    errors["base"] = "oauth_failed"
                else:
                    expires_in = token.get("expires_in")
                    lifetime = float(expires_in) if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) else 3600
                    access = token.get("access_token") or ""
                    username = ""
                    if access:
                        try:
                            username = await self.hass.async_add_executor_job(_spotify_user_id, access) or ""
                        except _PING_ERRORS:
                            username = ""
                    payload = self._companion_data(
                        {
                            "token": {
                                "access_token": access,
                                "refresh_token": token.get("refresh_token") or "",
                                "expires_at": time.time() + lifetime,
                            },
                            "username": username,
                        }
                    )
                    return await self._finish_spotify(payload)
        login_url = _authorize_url(verifier)
        return self.async_show_form(
            step_id="connect",
            data_schema=vol.Schema(
                {
                    vol.Optional("login_url", description={"suggested_value": login_url}): str,
                    vol.Required("redirect"): str,
                }
            ),
            description_placeholders={"authorize_url": login_url},
            errors=errors,
        )

    async def async_step_skip_spotify(self, user_input: dict | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={CONF_URL: self._url})
        return self.async_create_entry(title="Car Thing", data=self._companion_data({}))

    async def async_step_hassio(self, discovery_info: dict) -> ConfigFlowResult:
        host = discovery_info.get("host")
        port = discovery_info.get("port", API_PORT)
        preferred = f"http://{host}:{port}" if host else docker_host_url(port)
        found = await self.hass.async_add_executor_job(_first_reachable, preferred)
        if found is None:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={CONF_URL: found})
        self._discovered_url = found
        return await self.async_step_user()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._url = entry_data.get(CONF_URL) or DEFAULT_URL
        self._source = entry_data.get(CONF_SOURCE) or ""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))
        return await self.async_step_connect()

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        self._url = entry.options.get(CONF_URL) or entry.data.get(CONF_URL) or DEFAULT_URL
        self._source = entry.options.get(CONF_SOURCE) or entry.data.get(CONF_SOURCE) or ""
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema({}))
        return await self.async_step_connect()

    async def _finish_spotify(self, payload: dict[str, Any]) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=payload)
        if self.source == "reconfigure":
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data_updates=payload)
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={CONF_URL: payload[CONF_URL]})
        return self.async_create_entry(title="Car Thing", data=payload)

    def _companion_data(self, data: dict[str, Any]) -> dict[str, Any]:
        payload = dict(data)
        payload[CONF_URL] = getattr(self, "_url", None) or payload.get(CONF_URL) or DEFAULT_URL
        payload[CONF_SOURCE] = getattr(self, "_source", "") or payload.get(CONF_SOURCE) or ""
        return payload

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BridgethingOptionsFlow:
        return BridgethingOptionsFlow()


class BridgethingOptionsFlow(OptionsFlow):
    """Change the app URL and the media player that owns the sound."""

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            found = await self.hass.async_add_executor_job(_first_reachable, user_input[CONF_URL])
            if found is None:
                return self.async_show_form(step_id="init", data_schema=self._schema(), errors={"base": "cannot_connect"})
            return self.async_create_entry(
                title="",
                data={CONF_URL: found, CONF_SOURCE: user_input.get(CONF_SOURCE) or ""},
            )
        return self.async_show_form(step_id="init", data_schema=self._schema())

    def _schema(self) -> vol.Schema:
        source = self.config_entry.options.get(CONF_SOURCE) or self.config_entry.data.get(CONF_SOURCE) or ""
        url = self.config_entry.options.get(CONF_URL) or self.config_entry.data.get(CONF_URL) or docker_host_url()
        return vol.Schema(
            {
                vol.Required(CONF_URL, default=url): str,
                vol.Optional(CONF_SOURCE, description={"suggested_value": source}): selector(
                    {"entity": {"domain": "media_player"}}
                ),
            }
        )
