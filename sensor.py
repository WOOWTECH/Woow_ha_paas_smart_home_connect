"""Sensor entities for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from functools import partial

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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
from .coordinator import RouteInfo, TunnelCoordinator


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
    """Set up Woow PaaS Smart Home sensor entities.

    Smart Home: one fixed status sensor + one URL sensor.
    Security Access: one fixed status sensor + one URL sensor PER route, added
    dynamically as routes appear in the 30s coordinator poll (no reload needed).
    """
    coordinator = entry.runtime_data.coordinator
    product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)

    if product_type != PRODUCT_SECURITY_ACCESS:
        async_add_entities([
            TunnelStatusSensor(coordinator, entry),
            TunnelUrlSensor(coordinator, entry),
        ])
        return

    # Security Access: the single aggregate URL sensor (<= 0.2.0) is replaced by
    # one sensor per route. Remove the legacy entity so it does not linger.
    _async_remove_legacy_url_entity(hass, entry)
    async_add_entities([TunnelStatusSensor(coordinator, entry)])

    known_route_ids: set[str] = set()
    sync_routes = partial(
        _async_sync_route_sensors,
        coordinator,
        entry,
        known_route_ids,
        async_add_entities,
    )
    # Re-run on every poll; call once now to add routes already present.
    entry.async_on_unload(coordinator.async_add_listener(sync_routes))
    sync_routes()


@callback
def _async_sync_route_sensors(
    coordinator: TunnelCoordinator,
    entry: WoowConfigEntry,
    known_route_ids: set[str],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a RouteUrlSensor for each newly-appeared Security Access route.

    Add-only: a route removed in PaaS is handled by ``RouteUrlSensor.available``
    returning False (history is preserved, automations keep their entity_id).
    The orphaned entity becomes user-deletable after the next integration reload,
    when this add-only sync no longer recreates it.
    """
    data = coordinator.data
    if data is None:
        return
    new_entities: list[RouteUrlSensor] = []
    for route in data.routes:
        if route.route_id in known_route_ids:
            continue
        known_route_ids.add(route.route_id)
        new_entities.append(
            RouteUrlSensor(
                coordinator,
                entry,
                route.route_id,
                route.subdomain_prefix or route.hostname,
            )
        )
    if new_entities:
        async_add_entities(new_entities)


def _async_remove_legacy_url_entity(
    hass: HomeAssistant, entry: WoowConfigEntry
) -> None:
    """Remove the pre-0.3.0 aggregate Security Access URL entity if it exists.

    Before 0.3.0 a Security Access entry had a single ``{entry_id}_tunnel_url``
    sensor showing only the first route. It is superseded by per-route sensors;
    drop it so users do not see a stale duplicate. Idempotent / no-op when absent.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{entry.entry_id}_tunnel_url"
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


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


class RouteUrlSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """Sensor showing the URL of a single Security Access route.

    Bound to the route's stable backend id; reports ``unavailable`` (rather than
    being removed) when its route disappears from PaaS, so history and automation
    references survive a transient drop or a route the user can re-add later.

    Editable route fields (service_url, is_protected, ...) update live on the
    same entity every poll, because the route keeps its id. The hostname /
    subdomain_prefix is immutable in PaaS (paas-platform confirmed): "renaming"
    a route is a delete+create that yields a NEW route id, so this entity goes
    unavailable and a fresh entity appears — its history does not carry over.
    See README / sm-api-doc.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:web"
    _attr_has_entity_name = True
    _attr_translation_key = "route_url"

    def __init__(
        self,
        coordinator: TunnelCoordinator,
        entry: WoowConfigEntry,
        route_id: str,
        display_name: str,
    ) -> None:
        """Initialize the per-route URL sensor."""
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{entry.entry_id}_route_{route_id}"
        self._attr_translation_placeholders = {"route": display_name}
        self._attr_device_info = _device_info(entry)

    def _route(self) -> RouteInfo | None:
        """Return this sensor's current route from coordinator data, if present."""
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (route for route in data.routes if route.route_id == self._route_id),
            None,
        )

    @property
    def available(self) -> bool:
        """Unavailable when the backing route is gone (kept in registry, not removed)."""
        return super().available and self._route() is not None

    @property
    def native_value(self) -> str | None:
        """Return this route's URL (https://{hostname})."""
        route = self._route()
        return f"https://{route.hostname}" if route else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose this route's own metadata."""
        route = self._route()
        if route is None:
            return None
        return {
            "route_id": route.route_id,
            "subdomain_prefix": route.subdomain_prefix,
            "is_protected": route.is_protected,
            "service_url": route.service_url,
        }
