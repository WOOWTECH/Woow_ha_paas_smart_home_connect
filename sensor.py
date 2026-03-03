"""Sensor entities for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WoowConfigEntry
from .const import CONF_HOME_NAME, DOMAIN, TunnelStatus
from .coordinator import TunnelCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WoowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Woow PaaS Smart Home sensor entities."""
    coordinator = entry.runtime_data.coordinator

    async_add_entities([
        TunnelStatusSensor(coordinator, entry),
        TunnelUrlSensor(coordinator, entry),
    ])


class TunnelStatusSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """Sensor showing the tunnel connection status text."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:tunnel"
    _attr_has_entity_name = True
    _attr_options = [status.value for status in TunnelStatus]
    _attr_translation_key = "tunnel_status"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tunnel_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_HOME_NAME, "Woow Smart Home"),
            manufacturer="Woow",
            model="PaaS Smart Home",
        )

    @property
    def native_value(self) -> str | None:
        """Return the tunnel status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.status


class TunnelUrlSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """Sensor showing the tunnel subdomain URL."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:web"
    _attr_has_entity_name = True
    _attr_translation_key = "tunnel_url"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tunnel_url"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_HOME_NAME, "Woow Smart Home"),
            manufacturer="Woow",
            model="PaaS Smart Home",
        )

    @property
    def native_value(self) -> str | None:
        """Return the tunnel subdomain URL."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.subdomain_url
