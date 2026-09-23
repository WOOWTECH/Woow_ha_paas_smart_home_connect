"""Constants for the Woow PaaS Smart Home integration."""

from enum import StrEnum

from homeassistant.const import Platform


class TunnelStatus(StrEnum):
    """Tunnel connection status values."""

    CONNECTED = "connected"
    # tunnel 已在 Cloudflare 上被刪除（平台 #833 的第四格）。字面值必須與平台
    # tunnel_status Selection 一致；語意與 DISCONNECTED 相反地「非重建不可」。
    DELETED = "deleted"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    UNKNOWN = "unknown"


class McpIntegrationState(StrEnum):
    """社群 ha_mcp_tools integration 的三態偵測值（見 #270 §5）.

    純 HA 本地偵測結果，作為 MCP sensor 的 state 機器值。
    """

    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_RUNNING = "installed_not_running"
    RUNNING = "running"


DOMAIN = "woow_paas_smart_home"

# --- API ---
API_BASE_URL = "https://stg.woowtech.io"

# API endpoint paths (Smart Home)
API_PATH_WORKSPACES = "/api/smarthome/workspaces"
API_PATH_WORKSPACE_HOMES = "/api/smarthome/workspaces/{workspace_id}/homes"
API_PATH_HOME = "/api/smarthome/homes/{home_id}"
API_PATH_HOME_TUNNEL_TOKEN = "/api/smarthome/homes/{home_id}/tunnel-token"
API_PATH_HOME_STATUS = "/api/smarthome/homes/{home_id}/status"

# API endpoint paths (Security Access)
# Note: the workspace list is product-agnostic (paas-platform serves both product
# workspace endpoints from the same _get_user_workspaces helper), so the config
# flow reuses API_PATH_WORKSPACES for the shared workspace step.
API_PATH_SA_WORKSPACES = "/api/security-access/workspaces"
API_PATH_SA_WORKSPACE_ACCESSES = (
    "/api/security-access/workspaces/{workspace_id}/accesses"
)
API_PATH_SA_ACCESS = "/api/security-access/accesses/{access_id}"
API_PATH_SA_ACCESS_TUNNEL_TOKEN = (
    "/api/security-access/accesses/{access_id}/tunnel-token"
)
API_PATH_SA_ACCESS_STATUS = "/api/security-access/accesses/{access_id}/status"

# OAuth2 endpoints
OAUTH2_AUTHORIZE = "/oauth2/authorize"
OAUTH2_TOKEN = "/oauth2/token"
# OAuth2 Device Authorization Grant (RFC 8628) — used for HA Companion App
# onboarding, where the redirect/window.open web flow can't complete in-app
# (HA-wide limitation; see design doc §11). Browser onboarding keeps the web flow.
OAUTH2_DEVICE_AUTHORIZATION = "/oauth2/device_authorization"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# OAuth2 public client (PKCE-only, no client_secret).
# This client_id is registered as a public client on paas-platform and may be
# committed to a public repo. See sm-api-doc §3 / §4 for migration spec.
OAUTH2_CLIENT_ID = "woow-ha-smart-home"
# Request all 5 scopes the server seeds. Smart Home needs smarthome:* + workspace:read;
# Security Access needs security-access:* + workspace:read. Each new config entry runs
# a fresh authorize and is granted the current scope set, so SA entries get SA scopes
# automatically. Existing SH-only entries keep their previously granted scope on refresh.
OAUTH2_SCOPES = (
    "smarthome:read smarthome:tunnel workspace:read "
    "security-access:read security-access:tunnel"
)

# --- Product types ---
# A single config entry represents one product instance (one smart home OR one
# security access). PRODUCT_SMART_HOME is the default for entries created before
# multi-product support (i.e. when CONF_PRODUCT_TYPE is absent).
PRODUCT_SMART_HOME = "smart_home"
PRODUCT_SECURITY_ACCESS = "security_access"

# --- Config entry data keys ---
CONF_WORKSPACE_ID = "workspace_id"
CONF_WORKSPACE_NAME = "workspace_name"
CONF_PRODUCT_TYPE = "product_type"
# CONF_HOME_ID / CONF_HOME_NAME hold the instance id/name for BOTH products.
# For Security Access they hold the access id/name. The key name is kept for
# backward compatibility with existing smart home entries and the service schema.
CONF_HOME_ID = "home_id"
CONF_HOME_NAME = "home_name"
CONF_TUNNEL_TOKEN = "tunnel_token"
CONF_TUNNEL_ID = "tunnel_id"
# CONF_SUBDOMAIN is smart-home only. Security Access tunnel-token has no subdomain;
# its hostname(s) come from the access detail routes[].hostname instead.
CONF_SUBDOMAIN = "subdomain"

# --- MCP（#270 Smart Home MCP）---
# 社群 integration 的 HA domain（github.com/homeassistant-ai/ha-mcp-integration，
# manifest.json domain 實測 == 此字串）。偵測三態時用它查 config entries / manifest。
HA_MCP_DOMAIN = "ha_mcp_tools"
# 平台 /status payload 的 mcp 訂閱字串：== 此值代表 400 檔（該顯示 MCP sensor），
# ""／缺欄位代表 150 檔或無訂閱（不顯示）。語意上與 HA_MCP_DOMAIN 不同，恰好同值。
MCP_SUBSCRIPTION_VALUE = "ha_mcp_tools"

# --- MCP connect URL（跨 component 契約；我們永遠不 import ha_mcp_tools）---
# 下面兩個鍵是 ha_mcp_tools 自己 config entry 的欄位名，讀法與它 config_flow 顯示
# "Remote connect URL" 時完全一致（ha_mcp_tools/config_flow.py，1.2.2 實測）：
#
#   entry.data["webhook_id"]         生效中的 webhook id，格式 "mcp_" + 32 hex。
#       使用者若在選項裡填了 webhook_id_override，ha_mcp_tools 會在自己的 bring-up
#       把它正規化寫回這個 data 鍵（embedded_entry.py），所以**讀 data 一定是最終值**，
#       我們不需要、也不該自己再套一次 override。
#   entry.options["enable_webhook"]  預設 True。False＝local-only 模式，webhook 端點
#       根本不會註冊，這時給出 URL 會 404，所以要當作「沒有 URL」。
#
# 這是別人家的內部欄位名，升級時可能改。改了的症狀是 connect URL sensor 變 unknown
# （不會壞掉其他 sensor），對照 ha_mcp_tools 的 const.py DATA_WEBHOOK_ID /
# OPT_ENABLE_WEBHOOK 兩個常數即可修。
HA_MCP_DATA_WEBHOOK_ID = "webhook_id"
HA_MCP_OPT_ENABLE_WEBHOOK = "enable_webhook"
# HA 內建 webhook component 的固定路徑前綴（homeassistant.components.webhook）。
HA_WEBHOOK_PATH = "/api/webhook/"

# --- Platforms ---
PLATFORMS: list[Platform] = [Platform.SENSOR]

# --- Error constants ---
ERR_CANNOT_CONNECT = "cannot_connect"
ERR_INVALID_AUTH = "invalid_auth"
ERR_NO_WORKSPACES = "no_workspaces"
ERR_NO_HOMES = "no_homes"
ERR_NO_ACCESSES = "no_accesses"
ERR_UNKNOWN = "unknown"

# --- Service names ---
SERVICE_START_TUNNEL = "start_tunnel"
SERVICE_STOP_TUNNEL = "stop_tunnel"
SERVICE_GET_STATUS = "get_status"

# --- Coordinator ---
UPDATE_INTERVAL = 30  # seconds

# --- Custom OAuth2 callback (replaces my.home-assistant.io relay) ---
# Cross-repo contract constant: paas side OAuthClient.HA_CALLBACK_PATH must match
# this value byte-for-byte. Changing it is a cross-repo breaking change.
WOOW_AUTH_CALLBACK_PATH = "/auth/external/woow/callback"

# hass.data flag guarding idempotent callback-view registration.
DATA_CALLBACK_VIEW_REGISTERED = "woow_paas_smart_home_callback_view_registered"

# Callback 成功頁的自動返回行為（見設計 §4.5 decision #2）：
#   "linger"  — 先顯示方向 A 品牌頁 + 倒數 CALLBACK_LINGER_SECONDS 秒，再 window.close()/導回
#               （使用者看得到品牌頁；倒數畫面即同分頁/被擋時的 fallback 畫面）
#   "instant" — 載入即 window.close()（最貼 HA 原生、最快；popup 情境幾乎看不到，
#               同分頁/被擋時仍以倒數畫面 fallback 後導回）
CALLBACK_AUTO_RETURN_MODE = "linger"
# 倒數秒數：linger 模式的可見停留時間，也是 instant 模式的 fallback 倒數時間。
CALLBACK_LINGER_SECONDS = 3
