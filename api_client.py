"""API client for woow-paas-platform REST API."""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import Any

import aiohttp

from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import (
    API_PATH_HOME,
    API_PATH_HOME_STATUS,
    API_PATH_HOME_TUNNEL_TOKEN,
    API_PATH_SA_ACCESS,
    API_PATH_SA_ACCESS_STATUS,
    API_PATH_SA_ACCESS_TUNNEL_TOKEN,
    API_PATH_SA_WORKSPACE_ACCESSES,
    API_PATH_SA_WORKSPACES,
    API_PATH_WORKSPACE_HOMES,
    API_PATH_WORKSPACES,
)

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """Base exception for woow-paas API errors."""

    def __init__(self, status: int, message: str) -> None:
        """Initialize with HTTP status and error message."""
        super().__init__(message)
        self.status = status


class AuthenticationError(ApiError):
    """Raised on 401 Unauthorized or 403 Forbidden."""


class NotFoundError(ApiError):
    """Raised on 404 Not Found."""


class WoowPaasApiClient:
    """API client for woow-paas-platform.

    Uses an OAuth2Session for automatic Bearer Token management and refresh.
    """

    def __init__(self, session: OAuth2Session, base_url: str) -> None:
        """Initialize with OAuth2Session and configurable base URL."""
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def _request(self, method: str, path: str) -> Any:
        """Make an authenticated API request and return parsed JSON.

        Raises:
            AuthenticationError: On 401/403 responses.
            NotFoundError: On 404 responses.
            ApiError: On other non-success HTTP responses.

        """
        url = f"{self._base_url}{path}"
        resp = await self._session.async_request(method, url)

        if resp.status == HTTPStatus.OK:
            return await resp.json()

        # Attempt to extract error detail from response body
        try:
            body = await resp.json()
            detail = (
                body.get("detail", body.get("error", ""))
                if isinstance(body, dict)
                else ""
            )
        except (ValueError, aiohttp.ContentTypeError):
            detail = await resp.text()

        message = f"{resp.status} {detail}" if detail else f"{resp.status}"

        if resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AuthenticationError(resp.status, message)
        if resp.status == HTTPStatus.NOT_FOUND:
            raise NotFoundError(resp.status, message)

        raise ApiError(resp.status, message)

    async def get_workspaces(self) -> list[dict[str, Any]]:
        """Fetch all workspaces for the authenticated user.

        Returns:
            List of workspace dicts with keys: id, name, slug.

        """
        data = await self._request("GET", API_PATH_WORKSPACES)
        return data["workspaces"]

    async def get_homes(self, workspace_id: int) -> list[dict[str, Any]]:
        """Fetch all smart homes in a workspace.

        Returns:
            List of home dicts with keys: id, name, state, subdomain, tunnel_status.

        """
        path = API_PATH_WORKSPACE_HOMES.format(workspace_id=workspace_id)
        data = await self._request("GET", path)
        return data["homes"]

    async def get_home(self, home_id: int) -> dict[str, Any]:
        """Fetch full details for a single smart home.

        Returns:
            Home dict with full to_dict() output from the backend.

        """
        path = API_PATH_HOME.format(home_id=home_id)
        data = await self._request("GET", path)
        return data["home"]

    async def get_tunnel_token(self, home_id: int) -> dict[str, Any]:
        """Fetch a Cloudflare tunnel token for a smart home.

        Returns:
            Dict with keys: tunnel_token, tunnel_id, subdomain.

        """
        path = API_PATH_HOME_TUNNEL_TOKEN.format(home_id=home_id)
        data = await self._request("GET", path)
        _LOGGER.debug("Fetched tunnel token for home %s", home_id)
        return data

    async def get_home_status(self, home_id: int) -> dict[str, Any]:
        """Fetch updated status for a smart home.

        Returns:
            Home dict with updated status fields.

        """
        path = API_PATH_HOME_STATUS.format(home_id=home_id)
        data = await self._request("GET", path)
        return data["home"]

    # ------------------------------------------------------------------
    # Security Access
    #
    # Mirrors the smart home endpoints under the /api/security-access/ prefix.
    # Key differences (see sm-api-doc):
    #   - the instance is an "access" (id = access id) instead of a "home"
    #   - the tunnel-token response has NO subdomain; hostnames live on the
    #     access detail routes[].hostname instead
    #   - the access has both a tunnel_status and a higher-level `state`
    #     (pending/provisioning/active/active_no_route/error/suspended/deleting)
    # ------------------------------------------------------------------

    async def get_sa_workspaces(self) -> list[dict[str, Any]]:
        """Fetch all workspaces for the authenticated user (Security Access view).

        Product-agnostic: returns the same set as get_workspaces(). Provided for
        symmetry; the config flow uses get_workspaces() for the shared step.

        Returns:
            List of workspace dicts with keys: id, name, slug.

        """
        data = await self._request("GET", API_PATH_SA_WORKSPACES)
        return data["workspaces"]

    async def get_accesses(self, workspace_id: int) -> list[dict[str, Any]]:
        """Fetch all security accesses in a workspace.

        Returns:
            List of access dicts with keys: id, name, canonical_name, state,
            tunnel_status, plan_name, next_payment.

        """
        path = API_PATH_SA_WORKSPACE_ACCESSES.format(workspace_id=workspace_id)
        data = await self._request("GET", path)
        return data["accesses"]

    async def get_access(self, access_id: int) -> dict[str, Any]:
        """Fetch full details for a single security access.

        Returns:
            Access dict with full to_dict() output (includes routes[]).

        """
        path = API_PATH_SA_ACCESS.format(access_id=access_id)
        data = await self._request("GET", path)
        return data["access"]

    async def get_access_tunnel_token(self, access_id: int) -> dict[str, Any]:
        """Fetch a Cloudflare tunnel token for a security access.

        Returns:
            Dict with keys: tunnel_token, tunnel_id (NO subdomain).

        """
        path = API_PATH_SA_ACCESS_TUNNEL_TOKEN.format(access_id=access_id)
        data = await self._request("GET", path)
        _LOGGER.debug("Fetched tunnel token for access %s", access_id)
        return data

    async def get_access_status(self, access_id: int) -> dict[str, Any]:
        """Fetch updated status for a security access.

        Returns:
            Access dict (full to_dict()) with refreshed state, tunnel_status,
            and routes[].

        """
        path = API_PATH_SA_ACCESS_STATUS.format(access_id=access_id)
        data = await self._request("GET", path)
        return data["access"]
