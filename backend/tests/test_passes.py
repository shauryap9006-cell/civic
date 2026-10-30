"""Tests for POST /v1/passes endpoint.

Covers T-26 (VER-01, VER-02, TC-PASS-01..04, TC-COV-10, TC-VER-10, TC-VER-15).
"""

import base64
import json
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import _build_signed_message, register_or_get_device
from app.config import Settings
from app.logic.state_machine import PotholeStatus
from app.main import app
from app.models import Pass, PassCoverage
from app.services.potholes import create_pothole
from tests.factories import make_pass


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as s:
        await s.execute(
            text(
                "TRUNCATE status_history, reports, pass_coverage, events, passes, potholes CASCADE"
            )
        )
        await s.commit()
        yield s
        await s.execute(
            text(
                "TRUNCATE status_history, reports, pass_coverage, events, passes, potholes CASCADE"
            )
        )
        await s.commit()
    await engine.dispose()


def make_signing_key() -> nacl.signing.SigningKey:
    return nacl.signing.SigningKey.generate()


def key_b64(sk: nacl.signing.SigningKey) -> str:
    return base64.b64encode(bytes(sk.verify_key)).decode("utf-8")


def sign_request(
    sk: nacl.signing.SigningKey,
    method: str,
    path: str,
    timestamp: int,
    body: bytes,
) -> str:
    msg = _build_signed_message(method, path, str(timestamp), body)
    signed = sk.sign(msg)
    return base64.b64encode(signed.signature).decode("utf-8")


def sign_envelope(sk: nacl.signing.SigningKey, payload_dict: dict) -> dict:
    payload_str = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True)
    sig = sk.sign(payload_str.encode("utf-8")).signature
    sig_b64 = base64.b64encode(sig).decode("utf-8")
    return {"payload": payload_str, "signature": sig_b64}


async def post_pass_request(
    client: AsyncClient,
    device_id: uuid.UUID,
    sk: nacl.signing.SigningKey,
    envelope: dict,
    timestamp: int | None = None,
) -> httpx.Response:
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


def make_sample_payload(
    device_id: uuid.UUID,
    pass_id: uuid.UUID | None = None,
    lat_center: float = 30.733300,
    lon_center: float = 76.779400,
    lateral_offset_m: float = 1.0,
    quality: dict | None = None,
    t0: datetime | None = None,
) -> dict:
    start_time = t0 or datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    synth = make_pass(
        pothole_lat=lat_center,
        pothole_lon=lon_center,
        pass_id=pass_id,
        device_id=device_id,
        lateral_offset_m=lateral_offset_m,
        crossing_time=start_time + timedelta(seconds=10),
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


@pytest.mark.asyncio
async def test_upload_pass_valid(session: AsyncSession):
    """TC-PASS-02: Valid pass accepted, stored in DB with PostGIS geometry."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pass_id"] == payload["id"]
    assert data["result"] == "accepted"

    # Verify pass stored in DB
    db_pass = (
        await session.execute(select(Pass).where(Pass.id == uuid.UUID(payload["id"])))
    ).scalar_one_or_none()
    assert db_pass is not None
    assert db_pass.device_id == device.id
    assert db_pass.sample_count == len(payload["samples"])
    assert db_pass.path is not None


@pytest.mark.asyncio
async def test_upload_pass_duplicate_idempotent(session: AsyncSession):
    """TC-VER-10: Ingesting the same pass ID twice returns duplicate
    and does not duplicate records.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp1 = await post_pass_request(client, device.id, sk, envelope)
            assert resp1.status_code == 200
            assert resp1.json()["result"] == "accepted"

            resp2 = await post_pass_request(client, device.id, sk, envelope)
            assert resp2.status_code == 200
            assert resp2.json()["result"] == "duplicate"
            assert resp2.json()["potholes_evaluated"] == 0
            assert resp2.json()["status_changes"] == []

    passes_count = (
        (await session.execute(select(Pass).where(Pass.id == uuid.UUID(payload["id"]))))
        .scalars()
        .all()
    )
    assert len(passes_count) == 1


@pytest.mark.asyncio
async def test_upload_pass_invalid_envelope_signature(session: AsyncSession):
    """TC-PASS-03: Tampered envelope signature rejected with 401 invalid_signature."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)
    envelope["signature"] = base64.b64encode(
        b"wrong_sig_bytes_00000000000000000000000000000000000000000000000"
    ).decode()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_upload_pass_decreasing_timestamps(session: AsyncSession):
    """TC-PASS-04: Non-monotonic sample timestamps rejected with 422 decreasing_timestamps."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    # Invert timestamps of first two samples
    t0 = payload["samples"][0]["t"]
    t1 = payload["samples"][1]["t"]
    payload["samples"][0]["t"] = t1
    payload["samples"][1]["t"] = t0

    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "decreasing_timestamps"


@pytest.mark.asyncio
async def test_upload_pass_insufficient_samples(session: AsyncSession):
    """Pass with fewer than 2 samples rejected with 422 insufficient_samples."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    payload["samples"] = payload["samples"][:1]  # Only 1 sample

    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "insufficient_samples"


@pytest.mark.asyncio
async def test_upload_pass_device_mismatch(session: AsyncSession):
    """Pass payload with mismatched device_id rejected with 422 device_mismatch."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    other_device_id = uuid.uuid4()
    payload = make_sample_payload(other_device_id)

    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "device_mismatch"


@pytest.mark.asyncio
async def test_upload_pass_invalid_time_range(session: AsyncSession):
    """Pass with ended_at < started_at rejected with 422 invalid_time_range."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    t_start = payload["started_at"]
    t_end = payload["ended_at"]
    payload["started_at"] = t_end
    payload["ended_at"] = t_start

    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "invalid_time_range"


@pytest.mark.asyncio
async def test_upload_pass_crossing_detection_and_coverage_caching(session: AsyncSession):
    """Pass driving within 1m of a pothole creates pass_coverage row with clean=True, reason=ok."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await session.commit()

    # Pass crossing at 12:00 (after claimed_fixed_at)
    payload = make_sample_payload(
        device_id=device.id,
        lat_center=30.733300,
        lon_center=76.779400,
        lateral_offset_m=1.0,
        t0=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
    )
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 200, resp.text
    res_data = resp.json()
    assert res_data["potholes_evaluated"] == 1

    # Verify pass_coverage row in DB
    pass_uuid = uuid.UUID(payload["id"])
    cov_row = (
        await session.execute(
            select(PassCoverage).where(
                PassCoverage.pass_id == pass_uuid,
                PassCoverage.pothole_id == pothole.id,
            )
        )
    ).scalar_one_or_none()

    assert cov_row is not None
    assert cov_row.clean is True
    assert cov_row.reason == "ok"
    assert cov_row.window_samples >= 3
    assert cov_row.ok_fraction >= 0.80


@pytest.mark.asyncio
async def test_upload_pass_lateral_offset_too_large(session: AsyncSession):
    """TC-COV-10, TC-VER-15: Pass driving 15m away creates NO pass_coverage row."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await session.commit()

    # Pass driving 15m away (lateral offset = 15m > 6m ceiling)
    payload = make_sample_payload(
        device_id=device.id,
        lat_center=30.733300,
        lon_center=76.779400,
        lateral_offset_m=15.0,
        t0=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
    )
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 200, resp.text
    res_data = resp.json()
    assert res_data["potholes_evaluated"] == 0

    # Verify NO pass_coverage row was created
    pass_uuid = uuid.UUID(payload["id"])
    cov_row = (
        await session.execute(
            select(PassCoverage).where(
                PassCoverage.pass_id == pass_uuid,
                PassCoverage.pothole_id == pothole.id,
            )
        )
    ).scalar_one_or_none()
    assert cov_row is None


@pytest.mark.asyncio
async def test_upload_pass_quality_honesty_unclean_coverage(session: AsyncSession):
    """TC-PASS-01, TC-COV-02: Pass with low sharpness is unclean (blurred)
    and transitions pothole to unknown (T5).
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await session.commit()

    # Blurred pass (sharpness = 10.0 < 60.0)
    payload = make_sample_payload(
        device_id=device.id,
        lat_center=30.733300,
        lon_center=76.779400,
        lateral_offset_m=1.0,
        quality={"sharpness": 10.0},
        t0=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
    )
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 200, resp.text
    res_data = resp.json()
    assert res_data["potholes_evaluated"] == 1
    assert len(res_data["status_changes"]) == 1
    assert res_data["status_changes"][0]["pothole_id"] == str(pothole.id)
    assert res_data["status_changes"][0]["from_status"] == "claimed_fixed"
    assert res_data["status_changes"][0]["to_status"] == "unknown"

    # Verify pass_coverage row has clean=False and reason=blurred
    pass_uuid = uuid.UUID(payload["id"])
    cov_row = (
        await session.execute(
            select(PassCoverage).where(
                PassCoverage.pass_id == pass_uuid,
                PassCoverage.pothole_id == pothole.id,
            )
        )
    ).scalar_one()
    assert cov_row.clean is False
    assert cov_row.reason == "blurred"
