"""Config flow for Starling Bank Enhanced."""
from __future__ import annotations

from typing import Any
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import StarlingApiClient, StarlingApiError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_UID,
    CONF_FEATURES,
    CONF_HISTORY_LIMIT,
    CONF_INCLUDE_CLEARED,
    CONF_INCLUDE_EFFECTIVE,
    CONF_INCLUDE_KITE_SPACES,
    CONF_INCLUDE_SAVINGS_SPACES,
    CONF_INCLUDE_SPENDING_SPACES,
    CONF_SHOW_SAVINGS_ACCOUNT_SPACES,
    CONF_SANDBOX,
    CONF_SPACE_NAMES,
    CONF_UPCOMING_LIMIT,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_INCLUDE_KITE_SPACES,
    DEFAULT_INCLUDE_SAVINGS_SPACES,
    DEFAULT_INCLUDE_SPENDING_SPACES,
    DEFAULT_NAME,
    DEFAULT_SHOW_SAVINGS_ACCOUNT_SPACES,
    DEFAULT_UPCOMING_LIMIT,
    DOMAIN,
    FEATURE_LABELS,
    FEATURE_MAIN,
    FEATURE_PERMISSIONS,
    FEATURE_SCHEDULED,
    FEATURE_SPACES,
    FEATURE_TRANSFERS,
)

_LOGGER = logging.getLogger(__name__)


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def _feature_selector() -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[{"value": key, "label": label} for key, label in FEATURE_LABELS.items()],
            multiple=True,
            mode=SelectSelectorMode.LIST,
        )
    )


def _space_selector(space_options: list[dict[str, str]]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=space_options, multiple=True, mode=SelectSelectorMode.DROPDOWN)
    )


def _account_selector(accounts: list[dict[str, Any]]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                {
                    "value": account.get("accountUid"),
                    "label": f"{account.get('name') or account.get('accountType') or 'Account'} ({account.get('accountType') or 'UNKNOWN'})",
                }
                for account in accounts
                if account.get("accountUid")
            ],
            multiple=False,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def _int_selector() -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=1, max=25, step=1, mode=NumberSelectorMode.BOX))


def _is_auth_error(err: Exception) -> bool:
    text = str(err).lower()
    return any(part in text for part in ("401", "403", "forbidden", "unauthorized", "scope", "permission", "insufficient_scope"))


def _category_label(category: str) -> str:
    return {
        "savings": "Savings",
        "spending": "Spending",
        "kite": "Kite",
    }.get(category, "Unknown")




def _allow_savings_account_space_entities(account_type: str | None, filtered_space_names: list[str], current_value: bool | None = None) -> tuple[bool, bool]:
    if account_type != "SAVINGS":
        return False, True if current_value is None else current_value

    offer_checkbox = len(filtered_space_names) > 1
    default_value = bool(current_value) if current_value is not None else DEFAULT_SHOW_SAVINGS_ACCOUNT_SPACES
    return offer_checkbox, default_value

class StarlingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._token = ""
        self._sandbox = False
        self._account_name = DEFAULT_NAME
        self._account_type: str | None = None
        self._space_catalog: dict[str, dict[str, Any]] = {}
        self._features: list[str] = []
        self._required_permissions: list[str] = []
        self._validation_details = ""
        self._accounts: list[dict[str, Any]] = []
        self._account_uid: str | None = None

    def _set_spaces_from_goals(self, goals: list[dict[str, Any]]) -> None:
        catalog: dict[str, dict[str, Any]] = {}
        for goal in goals:
            name = (goal.get("name") or "").strip()
            if not name:
                continue
            category = StarlingApiClient.classify_space(goal)
            catalog[name] = {
                "name": name,
                "category": category,
                "supports_transfers": category == "savings" and bool(goal.get("savingsGoalUid")),
            }
        self._space_catalog = dict(sorted(catalog.items(), key=lambda item: item[0].lower()))

    def _filtered_space_names(
        self,
        *,
        include_savings: bool,
        include_spending: bool,
        include_kite: bool,
    ) -> list[str]:
        names: list[str] = []
        for name, meta in self._space_catalog.items():
            category = meta.get("category")
            if category == "savings" and include_savings:
                names.append(name)
            elif category == "spending" and include_spending:
                names.append(name)
            elif category == "kite" and include_kite:
                names.append(name)
        return names

    def _space_options(
        self,
        *,
        include_savings: bool,
        include_spending: bool,
        include_kite: bool,
    ) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for name in self._filtered_space_names(
            include_savings=include_savings,
            include_spending=include_spending,
            include_kite=include_kite,
        ):
            category = self._space_catalog.get(name, {}).get("category", "unknown")
            options.append({"value": name, "label": f"{name} [{_category_label(category)}]"})
        return options

    async def _validate_selected_features(self, api: StarlingApiClient, account_uid: str) -> tuple[list[str], list[str]]:
        missing_permissions: list[str] = []
        notes: list[str] = []
        savings_goals: list[dict[str, Any]] = []

        if FEATURE_MAIN in self._features:
            try:
                await api.async_get_balance(account_uid)
            except Exception as err:
                if _is_auth_error(err):
                    missing_permissions.append("balance:read")
                else:
                    notes.append(f"Main balance check returned: {err}")

        if FEATURE_SPACES in self._features or FEATURE_TRANSFERS in self._features:
            try:
                savings_goals = await api.async_get_savings_goals(account_uid)
                self._set_spaces_from_goals(savings_goals)
            except Exception as err:
                if _is_auth_error(err):
                    missing_permissions.extend(["savings-goal:read", "space:read"])
                else:
                    notes.append(f"Spaces check returned: {err}")
                    self._space_catalog = {}

        if FEATURE_SCHEDULED in self._features:
            try:
                await api.async_get_scheduled_payments(account_uid)
            except Exception as err:
                if _is_auth_error(err):
                    missing_permissions.append("scheduled-payment:read")
                else:
                    notes.append(f"Scheduled payments check returned non-blocking response: {err}")

        if FEATURE_TRANSFERS in self._features:
            try:
                first_goal_name = next(
                    (
                        name
                        for name, meta in self._space_catalog.items()
                        if meta.get("supports_transfers")
                    ),
                    None,
                )
                first_goal = next((goal for goal in savings_goals if (goal.get("name") or "").strip() == first_goal_name), None)
                if first_goal is None or not first_goal.get("savingsGoalUid"):
                    notes.append("Savings goal transfers check skipped: no savingsGoalUid available.")
                else:
                    await api.async_get_recurring_transfer(account_uid, first_goal["savingsGoalUid"])
            except Exception as err:
                if _is_auth_error(err):
                    missing_permissions.append("savings-goal-transfer:read")
                else:
                    notes.append(f"Savings goal transfers check returned non-blocking response: {err}")

        return sorted(set(missing_permissions)), notes

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._features = list(user_input.get(CONF_FEATURES, []))
            if FEATURE_TRANSFERS in self._features and FEATURE_SPACES not in self._features:
                self._features.append(FEATURE_SPACES)
            if not self._features:
                errors["base"] = "no_features_selected"
            else:
                permissions: set[str] = set()
                for feature in self._features:
                    permissions.update(FEATURE_PERMISSIONS.get(feature, []))
                self._required_permissions = sorted(permissions)
                return await self.async_step_token()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_FEATURES, default=[FEATURE_MAIN, FEATURE_SPACES]): _feature_selector()}),
            errors=errors,
        )

    async def async_step_token(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._token = user_input[CONF_ACCESS_TOKEN].strip()
            self._sandbox = user_input.get(CONF_SANDBOX, False)
            session = async_get_clientsession(self.hass)
            api = StarlingApiClient(session, self._token, self._sandbox)
            try:
                self._accounts = await api.async_get_accounts()
                if not self._accounts:
                    self._validation_details = "No accounts returned by Starling API."
                    errors["base"] = "cannot_connect"
                else:
                    if len(self._accounts) == 1:
                        self._account_uid = self._accounts[0].get("accountUid")
                        return await self.async_step_account()
                    return await self.async_step_account()
            except StarlingApiError as err:
                _LOGGER.warning(
                    "Token validation failed: status=%s retry_after=%s error=%s",
                    getattr(err, "status", None),
                    getattr(err, "retry_after", None),
                    err,
                )
                self._validation_details = str(err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                self._validation_details = str(err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({
                vol.Required(CONF_ACCESS_TOKEN): str,
                vol.Optional(CONF_SANDBOX, default=False): BooleanSelector(),
            }),
            errors=errors,
            description_placeholders={
                "permissions": ", ".join(self._required_permissions),
                "validation_details": self._validation_details or "No validation errors yet.",
            },
        )

    async def async_step_account(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._account_uid = user_input[CONF_ACCOUNT_UID]
            session = async_get_clientsession(self.hass)
            api = StarlingApiClient(session, self._token, self._sandbox)
            try:
                account = await api.async_get_account(self._account_uid)
                self._account_name = account.get("name") or DEFAULT_NAME
                self._account_type = account.get("accountType")
                missing_permissions, notes = await self._validate_selected_features(api, self._account_uid)
                if missing_permissions:
                    self._validation_details = "Missing permissions: " + ", ".join(missing_permissions)
                    errors["base"] = "permissions_not_ok"
                else:
                    self._validation_details = " | ".join(notes) if notes else ""
                    await self.async_set_unique_id(self._account_uid)
                    self._abort_if_unique_id_configured()
                    return await self.async_step_select_entities()
            except Exception as err:
                self._validation_details = str(err)
                errors["base"] = "cannot_connect"

        default_uid = self._account_uid or (self._accounts[0].get("accountUid") if self._accounts else None)
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({vol.Required(CONF_ACCOUNT_UID, default=default_uid): _account_selector(self._accounts)}),
            errors=errors,
            description_placeholders={
                "validation_details": self._validation_details or "Choose which Starling account to use for this entry."
            },
        )

    async def async_step_select_entities(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        include_main = FEATURE_MAIN in self._features
        include_spaces = FEATURE_SPACES in self._features
        include_scheduled = FEATURE_SCHEDULED in self._features
        include_transfers = FEATURE_TRANSFERS in self._features
        is_savings_account = self._account_type == "SAVINGS"

        if user_input is not None:
            include_cleared = user_input.get(CONF_INCLUDE_CLEARED, include_main)
            include_effective = user_input.get(CONF_INCLUDE_EFFECTIVE, include_main)
            include_savings_spaces = user_input.get(CONF_INCLUDE_SAVINGS_SPACES, DEFAULT_INCLUDE_SAVINGS_SPACES)
            include_spending_spaces = False if is_savings_account else user_input.get(CONF_INCLUDE_SPENDING_SPACES, DEFAULT_INCLUDE_SPENDING_SPACES)
            include_kite_spaces = False if is_savings_account else user_input.get(CONF_INCLUDE_KITE_SPACES, DEFAULT_INCLUDE_KITE_SPACES)

            filtered_space_names = self._filtered_space_names(
                include_savings=include_savings_spaces,
                include_spending=include_spending_spaces,
                include_kite=include_kite_spaces,
            )
            offer_savings_checkbox, default_show_savings_spaces = _allow_savings_account_space_entities(
                self._account_type,
                filtered_space_names,
                user_input.get(CONF_SHOW_SAVINGS_ACCOUNT_SPACES),
            )
            show_savings_account_spaces = default_show_savings_spaces if offer_savings_checkbox else self._account_type != "SAVINGS"

            allowed_space_names = set(filtered_space_names)
            selected_spaces = [name for name in user_input.get(CONF_SPACE_NAMES, []) if name in allowed_space_names] if include_spaces else []
            if self._account_type == "SAVINGS" and not show_savings_account_spaces:
                selected_spaces = []

            history_limit = int(user_input.get(CONF_HISTORY_LIMIT, DEFAULT_HISTORY_LIMIT))
            upcoming_limit = int(user_input.get(CONF_UPCOMING_LIMIT, DEFAULT_UPCOMING_LIMIT))

            has_any_entity = False
            if include_main and (include_cleared or include_effective):
                has_any_entity = True
            if include_scheduled:
                has_any_entity = True
            if include_spaces and selected_spaces:
                has_any_entity = True
            if include_transfers and any(self._space_catalog.get(name, {}).get("supports_transfers") for name in selected_spaces):
                has_any_entity = True

            if not has_any_entity:
                errors["base"] = "no_entities_selected"
            else:
                return self.async_create_entry(
                    title=self._account_name,
                    data={
                        CONF_ACCESS_TOKEN: self._token,
                        CONF_SANDBOX: self._sandbox,
                        CONF_ACCOUNT_UID: self._account_uid,
                        CONF_FEATURES: self._features,
                        CONF_INCLUDE_CLEARED: include_cleared,
                        CONF_INCLUDE_EFFECTIVE: include_effective,
                        CONF_INCLUDE_SAVINGS_SPACES: include_savings_spaces,
                        CONF_INCLUDE_SPENDING_SPACES: include_spending_spaces,
                        CONF_INCLUDE_KITE_SPACES: include_kite_spaces,
                        CONF_SHOW_SAVINGS_ACCOUNT_SPACES: show_savings_account_spaces,
                        CONF_SPACE_NAMES: selected_spaces,
                        CONF_HISTORY_LIMIT: history_limit,
                        CONF_UPCOMING_LIMIT: upcoming_limit,
                    },
                )

        include_savings_spaces = DEFAULT_INCLUDE_SAVINGS_SPACES
        include_spending_spaces = False if is_savings_account else DEFAULT_INCLUDE_SPENDING_SPACES
        include_kite_spaces = False if is_savings_account else DEFAULT_INCLUDE_KITE_SPACES
        default_space_names = self._filtered_space_names(
            include_savings=include_savings_spaces,
            include_spending=include_spending_spaces,
            include_kite=include_kite_spaces,
        )
        offer_savings_checkbox, default_show_savings_spaces = _allow_savings_account_space_entities(
            self._account_type,
            default_space_names,
            DEFAULT_SHOW_SAVINGS_ACCOUNT_SPACES,
        )

        schema_fields: dict[Any, Any] = {}
        if include_main:
            schema_fields[vol.Optional(CONF_INCLUDE_CLEARED, default=True)] = BooleanSelector()
            schema_fields[vol.Optional(CONF_INCLUDE_EFFECTIVE, default=True)] = BooleanSelector()
        if include_spaces:
            schema_fields[vol.Optional(CONF_INCLUDE_SAVINGS_SPACES, default=include_savings_spaces)] = BooleanSelector()
            if not is_savings_account:
                schema_fields[vol.Optional(CONF_INCLUDE_SPENDING_SPACES, default=include_spending_spaces)] = BooleanSelector()
                schema_fields[vol.Optional(CONF_INCLUDE_KITE_SPACES, default=include_kite_spaces)] = BooleanSelector()
            if offer_savings_checkbox:
                schema_fields[vol.Optional(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, default=default_show_savings_spaces)] = BooleanSelector()
            if not is_savings_account or offer_savings_checkbox:
                schema_fields[vol.Optional(CONF_SPACE_NAMES, default=default_space_names if (not is_savings_account or default_show_savings_spaces) else [])] = _space_selector(
                    self._space_options(
                        include_savings=include_savings_spaces,
                        include_spending=include_spending_spaces,
                        include_kite=include_kite_spaces,
                    )
                )
        if include_scheduled:
            schema_fields[vol.Optional(CONF_UPCOMING_LIMIT, default=DEFAULT_UPCOMING_LIMIT)] = _int_selector()
        if include_transfers:
            schema_fields[vol.Optional(CONF_HISTORY_LIMIT, default=DEFAULT_HISTORY_LIMIT)] = _int_selector()

        return self.async_show_form(step_id="select_entities", data_schema=vol.Schema(schema_fields), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        permissions: set[str] = set()
        for feature in entry.data.get(CONF_FEATURES, [FEATURE_MAIN, FEATURE_SPACES]):
            permissions.update(FEATURE_PERMISSIONS.get(feature, []))

        if user_input is not None:
            token = user_input[CONF_ACCESS_TOKEN].strip()
            sandbox = user_input.get(CONF_SANDBOX, False)
            session = async_get_clientsession(self.hass)
            api = StarlingApiClient(session, token, sandbox)
            try:
                account = await api.async_get_account(entry.data.get(CONF_ACCOUNT_UID)) if entry.data.get(CONF_ACCOUNT_UID) else await api.async_get_primary_account()
                account_uid = account.get("accountUid")
                if not account_uid:
                    self._validation_details = "No accountUid returned by Starling API."
                    errors["base"] = "cannot_connect"
                else:
                    self._features = list(entry.data.get(CONF_FEATURES, [FEATURE_MAIN, FEATURE_SPACES]))
                    missing_permissions, notes = await self._validate_selected_features(api, account_uid)
                    if missing_permissions:
                        self._validation_details = "Missing permissions: " + ", ".join(missing_permissions)
                        errors["base"] = "permissions_not_ok"
                    elif entry.data.get(CONF_ACCOUNT_UID) and account_uid != entry.data.get(CONF_ACCOUNT_UID):
                        return self.async_abort(reason="wrong_account")
                    else:
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data={
                                **entry.data,
                                CONF_ACCESS_TOKEN: token,
                                CONF_SANDBOX: sandbox,
                            },
                        )
                        return self.async_abort(reason="reconfigure_successful")
            except StarlingApiError as err:
                self._validation_details = str(err)
                errors["base"] = "cannot_connect"
            except Exception as err:
                self._validation_details = str(err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_ACCESS_TOKEN): str,
                vol.Optional(CONF_SANDBOX, default=entry.data.get(CONF_SANDBOX, False)): BooleanSelector(),
            }),
            errors=errors,
            description_placeholders={
                "permissions": ", ".join(sorted(permissions)),
                "validation_details": self._validation_details or f"Current token: {_mask_token(entry.data.get(CONF_ACCESS_TOKEN, ''))}",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return StarlingOptionsFlow(config_entry)


class StarlingOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._account_type: str | None = None
        self._space_catalog: dict[str, dict[str, Any]] = {}

    async def _load_space_catalog(self) -> None:
        if self._space_catalog:
            return

        session = async_get_clientsession(self.hass)
        api = StarlingApiClient(session, self._entry.data[CONF_ACCESS_TOKEN], self._entry.data.get(CONF_SANDBOX, False))
        account_uid = self._entry.data.get(CONF_ACCOUNT_UID)

        try:
            if account_uid:
                account = await api.async_get_account(account_uid)
                self._account_type = account.get("accountType")
                goals = await api.async_get_savings_goals(account_uid)
            else:
                account = await api.async_get_primary_account()
                self._account_type = account.get("accountType")
                goals = await api.async_get_savings_goals(account.get("accountUid"))
        except Exception as err:
            _LOGGER.debug("Options flow could not refresh spaces, using saved values: error=%s", err)
            goals = []
            self._account_type = self._entry.data.get("account_type")

        catalog: dict[str, dict[str, Any]] = {}
        for goal in goals:
            name = (goal.get("name") or "").strip()
            if not name:
                continue
            category = StarlingApiClient.classify_space(goal)
            catalog[name] = {
                "name": name,
                "category": category,
                "supports_transfers": category == "savings" and bool(goal.get("savingsGoalUid")),
            }

        # Keep previously selected spaces available even if refresh failed.
        for name in self._entry.options.get(CONF_SPACE_NAMES, self._entry.data.get(CONF_SPACE_NAMES, [])):
            if name not in catalog:
                catalog[name] = {
                    "name": name,
                    "category": "savings",
                    "supports_transfers": True,
                }

        self._space_catalog = dict(sorted(catalog.items(), key=lambda item: item[0].lower()))

    def _filtered_space_names(
        self,
        *,
        include_savings: bool,
        include_spending: bool,
        include_kite: bool,
    ) -> list[str]:
        names: list[str] = []
        for name, meta in self._space_catalog.items():
            category = meta.get("category")
            if category == "savings" and include_savings:
                names.append(name)
            elif category == "spending" and include_spending:
                names.append(name)
            elif category == "kite" and include_kite:
                names.append(name)
        return names

    def _space_options(
        self,
        *,
        include_savings: bool,
        include_spending: bool,
        include_kite: bool,
    ) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for name in self._filtered_space_names(
            include_savings=include_savings,
            include_spending=include_spending,
            include_kite=include_kite,
        ):
            category = self._space_catalog.get(name, {}).get("category", "unknown")
            options.append({"value": name, "label": f"{name} [{_category_label(category)}]"})
        return options

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        features = self._entry.data.get(CONF_FEATURES, [FEATURE_MAIN, FEATURE_SPACES])
        include_main = FEATURE_MAIN in features
        include_spaces = FEATURE_SPACES in features
        include_scheduled = FEATURE_SCHEDULED in features
        include_transfers = FEATURE_TRANSFERS in features

        if include_spaces:
            await self._load_space_catalog()

        is_savings_account = self._account_type == "SAVINGS"

        if user_input is not None:
            include_cleared = user_input.get(CONF_INCLUDE_CLEARED, include_main)
            include_effective = user_input.get(CONF_INCLUDE_EFFECTIVE, include_main)
            include_savings_spaces = user_input.get(
                CONF_INCLUDE_SAVINGS_SPACES,
                self._entry.options.get(CONF_INCLUDE_SAVINGS_SPACES, self._entry.data.get(CONF_INCLUDE_SAVINGS_SPACES, DEFAULT_INCLUDE_SAVINGS_SPACES)),
            )
            include_spending_spaces = False if is_savings_account else user_input.get(
                CONF_INCLUDE_SPENDING_SPACES,
                self._entry.options.get(CONF_INCLUDE_SPENDING_SPACES, self._entry.data.get(CONF_INCLUDE_SPENDING_SPACES, DEFAULT_INCLUDE_SPENDING_SPACES)),
            )
            include_kite_spaces = False if is_savings_account else user_input.get(
                CONF_INCLUDE_KITE_SPACES,
                self._entry.options.get(CONF_INCLUDE_KITE_SPACES, self._entry.data.get(CONF_INCLUDE_KITE_SPACES, DEFAULT_INCLUDE_KITE_SPACES)),
            )

            filtered_space_names = self._filtered_space_names(
                include_savings=include_savings_spaces,
                include_spending=include_spending_spaces,
                include_kite=include_kite_spaces,
            )
            offer_savings_checkbox, default_show_savings_spaces = _allow_savings_account_space_entities(
                self._account_type,
                filtered_space_names,
                user_input.get(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, self._entry.options.get(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, self._entry.data.get(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, DEFAULT_SHOW_SAVINGS_ACCOUNT_SPACES))),
            )
            show_savings_account_spaces = default_show_savings_spaces if offer_savings_checkbox else self._account_type != "SAVINGS"

            allowed_space_names = set(filtered_space_names)
            selected_spaces = [name for name in user_input.get(CONF_SPACE_NAMES, []) if name in allowed_space_names] if include_spaces else []
            if self._account_type == "SAVINGS" and not show_savings_account_spaces:
                selected_spaces = []
            history_limit = int(user_input.get(CONF_HISTORY_LIMIT, self._entry.options.get(CONF_HISTORY_LIMIT, self._entry.data.get(CONF_HISTORY_LIMIT, DEFAULT_HISTORY_LIMIT))))
            upcoming_limit = int(user_input.get(CONF_UPCOMING_LIMIT, self._entry.options.get(CONF_UPCOMING_LIMIT, self._entry.data.get(CONF_UPCOMING_LIMIT, DEFAULT_UPCOMING_LIMIT))))

            has_any_entity = False
            if include_main and (include_cleared or include_effective):
                has_any_entity = True
            if include_scheduled:
                has_any_entity = True
            if include_spaces and selected_spaces:
                has_any_entity = True
            if include_transfers and any(self._space_catalog.get(name, {}).get("supports_transfers") for name in selected_spaces):
                has_any_entity = True

            if not has_any_entity:
                errors["base"] = "no_entities_selected"
            else:
                return self.async_create_entry(data={
                    CONF_INCLUDE_CLEARED: include_cleared,
                    CONF_INCLUDE_EFFECTIVE: include_effective,
                    CONF_INCLUDE_SAVINGS_SPACES: include_savings_spaces,
                    CONF_INCLUDE_SPENDING_SPACES: include_spending_spaces,
                    CONF_INCLUDE_KITE_SPACES: include_kite_spaces,
                    CONF_SHOW_SAVINGS_ACCOUNT_SPACES: show_savings_account_spaces,
                    CONF_SPACE_NAMES: selected_spaces,
                    CONF_HISTORY_LIMIT: history_limit,
                    CONF_UPCOMING_LIMIT: upcoming_limit,
                })

        include_savings_spaces = self._entry.options.get(CONF_INCLUDE_SAVINGS_SPACES, self._entry.data.get(CONF_INCLUDE_SAVINGS_SPACES, DEFAULT_INCLUDE_SAVINGS_SPACES))
        include_spending_spaces = False if is_savings_account else self._entry.options.get(CONF_INCLUDE_SPENDING_SPACES, self._entry.data.get(CONF_INCLUDE_SPENDING_SPACES, DEFAULT_INCLUDE_SPENDING_SPACES))
        include_kite_spaces = False if is_savings_account else self._entry.options.get(CONF_INCLUDE_KITE_SPACES, self._entry.data.get(CONF_INCLUDE_KITE_SPACES, DEFAULT_INCLUDE_KITE_SPACES))
        filtered_default_space_names = self._filtered_space_names(
            include_savings=include_savings_spaces,
            include_spending=include_spending_spaces,
            include_kite=include_kite_spaces,
        )
        offer_savings_checkbox, default_show_savings_spaces = _allow_savings_account_space_entities(
            self._account_type,
            filtered_default_space_names,
            self._entry.options.get(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, self._entry.data.get(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, DEFAULT_SHOW_SAVINGS_ACCOUNT_SPACES)),
        )
        default_space_names = [
            name
            for name in self._entry.options.get(CONF_SPACE_NAMES, self._entry.data.get(CONF_SPACE_NAMES, []))
            if name in self._filtered_space_names(
                include_savings=include_savings_spaces,
                include_spending=include_spending_spaces,
                include_kite=include_kite_spaces,
            )
        ]
        if is_savings_account and not default_show_savings_spaces:
            default_space_names = []

        schema_fields: dict[Any, Any] = {}
        if include_main:
            schema_fields[vol.Optional(CONF_INCLUDE_CLEARED, default=self._entry.options.get(CONF_INCLUDE_CLEARED, self._entry.data.get(CONF_INCLUDE_CLEARED, True)))] = BooleanSelector()
            schema_fields[vol.Optional(CONF_INCLUDE_EFFECTIVE, default=self._entry.options.get(CONF_INCLUDE_EFFECTIVE, self._entry.data.get(CONF_INCLUDE_EFFECTIVE, True)))] = BooleanSelector()
        if include_spaces:
            schema_fields[vol.Optional(CONF_INCLUDE_SAVINGS_SPACES, default=include_savings_spaces)] = BooleanSelector()
            if not is_savings_account:
                schema_fields[vol.Optional(CONF_INCLUDE_SPENDING_SPACES, default=include_spending_spaces)] = BooleanSelector()
                schema_fields[vol.Optional(CONF_INCLUDE_KITE_SPACES, default=include_kite_spaces)] = BooleanSelector()
            if offer_savings_checkbox:
                schema_fields[vol.Optional(CONF_SHOW_SAVINGS_ACCOUNT_SPACES, default=default_show_savings_spaces)] = BooleanSelector()
            if not is_savings_account or offer_savings_checkbox:
                schema_fields[vol.Optional(CONF_SPACE_NAMES, default=default_space_names if (not is_savings_account or default_show_savings_spaces) else [])] = _space_selector(
                    self._space_options(
                        include_savings=include_savings_spaces,
                        include_spending=include_spending_spaces,
                        include_kite=include_kite_spaces,
                    )
                )
        if include_scheduled:
            schema_fields[vol.Optional(CONF_UPCOMING_LIMIT, default=self._entry.options.get(CONF_UPCOMING_LIMIT, self._entry.data.get(CONF_UPCOMING_LIMIT, DEFAULT_UPCOMING_LIMIT)))] = _int_selector()
        if include_transfers:
            schema_fields[vol.Optional(CONF_HISTORY_LIMIT, default=self._entry.options.get(CONF_HISTORY_LIMIT, self._entry.data.get(CONF_HISTORY_LIMIT, DEFAULT_HISTORY_LIMIT)))] = _int_selector()

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_fields), errors=errors)
