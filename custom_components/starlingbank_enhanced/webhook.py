"""Webhook support for Starling Bank Enhanced."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from homeassistant.components import webhook as hass_webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_WEBHOOK_ID,
    CONF_WEBHOOK_LAST_RECEIVED,
    CONF_WEBHOOK_URL,
    COORDINATOR,
    DOMAIN,
    WEBHOOK_DEBOUNCE_SECONDS,
    CONF_WEBHOOK_PUBLIC_KEY,
    CONF_WEBHOOK_LAST_EVENT_TYPE,
    CONF_WEBHOOK_LAST_NONCE,
    CONF_WEBHOOK_LAST_ROUTE,
    WEBHOOK_NONCE_CACHE_KEY,
    WEBHOOK_REPLAY_WINDOW_SECONDS,
    WEBHOOK_MAX_EVENT_AGE_SECONDS,
    WEBHOOK_SIGNATURE_HEADER,
)

_LOGGER = logging.getLogger(__name__)

EVENT_ROUTE_FEED = "feed"
EVENT_ROUTE_SCHEDULED = "scheduled"
EVENT_ROUTE_SPACE = "space"
EVENT_ROUTE_TRANSFER = "transfer"
EVENT_ROUTE_UNKNOWN = "unknown"


@dataclass(slots=True)
class StarlingWebhookEvent:
    """Parsed Starling webhook event."""

    event_type: str
    route: str
    occurred_at: datetime | None
    nonce: str
    payload: dict[str, Any]


def _looks_like_starling_payload(payload: dict) -> bool:
    """Very light validation for Starling webhook payloads."""
    if not isinstance(payload, dict):
        return False

    if "webhookNotificationUid" in payload:
        return True

    event_type = payload.get("type")
    if isinstance(event_type, str) and event_type.strip():
        return True

    content = payload.get("content")
    if isinstance(content, dict):
        nested_type = content.get("type")
        if isinstance(nested_type, str) and nested_type.strip():
            return True

    return False


def _normalize_public_key_pem(value: str) -> bytes:
    """Accept full PEM or bare base64 body."""
    text = value.strip()
    if not text:
        raise ValueError("Empty public key")

    if "BEGIN PUBLIC KEY" in text:
        return text.encode()

    wrapped = "\n".join(text[i : i + 64] for i in range(0, len(text), 64))
    return f"-----BEGIN PUBLIC KEY-----\n{wrapped}\n-----END PUBLIC KEY-----\n".encode()


def _load_starling_public_key(entry: ConfigEntry):
    """Load webhook public key from config entry."""
    value = entry.options.get(CONF_WEBHOOK_PUBLIC_KEY) or entry.data.get(CONF_WEBHOOK_PUBLIC_KEY)
    if not value:
        return None
    return load_pem_public_key(_normalize_public_key_pem(value))


def _payload_sha512_bytes(body: bytes) -> bytes:
    return hashlib.sha512(body).digest()


def _recover_rsa_signed_message(public_key, signature: bytes) -> bytes | None:
    """Compatibility fallback for docs describing 'decrypt signature with public key'."""
    if not isinstance(public_key, rsa.RSAPublicKey):
        return None

    numbers = public_key.public_numbers()
    sig_int = int.from_bytes(signature, "big")
    recovered_int = pow(sig_int, numbers.e, numbers.n)
    size = (public_key.key_size + 7) // 8
    recovered = recovered_int.to_bytes(size, "big")

    if not recovered.startswith(b"\x00\x01"):
        return None

    try:
        pad_end = recovered.index(b"\x00", 2)
    except ValueError:
        return None

    return recovered[pad_end + 1 :]


def _constant_time_compare_any(candidate: bytes, expected_variants: list[bytes]) -> bool:
    return any(hmac.compare_digest(candidate, item) for item in expected_variants)


def _verify_starling_signature(
    entry: ConfigEntry,
    signature_header: str | None,
    body: bytes,
) -> bool:
    """Verify Starling V2 webhook signature against configured public key."""
    public_key = _load_starling_public_key(entry)
    if public_key is None:
        _LOGGER.warning("Webhook public key is not configured")
        return False

    if not signature_header:
        _LOGGER.warning("Missing %s header", WEBHOOK_SIGNATURE_HEADER)
        return False

    try:
        signature = base64.b64decode(signature_header, validate=True)
    except Exception:
        _LOGGER.warning("Webhook signature is not valid base64")
        return False

    digest = _payload_sha512_bytes(body)

    # Primary verification path.
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            try:
                public_key.verify(signature, body, padding.PKCS1v15(), hashes.SHA512())
                return True
            except Exception:
                public_key.verify(
                    signature,
                    digest,
                    padding.PKCS1v15(),
                    utils.Prehashed(hashes.SHA512()),
                )
                return True

        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, body, ec.ECDSA(hashes.SHA512()))
            return True
    except Exception:
        pass

    # Compatibility fallback.
    recovered = _recover_rsa_signed_message(public_key, signature)
    if recovered is None:
        return False

    expected_variants = [
        digest,
        base64.b64encode(digest),
        hashlib.sha512(body).hexdigest().encode(),
    ]
    return _constant_time_compare_any(recovered, expected_variants)


def _parse_event_timestamp(payload: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "createdAt", "eventTimestamp"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            continue

        normalized = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            continue

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)

        return dt.astimezone(UTC)

    return None


def _is_fresh_enough(event_time: datetime | None) -> bool:
    if event_time is None:
        return True

    age = abs((datetime.now(UTC) - event_time).total_seconds())
    return age <= WEBHOOK_MAX_EVENT_AGE_SECONDS


def _build_replay_nonce(payload: dict[str, Any], body: bytes, signature_header: str | None) -> str:
    for key in ("webhookNotificationUid", "eventUid", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    basis = (signature_header or "").encode() + b":" + body
    return hashlib.sha256(basis).hexdigest()


def _prune_nonce_cache(runtime: dict[str, Any], now: float) -> dict[str, float]:
    cache = runtime.setdefault(WEBHOOK_NONCE_CACHE_KEY, {})
    expired = [nonce for nonce, ts in cache.items() if (now - ts) > WEBHOOK_REPLAY_WINDOW_SECONDS]
    for nonce in expired:
        cache.pop(nonce, None)
    return cache


def _register_nonce(runtime: dict[str, Any], nonce: str, now: float) -> bool:
    cache = _prune_nonce_cache(runtime, now)
    if nonce in cache:
        return False
    cache[nonce] = now
    return True


def _route_for_event_type(event_type: str) -> str:
    upper = event_type.upper()

    if any(token in upper for token in ("FEED", "TRANSACTION", "CARD")):
        return EVENT_ROUTE_FEED
    if any(token in upper for token in ("STANDING_ORDER", "SCHEDULED", "PAYMENT")):
        return EVENT_ROUTE_SCHEDULED
    if "SPACE" in upper:
        return EVENT_ROUTE_SPACE
    if "TRANSFER" in upper:
        return EVENT_ROUTE_TRANSFER

    return EVENT_ROUTE_UNKNOWN


def _parse_event(payload: dict[str, Any], body: bytes, signature_header: str | None) -> StarlingWebhookEvent:
    event_type = (
        payload.get("type")
        or payload.get("content", {}).get("type")
        or "unknown"
    )

    return StarlingWebhookEvent(
        event_type=event_type,
        route=_route_for_event_type(event_type),
        occurred_at=_parse_event_timestamp(payload),
        nonce=_build_replay_nonce(payload, body, signature_header),
        payload=payload,
    )


def _is_internal_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return host.startswith(("127.", "10.", "192.168.", "172.")) or host in {
        "localhost",
        "homeassistant.local",
    }


async def async_register_webhook(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[str, str | None, callable]:
    """Register webhook handler for a config entry."""
    webhook_id = entry.data.get(CONF_WEBHOOK_ID) or hass_webhook.async_generate_id()
    webhook_url = None

    try:
        base_url = get_url(
            hass,
            allow_internal=False,
            allow_external=True,
            allow_cloud=True,
        )
        webhook_url = f"{base_url}/api/webhook/{webhook_id}"
        if _is_internal_url(webhook_url):
            _LOGGER.warning("Generated webhook URL is internal: %s", webhook_url)
    except NoURLAvailableError:
        _LOGGER.warning(
            "No external Home Assistant URL available for Starling webhook; "
            "configure an External URL or Home Assistant Cloud."
        )
        webhook_url = None

    async def _handler(hass: HomeAssistant, webhook_id: str, request):
        return await async_handle_webhook(hass, entry, request)

    hass_webhook.async_register(
        hass,
        DOMAIN,
        f"Starling Bank Enhanced {entry.entry_id}",
        webhook_id,
        _handler,
    )

    def _unsubscribe() -> None:
        hass_webhook.async_unregister(hass, webhook_id)

    current_data = dict(entry.data)
    changed = False

    if current_data.get(CONF_WEBHOOK_ID) != webhook_id:
        current_data[CONF_WEBHOOK_ID] = webhook_id
        changed = True

    if current_data.get(CONF_WEBHOOK_URL) != webhook_url:
        current_data[CONF_WEBHOOK_URL] = webhook_url
        changed = True

    if changed:
        hass.config_entries.async_update_entry(entry, data=current_data)

    return webhook_id, webhook_url, _unsubscribe


async def async_handle_webhook(
    hass: HomeAssistant,
    entry: ConfigEntry,
    request,
) -> web.Response:
    """Handle inbound Starling webhook."""
    domain_data = hass.data.get(DOMAIN, {})
    runtime = domain_data.get(entry.entry_id)

    if runtime is None:
        _LOGGER.warning("Received webhook for unloaded entry_id=%s", entry.entry_id)
        return web.Response(status=404, text="entry not loaded")

    coordinator = runtime.get(COORDINATOR)
    if coordinator is None:
        _LOGGER.warning("Received webhook without coordinator for entry_id=%s", entry.entry_id)
        return web.Response(status=503, text="coordinator unavailable")

    try:
        body = await request.read()
        if not body:
            return web.Response(status=400, text="empty body")
        payload = json.loads(body)
    except Exception:
        return web.Response(status=400, text="invalid json")

    if not _looks_like_starling_payload(payload):
        return web.Response(status=400, text="invalid payload")

    signature_header = request.headers.get(WEBHOOK_SIGNATURE_HEADER)
    if not _verify_starling_signature(entry, signature_header, body):
        _LOGGER.warning("Rejected webhook with invalid signature for entry_id=%s", entry.entry_id)
        return web.Response(status=401, text="invalid signature")

    now = monotonic()
    last = runtime.get("last_webhook_monotonic")
    if last is not None and (now - last) < WEBHOOK_DEBOUNCE_SECONDS:
        return web.Response(status=202, text="debounced")

    event = _parse_event(payload, body, signature_header)
    if not _is_fresh_enough(event.occurred_at):
        _LOGGER.warning(
            "Rejected stale webhook for entry_id=%s event_type=%s",
            entry.entry_id,
            event.event_type,
        )
        return web.Response(status=409, text="stale webhook")

    if not _register_nonce(runtime, event.nonce, now):
        _LOGGER.info(
            "Ignored duplicate Starling webhook for entry_id=%s nonce=%s",
            entry.entry_id,
            event.nonce,
        )
        return web.Response(status=202, text="duplicate")

    runtime["last_webhook_monotonic"] = now
    runtime[CONF_WEBHOOK_LAST_RECEIVED] = (
        event.occurred_at.isoformat() if event.occurred_at else None
    )
    runtime[CONF_WEBHOOK_LAST_EVENT_TYPE] = event.event_type
    runtime[CONF_WEBHOOK_LAST_NONCE] = event.nonce
    runtime[CONF_WEBHOOK_LAST_ROUTE] = event.route

    _LOGGER.debug(
        "Accepted Starling webhook for entry_id=%s event_type=%s route=%s",
        entry.entry_id,
        event.event_type,
        event.route,
    )

    try:
        hass.async_create_task(coordinator.async_request_refresh())
    except Exception as err:
        _LOGGER.exception(
            "Failed to schedule refresh after webhook for entry_id=%s: %s",
            entry.entry_id,
            err,
        )
        return web.Response(status=500, text="refresh scheduling failed")

    return web.Response(status=200, text="ok")