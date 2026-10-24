"""Selects for backlight mode, the full-screen app, and the quieter device options."""

from __future__ import annotations

from collections import Counter

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BridgethingEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data["bridgething"][entry.entry_id]
    async_add_entities([BrightnessModeSelect(coordinator), WebappSelect(coordinator), GestureSelect(coordinator), BluetoothSelect(coordinator)])


class BrightnessModeSelect(BridgethingEntity, SelectEntity):
    _attr_name = "Backlight mode"
    _attr_icon = "mdi:brightness-auto"
    _attr_options = ["auto", "manual"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "brightness_mode")

    @property
    def current_option(self) -> str | None:
        brightness = ((self.coordinator.data or {}).get("hardware") or {}).get("brightness") or {}
        mode = brightness.get("mode")
        return mode if mode in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_post("/brightness/mode", {"mode": option})


class WebappSelect(BridgethingEntity, SelectEntity):
    _attr_name = "Webapp"
    _attr_icon = "mdi:apps"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "webapp")
        self._ids: dict[str, str] = {}

    @property
    def options(self) -> list[str]:
        labels, self._ids = _webapp_labels((self.coordinator.data or {}).get("webapps") or [])
        return labels

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        connected = bool(data.get("clientConnected") or data.get("gatewayConnected"))
        return self.coordinator.last_update_success and connected and bool(self.options)

    @property
    def current_option(self) -> str | None:
        labels, ids = _webapp_labels((self.coordinator.data or {}).get("webapps") or [])
        active = (self.coordinator.data or {}).get("activeWebapp")
        for label, webapp_id in ids.items():
            if webapp_id == active:
                return label
        name = (self.coordinator.data or {}).get("activeWebappName")
        if name in labels:
            return name
        return None

    async def async_select_option(self, option: str) -> None:
        _labels, ids = _webapp_labels((self.coordinator.data or {}).get("webapps") or [])
        webapp_id = ids.get(option)
        if webapp_id:
            await self.coordinator.async_post("/webapps/active", {"id": webapp_id})


class GestureSelect(BridgethingEntity, SelectEntity):
    _attr_name = "M button gesture"
    _attr_options = ["fivePress", "longPress"]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "launcher_gesture")

    @property
    def current_option(self) -> str | None:
        gesture = (self.coordinator.data or {}).get("launcherGesture") or "fivePress"
        return gesture if gesture in self._attr_options else "fivePress"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_post("/launcher_gesture", {"gesture": option})


class BluetoothSelect(BridgethingEntity, SelectEntity):
    _attr_name = "Paired Bluetooth device"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "bluetooth_device")

    @property
    def options(self) -> list[str]:
        return [item["mac"] for item in (self.coordinator.data or {}).get("bluetoothDevices") or [] if item.get("mac")]

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    @property
    def current_option(self) -> str | None:
        selected = (self.coordinator.data or {}).get("bluetoothSelection")
        if selected in self.options:
            return selected
        connected = (self.coordinator.data or {}).get("bluetoothConnected")
        if connected in self.options:
            return connected
        return None

    @property
    def extra_state_attributes(self) -> dict:
        names = {item.get("mac"): item.get("name") for item in (self.coordinator.data or {}).get("bluetoothDevices") or []}
        return {"names": names}

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_post("/bluetooth/select", {"mac": option})


def _webapp_labels(webapps: list) -> tuple[list[str], dict[str, str]]:
    names = [str(app.get("name") or app.get("id") or "") for app in webapps if isinstance(app, dict)]
    counts = Counter(names)
    labels: list[str] = []
    ids: dict[str, str] = {}
    for app in webapps:
        if not isinstance(app, dict) or not app.get("id"):
            continue
        name = str(app.get("name") or app.get("id"))
        label = name if counts[name] == 1 else f"{name} ({app['id']})"
        labels.append(label)
        ids[label] = str(app["id"])
    return labels, ids
