"""Webhook support for Starling Bank Enhanced."""
from __future__ import annotations

from time import monotonic
import logging

from aiohttp import web
from homeassistant.components import webhook as hass_webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_LAST_RECEIVED,
    CONF_WEBHOOK_URL,
    COORDINATOR,
    DOMAIN,
    WEBHOOK_DEBOUNCE_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

def _is_authorized_webhook(entry: ConfigEntry, request) -> bool:
    expected = entry.data.get("webhook_secret") or entry.options.get("webhook_secret")
    if not expected:
        return True

    provided = request.headers.get("X-Starling-Webhook-Secret")
    if not provided:
        provided = request.query.get("secret")

    return provided == expected


def _looks_like_starling_payload(payload: dict) -> bool:
    """Very light validation for Starling webhook payloads."""
    if not isinstance(payload, dict):
        return False

    if "webhookNotificationUid" in payload:
        return True

    event_type = payload.get("type")
    if isinstance(event_type, str) and event_type.strip():
        return True

    content = payload.get("content")
    if isinstance(content, dict):
        nested_type = content.get("type")
        if isinstance(nested_type, str) and nested_type.strip():
            return True

    return False


async def async_register_webhook(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[str, str, callable]:
    """Register webhook handler for a config entry."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID) or hass_webhook.async_generate_id()
    webhook_url = hass_webhook.async_generate_url(hass, webhook_id)

    async def _handler(hass: HomeAssistant, webhook_id: str, request):
        return await async_handle_webhook(hass, entry, request)

    hass_webhook.async_register(
        hass,
        DOMAIN,
        f"Starling Bank Enhanced {entry.entry_id}",
        webhook_id,
        _handler,
    )

    def _unsubscribe() -> None:
        hass_webhook.async_unregister(hass, webhook_id)

    current_data = dict(entry.data)
    changed = False

    if current_data.get(CONF_WEBHOOK_ID) != webhook_id:
        current_data[CONF_WEBHOOK_ID] = webhook_id
        changed = True

    if current_data.get(CONF_WEBHOOK_URL) != webhook_url:
        current_data[CONF_WEBHOOK_URL] = webhook_url
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=current_data)

    return webhook_id, webhook_url, _unsubscribe


async def async_handle_webhook(
    hass: HomeAssistant,
    entry: ConfigEntry,
    request,
) -> web.Response:
    """Handle inbound Starling webhook."""
    domain_data = hass.data.get(DOMAIN, {})
    runtime = domain_data.get(entry.entry_id)

    if runtime is None:
        _LOGGER.warning("Received webhook for unloaded entry_id=%s", entry.entry_id)
        return web.Response(status=404, text="entry not loaded")

    if not _is_authorized_webhook(entry, request):
        _LOGGER.warning("Rejected unauthorized webhook for entry_id=%s", entry.entry_id)
        return web.Response(status=401, text="unauthorized")

    coordinator = runtime.get(COORDINATOR)
    if coordinator is None:
        _LOGGER.warning("Received webhook without coordinator for entry_id=%s", entry.entry_id)
        return web.Response(status=503, text="coordinator unavailable")

    try:
        payload = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")

    if not _looks_like_starling_payload(payload):
        return web.Response(status=400, text="invalid payload")

    now = monotonic()
    last = runtime.get("last_webhook_monotonic")
    if last is not None and (now - last) < WEBHOOK_DEBOUNCE_SECONDS:
        return web.Response(status=202, text="debounced")

    runtime["last_webhook_monotonic"] = now
    runtime[CONF_WEBHOOK_LAST_RECEIVED] = (
        payload.get("timestamp")
        or payload.get("createdAt")
        or payload.get("eventTimestamp")
    )

    event_type = (
        payload.get("type")
        or payload.get("content", {}).get("type")
        or "unknown"
    )

    _LOGGER.debug(
        "Accepted Starling webhook for entry_id=%s event_type=%s",
        entry.entry_id,
        event_type,
    )

    try:
        hass.async_create_task(coordinator.async_request_refresh())
    except Exception as err:
        _LOGGER.exception(
            "Failed to schedule refresh after webhook for entry_id=%s: %s",
            entry.entry_id,
            err,
        )
        return web.Response(status=500, text="refresh scheduling failed")

    return web.Response(status=200, text="ok")