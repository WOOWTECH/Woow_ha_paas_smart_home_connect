"""Tests for the per-route sensor logic.

The route-diff callback and RouteUrlSensor properties are tested in isolation
with a fake coordinator (no hass) — CoordinatorEntity.__init__ only stores the
coordinator. The legacy-entity cleanup needs the entity registry, so it uses
the `hass` fixture from HA core's tests/conftest.py.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from custom_components.woow_paas_smart_home.const import (
    CONF_PRODUCT_TYPE,
    DOMAIN,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    McpIntegrationState,
    TunnelStatus,
)
from custom_components.woow_paas_smart_home.coordinator import (
    RouteInfo,
    TunnelStatusData,
)
from custom_components.woow_paas_smart_home.sensor import (
    McpConnectUrlSensor,
    McpStatusSensor,
    RouteUrlSensor,
    _async_remove_legacy_url_entity,
    _async_remove_mcp_entities,
    _async_sync_route_sensors,
    _setup_mcp_sensors,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


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
    """Available has two independent kill switches; value tracks the route."""
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


# ---------------------------------------------------------------------------
# MCP status sensor（#270 §5）——由 /status 的 mcp 訂閱字串驅動存在與否，
# 由本地三態偵測結果（coordinator.data.mcp_integration_state）驅動顯示值。
# ---------------------------------------------------------------------------


def _mcp_data(
    mcp: str,
    state: McpIntegrationState | None,
    webhook_id: str | None = "mcp_" + "a" * 32,
    subdomain_url: str = "https://paas-sm-home",
) -> TunnelStatusData:
    return TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url=subdomain_url,
        mcp=mcp,
        mcp_integration_state=state,
        mcp_webhook_id=webhook_id,
    )


def _mcp_coordinator(
    mcp: str,
    state: McpIntegrationState | None,
    webhook_id: str | None = "mcp_" + "a" * 32,
    subdomain_url: str = "https://paas-sm-home",
) -> types.SimpleNamespace:
    coord = types.SimpleNamespace()
    coord.data = _mcp_data(mcp, state, webhook_id, subdomain_url)
    coord.last_update_success = True
    coord.async_add_listener = MagicMock()
    return coord


def _mcp_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_PRODUCT_TYPE: PRODUCT_SMART_HOME}
    return entry


def test_mcp_status_native_value() -> None:
    """native_value 反映三態偵測結果；coordinator 尚無資料時回 None。"""
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.INSTALLED_NOT_RUNNING)
    sensor = McpStatusSensor(coord, _mcp_entry())

    assert sensor.native_value == McpIntegrationState.INSTALLED_NOT_RUNNING

    coord.data = None
    assert sensor.native_value is None


def test_mcp_status_options_and_unique_id() -> None:
    """ENUM options 為三態機器值；unique_id 綁 entry。"""
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    sensor = McpStatusSensor(coord, _mcp_entry())

    assert sensor.unique_id == "test_entry_mcp_status"
    assert sensor._attr_options == [
        "not_installed",
        "installed_not_running",
        "running",
    ]


def test_mcp_reconcile_adds_when_subscribed() -> None:
    """初次 setup 時 mcp=="ha_mcp_tools" → 一次建立狀態＋連線位址兩顆，並掛 poll listener。"""
    hass = MagicMock()
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    add = MagicMock()

    _setup_mcp_sensors(hass, coord, _mcp_entry(), add)

    assert add.call_count == 1
    batch = add.call_args[0][0]
    assert [type(e) for e in batch] == [McpStatusSensor, McpConnectUrlSensor]
    coord.async_add_listener.assert_called_once()


def test_mcp_reconcile_idempotent_no_double_add() -> None:
    """訂閱狀態下重複 poll 不重複新增（比照 route sync 的冪等性）。"""
    hass = MagicMock()
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    add = MagicMock()

    _setup_mcp_sensors(hass, coord, _mcp_entry(), add)
    assert add.call_count == 1

    sync = coord.async_add_listener.call_args[0][0]
    sync()
    sync()
    assert add.call_count == 1


async def test_mcp_reconcile_removes_on_unsubscribe(hass: HomeAssistant) -> None:
    """降頻/退訂：mcp 由 "ha_mcp_tools" 轉 "" → 從 registry 移除那顆 sensor（對稱移除）。"""
    registry = er.async_get(hass)
    entry = _mcp_entry()
    unique_ids = [
        f"{entry.entry_id}_mcp_status",
        f"{entry.entry_id}_mcp_connect_url",
    ]
    for unique_id in unique_ids:
        registry.async_get_or_create("sensor", DOMAIN, unique_id)
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None

    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    add = MagicMock()
    _setup_mcp_sensors(hass, coord, entry, add)
    sync = coord.async_add_listener.call_args[0][0]

    # 退訂 → 下一次 poll 的 _sync 應移除整組 entity（不能只清掉狀態那顆）。
    coord.data = _mcp_data("", None, webhook_id=None)
    sync()

    for unique_id in unique_ids:
        assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None


async def test_mcp_reconcile_noop_setup_when_unsubscribed(
    hass: HomeAssistant,
) -> None:
    """未訂閱時 setup：不新增 sensor；對空 registry 的移除為 no-op（不拋錯）。"""
    coord = _mcp_coordinator("", None)
    add = MagicMock()

    _setup_mcp_sensors(hass, coord, _mcp_entry(), add)

    add.assert_not_called()
    # 明確驗證移除路徑對缺席 entity 的冪等性。
    _async_remove_mcp_entities(hass, _mcp_entry())


# ---------------------------------------------------------------------------
# MCP connect URL sensor
# ---------------------------------------------------------------------------


def test_mcp_connect_url_native_value() -> None:
    """URL = tunnel URL + /api/webhook/{webhook_id}；無資料時回 None。"""
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    sensor = McpConnectUrlSensor(coord, _mcp_entry())

    assert sensor.native_value == (
        "https://paas-sm-home/api/webhook/mcp_" + "a" * 32
    )

    coord.data = None
    assert sensor.native_value is None


def test_mcp_connect_url_unique_id() -> None:
    """unique_id 綁 entry，且與狀態那顆不撞。"""
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    sensor = McpConnectUrlSensor(coord, _mcp_entry())

    assert sensor.unique_id == "test_entry_mcp_connect_url"
    assert sensor.unique_id != McpStatusSensor(coord, _mcp_entry()).unique_id


def test_mcp_connect_url_none_without_webhook_id() -> None:
    """ha_mcp_tools 沒跑／local-only 模式（webhook_id 為 None）→ 不吐 URL。"""
    coord = _mcp_coordinator(
        "ha_mcp_tools", McpIntegrationState.INSTALLED_NOT_RUNNING, webhook_id=None
    )
    assert McpConnectUrlSensor(coord, _mcp_entry()).native_value is None


def test_mcp_connect_url_none_without_tunnel_url() -> None:
    """tunnel 還沒起來（subdomain_url 為空）→ 不吐相對路徑，回 None。"""
    coord = _mcp_coordinator(
        "ha_mcp_tools", McpIntegrationState.RUNNING, subdomain_url=""
    )
    assert McpConnectUrlSensor(coord, _mcp_entry()).native_value is None


def test_mcp_connect_url_has_no_double_slash() -> None:
    """subdomain_url 不帶尾斜線、路徑常數帶前斜線 → 接起來剛好一個斜線。"""
    coord = _mcp_coordinator("ha_mcp_tools", McpIntegrationState.RUNNING)
    url = McpConnectUrlSensor(coord, _mcp_entry()).native_value

    assert url is not None
    assert "//api/webhook/" not in url
    assert url.count("/api/webhook/") == 1
