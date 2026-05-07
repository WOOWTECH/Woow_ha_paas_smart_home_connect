"""Config flow for Woow PaaS Smart Home integration."""

from __future__ import annotations

from http import HTTPStatus
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.config_entry_oauth2_flow import (
    AbstractOAuth2FlowHandler,
    async_oauth2_request,
)

from .api_client import ApiError, AuthenticationError
from .const import (
    API_BASE_URL,
    API_PATH_HOME_TUNNEL_TOKEN,
    API_PATH_WORKSPACE_HOMES,
    API_PATH_WORKSPACES,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_SUBDOMAIN,
    CONF_TUNNEL_ID,
    CONF_TUNNEL_TOKEN,
    CONF_WORKSPACE_ID,
    CONF_WORKSPACE_NAME,
    DOMAIN,
    ERR_CANNOT_CONNECT,
    ERR_INVALID_AUTH,
    ERR_NO_HOMES,
    ERR_NO_WORKSPACES,
    ERR_UNKNOWN,
)
from .oauth2 import create_implementation

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle a config flow for Woow PaaS Smart Home."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._oauth_data: dict[str, Any] = {}
        self._workspaces: list[dict[str, Any]] = []
        self._homes: list[dict[str, Any]] = []
        self._selected_workspace_id: int | None = None
        self._selected_workspace_name: str | None = None
        _LOGGER.debug("ConfigFlow initialized for domain %s", DOMAIN)

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Lazily register the PKCE OAuth implementation, then start the flow.

        HA only invokes ``async_setup`` for integrations referenced by YAML or
        with an existing config entry, so for first-time UI-driven setup the
        impl must be registered when the flow starts.
        """
        if not await config_entry_oauth2_flow.async_get_implementations(
            self.hass, self.DOMAIN
        ):
            config_entry_oauth2_flow.async_register_implementation(
                self.hass, self.DOMAIN, create_implementation(self.hass)
            )
        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle OAuth2 completion and proceed to workspace selection."""
        _LOGGER.debug(
            "async_oauth_create_entry called; data keys: %s, "
            "token keys: %s, token_type: %s, expires_in: %s",
            list(data.keys()),
            list(data["token"].keys()) if "token" in data else "<no token>",
            data.get("token", {}).get("token_type"),
            data.get("token", {}).get("expires_in"),
        )
        self._oauth_data = data
        return await self.async_step_select_workspace()

    async def _async_api_request(self, method: str, path: str) -> Any:
        """Make an authenticated API request using the OAuth2 token."""
        url = f"{API_BASE_URL}{path}"
        _LOGGER.debug("API request: %s %s", method, url)
        resp = await async_oauth2_request(
            self.hass, self._oauth_data["token"], method, url
        )
        _LOGGER.debug("API response: %s %s -> HTTP %s", method, url, resp.status)
        if resp.status == HTTPStatus.OK:
            return await resp.json()

        # Extract error detail from response body
        try:
            body = await resp.json()
            detail = (
                body.get("detail", body.get("error", ""))
                if isinstance(body, dict)
                else ""
            )
        except (ValueError, aiohttp.ContentTypeError):
            detail = await resp.text()

        message = f"{resp.status} {detail}" if detail else str(resp.status)

        if resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise AuthenticationError(resp.status, message)
        raise ApiError(resp.status, message)

    async def async_step_select_workspace(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle workspace selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input["workspace"]
            _LOGGER.debug("User selected workspace: %s", selected)
            for ws in self._workspaces:
                if str(ws["id"]) == selected:
                    self._selected_workspace_id = ws["id"]
                    self._selected_workspace_name = ws["name"]
                    break
            return await self.async_step_select_home()

        # Fetch workspaces from API
        _LOGGER.debug("Fetching workspaces from API")
        try:
            data = await self._async_api_request("GET", API_PATH_WORKSPACES)
            self._workspaces = data["workspaces"]
            _LOGGER.debug("Fetched %d workspace(s)", len(self._workspaces))
        except AuthenticationError:
            _LOGGER.warning("Authentication failed while fetching workspaces")
            errors["base"] = ERR_INVALID_AUTH
            return self.async_show_form(
                step_id="select_workspace",
                data_schema=vol.Schema({}),
                errors=errors,
            )
        except (ApiError, aiohttp.ClientError):
            _LOGGER.exception("Failed to fetch workspaces")
            errors["base"] = ERR_CANNOT_CONNECT
            return self.async_show_form(
                step_id="select_workspace",
                data_schema=vol.Schema({}),
                errors=errors,
            )
        except Exception:
            _LOGGER.exception("Unexpected error fetching workspaces")
            errors["base"] = ERR_UNKNOWN
            return self.async_show_form(
                step_id="select_workspace",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        if not self._workspaces:
            return self.async_abort(reason=ERR_NO_WORKSPACES)

        # Auto-skip if only one workspace
        if len(self._workspaces) == 1:
            self._selected_workspace_id = self._workspaces[0]["id"]
            self._selected_workspace_name = self._workspaces[0]["name"]
            return await self.async_step_select_home()

        workspace_options = {
            str(ws["id"]): ws["name"] for ws in self._workspaces
        }
        return self.async_show_form(
            step_id="select_workspace",
            data_schema=vol.Schema(
                {vol.Required("workspace"): vol.In(workspace_options)}
            ),
            errors=errors,
        )

    async def async_step_select_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle smart home selection step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = user_input["home"]
            _LOGGER.debug("User selected home: %s", selected)
            for home in self._homes:
                if str(home["id"]) == selected:
                    return await self._async_create_entry(
                        home_id=home["id"],
                        home_name=home["name"],
                    )
            errors["base"] = ERR_CANNOT_CONNECT

        if not errors:
            _LOGGER.debug(
                "Fetching homes for workspace_id=%s", self._selected_workspace_id
            )
            try:
                path = API_PATH_WORKSPACE_HOMES.format(
                    workspace_id=self._selected_workspace_id
                )
                data = await self._async_api_request("GET", path)
                self._homes = data["homes"]
                _LOGGER.debug("Fetched %d home(s)", len(self._homes))
            except AuthenticationError:
                _LOGGER.warning("Authentication failed while fetching homes")
                errors["base"] = ERR_INVALID_AUTH
            except (ApiError, aiohttp.ClientError):
                _LOGGER.exception("Failed to fetch homes")
                errors["base"] = ERR_CANNOT_CONNECT
            except Exception:
                _LOGGER.exception("Unexpected error fetching homes")
                errors["base"] = ERR_UNKNOWN

        if errors:
            return self.async_show_form(
                step_id="select_home",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        if not self._homes:
            return self.async_show_form(
                step_id="select_home",
                data_schema=vol.Schema({}),
                errors={"base": ERR_NO_HOMES},
            )

        # Auto-skip if only one home
        if len(self._homes) == 1:
            return await self._async_create_entry(
                home_id=self._homes[0]["id"],
                home_name=self._homes[0]["name"],
            )

        home_options = {
            str(home["id"]): home["name"] for home in self._homes
        }
        return self.async_show_form(
            step_id="select_home",
            data_schema=vol.Schema(
                {vol.Required("home"): vol.In(home_options)}
            ),
            errors=errors,
        )

    async def _async_create_entry(
        self, home_id: int, home_name: str
    ) -> ConfigFlowResult:
        """Fetch tunnel token and create the config entry."""
        _LOGGER.debug(
            "Creating config entry for home_id=%s, home_name=%s", home_id, home_name
        )
        await self.async_set_unique_id(str(home_id))
        self._abort_if_unique_id_configured()

        _LOGGER.debug("Fetching tunnel token for home_id=%s", home_id)
        try:
            path = API_PATH_HOME_TUNNEL_TOKEN.format(home_id=home_id)
            tunnel_data = await self._async_api_request("GET", path)
            _LOGGER.debug(
                "Tunnel token fetched; tunnel_id=%s, subdomain=%s",
                tunnel_data.get("tunnel_id"),
                tunnel_data.get("subdomain"),
            )
        except AuthenticationError:
            _LOGGER.warning("Authentication failed while fetching tunnel token")
            return self.async_show_form(
                step_id="select_home",
                data_schema=vol.Schema({}),
                errors={"base": ERR_INVALID_AUTH},
            )
        except (ApiError, aiohttp.ClientError):
            _LOGGER.exception("Failed to fetch tunnel token")
            return self.async_show_form(
                step_id="select_home",
                data_schema=vol.Schema({}),
                errors={"base": ERR_CANNOT_CONNECT},
            )
        except Exception:
            _LOGGER.exception("Unexpected error fetching tunnel token")
            return self.async_show_form(
                step_id="select_home",
                data_schema=vol.Schema({}),
                errors={"base": ERR_UNKNOWN},
            )

        return self.async_create_entry(
            title=home_name,
            data={
                **self._oauth_data,
                CONF_WORKSPACE_ID: self._selected_workspace_id,
                CONF_WORKSPACE_NAME: self._selected_workspace_name,
                CONF_HOME_ID: home_id,
                CONF_HOME_NAME: home_name,
                CONF_TUNNEL_TOKEN: tunnel_data["tunnel_token"],
                CONF_TUNNEL_ID: tunnel_data["tunnel_id"],
                CONF_SUBDOMAIN: tunnel_data["subdomain"],
            },
        )
