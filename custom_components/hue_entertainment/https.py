"""Opt-in HTTPS listener on :443 for official CLIP v2 clients (Hue Sync app).

Port 443 is never shared with Home Assistant's own server: official clients
check a bridge-shaped certificate, which HA's listener can't present. So the
integration opens its own TLS listener when the user enables it, checks the
port before saving the option, and raises a repair issue if it later can't
bind. The v1 HTTP + DTLS path (Ambilight TVs) never depends on it.
"""

from __future__ import annotations

import socket
from pathlib import Path

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import STORAGE_DIR

from .const import DOMAIN, HTTPS_PORT, HTTPS_PORT_UNAVAILABLE, HTTPS_PORT_USED_BY_HA

_ISSUES = (HTTPS_PORT_USED_BY_HA, HTTPS_PORT_UNAVAILABLE)


def cert_path(hass: HomeAssistant, bridge_id: str) -> Path:
    """Where the bridge's key + certificate PEM lives."""
    return Path(hass.config.path(STORAGE_DIR, f"{DOMAIN}.{bridge_id.lower()}.pem"))


@callback
def ha_holds_https_port(hass: HomeAssistant, bind_ip: str | None) -> bool:
    """Whether HA's own server is on :443 and no separate bridge IP is set."""
    return not bind_ip and hass.http.server_port == HTTPS_PORT


def probe_port(bind_ip: str | None, port: int = HTTPS_PORT) -> OSError | None:
    """Try to bind the listener's address; return the error, or None if free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Same as aiohttp's listener: a socket in TIME_WAIT is not a conflict
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_ip or "0.0.0.0", port))
        except OSError as err:
            return err
    return None


@callback
def async_set_https_issue(hass: HomeAssistant, status: str | None, error: str = "") -> None:
    """Show the repair issue for ``status`` (one of the two failures), clear the other."""
    for issue_id in _ISSUES:
        if issue_id == status:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=issue_id,
                translation_placeholders={"port": str(HTTPS_PORT), "error": error},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
