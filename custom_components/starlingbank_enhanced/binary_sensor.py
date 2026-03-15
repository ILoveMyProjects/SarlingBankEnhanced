"""Binary sensors for Starling Bank Enhanced."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_FEATURES,
    CONF_SPACE_NAMES,
    COORDINATOR,
    DOMAIN,
    FEATURE_SCHEDULED,
    FEATURE_TRANSFERS,
)
from .coordinator import StarlingDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StarlingDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    features = entry.data.get(CONF_FEATURES, [])
    selected_spaces = entry.options.get(CONF_SPACE_NAMES, entry.data.get(CONF_SPACE_NAMES, []))

    entities: list[BinarySensorEntity] = []
    if FEATURE_SCHEDULED in features:
        entities.append(StarlingHasScheduledPaymentsBinarySensor(coordinator, entry))
    if FEATURE_TRANSFERS in features:
        spaces_data = coordinator.data.get("spaces", {})
        for space_name in selected_spaces:
            space = spaces_data.get(space_name, {})
            if not space or not space.get("supports_transfers"):
                continue
            entities.append(StarlingHasRecurringTransferBinarySensor(coordinator, entry, space_name))

    async_add_entities(entities)


class StarlingBinaryBaseEntity(CoordinatorEntity[StarlingDataUpdateCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self) -> dict[str, Any]:
        account_uid = self.coordinator.data.get("account_uid", self._entry.entry_id)
        account_name = self.coordinator.data.get("account_name", self._entry.title)
        return {
            "identifiers": {(DOMAIN, account_uid)},
            "name": account_name,
            "manufacturer": "Starling Bank",
            "model": "Enhanced API integration",
        }


class StarlingHasScheduledPaymentsBinarySensor(StarlingBinaryBaseEntity):
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Account has scheduled payments"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_has_scheduled_payments"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("scheduled_payments", []))


class StarlingHasRecurringTransferBinarySensor(StarlingBinaryBaseEntity):
    _attr_icon = "mdi:autorenew"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} has recurring transfer"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_has_recurring_transfer"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("recurring_transfers", {}).get(self._space_name))
