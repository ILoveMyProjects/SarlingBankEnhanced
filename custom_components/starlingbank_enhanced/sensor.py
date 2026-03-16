"""Sensors for Starling Bank Enhanced."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_FEATURES,
    CONF_HISTORY_LIMIT,
    CONF_INCLUDE_CLEARED,
    CONF_INCLUDE_EFFECTIVE,
    CONF_INCLUDE_KITE_SPACES,
    CONF_INCLUDE_SAVINGS_SPACES,
    CONF_INCLUDE_SPENDING_SPACES,
    CONF_SPACE_NAMES,
    CONF_UPCOMING_LIMIT,
    COORDINATOR,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_UPCOMING_LIMIT,
    DOMAIN,
    FEATURE_MAIN,
    FEATURE_SCHEDULED,
    FEATURE_SPACES,
    FEATURE_TRANSFERS,
)
from .coordinator import StarlingDataUpdateCoordinator


def _minor_to_major(minor_units: int | None) -> float | None:
    if minor_units is None:
        return None
    return float(Decimal(int(minor_units)) / Decimal(100))


def _serialize_payment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": item.get("uid"),
        "amount": _minor_to_major(item.get("amount_minor")),
        "currency": item.get("currency"),
        "reference": item.get("reference"),
        "counterparty": item.get("counterparty"),
        "next_date": item.get("next_date").isoformat() if item.get("next_date") else None,
        "frequency": item.get("frequency"),
        "status": item.get("status"),
    }


def _serialize_transfer(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": item.get("uid"),
        "amount": _minor_to_major(item.get("amount_minor")),
        "currency": item.get("currency"),
        "direction": item.get("direction"),
        "source": item.get("source"),
        "reference": item.get("reference"),
        "counterparty": item.get("counterparty"),
        "transaction_time": item.get("transaction_time").isoformat() if item.get("transaction_time") else None,
        "matched_by": item.get("matched_by"),
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: StarlingDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]

    features = entry.data.get(CONF_FEATURES, [FEATURE_MAIN, FEATURE_SPACES])
    selected_spaces = entry.options.get(CONF_SPACE_NAMES, entry.data.get(CONF_SPACE_NAMES, []))
    history_limit = entry.options.get(CONF_HISTORY_LIMIT, entry.data.get(CONF_HISTORY_LIMIT, DEFAULT_HISTORY_LIMIT))
    upcoming_limit = entry.options.get(CONF_UPCOMING_LIMIT, entry.data.get(CONF_UPCOMING_LIMIT, DEFAULT_UPCOMING_LIMIT))
    spaces_data = coordinator.data.get("spaces", {})

    entities: list[SensorEntity] = [
        StarlingLastSuccessfulRefreshSensor(coordinator, entry),
        StarlingLastRateLimitAtSensor(coordinator, entry),
        StarlingBackoffUntilSensor(coordinator, entry),
        StarlingRequestCountLastCycleSensor(coordinator, entry),
        StarlingAccountTypeSensor(coordinator, entry),
        StarlingLastApiIssuesSensor(coordinator, entry),
    ]

    if FEATURE_MAIN in features:
        if entry.options.get(CONF_INCLUDE_CLEARED, entry.data.get(CONF_INCLUDE_CLEARED, True)):
            entities.append(StarlingMainBalanceSensor(coordinator, entry, "clearedBalance", "Cleared balance"))
        if entry.options.get(CONF_INCLUDE_EFFECTIVE, entry.data.get(CONF_INCLUDE_EFFECTIVE, True)):
            entities.append(StarlingMainBalanceSensor(coordinator, entry, "effectiveBalance", "Effective balance"))

    if FEATURE_SPACES in features:
        for space_name in selected_spaces:
            if space_name in spaces_data:
                entities.append(StarlingSpaceSensor(coordinator, entry, space_name))

    if FEATURE_SCHEDULED in features:
        entities.extend(
            [
                StarlingScheduledPaymentsCountSensor(coordinator, entry, upcoming_limit),
                StarlingScheduledPaymentsNextDateSensor(coordinator, entry),
                StarlingScheduledPaymentsNextAmountSensor(coordinator, entry),
                StarlingScheduledPaymentsNextPayeeSensor(coordinator, entry),
            ]
        )

    if FEATURE_TRANSFERS in features:
        for space_name in selected_spaces:
            space = spaces_data.get(space_name, {})
            if not space or not space.get("supports_transfers"):
                continue
            entities.extend(
                [
                    StarlingRecurringTransferAmountSensor(coordinator, entry, space_name),
                    StarlingRecurringTransferNextDateSensor(coordinator, entry, space_name),
                    StarlingRecurringTransferFrequencySensor(coordinator, entry, space_name),
                    StarlingTransferHistoryCountSensor(coordinator, entry, space_name, history_limit),
                    StarlingTransferHistoryLatestAmountSensor(coordinator, entry, space_name),
                    StarlingTransferHistoryLatestDateSensor(coordinator, entry, space_name),
                ]
            )

    async_add_entities(entities)


class StarlingBaseEntity(CoordinatorEntity[StarlingDataUpdateCoordinator], SensorEntity):
    _attr_icon = "mdi:currency-gbp"
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "GBP"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        if getattr(self, "_attr_native_unit_of_measurement", None) == "GBP":
            self._attr_native_unit_of_measurement = coordinator.data.get("currency") or coordinator.currency or "GBP"

    @property
    def device_info(self) -> dict[str, Any]:
        account_uid = self.coordinator.data.get("account_uid", self._entry.entry_id)
        account_name = self.coordinator.data.get("account_name", self._entry.title)
        return {
            "identifiers": {(DOMAIN, account_uid)},
            "name": account_name,
            "manufacturer": "Starling Bank",
            "model": "Enhanced API integration",
            "entry_type": DeviceEntryType.SERVICE,
        }


class StarlingDiagnosticsBaseSensor(StarlingBaseEntity):
    _attr_native_unit_of_measurement = None
    _attr_suggested_display_precision = None
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def _diagnostics(self) -> dict[str, Any]:
        return self.coordinator.data.get("diagnostics", {})


class StarlingLastSuccessfulRefreshSensor(StarlingDiagnosticsBaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Last successful refresh"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_last_successful_refresh"

    @property
    def native_value(self) -> datetime | None:
        return self._diagnostics.get("last_successful_refresh")


class StarlingLastRateLimitAtSensor(StarlingDiagnosticsBaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:speedometer-slow"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Last rate limit at"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_last_rate_limit_at"

    @property
    def native_value(self) -> datetime | None:
        return self._diagnostics.get("last_rate_limit_at")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "retry_after": self._diagnostics.get("last_rate_limit_retry_after"),
            "backoff_reason": self._diagnostics.get("backoff_reason"),
        }


class StarlingLastWebhookReceivedSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Last webhook received"
    _attr_icon = "mdi:webhook"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_last_webhook_received"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Starling Bank",
        }

    @property
    def native_value(self):
        runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return runtime.get(CONF_WEBHOOK_LAST_RECEIVED)


class StarlingBackoffUntilSensor(StarlingDiagnosticsBaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Backoff until"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_backoff_until"

    @property
    def native_value(self) -> datetime | None:
        return self._diagnostics.get("backoff_until")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "backoff_reason": self._diagnostics.get("backoff_reason"),
            "next_account_refresh": self._diagnostics.get("next_account_refresh").isoformat()
            if self._diagnostics.get("next_account_refresh")
            else None,
            "next_savings_refresh": self._diagnostics.get("next_savings_refresh").isoformat()
            if self._diagnostics.get("next_savings_refresh")
            else None,
            "next_scheduled_refresh": self._diagnostics.get("next_scheduled_refresh").isoformat()
            if self._diagnostics.get("next_scheduled_refresh")
            else None,
            "next_transfer_history_refresh": self._diagnostics.get("next_transfer_history_refresh").isoformat()
            if self._diagnostics.get("next_transfer_history_refresh")
            else None,
        }


class StarlingRequestCountLastCycleSensor(StarlingDiagnosticsBaseSensor):
    _attr_icon = "mdi:counter"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Request count last cycle"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_request_count_last_cycle"

    @property
    def native_value(self) -> int:
        value = self._diagnostics.get("request_count_last_cycle")
        return int(value or 0)





class StarlingAccountTypeSensor(StarlingDiagnosticsBaseSensor):
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:bank"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Account type"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_account_type"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("account_type")


class StarlingLastApiIssuesSensor(StarlingDiagnosticsBaseSensor):
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "API issues"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_api_issues"

    @property
    def native_value(self) -> str:
        issues = self._diagnostics.get("feature_issues") or {}
        recurring = issues.get("recurring_transfers") if isinstance(issues.get("recurring_transfers"), dict) else {}
        count = int(bool(issues.get("scheduled_payments"))) + int(bool(issues.get("transfer_history"))) + len([v for v in recurring.values() if v])
        return "ok" if count == 0 else f"{count} issue(s)"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        issues = self._diagnostics.get("feature_issues") or {}
        return {
            "scheduled_payments": issues.get("scheduled_payments"),
            "transfer_history": issues.get("transfer_history"),
            "recurring_transfers": issues.get("recurring_transfers"),
        }

class StarlingMainBalanceSensor(StarlingBaseEntity):
    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, balance_key: str, label: str) -> None:
        super().__init__(coordinator, entry)
        self._balance_key = balance_key
        self._attr_name = label
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{balance_key}"

    @property
    def native_value(self) -> float | None:
        balance_data = self.coordinator.data.get("balance", {})
        balance = balance_data.get(self._balance_key, {})
        return _minor_to_major(balance.get("minorUnits"))


class StarlingSpaceSensor(StarlingBaseEntity):
    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")

        space = coordinator.data.get("spaces", {}).get(space_name, {})
        category = space.get("space_category")

        if category == "kite":
            self._attr_name = f"Kite Space {space_name}"
            self._attr_icon = "mdi:account-child-circle"
        elif category == "spending":
            self._attr_name = f"Spending Space {space_name}"
            self._attr_icon = "mdi:wallet-outline"
        else:
            self._attr_name = f"Space {space_name}"
            self._attr_icon = "mdi:piggy-bank"

        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_space_{safe_space}"

    @property
    def available(self) -> bool:
        return super().available and self._space_name in self.coordinator.data.get("spaces", {})

    @property
    def native_value(self) -> float | None:
        space = self.coordinator.data.get("spaces", {}).get(self._space_name)
        if not space:
            return None
        return _minor_to_major(space.get("minor_units"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        space = self.coordinator.data.get("spaces", {}).get(self._space_name, {})
        diagnostics = self.coordinator.data.get("diagnostics", {})
        return {
            "space_category": space.get("space_category"),
            "supports_transfers": space.get("supports_transfers"),
            "savings_goal_uid": space.get("savings_goal_uid"),
            "space_uid": space.get("space_uid"),
            "account_type": self.coordinator.data.get("account_type"),
            "space_filters": diagnostics.get("space_filters"),
        }


class StarlingScheduledPaymentsCountSensor(StarlingBaseEntity):
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, upcoming_limit: int) -> None:
        super().__init__(coordinator, entry)
        self._upcoming_limit = upcoming_limit
        self._attr_name = "Scheduled payments count"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_scheduled_count"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("scheduled_payments", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        items = self.coordinator.data.get("scheduled_payments", [])[: self._upcoming_limit]
        diagnostics = self.coordinator.data.get("diagnostics", {})
        return {
            "next_payments": [_serialize_payment(item) for item in items],
            "issue": (diagnostics.get("feature_issues") or {}).get("scheduled_payments"),
            "account_type": self.coordinator.data.get("account_type"),
        }


class StarlingScheduledPaymentsNextDateSensor(StarlingBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:calendar-arrow-right"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Account next scheduled payment date"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_scheduled_next_date"

    @property
    def native_value(self) -> datetime | None:
        items = self.coordinator.data.get("scheduled_payments", [])
        if not items:
            return None
        if items[0].get("next_datetime"):
            return items[0]["next_datetime"]
        if items[0].get("next_date"):
            return datetime.combine(items[0]["next_date"], datetime.min.time(), tzinfo=UTC)
        return None


class StarlingScheduledPaymentsNextAmountSensor(StarlingBaseEntity):
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Account next scheduled payment amount"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_scheduled_next_amount"

    @property
    def native_value(self) -> float | None:
        items = self.coordinator.data.get("scheduled_payments", [])
        return _minor_to_major(items[0].get("amount_minor")) if items else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        items = self.coordinator.data.get("scheduled_payments", [])
        if not items:
            return {}
        item = items[0]
        diagnostics = self.coordinator.data.get("diagnostics", {})
        return {
            "reference": item.get("reference"),
            "counterparty": item.get("counterparty"),
            "next_date": item.get("next_date").isoformat() if item.get("next_date") else None,
            "frequency": item.get("frequency"),
            "status": item.get("status"),
            "issue": (diagnostics.get("feature_issues") or {}).get("scheduled_payments"),
        }


class StarlingScheduledPaymentsNextPayeeSensor(StarlingBaseEntity):
    _attr_native_unit_of_measurement = None
    _attr_suggested_display_precision = None
    _attr_icon = "mdi:account-cash"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Account next scheduled payment payee"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_scheduled_next_payee"

    @property
    def native_value(self) -> str | None:
        items = self.coordinator.data.get("scheduled_payments", [])
        return items[0].get("counterparty") if items else None


class StarlingRecurringTransferAmountSensor(StarlingBaseEntity):
    _attr_icon = "mdi:cash-sync"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} recurring transfer amount"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_recurring_amount"

    @property
    def native_value(self) -> float | None:
        transfer = self.coordinator.data.get("recurring_transfers", {}).get(self._space_name)
        if not transfer:
            return None
        return _minor_to_major(transfer.get("amount_minor"))


class StarlingRecurringTransferNextDateSensor(StarlingBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:calendar-sync"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} recurring transfer next date"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_recurring_next_date"

    @property
    def native_value(self) -> datetime | None:
        transfer = self.coordinator.data.get("recurring_transfers", {}).get(self._space_name)
        if not transfer:
            return None
        value = transfer.get("next_datetime")
        if isinstance(value, datetime):
            return value
        return None


class StarlingRecurringTransferFrequencySensor(StarlingBaseEntity):
    _attr_native_unit_of_measurement = None
    _attr_suggested_display_precision = None
    _attr_icon = "mdi:repeat"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} recurring transfer frequency"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_recurring_frequency"

    @property
    def native_value(self) -> str | None:
        transfer = self.coordinator.data.get("recurring_transfers", {}).get(self._space_name)
        if not transfer:
            return None
        value = transfer.get("frequency")
        interval = transfer.get("interval")
        if value and interval and interval != 1:
            return f"{value} every {interval}"
        return value


class StarlingTransferHistoryCountSensor(StarlingBaseEntity):
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str, history_limit: int) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        self._history_limit = history_limit
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} transfer history count"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_history_count"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("transfer_history", {}).get(self._space_name, []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        items = self.coordinator.data.get("transfer_history", {}).get(self._space_name, [])[: self._history_limit]
        diagnostics = self.coordinator.data.get("diagnostics", {})
        recurring_issues = ((diagnostics.get("feature_issues") or {}).get("recurring_transfers") or {})
        return {
            "recent_transfers": [_serialize_transfer(item) for item in items],
            "issue": (diagnostics.get("feature_issues") or {}).get("transfer_history"),
            "recurring_transfer_issue": recurring_issues.get(self._space_name),
            "account_type": self.coordinator.data.get("account_type"),
        }


class StarlingTransferHistoryLatestAmountSensor(StarlingBaseEntity):
    _attr_icon = "mdi:cash-fast"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} latest transfer amount"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_history_latest_amount"

    @property
    def native_value(self) -> float | None:
        items = self.coordinator.data.get("transfer_history", {}).get(self._space_name, [])
        return _minor_to_major(items[0].get("amount_minor")) if items else None


class StarlingTransferHistoryLatestDateSensor(StarlingBaseEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:clock-check"

    def __init__(self, coordinator: StarlingDataUpdateCoordinator, entry: ConfigEntry, space_name: str) -> None:
        super().__init__(coordinator, entry)
        self._space_name = space_name
        safe_space = space_name.strip().lower().replace(" ", "_")
        self._attr_name = f"Space {space_name} latest transfer date"
        self._attr_unique_id = f"starling_enhanced_{entry.entry_id}_{safe_space}_history_latest_date"

    @property
    def native_value(self) -> datetime | None:
        items = self.coordinator.data.get("transfer_history", {}).get(self._space_name, [])
        return items[0].get("transaction_time") if items else None
