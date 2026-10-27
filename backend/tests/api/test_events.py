"""Tests for POST /v1/events endpoint.

Covers T-23 (EVT-01..03, MRG-01..03, TC-ING-01..06, TC-MRG-01..04).
"""

import base64
import hashlib
import json
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import httpx
import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import _build_signed_message, register_or_get_device
from app.config import Settings
from app.logic.geo import haversine_distance_m
from app.logic.merge import recompute_canonical_location
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.main import app
from app.models import Event, Pothole, Report, ReportDeliveryStatus, ReportKind
from app.services.potholes import create_pothole, record_status_transition
from app.storage import generate_crop_key, get_s3_client


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as s:
        await s.execute(text("TRUNCATE status_history, reports, events, passes, potholes CASCADE"))
        await s.commit()
        yield s
        await s.execute(text("TRUNCATE status_history, reports, events, passes, potholes CASCADE"))
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


def put_test_crop(
    settings: Settings,
    crop_key: str,
    data: bytes = b"\xff\xd8\xfftestcrop\xff\xd9",
) -> str:
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


async def post_events_request(
    client: AsyncClient,
    device_id: uuid.UUID,
    sk: nacl.signing.SigningKey,
    envelopes: list[dict],
    timestamp: int | None = None,
) -> httpx.Response:
    ts = timestamp if timestamp is not None else int(time.time())
    body_dict = {"events": envelopes}
    body_bytes = json.dumps(body_dict).encode("utf-8")
    sig = sign_request(sk, "POST", "/v1/events", ts, body_bytes)

    headers = {
        "X-Device-Id": str(device_id),
        "X-Timestamp": str(ts),
        "X-Signature": sig,
        "Content-Type": "application/json",
    }
    return await client.post("/v1/events", content=body_bytes, headers=headers)


@pytest.mark.asyncio
async def test_events_tc_ing_01_invalid_signature(session: AsyncSession):
    """TC-ING-01: Event payload altered after envelope signing -> rejected, invalid_signature."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    crop_key = generate_crop_key(device_id=device.id)
    sha256 = put_test_crop(settings, crop_key)
    event_id = uuid.uuid4()

    payload = {
        "id": str(event_id),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": crop_key,
        "crop_sha256": sha256,
        "area_ratio": 0.05,
    }
    env = sign_envelope(sk, payload)
    # Tamper with the payload string after signing
    env["payload"] = env["payload"].replace("0.85", "0.99")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env])

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    res = data["results"][0]
    assert res["event_id"] == str(event_id)
    assert res["result"] == "rejected"
    assert res["reason"] == "invalid_signature"

    # Verify nothing was stored in events
    stored = await session.execute(select(Event).where(Event.id == event_id))
    assert stored.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_events_tc_ing_02_timestamp_out_of_range(session: AsyncSession):
    """TC-ING-02: Request timestamp 10 minutes old (tolerance 300 s) -> 401."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    crop_key = generate_crop_key(device_id=device.id)
    sha256 = put_test_crop(settings, crop_key)
    event_id = uuid.uuid4()

    payload = {
        "id": str(event_id),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": crop_key,
        "crop_sha256": sha256,
    }
    env = sign_envelope(sk, payload)

    # 10 minutes old
    old_ts = int(time.time()) - 600

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env], timestamp=old_ts)

    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "timestamp_out_of_range"


@pytest.mark.asyncio
async def test_events_tc_ing_03_duplicate_event_id(session: AsyncSession):
    """TC-ING-03: Same event id uploaded twice -> second is duplicate; one row in events."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    crop_key = generate_crop_key(device_id=device.id)
    sha256 = put_test_crop(settings, crop_key)
    event_id = uuid.uuid4()

    payload = {
        "id": str(event_id),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": crop_key,
        "crop_sha256": sha256,
    }
    env = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # First upload: accepted
            resp1 = await post_events_request(client, device.id, sk, [env])
            assert resp1.status_code == 200
            res1 = resp1.json()["results"][0]
            assert res1["result"] == "accepted"

            # Second upload: duplicate
            resp2 = await post_events_request(client, device.id, sk, [env])
            assert resp2.status_code == 200
            res2 = resp2.json()["results"][0]
            assert res2["result"] == "duplicate"
            assert res2["event_id"] == str(event_id)

    # Check only one row in events
    events_res = await session.execute(select(Event).where(Event.id == event_id))
    assert len(events_res.scalars().all()) == 1


@pytest.mark.asyncio
async def test_events_tc_ing_04_device_mismatch(session: AsyncSession):
    """TC-ING-04: Payload device_id differs from X-Device-Id -> rejected, device_mismatch."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    crop_key = generate_crop_key(device_id=device.id)
    sha256 = put_test_crop(settings, crop_key)
    event_id = uuid.uuid4()

    payload = {
        "id": str(event_id),
        "device_id": str(uuid.uuid4()),  # Mismatched device id!
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": crop_key,
        "crop_sha256": sha256,
    }
    env = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env])

    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["result"] == "rejected"
    assert res["reason"] == "device_mismatch"


@pytest.mark.asyncio
async def test_events_tc_ing_05_crop_missing_and_mismatch(session: AsyncSession):
    """TC-ING-05: Crop not in storage -> crop_missing; wrong SHA-256 -> crop_hash_mismatch."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    # Case A: Crop not in storage
    missing_key = f"crops/{uuid.uuid4()}.jpg"
    payload_missing = {
        "id": str(uuid.uuid4()),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": missing_key,
        "crop_sha256": "a" * 64,
    }
    env_missing = sign_envelope(sk, payload_missing)

    # Case B: Crop exists, wrong hash
    real_key = generate_crop_key(device_id=device.id)
    put_test_crop(settings, real_key)
    payload_mismatch = {
        "id": str(uuid.uuid4()),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": real_key,
        "crop_sha256": "0" * 64,
    }
    env_mismatch = sign_envelope(sk, payload_mismatch)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env_missing, env_mismatch])

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert results[0]["result"] == "rejected"
    assert results[0]["reason"] == "crop_missing"
    assert results[1]["result"] == "rejected"
    assert results[1]["reason"] == "crop_hash_mismatch"


@pytest.mark.asyncio
async def test_events_tc_ing_06_unregistered_device():
    """TC-ING-06: Unregistered device -> 401 unknown_device."""
    sk = make_signing_key()
    random_device_id = uuid.uuid4()
    payload = {
        "id": str(uuid.uuid4()),
        "device_id": str(random_device_id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": 12.9716,
        "lon": 77.5946,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": "crops/test.jpg",
        "crop_sha256": "0" * 64,
    }
    env = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, random_device_id, sk, [env])

    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "unknown_device"


@pytest.mark.asyncio
async def test_events_tc_mrg_01_and_04_merge_and_canonical_location(session: AsyncSession):
    """TC-MRG-01 & TC-MRG-04:

    Event 4 m from an existing pothole merges; event_count = 2.
    Location equals confidence-weighted mean (within 0.01 m).
    Initial report is queued with pending status.
    """
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    lat1, lon1, conf1 = 12.971600, 77.594600, 0.80
    crop_key1 = generate_crop_key(device_id=device.id)
    sha256_1 = put_test_crop(settings, crop_key1)
    event_id1 = uuid.uuid4()

    payload1 = {
        "id": str(event_id1),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": lat1,
        "lon": lon1,
        "confidence": conf1,
        "track_frames": 5,
        "crop_key": crop_key1,
        "crop_sha256": sha256_1,
        "area_ratio": 0.05,
    }
    env1 = sign_envelope(sk, payload1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp1 = await post_events_request(client, device.id, sk, [env1])
            assert resp1.status_code == 200
            res1 = resp1.json()["results"][0]
            assert res1["result"] == "accepted"
            pothole_id = uuid.UUID(res1["pothole_id"])

            # 4 meters north: delta_lat ~ 4 / 111000 ~ 0.00003603
            lat2, lon2, conf2 = 12.971636, 77.594600, 0.60
            assert 3.5 <= haversine_distance_m(lat1, lon1, lat2, lon2) <= 4.5

            crop_key2 = generate_crop_key(device_id=device.id)
            sha256_2 = put_test_crop(settings, crop_key2)
            event_id2 = uuid.uuid4()

            payload2 = {
                "id": str(event_id2),
                "device_id": str(device.id),
                "captured_at": datetime.now(UTC).isoformat(),
                "lat": lat2,
                "lon": lon2,
                "confidence": conf2,
                "track_frames": 4,
                "crop_key": crop_key2,
                "crop_sha256": sha256_2,
                "area_ratio": 0.04,
            }
            env2 = sign_envelope(sk, payload2)

            resp2 = await post_events_request(client, device.id, sk, [env2])
            assert resp2.status_code == 200
            res2 = resp2.json()["results"][0]
            assert res2["result"] == "accepted"
            assert res2["pothole_id"] == str(pothole_id)

    # Verify pothole in DB
    pothole_res = await session.execute(select(Pothole).where(Pothole.id == pothole_id))
    pothole = pothole_res.scalar_one()
    assert pothole.event_count == 2

    # Check canonical location
    expected_lat, expected_lon = recompute_canonical_location(
        [(lat1, lon1, conf1), (lat2, lon2, conf2)]
    )
    dist_error = haversine_distance_m(pothole.lat, pothole.lon, expected_lat, expected_lon)
    assert dist_error < 0.01

    # Check initial pending report queued
    reports_res = await session.execute(select(Report).where(Report.pothole_id == pothole_id))
    reports = reports_res.scalars().all()
    assert len(reports) == 1
    assert reports[0].kind == ReportKind.INITIAL
    assert reports[0].status == ReportDeliveryStatus.PENDING


@pytest.mark.asyncio
async def test_events_tc_mrg_02_outside_merge_radius(session: AsyncSession):
    """TC-MRG-02: Event 14 m away (merge radius 10 m) creates a new pothole."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    lat1, lon1 = 12.971600, 77.594600
    crop_key1 = generate_crop_key(device_id=device.id)
    sha256_1 = put_test_crop(settings, crop_key1)

    payload1 = {
        "id": str(uuid.uuid4()),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": lat1,
        "lon": lon1,
        "confidence": 0.8,
        "track_frames": 5,
        "crop_key": crop_key1,
        "crop_sha256": sha256_1,
    }
    env1 = sign_envelope(sk, payload1)

    # 14 meters away: delta_lat ~ 14 / 111000 ~ 0.0001261
    lat2, lon2 = 12.971726, 77.594600
    assert 13.5 <= haversine_distance_m(lat1, lon1, lat2, lon2) <= 14.5

    crop_key2 = generate_crop_key(device_id=device.id)
    sha256_2 = put_test_crop(settings, crop_key2)

    payload2 = {
        "id": str(uuid.uuid4()),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": lat2,
        "lon": lon2,
        "confidence": 0.8,
        "track_frames": 5,
        "crop_key": crop_key2,
        "crop_sha256": sha256_2,
    }
    env2 = sign_envelope(sk, payload2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env1, env2])

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert results[0]["result"] == "accepted"
    assert results[1]["result"] == "accepted"
    assert results[0]["pothole_id"] != results[1]["pothole_id"]


@pytest.mark.asyncio
async def test_events_tc_mrg_03_equidistant_tie_break(session: AsyncSession):
    """TC-MRG-03: Event equidistant between two potholes joins with deterministic tie-break."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )
    await session.commit()

    # Pre-create Pothole A and Pothole B symmetrically placed around (12.971600, 77.594600)
    # Distance between A and B is 8m (radius is 10m).
    # Center point is exactly 4m from both A and B.
    # Pothole A is created earlier, Pothole B created later.
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)
    pothole_a = await create_pothole(
        session=session,
        lat=12.971564,  # 4m south of center
        lon=77.594600,
        first_seen_at=t0,
    )
    pothole_b = await create_pothole(
        session=session,
        lat=12.971636,  # 4m north of center
        lon=77.594600,
        first_seen_at=t0,
    )
    await session.commit()

    center_lat, center_lon = 12.971600, 77.594600
    crop_key = generate_crop_key(device_id=device.id)
    sha256 = put_test_crop(settings, crop_key)

    payload = {
        "id": str(uuid.uuid4()),
        "device_id": str(device.id),
        "captured_at": datetime.now(UTC).isoformat(),
        "lat": center_lat,
        "lon": center_lon,
        "confidence": 0.85,
        "track_frames": 5,
        "crop_key": crop_key,
        "crop_sha256": sha256,
    }
    env = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env])

    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["result"] == "accepted"
    # Merges into one of the two potholes deterministically
    assert res["pothole_id"] in (str(pothole_a.id), str(pothole_b.id))


@pytest.mark.asyncio
async def test_events_contradicting_event_reopens_pothole(session: AsyncSession):
    """Contradiction check (T7): Contradicting event after claim reopens pothole."""
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(
        session=session,
        public_key_b64=key_b64(sk),
        platform="android",
    )

    t_first = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=12.971600,
        lon=77.594600,
        first_seen_at=t_first,
    )

    # Initial report sent -> transitions open to reported
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT,
        at=t_first,
    )

    # Authority claims fixed at t_claim
    t_claim = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        at=t_claim,
    )
    await session.commit()

    # Event captured at t_event > t_claim with high confidence (>= 0.80)
    t_event = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    crop_key = generate_crop_key(device_id=device.id)
    sha256 = put_test_crop(settings, crop_key)
    event_id = uuid.uuid4()

    payload = {
        "id": str(event_id),
        "device_id": str(device.id),
        "captured_at": t_event.isoformat(),
        "lat": 12.971600,
        "lon": 77.594600,
        "confidence": 0.88,
        "track_frames": 6,
        "crop_key": crop_key,
        "crop_sha256": sha256,
        "area_ratio": 0.08,
    }
    env = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_events_request(client, device.id, sk, [env])

    assert resp.status_code == 200
    res = resp.json()["results"][0]
    assert res["result"] == "accepted"
    assert res["pothole_id"] == str(pothole.id)
    assert res["pothole_status"] == PotholeStatus.REOPENED.value

    pothole_id = pothole.id
    pothole_db = (
        await session.execute(
            select(Pothole)
            .where(Pothole.id == pothole_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert pothole_db.status == PotholeStatus.REOPENED
    assert pothole_db.reopened_at is not None

    # Verify a pending reopened report was queued
    reopened_report = (
        await session.execute(
            select(Report).where(
                Report.pothole_id == pothole.id,
                Report.kind == ReportKind.REOPENED,
                Report.status == ReportDeliveryStatus.PENDING,
            )
        )
    ).scalar_one_or_none()
    assert reopened_report is not None
