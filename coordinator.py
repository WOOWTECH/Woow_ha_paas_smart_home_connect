"""DataUpdateCoordinator for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import os

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import ApiError, AuthenticationError, WoowPaasApiClient
from .cloudflared_manager import CloudflaredManager
from .const import (
    DOMAIN,
    HA_MCP_DATA_WEBHOOK_ID,
    HA_MCP_DOMAIN,
    HA_MCP_OPT_ENABLE_WEBHOOK,
    HA_WEBHOOK_PATH,
    MCP_SUBSCRIPTION_VALUE,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    UPDATE_INTERVAL,
    McpIntegrationState,
    TunnelStatus,
)

_LOGGER = logging.getLogger(__name__)


async def async_detect_ha_mcp_state(hass: HomeAssistant) -> McpIntegrationState:
    """偵測社群 ha_mcp_tools integration 的三態（見 #270 §5）.

    回傳 McpIntegrationState 三值之一。可安全於 coordinator 的 async context
    直接 await：只讀 config entries（純記憶體）＋ 一次 executor 的 os.path.isfile，
    不阻塞 event loop、也不 import ha_mcp_tools 的任何 Python 模組。

    偵測順序（此順序刻意，見 §5 對抗式驗證 skeptic #1/#2 修正）：
    1. 有任一「未停用且 LOADED」的 config entry → RUNNING。async_loaded_entries
       已結構性排除 ignored/disabled、且只回 state==LOADED 者。
    2. 否則以「manifest.json 是否在磁碟上」區分 INSTALLED_NOT_RUNNING 與
       NOT_INSTALLED。刻意不用 async_get_integration/IntegrationNotFound——後者
       會把「manifest 在磁碟但被 version/blocked/corrupt/陳舊快取拒絕」誤判成未安裝。
       stale entry（HACS 反安裝只刪檔留 entry）因不在 async_loaded_entries 內，
       會回落到此磁碟探測，得到正解 NOT_INSTALLED。
    """
    if hass.config_entries.async_loaded_entries(HA_MCP_DOMAIN):
        return McpIntegrationState.RUNNING

    manifest_path = hass.config.path(
        "custom_components", HA_MCP_DOMAIN, "manifest.json"
    )
    installed = await hass.async_add_executor_job(os.path.isfile, manifest_path)
    return (
        McpIntegrationState.INSTALLED_NOT_RUNNING
        if installed
        else McpIntegrationState.NOT_INSTALLED
    )


def async_get_ha_mcp_webhook_id(hass: HomeAssistant) -> str | None:
    """取回 ha_mcp_tools 目前生效、且真的對外可達的 webhook id（否則 None）。

    只有在「id 拿得到」**且**「該 id 對應的端點確實註冊了」兩件事同時成立時才回值——
    回一個會 404 的 URL 比不回更糟，使用者會拿去貼進 MCP client 然後在別處查錯。
    三道門，缺一不可：

    1. 有 LOADED 的 ha_mcp_tools entry。用 ``async_loaded_entries`` 而非
       ``async_entries``：entry 存在不等於 webhook 註冊了（disabled／setup 失敗／
       HACS 反安裝只刪檔留 entry 都會留下 stale entry），而 webhook 是在
       ``async_setup_entry`` 裡註冊的。這與 :func:`async_detect_ha_mcp_state`
       判定 RUNNING 用的是同一個判準，兩顆 sensor 因此不會互相矛盾。
    2. ``options["enable_webhook"]`` 不是 False（預設 True）。False＝local-only 模式，
       ha_mcp_tools 明確不註冊 webhook（見 const 註解）。
    3. ``data["webhook_id"]`` 有值。

    純記憶體讀取（config entries），無 I/O，可在 event loop 直接呼叫。
    多個 entry 時取第一個 loaded 的——ha_mcp_tools 是單例型 integration，
    實務上不會有第二個；真有的話第一個就是 UI 顯示的那個。
    """
    for entry in hass.config_entries.async_loaded_entries(HA_MCP_DOMAIN):
        if entry.options.get(HA_MCP_OPT_ENABLE_WEBHOOK, True) is False:
            continue
        webhook_id = entry.data.get(HA_MCP_DATA_WEBHOOK_ID)
        if webhook_id:
            return str(webhook_id)
    return None


@dataclass(frozen=True)
class RouteInfo:
    """A single Security Access application route.

    Keyed on ``route_id`` (the stable backend id from the API contract), so an
    entity bound to it survives hostname/subdomain edits in the PaaS UI.
    """

    route_id: str
    hostname: str
    subdomain_prefix: str = ""
    is_protected: bool = False
    service_url: str = ""


@dataclass(frozen=True)
class TunnelStatusData:
    """Data from coordinator update."""

    status: TunnelStatus
    subdomain_url: str  # https://{subdomain} (SH) or https://{first route hostname} (SA), or ""
    # Security-access-only fields (None/empty for smart home).
    state: str | None = None  # SA access state, e.g. active / active_no_route
    routes: tuple[RouteInfo, ...] = ()  # all SA routes (empty for smart home)
    # --- MCP（Smart Home 專用；SA 恆為預設值，見 #270 §5）---
    mcp: str = ""  # /status 的訂閱字串："ha_mcp_tools" 或 ""
    # 三態偵測結果，僅在 mcp == "ha_mcp_tools"（有訂閱）時有值，否則 None。
    mcp_integration_state: McpIntegrationState | None = None
    # ha_mcp_tools 生效中的 webhook id；無訂閱／未 RUNNING／local-only 模式時為 None。
    mcp_webhook_id: str | None = None

    @property
    def mcp_connect_url(self) -> str | None:
        """MCP client 要填的遠端連線位址，None 代表現在沒有可用的。

        刻意用 tunnel 的 ``subdomain_url`` 當 base，而不是 ha_mcp_tools 自己算的
        external_url 或 HA 的 ``get_url()``：這顆 sensor 的意義是「透過**這個
        PaaS Smart Home 訂閱**買到的那條對外路徑連 MCP」。tunnel 沒起來時
        subdomain_url 為空字串 ⇒ 這裡回 None，而不是吐一個 ``/api/webhook/...``
        的相對路徑出去。
        """
        if not self.subdomain_url or not self.mcp_webhook_id:
            return None
        return f"{self.subdomain_url}{HA_WEBHOOK_PATH}{self.mcp_webhook_id}"

    @property
    def is_connected(self) -> bool:
        """Derive connectivity from status."""
        return self.status in (TunnelStatus.CONNECTED, TunnelStatus.UNKNOWN)

    @property
    def route_hostnames(self) -> tuple[str, ...]:
        """All SA route hostnames (derived; kept for backward compatibility)."""
        return tuple(route.hostname for route in self.routes)


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

        # Attempt remote API call; use None as fallback on transient failure.
        # Preserve the last-known SA state/routes so a transient API failure does
        # not look like "the access lost all its routes" (which would flap every
        # per-route entity to unavailable). Only a SUCCESSFUL poll overwrites them.
        previous = self.data
        remote_status: str | None = None
        state: str | None = previous.state if previous else None
        routes: tuple[RouteInfo, ...] = previous.routes if previous else ()
        # #270：mcp 訂閱字串 / 三態偵測結果同樣沿用 last-known，僅成功 poll 才覆寫，
        # 避免 API 暫失被誤當退訂（會移除 sensor → 下輪恢復 → 存在性 flapping）。
        mcp: str = previous.mcp if previous else ""
        mcp_integration_state: McpIntegrationState | None = (
            previous.mcp_integration_state if previous else None
        )
        mcp_webhook_id: str | None = previous.mcp_webhook_id if previous else None
        try:
            if is_sa:
                data = await self._api_client.get_access_status(self._instance_id)
                remote_status = (data.get("tunnel_status") or "").lower()
                state = data.get("state")
                routes = tuple(
                    RouteInfo(
                        route_id=str(route["id"]),
                        hostname=route["hostname"],
                        subdomain_prefix=route.get("subdomain_prefix", ""),
                        is_protected=bool(route.get("is_protected", False)),
                        service_url=route.get("service_url", ""),
                    )
                    for route in (data.get("routes") or [])
                    if route.get("hostname") and route.get("id") is not None
                )
            else:
                data = await self._api_client.get_home_status(self._instance_id)
                remote_status = (data.get("tunnel_status") or "").lower()
                # #270：僅 SH payload 帶 mcp（SA 的 to_dict 無此 key）；成功才覆寫。
                mcp = data.get("mcp") or ""
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed while fetching tunnel status"
            ) from err
        except (ApiError, aiohttp.ClientError):
            _LOGGER.warning(
                "Failed to fetch remote status for %s %s, using fallback "
                "(keeping last-known routes)",
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
            subdomain_url = f"https://{routes[0].hostname}" if routes else ""
        else:
            subdomain_url = f"https://{self._subdomain}" if self._subdomain else ""

        # Merge local + remote status
        status = self._merge_status(local_running, remote_status)

        # #270：三態偵測。僅 Smart Home 且有 MCP 訂閱時才跑（避開無訂閱 home 的
        # executor 呼叫）。偵測與雲端 API 無關，故即使本輪 API 失敗（mcp 保留上一輪值）
        # 仍會重算當前本地實況。全程包 try/except：偵測自身的意外例外不可讓整個
        # update 失敗（否則 last_update_success=False → 所有 sensor 同時 unavailable）。
        if not is_sa and mcp == MCP_SUBSCRIPTION_VALUE:
            try:
                mcp_integration_state = await async_detect_ha_mcp_state(self.hass)
                # connect URL 與三態同源、同一輪算，兩顆 sensor 才不會有一輪的偏差
                # （例如狀態已 RUNNING 但 URL 還是上一輪的 None）。
                mcp_webhook_id = async_get_ha_mcp_webhook_id(self.hass)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "ha_mcp_tools 三態偵測失敗，沿用上一輪狀態", exc_info=True
                )
        elif mcp != MCP_SUBSCRIPTION_VALUE:
            # 無訂閱（含 SA、降頻退訂）→ 三態與 connect URL 都無意義。
            mcp_integration_state = None
            mcp_webhook_id = None

        return TunnelStatusData(
            status=status,
            subdomain_url=subdomain_url,
            state=state,
            routes=routes,
            mcp=mcp,
            mcp_integration_state=mcp_integration_state,
            mcp_webhook_id=mcp_webhook_id,
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
        | any      | deleted      | deleted        |
        """
        # API failure (remote_status is None)
        if remote_status is None:
            return TunnelStatus.UNKNOWN if local_running else TunnelStatus.DISCONNECTED

        # 遠端說 tunnel 已被刪除 ⇒ 優先於本地行程狀態（平台 #833）。tunnel 在
        # Cloudflare 上已不存在，本地 cloudflared 就算還活著也連不到任何東西，那個
        # running 不是健康訊號、是該被清掉的殘留。若讓它落進下面的既有分支，會得到
        # running -> error、stopped -> disconnected —— 兩者都與「tunnel 還在、只是
        # 沒連上」撞成同一個值，使用者無從分辨「非重建不可」與「等連線恢復就好」。
        if remote_status == TunnelStatus.DELETED:
            return TunnelStatus.DELETED

        # Both sources available
        if local_running and remote_status == TunnelStatus.CONNECTED:
            return TunnelStatus.CONNECTED

        if local_running or remote_status == TunnelStatus.CONNECTED:
            return TunnelStatus.ERROR

        # not local_running and remote != connected
        return TunnelStatus.DISCONNECTED
