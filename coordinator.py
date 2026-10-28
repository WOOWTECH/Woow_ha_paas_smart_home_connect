"""DataUpdateCoordinator for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import os
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .access_revocation import AccessRevisionTracker, security_access_policy_revision
from .api_client import (
    ApiError,
    AuthenticationError,
    InstanceGoneError,
    WoowPaasApiClient,
)
from .cloudflared_manager import CloudflaredManager
from .const import (
    ACCESS_RESET_RETRY_INITIAL,
    ACCESS_RESET_RETRY_MAX,
    ACCESS_REVOCATION_DELAY,
    CONF_HOME_NAME,
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

ISSUE_INSTANCE_DELETED = "instance_deleted"


def instance_deleted_issue_id(entry_id: str) -> str:
    """Repairs issue id for "this entry's SA/SH was deleted on the platform"."""
    return f"{ISSUE_INSTANCE_DELETED}_{entry_id}"


def _mcp_server_running(hass: HomeAssistant) -> bool:
    """A loaded tools entry or an unfinished pip install is not a running server."""
    if not any(
        entry.data.get("entry_type") == "server"
        for entry in hass.config_entries.async_loaded_entries(HA_MCP_DOMAIN)
    ):
        return False
    runtime = hass.data.get(HA_MCP_DOMAIN, {})
    manager = runtime.get("manager")
    # HA-MCP stores its forwarding configuration only after async_start has
    # passed its readiness probe. A live thread alone can still be starting.
    return manager is not None and manager.is_running is True and bool(runtime.get("webhook"))


async def async_detect_ha_mcp_state(hass: HomeAssistant) -> McpIntegrationState:
    """Report installation and actual server startup separately."""
    if _mcp_server_running(hass):
        return McpIntegrationState.RUNNING
    if hass.data.get(f"{DOMAIN}_mcp_installing"):
        return McpIntegrationState.INSTALLING
    task = hass.data.get(HA_MCP_DOMAIN, {}).get("bringup_task")
    if task is not None and not task.done():
        return McpIntegrationState.STARTING
    manifest_path = hass.config.path("custom_components", HA_MCP_DOMAIN, "manifest.json")
    installed = await hass.async_add_executor_job(os.path.isfile, manifest_path)
    return (
        McpIntegrationState.INSTALLED_NOT_RUNNING
        if installed else McpIntegrationState.NOT_INSTALLED
    )


def async_get_ha_mcp_webhook_id(hass: HomeAssistant) -> str | None:
    """Return only the running server's registered, remotely accessible webhook.

    HA-MCP 1.2.2 marks its entry loaded before installing/starting its server.
    Check the actual manager and HA webhook registry as well as entry options.
    Existing OAuth settings remain effective; a URL alone does not bypass OAuth.
    """
    if not _mcp_server_running(hass):
        return None
    for entry in hass.config_entries.async_loaded_entries(HA_MCP_DOMAIN):
        if entry.data.get("entry_type") != "server":
            continue
        if entry.options.get(HA_MCP_OPT_ENABLE_WEBHOOK, True) is False:
            continue
        webhook_id = entry.data.get(HA_MCP_DATA_WEBHOOK_ID)
        if not isinstance(webhook_id, str) or not webhook_id:
            continue
        handler = hass.data.get("webhook", {}).get(webhook_id)
        if (
            handler is not None
            and handler.domain == HA_MCP_DOMAIN
            and not handler.local_only
            and "POST" in handler.allowed_methods
        ):
            return webhook_id
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
    # 本輪 /status 回的 tunnel_status 原值（API 失敗為 None）。留著它，service 才能
    # 拿即時的本地行程狀態重跑同一張合併表，不必等下一輪輪詢（見
    # async_update_local_status）。
    remote_status: str | None = None
    # status API 回 404：這個 SA／SH 已在平台上刪除（不是暫時錯誤）。
    instance_deleted: bool = False

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
        # status API 回 404 ⇒ True；隧道重試迴圈與 start_tunnel service 會看它，
        # 不把 cloudflared 重新拉起來連一條已刪除的 tunnel。
        self.instance_deleted = False
        # 第一次收到平台 JSON 404 的時間；任何一次成功的 poll 會歸零。
        self._gone_since: datetime | None = None
        # Smart Home 自動撤權：access_revision 改變 ⇒ 背景 reset_tunnel()。
        # 背景任務掛在 config entry 上，卸載時 HA 也會取消（async_unload_entry
        # 另外先明確取消，確保在 stop_tunnel 之前收尾）。
        self._access = AccessRevisionTracker(
            reset=tunnel_manager.reset_tunnel,
            has_cutoff_target=lambda: tunnel_manager.has_cutoff_target,
            is_deleted=lambda: self.instance_deleted,
            create_task=lambda target, name: (
                config_entry.async_create_background_task(hass, target, name)
            ),
            on_change=self.async_update_listeners,
            delay=ACCESS_REVOCATION_DELAY,
            retry_initial=ACCESS_RESET_RETRY_INITIAL,
            retry_max=ACCESS_RESET_RETRY_MAX,
            label=f"{product_type} {instance_id}",
            now=dt_util.utcnow,
        )

    @property
    def access_revision_attributes(self) -> dict[str, Any] | None:
        """撤權版本狀態（給 tunnel status sensor）；缺完整平台讀回時為 None。"""
        return self._access.attributes

    async def async_cancel_access_reset(self) -> None:
        """取消執行中的撤權 reset（entry 卸載時，在 stop_tunnel 之前呼叫）。"""
        await self._access.async_cancel()

    async def _async_update_data(self) -> TunnelStatusData:
        """Fetch tunnel status by merging local process state and remote API."""
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
        # 撤權版本只採信成功 poll 的值；失敗為 None（只讓已落後的 reset 退避重試）。
        access_revision: object = None
        try:
            if is_sa:
                data = await self._api_client.get_access_status(self._instance_id)
                access_revision = security_access_policy_revision(data.get("routes"))
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
                access_revision = data.get("access_revision")
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed while fetching tunnel status"
            ) from err
        except InstanceGoneError:
            # 只有平台自己的 JSON 404（error="Not Found"）代表「平台上已經沒有這個
            # SA／SH」；其他 404、網路錯誤、5xx 落到下一個分支，照舊沿用 last-known 值。
            # 而且要連續兩輪（相隔至少一個輪詢間隔）都是它才算數：單次 404 可能是
            # 平台換版途中的誤回，誤判的代價是把使用者的隧道停掉。
            now = dt_util.utcnow()
            if self._gone_since is None:
                self._gone_since = now
            if self.instance_deleted or now - self._gone_since >= timedelta(
                seconds=UPDATE_INTERVAL
            ):
                return await self._async_mark_instance_deleted()
            _LOGGER.debug(
                "Platform reports %s %s not found; marking it deleted if the "
                "next poll agrees (keeping last-known values)",
                self._product_type,
                self._instance_id,
            )
        except (ApiError, aiohttp.ClientError):
            if self.instance_deleted and previous is not None:
                # 已判定刪除後的暫時錯誤不代表恢復，維持 deleted。
                return previous
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
        else:
            # 只有成功的 poll 才歸零／解除 deleted；暫時錯誤不算「正常回應」。
            self._gone_since = None
            if self.instance_deleted:
                self._async_clear_instance_deleted()

        # 缺欄位／型別不對（舊平台）由 tracker 忽略。SA 只採信完整政策清單。
        # 已判定刪除的路徑在上面就 return 了，不會走到這裡。
        self._access.async_poll(access_revision)

        # Build the display URL. Smart home uses its stored subdomain; security
        # access uses its first route hostname (empty when active_no_route / 0 routes).
        if is_sa:
            subdomain_url = f"https://{routes[0].hostname}" if routes else ""
        else:
            subdomain_url = f"https://{self._subdomain}" if self._subdomain else ""

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
                mcp_integration_state = None
                mcp_webhook_id = None
                _LOGGER.debug(
                    "MCP 狀態偵測失敗，本輪不發布連線網址", exc_info=True
                )
        elif mcp != MCP_SUBSCRIPTION_VALUE:
            # 無訂閱（含 SA、降頻退訂）→ 三態與 connect URL 都無意義。
            mcp_integration_state = None
            mcp_webhook_id = None

        # Read local state after all awaited work, so an in-flight poll cannot
        # overwrite an exit/spawn notification with an older process snapshot.
        status = self._merge_status(self._tunnel_manager.is_running, remote_status)

        return TunnelStatusData(
            status=status,
            subdomain_url=subdomain_url,
            state=state,
            routes=routes,
            mcp=mcp,
            mcp_integration_state=mcp_integration_state,
            mcp_webhook_id=mcp_webhook_id,
            remote_status=remote_status,
        )

    async def _async_mark_instance_deleted(self) -> TunnelStatusData:
        """status API 回 404：停掉 cloudflared、開 repairs issue、清掉所有舊值。

        舊版把 404 當暫時錯誤：路由 sensor 繼續顯示舊網址、cloudflared 繼續連一條
        已刪除的 tunnel、log 每 30 秒一條 warning，使用者卻沒有任何提示。
        """
        entry = self.config_entry
        if not self.instance_deleted:
            self.instance_deleted = True
            _LOGGER.warning(
                "%s %s no longer exists on the Woow PaaS platform (status 404); "
                "stopping its tunnel. Remove this integration entry",
                self._product_type,
                self._instance_id,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                instance_deleted_issue_id(entry.entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_INSTANCE_DELETED,
                translation_placeholders={
                    "name": str(entry.data.get(CONF_HOME_NAME) or entry.title)
                },
            )
        if self._tunnel_manager.is_running:
            await self._tunnel_manager.stop_tunnel()
        # 路由清空 ⇒ 路由 sensor 走既有的「路由消失 → unavailable」；mcp 清空 ⇒
        # MCP sensor 走既有的退訂移除；subdomain_url 清空 ⇒ 不再顯示舊網址。
        return TunnelStatusData(
            status=TunnelStatus.DELETED,
            subdomain_url="",
            remote_status=TunnelStatus.DELETED,
            instance_deleted=True,
        )

    @callback
    def _async_clear_instance_deleted(self) -> None:
        """404 之後 status 又正常（誤判或平台復原）：清 issue，重載 entry。

        重載會重新取 tunnel token 並走一般的隧道啟動流程，cloudflared 因此自動
        回來；不必在這裡另寫一條啟動路徑。
        """
        self.instance_deleted = False
        _LOGGER.info(
            "%s %s is reachable on the platform again; reloading the entry",
            self._product_type,
            self._instance_id,
        )
        ir.async_delete_issue(
            self.hass, DOMAIN, instance_deleted_issue_id(self.config_entry.entry_id)
        )
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    @callback
    def async_update_local_status(self) -> None:
        """用即時的本地行程狀態 × 上一輪的遠端狀態重算 status（不打 API）。

        start_tunnel／stop_tunnel 後呼叫，讓 sensor 與 get_status 立刻反映，而不是
        最多舊 30 秒（tunnel_running=false 卻還顯示 connected）。
        """
        data = self.data
        if data is None:
            return
        status = self._merge_status(self._tunnel_manager.is_running, data.remote_status)
        if status != data.status:
            self.async_set_updated_data(replace(data, status=status))

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
