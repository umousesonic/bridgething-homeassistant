"""Bridgething device integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS
from .coordinator import BridgethingCoordinator

_BRIGHTNESS = vol.Schema({vol.Required("value"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100))})
_MODE = vol.Schema({vol.Required("mode"): vol.In(["auto", "manual"])})
_WEBAPP = vol.Schema({vol.Required("webapp_id"): cv.string})
_URL = vol.Schema({vol.Required("url"): cv.string})
_NICKNAME = vol.Schema({vol.Required("nickname"): cv.string})
_EMPTY = vol.Schema({})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    async def coordinator() -> BridgethingCoordinator:
        entries: dict = hass.data.get(DOMAIN) or {}
        if not entries:
            raise HomeAssistantError("No Car Thing is configured")
        return next(iter(entries.values()))

    async def set_brightness(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/brightness", {"value": call.data["value"]})

    async def set_brightness_mode(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/brightness/mode", {"mode": call.data["mode"]})

    async def switch_webapp(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/webapps/active", {"id": call.data["webapp_id"]})

    async def navigate(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/navigate", {"url": call.data["url"]})

    async def reboot(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/reboot", {})

    async def power_off(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/power_off", {})

    async def set_nickname(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/nickname", {"nickname": call.data["nickname"]})

    async def factory_reset(call: ServiceCall) -> None:
        await (await coordinator()).async_post("/factory_reset", {})

    hass.services.async_register(DOMAIN, "set_brightness", set_brightness, schema=_BRIGHTNESS)
    hass.services.async_register(DOMAIN, "set_brightness_mode", set_brightness_mode, schema=_MODE)
    hass.services.async_register(DOMAIN, "switch_webapp", switch_webapp, schema=_WEBAPP)
    hass.services.async_register(DOMAIN, "navigate", navigate, schema=_URL)
    hass.services.async_register(DOMAIN, "reboot", reboot, schema=_EMPTY)
    hass.services.async_register(DOMAIN, "power_off", power_off, schema=_EMPTY)
    hass.services.async_register(DOMAIN, "set_nickname", set_nickname, schema=_NICKNAME)
    hass.services.async_register(DOMAIN, "factory_reset", factory_reset, schema=_EMPTY)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = BridgethingCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _enable_controls(hass, entry)
    await coordinator.async_watch_source()
    entry.async_on_unload(entry.add_update_listener(_reload))
    entry.async_on_unload(coordinator.async_unwatch)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: BridgethingCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    coordinator.async_unwatch()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _enable_controls(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Turn on controls that an older build created disabled."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            registry.async_update_entity(entity.entity_id, disabled_by=None)


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
