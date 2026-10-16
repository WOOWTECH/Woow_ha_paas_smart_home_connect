"""一個家只能綁一台 Home Assistant（#31）。

2026-10-17 正式站事故：同一個 PaaS 帳號在兩台 HA（192.168.2.34 與 .121）各跑一次
設定流程、都選了同一個家「woowtech-ha」，兩台都拿到同一份 tunnel 權杖並各自起了
cloudflared。Cloudflare 對一條 tunnel 的多個 connector 是「分流」而不是「拒絕」，
於是對外網址隨機落到其中一台，使用者在錯的那台上登入失敗；平台端 UI 只讀
``connections[0]``，兩邊都顯示「已連線」，完全看不出來。

這裡釘住元件這一側的兩道守線：
1. **本機**：entry 記著建立它的 HA uid，被搬到另一台機器就不起隧道（還原備份、
   複製映像）。
2. **平台**：取權杖時帶上本機 uid；平台回 409 ``already_bound`` 時不准退回用快取
   權杖——那正是會生出第二個 connector 的那條路。
"""
from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ImportError:  # 在 HA core 樹內執行（見 conftest.py）
    from tests.common import MockConfigEntry

from custom_components.woow_paas_smart_home import async_setup_entry
from custom_components.woow_paas_smart_home.api_client import (
    AlreadyBoundError,
    ApiError,
    api_error_for,
)
from custom_components.woow_paas_smart_home.const import (
    CONF_INSTANCE_LABEL,
    CONF_INSTANCE_UID,
    DOMAIN,
    ERROR_ALREADY_BOUND,
    PRODUCT_SMART_HOME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

THIS_UID = "11111111111111111111111111111111"
OTHER_UID = "22222222222222222222222222222222"


def _entry(hass: HomeAssistant, **extra) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="home",
        data={
            "auth_implementation": DOMAIN,
            "token": {"access_token": "acc", "refresh_token": "ref"},
            "home_id": 1,
            "product_type": PRODUCT_SMART_HOME,
            "tunnel_token": "cached-token",
            "tunnel_id": "tid",
            "subdomain": "paas-sm-x.woowtech.io",
            **extra,
        },
    )
    entry.add_to_hass(hass)
    return entry


# ---- 平台回應的對應 ------------------------------------------------------------


def test_409_already_bound_maps_to_its_own_error() -> None:
    err = api_error_for(
        HTTPStatus.CONFLICT,
        ERROR_ALREADY_BOUND,
        "409 already bound",
        {"bound_to": "展示機 (woowtech-sho)", "bound_at": "2026-10-17 01:00:00"},
    )
    assert isinstance(err, AlreadyBoundError)
    assert err.bound_to == "展示機 (woowtech-sho)"
    assert err.bound_at == "2026-10-17 01:00:00"


def test_other_409s_stay_generic() -> None:
    # 409 不是本元件專屬狀態碼；只有帶 already_bound 的才是綁定衝突。
    err = api_error_for(HTTPStatus.CONFLICT, "", "409")
    assert type(err) is ApiError


async def test_token_request_carries_this_installation_identity(
    hass: HomeAssistant,
) -> None:
    from custom_components.woow_paas_smart_home.api_client import WoowPaasApiClient

    session = AsyncMock()
    resp = AsyncMock()
    resp.status = HTTPStatus.OK
    resp.json.return_value = {"tunnel_token": "t", "tunnel_id": "i", "subdomain": "s"}
    session.async_request.return_value = resp
    client = WoowPaasApiClient(session, "https://example.invalid")

    await client.get_tunnel_token(7, THIS_UID, "家 (pi)")

    url = session.async_request.call_args[0][1]
    assert f"instance_uid={THIS_UID}" in url
    # label 只是給人看的，必須做 URL 編碼（中文、空白、括號）
    assert "instance_label=%E5%AE%B6+%28pi%29" in url


async def test_token_request_without_identity_sends_no_query(
    hass: HomeAssistant,
) -> None:
    """舊平台（還沒有綁定欄位）不該因為多了 query string 而壞掉。"""
    from custom_components.woow_paas_smart_home.api_client import WoowPaasApiClient

    session = AsyncMock()
    resp = AsyncMock()
    resp.status = HTTPStatus.OK
    resp.json.return_value = {"tunnel_token": "t", "tunnel_id": "i", "subdomain": "s"}
    session.async_request.return_value = resp
    client = WoowPaasApiClient(session, "https://example.invalid")

    await client.get_tunnel_token(7)

    assert "?" not in session.async_request.call_args[0][1]


# ---- 本機守線 ------------------------------------------------------------------


async def test_entry_from_another_installation_does_not_start_a_tunnel(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass, **{CONF_INSTANCE_UID: OTHER_UID,
                            CONF_INSTANCE_LABEL: "展示機 (woowtech-sho)"})

    with (
        patch("custom_components.woow_paas_smart_home.async_instance_identity",
              AsyncMock(return_value=(THIS_UID, "家 (pi)"))),
        patch("custom_components.woow_paas_smart_home.CloudflaredManager") as manager,
        patch("custom_components.woow_paas_smart_home."
              "async_get_config_entry_implementation", AsyncMock()),
        pytest.raises(ConfigEntryError, match="another Home Assistant"),
    ):
        await async_setup_entry(hass, entry)

    # 關鍵不是「有拋例外」，是「沒有去碰 cloudflared」
    manager.return_value.ensure_binary.assert_not_called()
    manager.return_value.start_tunnel.assert_not_called()


async def test_entry_without_identity_is_stamped_once(hass: HomeAssistant) -> None:
    """0.4.11 以前建立的 entry：第一次啟動蓋章成本機，之後才有判準。"""
    entry = _entry(hass)

    with (
        patch("custom_components.woow_paas_smart_home.async_instance_identity",
              AsyncMock(return_value=(THIS_UID, "家 (pi)"))),
        patch("custom_components.woow_paas_smart_home."
              "async_get_config_entry_implementation", AsyncMock()),
        patch("custom_components.woow_paas_smart_home.OAuth2Session"),
        patch("custom_components.woow_paas_smart_home.WoowPaasApiClient") as api,
        patch("custom_components.woow_paas_smart_home.CloudflaredManager"),
        patch("custom_components.woow_paas_smart_home.TunnelCoordinator") as coord,
        # entry 設定的最後一步會載入 sensor platform（需要整合本體被 HA 註冊）；
        # 這個測試只看蓋章那一段，不是 platform 轉發。
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        api.return_value.get_tunnel_token = AsyncMock(
            return_value={"tunnel_token": "fresh", "tunnel_id": "tid",
                          "subdomain": "paas-sm-x.woowtech.io"})
        coord.return_value.async_config_entry_first_refresh = AsyncMock()
        coord.return_value.instance_deleted = False
        await async_setup_entry(hass, entry)

    assert entry.data[CONF_INSTANCE_UID] == THIS_UID
    assert entry.data[CONF_INSTANCE_LABEL] == "家 (pi)"


# ---- 平台守線 ------------------------------------------------------------------


async def test_platform_says_bound_elsewhere_keeps_the_tunnel_down(
    hass: HomeAssistant,
) -> None:
    """⛔ 這一條是整個修法的核心：拿快取權杖起隧道＝製造第二個 connector。"""
    entry = _entry(hass, **{CONF_INSTANCE_UID: THIS_UID})

    with (
        patch("custom_components.woow_paas_smart_home.async_instance_identity",
              AsyncMock(return_value=(THIS_UID, "家 (pi)"))),
        patch("custom_components.woow_paas_smart_home."
              "async_get_config_entry_implementation", AsyncMock()),
        patch("custom_components.woow_paas_smart_home.OAuth2Session"),
        patch("custom_components.woow_paas_smart_home.WoowPaasApiClient") as api,
        patch("custom_components.woow_paas_smart_home.CloudflaredManager") as manager,
        pytest.raises(ConfigEntryError, match="bound to another"),
    ):
        api.return_value.get_tunnel_token = AsyncMock(
            side_effect=AlreadyBoundError(
                HTTPStatus.CONFLICT, "409", bound_to="展示機 (woowtech-sho)",
                bound_at="2026-10-17 01:00:00"))
        await async_setup_entry(hass, entry)

    assert entry.data["tunnel_token"] == "cached-token"  # 快取還在，但沒有被拿去用
    manager.return_value.ensure_binary.assert_not_called()
    manager.return_value.start_tunnel.assert_not_called()
