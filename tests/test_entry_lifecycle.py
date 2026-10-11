"""Entry 移除時撤銷平台授權，以及 start/stop_tunnel 後狀態立即一致。

2026-10-01 實測：
* 從 HA 移除 entry 後，平台上該 entry 的 OAuth token 仍有效（refresh token 30 天）；
* 呼叫 ``stop_tunnel`` 後立刻 ``get_status``，得到 ``tunnel_running=false`` 卻
  ``tunnel_status=connected``、``tunnel_connected=true``，要等下一輪 30 秒輪詢。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )
except ImportError:  # 在 HA core 樹內執行（見 conftest.py）
    from tests.common import MockConfigEntry
    from tests.test_util.aiohttp import AiohttpClientMocker

from custom_components.woow_paas_smart_home import (
    WoowRuntimeData,
    async_remove_entry,
    async_setup,
)
from custom_components.woow_paas_smart_home.const import (
    API_BASE_URL,
    DOMAIN,
    OAUTH2_CLIENT_ID,
    OAUTH2_REVOKE,
    PRODUCT_SMART_HOME,
    TunnelStatus,
)
from custom_components.woow_paas_smart_home.coordinator import (
    TunnelCoordinator,
    TunnelStatusData,
    instance_deleted_issue_id,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

REVOKE_URL = f"{API_BASE_URL}{OAUTH2_REVOKE}"


def _entry(hass: HomeAssistant, refresh: str, access: str = "acc") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"home-{refresh}",
        data={
            "auth_implementation": DOMAIN,
            "token": {"access_token": access, "refresh_token": refresh},
            "home_id": 6,
            "product_type": PRODUCT_SMART_HOME,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture(autouse=True)
def _no_binary_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """最後一個 entry 移除時會清 cloudflared 二進位；測試裡不碰檔案系統。"""
    monkeypatch.setattr(
        "custom_components.woow_paas_smart_home.CloudflaredManager.cleanup_binary",
        AsyncMock(),
    )


# ---------------------------------------------------------------------------
# 問題 2：移除 entry 時撤銷平台 OAuth 授權
# ---------------------------------------------------------------------------


async def test_remove_entry_revokes_its_refresh_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(REVOKE_URL, json={})
    entry = _entry(hass, "rt-a")

    await async_remove_entry(hass, entry)

    assert aioclient_mock.call_count == 1
    method, url, data, _headers = aioclient_mock.mock_calls[0]
    assert method == "POST"
    assert str(url) == REVOKE_URL
    # public client：只送 client_id，不送 client_secret。撤銷 refresh token 會把
    # 平台上同一筆 token 紀錄（含 access token）整筆標成 revoked。
    assert data == {"token": "rt-a", "client_id": OAUTH2_CLIENT_ID}


async def test_remove_entry_only_revokes_its_own_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """同一個 HA 的其他 entry 各自有 token，不可被連帶撤銷。"""
    aioclient_mock.post(REVOKE_URL, json={})
    _entry(hass, "rt-a")
    entry_b = _entry(hass, "rt-b")

    await async_remove_entry(hass, entry_b)

    assert [call[2]["token"] for call in aioclient_mock.mock_calls] == ["rt-b"]


async def test_remove_entry_falls_back_to_access_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(REVOKE_URL, json={})
    entry = _entry(hass, "", access="acc-only")

    await async_remove_entry(hass, entry)

    assert aioclient_mock.mock_calls[0][2]["token"] == "acc-only"


@pytest.mark.parametrize(
    "mock_kwargs",
    [
        {"exc": aiohttp.ClientError("offline")},
        {"exc": TimeoutError()},
        {"status": 401, "json": {"error": "invalid_client"}},
        {"status": 500, "text": "boom"},
    ],
    ids=["network", "timeout", "401", "500"],
)
async def test_revoke_failure_does_not_block_removal(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    mock_kwargs: dict,
) -> None:
    aioclient_mock.post(REVOKE_URL, **mock_kwargs)
    entry = _entry(hass, "rt-a")

    await async_remove_entry(hass, entry)  # 不可 raise

    assert "revok" in caplog.text.lower()


async def test_remove_entry_without_token_skips_revoke(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={"home_id": 6})
    entry.add_to_hass(hass)

    await async_remove_entry(hass, entry)

    assert aioclient_mock.call_count == 0


async def test_remove_entry_clears_deleted_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(REVOKE_URL, json={})
    entry = _entry(hass, "rt-a")
    issue_id = instance_deleted_issue_id(entry.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="instance_deleted",
        translation_placeholders={"name": "x"},
    )

    await async_remove_entry(hass, entry)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


# ---------------------------------------------------------------------------
# 問題 3：start/stop_tunnel 後 get_status 與 sensor 立即一致
# ---------------------------------------------------------------------------


@pytest.fixture
async def loaded(hass: HomeAssistant) -> tuple[MagicMock, TunnelCoordinator]:
    """註冊 services，並掛上一個 runtime_data 為真 coordinator＋假 tunnel 的 entry。"""
    hass.http = MagicMock()
    assert await async_setup(hass, {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="sh-e2e",
        data={"home_id": 6, "product_type": PRODUCT_SMART_HOME, "tunnel_token": "tok"},
    )
    entry.add_to_hass(hass)
    tunnel = MagicMock()
    tunnel.is_running = True

    async def _stop() -> bool:
        tunnel.is_running = False
        return True

    async def _start(_token: str) -> bool:
        tunnel.is_running = True
        return True

    tunnel.stop_tunnel = AsyncMock(side_effect=_stop)
    tunnel.start_tunnel = AsyncMock(side_effect=_start)
    tunnel.ensure_binary = AsyncMock(return_value=True)
    coordinator = TunnelCoordinator(
        hass,
        api_client=MagicMock(),
        tunnel_manager=tunnel,
        instance_id=6,
        subdomain="paas-sm-x.woowtech.io",
        config_entry=entry,
    )
    coordinator.data = TunnelStatusData(
        status=TunnelStatus.CONNECTED,
        subdomain_url="https://paas-sm-x.woowtech.io",
        remote_status="connected",
    )
    entry.runtime_data = WoowRuntimeData(
        api_client=MagicMock(),
        tunnel_manager=tunnel,
        coordinator=coordinator,
        session=MagicMock(),
    )
    return tunnel, coordinator


async def _get_status(hass: HomeAssistant) -> dict:
    return await hass.services.async_call(
        DOMAIN, "get_status", {"home_id": "6"}, blocking=True, return_response=True
    )


async def test_stop_tunnel_then_get_status_is_consistent(
    hass: HomeAssistant, loaded: tuple[MagicMock, TunnelCoordinator]
) -> None:
    _tunnel, coordinator = loaded

    await hass.services.async_call(DOMAIN, "stop_tunnel", {"home_id": "6"}, blocking=True)
    status = await _get_status(hass)

    assert status["tunnel_running"] is False
    # 停掉後平台多半還說 connected ⇒ 合併表給 error，絕不再是 connected。
    assert status["tunnel_status"] == TunnelStatus.ERROR
    assert status["tunnel_connected"] is False
    # sensor 讀的 coordinator.data 同步更新，不等 30 秒輪詢。
    assert coordinator.data.status is TunnelStatus.ERROR


async def test_start_tunnel_updates_status_immediately(
    hass: HomeAssistant, loaded: tuple[MagicMock, TunnelCoordinator]
) -> None:
    tunnel, coordinator = loaded
    tunnel.is_running = False
    coordinator.async_update_local_status()
    assert coordinator.data.status is TunnelStatus.ERROR

    await hass.services.async_call(
        DOMAIN, "start_tunnel", {"home_id": "6"}, blocking=True
    )

    assert coordinator.data.status is TunnelStatus.CONNECTED
    status = await _get_status(hass)
    assert status["tunnel_running"] is True
    assert status["tunnel_connected"] is True


async def test_get_status_reflects_process_that_died_between_polls(
    hass: HomeAssistant, loaded: tuple[MagicMock, TunnelCoordinator]
) -> None:
    """行程自己掛掉（沒經過 service）時，get_status 三個欄位也要彼此一致。"""
    tunnel, _coordinator = loaded
    tunnel.is_running = False

    status = await _get_status(hass)

    assert status["tunnel_running"] is False
    assert status["tunnel_connected"] is False


async def test_start_tunnel_refused_when_deleted_on_platform(
    hass: HomeAssistant, loaded: tuple[MagicMock, TunnelCoordinator]
) -> None:
    tunnel, coordinator = loaded
    coordinator.instance_deleted = True

    with pytest.raises(HomeAssistantError, match="deleted"):
        await hass.services.async_call(
            DOMAIN, "start_tunnel", {"home_id": "6"}, blocking=True
        )

    tunnel.start_tunnel.assert_not_awaited()
