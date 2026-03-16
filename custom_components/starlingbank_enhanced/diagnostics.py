"""Diagnostics support for Starling Bank Enhanced."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_LAST_RECEIVED,
    CONF_WEBHOOK_URL,
    DOMAIN,
)

TO_REDACT = {
    "access_token",
    "webhook_id",
}


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
            "url": entry.data.get(CONF_WEBHOOK_URL),
            "webhook_id_masked": _mask_webhook_id(entry.data.get(CONF_WEBHOOK_ID)),
            "last_received": runtime.get(CONF_WEBHOOK_LAST_RECEIVED),
            "registered_in_runtime": bool(runtime.get("unsub_webhook")),
        },
    }

    return async_redact_data(data, TO_REDACT)