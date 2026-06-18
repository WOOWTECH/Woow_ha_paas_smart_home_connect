"""Smoke-lock HA Core's private state-JWT helpers used by the callback view.

WoowOAuth2CallbackView reuses _decode_jwt; if a HA upgrade changes the helper's
name or round-trip behaviour, this fails loudly so we catch drift in CI.
`hass` comes from HA core's tests/conftest.py.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import _decode_jwt, _encode_jwt


async def test_state_jwt_roundtrip(hass: HomeAssistant) -> None:
    payload = {"flow_id": "abc", "redirect_uri": "https://ha.example.com/x"}

    token = _encode_jwt(hass, payload)
    decoded = _decode_jwt(hass, token)

    assert decoded is not None
    assert decoded["flow_id"] == "abc"
    assert decoded["redirect_uri"] == "https://ha.example.com/x"


async def test_decode_rejects_tampered_token(hass: HomeAssistant) -> None:
    # Seed the per-hass secret, then decode garbage -> None (the CSRF backstop).
    _encode_jwt(hass, {"flow_id": "x"})
    assert _decode_jwt(hass, "tampered.jwt.value") is None
