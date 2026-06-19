"""Config flow for Woow PaaS Smart Home integration."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.config_entry_oauth2_flow import (
    AbstractOAuth2FlowHandler,
    async_oauth2_request,
)

from .api_client import ApiError, AuthenticationError
from .const import (
    API_BASE_URL,
    API_PATH_HOME_TUNNEL_TOKEN,
    API_PATH_SA_ACCESS_TUNNEL_TOKEN,
    API_PATH_SA_WORKSPACE_ACCESSES,
    API_PATH_WORKSPACE_HOMES,
    API_PATH_WORKSPACES,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_PRODUCT_TYPE,
    CONF_SUBDOMAIN,
    CONF_TUNNEL_ID,
    CONF_TUNNEL_TOKEN,
    CONF_WORKSPACE_ID,
    CONF_WORKSPACE_NAME,
    DOMAIN,
    ERR_CANNOT_CONNECT,
    ERR_INVALID_AUTH,
    ERR_NO_ACCESSES,
    ERR_NO_HOMES,
    ERR_NO_WORKSPACES,
    ERR_UNKNOWN,
    PRODUCT_SECURITY_ACCESS,
    PRODUCT_SMART_HOME,
)
from .oauth2 import create_implementation
from .oauth_callback_view import async_register_woow_callback_view

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
        self._accesses: list[dict[str, Any]] = []
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
        async_register_woow_callback_view(self.hass)
        return await super().async_step_user(user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the stored OAuth token is rejected.

        Triggered by ConfigEntryAuthFailed (e.g. a 401/403 from the API, or a
        token lacking a now-required scope). Re-running OAuth grants the current
        scope set; the existing entry's workspace/instance/tunnel data is kept.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth, then re-run the OAuth authorize flow."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        # async_step_user lazily (re)registers the PKCE impl before delegating.
        return await self.async_step_user()

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle OAuth2 completion.

        Normal flow: continue to workspace -> product -> instance selection.
        Reauth flow: only the token changed, so update the existing entry's data
        in place (preserving workspace/instance/tunnel keys) and reload.
        """
        _LOGGER.debug(
            "async_oauth_create_entry called; source: %s, data keys: %s, "
            "token keys: %s, token_type: %s, expires_in: %s",
            self.source,
            list(data.keys()),
            list(data["token"].keys()) if "token" in data else "<no token>",
            data.get("token", {}).get("token_type"),
            data.get("token", {}).get("expires_in"),
        )
        if self.source == SOURCE_REAUTH:
            _LOGGER.debug("Reauth complete; refreshing token on existing entry")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=data,
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

        if user_input is not None and "workspace" in user_input:
            selected = user_input["workspace"]
            _LOGGER.debug("User selected workspace: %s", selected)
            for ws in self._workspaces:
                if str(ws["id"]) == selected:
                    self._selected_workspace_id = ws["id"]
                    self._selected_workspace_name = ws["name"]
                    break
            return await self.async_step_select_product()

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

        # Always show the list so the user explicitly confirms which workspace
        # to use, even when only one exists.
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

    async def async_step_select_product(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle product-type selection (Smart Home or Security Access)."""
        if user_input is not None and "product" in user_input:
            product = user_input["product"]
            _LOGGER.debug("User selected product: %s", product)
            if product == PRODUCT_SECURITY_ACCESS:
                return await self.async_step_select_access()
            return await self.async_step_select_home()

        product_options = {
            PRODUCT_SMART_HOME: "Smart Home",
            PRODUCT_SECURITY_ACCESS: "Security Access",
        }
        return self.async_show_form(
            step_id="select_product",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "product", default=PRODUCT_SMART_HOME
                    ): vol.In(product_options)
                }
            ),
        )

    async def async_step_select_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle smart home selection step."""
        errors: dict[str, str] = {}

        if user_input is not None and "home" in user_input:
            selected = user_input["home"]
            _LOGGER.debug("User selected home: %s", selected)
            for home in self._homes:
                if str(home["id"]) == selected:
                    return await self._async_create_entry(
                        product_type=PRODUCT_SMART_HOME,
                        instance_id=home["id"],
                        instance_name=home["name"],
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
            return self.async_abort(reason=ERR_NO_HOMES)

        # Always show the list so the user explicitly picks which smart home to
        # create an entry for, even when only one exists.
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

    async def async_step_select_access(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle security access selection step (mirrors select_home)."""
        errors: dict[str, str] = {}

        if user_input is not None and "access" in user_input:
            selected = user_input["access"]
            _LOGGER.debug("User selected access: %s", selected)
            for access in self._accesses:
                if str(access["id"]) == selected:
                    return await self._async_create_entry(
                        product_type=PRODUCT_SECURITY_ACCESS,
                        instance_id=access["id"],
                        instance_name=access["name"],
                    )
            errors["base"] = ERR_CANNOT_CONNECT

        if not errors:
            _LOGGER.debug(
                "Fetching accesses for workspace_id=%s",
                self._selected_workspace_id,
            )
            try:
                path = API_PATH_SA_WORKSPACE_ACCESSES.format(
                    workspace_id=self._selected_workspace_id
                )
                data = await self._async_api_request("GET", path)
                self._accesses = data["accesses"]
                _LOGGER.debug("Fetched %d access(es)", len(self._accesses))
            except AuthenticationError:
                _LOGGER.warning("Authentication failed while fetching accesses")
                errors["base"] = ERR_INVALID_AUTH
            except (ApiError, aiohttp.ClientError):
                _LOGGER.exception("Failed to fetch accesses")
                errors["base"] = ERR_CANNOT_CONNECT
            except Exception:
                _LOGGER.exception("Unexpected error fetching accesses")
                errors["base"] = ERR_UNKNOWN

        if errors:
            return self.async_show_form(
                step_id="select_access",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        if not self._accesses:
            return self.async_abort(reason=ERR_NO_ACCESSES)

        # Always show the list so the user explicitly picks which security
        # access to create an entry for, even when only one exists.
        access_options = {
            str(access["id"]): access["name"] for access in self._accesses
        }
        return self.async_show_form(
            step_id="select_access",
            data_schema=vol.Schema(
                {vol.Required("access"): vol.In(access_options)}
            ),
            errors=errors,
        )

    async def _async_create_entry(
        self,
        *,
        product_type: str,
        instance_id: int,
        instance_name: str,
    ) -> ConfigFlowResult:
        """Fetch tunnel token and create the config entry for either product.

        Smart Home and Security Access share the same shape (one entry per
        instance). They differ only in the tunnel-token endpoint and whether a
        subdomain is returned (Security Access has none).
        """
        is_sa = product_type == PRODUCT_SECURITY_ACCESS
        # Namespace the SA unique_id so an access and a home with the same numeric
        # id never collide. Smart home keeps its plain id for backward compat.
        unique_id = f"sa_{instance_id}" if is_sa else str(instance_id)
        error_step = "select_access" if is_sa else "select_home"

        _LOGGER.debug(
            "Creating config entry: product=%s, instance_id=%s, name=%s",
            product_type,
            instance_id,
            instance_name,
        )
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        _LOGGER.debug("Fetching tunnel token for %s %s", product_type, instance_id)
        try:
            if is_sa:
                path = API_PATH_SA_ACCESS_TUNNEL_TOKEN.format(access_id=instance_id)
            else:
                path = API_PATH_HOME_TUNNEL_TOKEN.format(home_id=instance_id)
            tunnel_data = await self._async_api_request("GET", path)
            _LOGGER.debug(
                "Tunnel token fetched; tunnel_id=%s, subdomain=%s",
                tunnel_data.get("tunnel_id"),
                tunnel_data.get("subdomain"),
            )
        except AuthenticationError:
            _LOGGER.warning("Authentication failed while fetching tunnel token")
            return self.async_show_form(
                step_id=error_step,
                data_schema=vol.Schema({}),
                errors={"base": ERR_INVALID_AUTH},
            )
        except (ApiError, aiohttp.ClientError):
            _LOGGER.exception("Failed to fetch tunnel token")
            return self.async_show_form(
                step_id=error_step,
                data_schema=vol.Schema({}),
                errors={"base": ERR_CANNOT_CONNECT},
            )
        except Exception:
            _LOGGER.exception("Unexpected error fetching tunnel token")
            return self.async_show_form(
                step_id=error_step,
                data_schema=vol.Schema({}),
                errors={"base": ERR_UNKNOWN},
            )

        data: dict[str, Any] = {
            **self._oauth_data,
            CONF_WORKSPACE_ID: self._selected_workspace_id,
            CONF_WORKSPACE_NAME: self._selected_workspace_name,
            CONF_PRODUCT_TYPE: product_type,
            CONF_HOME_ID: instance_id,
            CONF_HOME_NAME: instance_name,
            CONF_TUNNEL_TOKEN: tunnel_data["tunnel_token"],
            CONF_TUNNEL_ID: tunnel_data["tunnel_id"],
        }
        # Security Access tunnel-token carries no subdomain; the hostname(s) are
        # derived live from the access routes by the coordinator.
        if not is_sa:
            data[CONF_SUBDOMAIN] = tunnel_data["subdomain"]

        return self.async_create_entry(title=instance_name, data=data)
