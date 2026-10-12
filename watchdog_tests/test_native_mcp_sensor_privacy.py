"""Sensor privacy contracts against installed HA runtime; no core test fixtures."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.woow_paas_smart_home.const import McpIntegrationState, TunnelStatus
from custom_components.woow_paas_smart_home.coordinator import TunnelStatusData
from custom_components.woow_paas_smart_home.sensor import McpConnectUrlSensor


def _sensor(webhook_id="mcp_"+"a"*32, base="https://synthetic.invalid"):
    entry=MagicMock()
    entry.entry_id="stable_test_entry"
    entry.data={}
    coordinator=SimpleNamespace(data=TunnelStatusData(
        status=TunnelStatus.CONNECTED,subdomain_url=base,mcp="ha_mcp_tools",
        mcp_integration_state=McpIntegrationState.RUNNING,mcp_webhook_id=webhook_id,
    ),last_update_success=True)
    return McpConnectUrlSensor(coordinator,entry)


def test_available_state_never_publishes_endpoint():
    sensor=_sensor()
    assert sensor.native_value=="available"
    assert sensor.extra_state_attributes is None


@pytest.mark.parametrize("webhook_id,base",[(None,"https://synthetic.invalid"),("mcp_"+"a"*32,"")])
def test_incomplete_endpoint_has_safe_status(webhook_id,base):
    sensor=_sensor(webhook_id,base)
    assert sensor.native_value=="unavailable"
    assert sensor.extra_state_attributes is None


def test_missing_data_never_publishes_previous_credential():
    sensor=_sensor()
    sensor.coordinator.data=None
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


def test_existing_unique_id_is_preserved():
    assert _sensor().unique_id=="stable_test_entry_mcp_connect_url"
