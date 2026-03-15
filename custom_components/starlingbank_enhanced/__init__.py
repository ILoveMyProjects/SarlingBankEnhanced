"""The Starling Bank Enhanced integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StarlingApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_UID,
    CONF_FEATURES,
    CONF_INCLUDE_CLEARED,
    CONF_INCLUDE_EFFECTIVE,
    CONF_INCLUDE_KITE_SPACES,
    CONF_INCLUDE_SAVINGS_SPACES,
    CONF_INCLUDE_SPENDING_SPACES,
    CONF_SHOW_SAVINGS_ACCOUNT_SPACES,
    CONF_SANDBOX,
    CONF_SPACE_NAMES,
    COORDINATOR,
    DOMAIN,
    FEATURE_MAIN,
    FEATURE_SCHEDULED,
    FEATURE_SPACES,
    FEATURE_TRANSFERS,
)
from .coordinator import StarlingDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


def _slug_value(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _extract_runtime_space_names(runtime_data: dict[str, Any] | None) -> set[str]:
    if not runtime_data:
        return set()

    spaces = runtime_data.get("spaces")
    if not isinstance(spaces, dict):
        return set()

    return {name for name in spaces.keys() if isinstance(name, str) and name.strip()}


def _extract_runtime_transfer_space_names(runtime_data: dict[str, Any] | None) -> set[str]:
    if not runtime_data:
        return set()

    spaces = runtime_data.get("spaces")
    if not isinstance(spaces, dict):
        return set()

    return {
        name
        for name, value in spaces.items()
        if isinstance(name, str)
        and name.strip()
        and isinstance(value, dict)
        and bool(value.get("supports_transfers"))
    }


def _expected_unique_ids(entry: ConfigEntry, runtime_data: dict[str, Any] | None = None) -> set[str]:
    features = entry.data.get(CONF_FEATURES, [])
    configured_spaces = {
        name.strip()
        for name in entry.options.get(CONF_SPACE_NAMES, entry.data.get(CONF_SPACE_NAMES, []))
        if isinstance(name, str) and name.strip()
    }
    runtime_spaces = _extract_runtime_space_names(runtime_data)
    runtime_transfer_spaces = _extract_runtime_transfer_space_names(runtime_data)
    selected_spaces = configured_spaces | runtime_spaces

    include_cleared = entry.options.get(CONF_INCLUDE_CLEARED, entry.data.get(CONF_INCLUDE_CLEARED, True))
    include_effective = entry.options.get(CONF_INCLUDE_EFFECTIVE, entry.data.get(CONF_INCLUDE_EFFECTIVE, True))

    ids = {
        f"starling_enhanced_{entry.entry_id}_last_successful_refresh",
        f"starling_enhanced_{entry.entry_id}_last_rate_limit_at",
        f"starling_enhanced_{entry.entry_id}_backoff_until",
        f"starling_enhanced_{entry.entry_id}_request_count_last_cycle",
        f"starling_enhanced_{entry.entry_id}_account_type",
        f"starling_enhanced_{entry.entry_id}_api_issues",
    }

    if FEATURE_MAIN in features:
        if include_cleared:
            ids.add(f"starling_enhanced_{entry.entry_id}_clearedBalance")
        if include_effective:
            ids.add(f"starling_enhanced_{entry.entry_id}_effectiveBalance")

    if FEATURE_SCHEDULED in features:
        ids.update({
            f"starling_enhanced_{entry.entry_id}_has_scheduled_payments",
            f"starling_enhanced_{entry.entry_id}_scheduled_count",
            f"starling_enhanced_{entry.entry_id}_scheduled_next_date",
            f"starling_enhanced_{entry.entry_id}_scheduled_next_amount",
            f"starling_enhanced_{entry.entry_id}_scheduled_next_payee",
        })

    if FEATURE_SPACES in features:
        for space_name in selected_spaces:
            safe = _slug_value(space_name)
            ids.add(f"starling_enhanced_{entry.entry_id}_space_{safe}")

    if FEATURE_TRANSFERS in features:
        transfer_spaces = configured_spaces | runtime_transfer_spaces
        for space_name in transfer_spaces:
            safe = _slug_value(space_name)
            ids.update({
                f"starling_enhanced_{entry.entry_id}_{safe}_has_recurring_transfer",
                f"starling_enhanced_{entry.entry_id}_{safe}_recurring_amount",
                f"starling_enhanced_{entry.entry_id}_{safe}_recurring_next_date",
                f"starling_enhanced_{entry.entry_id}_{safe}_recurring_frequency",
                f"starling_enhanced_{entry.entry_id}_{safe}_history_count",
                f"starling_enhanced_{entry.entry_id}_{safe}_history_latest_amount",
                f"starling_enhanced_{entry.entry_id}_{safe}_history_latest_date",
            })

    return ids


async def _async_cleanup_stale_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime_data: dict[str, Any] | None = None,
) -> None:
    registry = er.async_get(hass)
    expected = _expected_unique_ids(entry, runtime_data)
    known_prefix = f"starling_enhanced_{entry.entry_id}_"

    for entity_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if not entity_entry.unique_id.startswith(known_prefix):
            continue
        if entity_entry.unique_id in expected:
            continue
        _LOGGER.debug(
            "Removing stale Starling entity from registry: entity_id=%s unique_id=%s",
            entity_entry.entity_id,
            entity_entry.unique_id,
        )
        registry.async_remove(entity_entry.entity_id)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    api = StarlingApiClient(session=session, access_token=entry.data[CONF_ACCESS_TOKEN], sandbox=entry.data.get(CONF_SANDBOX, False))

    features = entry.data.get(CONF_FEATURES, [])
    selected_spaces = entry.options.get(CONF_SPACE_NAMES, entry.data.get(CONF_SPACE_NAMES, []))
    coordinator = StarlingDataUpdateCoordinator(
        hass,
        api,
        entry.title,
        account_uid=entry.data.get(CONF_ACCOUNT_UID),
        include_scheduled=FEATURE_SCHEDULED in features,
        include_transfers=FEATURE_TRANSFERS in features,
        selected_spaces=selected_spaces,
        include_savings_spaces=entry.options.get(
            CONF_INCLUDE_SAVINGS_SPACES,
            entry.data.get(CONF_INCLUDE_SAVINGS_SPACES, True),
        ),
        include_spending_spaces=entry.options.get(
            CONF_INCLUDE_SPENDING_SPACES,
            entry.data.get(CONF_INCLUDE_SPENDING_SPACES, True),
        ),
        include_kite_spaces=entry.options.get(
            CONF_INCLUDE_KITE_SPACES,
            entry.data.get(CONF_INCLUDE_KITE_SPACES, True),
        ),
    )
    await coordinator.async_config_entry_first_refresh()
    await _async_cleanup_stale_entities(hass, entry, coordinator.data)

    @callback
    def _handle_coordinator_update() -> None:
        hass.async_create_task(_async_cleanup_stale_entities(hass, entry, coordinator.data))

    unsub_options_listener = entry.add_update_listener(_async_reload_entry)
    unsub_coordinator_listener = coordinator.async_add_listener(_handle_coordinator_update)
    hass.data[DOMAIN][entry.entry_id] = {
        COORDINATOR: coordinator,
        "unsub_options_listener": unsub_options_listener,
        "unsub_coordinator_listener": unsub_coordinator_listener,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        unsub_options = data.get("unsub_options_listener")
        if unsub_options:
            unsub_options()
        unsub_coordinator = data.get("unsub_coordinator_listener")
        if unsub_coordinator:
            unsub_coordinator()
    return unload_ok
