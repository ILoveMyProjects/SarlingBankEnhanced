"""Pytest fixtures for Starling Bank Enhanced tests."""
from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.starlingbank_enhanced.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_UID,
    CONF_FEATURES,
    CONF_HISTORY_LIMIT,
    CONF_INCLUDE_CLEARED,
    CONF_INCLUDE_EFFECTIVE,
    CONF_INCLUDE_KITE_SPACES,
    CONF_INCLUDE_SAVINGS_SPACES,
    CONF_INCLUDE_SPENDING_SPACES,
    CONF_SANDBOX,
    CONF_SHOW_SAVINGS_ACCOUNT_SPACES,
    CONF_SPACE_NAMES,
    CONF_UPCOMING_LIMIT,
    DOMAIN,
    FEATURE_MAIN,
    FEATURE_SPACES,
)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Create a mock Starling config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Personal",
        unique_id="account-123",
        data={
            CONF_ACCESS_TOKEN: "old-token",
            CONF_SANDBOX: False,
            CONF_ACCOUNT_UID: "account-123",
            CONF_FEATURES: [FEATURE_MAIN, FEATURE_SPACES],
            CONF_INCLUDE_CLEARED: True,
            CONF_INCLUDE_EFFECTIVE: True,
            CONF_INCLUDE_SAVINGS_SPACES: True,
            CONF_INCLUDE_SPENDING_SPACES: True,
            CONF_INCLUDE_KITE_SPACES: False,
            CONF_SHOW_SAVINGS_ACCOUNT_SPACES: True,
            CONF_SPACE_NAMES: ["Emergency Fund"],
            CONF_HISTORY_LIMIT: 10,
            CONF_UPCOMING_LIMIT: 5,
        },
    )


@pytest.fixture
def mock_accounts() -> list[dict]:
    """Mock accounts returned by Starling."""
    return [
        {
            "accountUid": "account-123",
            "name": "Personal",
            "accountType": "PRIMARY",
            "defaultCategory": "category-123",
            "currency": "GBP",
        }
    ]


@pytest.fixture
def mock_savings_goals() -> list[dict]:
    """Mock savings goals returned by Starling."""
    return [
        {
            "name": "Emergency Fund",
            "savingsGoalUid": "goal-123",
            "state": "ACTIVE",
        }
    ]


@pytest.fixture
def setup_entry_in_hass(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> MockConfigEntry:
    """Add the mock config entry to Home Assistant."""
    mock_config_entry.add_to_hass(hass)
    return mock_config_entry