"""Challenger stress-test suite for Milestone 1 (T-26 Pass Ingest and T-27 Verification).

Tests:
1. High-concurrency drive uploads (rapid simultaneous uploads & concurrent duplicate submissions).
2. Trajectory candidate buffer (40.9m vs 41.1m) and lateral offset boundary (5.9m vs 6.1m).
3. Timestamp boundary conditions (ended_at == started_at, minimal 2 samples, strict ordering).
4. Reopened transitions: 0.4999 vs 0.5000 vs 0.5001, and captured_at == claimed_fixed_at.
5. Pre-claim passes strict isolation from post-claim verification.
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
from app.config import Settings
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.main import app
from app.models import Event, Pass, PassCoverage, Pothole, StatusHistory
from app.services.potholes import create_pothole, evaluate_pothole
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
    speed_mps: float = 10.0,
    start_distance_m: float = 60.0,
    end_distance_m: float = 20.0,
) -> dict:
    start_time = t0 or datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    synth = make_pass(
        pothole_lat=lat_center,
        pothole_lon=lon_center,
        pass_id=pass_id,
        device_id=device_id,
        lateral_offset_m=lateral_offset_m,
        speed_mps=speed_mps,
        start_distance_m=start_distance_m,
        end_distance_m=end_distance_m,
        crossing_time=start_time + timedelta(seconds=int(start_distance_m / speed_mps)),
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
        "device_id": str(synth.device_id),
        "started_at": synth.started_at.isoformat(),
        "ended_at": synth.ended_at.isoformat(),
        "model_version": "yolo-v1",
        "app_version": "1.0.0",
        "sample_interval_s": 0.2,
        "samples": samples_list,
    }


# ===========================================================================
# 1. High-concurrency drive uploads
# ===========================================================================


@pytest.mark.asyncio
async def test_stress_high_concurrency_distinct_clean_passes(session: AsyncSession):
    """Stress test: 5 distinct clean passes uploaded simultaneously via asyncio.gather.

    Verifies:
    - All 5 passes are accepted (HTTP 200).
    - PostGIS row locks correctly serialize evaluation without deadlocks or StaleDataErrors.
    - Pothole transitions to VERIFIED.
    - Status history has exactly one VERIFIED transition (clean_passes_met).
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await session.commit()
    pothole_id = pothole.id

    # Prepare 5 distinct clean passes crossing at different times after claim
    envelopes = []
    for i in range(5):
        t0 = datetime(2026, 10, 25, 12, i * 5, 0, tzinfo=UTC)
        payload = make_sample_payload(
            device_id=device_id,
            lat_center=30.733300,
            lon_center=76.779400,
            lateral_offset_m=1.0,
            t0=t0,
        )
        envelopes.append(sign_envelope(sk, payload))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            tasks = [post_pass_request(client, device_id, sk, env) for env in envelopes]
            responses = await asyncio.gather(*tasks)

    # Assert all responses are HTTP 200 and accepted
    for idx, resp in enumerate(responses):
        assert resp.status_code == 200, f"Pass {idx} failed with {resp.text}"
        assert resp.json()["result"] == "accepted"

    # Re-query pothole and history with populate_existing=True to bypass session identity map
    res_p = await session.execute(
        select(Pothole).where(Pothole.id == pothole_id).execution_options(populate_existing=True)
    )
    pothole_db = res_p.scalar_one()
    assert pothole_db.status == PotholeStatus.VERIFIED

    res_h = await session.execute(
        select(StatusHistory).where(
            StatusHistory.pothole_id == pothole_id,
            StatusHistory.to_status == PotholeStatus.VERIFIED,
        )
    )
    verified_histories = res_h.scalars().all()
    # Must have transitioned to verified exactly ONCE (idempotent, no duplicate transitions)
    assert len(verified_histories) == 1
    assert verified_histories[0].reason == TransitionReason.CLEAN_PASSES_MET


@pytest.mark.asyncio
async def test_stress_high_concurrency_identical_pass_idempotency(session: AsyncSession):
    """Stress test: 5 simultaneous requests uploading the EXACT SAME pass_id.

    Verifies:
    - Exactly one returns 'accepted'.
    - Remainder return 'duplicate'.
    - Exactly 1 row in `passes` table.
    - No integrity constraint violations or deadlocks.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id
    await session.commit()

    shared_pass_id = uuid.uuid4()
    payload = make_sample_payload(
        device_id=device_id,
        pass_id=shared_pass_id,
        lat_center=30.733300,
        lon_center=76.779400,
    )
    envelope = sign_envelope(sk, payload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            tasks = [post_pass_request(client, device_id, sk, envelope) for _ in range(5)]
            responses = await asyncio.gather(*tasks)

    results = []
    for resp in responses:
        assert resp.status_code == 200, resp.text
        results.append(resp.json()["result"])

    assert results.count("accepted") == 1
    assert results.count("duplicate") == 4

    # Verify DB has exactly 1 row
    res_passes = await session.execute(select(Pass).where(Pass.id == shared_pass_id))
    assert len(res_passes.scalars().all()) == 1


# ===========================================================================
# 2. GPS trajectories that graze candidate buffer
# ===========================================================================


@pytest.mark.asyncio
async def test_stress_trajectory_buffer_grazing_40_9m_vs_41_1m(session: AsyncSession):
    """Stress test: trajectory grazing spatial candidate buffer (41.0m candidate radius).

    Candidate radius in passes.py is:
    window_max_m (25) + max_lateral_offset_m (6) + 10m margin = 41.0m.
    - Trajectory at 40.9m lateral offset: caught by PostGIS ST_DWithin candidate query,
      but coverage_for_pass finds lateral distance > 6.0m, returns None (no crossing).
      potholes_evaluated == 0, no pass_coverage row created.
    - Trajectory at 41.1m lateral offset: filtered out directly by PostGIS ST_DWithin.
      potholes_evaluated == 0, no pass_coverage row created.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # Test 40.9m lateral offset
            pass_40_9_id = uuid.uuid4()
            payload_40_9 = make_sample_payload(
                device_id=device_id,
                pass_id=pass_40_9_id,
                lat_center=30.733300,
                lon_center=76.779400,
                lateral_offset_m=40.9,
                t0=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
            )
            resp1 = await post_pass_request(client, device_id, sk, sign_envelope(sk, payload_40_9))
            assert resp1.status_code == 200
            assert resp1.json()["potholes_evaluated"] == 0

            # Test 41.1m lateral offset
            pass_41_1_id = uuid.uuid4()
            payload_41_1 = make_sample_payload(
                device_id=device_id,
                pass_id=pass_41_1_id,
                lat_center=30.733300,
                lon_center=76.779400,
                lateral_offset_m=41.1,
                t0=datetime(2026, 10, 25, 12, 10, 0, tzinfo=UTC),
            )
            resp2 = await post_pass_request(client, device_id, sk, sign_envelope(sk, payload_41_1))
            assert resp2.status_code == 200
            assert resp2.json()["potholes_evaluated"] == 0

    # Verify zero coverage rows created in DB for both passes
    res_cov = await session.execute(
        select(PassCoverage).where(PassCoverage.pass_id.in_([pass_40_9_id, pass_41_1_id]))
    )
    assert len(res_cov.scalars().all()) == 0


@pytest.mark.asyncio
async def test_stress_lateral_offset_boundary_5_9m_vs_6_1m(session: AsyncSession):
    """Stress test: lateral offset threshold boundary at exactly 6.0m.

    - 5.9m (<= 6.0m): Crossing registered! Evaluates approach window and saves pass_coverage.
    - 6.1m (> 6.0m): Exceeds max_lateral_offset_m. No crossing registered, coverage is None.
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    pothole.claimed_fixed_at = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # 5.9m: Valid crossing
            pass_5_9_id = uuid.uuid4()
            payload_5_9 = make_sample_payload(
                device_id=device_id,
                pass_id=pass_5_9_id,
                lat_center=30.733300,
                lon_center=76.779400,
                lateral_offset_m=5.9,
                t0=datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC),
            )
            resp_5_9 = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_5_9)
            )
            assert resp_5_9.status_code == 200
            assert resp_5_9.json()["potholes_evaluated"] == 1

            # 6.1m: Invalid crossing (too far laterally)
            pass_6_1_id = uuid.uuid4()
            payload_6_1 = make_sample_payload(
                device_id=device_id,
                pass_id=pass_6_1_id,
                lat_center=30.733300,
                lon_center=76.779400,
                lateral_offset_m=6.1,
                t0=datetime(2026, 10, 25, 12, 10, 0, tzinfo=UTC),
            )
            resp_6_1 = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_6_1)
            )
            assert resp_6_1.status_code == 200
            assert resp_6_1.json()["potholes_evaluated"] == 0

    # In DB, pass_5_9 must have a PassCoverage row, pass_6_1 must NOT
    res_5_9 = await session.execute(select(PassCoverage).where(PassCoverage.pass_id == pass_5_9_id))
    assert res_5_9.scalar_one_or_none() is not None

    res_6_1 = await session.execute(select(PassCoverage).where(PassCoverage.pass_id == pass_6_1_id))
    assert res_6_1.scalar_one_or_none() is None


# ===========================================================================
# 3. Timestamp boundary conditions
# ===========================================================================


@pytest.mark.asyncio
async def test_stress_timestamp_boundaries(session: AsyncSession):
    """Stress test: Pass timestamp edge cases:

    1. ended_at == started_at with 2 strictly increasing samples (t0 < t1):
       Validates whether schema allows ended_at == started_at or expects ended_at >= started_at.
    2. ended_at == started_at with t0 == t1:
       Must fail with HTTP 422 'decreasing_timestamps'.
    3. ended_at < started_at:
       Must fail with HTTP 422 'invalid_time_range'.
    4. Minimal valid 2-sample pass: started_at = t0, ended_at = t1.
       Must be accepted (HTTP 200).
    """
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id
    await session.commit()

    base_sample = {
        "lat": 30.733300,
        "lon": 76.779400,
        "accuracy_m": 5.0,
        "speed_mps": 10.0,
        "heading_deg": 0.0,
        "sharpness": 100.0,
        "mean_luma": 100.0,
        "detector_active": True,
        "inference_fps": 10.0,
        "occlusion_ratio": 0.0,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # Case 1: ended_at == started_at, but samples t0 < t1
            t_fixed = "2026-10-25T12:00:00Z"
            payload_equal_time = {
                "id": str(uuid.uuid4()),
                "device_id": str(device_id),
                "started_at": t_fixed,
                "ended_at": t_fixed,
                "samples": [
                    {"t": "2026-10-25T12:00:00Z", **base_sample},
                    {"t": "2026-10-25T12:00:00.200Z", **base_sample},
                ],
            }
            resp_equal = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_equal_time)
            )
            # Both started_at and ended_at are parsed, ended_at < started_at is false.
            assert resp_equal.status_code == 200
            assert resp_equal.json()["result"] == "accepted"

            # Case 2: ended_at == started_at and t0 == t1
            payload_decreasing = {
                "id": str(uuid.uuid4()),
                "device_id": str(device_id),
                "started_at": t_fixed,
                "ended_at": t_fixed,
                "samples": [
                    {"t": "2026-10-25T12:00:00Z", **base_sample},
                    {"t": "2026-10-25T12:00:00Z", **base_sample},
                ],
            }
            resp_dec = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_decreasing)
            )
            assert resp_dec.status_code == 422
            assert resp_dec.json()["error"]["code"] == "decreasing_timestamps"

            # Case 3: ended_at < started_at
            payload_inverted = {
                "id": str(uuid.uuid4()),
                "device_id": str(device_id),
                "started_at": "2026-10-25T12:01:00Z",
                "ended_at": "2026-10-25T12:00:00Z",
                "samples": [
                    {"t": "2026-10-25T12:00:00Z", **base_sample},
                    {"t": "2026-10-25T12:00:01Z", **base_sample},
                ],
            }
            resp_inv = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_inverted)
            )
            assert resp_inv.status_code == 422
            assert resp_inv.json()["error"]["code"] == "invalid_time_range"

            # Case 4: Minimal valid 2-sample pass: started_at = t0, ended_at = t1
            payload_minimal = {
                "id": str(uuid.uuid4()),
                "device_id": str(device_id),
                "started_at": "2026-10-25T12:00:00Z",
                "ended_at": "2026-10-25T12:00:00.200Z",
                "samples": [
                    {"t": "2026-10-25T12:00:00Z", **base_sample},
                    {"t": "2026-10-25T12:00:00.200Z", **base_sample},
                ],
            }
            resp_min = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_minimal)
            )
            assert resp_min.status_code == 200
            assert resp_min.json()["result"] == "accepted"


# ===========================================================================
# 4. Reopened transitions with borderline confidence
# ===========================================================================


@pytest.mark.asyncio
async def test_stress_borderline_confidence_reopen(session: AsyncSession):
    """Stress test: Contradicting event confidence threshold boundary (0.50).

    reopen_min_event_confidence = 0.50 (from config/thresholds.yaml).
    - Confidence 0.4999: Must NOT reopen (remains CLAIMED_FIXED).
    - Confidence 0.5000: Exactly at threshold. Must REOPEN (becomes REOPENED).
    - Confidence 0.5001: Above threshold. Must REOPEN (becomes REOPENED).
    - Event with captured_at == claimed_fixed_at: Must NOT reopen (must be > claimed_fixed_at).
    """
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id
    await session.commit()

    claim_time = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    # 1. Test confidence = 0.4999 (below threshold)
    p1 = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    p1.claimed_fixed_at = claim_time
    await session.commit()
    p1_id = p1.id

    ev_low = Event(
        id=uuid.uuid4(),
        device_id=device_id,
        pothole_id=p1_id,
        captured_at=claim_time + timedelta(minutes=5),
        location="SRID=4326;POINT(76.7794 30.7333)",
        confidence=0.4999,
        track_frames=5,
        area_ratio=0.06,
        crop_key="crop_dummy",
        crop_sha256="dummy_hash",
        payload="{}",
        signature="dummy_sig",
    )
    session.add(ev_low)
    await session.commit()

    updated_p1, dec1 = await evaluate_pothole(session, p1_id, settings=settings)
    await session.commit()
    assert updated_p1.status == PotholeStatus.CLAIMED_FIXED
    assert dec1.status_changed is False

    # 2. Test confidence = 0.5000 (exact threshold)
    p2 = await create_pothole(
        session=session,
        lat=30.734000,
        lon=76.780000,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    p2.claimed_fixed_at = claim_time
    await session.commit()
    p2_id = p2.id

    ev_exact = Event(
        id=uuid.uuid4(),
        device_id=device_id,
        pothole_id=p2_id,
        captured_at=claim_time + timedelta(minutes=5),
        location="SRID=4326;POINT(76.7800 30.7340)",
        confidence=0.5000,
        track_frames=5,
        area_ratio=0.06,
        crop_key="crop_dummy",
        crop_sha256="dummy_hash",
        payload="{}",
        signature="dummy_sig",
    )
    session.add(ev_exact)
    await session.commit()

    updated_p2, dec2 = await evaluate_pothole(session, p2_id, settings=settings)
    await session.commit()
    assert updated_p2.status == PotholeStatus.REOPENED
    assert dec2.status_changed is True
    assert dec2.reason == TransitionReason.CONTRADICTING_EVENT

    # 3. Test confidence = 0.5001 (above threshold)
    p3 = await create_pothole(
        session=session,
        lat=30.735000,
        lon=76.781000,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    p3.claimed_fixed_at = claim_time
    await session.commit()
    p3_id = p3.id

    ev_high = Event(
        id=uuid.uuid4(),
        device_id=device_id,
        pothole_id=p3_id,
        captured_at=claim_time + timedelta(minutes=5),
        location="SRID=4326;POINT(76.7810 30.7350)",
        confidence=0.5001,
        track_frames=5,
        area_ratio=0.06,
        crop_key="crop_dummy",
        crop_sha256="dummy_hash",
        payload="{}",
        signature="dummy_sig",
    )
    session.add(ev_high)
    await session.commit()

    updated_p3, dec3 = await evaluate_pothole(session, p3_id, settings=settings)
    await session.commit()
    assert updated_p3.status == PotholeStatus.REOPENED
    assert dec3.status_changed is True

    # 4. Test captured_at == claimed_fixed_at (boundary timestamp equality)
    # Guard: captured_at > claimed_fixed_at
    p4 = await create_pothole(
        session=session,
        lat=30.736000,
        lon=76.782000,
        first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.CLAIMED_FIXED,
    )
    p4.claimed_fixed_at = claim_time
    await session.commit()
    p4_id = p4.id

    ev_same_time = Event(
        id=uuid.uuid4(),
        device_id=device_id,
        pothole_id=p4_id,
        captured_at=claim_time,  # Exactly equal to claimed_fixed_at
        location="SRID=4326;POINT(76.7820 30.7360)",
        confidence=0.90,
        track_frames=5,
        area_ratio=0.06,
        crop_key="crop_dummy",
        crop_sha256="dummy_hash",
        payload="{}",
        signature="dummy_sig",
    )
    session.add(ev_same_time)
    await session.commit()

    updated_p4, dec4 = await evaluate_pothole(session, p4_id, settings=settings)
    await session.commit()
    assert updated_p4.status == PotholeStatus.CLAIMED_FIXED
    assert dec4.status_changed is False


# ===========================================================================
# 5. Pre-claim passes never contribute clean passes to post-claim verification
# ===========================================================================


@pytest.mark.asyncio
async def test_stress_pre_claim_passes_never_verify(session: AsyncSession):
    """Stress test: Pre-claim passes must NEVER count towards post-claim verification.

    Sequence:
    1. Upload 3 valid clean passes at t < claim_time.
    2. Authority claims fixed at t_claim.
    3. Re-evaluation must still show CLAIMED_FIXED (0 clean passes counted).
    4. Upload 1 valid clean pass at t > t_claim.
       Re-evaluation must show CLAIMED_FIXED (1 clean pass counted, need 2).
    5. Upload 2nd valid clean pass at t > t_claim.
       Re-evaluation must transition to VERIFIED.
    6. Boundary: Pass with crossing_at == claimed_fixed_at must NOT count towards post-claim.
    """
    settings = Settings()
    sk = make_signing_key()
    device = await register_or_get_device(session, key_b64(sk), "android")
    device_id = device.id

    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC),
        initial_status=PotholeStatus.REPORTED,
    )
    pothole_id = pothole.id
    await session.commit()

    claim_time = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            # Step 1: Upload 3 clean passes BEFORE claim (e.g. 10:00, 10:30, 10:50)
            for minute in [0, 30, 50]:
                t_pre = datetime(2026, 10, 25, 10, minute, 0, tzinfo=UTC)
                payload_pre = make_sample_payload(
                    device_id=device_id,
                    lat_center=30.733300,
                    lon_center=76.779400,
                    t0=t_pre,
                )
                resp = await post_pass_request(
                    client, device_id, sk, sign_envelope(sk, payload_pre)
                )
                assert resp.status_code == 200

            # Step 2: Authority claims fixed
            p_db = (
                await session.execute(
                    select(Pothole)
                    .where(Pothole.id == pothole_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            p_db.status = PotholeStatus.CLAIMED_FIXED
            p_db.claimed_fixed_at = claim_time
            await session.commit()

            # Step 3: Evaluate pothole now -> must NOT verify despite 3 pre-claim passes
            updated_p, dec = await evaluate_pothole(session, pothole_id, settings=settings)
            await session.commit()
            assert updated_p.status == PotholeStatus.CLAIMED_FIXED
            assert dec.status_changed is False
            assert len(dec.clean_pass_ids) == 0

            # Step 4: Upload 1 clean pass POST-claim (13:00)
            t_post1 = datetime(2026, 10, 25, 13, 0, 0, tzinfo=UTC)
            payload_post1 = make_sample_payload(
                device_id=device_id,
                lat_center=30.733300,
                lon_center=76.779400,
                t0=t_post1,
            )
            resp_post1 = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_post1)
            )
            assert resp_post1.status_code == 200

            # Pothole must still be CLAIMED_FIXED (only 1 clean pass post-claim)
            p_after_1 = (
                await session.execute(
                    select(Pothole)
                    .where(Pothole.id == pothole_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert p_after_1.status == PotholeStatus.CLAIMED_FIXED

            # Step 5: Upload 2nd clean pass POST-claim (14:00)
            t_post2 = datetime(2026, 10, 25, 14, 0, 0, tzinfo=UTC)
            payload_post2 = make_sample_payload(
                device_id=device_id,
                lat_center=30.733300,
                lon_center=76.779400,
                t0=t_post2,
            )
            resp_post2 = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_post2)
            )
            assert resp_post2.status_code == 200

            # Pothole must now be VERIFIED
            p_after_2 = (
                await session.execute(
                    select(Pothole)
                    .where(Pothole.id == pothole_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert p_after_2.status == PotholeStatus.VERIFIED

            # Step 6: Test exact boundary crossing_at == claimed_fixed_at
            # Reset another pothole to CLAIMED_FIXED with claim_time = crossing_time of a pass
            p_bound = await create_pothole(
                session=session,
                lat=30.740000,
                lon=76.790000,
                first_seen_at=datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC),
                initial_status=PotholeStatus.CLAIMED_FIXED,
            )
            p_bound_id = p_bound.id
            t_bound = datetime(2026, 10, 25, 15, 0, 0, tzinfo=UTC)
            # Crossing time for default start_distance 60m and speed 10 mps is t0 + 6s
            crossing_time = t_bound + timedelta(seconds=6)
            p_bound.claimed_fixed_at = crossing_time  # Exact match with crossing_at
            await session.commit()

            payload_bound = make_sample_payload(
                device_id=device_id,
                lat_center=30.740000,
                lon_center=76.790000,
                t0=t_bound,
            )
            resp_bound = await post_pass_request(
                client, device_id, sk, sign_envelope(sk, payload_bound)
            )
            assert resp_bound.status_code == 200

            # Guard: crossing_at > claimed_fixed_at. Since crossing_at == claimed_fixed_at,
            # this pass must NOT count as post-claim clean pass!
            p_bound_db = (
                await session.execute(
                    select(Pothole)
                    .where(Pothole.id == p_bound_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            assert p_bound_db.status == PotholeStatus.CLAIMED_FIXED
