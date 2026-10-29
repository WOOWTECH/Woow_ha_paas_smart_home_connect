"""Service-first onboarding, workspace choice and explicit trial activation."""
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.woow_paas_smart_home.config_flow import ConfigFlow
from custom_components.woow_paas_smart_home.const import PRODUCT_SMART_HOME
from custom_components.woow_paas_smart_home.api_client import ApiError


@pytest.fixture
def flow(hass):
    hass.http = MagicMock(server_port=8123)
    result = ConfigFlow()
    result.hass = hass
    result._service_kind = "remote_access_mcp"
    result._oauth_data = {"token": {"access_token": "test-only"}}
    return result


async def test_no_workspace_starts_creation(flow, monkeypatch):
    request = AsyncMock(return_value={"workspaces": [], "has_memberships": False})
    onboard = AsyncMock(return_value={"step_id": "onboarding"})
    monkeypatch.setattr(flow, "_async_api_request", request)
    monkeypatch.setattr(flow, "async_step_onboarding", onboard)
    result = await flow.async_step_select_workspace()
    assert result["step_id"] == "onboarding"
    assert flow._selected_workspace_id is None
    assert "service_kind=remote_access_mcp" in request.await_args.args[1]


async def test_member_without_admin_cannot_create_workspace(flow, monkeypatch):
    monkeypatch.setattr(flow, "_async_api_request", AsyncMock(
        return_value={"workspaces": [], "has_memberships": True}))
    onboard = AsyncMock()
    monkeypatch.setattr(flow, "async_step_onboarding", onboard)
    result = await flow.async_step_select_workspace()
    assert result["reason"] == "insufficient_role"
    onboard.assert_not_awaited()


async def test_multiple_workspaces_require_selection_then_matching_services(flow, monkeypatch):
    request = AsyncMock(side_effect=[
        {"workspaces": [{"id": 1, "name": "甲"}, {"id": 2, "name": "乙"}]},
        {"homes": [{"id": 10, "name": "MCP 甲"}, {"id": 11, "name": "MCP 乙"}]},
    ])
    monkeypatch.setattr(flow, "_async_api_request", request)
    result = await flow.async_step_select_workspace()
    assert result["step_id"] == "select_workspace"
    result = await flow.async_step_select_workspace({"workspace": "2"})
    assert result["step_id"] == "select_home"
    assert "workspace_id=2" in request.await_args.args[1]
    assert "service_kind=remote_access_mcp" in request.await_args.args[1]
    assert flow._selected_workspace_id == 2


async def test_missing_service_in_selected_workspace_is_created(flow, monkeypatch):
    flow._selected_workspace_id = 2
    monkeypatch.setattr(flow, "_async_api_request", AsyncMock(return_value={"homes": []}))
    onboard = AsyncMock(return_value={"step_id": "onboarding"})
    monkeypatch.setattr(flow, "async_step_onboarding", onboard)
    assert (await flow.async_step_select_home())["step_id"] == "onboarding"
    assert flow._onboarding_home is None
    onboard.assert_awaited_once()


def _select_home_options(result):
    return next(iter(result["data_schema"].schema.values())).container


async def test_single_free_service_is_connected_without_asking(flow, monkeypatch):
    flow._selected_workspace_id = 2
    home = {"id": 3, "name": "MCP", "binding_state": "unbound", "connector_count": 0}
    monkeypatch.setattr(flow, "_async_api_request", AsyncMock(return_value={"homes": [home]}))
    onboard = AsyncMock(return_value={"step_id": "onboarding"})
    monkeypatch.setattr(flow, "async_step_onboarding", onboard)
    assert (await flow.async_step_select_home())["step_id"] == "onboarding"
    assert flow._onboarding_home == home


@pytest.mark.parametrize("home, label", [
    ({"binding_state": "claimed", "bound_instance_label": "客廳 HA"}, "MCP（已連結：客廳 HA）"),
    ({"binding_state": "claimed"}, "MCP（已有其他 Home Assistant 連線）"),
    ({"binding_state": "legacy"}, "MCP（已有其他 Home Assistant 連線）"),
    ({"binding_state": "unbound", "connector_count": 1}, "MCP（已有其他 Home Assistant 連線）"),
    # A platform that does not say: still ask, without claiming the service is used.
    ({}, "MCP"),
], ids=["claimed-labelled", "claimed", "legacy", "connector-online", "unknown"])
async def test_single_service_in_use_needs_explicit_choice(flow, monkeypatch, home, label):
    flow._selected_workspace_id = 2
    home = {"id": 3, "name": "MCP", **home}
    monkeypatch.setattr(flow, "_async_api_request", AsyncMock(return_value={"homes": [home]}))
    onboard = AsyncMock()
    monkeypatch.setattr(flow, "async_step_onboarding", onboard)
    result = await flow.async_step_select_home()
    assert result["step_id"] == "select_home"
    assert _select_home_options(result) == {"3": label}
    assert flow._onboarding_home is None
    onboard.assert_not_awaited()


async def test_choosing_the_service_in_use_continues_to_onboarding(flow, monkeypatch):
    flow._selected_workspace_id = 2
    home = {"id": 3, "name": "MCP", "binding_state": "claimed"}
    flow._homes = [home]
    onboard = AsyncMock(return_value={"step_id": "onboarding"})
    monkeypatch.setattr(flow, "async_step_onboarding", onboard)
    assert (await flow.async_step_select_home({"home": "3"}))["step_id"] == "onboarding"
    assert flow._onboarding_home == home


def _claim_flow(flow, monkeypatch):
    """A flow that is about to claim a service; returns the mocked platform request."""
    flow.context = {"source": "user"}
    flow._oauth_data = {"auth_implementation": "woow_paas_smart_home", "token": {}}
    flow._selected_workspace_id = 2
    flow._selected_workspace_name = "乙"
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", MagicMock())
    request = AsyncMock(return_value={
        "tunnel_token": "tok", "tunnel_id": "tid", "subdomain": "s.example"})
    monkeypatch.setattr(flow, "_async_api_request", request)
    return request


def _claim_query(request):
    return parse_qs(urlsplit(request.await_args.args[1]).query)


async def test_setup_claim_says_it_is_a_new_entry_that_nobody_confirmed(flow, monkeypatch):
    request = _claim_flow(flow, monkeypatch)
    await flow._async_create_entry(
        product_type=PRODUCT_SMART_HOME, instance_id=3, instance_name="MCP")
    query = _claim_query(request)
    assert request.await_args.args[1].startswith("/api/smarthome/homes/3/tunnel-token?")
    assert query["new_entry"] == ["1"]
    assert query["confirmed"] == ["0"]
    assert query["instance_uid"]


async def test_choosing_the_service_from_the_list_confirms_the_claim(flow, monkeypatch):
    request = _claim_flow(flow, monkeypatch)
    home = {"id": 3, "name": "MCP", "binding_state": "legacy"}
    flow._homes = [home]
    monkeypatch.setattr(flow, "async_step_onboarding", AsyncMock(return_value={"step_id": "onboarding"}))
    await flow.async_step_select_home({"home": "3"})
    assert flow._claim_confirmed is True
    await flow._async_create_entry(
        product_type=PRODUCT_SMART_HOME, instance_id=3, instance_name="MCP")
    assert _claim_query(request)["confirmed"] == ["1"]


async def test_a_service_selected_without_asking_is_not_confirmed(flow, monkeypatch):
    flow._selected_workspace_id = 2
    flow._claim_confirmed = True  # left over from an earlier choice in the same flow
    free = {"id": 3, "name": "MCP", "binding_state": "unbound", "connector_count": 0}
    monkeypatch.setattr(flow, "_async_api_request", AsyncMock(return_value={"homes": [free]}))
    monkeypatch.setattr(flow, "async_step_onboarding", AsyncMock(return_value={"step_id": "onboarding"}))
    await flow.async_step_select_home()
    assert flow._onboarding_home == free
    assert flow._claim_confirmed is False


async def test_choosing_a_security_access_confirms_the_claim(flow, monkeypatch):
    request = _claim_flow(flow, monkeypatch)
    flow._accesses = [{"id": 5, "name": "Gateway"}]
    request.return_value = {"tunnel_token": "tok", "tunnel_id": "tid"}
    await flow.async_step_select_access({"access": "5"})
    assert request.await_args.args[1].startswith("/api/security-access/accesses/5/tunnel-token?")
    query = _claim_query(request)
    assert query["new_entry"] == ["1"]
    assert query["confirmed"] == ["1"]


@pytest.mark.parametrize("workspace_id", [None, 2])
async def test_create_prepare_wait_preserves_choice(flow, monkeypatch, workspace_id):
    flow._selected_workspace_id = workspace_id
    home = {"id": 3, "name": "MCP", "state": "pending"}
    request = AsyncMock(side_effect=[
        {"workspace": {"id": 2, "name": "乙"}, "homes": [home]},
        {"home": home},
        {"home": {**home, "state": "active"}},
    ])
    monkeypatch.setattr(flow, "_async_api_request", request)
    await flow._async_onboard()
    assert request.await_args_list[0].kwargs["json"] == {
        "service_kind": "remote_access_mcp", "workspace_id": workspace_id,
        "ha_port": 8123}
    assert request.await_args_list[1].args == (
        "POST", "/api/smarthome/onboarding/homes/3/prepare")
    assert flow._onboarding_home["state"] == "active"


@pytest.mark.parametrize("ha_port", [80, 18243])
async def test_new_service_uses_actual_ha_listener_port(flow, monkeypatch, ha_port):
    flow.hass.http.server_port = ha_port
    home = {"id": 3, "name": "MCP", "state": "active"}
    request = AsyncMock(side_effect=[
        {"workspace": {"id": 2, "name": "乙"}, "homes": [home]},
        {"home": home}, {"home": home},
    ])
    monkeypatch.setattr(flow, "_async_api_request", request)
    await flow._async_onboard()
    assert request.await_args_list[0].kwargs["json"]["ha_port"] == ha_port


async def test_existing_service_does_not_create_duplicate(flow, monkeypatch):
    home = {"id": 3, "name": "MCP", "state": "active"}
    flow._onboarding_home = home
    request = AsyncMock(side_effect=[{"home": home}, {"home": home}])
    monkeypatch.setattr(flow, "_async_api_request", request)
    await flow._async_onboard()
    assert len(request.await_args_list) == 2
    assert request.await_args_list[0].args[1].endswith("/3/prepare")


async def test_expired_trial_requires_explicit_paid_activation(flow, monkeypatch):
    flow._onboarding_home = {"id": 3, "subscription": {"trial_expired": True}}
    request = AsyncMock()
    monkeypatch.setattr(flow, "_async_api_request", request)
    await flow._async_onboard()
    result = await flow.async_step_onboarding_finish()
    assert result["reason"] == "trial_expired"
    request.assert_not_awaited()


async def test_concurrent_creation_returns_user_to_selection(flow, monkeypatch):
    request = AsyncMock(return_value={"workspace": {"id": 2, "name": "乙"},
        "homes": [{"id": 3}, {"id": 4}]})
    monkeypatch.setattr(flow, "_async_api_request", request)
    select = AsyncMock(return_value={"step_id": "select_home"})
    monkeypatch.setattr(flow, "async_step_select_home", select)
    await flow._async_onboard()
    assert flow._onboarding_home is None
    assert (await flow.async_step_onboarding_finish())["step_id"] == "select_home"
    request.assert_awaited_once()


@pytest.mark.parametrize("state", ["error", "suspended", "deleting"])
async def test_failed_setup_never_claims_connected(flow, monkeypatch, state):
    flow._onboarding_home = {"id": 3, "name": "MCP", "state": "pending"}
    request = AsyncMock(side_effect=[{}, {"home": {"id": 3, "state": state}}])
    monkeypatch.setattr(flow, "_async_api_request", request)
    with pytest.raises(ApiError):
        await flow._async_onboard()


async def test_invalid_workspace_clears_stale_selection(flow):
    flow._workspaces = [{"id": 2, "name": "乙"}]
    flow._selected_workspace_id = 2
    flow._onboarding_home = {"id": 3}
    assert (await flow.async_step_select_workspace({"workspace": "99"}))["reason"] == "insufficient_role"
    assert flow._selected_workspace_id is None
    assert flow._onboarding_home is None
