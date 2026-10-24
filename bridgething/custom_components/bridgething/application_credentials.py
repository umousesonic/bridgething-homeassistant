"""Tell Home Assistant where Spotify's login server is.

The credentials dialog asks for a client ID and secret. This file is what
makes that dialog available for the Bridgething integration.
"""

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return Spotify's authorization server."""
    return AuthorizationServer(
        authorize_url="https://accounts.spotify.com/authorize",
        token_url="https://accounts.spotify.com/api/token",
    )
