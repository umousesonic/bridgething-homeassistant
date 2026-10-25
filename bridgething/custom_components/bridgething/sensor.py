"""Sensors for light, health, and identity. Most stay off the dashboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import current_brightness
from .coordinator import BridgethingCoordinator
from .entity import BridgethingEntity


def _hardware(data: dict) -> dict:
    return data.get("hardware") or {}


def _diagnostics(data: dict) -> dict:
    return data.get("diagnostics") or {}


def _device(data: dict) -> dict:
    return data.get("device") or {}


def _load(data: dict):
    load = _diagnostics(data).get("loadAvg")
    if isinstance(load, list) and load:
        return load[0]
    return None


def _clip(value):
    if value is None:
        return None
    return str(value)[:255]


@dataclass(frozen=True, slots=True)
class SensorDesc:
    key: str
    name: str
    value: Callable[[dict], object]
    enabled: bool = False
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None


def _current_app(data: dict):
    name = data.get("activeWebappName")
    if name:
        return _clip(name)
    active = data.get("activeWebapp")
    for app in data.get("webapps") or []:
        if isinstance(app, dict) and app.get("id") == active:
            return _clip(app.get("name") or active)
    return _clip(active)


SENSORS = (
    SensorDesc("current_app", "Current app", _current_app, True),
    SensorDesc("spotify_account", "Spotify account", lambda data: _clip((data.get("spotify") or {}).get("username")), True),
    SensorDesc("ambient", "Ambient light", lambda data: _hardware(data).get("ambientLevel"), True, "%", None, SensorStateClass.MEASUREMENT),
    SensorDesc(
        "effective_backlight",
        "Backlight",
        lambda data: current_brightness(_hardware(data)),
        True,
        "%",
        None,
        SensorStateClass.MEASUREMENT,
    ),
    SensorDesc(
        "soc_temperature",
        "SoC temperature",
        lambda data: _diagnostics(data).get("socTempC"),
        False,
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
    ),
    SensorDesc(
        "disk_used",
        "Disk used",
        lambda data: _diagnostics(data).get("diskUsedBytes"),
        False,
        UnitOfInformation.BYTES,
        SensorDeviceClass.DATA_SIZE,
        SensorStateClass.MEASUREMENT,
    ),
    SensorDesc(
        "memory_used",
        "Memory used",
        lambda data: _diagnostics(data).get("memUsedBytes"),
        False,
        UnitOfInformation.BYTES,
        SensorDeviceClass.DATA_SIZE,
        SensorStateClass.MEASUREMENT,
    ),
    SensorDesc(
        "uptime",
        "Uptime",
        lambda data: _diagnostics(data).get("uptimeS"),
        False,
        UnitOfTime.SECONDS,
        SensorDeviceClass.DURATION,
        SensorStateClass.MEASUREMENT,
    ),
    SensorDesc("load", "Load average", _load, False, None, None, SensorStateClass.MEASUREMENT),
    SensorDesc("image_version", "Image version", lambda data: _clip(_device(data).get("imageVersion"))),
    SensorDesc("serial", "Serial number", lambda data: _clip(_device(data).get("serialNumber"))),
    SensorDesc("bluetooth_mac", "Bluetooth MAC", lambda data: _clip(_device(data).get("btMac"))),
    SensorDesc("bluetooth_name", "Bluetooth name", lambda data: _clip(_device(data).get("bluetoothName"))),
    SensorDesc("log_line", "Latest log", lambda data: _clip(data.get("logLine"))),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data["bridgething"][entry.entry_id]
    async_add_entities(ThingSensor(coordinator, desc) for desc in SENSORS)


class ThingSensor(BridgethingEntity, SensorEntity):
    def __init__(self, coordinator: BridgethingCoordinator, desc: SensorDesc) -> None:
        super().__init__(coordinator, desc.key)
        self._desc = desc
        self._attr_name = desc.name
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class = desc.device_class
        self._attr_state_class = desc.state_class
        self._attr_entity_registry_enabled_default = desc.enabled
        if desc.key != "current_app" and not desc.enabled:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        if self._desc.key == "current_app":
            data = self.coordinator.data or {}
            connected = bool(data.get("clientConnected") or data.get("gatewayConnected"))
            return self.coordinator.last_update_success and connected
        return super().available

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self._desc.value(self.coordinator.data)
