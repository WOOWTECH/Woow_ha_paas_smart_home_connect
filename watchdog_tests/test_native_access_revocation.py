"""撤權 wiring 對已安裝的 HA runtime：真 TunnelCoordinator／ConfigEntry 背景任務／sensor.

不用 HA core 的 test fixtures；平台 API 與 manager 為假物件，沒有真 token、真隧道、
真 Cloudflare。跟 test_native_entry_unload.py 一樣，從拋棄式目錄以
``custom_components/woow_paas_smart_home`` 在 PYTHONPATH 上執行。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import MappingProxyType
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntries, ConfigEntry, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.woow_paas_smart_home import (
    WoowRuntimeData,
    async_unload_entry,
)
from custom_components.woow_paas_smart_home import coordinator as coordinator_module
from custom_components.woow_paas_smart_home.api_client import (
    ApiError,
    InstanceGoneError,
)
from custom_components.woow_paas_smart_home.const import (
    DOMAIN,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    TunnelStatus,
)
from custom_components.woow_paas_smart_home.coordinator import TunnelCoordinator
from custom_components.woow_paas_smart_home.sensor import TunnelStatusSensor

DELAY = 0.05


class Result(NamedTuple):
    cutoff_verified: bool
    restarted: bool


class FakeManager:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.is_running = True
        self.has_cutoff_target = True
        self.gate: asyncio.Event | None = None
        self.reset_calls = 0

    async def reset_tunnel(self) -> Result:
        self.reset_calls += 1
        self.events.append("reset")
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.events.append("reset_cancelled")
            raise
        return Result(True, True)

    async def stop_tunnel(self) -> bool:
        self.events.append("stop")
        self.is_running = False
        return True


def _entry(product_type: str) -> ConfigEntry:
    return ConfigEntry(
        domain=DOMAIN, version=1, minor_version=1, source=SOURCE_USER,
        title="Synthetic revocation", unique_id=f"synthetic-{product_type}",
        options={}, data={"product_type": product_type, "home_name": "Synthetic"},
        discovery_keys=MappingProxyType({}), subentries_data=[],
    )


def _run(tmp_path, scenario, product_type=PRODUCT_SMART_HOME, delay=DELAY):
    async def wrapper() -> None:
        hass = HomeAssistant(str(tmp_path))
        hass.config_entries = ConfigEntries(hass, {})
        events: list[str] = []
        manager = FakeManager(events)
        api = MagicMock()
        api.get_home_status = AsyncMock()
        api.get_access_status = AsyncMock()
        entry = _entry(product_type)
        with patch.object(coordinator_module, "ACCESS_REVOCATION_DELAY", delay):
            coord = TunnelCoordinator(
                hass, api_client=api, tunnel_manager=manager, instance_id=6,
                subdomain="synthetic.invalid", config_entry=entry,
                product_type=product_type,
            )
        sensor = TunnelStatusSensor(coord, entry)
        try:
            await scenario(hass, entry, coord, api, manager, sensor, events)
        finally:
            await coord.async_cancel_access_reset()
            for task in list(entry._background_tasks):
                task.cancel()
            await asyncio.gather(*entry._background_tasks, return_exceptions=True)
            await hass.async_stop(force=True)
    asyncio.run(wrapper())


async def _poll(coord: TunnelCoordinator, api: MagicMock, payload) -> None:
    method = api.get_home_status
    if isinstance(payload, BaseException):
        method.side_effect = payload
    else:
        method.side_effect = None
        method.return_value = {"tunnel_status": "connected", **payload}
    coord.data = await coord._async_update_data()


async def _settle(entry: ConfigEntry) -> None:
    async with asyncio.timeout(5):
        while entry._background_tasks:
            await asyncio.sleep(0.01)


def test_successful_polls_drive_one_background_reset_and_sensor(tmp_path) -> None:
    async def scenario(hass, entry, coord, api, manager, sensor, events):
        updates: list[None] = []
        coord.async_add_listener(lambda: updates.append(None))
        await _poll(coord, api, {"access_revision": 3})
        assert not entry._background_tasks and manager.reset_calls == 0
        assert sensor.extra_state_attributes == {
            "access_revision": 3, "access_revision_applied": 3,
            "last_access_reset": None,
        }
        await _poll(coord, api, {"access_revision": 4})
        # 掛在 config entry 上的背景任務（卸載時 HA 也會取消）。
        assert len(entry._background_tasks) == 1
        assert manager.reset_calls == 0  # 先等傳播
        await _settle(entry)
        assert manager.reset_calls == 1 and updates  # sensor 不等下一輪 poll
        attrs = sensor.extra_state_attributes
        assert attrs["access_revision"] == attrs["access_revision_applied"] == 4
        last = attrs["last_access_reset"]
        assert (last["cutoff_verified"], last["restarted"]) == (True, True)
        assert sensor.native_value == TunnelStatus.CONNECTED
    _run(tmp_path, scenario)


def test_old_platform_payload_keeps_previous_sensor_behavior(tmp_path) -> None:
    async def scenario(hass, entry, coord, api, manager, sensor, events):
        await _poll(coord, api, {})
        await _poll(coord, api, {"access_revision": True})
        assert sensor.extra_state_attributes is None
        assert not entry._background_tasks and manager.reset_calls == 0
    _run(tmp_path, scenario)


def test_transient_api_failure_triggers_nothing(tmp_path) -> None:
    async def scenario(hass, entry, coord, api, manager, sensor, events):
        await _poll(coord, api, {"access_revision": 3})
        await _poll(coord, api, ApiError(503, "synthetic"))
        await asyncio.sleep(DELAY * 3)
        assert not entry._background_tasks and manager.reset_calls == 0
        assert sensor.extra_state_attributes["access_revision_applied"] == 3
    _run(tmp_path, scenario)


def test_instance_deleted_during_propagation_does_not_reset(tmp_path) -> None:
    async def scenario(hass, entry, coord, api, manager, sensor, events):
        await _poll(coord, api, {"access_revision": 3})
        await _poll(coord, api, {"access_revision": 4})
        # 第二次平台 JSON 404（距第一次已超過一個輪詢間隔）⇒ 判定刪除並停隧道。
        coord._gone_since = dt_util.utcnow() - timedelta(minutes=5)
        with patch.object(coordinator_module.ir, "async_create_issue"):
            await _poll(coord, api, InstanceGoneError(404, "synthetic"))
        assert coord.instance_deleted and events == ["stop"]
        await _settle(entry)
        assert manager.reset_calls == 0
        assert sensor.extra_state_attributes["access_revision_applied"] == 3
    _run(tmp_path, scenario, delay=0.3)


def _sa_routes(*policy_ids: int) -> list[dict]:
    return [{"id": 108, "is_protected": True,
             "access_policies": [{"id": pid, "decision": "allow"} for pid in policy_ids]}]


def test_security_access_resets_only_when_policy_fingerprint_changes(tmp_path) -> None:
    """SA has no numeric revision: the API's numeric field is ignored and only a
    complete route/policy readback with a changed fingerprint drives a reset."""
    async def scenario(hass, entry, coord, api, manager, sensor, events):
        async def poll(routes, **extra) -> None:
            api.get_access_status.return_value = {
                "tunnel_status": "connected", "state": "active", "routes": routes,
                **extra,
            }
            coord.data = await coord._async_update_data()

        await poll(_sa_routes(220), access_revision=1)
        await poll(_sa_routes(220), access_revision=2)  # numeric field is not consumed
        await asyncio.sleep(DELAY * 3)
        assert not entry._background_tasks and manager.reset_calls == 0

        await poll(_sa_routes())  # the allow policy was removed
        assert len(entry._background_tasks) == 1
        await _settle(entry)
        assert manager.reset_calls == 1
        attrs = sensor.extra_state_attributes
        assert attrs["product_type"] == PRODUCT_SECURITY_ACCESS
        assert attrs["access_revision"] == attrs["access_revision_applied"]
        last = attrs["last_access_reset"]
        assert (last["cutoff_verified"], last["restarted"]) == (True, True)

        # A readback without a policy list is not evidence of removal.
        await poll([{"id": 108, "is_protected": True}])
        await asyncio.sleep(DELAY * 3)
        assert not entry._background_tasks and manager.reset_calls == 1
    _run(tmp_path, scenario, product_type=PRODUCT_SECURITY_ACCESS)


@pytest.mark.parametrize("inflight", [False, True])
def test_unload_cancels_reset_before_stopping_tunnel(tmp_path, inflight) -> None:
    async def scenario(hass, entry, coord, api, manager, sensor, events):
        manager.gate = asyncio.Event()  # 讓 reset 卡在執行中
        await _poll(coord, api, {"access_revision": 3})
        await _poll(coord, api, {"access_revision": 4})
        if inflight:
            async with asyncio.timeout(5):
                while manager.reset_calls == 0:
                    await asyncio.sleep(0.01)
        (task,) = entry._background_tasks
        entry.runtime_data = WoowRuntimeData(
            api_client=api, tunnel_manager=manager, coordinator=coord,
            session=MagicMock(), unsub_tunnel_status=MagicMock(),
        )
        with patch.object(
            hass.config_entries, "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ):
            assert await async_unload_entry(hass, entry) is True
        assert task.cancelled()
        expected = ["reset", "reset_cancelled", "stop"] if inflight else ["stop"]
        assert events == expected
        assert sensor.extra_state_attributes["access_revision_applied"] == 3
    _run(tmp_path, scenario)
