"""API client for woow-paas-platform REST API."""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import Any
from urllib.parse import urlencode

import aiohttp

from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .const import (
    API_PATH_HOME,
    API_PATH_HOME_STATUS,
    API_PATH_HOME_TUNNEL_BINDING,
    API_PATH_HOME_TUNNEL_TOKEN,
    API_PATH_SA_ACCESS,
    API_PATH_SA_ACCESS_STATUS,
    API_PATH_SA_ACCESS_TUNNEL_TOKEN,
    API_PATH_SA_TUNNEL_BINDING,
    API_PATH_SA_WORKSPACE_ACCESSES,
    API_PATH_SA_WORKSPACES,
    API_PATH_WORKSPACE_HOMES,
    API_PATH_WORKSPACES,
    ERROR_ALREADY_BOUND,
    ERROR_INSUFFICIENT_ROLE,
    ERROR_NOT_FOUND,
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


class InsufficientRoleError(AuthenticationError):
    """403 ``insufficient_role``: the token is valid but the user is not an owner
    or admin of the workspace (the platform only hands tunnel tokens to those
    roles). Re-authorizing cannot fix this, so callers must not trigger reauth."""


class AlreadyBoundError(ApiError):
    """平台 409 ``already_bound``：這個家／門禁已經綁在另一台 Home Assistant 上。

    重新授權救不了，也不該退回用舊 token——那正是「兩台同時接同一條 tunnel」的
    成因。呼叫端要停下來，請人在平台上解除綁定或改綁另一個家。``detail`` 帶著
    平台給的對方機器標籤與綁定時間，原文轉給使用者看。"""

    def __init__(self, status: int, message: str, bound_to: str = "",
                 bound_at: str = "") -> None:
        """Keep the platform's description of the instance that holds the binding."""
        super().__init__(status, message)
        self.bound_to = bound_to
        self.bound_at = bound_at


class NotFoundError(ApiError):
    """Raised on 404 Not Found."""


class InstanceGoneError(NotFoundError):
    """平台本身回的 JSON 404 ``{"error": "Not Found"}``：這個 SA／SH 已不存在。

    與一般 404 分開：平台換版時 Odoo 路由還沒載入、Cloudflare 錯誤頁、反向代理
    都可能回 404，但那些不代表實例被刪除，不可據此停隧道。"""


def api_error_for(
    status: int,
    error_code: str,
    message: str,
    body: dict[str, Any] | None = None,
) -> ApiError:
    """Map a non-200 platform response to the exception type callers branch on."""
    if status == HTTPStatus.CONFLICT and error_code == ERROR_ALREADY_BOUND:
        info = body or {}
        return AlreadyBoundError(
            status,
            message,
            bound_to=str(info.get("bound_to") or ""),
            bound_at=str(info.get("bound_at") or ""),
        )
    if status == HTTPStatus.FORBIDDEN and error_code == ERROR_INSUFFICIENT_ROLE:
        return InsufficientRoleError(status, message)
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return AuthenticationError(status, message)
    if status == HTTPStatus.NOT_FOUND:
        if error_code == ERROR_NOT_FOUND:
            return InstanceGoneError(status, message)
        return NotFoundError(status, message)
    return ApiError(status, message)


def _identity_query(instance_uid: str, instance_label: str) -> str:
    """``?instance_uid=…&instance_label=…``，兩個都空時回空字串（舊平台無害）。"""
    params = {k: v for k, v in (
        ("instance_uid", instance_uid), ("instance_label", instance_label)) if v}
    return f"?{urlencode(params)}" if params else ""


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
            InsufficientRoleError: On 403 ``insufficient_role``.
            AuthenticationError: On other 401/403 responses.
            NotFoundError: On 404 responses.
            ApiError: On other non-success HTTP responses.

        """
        url = f"{self._base_url}{path}"
        resp = await self._session.async_request(method, url)

        if resp.status == HTTPStatus.OK:
            return await resp.json()

        # Attempt to extract error detail from response body
        error_code = ""
        parsed: dict[str, Any] | None = None
        try:
            body = await resp.json()
            if isinstance(body, dict):
                parsed = body
                error_code = body.get("error", "")
                detail = body.get("detail", error_code)
            else:
                detail = ""
        except (ValueError, aiohttp.ContentTypeError):
            detail = await resp.text()

        message = f"{resp.status} {detail}" if detail else f"{resp.status}"
        raise api_error_for(resp.status, error_code, message, parsed)

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

    async def get_tunnel_token(
        self,
        home_id: int,
        instance_uid: str = "",
        instance_label: str = "",
    ) -> dict[str, Any]:
        """Fetch a Cloudflare tunnel token for a smart home.

        ``instance_uid`` 讓平台認得「是哪一台 HA 在要這條 tunnel 的權杖」：第一台
        要到的會被記成綁定對象，之後其他 uid 來要同一個家就會收到 409
        ``already_bound``（#31）。同一台重複呼叫是冪等的（重啟、reload 都會再要一
        次）。不帶 uid 的舊版元件平台仍會放行，但那個家會被標成 legacy 綁定。

        Returns:
            Dict with keys: tunnel_token, tunnel_id, subdomain.

        Raises:
            AlreadyBoundError: 這個家已經綁在另一台 HA 上。

        """
        path = API_PATH_HOME_TUNNEL_TOKEN.format(home_id=home_id)
        path += _identity_query(instance_uid, instance_label)
        data = await self._request("GET", path)
        _LOGGER.debug("Fetched tunnel token for home %s", home_id)
        return data

    async def release_tunnel_binding(
        self, home_id: int, instance_uid: str = ""
    ) -> dict[str, Any]:
        """Tell the platform this installation no longer holds the smart home.

        使用者在 HA 移除整合時呼叫（#31 / 平台 #1766）：不送這一槍的話，平台上的
        綁定會一直記著這台機器，下一台要接就得先有人去後台按「解除綁定」——換機器、
        重灌、RMA 全部卡住。

        只有**持有者自己**放得掉（平台比對 `instance_uid`），所以要帶著建立 entry
        時存下來的那一個，不是「現在這台的 uid」——設定被還原到別台機器時兩者會不同。

        Raises:
            ApiError: 平台拒絕或連不上（呼叫端要吞掉，移除整合不能因此失敗）。

        """
        path = API_PATH_HOME_TUNNEL_BINDING.format(home_id=home_id)
        path += _identity_query(instance_uid, "")
        return await self._request("DELETE", path)

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

    async def get_access_tunnel_token(
        self,
        access_id: int,
        instance_uid: str = "",
        instance_label: str = "",
    ) -> dict[str, Any]:
        """Fetch a Cloudflare tunnel token for a security access.

        同 ``get_tunnel_token`` 的綁定語意（#31）——Security Access 這一側的端點是
        鏡像，收緊必須同步，否則攻擊面只是搬家（平台 repo 的
        ``two-layer-acl-lockstep`` pattern）。

        Returns:
            Dict with keys: tunnel_token, tunnel_id (NO subdomain).

        Raises:
            AlreadyBoundError: 這個門禁已經綁在另一台 HA 上。

        """
        path = API_PATH_SA_ACCESS_TUNNEL_TOKEN.format(access_id=access_id)
        path += _identity_query(instance_uid, instance_label)
        data = await self._request("GET", path)
        _LOGGER.debug("Fetched tunnel token for access %s", access_id)
        return data

    async def release_access_tunnel_binding(
        self, access_id: int, instance_uid: str = ""
    ) -> dict[str, Any]:
        """Mirror of ``release_tunnel_binding`` for a security access."""
        path = API_PATH_SA_TUNNEL_BINDING.format(access_id=access_id)
        path += _identity_query(instance_uid, "")
        return await self._request("DELETE", path)

    async def get_access_status(self, access_id: int) -> dict[str, Any]:
        """Fetch updated status for a security access.

        Returns:
            Access dict (full to_dict()) with refreshed state, tunnel_status,
            and routes[].

        """
        path = API_PATH_SA_ACCESS_STATUS.format(access_id=access_id)
        data = await self._request("GET", path)
        return data["access"]
