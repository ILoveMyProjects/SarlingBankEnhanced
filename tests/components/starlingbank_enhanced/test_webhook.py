from unittest.mock import AsyncMock

from aiohttp.test_utils import make_mocked_request

from custom_components.starlingbank_enhanced.const import (
    CONF_WEBHOOK_LAST_RECEIVED,
    COORDINATOR,
    DOMAIN,
)
from custom_components.starlingbank_enhanced.webhook import async_handle_webhook


async def test_webhook_valid_payload_triggers_refresh(hass, mock_config_entry):
    coordinator = AsyncMock()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: coordinator,
        "last_webhook_monotonic": None,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        return {
            "webhookNotificationUid": "123",
            "type": "TRANSACTION_FEED_ITEM",
            "timestamp": "2026-03-15T12:00:00Z",
        }

    request.json = _json

    response = await async_handle_webhook(hass, mock_config_entry, request)

    assert response.status == 200
    assert (
        hass.data[DOMAIN][mock_config_entry.entry_id][CONF_WEBHOOK_LAST_RECEIVED]
        == "2026-03-15T12:00:00Z"
    )
    assert coordinator.async_request_refresh.called


async def test_webhook_invalid_json_returns_400(hass, mock_config_entry):
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: AsyncMock(),
        "last_webhook_monotonic": None,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        raise ValueError("bad json")

    request.json = _json

    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 400


async def test_webhook_debounce_returns_202(hass, mock_config_entry):
    coordinator = AsyncMock()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: coordinator,
        "last_webhook_monotonic": 999999999.0,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        return {
            "webhookNotificationUid": "123",
            "type": "TRANSACTION_FEED_ITEM",
        }

    request.json = _json

    from custom_components.starlingbank_enhanced import webhook as webhook_module

    original = webhook_module.monotonic
    webhook_module.monotonic = lambda: 999999999.1
    try:
        response = await async_handle_webhook(hass, mock_config_entry, request)
    finally:
        webhook_module.monotonic = original

    assert response.status == 202
    assert not coordinator.async_request_refresh.called


async def test_webhook_without_coordinator_returns_503(hass, mock_config_entry):
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        "last_webhook_monotonic": None,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        return {
            "webhookNotificationUid": "123",
            "type": "TRANSACTION_FEED_ITEM",
        }

    request.json = _json

    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 503


async def test_webhook_invalid_payload_returns_400(hass, mock_config_entry):
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: AsyncMock(),
        "last_webhook_monotonic": None,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        return {"foo": "bar"}

    request.json = _json

    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 400