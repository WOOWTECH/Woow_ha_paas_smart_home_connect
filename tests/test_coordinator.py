"""Unit tests for TunnelCoordinator._async_update_data.

Instantiates the real coordinator with a mocked API client / tunnel manager
and a MagicMock config entry, then drives _async_update_data() directly. The
`hass` fixture comes from HA core's tests/conftest.py (wired in conftest.py).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

from custom_components.woow_paas_smart_home.api_client import (
    ApiError,
    AuthenticationError,
)
from custom_components.woow_paas_smart_home.const import (
    DOMAIN,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    McpIntegrationState,
    TunnelStatus,
)
from custom_components.woow_paas_smart_home.coordinator import (
    RouteInfo,
    TunnelCoordinator,
    TunnelStatusData,
)
from custom_components.woow_paas_smart_home.sensor import TunnelStatusSensor
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed


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


# ---------------------------------------------------------------------------
# MCP（#270 §5）— /status payload 的 mcp 字串併入 coordinator + 本地三態偵測。
# ---------------------------------------------------------------------------


async def test_sh_update_reads_mcp_and_detects_state(hass: HomeAssistant) -> None:
    """SH 成功 poll 讀出 mcp 字串，並跑本地三態偵測填 mcp_integration_state。

    真實 hass 未安裝 ha_mcp_tools → 偵測回 not_installed（端到端驗證偵測路徑）。
    """
    api = MagicMock()
    api.get_home_status = AsyncMock(
        return_value={"tunnel_status": "connected", "mcp": "ha_mcp_tools"}
    )
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SMART_HOME,
        subdomain="paas-sm-home",
    )

    data = await coord._async_update_data()

    assert data.mcp == "ha_mcp_tools"
    assert data.mcp_integration_state is McpIntegrationState.NOT_INSTALLED


async def test_sh_mcp_absent_defaults_empty(hass: HomeAssistant) -> None:
    """Payload 無 mcp key → mcp 空字串、不跑偵測（mcp_integration_state 為 None）。"""
    api = MagicMock()
    api.get_home_status = AsyncMock(return_value={"tunnel_status": "connected"})
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SMART_HOME,
        subdomain="paas-sm-home",
    )

    data = await coord._async_update_data()

    assert data.mcp == ""
    assert data.mcp_integration_state is None


async def test_sh_transient_failure_preserves_mcp(hass: HomeAssistant) -> None:
    """暫時性 API 失敗保留上一輪 mcp 字串（避免 sensor 存在性 flapping）。

    偵測本身與 API 無關，會照跑：真實 hass 未安裝 → not_installed（證明偵測獨立於雲端）。
    """
    api = MagicMock()
    api.get_home_status = AsyncMock(side_effect=ApiError(500, "boom"))
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SMART_HOME,
        subdomain="paas-sm-home",
    )
    coord.data = TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url="https://paas-sm-home",
        mcp="ha_mcp_tools",
        mcp_integration_state=McpIntegrationState.RUNNING,
    )

    data = await coord._async_update_data()

    # mcp 訂閱字串保留（不因 API 失誤被當成退訂而清掉）。
    assert data.mcp == "ha_mcp_tools"
    # remote 為 None + 本地 tunnel running -> UNKNOWN。
    assert data.status is TunnelStatus.UNKNOWN
    # 偵測獨立於 API，重算為當前實況（未安裝）。
    assert data.mcp_integration_state is McpIntegrationState.NOT_INSTALLED


async def test_sa_poll_has_no_mcp(hass: HomeAssistant) -> None:
    """SA 完全不受 MCP 影響：mcp 恆空、不觸發偵測。"""
    api = MagicMock()
    api.get_access_status = AsyncMock(
        return_value={"tunnel_status": "connected", "state": "active", "routes": []}
    )
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SECURITY_ACCESS
    )

    data = await coord._async_update_data()

    assert data.mcp == ""
    assert data.mcp_integration_state is None


# ---------------------------------------------------------------------------
# tunnel 已被刪除（平台 #833）— Cloudflare 刪 tunnel 是軟刪除，平台側新增
# tunnel_status = "deleted" 第四格。HA 這一側必須讓它與 "disconnected" 可區分：
# 前者要重建 tunnel、後者只要等連線恢復，但在此之前使用者看到的字一模一樣。
# 平台 PR: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/897
# ---------------------------------------------------------------------------


async def _sh_sensor_value(
    hass: HomeAssistant, *, remote: str, running: bool
) -> tuple[str | None, TunnelStatusSensor]:
    """跑一次真實 Smart Home poll，回傳「使用者在 sensor 上看到的值」與該 sensor。

    刻意走到 sensor 而不是只看 coordinator：#833 的症狀是使用者看到的字無從分辨，
    判別式就必須落在使用者看得到的那一層。
    """
    api = MagicMock()
    api.get_home_status = AsyncMock(return_value={"tunnel_status": remote})
    coord = _coordinator(
        hass,
        api=api,
        running=running,
        product_type=PRODUCT_SMART_HOME,
        subdomain="paas-sm-home",
    )
    coord.data = await coord._async_update_data()

    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {}
    sensor = TunnelStatusSensor(coord, entry)
    return sensor.native_value, sensor


@pytest.mark.parametrize("running", [True, False])
async def test_deleted_tunnel_is_distinguishable_from_disconnected(
    hass: HomeAssistant, running: bool
) -> None:
    """#833 判別式：已刪除的 tunnel 與「還在、只是沒連上」不得顯示成同一個值。

    刻意**不**寫成 ``assert value == TunnelStatus.DELETED``——那樣未修時會紅在
    AttributeError（只證明 enum 少一格），而 #833 本身是「兩種狀態不可區分」。
    改成同條件下跑兩次、比較使用者看得到的值，紅的訊息才會指向真正的病灶。

    local cloudflared 跑或不跑都要成立：未修時兩種 local 狀態各自被壓進不同的
    既有格（running -> error、stopped -> disconnected），但**都**與真正的
    disconnected 撞在一起。
    """
    deleted_value, _ = await _sh_sensor_value(hass, remote="deleted", running=running)
    disconnected_value, _ = await _sh_sensor_value(
        hass, remote="disconnected", running=running
    )

    assert deleted_value != disconnected_value, (
        f"#833（本地 cloudflared {'running' if running else 'stopped'}）："
        "已刪除的 tunnel 與「tunnel 還在、只是 cloudflared 沒連上」在 HA sensor "
        f"上是同一個值 ⇒ 使用者無從分辨要重建還是要等。兩次都得到：{deleted_value!r}"
    )


def _attach_fake_platform(sensor: TunnelStatusSensor) -> None:
    """讓 sensor 能在不跑整套 entity platform 的情況下求值 ``SensorEntity.state``.

    ⚠ 這是刻意踩 HA core 內部細節：``state`` 會先取 ``unit_of_measurement``，
    而那條路要求 entity 已被加進 platform。升 HA 版若這裡 AttributeError，
    改這個 shim 即可——那是大聲的失敗，正是我們要的（見下方測試為何需要真的
    走 HA 的檢查，而不是自己重寫一次條件）。
    """
    sensor.platform = types.SimpleNamespace(
        platform_name=DOMAIN,
        domain="sensor",
        default_language_platform_translations={},
    )
    sensor.entity_id = "sensor.woow_tunnel_status"


@pytest.mark.parametrize("running", [True, False])
async def test_deleted_passes_ha_core_enum_state_validation(
    hass: HomeAssistant, running: bool
) -> None:
    """deleted 必須通過 HA core 對 ENUM sensor 的 state 校驗，不是只「看起來對」。

    TunnelStatusSensor 是 SensorDeviceClass.ENUM；HA core 的 SensorEntity.state
    對 ``value not in options`` 直接 raise ValueError（homeassistant/components/
    sensor/__init__.py:639-643），且該例外一路傳出 _async_write_ha_state 沒有
    try/except 攔截 ⇒ 只加 _merge_status 分支而漏掉 const.py 的 enum，會讓
    entity 在真正發生刪除時炸掉，而不是顯示 deleted。

    這支刻意**呼叫 HA 真正的 ``state``**，而不是自己重寫一次 ``value in options``
    ——後者是把被測邏輯抄一份到測試裡，HA 哪天改了判斷條件也不會有人發現。
    末尾的反向對照證明這道閘門真的活著，這支測試不會靜默退化成 no-op。
    """
    value, sensor = await _sh_sensor_value(hass, remote="deleted", running=running)
    _attach_fake_platform(sensor)

    assert value == TunnelStatus.DELETED
    # 走 HA core 的真實校驗；options 與 enum 不同步的話這行就會 raise。
    assert sensor.state == TunnelStatus.DELETED

    # 反向對照：把 options 換成沒有 deleted 的舊清單，HA 必須擋下來。
    sensor._attr_options = [
        status.value for status in TunnelStatus if status is not TunnelStatus.DELETED
    ]
    with pytest.raises(ValueError, match="not in the list of options"):
        _ = sensor.state


@pytest.mark.parametrize("running", [True, False])
def test_deleted_wins_over_local_process_state(running: bool) -> None:
    """遠端說 deleted ⇒ 不論本地 cloudflared 跑不跑，一律 DELETED。

    tunnel 在 Cloudflare 上已不存在，本地行程就算還活著也連不到任何東西——那個
    running 不是健康訊號，是該被清掉的殘留。若讓 local running 蓋過去（回 ERROR），
    使用者會以為「連線出問題、等等看」，而實際上非重建不可。
    """
    assert TunnelCoordinator._merge_status(running, "deleted") is TunnelStatus.DELETED


async def test_sa_deleted_flows_through_the_same_merge(hass: HomeAssistant) -> None:
    """Security Access 走同一支 _merge_status（平台兩個 model 都加了 deleted）。"""
    api = MagicMock()
    api.get_access_status = AsyncMock(
        return_value={"tunnel_status": "deleted", "state": "active", "routes": []}
    )
    coord = _coordinator(
        hass, api=api, running=True, product_type=PRODUCT_SECURITY_ACCESS
    )

    data = await coord._async_update_data()

    assert data.status is TunnelStatus.DELETED
    # 已刪除不算連上（is_connected 只認 CONNECTED / UNKNOWN）。
    assert data.is_connected is False


@pytest.mark.parametrize(
    ("running", "remote", "expected"),
    [
        # 既有合併表（見 _merge_status docstring）——一格都不許動。
        (True, "connected", TunnelStatus.CONNECTED),
        (True, "disconnected", TunnelStatus.ERROR),
        (False, "connected", TunnelStatus.ERROR),
        (False, "disconnected", TunnelStatus.DISCONNECTED),
        (True, None, TunnelStatus.UNKNOWN),
        (False, None, TunnelStatus.DISCONNECTED),
        # 平台既有的第三格 error，以及舊平台可能回的空字串：兩者都沒被具名，
        # 靠 catch-all 收；加了 deleted 分支後行為必須照舊。
        (True, "error", TunnelStatus.ERROR),
        (False, "error", TunnelStatus.DISCONNECTED),
        (True, "", TunnelStatus.ERROR),
        (False, "", TunnelStatus.DISCONNECTED),
    ],
)
def test_legacy_merge_table_unchanged(
    running: bool, remote: str | None, expected: TunnelStatus
) -> None:
    """向後相容陽性對照：舊平台不會回 deleted，既有每一格行為原封不動。

    沒有這張表，「把 deleted 修好」與「把兩種狀態一起弄壞」在測試上是同一個綠燈。
    """
    assert TunnelCoordinator._merge_status(running, remote) is expected
