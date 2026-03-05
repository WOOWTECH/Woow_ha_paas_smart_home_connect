"""Application credentials platform for Woow PaaS Smart Home."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError, ClientResponseError

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_BASE_URL, DOMAIN, OAUTH2_AUTHORIZE, OAUTH2_TOKEN

_LOGGER = logging.getLogger(__name__)

OAUTH2_SCOPES = "smarthome:read smarthome:tunnel workspace:read"


def _mask(value: str | None, visible: int = 4) -> str:
    """Mask a sensitive string, showing only the last `visible` characters."""
    if not value:
        return "<empty>"
    if len(value) <= visible:
        return "****"
    return f"****{value[-visible:]}"


class WoowOAuth2Implementation(AuthImplementation):
    """OAuth2 implementation that injects required scopes."""

    @property
    def extra_authorize_data(self) -> dict:
        """Extra data that needs to be appended to the authorize url."""
        return {"scope": OAUTH2_SCOPES}

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Resolve the authorization code to tokens with debug logging."""
        _LOGGER.debug(
            "async_resolve_external_data called; external_data keys: %s",
            list(external_data.keys()) if isinstance(external_data, dict) else type(external_data),
        )
        if isinstance(external_data, dict):
            _LOGGER.debug(
                "Authorization code (masked): %s, redirect_uri: %s",
                _mask(external_data.get("code"), 6),
                external_data.get("state", {}).get("redirect_uri", "<missing>"),
            )
        try:
            token = await super().async_resolve_external_data(external_data)
        except (ClientResponseError, ClientError) as err:
            _LOGGER.error(
                "Token exchange failed during async_resolve_external_data: %s", err
            )
            raise
        _LOGGER.debug(
            "Token exchange succeeded; token type: %s, expires_in: %s, scopes: %s",
            token.get("token_type"),
            token.get("expires_in"),
            token.get("scope"),
        )
        return token

    async def _token_request(self, data: dict) -> dict:
        """Make a token request with detailed debug logging."""
        _LOGGER.debug(
            "Token request -- url: %s, grant_type: %s, client_id: %s, "
            "client_secret (masked): %s, redirect_uri: %s, code (masked): %s, "
            "refresh_token (masked): %s",
            self.token_url,
            data.get("grant_type"),
            data.get("client_id", self.client_id),
            _mask(data.get("client_secret") or self.client_secret),
            data.get("redirect_uri", "<not set>"),
            _mask(data.get("code"), 6),
            _mask(data.get("refresh_token")),
        )

        session = async_get_clientsession(self.hass)

        # Ensure client credentials are present
        data["client_id"] = self.client_id
        if self.client_secret:
            data["client_secret"] = self.client_secret

        try:
            resp = await session.post(self.token_url, data=data)
        except ClientError as err:
            _LOGGER.error(
                "Network error sending token request to %s: %s", self.token_url, err
            )
            raise

        if resp.status >= 400:
            # Read raw body first; then attempt JSON parse from the text
            error_text = ""
            try:
                error_text = await resp.text()
            except Exception:
                pass

            error_body: dict[str, str] = {}
            if error_text:
                try:
                    import json

                    error_body = json.loads(error_text)
                except Exception:
                    pass

            _LOGGER.error(
                "Token request to %s failed -- HTTP %s, "
                "error: %s, error_description: %s, raw body: %.500s",
                self.token_url,
                resp.status,
                error_body.get("error", "<none>"),
                error_body.get("error_description", "<none>"),
                error_text,
            )
            resp.raise_for_status()

        result = await resp.json()
        _LOGGER.debug(
            "Token response OK -- token_type: %s, expires_in: %s, scope: %s, "
            "access_token (masked): %s",
            result.get("token_type"),
            result.get("expires_in"),
            result.get("scope"),
            _mask(result.get("access_token")),
        )
        return result


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return auth implementation."""
    auth_server = AuthorizationServer(
        authorize_url=f"{API_BASE_URL}{OAUTH2_AUTHORIZE}",
        token_url=f"{API_BASE_URL}{OAUTH2_TOKEN}",
    )
    _LOGGER.debug(
        "Creating WoowOAuth2Implementation -- authorize_url: %s, "
        "token_url: %s, client_id: %s, client_secret (masked): %s",
        auth_server.authorize_url,
        auth_server.token_url,
        credential.client_id,
        _mask(credential.client_secret),
    )
    return WoowOAuth2Implementation(
        hass,
        auth_domain,
        credential,
        authorization_server=auth_server,
    )
