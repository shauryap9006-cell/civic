"""E2E Test fixtures, signing helpers, client setup, and data generators.

Provides end-to-end test infrastructure for the CivicLens test suite.
"""

import base64
import hashlib
import json
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import _build_signed_message, register_or_get_device
from app.config import Settings, Thresholds, load_thresholds
from app.main import app
from app.models import Authority, Device
from app.storage import get_s3_client
from tests.factories import make_pass


async def _clean_transactional_data(s: AsyncSession) -> None:
    """Delete all transactional rows in order of foreign key dependencies."""
    await s.execute(text("UPDATE potholes SET before_event_id = NULL"))
    await s.execute(text("DELETE FROM status_history"))
    await s.execute(text("DELETE FROM reports"))
    await s.execute(text("DELETE FROM pass_coverage"))
    await s.execute(text("DELETE FROM events"))
    await s.execute(text("DELETE FROM passes"))
    await s.execute(text("DELETE FROM potholes"))
    await s.commit()


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """Database session fixture with clean deletion before and after test."""
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as s:
        await _clean_transactional_data(s)
        yield s
        await _clean_transactional_data(s)
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client fixture wrapped in the FastAPI application lifespan context."""
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
def settings() -> Settings:
    """Settings instance fixture."""
    return Settings()


@pytest.fixture
def thresholds(settings: Settings) -> Thresholds:
    """Loaded Thresholds instance fixture."""
    return load_thresholds(settings.THRESHOLDS_PATH)


# ---------------------------------------------------------------------------
# Cryptographic helpers
# ---------------------------------------------------------------------------


def make_signing_key() -> nacl.signing.SigningKey:
    """Generate a new Ed25519 signing key."""
    return nacl.signing.SigningKey.generate()


def key_b64(sk: nacl.signing.SigningKey) -> str:
    """Return base64-encoded verify key (public key)."""
    return base64.b64encode(bytes(sk.verify_key)).decode("utf-8")


def sign_request(
    sk: nacl.signing.SigningKey,
    method: str,
    path: str,
    timestamp: int,
    body: bytes,
) -> str:
    """Sign an HTTP request using Ed25519 per CivicLens authentication spec."""
    msg = _build_signed_message(method, path, str(timestamp), body)
    signed = sk.sign(msg)
    return base64.b64encode(signed.signature).decode("utf-8")


def sign_envelope(sk: nacl.signing.SigningKey, payload_dict: dict[str, Any]) -> dict[str, str]:
    """Wrap a payload dictionary into a signed envelope with canonical JSON serialization."""
    payload_str = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True)
    sig = sk.sign(payload_str.encode("utf-8")).signature
    sig_b64 = base64.b64encode(sig).decode("utf-8")
    return {"payload": payload_str, "signature": sig_b64}


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def put_test_crop(
    settings: Settings,
    crop_key: str,
    data: bytes = b"\xff\xd8\xfftestcrop\xff\xd9",
) -> str:
    """Upload a mock JPEG crop directly to MinIO/S3 and return its SHA-256 hex digest."""
    s3 = get_s3_client(
        settings.STORAGE_ENDPOINT,
        settings.STORAGE_ACCESS_KEY,
        settings.STORAGE_SECRET_KEY,
    )
    s3.put_object(
        Bucket=settings.STORAGE_BUCKET,
        Key=crop_key,
        Body=data,
        ContentType="image/jpeg",
    )
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# HTTP request dispatchers
# ---------------------------------------------------------------------------


async def post_pass_request(
    client: AsyncClient,
    device_id: uuid.UUID,
    sk: nacl.signing.SigningKey,
    envelope: dict[str, Any],
    timestamp: int | None = None,
) -> httpx.Response:
    """Dispatch a signed POST /v1/passes request."""
    ts = timestamp if timestamp is not None else int(time.time())
    body_bytes = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = sign_request(sk, "POST", "/v1/passes", ts, body_bytes)
    headers = {
        "X-Device-Id": str(device_id),
        "X-Timestamp": str(ts),
        "X-Signature": signature,
        "Content-Type": "application/json",
    }
    return await client.post("/v1/passes", content=body_bytes, headers=headers)


async def post_events_request(
    client: AsyncClient,
    device_id: uuid.UUID,
    sk: nacl.signing.SigningKey,
    envelopes: list[dict[str, Any]],
    timestamp: int | None = None,
) -> httpx.Response:
    """Dispatch a signed POST /v1/events batch request."""
    ts = timestamp if timestamp is not None else int(time.time())
    body_dict = {"events": envelopes}
    body_bytes = json.dumps(body_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = sign_request(sk, "POST", "/v1/events", ts, body_bytes)
    headers = {
        "X-Device-Id": str(device_id),
        "X-Timestamp": str(ts),
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
    return await client.post("/v1/events", content=body_bytes, headers=headers)


# ---------------------------------------------------------------------------
# Data seeding helpers
# ---------------------------------------------------------------------------


async def create_test_authority(
    session: AsyncSession,
    name: str = "Municipal Corporation Chandigarh",
    city: str = "Chandigarh",
    email: str = "authority@chandigarh.gov.in",
    escalation_email: str = "commissioner@chandigarh.gov.in",
) -> Authority:
    """Retrieve existing authority or seed default authority."""
    auth_res = await session.execute(select(Authority).limit(1))
    auth = auth_res.scalar_one_or_none()
    if auth is None:
        auth = Authority(
            id=uuid.uuid4(),
            name=name,
            city=city,
            email=email,
            escalation_email=escalation_email,
        )
        session.add(auth)
        await session.flush()
    return auth


async def create_test_device(
    session: AsyncSession,
    sk: nacl.signing.SigningKey | None = None,
    platform: str = "android",
) -> tuple[Device, nacl.signing.SigningKey]:
    """Register a device in the database and return (Device, SigningKey)."""
    signing_key = sk or make_signing_key()
    pub_b64 = key_b64(signing_key)
    device = await register_or_get_device(
        session=session,
        public_key_b64=pub_b64,
        platform=platform,
    )
    await session.commit()
    return device, signing_key


def make_sample_pass_payload(
    device_id: uuid.UUID,
    pass_id: uuid.UUID | None = None,
    lat_center: float = 30.733300,
    lon_center: float = 76.779400,
    lateral_offset_m: float = 1.0,
    speed_mps: float = 10.0,
    quality: dict[str, Any] | None = None,
    crossing_time: datetime | None = None,
) -> dict[str, Any]:
    """Generate a synthetically realistic pass payload driving past (lat_center, lon_center)."""
    t_crossing = crossing_time or datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    synth = make_pass(
        pothole_lat=lat_center,
        pothole_lon=lon_center,
        pass_id=pass_id,
        device_id=device_id,
        lateral_offset_m=lateral_offset_m,
        speed_mps=speed_mps,
        crossing_time=t_crossing,
        quality=quality,
    )
    samples_list = [
        {
            "t": s.t.isoformat(),
            "lat": s.lat,
            "lon": s.lon,
            "accuracy_m": s.accuracy_m,
            "speed_mps": s.speed_mps,
            "heading_deg": s.heading_deg,
            "sharpness": s.sharpness,
            "mean_luma": s.mean_luma,
            "detector_active": s.detector_active,
            "inference_fps": s.inference_fps,
            "occlusion_ratio": s.occlusion_ratio,
        }
        for s in synth.samples
    ]
    return {
        "id": str(synth.id),
        "device_id": str(device_id),
        "started_at": synth.started_at.isoformat(),
        "ended_at": synth.ended_at.isoformat(),
        "model_version": "yolov8n-cl-v1",
        "app_version": "1.0.0",
        "sample_interval_s": 0.2,
        "samples": samples_list,
    }


def make_sample_event_payload(
    device_id: uuid.UUID,
    event_id: uuid.UUID | None = None,
    lat: float = 30.733300,
    lon: float = 76.779400,
    captured_at: datetime | None = None,
    confidence: float = 0.85,
    crop_key: str = "crops/sample.jpg",
    crop_sha256: str = "0123456789abcdef" * 4,
    area_ratio: float = 0.05,
) -> dict[str, Any]:
    """Generate a sample event payload dictionary."""
    t_captured = captured_at or datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    return {
        "id": str(event_id or uuid.uuid4()),
        "device_id": str(device_id),
        "captured_at": t_captured.isoformat(),
        "lat": lat,
        "lon": lon,
        "confidence": confidence,
        "track_frames": 5,
        "crop_key": crop_key,
        "crop_sha256": crop_sha256,
        "area_ratio": area_ratio,
    }


def is_route_mounted(path: str, method: str = "GET") -> bool:
    """Check whether a given route path and HTTP method are mounted in the FastAPI app."""
    for route in app.routes:
        if getattr(route, "path", None) == path:
            if hasattr(route, "methods") and method.upper() in route.methods:
                return True
        if hasattr(route, "original_router"):
            for sub_route in getattr(route.original_router, "routes", []):
                if getattr(sub_route, "path", None) == path:
                    if hasattr(sub_route, "methods") and method.upper() in sub_route.methods:
                        return True
    return False
