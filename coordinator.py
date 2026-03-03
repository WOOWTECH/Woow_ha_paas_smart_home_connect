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
from .const import DOMAIN, UPDATE_INTERVAL, TunnelStatus

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TunnelStatusData:
    """Data from coordinator update."""

    status: TunnelStatus
    subdomain_url: str  # https://{subdomain} or ""

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
        home_id: int | str,
        subdomain: str,
        config_entry: ConfigEntry,
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
        self._home_id = home_id
        self._subdomain = subdomain

    async def _async_update_data(self) -> TunnelStatusData:
        """Fetch tunnel status by merging local process state and remote API."""
        local_running = self._tunnel_manager.is_running

        # Attempt remote API call; use None as fallback on transient failure
        remote_status: str | None = None
        try:
            data = await self._api_client.get_home_status(self._home_id)
            remote_status = data.get("tunnel_status", "").lower()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed while fetching tunnel status"
            ) from err
        except (ApiError, aiohttp.ClientError):
            _LOGGER.warning(
                "Failed to fetch remote status for home %s, using fallback",
                self._home_id,
            )
        except Exception as err:
            raise UpdateFailed(
                f"Unexpected error fetching status for home {self._home_id}"
            ) from err

        # Build subdomain URL
        subdomain_url = (
            f"https://{self._subdomain}" if self._subdomain else ""
        )

        # Merge local + remote status
        status = self._merge_status(local_running, remote_status)

        return TunnelStatusData(
            status=status,
            subdomain_url=subdomain_url,
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
