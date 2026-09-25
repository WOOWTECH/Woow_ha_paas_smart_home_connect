"""Unit tests for WoowPaasOAuth2.redirect_uri override.

`hass` fixture comes from HA core's tests/conftest.py (wired in conftest.py).
The override mirrors HA Core's non-`my` branch but always targets the WOOW
callback path, so the my.home-assistant.io relay is never used.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import http as helpers_http

from custom_components.woow_paas_smart_home.const import WOOW_AUTH_CALLBACK_PATH
from custom_components.woow_paas_smart_home.oauth2 import create_implementation


def _request_with_base(base: str) -> MagicMock:
    request = MagicMock()
    request.headers = {"HA-Frontend-Base": base}
    return request


async def test_redirect_uri_targets_local_woow_callback(hass: HomeAssistant) -> None:
    token = helpers_http.current_request.set(_request_with_base("https://ha.example.com"))
    try:
        impl = create_implementation(hass)
        assert (
            impl.redirect_uri
            == f"https://ha.example.com{WOOW_AUTH_CALLBACK_PATH}"
        )
    finally:
        helpers_http.current_request.reset(token)


async def test_redirect_uri_never_uses_my_relay_even_when_my_loaded(
    hass: HomeAssistant,
) -> None:
    hass.config.components.add("my")
    token = helpers_http.current_request.set(_request_with_base("http://homeassistant.local:8123"))
    try:
        impl = create_implementation(hass)
        uri = impl.redirect_uri
        assert uri == f"http://homeassistant.local:8123{WOOW_AUTH_CALLBACK_PATH}"
        assert "my.home-assistant.io" not in uri
    finally:
        helpers_http.current_request.reset(token)


async def test_redirect_uri_raises_without_request(hass: HomeAssistant) -> None:
    impl = create_implementation(hass)
    with pytest.raises(RuntimeError):
        _ = impl.redirect_uri
