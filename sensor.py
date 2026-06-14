"""Sensor entities for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WoowConfigEntry
from .const import (
    CONF_HOME_NAME,
    CONF_PRODUCT_TYPE,
    DOMAIN,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    TunnelStatus,
)
from .coordinator import TunnelCoordinator


def _device_info(entry: WoowConfigEntry) -> DeviceInfo:
    """Build DeviceInfo with a product-aware model label."""
    product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
    model = (
        "PaaS Security Access"
        if product_type == PRODUCT_SECURITY_ACCESS
        else "PaaS Smart Home"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_HOME_NAME, "Woow Smart Home"),
        manufacturer="Woow",
        model=model,
    )


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
        self._product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the tunnel status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.status

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """For security access, expose product type and the access state.

        Smart home entities keep their previous behavior (no extra attributes).
        """
        if self._product_type != PRODUCT_SECURITY_ACCESS:
            return None
        attrs: dict[str, str] = {"product_type": self._product_type}
        data = self.coordinator.data
        if data is not None and data.state is not None:
            attrs["state"] = data.state
        return attrs


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
        self._product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the primary tunnel URL.

        For smart home this is https://{subdomain}; for security access it is
        the first route hostname (empty when there are no routes).
        """
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.subdomain_url

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """For security access, expose every route hostname (not just the first)."""
        if self._product_type != PRODUCT_SECURITY_ACCESS:
            return None
        data = self.coordinator.data
        if data is None:
            return None
        return {
            "route_hostnames": list(data.route_hostnames),
            "route_count": len(data.route_hostnames),
        }
