"""Config flow for the Car Thing companion.

Home Assistant Core is a container. The app uses the host network so it can
see the USB device, and Core reaches that host through the Docker bridge
gateway, not through the host's loopback.
"""

from __future__ import annotations

import json
import socket
import struct
import urllib.error
import urllib.request

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import API_PORT, CONF_SOURCE, CONF_URL, DEFAULT_URL, DOMAIN

_PING_ERRORS = (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError, ConnectionError)


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


class BridgethingConfigFlow(ConfigFlow, domain=DOMAIN):
    """One config entry is one Car Thing."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        errors = {}
        if user_input is not None:
            found = await self.hass.async_add_executor_job(_first_reachable, user_input[CONF_URL])
            if found is None:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured(updates={CONF_URL: found})
                return self.async_create_entry(
                    title="Car Thing",
                    data={CONF_URL: found, CONF_SOURCE: user_input.get(CONF_SOURCE) or ""},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=docker_host_url()): str,
                    vol.Optional(CONF_SOURCE): selector({"entity": {"domain": "media_player"}}),
                }
            ),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: dict):
        host = discovery_info.get("host")
        port = discovery_info.get("port", API_PORT)
        preferred = f"http://{host}:{port}" if host else docker_host_url(port)
        found = await self.hass.async_add_executor_job(_first_reachable, preferred)
        if found is None:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured(updates={CONF_URL: found})
        return self.async_create_entry(title="Car Thing", data={CONF_URL: found, CONF_SOURCE: ""})

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
