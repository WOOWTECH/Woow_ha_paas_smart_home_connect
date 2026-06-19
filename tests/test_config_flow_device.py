"""Unit tests for the device-flow (RFC 8628) onboarding path.

The HA Companion App can't complete the redirect/window.open web flow in its
WebView (HA-wide limitation; design §11), so app requests are routed to the
device flow. `hass` comes from HA core's tests/conftest.py (wired in conftest).
"""
from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import http

from custom_components.woow_paas_smart_home import config_flow as cf
from custom_components.woow_paas_smart_home.api_client import ApiError
from custom_components.woow_paas_smart_home.config_flow import (
    ConfigFlow,
    _is_companion_app,
    _request_user_agent,
)

IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "Home Assistant/2024.12.0 (io.robbie.HomeAssistant; build:2024.1234; iOS 17.5)"
)
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/126.0 Mobile Safari/537.36 "
    "Home Assistant/2024.12.0-12345 (Android 14; wv)"
)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def test_is_companion_app_detects_ios_and_android() -> None:
    assert _is_companion_app(IOS_UA) is True
    assert _is_companion_app(ANDROID_UA) is True


def test_is_companion_app_rejects_browser_and_empty() -> None:
    assert _is_companion_app(BROWSER_UA) is False
    assert _is_companion_app("") is False


async def test_request_user_agent_reads_current_request() -> None:
    request = MagicMock()
    request.headers = {"User-Agent": IOS_UA}
    token = http.current_request.set(request)
    try:
        assert _request_user_agent() == IOS_UA
    finally:
        http.current_request.reset(token)


async def test_request_user_agent_without_request() -> None:
    assert _request_user_agent() == ""


async def test_async_step_user_routes_app_to_device(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.http = MagicMock()
    flow = ConfigFlow()
    flow.hass = hass
    device_step = AsyncMock(return_value={"type": "progress"})
    monkeypatch.setattr(flow, "async_step_device", device_step)
    request = MagicMock()
    request.headers = {"User-Agent": ANDROID_UA}

    token = http.current_request.set(request)
    try:
        await flow.async_step_user(None)
    finally:
        http.current_request.reset(token)

    device_step.assert_awaited_once()


async def test_async_step_user_routes_browser_to_web_flow(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.http = MagicMock()
    flow = ConfigFlow()
    flow.hass = hass
    device_step = AsyncMock()
    monkeypatch.setattr(flow, "async_step_device", device_step)
    super_step = AsyncMock(return_value={"type": "external_step"})
    monkeypatch.setattr(
        "homeassistant.helpers.config_entry_oauth2_flow."
        "AbstractOAuth2FlowHandler.async_step_user",
        super_step,
    )
    request = MagicMock()
    request.headers = {"User-Agent": BROWSER_UA}

    token = http.current_request.set(request)
    try:
        await flow.async_step_user(None)
    finally:
        http.current_request.reset(token)

    device_step.assert_not_awaited()
    super_step.assert_awaited_once()


def _response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    return resp


def _mock_session(monkeypatch: pytest.MonkeyPatch, responses: list) -> AsyncMock:
    session = MagicMock()
    session.post = AsyncMock(side_effect=responses)
    monkeypatch.setattr(cf, "async_get_clientsession", lambda hass: session)
    return session.post


async def test_request_device_code_posts_and_returns(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "device_code": "dc",
        "user_code": "BCDF-GHJK",
        "verification_uri": "https://stg.woowtech.io/device",
        "verification_uri_complete": "https://stg.woowtech.io/device?code=BCDF-GHJK",
        "expires_in": 900,
        "interval": 5,
    }
    post = _mock_session(monkeypatch, [_response(HTTPStatus.OK, payload)])
    flow = ConfigFlow()
    flow.hass = hass

    result = await flow._async_request_device_code()

    assert result == payload
    args, kwargs = post.await_args
    assert args[0].endswith("/oauth2/device_authorization")
    assert kwargs["data"]["client_id"] == "woow-ha-smart-home"
    assert "scope" in kwargs["data"]


async def test_poll_device_token_pending_then_slowdown_then_success(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf.asyncio, "sleep", AsyncMock())
    token = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    _mock_session(
        monkeypatch,
        [
            _response(HTTPStatus.BAD_REQUEST, {"error": "authorization_pending"}),
            _response(HTTPStatus.BAD_REQUEST, {"error": "slow_down"}),
            _response(HTTPStatus.OK, token),
        ],
    )
    flow = ConfigFlow()
    flow.hass = hass
    flow._device_flow = {"device_code": "dc", "interval": 0, "expires_in": 900}

    await flow._async_poll_device_token()

    assert flow._device_token is not None
    assert flow._device_token["access_token"] == "at"
    assert "expires_at" in flow._device_token


async def test_poll_device_token_access_denied_raises(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cf.asyncio, "sleep", AsyncMock())
    _mock_session(
        monkeypatch,
        [_response(HTTPStatus.BAD_REQUEST, {"error": "access_denied"})],
    )
    flow = ConfigFlow()
    flow.hass = hass
    flow._device_flow = {"device_code": "dc", "interval": 0, "expires_in": 900}

    with pytest.raises(ApiError):
        await flow._async_poll_device_token()

    assert flow._device_token is None


async def test_device_finish_reuses_oauth_create_entry(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = ConfigFlow()
    flow.hass = hass
    flow._device_token = {"access_token": "at", "expires_at": 1}
    create_entry = AsyncMock(return_value={"type": "form"})
    monkeypatch.setattr(flow, "async_oauth_create_entry", create_entry)

    await flow.async_step_device_finish()

    create_entry.assert_awaited_once()
    passed = create_entry.await_args.args[0]
    assert passed["token"] == {"access_token": "at", "expires_at": 1}
    assert passed["auth_implementation"] == "woow_paas_smart_home"
