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
from homeassistant.helpers import config_entry_oauth2_flow, config_validation as cv
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
    CONF_PRODUCT_TYPE,
    CONF_SUBDOMAIN,
    CONF_TUNNEL_ID,
    CONF_TUNNEL_TOKEN,
    CONF_WORKSPACE_ID,
    DOMAIN,
    PLATFORMS,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    SERVICE_GET_STATUS,
    SERVICE_START_TUNNEL,
    SERVICE_STOP_TUNNEL,
)
from .coordinator import TunnelCoordinator
from .oauth2 import create_implementation
from .oauth_callback_view import async_register_woow_callback_view

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("home_id"): vol.Coerce(str),
        vol.Optional("product_type"): vol.In(
            [PRODUCT_SMART_HOME, PRODUCT_SECURITY_ACCESS]
        ),
    }
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
    hass: HomeAssistant, home_id: str, product_type: str | None = None
) -> tuple[WoowConfigEntry, WoowRuntimeData]:
    """Find the loaded config entry and runtime data for a given instance id.

    ``home_id`` is the instance id (smart home id or security access id); it is
    stored under CONF_HOME_ID for both products. A smart home and a security
    access can carry the same numeric id (independent backend id sequences), so
    ``product_type`` disambiguates. Without it, an ambiguous match raises rather
    than silently operating on the wrong product (fail loudly).

    Raises:
        HomeAssistantError: if no loaded entry matches, or the match is ambiguous.

    """
    matches: list[tuple[WoowConfigEntry, WoowRuntimeData]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(CONF_HOME_ID)) != str(home_id):
            continue
        entry_product = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
        if product_type is not None and entry_product != product_type:
            continue
        runtime_data = getattr(entry, "runtime_data", None)
        if runtime_data is not None:
            matches.append((entry, runtime_data))

    if not matches:
        suffix = f", product_type={product_type}" if product_type else ""
        raise HomeAssistantError(
            f"No loaded config entry found for home_id={home_id}{suffix}"
        )
    if len(matches) > 1:
        products = ", ".join(
            entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
            for entry, _ in matches
        )
        raise HomeAssistantError(
            f"Ambiguous home_id={home_id}: matches multiple products "
            f"({products}). Pass product_type to disambiguate."
        )
    return matches[0]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Woow PaaS Smart Home component."""
    config_entry_oauth2_flow.async_register_implementation(
        hass, DOMAIN, create_implementation(hass)
    )
    async_register_woow_callback_view(hass)

    async def _handle_start_tunnel(call: ServiceCall) -> None:
        """Handle start_tunnel service call."""
        home_id = call.data["home_id"]
        entry, runtime_data = _find_entry_and_data(
            hass, home_id, call.data.get("product_type")
        )

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
        _, runtime_data = _find_entry_and_data(
            hass, home_id, call.data.get("product_type")
        )

        if not await runtime_data.tunnel_manager.stop_tunnel():
            raise HomeAssistantError("Failed to stop cloudflared tunnel")

    async def _handle_get_status(call: ServiceCall) -> ServiceResponse:
        """Handle get_status service call."""
        home_id = call.data["home_id"]
        entry, runtime_data = _find_entry_and_data(
            hass, home_id, call.data.get("product_type")
        )

        coord_data = runtime_data.coordinator.data
        return {
            "product_type": entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME),
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

    product_type: str = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
    is_sa = product_type == PRODUCT_SECURITY_ACCESS
    instance_id: int = entry.data[CONF_HOME_ID]

    # Re-fetch tunnel token on startup to ensure freshness
    try:
        if is_sa:
            token_data = await api_client.get_access_tunnel_token(instance_id)
            new_data = {
                **entry.data,
                CONF_TUNNEL_TOKEN: token_data["tunnel_token"],
                CONF_TUNNEL_ID: token_data["tunnel_id"],
            }
        else:
            token_data = await api_client.get_tunnel_token(instance_id)
            new_data = {
                **entry.data,
                CONF_TUNNEL_TOKEN: token_data["tunnel_token"],
                CONF_TUNNEL_ID: token_data["tunnel_id"],
                CONF_SUBDOMAIN: token_data["subdomain"],
            }
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.debug("Refreshed tunnel token for %s %s", product_type, instance_id)
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
        instance_id=instance_id,
        subdomain=entry.data.get(CONF_SUBDOMAIN, ""),
        config_entry=entry,
        product_type=product_type,
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
                    "No tunnel token available for %s %s; tunnel will not start",
                    product_type,
                    instance_id,
                )
                return
            if not await tunnel_manager.start_tunnel(tunnel_token):
                _LOGGER.error(
                    "Failed to start cloudflared tunnel for %s %s in background",
                    product_type,
                    instance_id,
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

    # Forward entry setup to sensor platform
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
