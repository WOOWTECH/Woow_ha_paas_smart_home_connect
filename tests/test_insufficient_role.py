"""Platform 403 ``insufficient_role``: members who are not owner/admin.

The platform only hands tunnel tokens to workspace owners/admins and answers
everyone else with 403 ``{"error": "insufficient_role"}``. That is a different
situation from a bad/under-scoped token (also 403, ``error: "Forbidden"``):
re-authorizing cannot change a role, so the flow must say "ask an owner/admin"
instead of sending the user into reauth.
"""
from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.woow_paas_smart_home import config_flow as cf
from custom_components.woow_paas_smart_home.api_client import (
    ApiError,
    AuthenticationError,
    InsufficientRoleError,
    NotFoundError,
    WoowPaasApiClient,
    api_error_for,
)
from custom_components.woow_paas_smart_home.config_flow import ConfigFlow
from custom_components.woow_paas_smart_home.const import PRODUCT_SMART_HOME

_ROLE_BODY = {
    "error": "insufficient_role",
    "detail": "Only workspace owners and admins can get the tunnel token.",
}


def _response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    return resp


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (HTTPStatus.FORBIDDEN, "insufficient_role", InsufficientRoleError),
        (HTTPStatus.FORBIDDEN, "Forbidden", AuthenticationError),
        (HTTPStatus.UNAUTHORIZED, "insufficient_role", AuthenticationError),
        (HTTPStatus.NOT_FOUND, "", NotFoundError),
        (HTTPStatus.INTERNAL_SERVER_ERROR, "", ApiError),
    ],
)
def test_api_error_for_maps_status_and_code(status, code, expected) -> None:
    err = api_error_for(status, code, "msg")
    assert type(err) is expected
    assert err.status == status


def test_insufficient_role_is_still_an_authentication_error() -> None:
    # 既有只認 AuthenticationError 的呼叫點不會漏接。
    assert issubclass(InsufficientRoleError, AuthenticationError)


async def test_api_client_raises_insufficient_role() -> None:
    session = MagicMock()
    session.async_request = AsyncMock(
        return_value=_response(HTTPStatus.FORBIDDEN, _ROLE_BODY)
    )
    client = WoowPaasApiClient(session, "https://paas.example")

    with pytest.raises(InsufficientRoleError) as exc:
        await client.get_tunnel_token(6)

    assert "owners and admins" in str(exc.value)


async def test_flow_api_request_raises_insufficient_role(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cf,
        "async_oauth2_request",
        AsyncMock(return_value=_response(HTTPStatus.FORBIDDEN, _ROLE_BODY)),
    )
    flow = ConfigFlow()
    flow.hass = hass
    flow._oauth_data = {"token": {"access_token": "at"}}

    with pytest.raises(InsufficientRoleError):
        await flow._async_api_request("GET", "/api/smarthome/homes/6/tunnel-token")


async def test_create_entry_aborts_with_insufficient_role(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = ConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow._oauth_data = {"auth_implementation": "woow_paas_smart_home", "token": {}}
    flow._selected_workspace_id = 3
    flow._selected_workspace_name = "HA Connect Test 0923"
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock())
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", MagicMock())
    monkeypatch.setattr(
        flow,
        "_async_api_request",
        AsyncMock(side_effect=InsufficientRoleError(403, "403 insufficient_role")),
    )

    result = await flow._async_create_entry(
        product_type=PRODUCT_SMART_HOME, instance_id=6, instance_name="sh-e2e-oauth"
    )

    # abort 而不是表單：HA 前端在空欄位表單上不顯示 base 錯誤（見 test_translations）。
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "insufficient_role"
