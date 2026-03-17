import base64
import json
from unittest.mock import AsyncMock

from aiohttp.test_utils import make_mocked_request
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_components.starlingbank_enhanced.const import (
    CONF_WEBHOOK_LAST_RECEIVED,
    CONF_WEBHOOK_PUBLIC_KEY,
    COORDINATOR,
    DOMAIN,
    WEBHOOK_SIGNATURE_HEADER,
)
from custom_components.starlingbank_enhanced.webhook import (
    _verify_starling_signature,
    async_handle_webhook,
)


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_key_pem


def _sign_body(private_key, body: bytes) -> str:
    signature = private_key.sign(body, padding.PKCS1v15(), hashes.SHA512())
    return base64.b64encode(signature).decode()


async def test_webhook_valid_payload_triggers_refresh(hass, mock_config_entry):
    coordinator = AsyncMock()
    private_key, public_key_pem = _generate_keypair()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: coordinator,
        "last_webhook_monotonic": None,
    }
    mock_config_entry.options = {CONF_WEBHOOK_PUBLIC_KEY: public_key_pem}

    request = make_mocked_request("POST", "/api/webhook/test")
    body = {
        "webhookNotificationUid": "123",
        "type": "TRANSACTION_FEED_ITEM",
        "timestamp": "2026-03-15T12:00:00Z",
    }
    body_bytes = json.dumps(body).encode()

    async def _read():
        return body_bytes
    request.read = _read
    request.headers[WEBHOOK_SIGNATURE_HEADER] = _sign_body(private_key, body_bytes)

    response = await async_handle_webhook(hass, mock_config_entry, request)

    assert response.status == 200
    assert (
        hass.data[DOMAIN][mock_config_entry.entry_id][CONF_WEBHOOK_LAST_RECEIVED]
        == "2026-03-15T12:00:00Z"
    )
    assert coordinator.async_request_refresh.called


async def test_webhook_invalid_json_returns_400(hass, mock_config_entry):
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: AsyncMock(),
        "last_webhook_monotonic": None,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _read():
        return b"{bad json"
    request.read = _read

    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 400


async def test_webhook_debounce_returns_202(hass, mock_config_entry):
    coordinator = AsyncMock()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: coordinator,
        "last_webhook_monotonic": 999999999.0,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        return {
            "webhookNotificationUid": "123",
            "type": "TRANSACTION_FEED_ITEM",
        }

    request.json = _json

    from custom_components.starlingbank_enhanced import webhook as webhook_module

    original = webhook_module.monotonic
    webhook_module.monotonic = lambda: 999999999.1
    try:
        response = await async_handle_webhook(hass, mock_config_entry, request)
    finally:
        webhook_module.monotonic = original

    assert response.status == 202
    assert not coordinator.async_request_refresh.called


async def test_webhook_without_coordinator_returns_503(hass, mock_config_entry):
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        "last_webhook_monotonic": None,
    }

    request = make_mocked_request("POST", "/api/webhook/test")

    async def _json():
        return {
            "webhookNotificationUid": "123",
            "type": "TRANSACTION_FEED_ITEM",
        }

    request.json = _json

    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 503


async def test_webhook_invalid_payload_returns_400(hass, mock_config_entry):
    private_key, public_key_pem = _generate_keypair()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: AsyncMock(),
        "last_webhook_monotonic": None,
    }
    mock_config_entry.options = {CONF_WEBHOOK_PUBLIC_KEY: public_key_pem}

    request = make_mocked_request("POST", "/api/webhook/test")

    body = {"foo": "bar"}
    body_bytes = json.dumps(body).encode()
    async def _read():
        return body_bytes
    request.read = _read
    request.headers[WEBHOOK_SIGNATURE_HEADER] = _sign_body(private_key, body_bytes)

    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 400


async def test_verify_signature_success_and_failure(mock_config_entry):
    private_key, public_key_pem = _generate_keypair()
    mock_config_entry.options = {CONF_WEBHOOK_PUBLIC_KEY: public_key_pem}
    body = b'{"type":"TRANSACTION_FEED_ITEM","webhookNotificationUid":"abc"}'
    ok_sig = _sign_body(private_key, body)

    assert _verify_starling_signature(mock_config_entry, ok_sig, body) is True
    assert _verify_starling_signature(mock_config_entry, ok_sig, body + b"x") is False
    assert _verify_starling_signature(mock_config_entry, "%%%not-base64%%%", body) is False


async def test_webhook_replay_duplicate_returns_202(hass, mock_config_entry):
    coordinator = AsyncMock()
    private_key, public_key_pem = _generate_keypair()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: coordinator,
        "last_webhook_monotonic": None,
    }
    mock_config_entry.options = {CONF_WEBHOOK_PUBLIC_KEY: public_key_pem}

    body = {
        "webhookNotificationUid": "same-nonce",
        "type": "TRANSACTION_FEED_ITEM",
        "timestamp": "2026-03-15T12:00:00Z",
    }
    body_bytes = json.dumps(body).encode()
    sig = _sign_body(private_key, body_bytes)

    request1 = make_mocked_request("POST", "/api/webhook/test")
    request2 = make_mocked_request("POST", "/api/webhook/test")

    async def _read():
        return body_bytes

    request1.read = _read
    request1.headers[WEBHOOK_SIGNATURE_HEADER] = sig
    request2.read = _read
    request2.headers[WEBHOOK_SIGNATURE_HEADER] = sig

    response1 = await async_handle_webhook(hass, mock_config_entry, request1)
    # Move time outside debounce but keep inside replay window.
    hass.data[DOMAIN][mock_config_entry.entry_id]["last_webhook_monotonic"] = -999999
    response2 = await async_handle_webhook(hass, mock_config_entry, request2)

    assert response1.status == 200
    assert response2.status == 202


async def test_webhook_missing_or_bad_signature_returns_401(hass, mock_config_entry):
    private_key, public_key_pem = _generate_keypair()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][mock_config_entry.entry_id] = {
        COORDINATOR: AsyncMock(),
        "last_webhook_monotonic": None,
    }
    mock_config_entry.options = {CONF_WEBHOOK_PUBLIC_KEY: public_key_pem}
    body_bytes = json.dumps({"webhookNotificationUid": "123", "type": "TRANSACTION_FEED_ITEM"}).encode()
    request = make_mocked_request("POST", "/api/webhook/test")
    async def _read():
        return body_bytes
    request.read = _read
    request.headers[WEBHOOK_SIGNATURE_HEADER] = base64.b64encode(b"bad-signature").decode()
    response = await async_handle_webhook(hass, mock_config_entry, request)
    assert response.status == 401