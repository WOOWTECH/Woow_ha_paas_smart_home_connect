"""OAuth2 implementation for Woow PaaS Smart Home (PKCE-only public client)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    LocalOAuth2ImplementationWithPkce,
)

from .const import (
    API_BASE_URL,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CLIENT_ID,
    OAUTH2_SCOPES,
    OAUTH2_TOKEN,
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


def create_implementation(hass: HomeAssistant) -> WoowPaasOAuth2:
    """Create the PKCE-only OAuth2 implementation."""
    return WoowPaasOAuth2(
        hass,
        DOMAIN,
        OAUTH2_CLIENT_ID,
        f"{API_BASE_URL}{OAUTH2_AUTHORIZE}",
        f"{API_BASE_URL}{OAUTH2_TOKEN}",
    )
