"""Tests for Starling Bank Enhanced setup and unload."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.starlingbank_enhanced.api import StarlingApiError
from custom_components.starlingbank_enhanced.const import (
    COORDINATOR,
    DOMAIN,
    CONF_USE_WEBHOOK,
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_URL,
)

async def test_setup_entry_success(
    hass,
    setup_entry_in_hass,
):
    """Test successful setup of a config entry."""
    entry = setup_entry_in_hass

    mock_coordinator = MagicMock()
    mock_coordinator.data = {
        "spaces": {
            "Emergency Fund": {"supports_transfers": True}
        }
    }
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    mock_coordinator.async_add_listener = MagicMock(return_value=lambda: None)

    with (
        patch(
            "custom_components.starlingbank_enhanced.StarlingDataUpdateCoordinator",
            return_value=mock_coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=None),
        ) as mock_forward,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert DOMAIN in hass.data
        assert entry.entry_id in hass.data[DOMAIN]
        assert hass.data[DOMAIN][entry.entry_id][COORDINATOR] is mock_coordinator
        mock_coordinator.async_config_entry_first_refresh.assert_awaited_once()
        mock_forward.assert_awaited_once()

async def test_unload_entry_calls_unsub_webhook(
    hass,
    setup_entry_in_hass,
):
    entry = setup_entry_in_hass

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    mock_coordinator.async_add_listener = MagicMock(return_value=lambda: None)

    unsub_webhook = MagicMock()

    with (
        patch(
            "custom_components.starlingbank_enhanced.StarlingDataUpdateCoordinator",
            return_value=mock_coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=None),
        ),
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=True),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.data[DOMAIN][entry.entry_id]["unsub_webhook"] = unsub_webhook

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        unsub_webhook.assert_called_once()



async def test_setup_entry_clears_webhook_metadata_when_disabled(
    hass,
    mock_config_entry,
):
    mock_config_entry.data.update(
        {
            CONF_WEBHOOK_ID: "old-webhook-id",
            CONF_WEBHOOK_URL: "https://example.com/api/webhook/old-webhook-id",
        }
    )
    mock_config_entry.options = {
        CONF_USE_WEBHOOK: False,
    }
    mock_config_entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_update_entry") as mock_update_entry:
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_update_entry.called
    updated_data = mock_update_entry.call_args.kwargs["data"]
    assert CONF_WEBHOOK_ID not in updated_data
    assert CONF_WEBHOOK_URL not in updated_data


async def test_unload_entry_success(
    hass,
    setup_entry_in_hass,
):
    """Test unloading a config entry."""
    entry = setup_entry_in_hass

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    mock_coordinator.async_config_entry_first_refresh = AsyncMock()
    mock_coordinator.async_add_listener = MagicMock(return_value=lambda: None)

    unsub_options_listener = MagicMock()
    unsub_coordinator_listener = MagicMock()

    with (
        patch(
            "custom_components.starlingbank_enhanced.StarlingDataUpdateCoordinator",
            return_value=mock_coordinator,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=None),
        ),
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=True),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.data[DOMAIN][entry.entry_id]["unsub_options_listener"] = unsub_options_listener
        hass.data[DOMAIN][entry.entry_id]["unsub_coordinator_listener"] = unsub_coordinator_listener

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        unsub_options_listener.assert_called_once()
        unsub_coordinator_listener.assert_called_once()
        assert entry.entry_id not in hass.data[DOMAIN]


async def test_setup_entry_raises_reauth_on_auth_failure(
    hass,
    setup_entry_in_hass,
):
    """Test setup raises ConfigEntryAuthFailed on auth problems."""
    entry = setup_entry_in_hass

    mock_coordinator = MagicMock()
    mock_coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=StarlingApiError(
            "Unauthorized",
            status=401,
        )
    )
    mock_coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    mock_coordinator.data = {}

    with patch(
        "custom_components.starlingbank_enhanced.StarlingDataUpdateCoordinator",
        return_value=mock_coordinator,
    ):
        try:
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        except ConfigEntryAuthFailed:
            return

    raise AssertionError("ConfigEntryAuthFailed was not raised")