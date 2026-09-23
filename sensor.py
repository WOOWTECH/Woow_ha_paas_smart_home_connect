"""Sensor entities for Woow PaaS Smart Home tunnel status."""

from __future__ import annotations

from functools import partial

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WoowConfigEntry
from .const import (
    CONF_HOME_NAME,
    CONF_PRODUCT_TYPE,
    DOMAIN,
    MCP_SUBSCRIPTION_VALUE,
    MCP_URL_STATE_AVAILABLE,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
    McpIntegrationState,
    TunnelStatus,
)
from .coordinator import RouteInfo, TunnelCoordinator


def _device_info(entry: WoowConfigEntry) -> DeviceInfo:
    """Build DeviceInfo with a product-aware model label."""
    product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
    model = (
        "PaaS Security Access"
        if product_type == PRODUCT_SECURITY_ACCESS
        else "PaaS Smart Home"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_HOME_NAME, "Woow Smart Home"),
        manufacturer="Woow",
        model=model,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WoowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Woow PaaS Smart Home sensor entities.

    Smart Home: one fixed status sensor + one URL sensor.
    Security Access: one fixed status sensor + one URL sensor PER route, added
    dynamically as routes appear in the 30s coordinator poll (no reload needed).
    """
    coordinator = entry.runtime_data.coordinator
    product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)

    if product_type != PRODUCT_SECURITY_ACCESS:
        async_add_entities([
            TunnelStatusSensor(coordinator, entry),
            TunnelUrlSensor(coordinator, entry),
        ])
        # #270：MCP sensor 依 /status 的 mcp 訂閱字串冪等 reconcile（僅 Smart Home）。
        _setup_mcp_sensors(hass, coordinator, entry, async_add_entities)
        return

    # Security Access: the single aggregate URL sensor (<= 0.2.0) is replaced by
    # one sensor per route. Remove the legacy entity so it does not linger.
    _async_remove_legacy_url_entity(hass, entry)
    async_add_entities([TunnelStatusSensor(coordinator, entry)])

    known_route_ids: set[str] = set()
    sync_routes = partial(
        _async_sync_route_sensors,
        coordinator,
        entry,
        known_route_ids,
        async_add_entities,
    )
    # Re-run on every poll; call once now to add routes already present.
    entry.async_on_unload(coordinator.async_add_listener(sync_routes))
    sync_routes()


@callback
def _async_sync_route_sensors(
    coordinator: TunnelCoordinator,
    entry: WoowConfigEntry,
    known_route_ids: set[str],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a RouteUrlSensor for each newly-appeared Security Access route.

    Add-only: a route removed in PaaS is handled by ``RouteUrlSensor.available``
    returning False (history is preserved, automations keep their entity_id).
    The orphaned entity becomes user-deletable after the next integration reload,
    when this add-only sync no longer recreates it.
    """
    data = coordinator.data
    if data is None:
        return
    new_entities: list[RouteUrlSensor] = []
    for route in data.routes:
        if route.route_id in known_route_ids:
            continue
        known_route_ids.add(route.route_id)
        new_entities.append(
            RouteUrlSensor(
                coordinator,
                entry,
                route.route_id,
                route.subdomain_prefix or route.hostname,
            )
        )
    if new_entities:
        async_add_entities(new_entities)


def _async_remove_legacy_url_entity(
    hass: HomeAssistant, entry: WoowConfigEntry
) -> None:
    """Remove the pre-0.3.0 aggregate Security Access URL entity if it exists.

    Before 0.3.0 a Security Access entry had a single ``{entry_id}_tunnel_url``
    sensor showing only the first route. It is superseded by per-route sensors;
    drop it so users do not see a stale duplicate. Idempotent / no-op when absent.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{entry.entry_id}_tunnel_url"
    )
    if entity_id is not None:
        registry.async_remove(entity_id)


class McpStatusSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """社群 ha_mcp_tools integration 的三態狀態 sensor（見 #270 §5）.

    僅 Smart Home 且該 home 綁定含 MCP 的方案（/status 的 ``mcp == "ha_mcp_tools"``）
    時才存在。顯示值為 coordinator 每 poll 算好的本地三態偵測結果
    （``mcp_integration_state``）——未安裝 / 已安裝未啟動 / 運作中。

    存在與否由 :func:`_setup_mcp_sensors` 依訂閱字串冪等 reconcile；顯示值
    的刷新沿用 CoordinatorEntity：每次 30s coordinator update 自動 write state。
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:robot"
    _attr_has_entity_name = True
    _attr_options = [state.value for state in McpIntegrationState]
    _attr_translation_key = "mcp_status"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the MCP status sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mcp_status"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the detected ha_mcp_tools three-state value (None if no data)."""
        data = self.coordinator.data
        if data is None:
            return None
        return data.mcp_integration_state


class McpConnectUrlSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """MCP client 要填的遠端連線位址：state 放 webhook id，完整 URL 放 connect_url 屬性。

    **為什麼 state 不是完整 URL**（這是量出來的，不是偏好）：HA 裝置頁的實體列
    固定 350px 寬（與視窗大小無關），而完整 URL 從最後一個 ``-`` 到結尾是一整段
    69 字元、520px 的不可斷字串——瀏覽器只在 ``-`` 後面斷行，``hui-generic-entity-row``
    的狀態又沒有 ``overflow-wrap: anywhere``。那一段會撐開整列（需要 584px），把
    名稱欄（``flex: 1 1 30%`` + ellipsis）壓到 24px，名稱只剩第一個字。
    **縮短名稱救不了**：實測名稱改成單一字元，名稱欄仍是 24px——它拿到多少寬度
    只取決於狀態的最小寬度。

    改放 webhook id（36 字元 / 278px）後，名稱欄拿得到 72px，``MCP URL``（60px）
    完整顯示，整列不溢出。完整 URL 走 ``connect_url`` 屬性，點開該列的詳細視窗即可
    看到並複製（那裡是 ``word-break: break-word`` / 594px 寬，長字串正常換行）。

    模板取用請用 ``state_attr('sensor.xxx_mcp_url', 'connect_url')``，不是 ``states()``。

    與 :class:`McpStatusSensor` 成對存在：狀態那顆回答「MCP 有沒有在跑」，這顆回答
    「那要連去哪」。生命週期完全綁在一起（同一個訂閱閘門、同一輪 reconcile），因為
    對使用者而言少了任一顆，另一顆都不完整。

    值為 ``None``（UI 顯示 unknown）而非讓 entity 消失，原因有二：
    tunnel 斷線、ha_mcp_tools 沒跑、local-only 模式這三種「暫時沒有 URL」都會復原，
    entity 消失會連帶丟掉歷史與 dashboard 參照；而且旁邊那顆狀態 sensor 已經說明了
    是哪一種原因，這裡不需要再自己解釋一次。

    ⚠️ state 與 ``connect_url`` 屬性都是**憑證**——webhook id 就是進入 MCP server
    的鑰匙（ha_mcp_tools 預設 ``webhook_auth=none``，密鑰即網址）。兩者都會進 recorder
    歷史、也會出現在任何顯示它們的 dashboard。這是刻意的取捨：使用者就是需要把它複製
    出去貼給 MCP client，跟 ha_mcp_tools 自己在設定畫面顯示 "Remote connect URL"
    是同一個決定。
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:transit-connection-variant"
    _attr_has_entity_name = True
    _attr_options = [MCP_URL_STATE_AVAILABLE]
    _attr_translation_key = "mcp_connect_url"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the MCP connect URL sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mcp_connect_url"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """有可用位址時回 ``available``（顯示為 "Open to copy"），否則 None。

        state 不帶位址本身：裝置頁那一列放不下，放了會把名稱欄壓到只剩一個字。
        位址在 ``connect_url`` 屬性。與屬性同一個判準，兩者不會一個有值一個沒有。
        """
        data = self.coordinator.data
        if data is None or data.mcp_connect_url is None:
            return None
        return MCP_URL_STATE_AVAILABLE

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """完整連線位址與它的組成部分。

        ``connect_url`` 擺第一個：它是使用者點開詳細視窗要找的東西，屬性區依序
        顯示。``base_url`` 附上是為了讓人一眼看出這條路徑掛在哪個 tunnel 底下。
        """
        data = self.coordinator.data
        if data is None or data.mcp_connect_url is None:
            return None
        return {
            "connect_url": data.mcp_connect_url,
            "webhook_id": data.mcp_webhook_id or "",
            "base_url": data.subdomain_url,
        }


class McpConnectUrlFullSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """完整 MCP 連線位址本體：state 就是那串 URL。**預設隱藏。**

    為什麼要獨立一顆、又為什麼預設隱藏——HA 裝置頁的實體列放不下這串 URL：最長的
    不可斷段是 69 字元 / 520px，而整列只有 350px（固定欄寬）。它會撐開整列、把名稱欄
    壓到 24px 只剩一個字（實測；連把名稱改成單一字元都救不了，名稱欄拿到多少寬度只
    取決於狀態的最小寬度）。所以：

    * :class:`McpConnectUrlSensor`（``MCP URL``）**顯示**在診斷區，用短狀態值撐住版面。
    * 這一顆**預設停用**，被收進裝置頁的「未啟用的實體」摺疊區，不佔一列、不會弄亂
      版面。想要完整 URL 成為一級 entity 的人自己啟用它：啟用後
      ``states('sensor.xxx_mcp_connect_url')`` 直接拿到 URL，詳細視窗的主狀態區是
      ``word-break: break-word`` / 594px，長 URL 正常換行、可直接選取複製
      （代價是它在診斷區的那一列會恢復成擠壓的樣子——那是使用者自己選的）。

    用停用而不是隱藏（``entity_registry_visible_default = False``）：實測隱藏的 entity
    在裝置頁**仍然會被列出來**（名稱後面加「已隱藏」），照樣溢出 234px、名稱照樣只剩
    一個字，等於沒解決。停用才真的不佔版面。

    不需要啟用它也拿得到 URL：``MCP URL`` 那顆的 ``connect_url`` 屬性就是同一個值，
    模板用 ``state_attr()``、儀表板用 markdown 卡片都可以。
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:link-variant"
    _attr_has_entity_name = True
    _attr_translation_key = "mcp_connect_url_full"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the full-URL sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mcp_connect_url_full"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the full MCP connect URL, or None when there is not a usable one."""
        data = self.coordinator.data
        if data is None:
            return None
        return data.mcp_connect_url


@callback
def _setup_mcp_sensors(
    hass: HomeAssistant,
    coordinator: TunnelCoordinator,
    entry: WoowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """依 /status 的 mcp 訂閱字串，每 poll 冪等地建立／移除兩顆 MCP sensor.

    兩顆＝McpStatusSensor（狀態）＋ McpConnectUrlSensor（連線位址），同進同出。

    ``mcp == "ha_mcp_tools"``（400 檔）→ 確保存在一顆 sensor。
    ``mcp == ""``（150 檔／退訂／降頻）→ 確保不存在（含先前建過的，從 registry 移除）。

    平台每次同步都廣播當前 mcp 值（無另發「取消」信號），故降頻時 component 會看到
    mcp 由 "ha_mcp_tools" 轉 ""，據此對稱移除 sensor（§5 reconcile 對稱性）。
    """
    present = {"value": False}  # 冪等旗標：避免重複新增／重複移除。

    @callback
    def _sync() -> None:
        data = coordinator.data
        if data is None:
            return
        subscribed = data.mcp == MCP_SUBSCRIPTION_VALUE
        if subscribed and not present["value"]:
            present["value"] = True
            async_add_entities(_mcp_entities(coordinator, entry))
        elif not subscribed and present["value"]:
            present["value"] = False
            _async_remove_mcp_entities(hass, entry)

    # 初次收斂：訂閱中就建；未訂閱則清掉可能殘留的 registry entity（no-op if absent）——
    # 對稱於 _async_remove_legacy_url_entity，保證每次 setup 都收斂到期望狀態。
    data = coordinator.data
    if data is not None and data.mcp == MCP_SUBSCRIPTION_VALUE:
        present["value"] = True
        async_add_entities(_mcp_entities(coordinator, entry))
    else:
        _async_remove_mcp_entities(hass, entry)

    entry.async_on_unload(coordinator.async_add_listener(_sync))


def _mcp_entities(
    coordinator: TunnelCoordinator, entry: WoowConfigEntry
) -> list[SensorEntity]:
    """訂閱成立時要存在的整組 MCP sensor（新增與移除兩邊共用這份清單）."""
    return [
        McpStatusSensor(coordinator, entry),
        McpConnectUrlSensor(coordinator, entry),
        McpConnectUrlFullSensor(coordinator, entry),
    ]


# 移除時要掃的 unique_id 後綴，與 _mcp_entities 的組成一一對應。加新的 MCP sensor
# 記得兩邊都加，否則退訂後會留下孤兒 entity。
_MCP_UNIQUE_ID_SUFFIXES = (
    "_mcp_status",
    "_mcp_connect_url",
    "_mcp_connect_url_full",
)


def _async_remove_mcp_entities(hass: HomeAssistant, entry: WoowConfigEntry) -> None:
    """從 entity registry 移除整組 MCP sensor（冪等，缺席時 no-op）.

    沿用 :func:`_async_remove_legacy_url_entity` 的對稱移除範式：
    ``registry.async_remove`` 會連帶讓 live entity 下線。
    """
    registry = er.async_get(hass)
    for suffix in _MCP_UNIQUE_ID_SUFFIXES:
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, f"{entry.entry_id}{suffix}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)


class TunnelStatusSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """Sensor showing the tunnel connection status text."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:tunnel"
    _attr_has_entity_name = True
    _attr_options = [status.value for status in TunnelStatus]
    _attr_translation_key = "tunnel_status"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tunnel_status"
        self._product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the tunnel status."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.status

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """For security access, expose product type and the access state.

        Smart home entities keep their previous behavior (no extra attributes).
        """
        if self._product_type != PRODUCT_SECURITY_ACCESS:
            return None
        attrs: dict[str, str] = {"product_type": self._product_type}
        data = self.coordinator.data
        if data is not None and data.state is not None:
            attrs["state"] = data.state
        return attrs


class TunnelUrlSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """Sensor showing the tunnel subdomain URL."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:web"
    _attr_has_entity_name = True
    _attr_translation_key = "tunnel_url"

    def __init__(
        self, coordinator: TunnelCoordinator, entry: WoowConfigEntry
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tunnel_url"
        self._product_type = entry.data.get(CONF_PRODUCT_TYPE, PRODUCT_SMART_HOME)
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> str | None:
        """Return the primary tunnel URL.

        For smart home this is https://{subdomain}; for security access it is
        the first route hostname (empty when there are no routes).
        """
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.subdomain_url

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """For security access, expose every route hostname (not just the first)."""
        if self._product_type != PRODUCT_SECURITY_ACCESS:
            return None
        data = self.coordinator.data
        if data is None:
            return None
        return {
            "route_hostnames": list(data.route_hostnames),
            "route_count": len(data.route_hostnames),
        }


class RouteUrlSensor(CoordinatorEntity[TunnelCoordinator], SensorEntity):
    """Sensor showing the URL of a single Security Access route.

    Bound to the route's stable backend id; reports ``unavailable`` (rather than
    being removed) when its route disappears from PaaS, so history and automation
    references survive a transient drop or a route the user can re-add later.

    Editable route fields (service_url, is_protected, ...) update live on the
    same entity every poll, because the route keeps its id. The hostname /
    subdomain_prefix is immutable in PaaS (paas-platform confirmed): "renaming"
    a route is a delete+create that yields a NEW route id, so this entity goes
    unavailable and a fresh entity appears — its history does not carry over.
    See README / sm-api-doc.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:web"
    _attr_has_entity_name = True
    _attr_translation_key = "route_url"

    def __init__(
        self,
        coordinator: TunnelCoordinator,
        entry: WoowConfigEntry,
        route_id: str,
        display_name: str,
    ) -> None:
        """Initialize the per-route URL sensor."""
        super().__init__(coordinator)
        self._route_id = route_id
        self._attr_unique_id = f"{entry.entry_id}_route_{route_id}"
        self._attr_translation_placeholders = {"route": display_name}
        self._attr_device_info = _device_info(entry)

    def _route(self) -> RouteInfo | None:
        """Return this sensor's current route from coordinator data, if present."""
        data = self.coordinator.data
        if data is None:
            return None
        return next(
            (route for route in data.routes if route.route_id == self._route_id),
            None,
        )

    @property
    def available(self) -> bool:
        """Unavailable when the backing route is gone (kept in registry, not removed)."""
        return super().available and self._route() is not None

    @property
    def native_value(self) -> str | None:
        """Return this route's URL (https://{hostname})."""
        route = self._route()
        return f"https://{route.hostname}" if route else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose this route's own metadata."""
        route = self._route()
        if route is None:
            return None
        return {
            "route_id": route.route_id,
            "subdomain_prefix": route.subdomain_prefix,
            "is_protected": route.is_protected,
            "service_url": route.service_url,
        }
