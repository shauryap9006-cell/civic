"""Tests for device registration and Ed25519 request signature verification.

Covers TC-ING-01..TC-ING-06 from docs/TEST_PLAN.md §4.3.
"""

import base64
import time

import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    _build_signed_message,
    derive_fixed_token,
    hash_fixed_token,
    register_or_get_device,
    verify_envelope_signature,
    verify_request_signature,
)
from app.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_signing_key() -> nacl.signing.SigningKey:
    return nacl.signing.SigningKey.generate()


def key_b64(sk: nacl.signing.SigningKey) -> str:
    return base64.b64encode(bytes(sk.verify_key)).decode()


def sign_request(
    sk: nacl.signing.SigningKey,
    method: str,
    path: str,
    timestamp: int,
    body: bytes,
) -> str:
    msg = _build_signed_message(method, path, str(timestamp), body)
    signed = sk.sign(msg)
    # signed.signature is the 64-byte raw signature
    return base64.b64encode(signed.signature).decode()


# ---------------------------------------------------------------------------
# Device registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_device_success(db_session: AsyncSession):
    sk = make_signing_key()
    device = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="linux",
        app_version="0.1.0",
        model_version=None,
    )
    assert device.id is not None
    assert device.platform == "linux"
    assert device.public_key == key_b64(sk)


@pytest.mark.asyncio
async def test_register_device_idempotent(db_session: AsyncSession):
    sk = make_signing_key()
    d1 = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="replay",
        app_version=None,
        model_version=None,
    )
    d2 = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="replay",
        app_version=None,
        model_version=None,
    )
    assert d1.id == d2.id


@pytest.mark.asyncio
async def test_register_device_invalid_key_rejected(db_session: AsyncSession):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await register_or_get_device(
            session=db_session,
            public_key_b64=base64.b64encode(b"tooshort").decode(),
            platform="linux",
            app_version=None,
            model_version=None,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"]["code"] == "invalid_public_key"


# ---------------------------------------------------------------------------
# /v1/devices/register HTTP endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_register_device():
    sk = make_signing_key()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await client.post(
                "/v1/devices/register",
                json={
                    "public_key": key_b64(sk),
                    "platform": "linux",
                    "app_version": "0.1.0",
                },
            )
    assert resp.status_code == 201
    data = resp.json()
    assert "device_id" in data
    assert "server_time" in data


@pytest.mark.asyncio
async def test_http_register_device_invalid_platform():
    sk = make_signing_key()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await client.post(
                "/v1/devices/register",
                json={"public_key": key_b64(sk), "platform": "windows"},
            )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Signature verification — using a thin stub Request wrapper
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Minimal stub that mimics fastapi.Request for auth tests."""

    def __init__(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ):
        self.method = method
        self.url = type("URL", (), {"path": path})()
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


@pytest.mark.asyncio
async def test_TC_ING_01_altered_payload_rejected(db_session: AsyncSession):
    """TC-ING-01: Altered payload after signing → invalid_signature."""
    sk = make_signing_key()
    device = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="linux",
        app_version=None,
        model_version=None,
    )
    await db_session.flush()

    body = b'{"events": []}'
    ts = int(time.time())
    sig = sign_request(sk, "POST", "/v1/events", ts, body)

    # Alter the body after signing
    tampered_body = b'{"events": [{"id": "evil"}]}'

    from fastapi import HTTPException

    req = _FakeRequest(
        method="POST",
        path="/v1/events",
        body=tampered_body,
        headers={
            "X-Device-Id": str(device.id),
            "X-Timestamp": str(ts),
            "X-Signature": sig,
        },
    )
    with pytest.raises(HTTPException) as exc_info:
        await verify_request_signature(req, db_session, tolerance_s=300)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"]["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_TC_ING_02_stale_timestamp_rejected(db_session: AsyncSession):
    """TC-ING-02: Timestamp 10 min old → timestamp_out_of_range."""
    sk = make_signing_key()
    device = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="linux",
        app_version=None,
        model_version=None,
    )
    await db_session.flush()

    body = b"{}"
    stale_ts = int(time.time()) - 610  # 10m 10s ago
    sig = sign_request(sk, "POST", "/v1/events", stale_ts, body)

    from fastapi import HTTPException

    req = _FakeRequest(
        method="POST",
        path="/v1/events",
        body=body,
        headers={
            "X-Device-Id": str(device.id),
            "X-Timestamp": str(stale_ts),
            "X-Signature": sig,
        },
    )
    with pytest.raises(HTTPException) as exc_info:
        await verify_request_signature(req, db_session, tolerance_s=300)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"]["code"] == "timestamp_out_of_range"


@pytest.mark.asyncio
async def test_TC_ING_06_unknown_device_rejected(db_session: AsyncSession):
    """TC-ING-06: Unregistered device id → unknown_device."""
    import uuid

    sk = make_signing_key()
    body = b"{}"
    ts = int(time.time())
    sig = sign_request(sk, "POST", "/v1/events", ts, body)

    from fastapi import HTTPException

    req = _FakeRequest(
        method="POST",
        path="/v1/events",
        body=body,
        headers={
            "X-Device-Id": str(uuid.uuid4()),  # not in DB
            "X-Timestamp": str(ts),
            "X-Signature": sig,
        },
    )
    with pytest.raises(HTTPException) as exc_info:
        await verify_request_signature(req, db_session, tolerance_s=300)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"]["code"] == "unknown_device"


@pytest.mark.asyncio
async def test_valid_signature_passes(db_session: AsyncSession):
    """A correctly signed request with a registered device is accepted."""
    sk = make_signing_key()
    device = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="linux",
        app_version=None,
        model_version=None,
    )
    await db_session.flush()

    body = b'{"events": []}'
    ts = int(time.time())
    sig = sign_request(sk, "POST", "/v1/events", ts, body)

    req = _FakeRequest(
        method="POST",
        path="/v1/events",
        body=body,
        headers={
            "X-Device-Id": str(device.id),
            "X-Timestamp": str(ts),
            "X-Signature": sig,
        },
    )
    authenticated = await verify_request_signature(req, db_session, tolerance_s=300)
    assert authenticated.id == device.id


@pytest.mark.asyncio
async def test_missing_headers_rejected(db_session: AsyncSession):
    """Missing any of the three auth headers → missing_auth_headers."""
    from fastapi import HTTPException

    req = _FakeRequest(
        method="POST",
        path="/v1/events",
        body=b"{}",
        headers={},  # no auth headers
    )
    with pytest.raises(HTTPException) as exc_info:
        await verify_request_signature(req, db_session, tolerance_s=300)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"]["code"] == "missing_auth_headers"


def test_verify_envelope_signature():
    sk = make_signing_key()
    pub_b64 = key_b64(sk)

    payload = '{"id":"00000000-0000-0000-0000-000000000001","lat":51.5,"lon":-0.12}'
    payload_bytes = payload.encode("utf-8")
    sig_b64 = base64.b64encode(sk.sign(payload_bytes).signature).decode()

    # Valid signature
    assert verify_envelope_signature(pub_b64, payload, sig_b64) is True

    # Tampered payload
    tampered = '{"id":"00000000-0000-0000-0000-000000000001","lat":51.5,"lon":-0.13}'
    assert verify_envelope_signature(pub_b64, tampered, sig_b64) is False

    # Invalid signature bytes
    assert verify_envelope_signature(pub_b64, payload, "aW52YWxpZHNpZw==") is False


def test_fixed_token_derivation_and_hashing():
    import uuid

    pothole_id = uuid.uuid4()
    secret = "test-fixed-secret"

    token_epoch0 = derive_fixed_token(pothole_id, 0, secret)
    token_epoch1 = derive_fixed_token(pothole_id, 1, secret)

    assert len(token_epoch0) == 40
    assert len(token_epoch1) == 40
    assert token_epoch0 != token_epoch1

    # Deterministic
    assert derive_fixed_token(pothole_id, 0, secret) == token_epoch0

    # Hashing
    h = hash_fixed_token(token_epoch0)
    assert len(h) == 64
    assert hash_fixed_token(token_epoch0) == h
