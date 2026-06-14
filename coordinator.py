"""DataUpdateCoordinator for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import ApiError, AuthenticationError, WoowPaasApiClient
from .cloudflared_manager import CloudflaredManager
from .const import (
    DOMAIN,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    UPDATE_INTERVAL,
    TunnelStatus,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TunnelStatusData:
    """Data from coordinator update."""

    status: TunnelStatus
    subdomain_url: str  # https://{subdomain} (SH) or https://{route hostname} (SA), or ""
    # Security-access-only fields (None/empty for smart home).
    state: str | None = None  # SA access state, e.g. active / active_no_route
    route_hostnames: tuple[str, ...] = ()  # all SA route hostnames

    @property
    def is_connected(self) -> bool:
        """Derive connectivity from status."""
        return self.status in (TunnelStatus.CONNECTED, TunnelStatus.UNKNOWN)


class TunnelCoordinator(DataUpdateCoordinator[TunnelStatusData]):
    """Coordinator to poll tunnel status from local process and remote API."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        api_client: WoowPaasApiClient,
        tunnel_manager: CloudflaredManager,
        instance_id: int | str,
        subdomain: str,
        config_entry: ConfigEntry,
        product_type: str = PRODUCT_SMART_HOME,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tunnel_status",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            config_entry=config_entry,
        )
        self._api_client = api_client
        self._tunnel_manager = tunnel_manager
        self._instance_id = instance_id
        self._subdomain = subdomain
        self._product_type = product_type

    async def _async_update_data(self) -> TunnelStatusData:
        """Fetch tunnel status by merging local process state and remote API."""
        local_running = self._tunnel_manager.is_running
        is_sa = self._product_type == PRODUCT_SECURITY_ACCESS

        # Attempt remote API call; use None as fallback on transient failure
        remote_status: str | None = None
        state: str | None = None
        route_hostnames: tuple[str, ...] = ()
        try:
            if is_sa:
                data = await self._api_client.get_access_status(self._instance_id)
                remote_status = (data.get("tunnel_status") or "").lower()
                state = data.get("state")
                route_hostnames = tuple(
                    route["hostname"]
                    for route in (data.get("routes") or [])
                    if route.get("hostname")
                )
            else:
                data = await self._api_client.get_home_status(self._instance_id)
                remote_status = (data.get("tunnel_status") or "").lower()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed while fetching tunnel status"
            ) from err
        except (ApiError, aiohttp.ClientError):
            _LOGGER.warning(
                "Failed to fetch remote status for %s %s, using fallback",
                self._product_type,
                self._instance_id,
            )
        except Exception as err:
            raise UpdateFailed(
                f"Unexpected error fetching status for "
                f"{self._product_type} {self._instance_id}"
            ) from err

        # Build the display URL. Smart home uses its stored subdomain; security
        # access uses its first route hostname (empty when active_no_route / 0 routes).
        if is_sa:
            subdomain_url = (
                f"https://{route_hostnames[0]}" if route_hostnames else ""
            )
        else:
            subdomain_url = f"https://{self._subdomain}" if self._subdomain else ""

        # Merge local + remote status
        status = self._merge_status(local_running, remote_status)

        return TunnelStatusData(
            status=status,
            subdomain_url=subdomain_url,
            state=state,
            route_hostnames=route_hostnames,
        )

    @staticmethod
    def _merge_status(
        local_running: bool, remote_status: str | None
    ) -> TunnelStatus:
        """Merge local process state and remote API status into a single status.

        is_connected is derived from status via TunnelStatusData.is_connected.

        | local    | remote       | -> status      |
        |----------|--------------|----------------|
        | running  | connected    | connected      |
        | running  | disconnected | error          |
        | stopped  | connected    | error          |
        | stopped  | disconnected | disconnected   |
        | running  | API fail     | unknown        |
        | stopped  | API fail     | disconnected   |
        """
        # API failure (remote_status is None)
        if remote_status is None:
            return TunnelStatus.UNKNOWN if local_running else TunnelStatus.DISCONNECTED

        # Both sources available
        if local_running and remote_status == TunnelStatus.CONNECTED:
            return TunnelStatus.CONNECTED

        if local_running or remote_status == TunnelStatus.CONNECTED:
            return TunnelStatus.ERROR

        # not local_running and remote != connected
        return TunnelStatus.DISCONNECTED
