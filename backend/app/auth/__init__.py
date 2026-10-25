"""Authentication module: device lookup and Ed25519 request signature verification.

Covers T-21 (API-02, EVT-03):
- verify_request_signature: validates X-Device-Id / X-Timestamp / X-Signature headers
- register_or_get_device: upserts a device row keyed by public_key
- get_device: fetches by device_id (raises if unknown)
"""

import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime

import nacl.exceptions
import nacl.signing
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.models import Device


def _ed25519_verify(public_key_b64: str, message_bytes: bytes, signature_b64: str) -> None:
    """Verify an Ed25519 signature.

    Raises nacl.exceptions.BadSignatureError if invalid.
    """
    key_bytes = base64.b64decode(public_key_b64)
    sig_bytes = base64.b64decode(signature_b64)
    verify_key = nacl.signing.VerifyKey(key_bytes)
    verify_key.verify(message_bytes, sig_bytes)


def verify_envelope_signature(
    public_key_b64: str,
    payload_str: str,
    signature_b64: str,
) -> bool:
    """Verify an individual envelope {payload: str, signature: str}.

    Per openapi.yaml, payload is a JSON string and signature is base64 Ed25519
    over the exact UTF-8 bytes of that string.
    Returns True if valid, False otherwise.
    """
    try:
        _ed25519_verify(public_key_b64, payload_str.encode("utf-8"), signature_b64)
        return True
    except (nacl.exceptions.BadSignatureError, Exception):
        return False


def _build_signed_message(method: str, path: str, timestamp_str: str, body_bytes: bytes) -> bytes:
    """Build the canonical byte string that the device signs.

    FORMAT (openapi.yaml auth section):
        METHOD\nPATH\nX-Timestamp\nlowercase_hex(SHA-256(body))
    """
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    return f"{method}\n{path}\n{timestamp_str}\n{body_hash}".encode()


async def verify_request_signature(
    request: Request,
    session: AsyncSession,
    tolerance_s: int,
) -> Device:
    """Verify Ed25519 device request headers; return the authenticated Device.

    Reads X-Device-Id, X-Timestamp, X-Signature from request headers.
    Uses real clock (not simulated) for timestamp tolerance per openapi.yaml spec.
    Raises HTTP 401 with a stable error code on any failure.
    """

    def _401(code: str, msg: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": code, "message": msg}},
        )

    # --- Extract headers ---
    device_id_str = request.headers.get("X-Device-Id")
    timestamp_str = request.headers.get("X-Timestamp")
    signature_b64 = request.headers.get("X-Signature")

    if not device_id_str or not timestamp_str or not signature_b64:
        raise _401("missing_auth_headers", "X-Device-Id, X-Timestamp and X-Signature are required")

    # --- Parse device id ---
    try:
        device_id = uuid.UUID(device_id_str)
    except ValueError:
        raise _401("invalid_device_id", "X-Device-Id is not a valid UUID") from None

    # --- Timestamp check (real clock only) ---
    try:
        ts_unix = int(timestamp_str)
    except ValueError:
        raise _401("invalid_timestamp", "X-Timestamp must be an integer Unix timestamp") from None

    now_real = clock.real_now()
    request_time = datetime.fromtimestamp(ts_unix, tz=UTC)
    age_s = abs((now_real - request_time).total_seconds())
    if age_s > tolerance_s:
        raise _401(
            "timestamp_out_of_range",
            f"Request timestamp is {age_s:.0f}s away from server time (tolerance: {tolerance_s}s)",
        )

    # --- Look up device ---
    device = await _get_device_by_id(session, device_id)
    if device is None:
        raise _401("unknown_device", f"No device registered with id {device_id}")

    # --- Verify Ed25519 signature ---
    body_bytes = await request.body()
    method = request.method
    path = request.url.path
    signed_message = _build_signed_message(method, path, timestamp_str, body_bytes)

    try:
        _ed25519_verify(device.public_key, signed_message, signature_b64)
    except (nacl.exceptions.BadSignatureError, Exception):
        raise _401("invalid_signature", "Ed25519 signature verification failed") from None

    # Update last_seen_at (fire and forget inside the caller's transaction)
    device.last_seen_at = now_real

    return device


async def _get_device_by_id(session: AsyncSession, device_id: uuid.UUID) -> Device | None:
    """Fetch a Device by primary key."""
    stmt = select(Device).where(Device.id == device_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_device(session: AsyncSession, device_id: uuid.UUID) -> Device:
    """Fetch a Device by ID; raise 401 unknown_device if not found."""
    device = await _get_device_by_id(session, device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {"code": "unknown_device", "message": f"Device {device_id} not found"}
            },
        )
    return device


async def register_or_get_device(
    session: AsyncSession,
    public_key_b64: str,
    platform: str,
    app_version: str | None,
    model_version: str | None,
) -> Device:
    """Register a new device or return the existing one with the same public_key.

    Upsert: if a device with this public_key already exists, return it unchanged
    (idempotent per openapi.yaml spec). Validates the public key is a valid 32-byte
    Ed25519 key before storing.
    """
    # Validate the public key early
    try:
        key_bytes = base64.b64decode(public_key_b64)
        if len(key_bytes) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        nacl.signing.VerifyKey(key_bytes)  # structural check
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "invalid_public_key", "message": str(exc)}},
        ) from exc

    # Upsert: look up by public_key
    stmt = select(Device).where(Device.public_key == public_key_b64)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    device = Device(
        public_key=public_key_b64,
        platform=platform,
        app_version=app_version,
        model_version=model_version,
    )
    session.add(device)
    await session.flush()
    return device


def derive_fixed_token(pothole_id: uuid.UUID, epoch: int, secret: str) -> str:
    """Derive an HMAC-SHA256 fixed token for the given pothole and epoch.

    Only the hex digest hash is stored in the DB (never the token itself).
    Returns the URL-safe token string (first 40 hex chars, unguessable).
    Per T-25 / API-03: fixed links use unguessable tokens; only hash stored.
    """
    msg = f"{pothole_id}:{epoch}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return digest[:40]  # 20 bytes / 160 bits of hex — plenty for a URL token


def hash_fixed_token(token: str) -> str:
    """SHA-256 hash of the token (the value stored in potholes.fixed_token_hash)."""
    return hashlib.sha256(token.encode()).hexdigest()
