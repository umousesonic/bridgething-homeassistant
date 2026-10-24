"""Shared device identity for every Car Thing entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BridgethingCoordinator


class BridgethingEntity(CoordinatorEntity[BridgethingCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: BridgethingCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data or {}
        device = data.get("device") or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.entry.entry_id)},
            name=device.get("nickname") or "Car Thing",
            manufacturer="Spotify",
            model=device.get("modelName") or "Car Thing",
            sw_version=device.get("imageVersion"),
            serial_number=device.get("serialNumber"),
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool((self.coordinator.data or {}).get("clientConnected"))
