"""OAuth2 implementation for Woow PaaS Smart Home (PKCE-only public client)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import http
from homeassistant.helpers.config_entry_oauth2_flow import (
    HEADER_FRONTEND_BASE,
    LocalOAuth2ImplementationWithPkce,
)

from .const import (
    API_BASE_URL,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_SCOPES,
    OAUTH2_TOKEN,
    WOOW_AUTH_CALLBACK_PATH,
)


class WoowPaasOAuth2(LocalOAuth2ImplementationWithPkce):
    """OAuth2 implementation with PKCE (S256) and required scopes."""

    @property
    def name(self) -> str:
        """Name of the implementation."""
        return "Woow PaaS Smart Home"

    @property
    def extra_authorize_data(self) -> dict:
        """Add scopes while preserving PKCE challenge from parent."""
        data = {"scope": OAUTH2_SCOPES}
        # super() returns {"code_challenge": ..., "code_challenge_method": "S256"}
        data.update(super().extra_authorize_data)
        return data

    @property
    def redirect_uri(self) -> str:
        """Return the WOOW-hosted callback on the local HA instance.

        Bypasses HA Core's my.home-assistant.io relay (async_get_redirect_uri
        returns MY_AUTH_CALLBACK_PATH whenever the `my` component is loaded) so
        the OAuth callback lands directly on a component-rendered WOOW page.
        Mirrors HA Core's non-`my` branch.
        """
        if (req := http.current_request.get()) is None:
            raise RuntimeError("No current request in context")
        if (ha_host := req.headers.get(HEADER_FRONTEND_BASE)) is None:
            raise RuntimeError("No header in request")
        return f"{ha_host}{WOOW_AUTH_CALLBACK_PATH}"


def create_implementation(hass: HomeAssistant) -> WoowPaasOAuth2:
    """Create the PKCE-only OAuth2 implementation."""
    return WoowPaasOAuth2(
        hass,
        DOMAIN,
        OAUTH2_CLIENT_ID,
        f"{API_BASE_URL}{OAUTH2_AUTHORIZE}",
        f"{API_BASE_URL}{OAUTH2_TOKEN}",
    )
