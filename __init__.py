"""The Woow PaaS Smart Home integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.typing import ConfigType

from .api_client import WoowPaasApiClient
from .cloudflared_manager import CloudflaredManager
from .const import (
    API_BASE_URL,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_TUNNEL_TOKEN,
    CONF_WORKSPACE_ID,
    DOMAIN,
    PLATFORMS,
    SERVICE_GET_STATUS,
    SERVICE_START_TUNNEL,
    SERVICE_STOP_TUNNEL,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA = vol.Schema(
    {vol.Required("home_id"): vol.Coerce(str)}
)

type WoowConfigEntry = ConfigEntry


def _find_entry_data_by_home_id(
    hass: HomeAssistant, home_id: str
) -> dict[str, Any] | None:
    """Find the entry data dict that matches a given home_id."""
    for entry_id, data in hass.data.get(DOMAIN, {}).items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and str(entry.data.get(CONF_HOME_ID)) == str(home_id):
            return data
    return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Woow PaaS Smart Home component."""
    hass.data.setdefault(DOMAIN, {})

    async def _handle_start_tunnel(call: ServiceCall) -> None:
        """Handle start_tunnel service call."""
        home_id = call.data["home_id"]
        entry_data = _find_entry_data_by_home_id(hass, home_id)
        if entry_data is None:
            _LOGGER.error("No config entry found for home_id=%s", home_id)
            return

        manager: CloudflaredManager = entry_data["tunnel_manager"]
        # Find the tunnel token from the config entry
        for entry_id, data in hass.data[DOMAIN].items():
            if data is entry_data:
                entry = hass.config_entries.async_get_entry(entry_id)
                if entry:
                    tunnel_token = entry.data.get(CONF_TUNNEL_TOKEN)
                    if tunnel_token:
                        await manager.ensure_binary()
                        await manager.start_tunnel(tunnel_token)
                    else:
                        _LOGGER.error(
                            "No tunnel token for home_id=%s", home_id
                        )
                break

    async def _handle_stop_tunnel(call: ServiceCall) -> None:
        """Handle stop_tunnel service call."""
        home_id = call.data["home_id"]
        entry_data = _find_entry_data_by_home_id(hass, home_id)
        if entry_data is None:
            _LOGGER.error("No config entry found for home_id=%s", home_id)
            return

        manager: CloudflaredManager = entry_data["tunnel_manager"]
        await manager.stop_tunnel()

    async def _handle_get_status(call: ServiceCall) -> ServiceResponse:
        """Handle get_status service call."""
        home_id = call.data["home_id"]
        entry_data = _find_entry_data_by_home_id(hass, home_id)
        if entry_data is None:
            _LOGGER.error("No config entry found for home_id=%s", home_id)
            return {"error": f"No config entry found for home_id={home_id}"}

        manager: CloudflaredManager = entry_data["tunnel_manager"]

        # Find the config entry for additional info
        entry_info: dict[str, Any] = {}
        for entry_id, data in hass.data[DOMAIN].items():
            if data is entry_data:
                entry = hass.config_entries.async_get_entry(entry_id)
                if entry:
                    entry_info = {
                        "home_id": str(entry.data.get(CONF_HOME_ID, "")),
                        "home_name": entry.data.get(CONF_HOME_NAME, ""),
                        "workspace_id": str(
                            entry.data.get(CONF_WORKSPACE_ID, "")
                        ),
                    }
                break

        return {
            "tunnel_running": manager.is_running,
            **entry_info,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_START_TUNNEL,
        _handle_start_tunnel,
        schema=SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_TUNNEL,
        _handle_stop_tunnel,
        schema=SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_STATUS,
        _handle_get_status,
        schema=SERVICE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: WoowConfigEntry) -> bool:
    """Set up Woow PaaS Smart Home from a config entry."""
    implementation = await async_get_config_entry_implementation(hass, entry)
    session = OAuth2Session(hass, entry, implementation)

    api_client = WoowPaasApiClient(session, API_BASE_URL)
    tunnel_manager = CloudflaredManager(hass)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api_client": api_client,
        "tunnel_manager": tunnel_manager,
        "coordinator": None,  # placeholder, Task 007 will set this up
        "session": session,
    }

    # Start tunnel in background (non-blocking)
    async def _start_tunnel_background() -> None:
        """Download cloudflared binary and start tunnel in background."""
        try:
            await asyncio.sleep(1)  # let HA finish startup
            await tunnel_manager.ensure_binary()
            tunnel_token = entry.data.get(CONF_TUNNEL_TOKEN)
            if tunnel_token:
                await tunnel_manager.start_tunnel(tunnel_token)
        except Exception:
            _LOGGER.warning(
                "Failed to start tunnel in background", exc_info=True
            )

    hass.async_create_task(
        _start_tunnel_background(), eager_start=False
    )

    # Forward entry setup to sensor and binary_sensor platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: WoowConfigEntry
) -> bool:
    """Unload a Woow PaaS Smart Home config entry."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id)

    # Stop the cloudflared tunnel
    if entry_data:
        manager: CloudflaredManager = entry_data["tunnel_manager"]
        await manager.stop_tunnel()

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    # Clean up hass.data
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
