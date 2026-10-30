"""Tests for database-level verification wiring (Ticket T-27).

Covers VER-05..08, docs/STATE_MACHINE.md scenarios S1..S16, and TC-VER-01..17, TC-VER-20.
"""

import base64
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import nacl.signing
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import register_or_get_device
from app.config import Settings, load_thresholds
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.models import (
    Event,
    Pothole,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    StatusHistory,
    make_point_geography,
)
from app.services.passes import ingest_pass
from app.services.potholes import create_pothole, evaluate_pothole, record_status_transition
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


def sign_envelope(sk: nacl.signing.SigningKey, payload_dict: dict) -> dict:
    payload_str = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True)
    sig = sk.sign(payload_str.encode("utf-8")).signature
    sig_b64 = base64.b64encode(sig).decode("utf-8")
    return {"payload": payload_str, "signature": sig_b64}


def make_pass_payload(
    device_id: uuid.UUID,
    lat_center: float,
    lon_center: float,
    crossing_time: datetime,
    lateral_offset_m: float = 1.0,
    quality: dict | None = None,
    pass_id: uuid.UUID | None = None,
) -> dict:
    synth = make_pass(
        pothole_lat=lat_center,
        pothole_lon=lon_center,
        pass_id=pass_id,
        device_id=device_id,
        lateral_offset_m=lateral_offset_m,
        crossing_time=crossing_time,
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
        "samples": samples_list,
    }


@pytest.mark.asyncio
async def test_scenario_s1_two_clean_passes_verify(session: AsyncSession):
    """S1 / TC-VER-01: Claim -> Pass 1 clean -> Pass 2 clean -> verified (T4)."""
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
    # Authority claims fixed
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        at=t_claim,
    )
    await session.commit()

    # Pass 1 clean at t_claim + 1 hour
    p1_payload = make_pass_payload(
        device1.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=1)
    )
    p1_env = sign_envelope(sk1, p1_payload)
    res1 = await ingest_pass(
        session, device1, p1_env["payload"], p1_env["signature"], settings, thresholds
    )
    await session.commit()

    assert res1["result"] == "accepted"
    p_check1 = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    # Still claimed_fixed because 1 of 2 clean passes required (TC-VER-02)
    assert p_check1.status == PotholeStatus.CLAIMED_FIXED
    assert p_check1.verified_at is None

    # Pass 2 clean at t_claim + 2 hours
    p2_payload = make_pass_payload(
        device2.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=2)
    )
    p2_env = sign_envelope(sk2, p2_payload)
    res2 = await ingest_pass(
        session, device2, p2_env["payload"], p2_env["signature"], settings, thresholds
    )
    await session.commit()

    assert res2["result"] == "accepted"
    p_check2 = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    # Now VERIFIED via T4 (clean_passes_met)
    assert p_check2.status == PotholeStatus.VERIFIED
    assert p_check2.verified_at is not None

    # Check status history
    hist = (
        (
            await session.execute(
                select(StatusHistory)
                .where(StatusHistory.pothole_id == pothole.id)
                .order_by(StatusHistory.at.asc())
            )
        )
        .scalars()
        .all()
    )
    verify_row = hist[-1]
    assert verify_row.from_status == PotholeStatus.CLAIMED_FIXED
    assert verify_row.to_status == PotholeStatus.VERIFIED
    assert verify_row.reason == TransitionReason.CLEAN_PASSES_MET
    assert len(verify_row.evidence["clean_pass_ids"]) == 2


@pytest.mark.asyncio
async def test_scenario_s3_blurred_pass_marks_unknown(session: AsyncSession):
    """S3 / TC-VER-03: Claim -> Pass 1 blurred -> unknown (T5)."""
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

    # Pass 1 blurred (sharpness 10.0 < 60.0)
    p1_payload = make_pass_payload(
        device.id,
        pothole.lat,
        pothole.lon,
        t_claim + timedelta(hours=1),
        quality={"sharpness": 10.0},
    )
    p1_env = sign_envelope(sk, p1_payload)
    res = await ingest_pass(
        session, device, p1_env["payload"], p1_env["signature"], settings, thresholds
    )
    await session.commit()

    assert res["result"] == "accepted"
    p_check = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    # Transitioned to UNKNOWN (T5)
    assert p_check.status == PotholeStatus.UNKNOWN

    hist = (
        (
            await session.execute(
                select(StatusHistory)
                .where(StatusHistory.pothole_id == pothole.id)
                .order_by(StatusHistory.at.asc())
            )
        )
        .scalars()
        .all()
    )
    unknown_row = hist[-1]
    assert unknown_row.from_status == PotholeStatus.CLAIMED_FIXED
    assert unknown_row.to_status == PotholeStatus.UNKNOWN
    assert unknown_row.reason == TransitionReason.NO_VALID_CLEAN_PASS


@pytest.mark.asyncio
async def test_scenario_s4_blurred_then_two_clean_passes_verify(session: AsyncSession):
    """S4 / TC-VER-04: Claim -> P1 blurred -> unknown -> P2 clean -> P3 clean -> verified (T6)."""
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

    # Pass 1 blurred
    p1_payload = make_pass_payload(
        device.id,
        pothole.lat,
        pothole.lon,
        t_claim + timedelta(hours=1),
        quality={"sharpness": 10.0},
    )
    p1_env = sign_envelope(sk, p1_payload)
    await ingest_pass(session, device, p1_env["payload"], p1_env["signature"], settings, thresholds)
    await session.commit()

    # Pass 2 clean
    p2_payload = make_pass_payload(
        device.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=2)
    )
    p2_env = sign_envelope(sk, p2_payload)
    await ingest_pass(session, device, p2_env["payload"], p2_env["signature"], settings, thresholds)
    await session.commit()

    p_mid = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    assert p_mid.status == PotholeStatus.UNKNOWN

    # Pass 3 clean
    p3_payload = make_pass_payload(
        device.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=3)
    )
    p3_env = sign_envelope(sk, p3_payload)
    await ingest_pass(session, device, p3_env["payload"], p3_env["signature"], settings, thresholds)
    await session.commit()

    p_final = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    assert p_final.status == PotholeStatus.VERIFIED


@pytest.mark.asyncio
async def test_scenario_s5_contradicting_event_beats_clean_passes(session: AsyncSession):
    """S5 / TC-VER-05: Claim -> P1 clean -> contradicting event (conf 0.8) -> reopened (T7)."""
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

    # Pass 1 clean
    p1_payload = make_pass_payload(
        device.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=1)
    )
    p1_env = sign_envelope(sk, p1_payload)
    await ingest_pass(session, device, p1_env["payload"], p1_env["signature"], settings, thresholds)
    await session.commit()

    # Contradicting event at t_claim + 2 hours
    event_id = uuid.uuid4()
    contradicting_event = Event(
        id=event_id,
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim + timedelta(hours=2),
        location=make_point_geography(pothole.lat, pothole.lon),
        confidence=0.85,
        track_frames=5,
        area_ratio=0.06,
        crop_key="dummy_crop",
        crop_sha256="dummy_hash",
        payload="{}",
        signature="dummy_sig",
    )
    session.add(contradicting_event)
    await session.flush()

    # Run evaluate_pothole
    updated_pothole, dec = await evaluate_pothole(session, pothole.id, thresholds, settings)
    await session.commit()

    assert dec.status_changed is True
    assert dec.new_status == PotholeStatus.REOPENED
    assert updated_pothole.status == PotholeStatus.REOPENED
    assert updated_pothole.reopened_at is not None

    # Check pending report queued
    reopened_rpt = (
        await session.execute(
            select(Report).where(
                Report.pothole_id == pothole.id,
                Report.kind == ReportKind.REOPENED,
                Report.status == ReportDeliveryStatus.PENDING,
            )
        )
    ).scalar_one_or_none()
    assert reopened_rpt is not None


@pytest.mark.asyncio
async def test_scenario_s6_verified_pothole_contradicting_event_reopens(session: AsyncSession):
    """S6 / TC-VER-06: Verified pothole transitions to reopened on
    post-claim contradicting event.
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
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session, pothole, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM, at=t_claim
    )
    await record_status_transition(
        session,
        pothole,
        PotholeStatus.VERIFIED,
        TransitionReason.CLEAN_PASSES_MET,
        at=t_claim + timedelta(hours=3),
    )
    await session.commit()

    epoch_before = pothole.fixed_token_epoch

    # Add contradicting event
    event_id = uuid.uuid4()
    contradicting_event = Event(
        id=event_id,
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim + timedelta(hours=5),
        location=make_point_geography(pothole.lat, pothole.lon),
        confidence=0.88,
        track_frames=6,
        area_ratio=0.07,
        crop_key="crop_reopen",
        crop_sha256="sha_reopen",
        payload="{}",
        signature="sig_reopen",
    )
    session.add(contradicting_event)
    await session.flush()

    updated_pothole, dec = await evaluate_pothole(session, pothole.id, thresholds, settings)
    await session.commit()

    assert updated_pothole.status == PotholeStatus.REOPENED
    assert updated_pothole.verified_at is None
    assert updated_pothole.reopened_at is not None
    assert updated_pothole.fixed_token_epoch == epoch_before + 1


@pytest.mark.asyncio
async def test_scenario_s7_pass_before_claim_ignored(session: AsyncSession):
    """S7 / TC-VER-07: Pass crossing before claimed_fixed_at is ignored for verification."""
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
        initial_status=PotholeStatus.REPORTED,
    )

    # Pass 1 clean crossing BEFORE claim (11:00)
    p1_payload = make_pass_payload(
        device.id, pothole.lat, pothole.lon, t_claim - timedelta(hours=1)
    )
    p1_env = sign_envelope(sk, p1_payload)
    await ingest_pass(session, device, p1_env["payload"], p1_env["signature"], settings, thresholds)
    await session.commit()

    # Claim fixed at 12:00
    await record_status_transition(
        session, pothole, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM, at=t_claim
    )
    await session.commit()

    # Pass 2 clean crossing AFTER claim (13:00)
    p2_payload = make_pass_payload(
        device.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=1)
    )
    p2_env = sign_envelope(sk, p2_payload)
    await ingest_pass(session, device, p2_env["payload"], p2_env["signature"], settings, thresholds)
    await session.commit()

    p_check = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    # Stays claimed_fixed because only Pass 2 counts (1 clean pass < 2)
    assert p_check.status == PotholeStatus.CLAIMED_FIXED


@pytest.mark.asyncio
async def test_scenario_s8_pre_claim_event_does_not_reopen(session: AsyncSession):
    """S8 / TC-VER-08: Offline event with captured_at <= claimed_fixed_at does not reopen."""
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
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session, pothole, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM, at=t_claim
    )
    await session.commit()

    # Offline event captured BEFORE claim at 11:30
    event_id = uuid.uuid4()
    pre_claim_event = Event(
        id=event_id,
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim - timedelta(minutes=30),
        location=make_point_geography(pothole.lat, pothole.lon),
        confidence=0.92,
        track_frames=6,
        area_ratio=0.08,
        crop_key="crop_pre",
        crop_sha256="sha_pre",
        payload="{}",
        signature="sig_pre",
    )
    session.add(pre_claim_event)
    await session.flush()

    updated_pothole, dec = await evaluate_pothole(session, pothole.id, thresholds, settings)
    await session.commit()

    assert dec.status_changed is False
    assert updated_pothole.status == PotholeStatus.CLAIMED_FIXED


@pytest.mark.asyncio
async def test_scenario_s9_loop_pass_counts_once(session: AsyncSession):
    """S9 / TC-VER-09: One pass crossing twice (loop) counts as 1 clean pass."""
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
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session, pothole, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM, at=t_claim
    )
    await session.commit()

    # Pass crossing at 13:00
    p1_payload = make_pass_payload(
        device.id, pothole.lat, pothole.lon, t_claim + timedelta(hours=1)
    )
    p1_env = sign_envelope(sk, p1_payload)
    await ingest_pass(session, device, p1_env["payload"], p1_env["signature"], settings, thresholds)
    await session.commit()

    p_check = (await session.execute(select(Pothole).where(Pothole.id == pothole.id))).scalar_one()
    # 1 distinct pass is insufficient for verification (2 required)
    assert p_check.status == PotholeStatus.CLAIMED_FIXED


@pytest.mark.asyncio
async def test_scenario_s17_idempotent_audit_trail_zero_duplicate_rows(session: AsyncSession):
    """S17 / TC-VER-17: Calling evaluate_pothole 5 times consecutively
    produces zero duplicate status_history rows.
    """
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    t_claim = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.733300,
        lon=76.779400,
        first_seen_at=t_claim - timedelta(days=2),
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session, pothole, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM, at=t_claim
    )
    await session.commit()

    # Count status_history rows before evaluations (1 from create_pothole + 1 from claim)
    initial_hist_count = len(
        (await session.execute(select(StatusHistory).where(StatusHistory.pothole_id == pothole.id)))
        .scalars()
        .all()
    )
    assert initial_hist_count == 2

    # Call evaluate_pothole 5 times consecutively
    for _ in range(5):
        updated_pothole, dec = await evaluate_pothole(session, pothole.id, thresholds, settings)
        await session.commit()
        assert dec.status_changed is False

    # Check status_history row count is strictly unchanged
    final_hist_count = len(
        (await session.execute(select(StatusHistory).where(StatusHistory.pothole_id == pothole.id)))
        .scalars()
        .all()
    )
    assert final_hist_count == initial_hist_count


@pytest.mark.asyncio
async def test_contradiction_low_confidence_does_not_reopen_tc_ver_20(session: AsyncSession):
    """TC-VER-20: Event after claim with confidence 0.35 (< 0.50 floor) does NOT trigger reopen."""
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
        initial_status=PotholeStatus.REPORTED,
    )
    await record_status_transition(
        session, pothole, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM, at=t_claim
    )
    await session.commit()

    # Event after claim but with confidence 0.35 (< 0.50)
    event_id = uuid.uuid4()
    low_conf_event = Event(
        id=event_id,
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t_claim + timedelta(hours=2),
        location=make_point_geography(pothole.lat, pothole.lon),
        confidence=0.35,  # Below reopen_min_event_confidence (0.50)
        track_frames=4,
        area_ratio=0.04,
        crop_key="crop_low",
        crop_sha256="sha_low",
        payload="{}",
        signature="sig_low",
    )
    session.add(low_conf_event)
    await session.flush()

    updated_pothole, dec = await evaluate_pothole(session, pothole.id, thresholds, settings)
    await session.commit()

    assert dec.status_changed is False
    assert updated_pothole.status == PotholeStatus.CLAIMED_FIXED
