"""Nickname, Bluetooth alias, and the URL opened by the browser button."""

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BridgethingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data["bridgething"][entry.entry_id]
    async_add_entities([NicknameText(coordinator), AliasText(coordinator), NavigateText(coordinator)])


class _QuietText(BridgethingEntity, TextEntity):
    _attr_mode = TextMode.TEXT

    def __init__(self, coordinator, key: str, name: str, path: str, field: str, state_key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._path = path
        self._field = field
        self._state_key = state_key

    @property
    def native_value(self) -> str:
        value = (self.coordinator.data or {}).get(self._state_key)
        return "" if value is None else str(value)

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.async_post(self._path, {self._field: value})


class NicknameText(_QuietText):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "nickname", "Nickname", "/nickname", "nickname", "nickname")

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        return self.coordinator.last_update_success and bool(data.get("gatewayConnected") or data.get("clientConnected"))


class AliasText(_QuietText):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "bluetooth_alias", "Bluetooth alias", "/bluetooth/alias", "name", "bluetoothAlias")


class NavigateText(_QuietText):
    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "navigate_url", "Browser URL", "/navigate/url", "url", "navigateUrl")

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool((self.coordinator.data or {}).get("gatewayConnected"))
