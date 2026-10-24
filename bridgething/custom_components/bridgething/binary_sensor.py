"""Link status for the USB lease and both sockets."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import BridgethingCoordinator
from .entity import BridgethingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([LinkSensor(hass.data["bridgething"][entry.entry_id])])


class LinkSensor(BridgethingEntity, BinarySensorEntity):
    _attr_name = "Link"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: BridgethingCoordinator) -> None:
        super().__init__(coordinator, "link")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return bool(data.get("ncm") and data.get("gatewayConnected") and data.get("clientConnected"))

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "ncm": bool(data.get("ncm")),
            "gateway": bool(data.get("gatewayConnected")),
            "client": bool(data.get("clientConnected")),
            "host": data.get("gatewayHost"),
        }
