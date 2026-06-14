"""Unit tests for TunnelCoordinator._async_update_data.

Instantiates the real coordinator with a mocked API client / tunnel manager
and a MagicMock config entry, then drives _async_update_data() directly. The
`hass` fixture comes from HA core's tests/conftest.py (wired in conftest.py).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.woow_paas_smart_home.api_client import (
    ApiError,
    AuthenticationError,
)
from custom_components.woow_paas_smart_home.const import (
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    TunnelStatus,
)
from custom_components.woow_paas_smart_home.coordinator import (
    RouteInfo,
    TunnelCoordinator,
    TunnelStatusData,
)


def _coordinator(
    hass: HomeAssistant,
    *,
    api: MagicMock,
    running: bool,
    product_type: str,
    subdomain: str = "",
) -> TunnelCoordinator:
    """Build a coordinator with mocked collaborators."""
    tunnel = MagicMock()
    tunnel.is_running = running
    entry = MagicMock()
    entry.entry_id = "e1"
    return TunnelCoordinator(
        hass,
        api_client=api,
        tunnel_manager=tunnel,
        instance_id=6,
        subdomain=subdomain,
        config_entry=entry,
        product_type=product_type,
    )


async def test_sa_update_builds_routes(hass: HomeAssistant) -> None:
    """A successful SA poll maps every route into the routes tuple."""
    api = MagicMock()
    api.get_access_status = AsyncMock(
        return_value={
            "tunnel_status": "connected",
            "state": "active",
            "routes": [
                {
                    "id": 7,
                    "hostname": "test1.woowtech.io",
                    "subdomain_prefix": "test1",
                    "is_protected": True,
                    "service_url": "http://localhost:80",
                },
                {"id": 8, "hostname": "test2.woowtech.io"},
            ],
        }
    )
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SECURITY_ACCESS
    )

    data = await coord._async_update_data()

    assert data.status is TunnelStatus.CONNECTED
    assert data.state == "active"
    assert [r.route_id for r in data.routes] == ["7", "8"]
    assert data.routes[0] == RouteInfo(
        route_id="7",
        hostname="test1.woowtech.io",
        subdomain_prefix="test1",
        is_protected=True,
        service_url="http://localhost:80",
    )
    # Missing optional fields default cleanly.
    assert data.routes[1].subdomain_prefix == ""
    assert data.routes[1].is_protected is False
    # Display URL is the first route; derived hostnames cover all routes.
    assert data.subdomain_url == "https://test1.woowtech.io"
    assert data.route_hostnames == ("test1.woowtech.io", "test2.woowtech.io")


async def test_sa_transient_failure_preserves_routes(hass: HomeAssistant) -> None:
    """A transient API error keeps the last-known routes (the bug this fixes).

    Without preservation the coordinator would report 0 routes on every API
    hiccup, flapping all per-route entities to unavailable.
    """
    api = MagicMock()
    api.get_access_status = AsyncMock(side_effect=ApiError(500, "boom"))
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SECURITY_ACCESS
    )
    previous_routes = (
        RouteInfo(route_id="7", hostname="test1.woowtech.io"),
        RouteInfo(route_id="8", hostname="test2.woowtech.io"),
    )
    coord.data = TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url="https://test1.woowtech.io",
        state="active",
        routes=previous_routes,
    )

    data = await coord._async_update_data()

    # Routes/state survive the transient failure.
    assert data.routes == previous_routes
    assert data.state == "active"
    # remote_status is None + tunnel running -> UNKNOWN (treated as connected).
    assert data.status is TunnelStatus.UNKNOWN


async def test_sa_transient_failure_disconnected_when_not_running(
    hass: HomeAssistant,
) -> None:
    """Transient failure with the local tunnel stopped -> DISCONNECTED."""
    api = MagicMock()
    api.get_access_status = AsyncMock(side_effect=ApiError(503, "down"))
    coord = _coordinator(
        hass, api=api, running=False, product_type=PRODUCT_SECURITY_ACCESS
    )
    coord.data = TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url="https://test1.woowtech.io",
        state="active",
        routes=(RouteInfo(route_id="7", hostname="test1.woowtech.io"),),
    )

    data = await coord._async_update_data()

    assert data.status is TunnelStatus.DISCONNECTED
    assert [r.route_id for r in data.routes] == ["7"]  # still preserved


async def test_sa_active_no_route_clears_routes(hass: HomeAssistant) -> None:
    """A successful poll with zero routes genuinely empties the tuple."""
    api = MagicMock()
    api.get_access_status = AsyncMock(
        return_value={
            "tunnel_status": "connected",
            "state": "active_no_route",
            "routes": [],
        }
    )
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SECURITY_ACCESS
    )
    # Even with a stale non-empty previous, a successful empty poll wins.
    coord.data = TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url="https://old.woowtech.io",
        state="active",
        routes=(RouteInfo(route_id="9", hostname="old.woowtech.io"),),
    )

    data = await coord._async_update_data()

    assert data.routes == ()
    assert data.subdomain_url == ""
    assert data.state == "active_no_route"


async def test_sa_auth_error_raises_config_entry_auth_failed(
    hass: HomeAssistant,
) -> None:
    """A 401/403 surfaces as ConfigEntryAuthFailed (triggers reauth)."""
    api = MagicMock()
    api.get_access_status = AsyncMock(
        side_effect=AuthenticationError(401, "unauthorized")
    )
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SECURITY_ACCESS
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_smart_home_uses_subdomain_and_has_no_routes(
    hass: HomeAssistant,
) -> None:
    """Smart Home path is unchanged: single subdomain URL, no routes."""
    api = MagicMock()
    api.get_home_status = AsyncMock(return_value={"tunnel_status": "connected"})
    coord = _coordinator(
        hass,
        api=api,
        running=True,
        product_type=PRODUCT_SMART_HOME,
        subdomain="paas-sm-home",
    )

    data = await coord._async_update_data()

    assert data.status is TunnelStatus.CONNECTED
    assert data.subdomain_url == "https://paas-sm-home"
    assert data.routes == ()
    api.get_home_status.assert_awaited_once_with(6)
