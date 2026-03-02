"""Application credentials platform for Woow PaaS Smart Home."""

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import API_BASE_URL, DOMAIN, OAUTH2_AUTHORIZE, OAUTH2_TOKEN

OAUTH2_SCOPES = "smarthome:read smarthome:tunnel workspace:read"


class WoowOAuth2Implementation(AuthImplementation):
    """OAuth2 implementation that injects required scopes."""

    @property
    def extra_authorize_data(self) -> dict:
        """Extra data that needs to be appended to the authorize url."""
        return {"scope": OAUTH2_SCOPES}


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return auth implementation."""
    return WoowOAuth2Implementation(
        hass,
        auth_domain,
        credential,
        authorization_server=AuthorizationServer(
            authorize_url=f"{API_BASE_URL}{OAUTH2_AUTHORIZE}",
            token_url=f"{API_BASE_URL}{OAUTH2_TOKEN}",
        ),
    )
