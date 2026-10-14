"""Unload contracts using HA's installed runtime, without HA core test fixtures.

Run from a disposable test directory with the integration on PYTHONPATH.
Native full-entry/OS-process acceptance remains a separate lifecycle fixture.
"""
from __future__ import annotations

import asyncio
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant

from custom_components.woow_paas_smart_home import (
    WoowRuntimeData,
    async_unload_entry,
)
from custom_components.woow_paas_smart_home.const import DOMAIN, PLATFORMS


async def _check_unload(tmp_path, stopped: bool, platform_result: bool) -> None:
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    entry = ConfigEntry(
        domain=DOMAIN, version=1, minor_version=1, source=SOURCE_USER,
        title="Synthetic unload contract", unique_id="synthetic-unload",
        options={}, data={}, discovery_keys=MappingProxyType({}), subentries_data=[],
    )
    manager = MagicMock()
    manager.stop_tunnel = AsyncMock(return_value=stopped)
    unsubscribe = MagicMock()
    runtime = WoowRuntimeData(
        api_client=MagicMock(), tunnel_manager=manager,
        coordinator=MagicMock(async_cancel_access_reset=AsyncMock()),
        session=MagicMock(),
        unsub_tunnel_status=unsubscribe,
    )
    entry.runtime_data = runtime
    try:
        with patch.object(
            hass.config_entries, "async_unload_platforms",
            new=AsyncMock(return_value=platform_result),
        ) as unload:
            assert await async_unload_entry(hass, entry) is (stopped and platform_result)
        manager.stop_tunnel.assert_awaited_once()
        if stopped:
            unload.assert_awaited_once_with(entry, PLATFORMS)
        else:
            unload.assert_not_awaited()
        assert entry.runtime_data is runtime
        if stopped and platform_result:
            unsubscribe.assert_called_once_with()
        else:
            unsubscribe.assert_not_called()
    finally:
        await hass.async_stop(force=True)


def test_unconfirmed_stop_preserves_platform_ownership(tmp_path) -> None:
    """Do not unload sensors while process cleanup remains unconfirmed."""
    asyncio.run(_check_unload(tmp_path, False, True))


@pytest.mark.parametrize("platform_result", [True, False])
def test_confirmed_stop_forwards_platform_result(tmp_path, platform_result) -> None:
    """Confirmed process cleanup still respects native platform failures."""
    asyncio.run(_check_unload(tmp_path, True, platform_result))
