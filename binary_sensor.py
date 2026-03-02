"""Binary sensor entities for Woow PaaS Smart Home tunnel connectivity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HOME_NAME, DOMAIN
from .coordinator import TunnelCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Woow PaaS Smart Home binary sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: TunnelCoordinator = data["coordinator"]

    async_add_entities([
        TunnelConnectedBinarySensor(coordinator, entry),
    ])


class TunnelConnectedBinarySensor(
    CoordinatorEntity[TunnelCoordinator], BinarySensorEntity
):
    """Binary sensor indicating whether the tunnel is connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "tunnel_connected"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tunnel_connected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_HOME_NAME, "Woow Smart Home"),
            manufacturer="Woow",
            model="PaaS Smart Home",
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if the tunnel is connected."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.is_connected
