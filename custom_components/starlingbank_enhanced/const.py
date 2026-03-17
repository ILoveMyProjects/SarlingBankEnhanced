"""Constants for Starling Bank Enhanced."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "starlingbank_enhanced"

CONF_ACCESS_TOKEN = "access_token"
CONF_SANDBOX = "sandbox"
CONF_ACCOUNT_UID = "account_uid"
CONF_FEATURES = "features"
CONF_INCLUDE_CLEARED = "include_cleared_balance"
CONF_INCLUDE_EFFECTIVE = "include_effective_balance"
CONF_SPACE_NAMES = "space_names"
CONF_INCLUDE_SAVINGS_SPACES = "include_savings_spaces"
CONF_INCLUDE_SPENDING_SPACES = "include_spending_spaces"
CONF_INCLUDE_KITE_SPACES = "include_kite_spaces"
CONF_SHOW_SAVINGS_ACCOUNT_SPACES = "show_savings_account_spaces"
CONF_UPCOMING_LIMIT = "upcoming_limit"
CONF_HISTORY_LIMIT = "history_limit"
CONF_USE_WEBHOOK = "use_webhook"
CONF_WEBHOOK_ID = "webhook_id"
CONF_WEBHOOK_URL = "webhook_url"
CONF_WEBHOOK_LAST_RECEIVED = "webhook_last_received"
CONF_WEBHOOK_SECRET = "webhook_secret"
CONF_WEBHOOK_PUBLIC_KEY = "webhook_public_key"
CONF_WEBHOOK_LAST_EVENT_TYPE = "webhook_last_event_type"
CONF_WEBHOOK_LAST_NONCE = "webhook_last_nonce"
CONF_WEBHOOK_LAST_ROUTE = "webhook_last_route"

WEBHOOK_DEBOUNCE_SECONDS = 5
WEBHOOK_REPLAY_WINDOW_SECONDS = 600
WEBHOOK_MAX_EVENT_AGE_SECONDS = 900
WEBHOOK_SIGNATURE_HEADER = "X-Hook-Signature"
WEBHOOK_NONCE_CACHE_KEY = "seen_webhook_nonces"

DEFAULT_USE_WEBHOOK = False
DEFAULT_NAME = "Starling"
DEFAULT_UPCOMING_LIMIT = 5
DEFAULT_HISTORY_LIMIT = 5
DEFAULT_INCLUDE_SAVINGS_SPACES = True
DEFAULT_INCLUDE_SPENDING_SPACES = True
DEFAULT_INCLUDE_KITE_SPACES = True
DEFAULT_SHOW_SAVINGS_ACCOUNT_SPACES = False
DEFAULT_SCAN_INTERVAL = timedelta(minutes=10)
DEFAULT_ACCOUNT_REFRESH_INTERVAL = timedelta(hours=6)
DEFAULT_SAVINGS_REFRESH_INTERVAL = timedelta(minutes=30)
DEFAULT_SCHEDULED_REFRESH_INTERVAL = timedelta(minutes=30)
DEFAULT_TRANSFER_HISTORY_REFRESH_INTERVAL = timedelta(minutes=30)
DEFAULT_BACKOFF_SECONDS = 300
DEFAULT_TRANSFER_LOOKBACK_DAYS = 90


COORDINATOR = "coordinator"

FEATURE_MAIN = "main_balance"
FEATURE_SPACES = "spaces"
FEATURE_SCHEDULED = "scheduled_payments"
FEATURE_TRANSFERS = "savings_goal_transfers"

FEATURE_LABELS = {
    FEATURE_MAIN: "Main balance [account:read, balance:read]",
    FEATURE_SPACES: "Spaces [account:read, savings-goal:read, space:read]",
    FEATURE_SCHEDULED: "Scheduled payments [account:read, scheduled-payment:read, transaction:read]",
    FEATURE_TRANSFERS: (
        "Savings goal transfers "
        "[account:read, savings-goal-transfer:read, savings-goal:read, space:read, transaction:read]"
    ),
}

FEATURE_PERMISSIONS = {
    FEATURE_MAIN: ["account:read", "balance:read"],
    FEATURE_SPACES: ["account:read", "savings-goal:read", "space:read"],
    FEATURE_SCHEDULED: ["account:read", "scheduled-payment:read", "transaction:read"],
    FEATURE_TRANSFERS: [
        "account:read",
        "savings-goal-transfer:read",
        "savings-goal:read",
        "space:read",
        "transaction:read",
    ],
}
