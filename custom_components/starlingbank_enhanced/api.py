"""Starling Bank Enhanced API helper."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import logging
import time
import asyncio
from typing import Any
from urllib.parse import urlencode

from aiohttp import ClientError, ClientSession

from .const import DEFAULT_BACKOFF_SECONDS, DEFAULT_TRANSFER_LOOKBACK_DAYS

_LOGGER = logging.getLogger(__name__)


class StarlingApiError(Exception):
    """Raised when the Starling API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or ""
        self.retry_after = retry_after


class StarlingApiClient:
    """Thin async client for Starling API."""

    def __init__(self, session: ClientSession, access_token: str, sandbox: bool) -> None:
        self._session = session
        self._access_token = access_token
        self._base_url = (
            "https://api-sandbox.starlingbank.com"
            if sandbox
            else "https://api.starlingbank.com"
        )
        self._request_counter = 0
        self._backoff_until: datetime | None = None
        self._backoff_reason: str | None = None
        self._min_request_interval = 0.25
        self._last_request_started = 0.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

    def get_default_category_uid(self, account: dict[str, Any]) -> str | None:
        return self._default_category_uid_from_account(account)
    
    def _mask_token(self) -> str:
        token = self._access_token or ""
        if len(token) <= 8:
            return "***"
        return f"{token[:4]}...{token[-4:]}"

    def reset_request_counter(self) -> int:
        current = self._request_counter
        self._request_counter = 0
        return current

    def _parse_retry_after_seconds(self, retry_after: str | None) -> int:
        if retry_after:
            retry_after = retry_after.strip()
            if retry_after.isdigit():
                return max(int(retry_after), 1)
            try:
                retry_dt = parsedate_to_datetime(retry_after)
                if retry_dt.tzinfo is None:
                    retry_dt = retry_dt.replace(tzinfo=UTC)
                delta = retry_dt - datetime.now(UTC)
                return max(int(delta.total_seconds()), 1)
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
        return DEFAULT_BACKOFF_SECONDS

    def _set_backoff(self, retry_after: str | None, *, reason: str) -> None:
        seconds = self._parse_retry_after_seconds(retry_after)
        self._backoff_until = datetime.now(UTC) + timedelta(seconds=seconds)
        self._backoff_reason = reason
        _LOGGER.warning(
            "Starling API backoff enabled: seconds=%s until=%s reason=%s",
            seconds,
            self._backoff_until.isoformat(),
            reason,
        )

    def _format_rate_limit_message(self, path: str, retry_after: str | None) -> str:
        seconds = self._parse_retry_after_seconds(retry_after)
        until = datetime.now(UTC) + timedelta(seconds=seconds)
        return (
            f"GET {path} failed: HTTP 429: rate limited, "
            f"retry after {seconds}s (until {until.isoformat()})"
        )

    @property
    def backoff_until(self) -> datetime | None:
        return self._backoff_until

    @property
    def backoff_reason(self) -> str | None:
        return self._backoff_reason

    @staticmethod
    def _default_category_uid_from_account(account: dict[str, Any]) -> str | None:
        for key in (
            "defaultCategory",
            "defaultCategoryUid",
            "categoryUid",
        ):
            value = account.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _raise_if_backing_off(self, path: str) -> None:
        if not self._backoff_until:
            return
        now = datetime.now(UTC)
        if now >= self._backoff_until:
            _LOGGER.debug(
                "Starling API backoff expired: previous_until=%s reason=%s",
                self._backoff_until.isoformat(),
                self._backoff_reason,
            )
            self._backoff_until = None
            self._backoff_reason = None
            return

        retry_after_seconds = max(int((self._backoff_until - now).total_seconds()), 1)
        _LOGGER.warning(
            "Starling API request skipped during backoff: path=%s retry_after_seconds=%s reason=%s",
            path,
            retry_after_seconds,
            self._backoff_reason,
        )
        raise StarlingApiError(
            f"GET {path} skipped during backoff window",
            status=429,
            retry_after=str(retry_after_seconds),
        )

    @staticmethod
    def _extract_error_details(body: str) -> str:
        try:
            payload = json.loads(body)
        except Exception:
            return body[:500]

        error = payload.get("error")
        description = payload.get("error_description")
        if error and description:
            return f"{error}: {description}"
        if description:
            return str(description)
        if error:
            return str(error)
        return body[:500]

    async def _wait_for_client_rate_limit(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_started
        remaining = self._min_request_interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_started = time.monotonic()

    async def async_validate_scheduled_payments_access(self, account_uid: str) -> None:
        account = await self.async_get_account(account_uid)
        category_uid = self._default_category_uid_from_account(account)

        if not category_uid:
            raise StarlingApiError(
                f"Could not determine default categoryUid for account {account_uid}"
            )

        await self._get(
            f"/api/v2/payments/local/account/{account_uid}/category/{category_uid}/standing-orders"
        )

    async def async_get_scheduled_payments_for_category(
        self,
        account_uid: str,
        category_uid: str,
    ) -> list[dict[str, Any]]:
        standing_orders = await self.async_get_standing_orders(account_uid, category_uid)
        scheduled_payments: list[dict[str, Any]] = []

        for order in standing_orders:
            payment_order_uid = str(order.get("paymentOrderUid") or "").strip()
            if not payment_order_uid:
                continue

            if order.get("cancelledAt"):
                continue

            recurrence = order.get("standingOrderRecurrence") or {}
            if not recurrence.get("startDate"):
                continue

            try:
                upcoming = await self.async_get_standing_order_upcoming_payments(
                    account_uid,
                    category_uid,
                    payment_order_uid,
                )
            except StarlingApiError as err:
                if err.status == 404 or "http 404" in str(err).lower():
                    upcoming = []
                else:
                    raise

            if not upcoming:
                next_date = order.get("nextDate")
                if isinstance(next_date, str) and next_date.strip():
                    upcoming = [{"nextDate": next_date}]

            for item in upcoming:
                enriched = dict(item)
                enriched.setdefault("paymentOrderUid", payment_order_uid)
                enriched.setdefault("accountUid", account_uid)
                enriched.setdefault("categoryUid", order.get("categoryUid") or category_uid)
                enriched.setdefault("reference", order.get("reference"))
                enriched.setdefault("payeeUid", order.get("payeeUid"))
                enriched.setdefault("payeeAccountUid", order.get("payeeAccountUid"))
                enriched.setdefault("amount", order.get("amount"))
                enriched.setdefault("standingOrderRecurrence", recurrence)
                enriched.setdefault("updatedAt", order.get("updatedAt"))
                enriched["_source_order"] = order
                scheduled_payments.append(enriched)

        scheduled_payments.sort(key=lambda x: x.get("nextDate") or "")
        return scheduled_payments

    async def _get(self, path: str) -> dict[str, Any]:
        self._raise_if_backing_off(path)
        await self._wait_for_client_rate_limit()
  
        url = f"{self._base_url}{path}"
        started = time.monotonic()
        self._request_counter += 1

        _LOGGER.debug(
            "Starling API request started: method=GET path=%s base_url=%s token=%s request_counter=%s",
            path,
            self._base_url,
            self._mask_token(),
            self._request_counter,
        )

        try:
            async with self._session.get(url, headers=self._headers, timeout=20) as resp:
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                retry_after = resp.headers.get("Retry-After")
                rate_headers = {
                    key: resp.headers.get(key)
                    for key in (
                        "Retry-After",
                        "X-RateLimit-Limit",
                        "X-RateLimit-Remaining",
                        "X-RateLimit-Reset",
                    )
                    if resp.headers.get(key) is not None
                }

                _LOGGER.debug(
                    "Starling API response: method=GET path=%s status=%s elapsed_ms=%s headers=%s",
                    path,
                    resp.status,
                    elapsed_ms,
                    rate_headers,
                )

                if resp.status >= 400:
                    text = await resp.text()
                    details = self._extract_error_details(text)

                    if resp.status == 429:
                        self._set_backoff(retry_after, reason=f"429 from {path}")
                        _LOGGER.warning(
                            "Starling API rate limited: path=%s status=429 retry_after=%s elapsed_ms=%s body=%s",
                            path,
                            retry_after,
                            elapsed_ms,
                            details,
                        )
                    else:
                        log_fn = _LOGGER.debug if resp.status == 404 and any(marker in path for marker in ("/recurring-transfer", "/standing-orders", "/settled-transactions-between")) else _LOGGER.warning
                        log_fn(
                            "Starling API error response: path=%s status=%s elapsed_ms=%s body=%s",
                            path,
                            resp.status,
                            elapsed_ms,
                            details,
                        )

                    if resp.status == 429:
                        message = self._format_rate_limit_message(path, retry_after)
                    else:
                        suffix = f": {details}" if details else ""
                        message = f"GET {path} failed: HTTP {resp.status}{suffix}"

                    raise StarlingApiError(
                        message,
                        status=resp.status,
                        body=text,
                        retry_after=retry_after,
                    )

                data = await resp.json()
                _LOGGER.debug(
                    "Starling API request succeeded: path=%s status=%s elapsed_ms=%s",
                    path,
                    resp.status,
                    elapsed_ms,
                )
                return data
        except ClientError as err:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            _LOGGER.warning(
                "Starling API client error: path=%s elapsed_ms=%s error=%r",
                path,
                elapsed_ms,
                err,
            )
            raise StarlingApiError(f"GET {path} failed: {err}") from err



    @staticmethod
    def classify_space(goal: dict[str, Any]) -> str:
        """Classify a Starling space-like object into savings/spending/kite/unknown."""

        raw_values = " ".join(
            str(goal.get(key) or "")
            for key in (
                "spaceType",
                "type",
                "goalType",
                "category",
                "subType",
                "spaceCategory",
                "spendingType",
                "spendingSpaceType",
                "cardType",
                "ownerType",
                "accessType",
            )
        ).lower()
        name = str(goal.get("name") or "").strip().lower()
        source = str(goal.get("_source_endpoint") or "").lower()
        serialized = str(goal).lower()

        if "kite" in raw_values or "kite" in name or "kite" in serialized:
            return "kite"

        if goal.get("spendingSpace") is True or goal.get("isSpendingSpace") is True:
            return "spending"

        if "spending" in raw_values:
            return "spending"

        if goal.get("savingsGoalUid") or source == "savings_goals":
            return "savings"

        if goal.get("spaceUid"):
            return "spending"

        return "unknown"

    @classmethod
    def should_include_space(
        cls,
        account_type: str | None,
        goal: dict[str, Any],
        *,
        include_savings: bool,
        include_spending: bool,
        include_kite: bool,
    ) -> bool:
        """Decide whether a given space should be surfaced."""
        category = cls.classify_space(goal)

        if account_type == "SAVINGS":
            return category == "savings" and include_savings

        if category == "savings":
            return include_savings
        if category == "spending":
            return include_spending
        if category == "kite":
            return include_kite
        return False

    async def async_get_accounts(self) -> list[dict[str, Any]]:
        data = await self._get("/api/v2/accounts")
        accounts = data.get("accounts", [])
        if not accounts:
            raise StarlingApiError("No accounts returned by /api/v2/accounts")
        return accounts

    async def async_get_primary_account(self) -> dict[str, Any]:
        return (await self.async_get_accounts())[0]

    async def async_get_account(self, account_uid: str) -> dict[str, Any]:
        for account in await self.async_get_accounts():
            if account.get("accountUid") == account_uid:
                return account
        raise StarlingApiError(f"Account {account_uid} not found")

    async def async_get_balance(self, account_uid: str) -> dict[str, Any]:
        return await self._get(f"/api/v2/accounts/{account_uid}/balance")

    @staticmethod
    def _merge_goal_lists(*goal_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        anonymous_index = 0

        for goals in goal_lists:
            for goal in goals or []:
                uid = goal.get("savingsGoalUid") or goal.get("spaceUid")
                name = (goal.get("name") or "").strip()
                key = uid or f"name:{name}" or f"anonymous:{anonymous_index}"
                if key not in merged:
                    merged[key] = {}
                    order.append(key)
                merged[key].update(goal)
                if not uid and not name:
                    anonymous_index += 1

        return [merged[key] for key in order]


    async def async_get_standing_orders(
        self,
        account_uid: str,
        category_uid: str,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/api/v2/payments/local/account/{account_uid}/category/{category_uid}/standing-orders"
        )

        if isinstance(data, dict):
            for key in ("standingOrders", "paymentOrders", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]

        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]

        return []


    async def async_get_standing_order_upcoming_payments(
        self,
        account_uid: str,
        category_uid: str,
        payment_order_uid: str,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/api/v2/payments/local/account/{account_uid}/category/{category_uid}/standing-orders/{payment_order_uid}/upcoming-payments"
        )

        if isinstance(data, dict):
            value = data.get("nextPaymentDates")
            if isinstance(value, list):
                return [
                    {"nextDate": item}
                    for item in value
                    if isinstance(item, str) and item.strip()
                ]

        return []


    async def async_get_scheduled_payments(self, account_uid: str) -> list[dict[str, Any]]:
        account = await self.async_get_account(account_uid)
        category_uid = self._default_category_uid_from_account(account)

        if not category_uid:
            raise StarlingApiError(
                f"Could not determine default categoryUid for account {account_uid}"
            )

        return await self.async_get_scheduled_payments_for_category(
            account_uid,
            category_uid,
        )

    async def async_get_savings_goals(self, account_uid: str) -> list[dict[str, Any]]:
        savings_goals: list[dict[str, Any]] = []
        spaces_goals: list[dict[str, Any]] = []
        savings_error: StarlingApiError | None = None
        spaces_error: StarlingApiError | None = None

        try:
            data = await self._get(f"/api/v2/account/{account_uid}/savings-goals")
            value = data.get("savingsGoalList")
            if isinstance(value, list):
                savings_goals = [
                    {**item, "_source_endpoint": "savings_goals"}
                    for item in value
                    if isinstance(item, dict)
                ]
        except StarlingApiError as err:
            savings_error = err

        spending_refs: list[dict[str, Any]] = []
        try:
            data = await self._get(f"/api/v2/account/{account_uid}/spaces")
            values: list[dict[str, Any]] = []
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, list) and ("space" in key.lower() or "goal" in key.lower()):
                        values.extend(item for item in value if isinstance(item, dict))
            elif isinstance(data, list):
                values.extend(item for item in data if isinstance(item, dict))

            for item in values:
                source_item = {**item, "_source_endpoint": "spaces"}
                if item.get("savingsGoalUid"):
                    spaces_goals.append(source_item)
                elif item.get("spaceUid"):
                    spending_refs.append(source_item)
        except StarlingApiError as err:
            spaces_error = err

        for ref in spending_refs:
            space_uid = ref.get("spaceUid")
            if not space_uid:
                continue
            try:
                detail = await self.async_get_spending_space(account_uid, space_uid)
            except StarlingApiError as err:
                _LOGGER.debug(
                    "Could not hydrate spending space detail: account_uid=%s space_uid=%s error=%s",
                    account_uid,
                    space_uid,
                    err,
                )
                detail = None
            merged_item = {
                **ref,
                **(detail or {}),
                "_source_endpoint": "spaces_spending_detail" if detail else "spaces",
            }
            spaces_goals.append(merged_item)

        merged = self._merge_goal_lists(savings_goals, spaces_goals)
        if merged:
            return merged

        if savings_error and spaces_error:
            raise savings_error
        if savings_error:
            raise savings_error
        if spaces_error:
            raise spaces_error
        return []

    async def async_get_spending_space(self, account_uid: str, space_uid: str) -> dict[str, Any] | None:
        try:
            data = await self._get(f"/api/v2/account/{account_uid}/spaces/spending/{space_uid}")
        except StarlingApiError as err:
            if err.status == 404 or "http 404" in str(err).lower():
                return None
            raise
        return data or None

    async def async_get_recurring_transfer(self, account_uid: str, savings_goal_uid: str) -> dict[str, Any] | None:
        try:
            data = await self._get(
                f"/api/v2/account/{account_uid}/savings-goals/{savings_goal_uid}/recurring-transfer"
            )
        except StarlingApiError as err:
            if err.status == 404 or "http 404" in str(err).lower():
                return None
            raise
        return data or None
    


   

    async def async_get_settled_feed_items(self, account_uid: str, category_uid: str, *, days: int = DEFAULT_TRANSFER_LOOKBACK_DAYS) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        start = now - timedelta(days=max(days, 1))
        query = urlencode({
            "minTransactionTimestamp": start.isoformat().replace("+00:00", "Z"),
            "maxTransactionTimestamp": now.isoformat().replace("+00:00", "Z"),
        })
        data = await self._get(
            f"/api/v2/feed/account/{account_uid}/settled-transactions-between?{query}"
        )
        return data.get("feedItems", [])
