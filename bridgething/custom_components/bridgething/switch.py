"""Whether the device opens the companion app when an iPhone reconnects."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BridgethingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([CompanionLaunchSwitch(hass.data["bridgething"][entry.entry_id])])


class CompanionLaunchSwitch(BridgethingEntity, SwitchEntity):
    _attr_name = "Open companion on reconnect"
    _attr_icon = "mdi:cellphone-link"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "companion_launch")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("companionLaunch"))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_post("/companion_launch", {"enabled": True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_post("/companion_launch", {"enabled": False})
