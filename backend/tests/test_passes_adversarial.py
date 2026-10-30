"""Adversarial stress-test suite for pass ingestion and repair verification logic.

EMPIRICAL CHALLENGER SUITE:
Targeting:
1. Malformed / corrupted Ed25519 signatures (envelope and request headers).
2. Non-monotonic, duplicate, and backwards timestamps in 5Hz GPS sample stream.
3. GPS trajectories that loop or circle a pothole multiple times in the same pass.
4. Trajectory thresholds (max_lateral_offset_m = 6.0m, heading_diff = 35.0 deg, window 5m..25m).
5. Sudden speed jumps exceeding max_speed_mps = 25.0.
6. Sharpness drops below min_sharpness = 60.0, luma drops, occlusion, and defect priority hierarchy.
7. Concurrent pass ingestion evaluating the same pothole (PostgreSQL row-lock race conditions).
8. Reopened transitions with contradicting event confidence 0.49 (rejected) vs 0.50 (accepted).
"""

import asyncio
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
from app.config import Settings, load_thresholds
from app.logic.coverage import (
    PassQualityThresholds,
    check_sample,
    coverage_for_pass,
)
from app.logic.geo import haversine_distance_m
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.main import app
from app.models import (
    Event,
    Pass,
    PassCoverage,
    Pothole,
    StatusHistory,
    make_point_geography,
)
from app.services.passes import ingest_pass
from app.services.potholes import create_pothole, evaluate_pothole, record_status_transition
from tests.factories import SyntheticSample, make_pass


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as s:
        await s.rollback()
        await s.execute(
            text(
                "TRUNCATE status_history, reports, pass_coverage, events, passes, potholes CASCADE"
            )
        )
        await s.commit()
        try:
            yield s
        finally:
            await s.rollback()
            await s.execute(
                text("TRUNCATE status_history, reports, pass_coverage, events, passes, potholes")
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
    tampered_headers: dict | None = None,
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
    if tampered_headers:
        headers.update(tampered_headers)
    return await client.post("/v1/passes", content=body_bytes, headers=headers)


def make_sample_payload(
    device_id: uuid.UUID,
    pass_id: uuid.UUID | None = None,
    lat_center: float = 30.733300,
    lon_center: float = 76.779400,
    lateral_offset_m: float = 1.0,
    quality: dict | None = None,
    t0: datetime | None = None,
    approach_bearing_deg: float = 0.0,
    speed_mps: float = 10.0,
) -> dict:
    start_time = t0 or datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    synth = make_pass(
        pothole_lat=lat_center,
        pothole_lon=lon_center,
        pass_id=pass_id,
        device_id=device_id,
        approach_bearing_deg=approach_bearing_deg,
        lateral_offset_m=lateral_offset_m,
        speed_mps=speed_mps,
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


def make_db_event(
    device_id: uuid.UUID,
    pothole_id: uuid.UUID,
    captured_at: datetime,
    confidence: float,
    lat: float = 30.733300,
    lon: float = 76.779400,
    pass_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> Event:
    return Event(
        id=event_id or uuid.uuid4(),
        device_id=device_id,
        pass_id=pass_id,
        pothole_id=pothole_id,
        captured_at=captured_at,
        location=make_point_geography(lat, lon),
        confidence=confidence,
        track_frames=5,
        area_ratio=0.05,
        crop_key="dummy_crop_key",
        crop_sha256="dummy_crop_sha256",
        payload="{}",
        signature="dummy_sig",
    )


# ===========================================================================
# Target 1: Malformed & Corrupted Ed25519 Signatures
# ===========================================================================


@pytest.mark.asyncio
async def test_envelope_signature_invalid_base64_chars(session: AsyncSession):
    """Adversarial signature containing invalid base64 characters rejected with 401."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)
    envelope["signature"] = "NOT_VALID_BASE64!@#$%^&*()_+"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_envelope_signature_wrong_byte_length(session: AsyncSession):
    """Adversarial signature that is valid base64 but 32 bytes instead of 64 rejected with 401."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)
    envelope["signature"] = base64.b64encode(b"short_bytes_32_len_0123456789012").decode("utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_envelope_signature_corrupted_zeroed_bytes(session: AsyncSession):
    """Adversarial signature that is 64 zeroed bytes rejected with 401."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)
    envelope["signature"] = base64.b64encode(b"\x00" * 64).decode("utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_envelope_signature_cross_device_impersonation(session: AsyncSession):
    """Adversarial attack: Envelope signed with private key B submitted under Device A."""
    sk_a = make_signing_key()
    device_a = await register_or_get_device(session, key_b64(sk_a), "android")
    sk_b = make_signing_key()
    await register_or_get_device(session, key_b64(sk_b), "android")
    await session.commit()

    # Payload claims to be from device_a
    payload = make_sample_payload(device_a.id)
    # But envelope is signed with sk_b
    envelope = sign_envelope(sk_b, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device_a.id, sk_a, envelope)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


@pytest.mark.asyncio
async def test_request_header_tampered_signature(session: AsyncSession):
    """Request header X-Signature tampered by single character flip rejected with 401."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)

    tampered_sig = base64.b64encode(b"\xff" * 64).decode("utf-8")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(
                client, device.id, sk, envelope, tampered_headers={"X-Signature": tampered_sig}
            )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


# ===========================================================================
# Target 2: Non-Monotonic & Malformed Timestamps
# ===========================================================================


@pytest.mark.asyncio
async def test_samples_identical_duplicate_timestamps(session: AsyncSession):
    """Consecutive samples with identical timestamps (t[i] == t[i-1]) rejected with 422."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    # Set sample 1 timestamp equal to sample 0
    payload["samples"][1]["t"] = payload["samples"][0]["t"]
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "decreasing_timestamps"


@pytest.mark.asyncio
async def test_samples_midstream_backwards_jitter(session: AsyncSession):
    """Mid-stream GPS jitter where sample 10 has t < sample 9 rejected with 422."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    assert len(payload["samples"]) > 10
    # Jitter sample 10 backwards by 1 second relative to sample 9
    t9 = datetime.fromisoformat(payload["samples"][9]["t"])
    payload["samples"][10]["t"] = (t9 - timedelta(seconds=1)).isoformat()
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "decreasing_timestamps"


@pytest.mark.asyncio
async def test_samples_out_of_range_coordinates(session: AsyncSession):
    """Sample with latitude > 90.0 rejected with 422 invalid_coordinates."""
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    payload["samples"][0]["lat"] = 91.0
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp = await post_pass_request(client, device.id, sk, envelope)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_coordinates"


# ===========================================================================
# Target 3: Looping & Circling GPS Trajectories
# ===========================================================================


def test_looping_pass_clean_then_blurred_accepted():
    """Empirical verification: 'A pass is clean if at least one crossing is clean'.

    When clean crossing occurs first (k1) and blurred crossing occurs second (k2),
    crossing 1 evaluates clean (ok_fraction=1.0) and the entire pass is clean.
    """
    p_clean = make_pass(30.733300, 76.779400, quality={"sharpness": 100.0})
    p_blurred = make_pass(
        30.733300,
        76.779400,
        crossing_time=p_clean.ended_at + timedelta(seconds=20),
        quality={"sharpness": 20.0},
    )
    cov = coverage_for_pass(uuid.uuid4(), p_clean.samples + p_blurred.samples, 30.733300, 76.779400)
    assert cov is not None
    assert cov.clean is True
    assert cov.reason == "ok"


def test_looping_pass_blurred_then_clean_asymmetry_vulnerability():
    """VULNERABILITY DISCOVERY:

    If the blurred crossing occurs first (k1), and the clean crossing occurs second (k2),
    evaluate_crossing(k2) accumulates all prior samples from range(k2), including the
    earlier blurred samples.
    This dilutes ok_fraction and results in the entire pass being rejected as blurred,
    violating the specification that 'a pass is clean if at least one crossing is clean'.
    """
    p_blurred = make_pass(30.733300, 76.779400, quality={"sharpness": 20.0})
    p_clean = make_pass(
        30.733300,
        76.779400,
        crossing_time=p_blurred.ended_at + timedelta(seconds=20),
        quality={"sharpness": 100.0},
    )
    cov = coverage_for_pass(uuid.uuid4(), p_blurred.samples + p_clean.samples, 30.733300, 76.779400)
    assert cov is not None
    # Empirically verify that coverage logic fails due to global range(k) accumulation:
    assert cov.clean is False
    assert cov.reason == "blurred"
    assert cov.ok_fraction == 0.50  # 10 clean / 20 accumulated samples = 50% < 80% threshold


@pytest.mark.asyncio
async def test_looping_pass_detection_in_pass_overrides_clean_crossing(session: AsyncSession):
    """Contradiction check: Even if a crossing was clean, an event detected during

    the pass invalidates the entire pass (clean=False, reason="detection_in_pass").
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    t_claim = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=1),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = t_claim
    await session.commit()

    pass_id = uuid.uuid4()
    t_start = t_claim + timedelta(hours=1)

    synth = make_pass(
        pothole_lat=30.733300,
        pothole_lon=76.779400,
        pass_id=pass_id,
        device_id=device.id,
        crossing_time=t_start + timedelta(seconds=10),
        quality={"sharpness": 100.0},
    )

    # Insert an Event from this device during this pass using make_db_event
    event_time = synth.started_at + timedelta(seconds=5)
    evt = make_db_event(
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=event_time,
        confidence=0.85,
        pass_id=pass_id,
    )
    session.add(evt)
    await session.commit()

    # Pass in-memory pure evaluation
    cov = coverage_for_pass(
        pass_id=pass_id,
        samples=synth.samples,
        pothole_lat=pothole.lat,
        pothole_lon=pothole.lon,
        events_in_pass=[evt],
    )
    assert cov is not None
    assert cov.clean is False
    assert cov.reason == "detection_in_pass"


def test_tight_circle_without_approach_window_insufficient_samples():
    """A vehicle circling tightly within 4m radius never enters approach window (5m..25m).

    Empirically verifies 'clean=False, reason=insufficient_window_samples'.
    """
    pass_id = uuid.uuid4()
    pothole_lat, pothole_lon = 30.733300, 76.779400

    # Generate samples circling at constant radius of 3.5m
    t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    samples = []
    for i in range(20):
        t_i = t0 + timedelta(seconds=i * 0.2)
        # 3.5m offset
        lat_i = pothole_lat + (3.5 / 111195.0)
        lon_i = pothole_lon
        samples.append(
            SyntheticSample(
                t=t_i,
                lat=lat_i,
                lon=lon_i,
                accuracy_m=5.0,
                speed_mps=5.0,
                heading_deg=90.0,
                sharpness=100.0,
                mean_luma=100.0,
                detector_active=True,
                inference_fps=10.0,
                occlusion_ratio=0.0,
            )
        )

    cov = coverage_for_pass(
        pass_id=pass_id,
        samples=samples,
        pothole_lat=pothole_lat,
        pothole_lon=pothole_lon,
    )
    assert cov is not None
    assert cov.clean is False
    assert cov.reason == "insufficient_window_samples"


# ===========================================================================
# Target 4: Boundary Value Testing (Lateral Distance, Window, Heading)
# ===========================================================================


def test_lateral_offset_exact_boundary_6_0m_vs_6_05m():
    """Stress-test lateral offset boundary:

    At 6.00m <= max_lateral_offset_m (6.0m): Crossing registered.
    At 6.05m > max_lateral_offset_m: No crossing registered.
    """
    th = PassQualityThresholds(max_lateral_offset_m=6.0)

    # 1. At 6.0m
    synth_6m = make_pass(
        pothole_lat=30.733300,
        pothole_lon=76.779400,
        lateral_offset_m=6.0,
        approach_bearing_deg=0.0,
    )
    cov_6m = coverage_for_pass(
        pass_id=synth_6m.id,
        samples=synth_6m.samples,
        pothole_lat=30.733300,
        pothole_lon=76.779400,
        th=th,
    )
    assert cov_6m is not None, "6.0m should register a crossing"
    assert cov_6m.clean is True

    # 2. At 6.05m
    synth_6_05m = make_pass(
        pothole_lat=30.733300,
        pothole_lon=76.779400,
        lateral_offset_m=6.05,
        approach_bearing_deg=0.0,
    )
    cov_6_05m = coverage_for_pass(
        pass_id=synth_6_05m.id,
        samples=synth_6_05m.samples,
        pothole_lat=30.733300,
        pothole_lon=76.779400,
        th=th,
    )
    assert cov_6_05m is None, "6.05m must NOT register a crossing"


def test_heading_diff_exact_boundary_35_0_vs_35_1():
    """Stress-test heading difference boundary against max_heading_diff_deg = 35.0.

    When lateral offset is 0.0m along North trajectory, bearing to pothole is exactly 0.0 deg.
    Heading 35.0 deg -> difference is 35.0 deg <= 35.0 (admitted).
    Heading 35.1 deg -> difference is 35.1 deg > 35.0 (excluded).
    """
    th = PassQualityThresholds(max_heading_diff_deg=35.0, min_window_samples=3)
    pothole_lat, pothole_lon = 30.733300, 76.779400

    # Test 35.0 deg heading with lateral_offset = 0.0m
    synth_35 = make_pass(
        pothole_lat=pothole_lat,
        pothole_lon=pothole_lon,
        approach_bearing_deg=0.0,
        lateral_offset_m=0.0,
        quality={"heading_deg": 35.0},
    )
    cov_35 = coverage_for_pass(
        pass_id=synth_35.id,
        samples=synth_35.samples,
        pothole_lat=pothole_lat,
        pothole_lon=pothole_lon,
        th=th,
    )
    assert cov_35 is not None
    assert cov_35.clean is True
    assert cov_35.reason == "ok"

    # Test 35.1 deg heading with lateral_offset = 0.0m
    synth_35_1 = make_pass(
        pothole_lat=pothole_lat,
        pothole_lon=pothole_lon,
        approach_bearing_deg=0.0,
        lateral_offset_m=0.0,
        quality={"heading_deg": 35.1},
    )
    cov_35_1 = coverage_for_pass(
        pass_id=synth_35_1.id,
        samples=synth_35_1.samples,
        pothole_lat=pothole_lat,
        pothole_lon=pothole_lon,
        th=th,
    )
    assert cov_35_1 is not None
    assert cov_35_1.clean is False
    assert cov_35_1.reason == "insufficient_window_samples"


# ===========================================================================
# Target 5: Sudden Speed Spikes & Speed Threshold Boundary
# ===========================================================================


def test_speed_boundary_exact_25_0_vs_25_01():
    """Stress-test max_speed_mps = 25.0 boundary."""
    th = PassQualityThresholds(max_speed_mps=25.0)
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    sample_25 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=25.0,
        heading_deg=0.0,
        sharpness=80.0,
        mean_luma=80.0,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    ok_25, reason_25 = check_sample(sample_25, 30.7333, 76.7794, th)
    assert ok_25 is True
    assert reason_25 == "ok"

    sample_25_01 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=25.01,
        heading_deg=0.0,
        sharpness=80.0,
        mean_luma=80.0,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    ok_25_01, reason_25_01 = check_sample(sample_25_01, 30.7333, 76.7794, th)
    assert ok_25_01 is False
    assert reason_25_01 == "too_fast"


@pytest.mark.asyncio
async def test_sudden_speed_spike_causes_unclean_coverage(session: AsyncSession):
    """Pass with sudden speed spike (45 mps) dropping ok_fraction below 80%

    transitions pothole from claimed_fixed to unknown.
    """
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

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

    # Pass driving past with speed spike in approach window
    def degrade_spike(idx: int, dist: float) -> dict:
        # If in approach window (5..25m), spike speed on multiple samples
        if 5.0 <= dist <= 25.0 and idx % 2 == 0:
            return {"speed_mps": 45.0}
        return {}

    synth = make_pass(
        pothole_lat=30.733300,
        pothole_lon=76.779400,
        device_id=device.id,
        lateral_offset_m=1.0,
        crossing_time=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
        degrade=degrade_spike,
    )

    # Degrade speed to 45.0 mps on approach window samples (distance 5..25m)
    samples_list = []
    for s in synth.samples:
        d = haversine_distance_m(s.lat, s.lon, 30.733300, 76.779400)
        speed = 45.0 if 5.0 <= d <= 25.0 else s.speed_mps
        samples_list.append(
            {
                "t": s.t.isoformat(),
                "lat": s.lat,
                "lon": s.lon,
                "accuracy_m": s.accuracy_m,
                "speed_mps": speed,
                "heading_deg": s.heading_deg,
                "sharpness": s.sharpness,
                "mean_luma": s.mean_luma,
                "detector_active": s.detector_active,
                "inference_fps": s.inference_fps,
                "occlusion_ratio": s.occlusion_ratio,
            }
        )

    payload = {
        "id": str(synth.id),
        "device_id": str(device.id),
        "started_at": synth.started_at.isoformat(),
        "ended_at": synth.ended_at.isoformat(),
        "samples": samples_list,
    }
    envelope = sign_envelope(sk, payload)

    res = await ingest_pass(
        session, device, envelope["payload"], envelope["signature"], settings, thresholds
    )
    await session.commit()

    assert res["result"] == "accepted"
    assert res["potholes_evaluated"] == 1
    assert len(res["status_changes"]) == 1
    assert res["status_changes"][0]["to_status"] == "unknown"


# ===========================================================================
# Target 6: Sharpness, Luma, and Quality Defect Priorities
# ===========================================================================


def test_sharpness_boundary_exact_60_0_vs_59_99():
    """Stress-test min_sharpness = 60.0 boundary."""
    th = PassQualityThresholds(min_sharpness=60.0)
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    s_60 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=10.0,
        heading_deg=0.0,
        sharpness=60.0,
        mean_luma=80.0,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    ok_60, reason_60 = check_sample(s_60, 30.7333, 76.7794, th)
    assert ok_60 is True
    assert reason_60 == "ok"

    s_59 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=10.0,
        heading_deg=0.0,
        sharpness=59.99,
        mean_luma=80.0,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    ok_59, reason_59 = check_sample(s_59, 30.7333, 76.7794, th)
    assert ok_59 is False
    assert reason_59 == "blurred"


def test_mean_luma_boundary_exact_45_0_vs_44_99():
    """Stress-test min_mean_luma = 45.0 boundary."""
    th = PassQualityThresholds(min_mean_luma=45.0)
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    s_45 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=10.0,
        heading_deg=0.0,
        sharpness=80.0,
        mean_luma=45.0,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    ok_45, reason_45 = check_sample(s_45, 30.7333, 76.7794, th)
    assert ok_45 is True
    assert reason_45 == "ok"

    s_44 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=10.0,
        heading_deg=0.0,
        sharpness=80.0,
        mean_luma=44.99,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    ok_44, reason_44 = check_sample(s_44, 30.7333, 76.7794, th)
    assert ok_44 is False
    assert reason_44 == "too_dark"


def test_defect_priority_ordering():
    """Verify strict priority hierarchy in check_sample:

    detector_inactive > low_inference_fps > poor_gps > too_fast > blurred > too_dark > occluded.
    """
    th = PassQualityThresholds()
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    # 1. Detector inactive vs all others
    s1 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=50.0,  # poor gps
        speed_mps=50.0,  # too fast
        heading_deg=0.0,
        sharpness=10.0,  # blurred
        mean_luma=10.0,  # too dark
        detector_active=False,  # detector inactive
        inference_fps=1.0,  # low fps
        occlusion_ratio=0.9,  # occluded
    )
    _, r1 = check_sample(s1, 30.7333, 76.7794, th)
    assert r1 == "detector_inactive"

    # 2. Too fast vs blurred
    s2 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=30.0,  # too fast
        heading_deg=0.0,
        sharpness=20.0,  # blurred
        mean_luma=80.0,
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    _, r2 = check_sample(s2, 30.7333, 76.7794, th)
    assert r2 == "too_fast"

    # 3. Blurred vs too dark
    s3 = SyntheticSample(
        t=now,
        lat=30.7333,
        lon=76.7794,
        accuracy_m=5.0,
        speed_mps=10.0,
        heading_deg=0.0,
        sharpness=20.0,  # blurred
        mean_luma=20.0,  # too dark
        detector_active=True,
        inference_fps=10.0,
        occlusion_ratio=0.0,
    )
    _, r3 = check_sample(s3, 30.7333, 76.7794, th)
    assert r3 == "blurred"


# ===========================================================================
# Target 7: Concurrency & Row-Lock Stress Harness
# ===========================================================================


@pytest.mark.asyncio
async def test_concurrent_passes_evaluating_same_pothole_row_lock(session: AsyncSession):
    """Concurrency stress harness: Two passes evaluating the same pothole simultaneously.

    Verifies:
    - Postgres row locks serialize evaluate_pothole without deadlock.
    - Exactly 1 transition to VERIFIED in status_history (zero duplicate transitions).
    - Both pass_coverage rows exist.
    """
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    sk1 = make_signing_key()
    device1 = await register_or_get_device(session, key_b64(sk1), "android")
    sk2 = make_signing_key()
    device2 = await register_or_get_device(session, key_b64(sk2), "android")

    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=2),
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        at=t_claim,
    )
    await session.commit()
    pothole_id = pothole.id

    # Pass 1: Device 1 at t_claim + 1 hr
    p1_payload = make_sample_payload(
        device_id=device1.id,
        t0=t_claim + timedelta(hours=1),
    )
    p1_env = sign_envelope(sk1, p1_payload)

    # Pass 2: Device 2 at t_claim + 2 hr
    p2_payload = make_sample_payload(
        device_id=device2.id,
        t0=t_claim + timedelta(hours=2),
    )
    p2_env = sign_envelope(sk2, p2_payload)

    # Execute concurrent ingestion across two separate AsyncSession instances
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def ingest_and_commit(s, dev, env):
        res = await ingest_pass(s, dev, env["payload"], env["signature"], settings, thresholds)
        await s.commit()
        return res

    async with sessionmaker() as s1, sessionmaker() as s2:
        res1, res2 = await asyncio.gather(
            ingest_and_commit(s1, device1, p1_env),
            ingest_and_commit(s2, device2, p2_env),
        )
    await engine.dispose()

    print("CONCURRENT RES1:", res1)
    print("CONCURRENT RES2:", res2)
    assert res1["result"] == "accepted"
    assert res2["result"] == "accepted"

    # Final DB assertions: query pothole in session
    await session.commit()
    session.expire_all()
    p_check = (await session.execute(select(Pothole).where(Pothole.id == pothole_id))).scalar_one()
    print("CONCURRENT P_CHECK:", p_check.status, p_check.verified_at)
    assert p_check.status == PotholeStatus.VERIFIED
    assert p_check.verified_at is not None

    # Check status history has EXACTLY ONE clean_passes_met transition
    hist_rows = (
        (
            await session.execute(
                select(StatusHistory).where(
                    StatusHistory.pothole_id == pothole_id,
                    StatusHistory.to_status == PotholeStatus.VERIFIED,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(hist_rows) == 1, f"Expected exactly 1 transition to verified, found {len(hist_rows)}"

    # Both coverage rows exist
    cov_rows = (
        (await session.execute(select(PassCoverage).where(PassCoverage.pothole_id == pothole_id)))
        .scalars()
        .all()
    )
    assert len(cov_rows) == 2


@pytest.mark.asyncio
async def test_concurrent_duplicate_pass_submissions(session: AsyncSession):
    """Concurrency stress harness: The exact same pass is submitted simultaneously twice.

    Verifies system handles concurrent idempotent requests without corruption.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    await session.commit()

    payload = make_sample_payload(device.id)
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            resp1, resp2 = await asyncio.gather(
                post_pass_request(client, device.id, sk, envelope),
                post_pass_request(client, device.id, sk, envelope),
                return_exceptions=True,
            )

    # At least one request must succeed with 200 accepted
    responses = [r for r in [resp1, resp2] if isinstance(r, httpx.Response)]
    assert len(responses) >= 1
    status_codes = [r.status_code for r in responses]
    assert 200 in status_codes

    # Exactly one pass record in DB
    pass_rows = (
        (await session.execute(select(Pass).where(Pass.id == uuid.UUID(payload["id"]))))
        .scalars()
        .all()
    )
    assert len(pass_rows) == 1


# ===========================================================================
# Target 8: Reopened Transitions Confidence Edge Cases (0.49 vs 0.50)
# ===========================================================================


@pytest.mark.asyncio
async def test_reopen_confidence_rejected_at_0_49(session: AsyncSession):
    """A new event with confidence 0.49 (< 0.50 reopen_min_event_confidence)

    does NOT reopen a claimed_fixed pothole.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=2),
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        at=t_claim,
    )
    await session.commit()

    # Event arrives after claim with confidence 0.49
    evt_time = t_claim + timedelta(hours=1)
    evt = make_db_event(
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=evt_time,
        confidence=0.49,
    )
    session.add(evt)
    await session.commit()

    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)
    updated_pothole, decision = await evaluate_pothole(
        session=session,
        pothole_id=pothole.id,
        thresholds=thresholds,
        settings=settings,
        now=evt_time,
    )
    await session.commit()

    assert decision.status_changed is False
    assert updated_pothole.status == PotholeStatus.CLAIMED_FIXED


@pytest.mark.asyncio
async def test_reopen_confidence_accepted_at_exact_0_50(session: AsyncSession):
    """A new event with confidence 0.50 (>= 0.50 reopen_min_event_confidence)

    REOPENS a claimed_fixed pothole with reason contradicting_event.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=2),
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        at=t_claim,
    )
    initial_epoch = pothole.fixed_token_epoch
    await session.commit()

    # Event arrives after claim with confidence 0.50
    evt_time = t_claim + timedelta(hours=1)
    evt = make_db_event(
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=evt_time,
        confidence=0.50,
    )
    session.add(evt)
    await session.commit()

    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)
    updated_pothole, decision = await evaluate_pothole(
        session=session,
        pothole_id=pothole.id,
        thresholds=thresholds,
        settings=settings,
        now=evt_time,
    )
    await session.commit()

    assert decision.status_changed is True
    assert updated_pothole.status == PotholeStatus.REOPENED
    assert updated_pothole.fixed_token_epoch == initial_epoch + 1
    assert decision.reason == TransitionReason.CONTRADICTING_EVENT


@pytest.mark.asyncio
async def test_reopen_verified_pothole_float_precision_0_4999_vs_0_5000(session: AsyncSession):
    """Float precision stress-test:

    Confidence 0.4999 does NOT reopen a VERIFIED pothole.
    Confidence 0.5000 DOES reopen a VERIFIED pothole.
    """
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=2),
        initial_status=PotholeStatus.VERIFIED,
    )
    pothole.claimed_fixed_at = t_claim
    pothole.verified_at = t_claim + timedelta(hours=2)
    await session.commit()

    # 1. Event with 0.4999
    evt_low = make_db_event(
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim + timedelta(hours=3),
        confidence=0.4999,
    )
    session.add(evt_low)
    await session.commit()

    p, dec1 = await evaluate_pothole(
        session=session,
        pothole_id=pothole.id,
        thresholds=thresholds,
        settings=settings,
    )
    await session.commit()
    assert dec1.status_changed is False
    assert p.status == PotholeStatus.VERIFIED

    # 2. Event with 0.5000
    evt_high = make_db_event(
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim + timedelta(hours=4),
        confidence=0.5000,
    )
    session.add(evt_high)
    await session.commit()

    p, dec2 = await evaluate_pothole(
        session=session,
        pothole_id=pothole.id,
        thresholds=thresholds,
        settings=settings,
    )
    await session.commit()
    assert dec2.status_changed is True
    assert p.status == PotholeStatus.REOPENED
    assert dec2.reason == TransitionReason.CONTRADICTING_EVENT


@pytest.mark.asyncio
async def test_pre_claim_high_confidence_event_never_reopens(session: AsyncSession):
    """High-confidence event (0.99) captured BEFORE claimed_fixed_at does NOT reopen."""
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")

    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=2),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = t_claim
    await session.commit()

    # Pre-claim event with 0.99 arriving late
    evt_pre = make_db_event(
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim - timedelta(minutes=5),
        confidence=0.99,
    )
    session.add(evt_pre)
    await session.commit()

    p, dec = await evaluate_pothole(
        session=session,
        pothole_id=pothole.id,
        thresholds=thresholds,
        settings=settings,
    )
    await session.commit()
    assert dec.status_changed is False
    assert p.status == PotholeStatus.CLAIMED_FIXED
