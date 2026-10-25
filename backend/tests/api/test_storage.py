"""Tests for object storage helpers and /v1/uploads/sign endpoint.

Covers T-22 (EVT-02, TC-ING-05).
"""

import base64
import hashlib
import json
import time

import httpx
import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import _build_signed_message, register_or_get_device
from app.config import Settings
from app.main import app
from app.storage import (
    check_object_exists,
    check_storage_health,
    generate_crop_key,
    generate_presigned_get,
    generate_presigned_put,
    get_object_bytes,
    get_object_sha256,
    verify_crop,
)


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
    return base64.b64encode(signed.signature).decode()


@pytest.fixture
def settings() -> Settings:
    return Settings()


def test_storage_health(settings: Settings):
    """MinIO storage health check passes."""
    assert check_storage_health(settings) is True


@pytest.mark.asyncio
async def test_presigned_put_and_get_lifecycle(settings: Settings):
    """Test generating presigned PUT, uploading an image, verifying, and presigned GET."""
    crop_key = generate_crop_key()
    test_jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64 + b"\xff\xd9"
    expected_sha256 = hashlib.sha256(test_jpeg_bytes).hexdigest()

    # 1. Object should not exist initially
    assert check_object_exists(settings, crop_key) is False

    # 2. Generate presigned PUT
    upload_url, headers, expires_at = generate_presigned_put(
        settings, crop_key, content_type="image/jpeg", expires_in_s=600
    )
    assert upload_url.startswith("http")
    assert headers == {"Content-Type": "image/jpeg"}
    assert expires_at is not None

    # 3. Upload bytes using HTTP client
    async with httpx.AsyncClient() as client:
        put_resp = await client.put(upload_url, content=test_jpeg_bytes, headers=headers)
        assert put_resp.status_code in (200, 204)

    # 4. Verify object now exists
    assert check_object_exists(settings, crop_key) is True
    assert get_object_bytes(settings, crop_key) == test_jpeg_bytes
    assert get_object_sha256(settings, crop_key) == expected_sha256

    # 5. verify_crop helper: success, hash mismatch, missing
    ok, reason = verify_crop(settings, crop_key, expected_sha256)
    assert ok is True
    assert reason == "ok"

    bad_hash = "0" * 64
    ok_mismatch, reason_mismatch = verify_crop(settings, crop_key, bad_hash)
    assert ok_mismatch is False
    assert reason_mismatch == "crop_hash_mismatch"

    ok_missing, reason_missing = verify_crop(settings, "crops/nonexistent.jpg", expected_sha256)
    assert ok_missing is False
    assert reason_missing == "crop_missing"

    # 6. Presigned GET URL
    get_url = generate_presigned_get(settings, crop_key, expires_in_s=600)
    async with httpx.AsyncClient() as client:
        get_resp = await client.get(get_url)
        assert get_resp.status_code == 200
        assert get_resp.content == test_jpeg_bytes


@pytest.mark.asyncio
async def test_http_uploads_sign_endpoint(db_session: AsyncSession):
    """Test the POST /v1/uploads/sign endpoint end-to-end with device signature auth."""
    sk = make_signing_key()
    device = await register_or_get_device(
        session=db_session,
        public_key_b64=key_b64(sk),
        platform="android",
        app_version="1.0.0",
        model_version=None,
    )
    await db_session.commit()

    test_image_bytes = b"\xff\xd8\xfftest-crop-data\xff\xd9"
    img_sha256 = hashlib.sha256(test_image_bytes).hexdigest()

    req_body_dict = {
        "content_type": "image/jpeg",
        "sha256": img_sha256,
        "size_bytes": len(test_image_bytes),
    }
    req_body_bytes = json.dumps(req_body_dict).encode("utf-8")

    ts = int(time.time())
    sig = sign_request(sk, "POST", "/v1/uploads/sign", ts, req_body_bytes)

    headers = {
        "X-Device-Id": str(device.id),
        "X-Timestamp": str(ts),
        "X-Signature": sig,
        "Content-Type": "application/json",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await client.post("/v1/uploads/sign", content=req_body_bytes, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert "crop_key" in data
    assert "upload_url" in data
    assert data["method"] == "PUT"
    assert data["headers"] == {"Content-Type": "image/jpeg"}

    # Upload using the returned presigned URL
    async with httpx.AsyncClient() as client:
        put_resp = await client.put(
            data["upload_url"],
            content=test_image_bytes,
            headers=data["headers"],
        )
        assert put_resp.status_code in (200, 204)

    # Verify storage has the object
    settings = Settings()
    ok, reason = verify_crop(settings, data["crop_key"], img_sha256)
    assert ok is True
    assert reason == "ok"
