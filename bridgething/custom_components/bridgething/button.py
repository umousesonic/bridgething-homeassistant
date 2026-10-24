"""Power, webapp navigation, Bluetooth, and factory reset buttons."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BridgethingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data["bridgething"][entry.entry_id]
    async_add_entities(
        [
            CommandButton(coordinator, "reboot", "Reboot", "/reboot", "mdi:restart", None),
            CommandButton(coordinator, "power_off", "Power off", "/power_off", "mdi:power", None),
            CommandButton(coordinator, "factory_reset", "Factory reset", "/factory_reset", "mdi:delete-forever", EntityCategory.CONFIG),
            CommandButton(coordinator, "bluetooth_connect", "Connect Bluetooth", "/bluetooth/connect", "mdi:bluetooth-connect", None),
            CommandButton(coordinator, "bluetooth_forget", "Forget Bluetooth", "/bluetooth/forget", "mdi:bluetooth-off", None),
            DiscoverableButton(coordinator),
            NavigateButton(coordinator),
        ]
    )


class CommandButton(BridgethingEntity, ButtonEntity):
    def __init__(self, coordinator, key: str, name: str, path: str, icon: str, category: EntityCategory | None) -> None:
        super().__init__(coordinator, key)
        self._path = path
        self._attr_name = name
        self._attr_icon = icon
        if category is not None:
            self._attr_entity_category = category

    async def async_press(self) -> None:
        body = {}
        if self._path == "/bluetooth/connect":
            body["mac"] = (self.coordinator.data or {}).get("bluetoothSelection") or ""
        elif self._path == "/bluetooth/forget":
            body["mac"] = (self.coordinator.data or {}).get("bluetoothSelection") or ""
        await self.coordinator.async_post(self._path, body)


class DiscoverableButton(BridgethingEntity, ButtonEntity):
    _attr_name = "Bluetooth discoverable"
    _attr_icon = "mdi:bluetooth-settings"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "bluetooth_discoverable")

    async def async_press(self) -> None:
        enabled = not bool((self.coordinator.data or {}).get("discoverable"))
        await self.coordinator.async_post("/bluetooth/discoverable", {"enabled": enabled})


class NavigateButton(BridgethingEntity, ButtonEntity):
    _attr_name = "Open URL"
    _attr_icon = "mdi:web"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "navigate")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool((self.coordinator.data or {}).get("gatewayConnected"))

    async def async_press(self) -> None:
        url = (self.coordinator.data or {}).get("navigateUrl") or ""
        await self.coordinator.async_post("/navigate", {"url": url})
