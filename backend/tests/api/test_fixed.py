"""Tests for Fixed token preview and claim endpoints.

Covers T-25 (VER-04, API-03, TC-VER-18, TC-VER-19, TC-API-05, W-07).
"""

import base64
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import derive_fixed_token, hash_fixed_token, register_or_get_device
from app.config import Settings
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.main import app
from app.models import (
    Authority,
    Device,
    Event,
    SeverityBand,
    StatusHistory,
    make_point_geography,
)
from app.services.potholes import (
    create_pothole,
    record_status_transition,
)


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


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _create_test_authority(session: AsyncSession) -> Authority:
    auth_res = await session.execute(select(Authority).limit(1))
    auth = auth_res.scalar_one_or_none()
    if auth is None:
        auth = Authority(
            id=uuid.uuid4(),
            name="Municipal Corporation",
            city="Chandigarh",
            email="authority@chandigarh.gov.in",
        )
        session.add(auth)
        await session.flush()
    return auth


async def _create_test_device(session: AsyncSession, platform: str = "android") -> Device:
    sk = nacl.signing.SigningKey.generate()
    pub_b64 = base64.b64encode(bytes(sk.verify_key)).decode("utf-8")
    return await register_or_get_device(session=session, public_key_b64=pub_b64, platform=platform)


@pytest.mark.asyncio
async def test_fixed_preview_tc_api_05(client: AsyncClient, session: AsyncSession) -> None:
    """TC-API-05: GET /v1/fixed/{token} returns the correct can_claim and message for statuses."""
    settings = Settings()
    auth = await _create_test_authority(session)
    device = await _create_test_device(session)

    t0 = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        severity=SeverityBand.MEDIUM,
    )

    # Add an event with a crop
    evt = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0,
        location=make_point_geography(30.7333, 76.7794),
        confidence=0.85,
        track_frames=5,
        area_ratio=0.08,
        severity=SeverityBand.MEDIUM,
        crop_key="crops/test_evt.jpg",
        crop_sha256="0123456789abcdef" * 4,
        payload="{}",
        signature="dummy",
    )
    session.add(evt)
    await session.flush()

    pothole.before_event_id = evt.id

    # 1. Transition to REPORTED (T2) and derive token
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT,
        at=t0 + timedelta(minutes=1),
    )
    token = derive_fixed_token(pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET)
    pothole.fixed_token_hash = hash_fixed_token(token)
    await session.commit()

    # Test GET /v1/fixed/{token} on REPORTED
    resp = await client.get(f"/v1/fixed/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_claim"] is True
    assert "ready for repair claim" in data["message"].lower()
    p_info = data["pothole"]
    assert p_info["id"] == str(pothole.id)
    assert p_info["status"] == "reported"
    assert p_info["severity"] == "medium"
    assert p_info["event_count"] == 1
    assert p_info["source_badge"] == "live"
    assert p_info["thumbnail_url"] is not None

    # 2. Test status CLAIMED_FIXED
    pothole.status = PotholeStatus.CLAIMED_FIXED
    pothole.claimed_fixed_at = t0 + timedelta(minutes=10)
    await session.commit()

    resp = await client.get(f"/v1/fixed/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_claim"] is False
    assert "already been claimed as fixed" in data["message"].lower()
    assert data["pothole"]["status"] == "claimed_fixed"

    # 3. Test status VERIFIED
    pothole.status = PotholeStatus.VERIFIED
    pothole.verified_at = t0 + timedelta(minutes=20)
    await session.commit()

    resp = await client.get(f"/v1/fixed/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_claim"] is False
    assert "already been verified" in data["message"].lower()
    assert data["pothole"]["status"] == "verified"

    # 4. Test status UNKNOWN
    pothole.status = PotholeStatus.UNKNOWN
    await session.commit()

    resp = await client.get(f"/v1/fixed/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["can_claim"] is False
    assert "not yet verified the repair" in data["message"].lower()
    assert data["pothole"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_fixed_claim_twice_idempotent_tc_ver_18(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """TC-VER-18: POST /v1/fixed/{token} called twice returns 200 and the same claimed_fixed_at."""
    settings = Settings()
    auth = await _create_test_authority(session)
    device = await _create_test_device(session)

    t0 = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
    )

    # Event 1: lower confidence * area_ratio
    evt1 = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0,
        location=make_point_geography(30.7333, 76.7794),
        confidence=0.70,
        track_frames=3,
        area_ratio=0.04,  # score = 0.028
        severity=SeverityBand.LOW,
        crop_key="crops/evt1.jpg",
        crop_sha256="1111111111111111" * 4,
        payload="{}",
        signature="dummy",
    )
    # Event 2: higher confidence * area_ratio (best crop)
    evt2 = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0 + timedelta(seconds=10),
        location=make_point_geography(30.7333, 76.7794),
        confidence=0.92,
        track_frames=7,
        area_ratio=0.12,  # score = 0.1104
        severity=SeverityBand.HIGH,
        crop_key="crops/evt2.jpg",
        crop_sha256="2222222222222222" * 4,
        payload="{}",
        signature="dummy",
    )
    session.add_all([evt1, evt2])
    await session.flush()

    # Move to reported and mint token
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT,
        at=t0 + timedelta(minutes=1),
    )
    token = derive_fixed_token(pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET)
    pothole.fixed_token_hash = hash_fixed_token(token)
    await session.commit()

    # 1. First claim
    resp1 = await client.post(
        f"/v1/fixed/{token}",
        json={"note": "Repair crew dispatched, work order #WO-987"},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["pothole_id"] == str(pothole.id)
    assert data1["status"] == "claimed_fixed"
    assert data1["message"] == "Pothole repair claimed successfully."
    claimed_at_1 = data1["claimed_fixed_at"]

    # Verify best before_event_id is evt2
    hist_stmt = select(StatusHistory).where(StatusHistory.pothole_id == pothole.id)
    hist_res = await session.execute(hist_stmt)
    history_rows = hist_res.scalars().all()
    claim_history = [h for h in history_rows if h.reason == TransitionReason.FIXED_CLAIM]
    assert len(claim_history) == 1
    assert claim_history[0].evidence.get("before_event_id") == str(evt2.id)
    assert claim_history[0].evidence.get("note") == "Repair crew dispatched, work order #WO-987"

    # Refresh pothole
    await session.refresh(pothole)
    assert pothole.status == PotholeStatus.CLAIMED_FIXED
    assert pothole.before_event_id == evt2.id

    # 2. Second claim (idempotent call)
    resp2 = await client.post(
        f"/v1/fixed/{token}",
        json={"note": "Duplicate click by inspector"},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["pothole_id"] == str(pothole.id)
    assert data2["status"] == "claimed_fixed"
    assert data2["message"] == "Pothole is already claimed as fixed."
    assert data2["claimed_fixed_at"] == claimed_at_1

    # Verify NO duplicate history row was written
    hist_res2 = await session.execute(hist_stmt)
    history_rows2 = hist_res2.scalars().all()
    claim_history2 = [h for h in history_rows2 if h.reason == TransitionReason.FIXED_CLAIM]
    assert len(claim_history2) == 1


@pytest.mark.asyncio
async def test_fixed_token_superseded_after_reopen_tc_ver_19(
    client: AsyncClient,
    session: AsyncSession,
) -> None:
    """TC-VER-19: Token after reopen returns 410 token_superseded; the new link works."""
    settings = Settings()
    auth = await _create_test_authority(session)
    device = await _create_test_device(session)

    t0 = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
    )
    evt = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0,
        location=make_point_geography(30.7333, 76.7794),
        confidence=0.88,
        track_frames=4,
        area_ratio=0.06,
        severity=SeverityBand.LOW,
        crop_key="crops/evt_init.jpg",
        crop_sha256="3333333333333333" * 4,
        payload="{}",
        signature="dummy",
    )
    session.add(evt)
    await session.flush()

    # Move to reported and mint token_0
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT,
        at=t0 + timedelta(minutes=1),
    )
    token_0 = derive_fixed_token(pothole.id, 0, settings.FIXED_TOKEN_SECRET)
    pothole.fixed_token_hash = hash_fixed_token(token_0)
    await session.commit()

    # First claim succeeds with token_0
    resp_claim1 = await client.post(f"/v1/fixed/{token_0}")
    assert resp_claim1.status_code == 200

    # Reopen pothole (T7: claimed_fixed -> reopened, fixed_token_epoch increments to 1)
    await session.refresh(pothole)
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REOPENED,
        reason=TransitionReason.CONTRADICTING_EVENT,
        at=t0 + timedelta(days=2),
    )
    await session.commit()
    assert pothole.fixed_token_epoch == 1

    # Old token_0 now returns 410 token_superseded
    resp_get_old = await client.get(f"/v1/fixed/{token_0}")
    assert resp_get_old.status_code == 410
    err_data = resp_get_old.json()["error"]
    assert err_data["code"] == "token_superseded"

    resp_post_old = await client.post(f"/v1/fixed/{token_0}")
    assert resp_post_old.status_code == 410
    assert resp_post_old.json()["error"]["code"] == "token_superseded"

    # Send reopened report (T8: reopened -> reported, mint token_1)
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REOPEN_REPORT_SENT,
        at=t0 + timedelta(days=2, hours=1),
    )
    token_1 = derive_fixed_token(pothole.id, 1, settings.FIXED_TOKEN_SECRET)
    pothole.fixed_token_hash = hash_fixed_token(token_1)
    await session.commit()

    # Old link still returns 410
    resp_old_again = await client.get(f"/v1/fixed/{token_0}")
    assert resp_old_again.status_code == 410

    # New link works!
    resp_get_new = await client.get(f"/v1/fixed/{token_1}")
    assert resp_get_new.status_code == 200
    assert resp_get_new.json()["can_claim"] is True

    resp_post_new = await client.post(
        f"/v1/fixed/{token_1}",
        json={"note": "Fixed after reopening, re-paved entire road segment"},
    )
    assert resp_post_new.status_code == 200
    assert resp_post_new.json()["status"] == "claimed_fixed"


@pytest.mark.asyncio
async def test_fixed_token_not_found(client: AsyncClient, session: AsyncSession) -> None:
    """Invalid or unknown token returns 404."""
    resp_get = await client.get("/v1/fixed/unknown-token-1234567890123456789012345678")
    assert resp_get.status_code == 404
    assert resp_get.json()["error"]["code"] == "not_found"

    resp_post = await client.post("/v1/fixed/unknown-token-1234567890123456789012345678")
    assert resp_post.status_code == 404
    assert resp_post.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_fixed_claim_invalid_status_409(client: AsyncClient, session: AsyncSession) -> None:
    """POST /v1/fixed/{token} returns 409 if pothole is not in a claimable state (e.g. open)."""
    settings = Settings()
    auth = await _create_test_authority(session)
    t0 = datetime(2026, 4, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
    )
    # Directly set token on OPEN pothole (simulating an edge case)
    token = derive_fixed_token(pothole.id, 0, settings.FIXED_TOKEN_SECRET)
    pothole.fixed_token_hash = hash_fixed_token(token)
    await session.commit()

    # Claim on OPEN should return 409 invalid_claim_state
    resp = await client.post(f"/v1/fixed/{token}")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "invalid_claim_state"
    assert "open" in err["message"]

