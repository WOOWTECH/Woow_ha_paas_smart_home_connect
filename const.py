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

# API endpoint paths
API_PATH_WORKSPACES = "/api/smarthome/workspaces"
API_PATH_WORKSPACE_HOMES = "/api/smarthome/workspaces/{workspace_id}/homes"
API_PATH_HOME = "/api/smarthome/homes/{home_id}"
API_PATH_HOME_TUNNEL_TOKEN = "/api/smarthome/homes/{home_id}/tunnel-token"
API_PATH_HOME_STATUS = "/api/smarthome/homes/{home_id}/status"

# OAuth2 endpoints
OAUTH2_AUTHORIZE = "/oauth2/authorize"
OAUTH2_TOKEN = "/oauth2/token"

# OAuth2 public client (PKCE-only, no client_secret).
# This client_id is registered as a public client on paas-platform and may be
# committed to a public repo. See sm-api-doc §3 / §4 for migration spec.
OAUTH2_CLIENT_ID = "woow-ha-smart-home"
OAUTH2_SCOPES = "smarthome:read smarthome:tunnel workspace:read"

# --- Config entry data keys ---
CONF_WORKSPACE_ID = "workspace_id"
CONF_WORKSPACE_NAME = "workspace_name"
CONF_HOME_ID = "home_id"
CONF_HOME_NAME = "home_name"
CONF_TUNNEL_TOKEN = "tunnel_token"
CONF_TUNNEL_ID = "tunnel_id"
CONF_SUBDOMAIN = "subdomain"

# --- Platforms ---
PLATFORMS: list[Platform] = [Platform.SENSOR]

# --- Error constants ---
ERR_CANNOT_CONNECT = "cannot_connect"
ERR_INVALID_AUTH = "invalid_auth"
ERR_NO_WORKSPACES = "no_workspaces"
ERR_NO_HOMES = "no_homes"
ERR_UNKNOWN = "unknown"

# --- Service names ---
SERVICE_START_TUNNEL = "start_tunnel"
SERVICE_STOP_TUNNEL = "stop_tunnel"
SERVICE_GET_STATUS = "get_status"

# --- Coordinator ---
UPDATE_INTERVAL = 30  # seconds
