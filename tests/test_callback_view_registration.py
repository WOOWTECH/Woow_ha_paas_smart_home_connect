"""Callback view is registered at both lazy entry points.

First-time UI setup never calls async_setup, so config_flow.async_step_user
must register the view; restarts with existing entries register via async_setup.
Both paths are idempotent. `hass` comes from HA core's tests/conftest.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.woow_paas_smart_home import async_setup
from custom_components.woow_paas_smart_home.config_flow import ConfigFlow
from custom_components.woow_paas_smart_home.const import (
    DATA_CALLBACK_VIEW_REGISTERED,
)


async def test_async_setup_registers_callback_view(hass: HomeAssistant) -> None:
    hass.http = MagicMock()

    assert await async_setup(hass, {}) is True

    assert hass.data.get(DATA_CALLBACK_VIEW_REGISTERED) is True
    hass.http.register_view.assert_called_once()


async def test_config_flow_step_user_registers_callback_view(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass.http = MagicMock()
    # Stub the heavy OAuth external-step path; we only assert our registration ran.
    monkeypatch.setattr(
        "homeassistant.helpers.config_entry_oauth2_flow."
        "AbstractOAuth2FlowHandler.async_step_user",
        AsyncMock(return_value={"type": "external_step"}),
    )
    flow = ConfigFlow()
    flow.hass = hass

    await flow.async_step_user(None)

    assert hass.data.get(DATA_CALLBACK_VIEW_REGISTERED) is True
