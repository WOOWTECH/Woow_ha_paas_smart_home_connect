"""Unit tests for async_detect_ha_mcp_state (社群 ha_mcp_tools 三態偵測).

用 MagicMock hass 隔離測試——偵測函式只讀 hass.config_entries.async_loaded_entries
（純記憶體）與一次 executor 的 os.path.isfile；兩者皆 mock，故不需真實 HA runtime。

設計依據（#270 §5）：偵測結果三態
  - running               : 有任一 LOADED 且未停用的 ha_mcp_tools config entry
  - installed_not_running : 無 LOADED entry，但 manifest.json 在磁碟上
  - not_installed         : 無 LOADED entry，且 manifest.json 不在磁碟上

其中 installed/not_installed 刻意用「manifest 檔是否存在」判定，而非
async_get_integration/IntegrationNotFound——後者會把「manifest 在磁碟但被
version/blocked/corrupt/陳舊快取拒絕」誤判成未安裝（見 test_* 註解的 skeptic 反例）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.woow_paas_smart_home.const import (
    HA_MCP_DOMAIN,
    McpIntegrationState,
)
from custom_components.woow_paas_smart_home.coordinator import async_detect_ha_mcp_state


def _hass(*, loaded_entries: list, manifest_isfile: bool | None) -> MagicMock:
    """Build a MagicMock hass wired for detection.

    loaded_entries: async_loaded_entries(HA_MCP_DOMAIN) 的回傳。
    manifest_isfile: executor(os.path.isfile) 的回傳；None 代表預期不會走到
        executor（測試會另行斷言未呼叫）。
    """
    hass = MagicMock()
    hass.config_entries.async_loaded_entries.return_value = loaded_entries
    hass.config.path.return_value = "/config/custom_components/ha_mcp_tools/manifest.json"
    if manifest_isfile is None:
        hass.async_add_executor_job = MagicMock()  # 斷言 not_called 用
    else:
        hass.async_add_executor_job = AsyncMock(return_value=manifest_isfile)
    return hass


async def test_detect_running_when_loaded_entry_present() -> None:
    """有 LOADED entry → running，且不觸碰磁碟（純記憶體快路徑）。"""
    hass = _hass(loaded_entries=[MagicMock()], manifest_isfile=None)

    result = await async_detect_ha_mcp_state(hass)

    assert result == McpIntegrationState.RUNNING
    hass.config_entries.async_loaded_entries.assert_called_once_with(HA_MCP_DOMAIN)
    hass.async_add_executor_job.assert_not_called()


async def test_detect_installed_not_running_when_manifest_present() -> None:
    """無 LOADED entry 但 manifest 在磁碟 → installed_not_running。

    涵蓋常見中間態：使用者以 HACS 裝了 ha_mcp_tools，但尚未在 UI 新增 integration
    （無任何 config entry）。
    """
    hass = _hass(loaded_entries=[], manifest_isfile=True)

    result = await async_detect_ha_mcp_state(hass)

    assert result == McpIntegrationState.INSTALLED_NOT_RUNNING


async def test_detect_not_installed_when_manifest_absent() -> None:
    """無 LOADED entry 且 manifest 不在磁碟 → not_installed。"""
    hass = _hass(loaded_entries=[], manifest_isfile=False)

    result = await async_detect_ha_mcp_state(hass)

    assert result == McpIntegrationState.NOT_INSTALLED


async def test_detect_stale_entry_after_hacs_uninstall_is_not_installed() -> None:
    """回歸：skeptic #2——HACS 反安裝只刪檔、留下 stale NOT_LOADED entry。

    async_loaded_entries 只回 LOADED entry，stale NOT_LOADED 天然被排除 → 走
    manifest 探測 → 檔案已不在 → not_installed（而非誤判 installed_not_running）。
    """
    hass = _hass(loaded_entries=[], manifest_isfile=False)

    result = await async_detect_ha_mcp_state(hass)

    assert result == McpIntegrationState.NOT_INSTALLED


async def test_detect_blocked_or_corrupt_manifest_still_installed() -> None:
    """回歸：skeptic #1——manifest 在磁碟但可能被 version/blocked/corrupt 拒絕。

    只要檔案存在就回 installed_not_running；不經 async_get_integration 的
    version/blocklist/JSON 解析，故不會被那些情形誤判成 not_installed。
    """
    hass = _hass(loaded_entries=[], manifest_isfile=True)

    result = await async_detect_ha_mcp_state(hass)

    assert result == McpIntegrationState.INSTALLED_NOT_RUNNING
