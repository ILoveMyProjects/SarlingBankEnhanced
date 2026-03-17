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

    if "webhookEventUid" in payload:
        return True

    if "webhookNotificationUid" in payload:
        return True

    event_type = payload.get("webhookType") or payload.get("type")
    if isinstance(event_type, str) and event_type.strip():
        return True

    content = payload.get("content")
    if isinstance(content, dict) and content:
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
        _LOGGER.warning("STARLING SIGNATURE FAIL reason=no_public_key")
        return False

    if not signature_header:
        _LOGGER.warning("STARLING SIGNATURE FAIL reason=missing_header header=%s", WEBHOOK_SIGNATURE_HEADER)
        return False

    try:
        signature = base64.b64decode(signature_header, validate=True)
    except Exception:
        _LOGGER.warning("STARLING SIGNATURE FAIL reason=invalid_base64")
        return False

    digest = _payload_sha512_bytes(body)

    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            try:
                public_key.verify(signature, body, padding.PKCS1v15(), hashes.SHA512())
                _LOGGER.warning("STARLING SIGNATURE OK mode=rsa_body")
                return True
            except Exception:
                public_key.verify(
                    signature,
                    digest,
                    padding.PKCS1v15(),
                    utils.Prehashed(hashes.SHA512()),
                )
                _LOGGER.warning("STARLING SIGNATURE OK mode=rsa_prehash")
                return True

        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, body, ec.ECDSA(hashes.SHA512()))
            _LOGGER.warning("STARLING SIGNATURE OK mode=ecdsa_body")
            return True
    except Exception as err:
        _LOGGER.warning("STARLING SIGNATURE PRIMARY VERIFY FAIL err=%s", err)

    recovered = _recover_rsa_signed_message(public_key, signature)
    if recovered is None:
        _LOGGER.warning("STARLING SIGNATURE FAIL reason=rsa_recover_none")
        return False

    expected_variants = [
        digest,
        base64.b64encode(digest),
        hashlib.sha512(body).hexdigest().encode(),
    ]
    ok = _constant_time_compare_any(recovered, expected_variants)
    if ok:
        _LOGGER.warning("STARLING SIGNATURE OK mode=rsa_recover_compare")
    else:
        _LOGGER.warning("STARLING SIGNATURE FAIL reason=rsa_recover_compare_mismatch")
    return ok


def _parse_event_timestamp(payload: dict[str, Any]) -> datetime | None:
    for key in ("eventTimestamp", "timestamp", "createdAt", "updatedAt"):
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

    content = payload.get("content")
    if isinstance(content, dict):
        for key in ("updatedAt", "createdAt"):
            value = content.get(key)
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


def _build_replay_nonce(
    payload: dict[str, Any],
    body: bytes,
    signature_header: str | None,
) -> str:
    for key in ("webhookEventUid", "webhookNotificationUid", "eventUid", "id"):
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


def _parse_event(
    payload: dict[str, Any],
    body: bytes,
    signature_header: str | None,
) -> StarlingWebhookEvent:
    event_type = (
        payload.get("webhookType")
        or payload.get("type")
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
    _LOGGER.warning("STARLING WEBHOOK HIT entry_id=%s", entry.entry_id)

    domain_data = hass.data.get(DOMAIN, {})
    runtime = domain_data.get(entry.entry_id)

    _LOGGER.warning(
        "STARLING WEBHOOK META method=%s path=%s headers=%s",
        request.method,
        request.path,
        dict(request.headers),
    )

    if runtime is None:
        _LOGGER.warning("STARLING WEBHOOK FAIL reason=runtime_missing entry_id=%s", entry.entry_id)
        return web.Response(status=404, text="entry not loaded")

    coordinator = runtime.get(COORDINATOR)
    if coordinator is None:
        _LOGGER.warning("STARLING WEBHOOK FAIL reason=coordinator_missing entry_id=%s", entry.entry_id)
        return web.Response(status=503, text="coordinator unavailable")

    try:
        body = await request.read()
        _LOGGER.warning(
            "STARLING WEBHOOK BODY bytes=%s preview=%s",
            len(body),
            body[:500].decode(errors="replace"),
        )
        if not body:
            _LOGGER.warning("STARLING WEBHOOK FAIL reason=empty_body")
            return web.Response(status=400, text="empty body")
        payload = json.loads(body)
        _LOGGER.warning(
            "STARLING WEBHOOK JSON PARSED keys=%s",
            list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
    except Exception as err:
        _LOGGER.warning("STARLING WEBHOOK FAIL reason=invalid_json err=%s", err)
        return web.Response(status=400, text="invalid json")

    looks_valid = _looks_like_starling_payload(payload)
    _LOGGER.warning("STARLING WEBHOOK PAYLOAD CHECK looks_like_starling=%s", looks_valid)
    if not looks_valid:
        _LOGGER.warning("STARLING WEBHOOK FAIL reason=invalid_payload")
        return web.Response(status=400, text="invalid payload")

    signature_header = request.headers.get(WEBHOOK_SIGNATURE_HEADER)
    _LOGGER.warning(
        "STARLING WEBHOOK SIGNATURE HEADER present=%s length=%s",
        bool(signature_header),
        len(signature_header) if signature_header else 0,
    )

    if not _verify_starling_signature(entry, signature_header, body):
        _LOGGER.warning("STARLING WEBHOOK FAIL reason=invalid_signature entry_id=%s", entry.entry_id)
        return web.Response(status=401, text="invalid signature")

    now = monotonic()
    last = runtime.get("last_webhook_monotonic")
    _LOGGER.warning("STARLING WEBHOOK TIMING now=%s last=%s debounce_seconds=%s", now, last, WEBHOOK_DEBOUNCE_SECONDS)

    if last is not None and (now - last) < WEBHOOK_DEBOUNCE_SECONDS:
        _LOGGER.warning("STARLING WEBHOOK DEBOUNCED delta=%s", now - last)
        return web.Response(status=202, text="debounced")

    event = _parse_event(payload, body, signature_header)
    _LOGGER.warning(
        "STARLING WEBHOOK EVENT type=%s route=%s occurred_at=%s nonce=%s",
        event.event_type,
        event.route,
        event.occurred_at,
        event.nonce,
    )

    fresh = _is_fresh_enough(event.occurred_at)
    _LOGGER.warning("STARLING WEBHOOK FRESH_CHECK fresh=%s", fresh)
    if not fresh:
        _LOGGER.warning(
            "STARLING WEBHOOK FAIL reason=stale entry_id=%s event_type=%s",
            entry.entry_id,
            event.event_type,
        )
        return web.Response(status=409, text="stale webhook")

    nonce_ok = _register_nonce(runtime, event.nonce, now)
    _LOGGER.warning(
        "STARLING WEBHOOK NONCE register_ok=%s cache_size=%s",
        nonce_ok,
        len(runtime.get(WEBHOOK_NONCE_CACHE_KEY, {})),
    )
    if not nonce_ok:
        _LOGGER.warning(
            "STARLING WEBHOOK DUPLICATE entry_id=%s nonce=%s",
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

    _LOGGER.warning(
        "STARLING WEBHOOK RUNTIME UPDATED last_received=%s last_event_type=%s last_route=%s",
        runtime[CONF_WEBHOOK_LAST_RECEIVED],
        runtime[CONF_WEBHOOK_LAST_EVENT_TYPE],
        runtime[CONF_WEBHOOK_LAST_ROUTE],
    )

    try:
        _LOGGER.warning("STARLING WEBHOOK REFRESH REQUESTED entry_id=%s", entry.entry_id)
        task = hass.async_create_task(coordinator.async_request_refresh())
        _LOGGER.warning("STARLING WEBHOOK REFRESH TASK CREATED task=%s", task)
    except Exception as err:
        _LOGGER.exception(
            "STARLING WEBHOOK FAIL reason=refresh_schedule_error entry_id=%s err=%s",
            entry.entry_id,
            err,
        )
        return web.Response(status=500, text="refresh scheduling failed")

    _LOGGER.warning("STARLING WEBHOOK SUCCESS entry_id=%s", entry.entry_id)
    return web.Response(status=200, text="ok")