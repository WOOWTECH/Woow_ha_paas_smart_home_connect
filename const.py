"""Constants for the Woow PaaS Smart Home integration."""

from enum import StrEnum

from homeassistant.const import Platform


class TunnelStatus(StrEnum):
    """Tunnel connection status values."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    UNKNOWN = "unknown"

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
