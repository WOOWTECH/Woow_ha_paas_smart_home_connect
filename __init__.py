"""The Woow PaaS Smart Home integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.typing import ConfigType

from .api_client import ApiError, AuthenticationError, WoowPaasApiClient
from .cloudflared_manager import CloudflaredManager
from .const import (
    API_BASE_URL,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_SUBDOMAIN,
    CONF_TUNNEL_ID,
    CONF_TUNNEL_TOKEN,
    CONF_WORKSPACE_ID,
    DOMAIN,
    PLATFORMS,
    SERVICE_GET_STATUS,
    SERVICE_START_TUNNEL,
    SERVICE_STOP_TUNNEL,
)
from .coordinator import TunnelCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA = vol.Schema(
    {vol.Required("home_id"): vol.Coerce(str)}
)

type WoowConfigEntry = ConfigEntry[WoowRuntimeData]


@dataclass(frozen=True)
class WoowRuntimeData:
    """Runtime data stored in entry.runtime_data for each config entry."""

    api_client: WoowPaasApiClient
    tunnel_manager: CloudflaredManager
    coordinator: TunnelCoordinator
    session: OAuth2Session


def _find_entry_and_data(
    hass: HomeAssistant, home_id: str
) -> tuple[WoowConfigEntry, WoowRuntimeData] | None:
    """Find the loaded config entry and runtime data that match a given home_id."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(CONF_HOME_ID)) == str(home_id):
            runtime_data = getattr(entry, "runtime_data", None)
            if runtime_data is not None:
                return entry, runtime_data
    return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Woow PaaS Smart Home component."""

    async def _handle_start_tunnel(call: ServiceCall) -> None:
        """Handle start_tunnel service call."""
        home_id = call.data["home_id"]
        result = _find_entry_and_data(hass, home_id)
        if result is None:
            raise HomeAssistantError(
                f"No config entry found for home_id={home_id}"
            )
        entry, runtime_data = result

        tunnel_token = entry.data.get(CONF_TUNNEL_TOKEN)
        if not tunnel_token:
            raise HomeAssistantError(
                f"No tunnel token for home_id={home_id}"
            )

        manager = runtime_data.tunnel_manager
        if not await manager.ensure_binary():
            raise HomeAssistantError("Failed to download cloudflared binary")

        if not await manager.start_tunnel(tunnel_token):
            raise HomeAssistantError("Failed to start cloudflared tunnel")

    async def _handle_stop_tunnel(call: ServiceCall) -> None:
        """Handle stop_tunnel service call."""
        home_id = call.data["home_id"]
        result = _find_entry_and_data(hass, home_id)
        if result is None:
            raise HomeAssistantError(
                f"No config entry found for home_id={home_id}"
            )
        _, runtime_data = result

        if not await runtime_data.tunnel_manager.stop_tunnel():
            raise HomeAssistantError("Failed to stop cloudflared tunnel")

    async def _handle_get_status(call: ServiceCall) -> ServiceResponse:
        """Handle get_status service call."""
        home_id = call.data["home_id"]
        result = _find_entry_and_data(hass, home_id)
        if result is None:
            raise HomeAssistantError(
                f"No config entry found for home_id={home_id}"
            )
        entry, runtime_data = result

        coord_data = runtime_data.coordinator.data
        return {
            "tunnel_running": runtime_data.tunnel_manager.is_running,
            "tunnel_status": coord_data.status if coord_data else "unknown",
            "tunnel_connected": coord_data.is_connected if coord_data else False,
            "home_id": str(entry.data.get(CONF_HOME_ID, "")),
            "home_name": entry.data.get(CONF_HOME_NAME, ""),
            "workspace_id": str(entry.data.get(CONF_WORKSPACE_ID, "")),
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

    # Re-fetch tunnel token on startup to ensure freshness
    home_id: int = entry.data[CONF_HOME_ID]
    try:
        token_data = await api_client.get_tunnel_token(home_id)
        new_data = {
            **entry.data,
            CONF_TUNNEL_TOKEN: token_data["tunnel_token"],
            CONF_TUNNEL_ID: token_data["tunnel_id"],
            CONF_SUBDOMAIN: token_data["subdomain"],
        }
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.debug("Refreshed tunnel token for home %s", home_id)
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(
            "Authentication failed during tunnel token refresh"
        ) from err
    except (ApiError, aiohttp.ClientError, TimeoutError):
        _LOGGER.warning(
            "Failed to refresh tunnel token on startup due to transient error, "
            "using cached token",
            exc_info=True,
        )

    coordinator = TunnelCoordinator(
        hass,
        api_client=api_client,
        tunnel_manager=tunnel_manager,
        home_id=home_id,
        subdomain=entry.data.get(CONF_SUBDOMAIN, ""),
        config_entry=entry,
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = WoowRuntimeData(
        api_client=api_client,
        tunnel_manager=tunnel_manager,
        coordinator=coordinator,
        session=session,
    )

    # Start tunnel in background (non-blocking)
    async def _start_tunnel_background() -> None:
        """Download cloudflared binary and start tunnel in background."""
        try:
            await asyncio.sleep(1)  # let HA finish startup
            if not await tunnel_manager.ensure_binary():
                _LOGGER.error("Failed to download cloudflared binary in background")
                return
            tunnel_token = entry.data.get(CONF_TUNNEL_TOKEN)
            if not tunnel_token:
                _LOGGER.error(
                    "No tunnel token available for home %s; tunnel will not start",
                    home_id,
                )
                return
            if not await tunnel_manager.start_tunnel(tunnel_token):
                _LOGGER.error(
                    "Failed to start cloudflared tunnel for home %s in background",
                    home_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Failed to start tunnel in background")

    entry.async_create_background_task(
        hass,
        _start_tunnel_background(),
        f"woow_tunnel_start_{entry.entry_id}",
    )

    # Forward entry setup to sensor and binary_sensor platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: WoowConfigEntry
) -> bool:
    """Unload a Woow PaaS Smart Home config entry."""
    await entry.runtime_data.tunnel_manager.stop_tunnel()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: WoowConfigEntry
) -> None:
    """Clean up resources when a config entry is removed."""
    # Only clean up binary if no other entries remain for this domain
    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id
    ]
    if not remaining:
        tunnel_manager = CloudflaredManager(hass)
        await tunnel_manager.cleanup_binary()
