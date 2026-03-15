"""Tests for the Starling Bank Enhanced config flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, data_entry_flow

from custom_components.starlingbank_enhanced.api import StarlingApiError
from custom_components.starlingbank_enhanced.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_UID,
    CONF_FEATURES,
    CONF_SANDBOX,
    CONF_SPACE_NAMES,
    DOMAIN,
    FEATURE_MAIN,
    FEATURE_SPACES,
)


async def test_full_user_flow_creates_entry(
    hass,
    mock_accounts,
    mock_savings_goals,
):
    """Test the full user flow."""
    with (
        patch(
            "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_accounts",
            AsyncMock(return_value=mock_accounts),
        ),
        patch(
            "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_balance",
            AsyncMock(return_value={"clearedBalance": {"minorUnits": 1000, "currency": "GBP"}}),
        ),
        patch(
            "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_savings_goals",
            AsyncMock(return_value=mock_savings_goals),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_FEATURES: [FEATURE_MAIN, FEATURE_SPACES],
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "token"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ACCESS_TOKEN: "new-token",
                CONF_SANDBOX: False,
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "account"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ACCOUNT_UID: "account-123",
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "select_entities"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "include_cleared_balance": True,
                "include_effective_balance": True,
                "include_savings_spaces": True,
                "include_spending_spaces": False,
                "include_kite_spaces": False,
                CONF_SPACE_NAMES: ["Emergency Fund"],
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.CREATE_ENTRY
        assert result["title"] == "Personal"
        assert result["data"][CONF_ACCESS_TOKEN] == "new-token"
        assert result["data"][CONF_ACCOUNT_UID] == "account-123"


async def test_reauth_flow_updates_entry(
    hass,
    setup_entry_in_hass,
    mock_savings_goals,
):
    """Test reauth flow updates the existing config entry."""
    entry = setup_entry_in_hass

    with (
        patch(
            "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_account",
            AsyncMock(
                return_value={
                    "accountUid": "account-123",
                    "name": "Personal",
                    "accountType": "PRIMARY",
                }
            ),
        ),
        patch(
            "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_balance",
            AsyncMock(return_value={"clearedBalance": {"minorUnits": 1000, "currency": "GBP"}}),
        ),
        patch(
            "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_savings_goals",
            AsyncMock(return_value=mock_savings_goals),
        ),
        patch.object(
            hass.config_entries,
            "async_reload",
            AsyncMock(return_value=True),
        ) as mock_reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ACCESS_TOKEN: "replacement-token",
                CONF_SANDBOX: False,
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"

        assert entry.data[CONF_ACCESS_TOKEN] == "replacement-token"
        mock_reload.assert_awaited_once_with(entry.entry_id)


async def test_reauth_flow_wrong_account(
    hass,
    setup_entry_in_hass,
):
    """Test reauth rejects a token for another account."""
    entry = setup_entry_in_hass

    with patch(
        "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_account",
        AsyncMock(
            return_value={
                "accountUid": "other-account",
                "name": "Other",
                "accountType": "PRIMARY",
            }
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ACCESS_TOKEN: "wrong-token",
                CONF_SANDBOX: False,
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["errors"]["base"] == "wrong_account"


async def test_reauth_flow_shows_connection_error(
    hass,
    setup_entry_in_hass,
):
    """Test reauth shows connection/auth validation error."""
    entry = setup_entry_in_hass

    with patch(
        "custom_components.starlingbank_enhanced.config_flow.StarlingApiClient.async_get_account",
        AsyncMock(
            side_effect=StarlingApiError(
                "Unauthorized",
                status=401,
            )
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_ACCESS_TOKEN: "expired-token",
                CONF_SANDBOX: False,
            },
        )
        assert result["type"] is data_entry_flow.FlowResultType.FORM
        assert result["errors"]["base"] == "cannot_connect"