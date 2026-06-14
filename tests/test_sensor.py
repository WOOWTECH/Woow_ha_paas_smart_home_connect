"""Tests for the per-route sensor logic.

The route-diff callback and RouteUrlSensor properties are tested in isolation
with a fake coordinator (no hass) — CoordinatorEntity.__init__ only stores the
coordinator. The legacy-entity cleanup needs the entity registry, so it uses
the `hass` fixture from HA core's tests/conftest.py.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.woow_paas_smart_home.const import (
    CONF_PRODUCT_TYPE,
    DOMAIN,
    PRODUCT_SECURITY_ACCESS,
    TunnelStatus,
)
from custom_components.woow_paas_smart_home.coordinator import (
    RouteInfo,
    TunnelStatusData,
)
from custom_components.woow_paas_smart_home.sensor import (
    RouteUrlSensor,
    _async_remove_legacy_url_entity,
    _async_sync_route_sensors,
)


def _data(routes: list[RouteInfo]) -> TunnelStatusData:
    return TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url=f"https://{routes[0].hostname}" if routes else "",
        state="active",
        routes=tuple(routes),
    )


def _fake_coordinator(routes: list[RouteInfo]) -> types.SimpleNamespace:
    coord = types.SimpleNamespace()
    coord.data = _data(routes)
    coord.last_update_success = True
    coord.async_add_listener = MagicMock()
    return coord


def _fake_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_PRODUCT_TYPE: PRODUCT_SECURITY_ACCESS}
    return entry


def test_sync_adds_new_and_no_double_add() -> None:
    """The diff is add-only, keyed on route id, and never double-adds."""
    r1 = RouteInfo(route_id="1", hostname="a.example.com", subdomain_prefix="a")
    r2 = RouteInfo(route_id="2", hostname="b.example.com", subdomain_prefix="b")
    coord = _fake_coordinator([r1])
    entry = _fake_entry()
    known: set[str] = set()
    add = MagicMock()

    _async_sync_route_sensors(coord, entry, known, add)
    assert known == {"1"}
    assert add.call_count == 1
    first_batch = add.call_args[0][0]
    assert len(first_batch) == 1
    assert isinstance(first_batch[0], RouteUrlSensor)
    assert first_batch[0].unique_id == "test_entry_route_1"

    # Same data again -> nothing new added.
    _async_sync_route_sensors(coord, entry, known, add)
    assert add.call_count == 1

    # A second route appears -> only the NEW one is added.
    coord.data = _data([r1, r2])
    _async_sync_route_sensors(coord, entry, known, add)
    assert known == {"1", "2"}
    assert add.call_count == 2
    assert [s._route_id for s in add.call_args[0][0]] == ["2"]


def test_sync_noop_when_data_none() -> None:
    """No coordinator data yet -> nothing is added."""
    coord = types.SimpleNamespace(data=None, last_update_success=False)
    add = MagicMock()
    _async_sync_route_sensors(coord, _fake_entry(), set(), add)
    add.assert_not_called()


def test_route_sensor_available_and_value() -> None:
    """available has two independent kill switches; value tracks the route."""
    r1 = RouteInfo(route_id="1", hostname="a.example.com")
    coord = _fake_coordinator([r1])
    sensor = RouteUrlSensor(coord, _fake_entry(), "1", "a")

    assert sensor.available is True
    assert sensor.native_value == "https://a.example.com"

    # Route removed in PaaS -> unavailable (not removed), value gone.
    coord.data = _data([])
    assert sensor.available is False
    assert sensor.native_value is None

    # Route back but the coordinator poll failed -> still unavailable.
    coord.data = _data([r1])
    coord.last_update_success = False
    assert sensor.available is False


def test_route_sensor_attributes() -> None:
    """Attributes expose this route's own metadata, or None when gone."""
    r1 = RouteInfo(
        route_id="1",
        hostname="a.example.com",
        subdomain_prefix="a",
        is_protected=True,
        service_url="http://localhost:80",
    )
    coord = _fake_coordinator([r1])
    sensor = RouteUrlSensor(coord, _fake_entry(), "1", "a")

    assert sensor.extra_state_attributes == {
        "route_id": "1",
        "subdomain_prefix": "a",
        "is_protected": True,
        "service_url": "http://localhost:80",
    }

    coord.data = _data([])
    assert sensor.extra_state_attributes is None


async def test_legacy_url_entity_removed(hass: HomeAssistant) -> None:
    """The pre-0.3.0 aggregate {entry_id}_tunnel_url entity is cleaned up."""
    registry = er.async_get(hass)
    entry = _fake_entry()
    unique_id = f"{entry.entry_id}_tunnel_url"
    registry.async_get_or_create("sensor", DOMAIN, unique_id)
    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None

    _async_remove_legacy_url_entity(hass, entry)

    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
    # Idempotent: a second call on an already-clean registry is a no-op.
    _async_remove_legacy_url_entity(hass, entry)
