"""Data update coordinator for Starling Bank Enhanced."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import StarlingApiClient, StarlingApiError
from .const import (
    CONF_INCLUDE_KITE_SPACES,
    CONF_INCLUDE_SAVINGS_SPACES,
    CONF_INCLUDE_SPENDING_SPACES,
    DEFAULT_ACCOUNT_REFRESH_INTERVAL,
    DEFAULT_INCLUDE_KITE_SPACES,
    DEFAULT_INCLUDE_SAVINGS_SPACES,
    DEFAULT_INCLUDE_SPENDING_SPACES,
    DEFAULT_SAVINGS_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SCHEDULED_REFRESH_INTERVAL,
    DEFAULT_TRANSFER_HISTORY_REFRESH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

def _is_auth_failure(err: StarlingApiError) -> bool:
    return err.status in (401, 403)

class StarlingDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate Starling API polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: StarlingApiClient,
        name: str,
        *,
        account_uid: str | None = None,
        include_scheduled: bool = False,
        include_transfers: bool = False,
        selected_spaces: list[str] | None = None,
        include_savings_spaces: bool = DEFAULT_INCLUDE_SAVINGS_SPACES,
        include_spending_spaces: bool = DEFAULT_INCLUDE_SPENDING_SPACES,
        include_kite_spaces: bool = DEFAULT_INCLUDE_KITE_SPACES,
    ) -> None:
        super().__init__(hass, _LOGGER, name=name, update_interval=DEFAULT_SCAN_INTERVAL)
        self.api = api
        self.include_scheduled = include_scheduled
        self.include_transfers = include_transfers
        self.configured_account_uid = account_uid
        self.selected_spaces = {name.strip() for name in (selected_spaces or []) if name and name.strip()}
        self.include_savings_spaces = include_savings_spaces
        self.include_spending_spaces = include_spending_spaces
        self.include_kite_spaces = include_kite_spaces

        self.account_uid: str | None = account_uid
        self.account_name: str = name
        self.currency: str = "GBP"
        self.default_category: str | None = None
        self.account_type: str | None = None

        self._feature_issues: dict[str, Any] = {
            "scheduled_payments": None,
            "transfer_history": None,
            "recurring_transfers": {},
        }

        self._cached_account: dict[str, Any] | None = None
        self._cached_spaces: dict[str, dict[str, Any]] = {}
        self._cached_scheduled_payments: list[dict[str, Any]] = []
        self._cached_recurring_transfers: dict[str, dict[str, Any]] = {}
        self._cached_transfer_history: dict[str, list[dict[str, Any]]] = {}
        self._last_successful_data: dict[str, Any] | None = None

        self._next_account_refresh: datetime | None = None
        self._next_savings_refresh: datetime | None = None
        self._next_scheduled_refresh: datetime | None = None
        self._next_transfer_history_refresh: datetime | None = None

        self._last_successful_refresh: datetime | None = None
        self._last_rate_limit_at: datetime | None = None
        self._last_rate_limit_retry_after: str | None = None
        self._request_count_last_cycle: int = 0

    def _feature_flags_for_log(self) -> dict[str, bool]:
        return {
            "include_scheduled": self.include_scheduled,
            "include_transfers": self.include_transfers,
            "selected_spaces": sorted(self.selected_spaces),
            "include_savings_spaces": self.include_savings_spaces,
            "include_spending_spaces": self.include_spending_spaces,
            "include_kite_spaces": self.include_kite_spaces,
        }

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _parse_minor_units(value: Any) -> int | None:
        if isinstance(value, int):
            return value
        if isinstance(value, dict):
            maybe = value.get("minorUnits")
            if isinstance(maybe, int):
                return maybe
        return None

    @staticmethod
    def _parse_date_value(value: Any) -> date | None:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_datetime_value(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_next_date(item: dict[str, Any]) -> date | None:
        for key in ("nextDate", "next_date"):
            parsed = StarlingDataUpdateCoordinator._parse_date_value(item.get(key))
            if parsed:
                return parsed
        return None

    @staticmethod
    def _parse_next_datetime(item: dict[str, Any]) -> datetime | None:
        for key in ("nextPaymentDateTime", "next_datetime"):
            parsed = StarlingDataUpdateCoordinator._parse_datetime_value(item.get(key))
            if parsed:
                return parsed
        parsed_date = StarlingDataUpdateCoordinator._parse_next_date(item)
        if parsed_date:
            return datetime.combine(parsed_date, time.min, tzinfo=UTC)
        return None

    @staticmethod
    def _advance_recurrence(current: date, frequency: str | None, interval: int | None) -> date | None:
        step = max(interval or 1, 1)
        if frequency == "DAILY":
            return current + timedelta(days=step)
        if frequency == "WEEKLY":
            return current + timedelta(weeks=step)
        if frequency == "MONTHLY":
            month = current.month - 1 + step
            year = current.year + month // 12
            month = month % 12 + 1
            day = min(current.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
            return date(year, month, day)
        if frequency == "YEARLY":
            try:
                return current.replace(year=current.year + step)
            except ValueError:
                return current.replace(month=2, day=28, year=current.year + step)
        return None

    @classmethod
    def _calculate_next_date(cls, start_date: date | None, frequency: str | None, interval: int | None) -> date | None:
        if not start_date or not frequency:
            return None
        today = datetime.now(UTC).date()
        next_date = start_date
        limit = 366
        while next_date and next_date < today and limit > 0:
            next_date = cls._advance_recurrence(next_date, frequency, interval)
            limit -= 1
        return next_date

    @classmethod
    def _normalize_recurring_transfer(cls, item: dict[str, Any]) -> dict[str, Any]:
        amount = item.get("currencyAndAmount") if isinstance(item.get("currencyAndAmount"), dict) else {}
        recurrence = item.get("recurrenceRule") if isinstance(item.get("recurrenceRule"), dict) else {}

        start_date = cls._parse_date_value(recurrence.get("startDate") or item.get("startDate"))
        next_date = cls._parse_date_value(item.get("nextDate") or recurrence.get("nextDate"))
        frequency = recurrence.get("frequency") or item.get("frequency")
        interval = recurrence.get("interval")

        if next_date is None:
            next_date = cls._calculate_next_date(start_date, frequency, interval)

        return {
            "amount_minor": cls._parse_minor_units(amount),
            "currency": amount.get("currency") or item.get("currency") or "GBP",
            "frequency": frequency,
            "interval": interval,
            "start_date": start_date,
            "next_date": next_date,
            "next_datetime": datetime.combine(next_date, time.min, tzinfo=UTC) if next_date else None,
            "raw": item,
        }

    @classmethod
    def _normalize_scheduled_payment(cls, item: dict[str, Any]) -> dict[str, Any]:
        next_payment_amount = item.get("nextPaymentAmount") if isinstance(item.get("nextPaymentAmount"), dict) else {}
        payment_amount = item.get("paymentAmount") if isinstance(item.get("paymentAmount"), dict) else {}
        amount_dict = item.get("amount") if isinstance(item.get("amount"), dict) else {}
        recurrence_rule = item.get("recurrenceRule") if isinstance(item.get("recurrenceRule"), dict) else {}
        amount_minor = (
            cls._parse_minor_units(next_payment_amount)
            or cls._parse_minor_units(payment_amount)
            or cls._parse_minor_units(amount_dict)
            or item.get("minorUnits")
            or item.get("amount_minor")
            or 0
        )
        next_date = cls._parse_date_value(item.get("nextDate") or recurrence_rule.get("nextDate") or item.get("startDate"))
        next_datetime = cls._parse_datetime_value(item.get("nextPaymentDateTime"))
        if next_datetime is None and next_date is not None:
            next_datetime = datetime.combine(next_date, time.min, tzinfo=UTC)
        return {
            "uid": item.get("paymentOrderUid") or item.get("uid"),
            "amount_minor": amount_minor,
            "currency": next_payment_amount.get("currency") or payment_amount.get("currency") or amount_dict.get("currency") or item.get("currency") or "GBP",
            "reference": item.get("reference") or item.get("paymentReference"),
            "counterparty": (
                item.get("recipientName")
                or item.get("counterpartyName")
                or item.get("payeeName")
                or item.get("reference")
                or item.get("payeeUid")
                or item.get("payeeAccountUid")
            ),
            "next_date": next_date,
            "next_datetime": next_datetime,
            "frequency": recurrence_rule.get("frequency") or item.get("frequency"),
            "status": (
                item.get("status")
                or item.get("state")
                or item.get("paymentType")
                or ("cancelled" if item.get("cancelledAt") else "scheduled")
            ),
            "raw": item,
        }

    @classmethod
    def _normalize_transfer_history_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        amount = item.get("amount") if isinstance(item.get("amount"), dict) else {}
        settled = item.get("transactionTime") or item.get("settlementTime")
        transaction_time = cls._parse_datetime_value(settled)
        return {
            "uid": item.get("feedItemUid") or item.get("uid"),
            "amount_minor": amount.get("minorUnits") if isinstance(amount, dict) else None,
            "currency": amount.get("currency") if isinstance(amount, dict) else None,
            "direction": item.get("direction"),
            "source": item.get("source"),
            "reference": item.get("reference"),
            "counterparty": item.get("counterPartyName"),
            "transaction_time": transaction_time,
            "matched_by": None,
            "raw": item,
        }

    @staticmethod
    def _should_refresh(next_refresh: datetime | None, now: datetime) -> bool:
        return next_refresh is None or now >= next_refresh

    @staticmethod
    def _next_refresh(now: datetime, interval: timedelta) -> datetime:
        return now + interval

    def _build_payload(self, balance: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "account_uid": self.account_uid,
            "account_name": self.account_name,
            "currency": self.currency,
            "default_category": self.default_category,
            "account_type": self.account_type,
            "balance": balance,
            "spaces": self._cached_spaces,
            "scheduled_payments": self._cached_scheduled_payments,
            "recurring_transfers": self._cached_recurring_transfers,
            "transfer_history": self._cached_transfer_history,
            "diagnostics": {
                "last_successful_refresh": self._last_successful_refresh,
                "last_rate_limit_at": self._last_rate_limit_at,
                "last_rate_limit_retry_after": self._last_rate_limit_retry_after,
                "backoff_until": self.api.backoff_until,
                "backoff_reason": self.api.backoff_reason,
                "request_count_last_cycle": self._request_count_last_cycle,
                "account_type": self.account_type,
                "feature_issues": self._feature_issues,
                "space_filters": {
                    CONF_INCLUDE_SAVINGS_SPACES: self.include_savings_spaces,
                    CONF_INCLUDE_SPENDING_SPACES: self.include_spending_spaces,
                    CONF_INCLUDE_KITE_SPACES: self.include_kite_spaces,
                },
                "next_account_refresh": self._next_account_refresh,
                "next_savings_refresh": self._next_savings_refresh,
                "next_scheduled_refresh": self._next_scheduled_refresh,
                "next_transfer_history_refresh": self._next_transfer_history_refresh,
            },
        }
        self._last_successful_data = payload
        return payload

    def _log_cache_plan(self, now: datetime) -> None:
        _LOGGER.debug(
            "Starling refresh cache plan: now=%s account_due=%s savings_due=%s scheduled_due=%s transfer_history_due=%s has_cached_account=%s has_cached_payload=%s",
            now.isoformat(),
            self._should_refresh(self._next_account_refresh, now),
            self._should_refresh(self._next_savings_refresh, now),
            self._should_refresh(self._next_scheduled_refresh, now),
            self._should_refresh(self._next_transfer_history_refresh, now),
            self._cached_account is not None,
            self._last_successful_data is not None,
        )

    def _record_rate_limit(self, err: StarlingApiError) -> None:
        self._last_rate_limit_at = self._utcnow()
        self._last_rate_limit_retry_after = err.retry_after

    async def _refresh_account_metadata(self, now: datetime) -> None:
        if self._cached_account and not self._should_refresh(self._next_account_refresh, now):
            return

        account = await self.api.async_get_account(self.configured_account_uid) if self.configured_account_uid else await self.api.async_get_primary_account()
        account_uid = account.get("accountUid")
        if not account_uid:
            raise UpdateFailed("Starling API did not return accountUid")

        self._cached_account = account
        self.account_uid = account_uid
        self.account_name = account.get("name") or self.account_name
        self.account_type = account.get("accountType")
        self.default_category = account.get("defaultCategory")
        self.currency = account.get("currency") or account.get("accountCurrency") or self.currency or "GBP"
        self._next_account_refresh = self._next_refresh(now, DEFAULT_ACCOUNT_REFRESH_INTERVAL)

    async def _refresh_spaces_and_transfers(self, now: datetime) -> None:
        if not self.account_uid:
            raise UpdateFailed("Starling account UID is not available")
        if not self._should_refresh(self._next_savings_refresh, now):
            return

        savings_goals = await self.api.async_get_savings_goals(self.account_uid)
        spaces: dict[str, dict[str, Any]] = {}
        recurring_transfers: dict[str, dict[str, Any]] = {}
        recurring_issues: dict[str, str] = {}

        for goal in savings_goals:
            goal_name = (goal.get("name") or "").strip()
            if not goal_name:
                continue

            if not self.api.should_include_space(
                self.account_type,
                goal,
                include_savings=self.include_savings_spaces,
                include_spending=self.include_spending_spaces,
                include_kite=self.include_kite_spaces,
            ):
                continue

            if self.selected_spaces and goal_name not in self.selected_spaces:
                continue

            goal_uid = goal.get("savingsGoalUid")
            total_saved = goal.get("totalSaved", {}) or {}
            spending_balance = goal.get("balance", {}) or {}
            balance_payload = total_saved if goal.get("savingsGoalUid") else spending_balance
            minor_units = balance_payload.get("minorUnits", 0)
            space_category = self.api.classify_space(goal)
            supports_transfers = space_category == "savings" and bool(goal_uid)

            recurring_transfer: dict[str, Any] | None = None
            should_fetch_recurring = bool(self.include_transfers and supports_transfers)

            if should_fetch_recurring:
                try:
                    raw_transfer = await self.api.async_get_recurring_transfer(self.account_uid, goal_uid)
                    if raw_transfer:
                        recurring_transfer = self._normalize_recurring_transfer(raw_transfer)
                        recurring_transfers[goal_name] = recurring_transfer
                except StarlingApiError as err:
                    if err.status == 429:
                        recurring_issues[goal_name] = str(err)
                        _LOGGER.debug(
                            "Recurring transfer refresh rate-limited: account_uid=%s space=%s goal_uid=%s error=%s",
                            self.account_uid,
                            goal_name,
                            goal_uid,
                            err,
                        )
                        break
                    recurring_issues[goal_name] = str(err)
                    _LOGGER.debug(
                        "Could not load recurring transfer: account_uid=%s space=%s goal_uid=%s error=%s",
                        self.account_uid,
                        goal_name,
                        goal_uid,
                        err,
                    )
                except Exception as err:
                    recurring_issues[goal_name] = str(err)
                    _LOGGER.debug(
                        "Could not load recurring transfer: account_uid=%s space=%s goal_uid=%s error=%s",
                        self.account_uid,
                        goal_name,
                        goal_uid,
                        err,
                    )
            elif self.include_transfers and space_category != "savings":
                recurring_issues[goal_name] = (
                    "Recurring transfer is only available for savings spaces."
                )

            spaces[goal_name] = {
                "name": goal_name,
                "savings_goal_uid": goal_uid,
                "space_uid": goal.get("spaceUid"),
                "minor_units": minor_units,
                "raw": goal,
                "space_category": space_category,
                "supports_transfers": supports_transfers,
                "recurring_transfer": recurring_transfer,
            }

        self._cached_spaces = spaces
        self._cached_recurring_transfers = recurring_transfers
        self._feature_issues["recurring_transfers"] = recurring_issues
        self._next_savings_refresh = self._next_refresh(now, DEFAULT_SAVINGS_REFRESH_INTERVAL)

    async def _refresh_scheduled_payments(self, now: datetime) -> None:
        if not self.include_scheduled:
            self._cached_scheduled_payments = []
            self._feature_issues["scheduled_payments"] = None
            return
        if not self.account_uid:
            raise UpdateFailed("Starling account UID is not available")
        if not self._should_refresh(self._next_scheduled_refresh, now):
            return

        try:
            if not self._cached_account:
                raise UpdateFailed("Starling account metadata is not available")

            category_uid = self.api.get_default_category_uid(self._cached_account)
            if not category_uid:
                raise UpdateFailed(
                    f"Could not determine default categoryUid for account {self.account_uid}"
                )

            raw_payments = await self.api.async_get_scheduled_payments_for_category(
                self.account_uid,
                category_uid,
            )
        except StarlingApiError as err:
            if err.status == 404:
                self._cached_scheduled_payments = []
                self._feature_issues["scheduled_payments"] = str(err)
                self._next_scheduled_refresh = self._next_refresh(now, DEFAULT_SCHEDULED_REFRESH_INTERVAL)
                return
            raise

        self._feature_issues["scheduled_payments"] = None
        scheduled_payments = [self._normalize_scheduled_payment(item) for item in (raw_payments or [])]
        scheduled_payments = [item for item in scheduled_payments if item.get("next_date") or item.get("next_datetime")]
        scheduled_payments.sort(key=lambda item: item.get("next_datetime") or datetime.max.replace(tzinfo=UTC))
        self._cached_scheduled_payments = scheduled_payments
        self._next_scheduled_refresh = self._next_refresh(now, DEFAULT_SCHEDULED_REFRESH_INTERVAL)

    async def _refresh_transfer_history(self, now: datetime) -> None:
        if not self.include_transfers:
            self._cached_transfer_history = {}
            self._feature_issues["transfer_history"] = None
            return
        if not self.account_uid or not self.default_category:
            self._cached_transfer_history = {}
            self._feature_issues["transfer_history"] = "Account category is missing, so transfer history cannot be resolved."
            return
        if self.account_type == "SAVINGS":
            self._cached_transfer_history = {}
            self._feature_issues["transfer_history"] = "Selected SAVINGS account does not provide savings-goal transfer history mapping in this integration."
            self._next_transfer_history_refresh = self._next_refresh(now, DEFAULT_TRANSFER_HISTORY_REFRESH_INTERVAL)
            return
        if not self._should_refresh(self._next_transfer_history_refresh, now):
            return

        try:
            feed_items = await self.api.async_get_settled_feed_items(self.account_uid, self.default_category)
        except StarlingApiError as err:
            if err.status == 404:
                self._cached_transfer_history = {space_name: [] for space_name in self._cached_spaces.keys()}
                self._feature_issues["transfer_history"] = "unsupported"
                self._next_transfer_history_refresh = self._next_refresh(now, DEFAULT_TRANSFER_HISTORY_REFRESH_INTERVAL)
                return
            raise

        normalized_feed = [self._normalize_transfer_history_item(item) for item in feed_items]
        transfer_history: dict[str, list[dict[str, Any]]] = {}

        for space_name, space_data in self._cached_spaces.items():
            if not space_data.get("supports_transfers"):
                transfer_history[space_name] = []
                continue

            goal_uid = space_data.get("savings_goal_uid")
            matched: list[dict[str, Any]] = []

            for item in normalized_feed:
                raw = item.get("raw", {})
                raw_text = str(raw)
                if goal_uid and goal_uid in raw_text:
                    copy = dict(item)
                    copy["matched_by"] = "savings_goal_uid"
                    matched.append(copy)
                    continue

                haystack = " ".join([
                    str(raw.get("title") or ""),
                    str(raw.get("reference") or ""),
                    str(raw.get("counterPartyName") or ""),
                    str(raw.get("counterPartySubEntityName") or ""),
                    str(raw.get("userNote") or ""),
                ]).lower()
                if space_name.lower() in haystack:
                    copy = dict(item)
                    copy["matched_by"] = "name/reference_best_effort"
                    matched.append(copy)

            matched.sort(key=lambda item: item.get("transaction_time") or datetime.min.replace(tzinfo=UTC), reverse=True)
            transfer_history[space_name] = matched

        self._cached_transfer_history = transfer_history
        self._feature_issues["transfer_history"] = None
        self._next_transfer_history_refresh = self._next_refresh(now, DEFAULT_TRANSFER_HISTORY_REFRESH_INTERVAL)

    async def _async_update_data(self) -> dict[str, Any]:
        self.api.reset_request_counter()
        now = self._utcnow()
        _LOGGER.debug("Starting Starling refresh: account_name=%s flags=%s", self.account_name, self._feature_flags_for_log())
        self._log_cache_plan(now)

        try:
            await self._refresh_account_metadata(now)
        except StarlingApiError as err:
            if _is_auth_failure(err):
                raise ConfigEntryAuthFailed("Starling credentials expired or were revoked") from err
            if err.status == 429 and self._last_successful_data:
                self._record_rate_limit(err)
                return self._last_successful_data
            if err.status == 429:
                self._record_rate_limit(err)
            raise UpdateFailed(f"Could not fetch Starling account: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Could not fetch Starling account: {err}") from err

        if not self.account_uid:
            raise UpdateFailed("Starling API did not return accountUid")

        try:
            balance = await self.api.async_get_balance(self.account_uid)
        except StarlingApiError as err:
            if _is_auth_failure(err):
                raise ConfigEntryAuthFailed("Starling credentials expired or were revoked") from err
            if err.status == 429 and self._last_successful_data:
                self._record_rate_limit(err)
                return self._last_successful_data
            if err.status == 429:
                self._record_rate_limit(err)
            raise UpdateFailed(f"Could not fetch Starling balance: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Could not fetch Starling balance: {err}") from err

        try:
            await self._refresh_spaces_and_transfers(now)
        except StarlingApiError as err:
            if err.status == 429:
                self._record_rate_limit(err)
            else:
                _LOGGER.warning("Could not load savings goals: account_uid=%s error=%s", self.account_uid, err)
        except Exception as err:
            _LOGGER.warning("Could not load savings goals: account_uid=%s error=%s", self.account_uid, err)

        try:
            await self._refresh_scheduled_payments(now)
        except StarlingApiError as err:
            if err.status == 429:
                self._record_rate_limit(err)
            else:
                if err.status == 404:
                    self._feature_issues["scheduled_payments"] = "unsupported"
                else:
                    _LOGGER.warning("Could not load scheduled payments: account_uid=%s error=%s", self.account_uid, err)
        except Exception as err:
            _LOGGER.warning("Could not load scheduled payments: account_uid=%s error=%s", self.account_uid, err)

        try:
            await self._refresh_transfer_history(now)
        except StarlingApiError as err:
            if err.status == 429:
                self._record_rate_limit(err)
            else:
                if err.status == 404:
                    self._feature_issues["transfer_history"] = "unsupported"
                else:
                    _LOGGER.warning("Could not load transfer history feed: account_uid=%s category_uid=%s error=%s", self.account_uid, self.default_category, err)
        except Exception as err:
            _LOGGER.warning("Could not load transfer history feed: account_uid=%s category_uid=%s error=%s", self.account_uid, self.default_category, err)

        self._request_count_last_cycle = self.api.reset_request_counter()
        self._last_successful_refresh = self._utcnow()
        return self._build_payload(balance)
