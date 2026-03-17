"""Diagnostics support for Starling Bank Enhanced."""
from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_LAST_EVENT_TYPE,
    CONF_WEBHOOK_LAST_NONCE,
    CONF_WEBHOOK_LAST_ROUTE,
    CONF_WEBHOOK_PUBLIC_KEY,
    CONF_WEBHOOK_LAST_RECEIVED,
    CONF_WEBHOOK_URL,
    DOMAIN,
    WEBHOOK_NONCE_CACHE_KEY,
)

TO_REDACT = {
    "access_token",
    "webhook_id",
}


def _is_internal_url(value: str | None) -> bool | None:
    if not value:
        return None
    parsed = urlparse(value)
    host = parsed.hostname
    if not host:
        return None
    if host in {"localhost", "homeassistant.local", "127.0.0.1"}:
        return True
    try:
        return ip_address(host).is_private
    except ValueError:
        return False


def _mask_webhook_id(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    webhook_url = entry.data.get(CONF_WEBHOOK_URL)

    data = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "runtime_loaded": entry.entry_id in hass.data.get(DOMAIN, {}),
        "webhook": {
            "enabled": entry.options.get(
                CONF_USE_WEBHOOK,
                entry.data.get(CONF_USE_WEBHOOK, False),
            ),
            "url": webhook_url,
            "url_is_internal": _is_internal_url(webhook_url),
            "webhook_id_masked": _mask_webhook_id(entry.data.get(CONF_WEBHOOK_ID)),
            "public_key_configured": bool(
                entry.options.get(CONF_WEBHOOK_PUBLIC_KEY, entry.data.get(CONF_WEBHOOK_PUBLIC_KEY))
            ),
            "last_received": runtime.get(CONF_WEBHOOK_LAST_RECEIVED),
            "last_event_type": runtime.get(CONF_WEBHOOK_LAST_EVENT_TYPE),
            "last_route": runtime.get(CONF_WEBHOOK_LAST_ROUTE),
            "last_nonce": runtime.get(CONF_WEBHOOK_LAST_NONCE),
            "registered_in_runtime": bool(runtime.get("unsub_webhook")),
            "nonce_cache_size": len(runtime.get(WEBHOOK_NONCE_CACHE_KEY, {})),
        },
    }

    return async_redact_data(data, TO_REDACT)