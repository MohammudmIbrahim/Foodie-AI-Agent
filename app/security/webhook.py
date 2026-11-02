"""Webhook origin checks for the local BlueBubbles integration."""

from __future__ import annotations

import hmac
import ipaddress

from app.config import Settings


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def verify_webhook(settings: Settings, token: str | None, client_host: str | None) -> bool:
    """BlueBubbles webhooks are unsigned; require loopback origin AND the shared URL token."""
    secret = settings.bb_webhook_secret
    if not secret or not token or not is_loopback(client_host):
        return False
    return hmac.compare_digest(token.encode(), secret.encode())
