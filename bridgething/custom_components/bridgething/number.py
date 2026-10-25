"""Set-backlight slider. Moving it switches the panel to manual mode."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import set_brightness
from .entity import BridgethingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([BacklightNumber(hass.data["bridgething"][entry.entry_id])])


class BacklightNumber(BridgethingEntity, NumberEntity):
    _attr_name = "Set backlight"
    _attr_icon = "mdi:brightness-6"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "backlight")

    @property
    def native_value(self) -> float | None:
        return set_brightness((self.coordinator.data or {}).get("hardware"))

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_post("/brightness", {"value": value})
