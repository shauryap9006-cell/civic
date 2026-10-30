"""Tests for Public Read API endpoints.

Covers T-28 (WEB-01..04, TC-API-01..04, TC-WEB-01..04).
Endpoints tested:
- GET /v1/potholes (bounding box, status filtering, limit, validation)
- GET /v1/potholes/{id} (pothole detail, before/after evidence, audit history, coverage)
- GET /v1/stats (potholes_total, by_status, reports_sent, km_covered)
"""

import base64
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import nacl.signing
import pytest
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.read_router import read_router
from app.auth import register_or_get_device
from app.config import Settings
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.main import app
from app.models import (
    Authority,
    Device,
    Event,
    Pass,
    PassCoverage,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    SeverityBand,
    StatusHistory,
    make_point_geography,
)
from app.services.potholes import create_pothole


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as s:
        await s.execute(text("UPDATE potholes SET before_event_id = NULL"))
        for tbl in ["status_history", "pass_coverage", "reports", "events", "passes", "potholes"]:
            await s.execute(text(f"DELETE FROM {tbl}"))
        await s.commit()
        yield s
        await s.execute(text("UPDATE potholes SET before_event_id = NULL"))
        for tbl in ["status_history", "pass_coverage", "reports", "events", "passes", "potholes"]:
            await s.execute(text(f"DELETE FROM {tbl}"))
        await s.commit()
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    if not getattr(app.state, "_read_router_mounted", False):
        app.include_router(read_router)
        app.state._read_router_mounted = True
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _create_authority(session: AsyncSession, name: str = "Chandigarh MC") -> Authority:
    auth = Authority(
        id=uuid.uuid4(),
        name=name,
        city="Chandigarh",
        email="roads@chandigarh.gov.in",
        escalation_email="commissioner@chandigarh.gov.in",
    )
    session.add(auth)
    await session.flush()
    return auth


async def _create_device(session: AsyncSession, platform: str = "android") -> Device:
    sk = nacl.signing.SigningKey.generate()
    pub_b64 = base64.b64encode(bytes(sk.verify_key)).decode("utf-8")
    return await register_or_get_device(session=session, public_key_b64=pub_b64, platform=platform)


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_potholes_empty(client: AsyncClient, session: AsyncSession) -> None:
    """Empty database returns 200 with empty items list."""
    resp = await client.get("/v1/potholes")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert data["items"] == []


@pytest.mark.asyncio
async def test_list_potholes_bbox_filtering_tc_api_03(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-API-03: Bounding box filter returns only potholes inside the geographic envelope."""
    auth = await _create_authority(session)
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

    # Pothole inside envelope (Chandigarh: 30.7333, 76.7794)
    p_inside = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        severity=SeverityBand.HIGH,
    )

    # Pothole outside envelope (Delhi: 28.6139, 77.2090)
    p_outside = await create_pothole(
        session=session,
        lat=28.6139,
        lon=77.2090,
        first_seen_at=t0,
        authority_id=auth.id,
        severity=SeverityBand.LOW,
    )
    await session.commit()

    # Query with bounding box covering Chandigarh
    bbox = "76.70,30.70,76.90,30.80"
    resp = await client.get(f"/v1/potholes?bbox={bbox}")
    assert resp.status_code == 200
    data = resp.json()
    ids = [item["id"] for item in data["items"]]
    assert str(p_inside.id) in ids
    assert str(p_outside.id) not in ids

    # Query with bounding box where no potholes exist
    empty_bbox = "70.0,20.0,71.0,21.0"
    resp_empty = await client.get(f"/v1/potholes?bbox={empty_bbox}")
    assert resp_empty.status_code == 200
    assert resp_empty.json()["items"] == []


@pytest.mark.asyncio
async def test_list_potholes_status_filtering_tc_api_03(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-API-03: Status array filtering returns only potholes matching specified statuses."""
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

    p1 = await create_pothole(
        session=session, lat=30.71, lon=76.71, first_seen_at=t0, initial_status=PotholeStatus.OPEN
    )
    p2 = await create_pothole(
        session=session,
        lat=30.72,
        lon=76.72,
        first_seen_at=t0,
        initial_status=PotholeStatus.REPORTED,
    )
    p3 = await create_pothole(
        session=session,
        lat=30.73,
        lon=76.73,
        first_seen_at=t0,
        initial_status=PotholeStatus.VERIFIED,
    )
    await session.commit()

    # Query for reported and verified
    resp = await client.get("/v1/potholes?status=reported&status=verified")
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = {item["id"] for item in items}
    assert str(p2.id) in ids
    assert str(p3.id) in ids
    assert str(p1.id) not in ids


@pytest.mark.asyncio
async def test_list_potholes_validation_errors_tc_api_02(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-API-02: Malformed bounding box, inverted coordinates, or invalid statuses return 422."""
    # Non-numeric coordinates
    r1 = await client.get("/v1/potholes?bbox=abc,def,ghi,jkl")
    assert r1.status_code == 422
    assert r1.json()["error"]["code"] == "invalid_bbox"

    # Incomplete coordinates (only 3 parts)
    r2 = await client.get("/v1/potholes?bbox=76.70,30.70,76.90")
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "invalid_bbox"

    # Inverted bbox (min_lon > max_lon)
    r3 = await client.get("/v1/potholes?bbox=76.90,30.70,76.70,30.80")
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "invalid_bbox"

    # Invalid status
    r4 = await client.get("/v1/potholes?status=invalid_status_xyz")
    assert r4.status_code == 422
    assert r4.json()["error"]["code"] == "invalid_status"

    # Invalid limit (0 or > 1000)
    r5 = await client.get("/v1/potholes?limit=0")
    assert r5.status_code == 422
    r6 = await client.get("/v1/potholes?limit=5000")
    assert r6.status_code == 422


@pytest.mark.asyncio
async def test_get_pothole_detail_404_tc_api_02(client: AsyncClient, session: AsyncSession) -> None:
    """TC-API-02: Non-existent pothole ID returns 404 with standard error body."""
    non_existent = uuid.uuid4()
    resp = await client.get(f"/v1/potholes/{non_existent}")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "not_found"
    assert "not found" in err["message"].lower()


@pytest.mark.asyncio
async def test_get_pothole_detail_composition_tc_api_04(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-API-04 & TC-WEB-02: GET /v1/potholes/{id} returns detail, evidence, and source badge."""
    auth = await _create_authority(session, name="Punjab PWD")
    device = await _create_device(session, platform="seed")
    t0 = datetime(2026, 10, 25, 9, 30, 0, tzinfo=UTC)

    pothole = await create_pothole(
        session=session,
        lat=30.7333123,
        lon=76.7794123,
        first_seen_at=t0,
        authority_id=auth.id,
        severity=SeverityBand.MEDIUM,
        initial_status=PotholeStatus.REPORTED,
    )
    pothole.reported_at = t0
    pothole.last_reported_at = t0

    # Add an event with crop
    evt_id = uuid.uuid4()
    event = Event(
        id=evt_id,
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0,
        location=make_point_geography(30.7333123, 76.7794123),
        confidence=0.88,
        track_frames=5,
        area_ratio=0.15,
        severity=SeverityBand.MEDIUM,
        crop_key=f"crops/{evt_id}.jpg",
        crop_sha256="0" * 64,
        payload="{}",
        signature="sig",
    )
    session.add(event)
    await session.flush()

    pothole.before_event_id = event.id
    pothole.event_count = 1

    # Add history row
    hist = StatusHistory(
        pothole_id=pothole.id,
        from_status=PotholeStatus.OPEN,
        to_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT.value,
        evidence={"report_id": "test"},
        at=t0,
    )
    session.add(hist)
    await session.commit()

    resp = await client.get(f"/v1/potholes/{pothole.id}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["id"] == str(pothole.id)
    assert data["lat"] == 30.7333123
    assert data["lon"] == 76.7794123
    assert data["status"] == "reported"
    assert data["severity"] == "medium"
    assert data["source_badge"] == "seed"
    assert data["authority_name"] == "Punjab PWD"
    assert data["reported_at"] is not None

    # Before event inspection
    assert data["before_event"] is not None
    assert data["before_event"]["id"] == str(event.id)
    assert data["before_event"]["confidence"] == 0.88
    assert data["before_event"]["device_platform"] == "seed"

    # History audit trail inspection
    assert len(data["history"]) >= 1
    reasons = [h["reason"] for h in data["history"]]
    assert "first_event" in reasons
    assert "report_sent" in reasons

    # Requirements thresholds present
    assert data["required_clean_passes"] == 2
    assert data["occlusion_check_enabled"] is True


@pytest.mark.asyncio
async def test_get_pothole_detail_verified_and_reopened_tc_web_03(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-WEB-03: Verified pothole hides after images; reopened pothole shows after events."""
    device = await _create_device(session, platform="android")
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)
    t_claim = t0 + timedelta(hours=2)
    t_after = t_claim + timedelta(hours=1)

    # 1. Reopened Pothole
    p_reopened = await create_pothole(
        session=session,
        lat=30.74,
        lon=76.74,
        first_seen_at=t0,
        initial_status=PotholeStatus.REPORTED,
    )
    p_reopened.claimed_fixed_at = t_claim
    p_reopened.status = PotholeStatus.REOPENED
    p_reopened.reopened_at = t_after

    # Initial event
    e_init = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=p_reopened.id,
        captured_at=t0,
        location=make_point_geography(30.74, 76.74),
        confidence=0.85,
        track_frames=4,
        crop_key="crops/e1.jpg",
        crop_sha256="0" * 64,
        payload="{}",
        signature="sig",
    )
    # Contradicting after event
    e_after = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=p_reopened.id,
        captured_at=t_after,
        location=make_point_geography(30.74, 76.74),
        confidence=0.92,
        track_frames=6,
        crop_key="crops/e2.jpg",
        crop_sha256="0" * 64,
        payload="{}",
        signature="sig",
    )
    session.add_all([e_init, e_after])
    await session.flush()
    p_reopened.before_event_id = e_init.id
    await session.commit()

    resp_reopened = await client.get(f"/v1/potholes/{p_reopened.id}")
    assert resp_reopened.status_code == 200
    data_reopened = resp_reopened.json()
    assert data_reopened["status"] == "reopened"
    assert data_reopened["before_event"]["id"] == str(e_init.id)
    assert len(data_reopened["after_events"]) == 1
    assert data_reopened["after_events"][0]["id"] == str(e_after.id)

    # 2. Verified Pothole: Must hide after images (after_events == [])
    p_verified = await create_pothole(
        session=session,
        lat=30.75,
        lon=76.75,
        first_seen_at=t0,
        initial_status=PotholeStatus.REPORTED,
    )
    p_verified.claimed_fixed_at = t_claim
    p_verified.status = PotholeStatus.VERIFIED
    p_verified.verified_at = t_after

    # Add pass and pass coverage row
    pass_obj = Pass(
        id=uuid.uuid4(),
        device_id=device.id,
        started_at=t_after - timedelta(minutes=5),
        ended_at=t_after + timedelta(minutes=5),
        path=WKTElement("LINESTRING(76.749 30.75, 76.751 30.75)", srid=4326),
        samples=[{"lat": 30.75, "lon": 76.75, "sharpness": 50.0}],
        sample_count=1,
        payload="{}",
        signature="sig",
    )
    session.add(pass_obj)
    await session.flush()

    cov = PassCoverage(
        pass_id=pass_obj.id,
        pothole_id=p_verified.id,
        crossing_at=t_after,
        clean=True,
        reason="clean_pass",
        window_samples=10,
        ok_fraction=1.0,
    )
    session.add(cov)
    await session.commit()

    resp_verified = await client.get(f"/v1/potholes/{p_verified.id}")
    assert resp_verified.status_code == 200
    data_verified = resp_verified.json()
    assert data_verified["status"] == "verified"
    assert data_verified["after_events"] == []  # Verified never shows after images
    assert len(data_verified["coverage_after_claim"]) == 1
    assert data_verified["coverage_after_claim"][0]["clean"] is True


@pytest.mark.asyncio
async def test_get_stats_endpoint_tc_web_04(client: AsyncClient, session: AsyncSession) -> None:
    """TC-WEB-04: GET /v1/stats accurately returns impact counts and km_covered."""
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)
    device = await _create_device(session)
    auth = await _create_authority(session)

    # 1. Potholes
    p1 = await create_pothole(
        session=session, lat=30.71, lon=76.71, first_seen_at=t0, initial_status=PotholeStatus.OPEN
    )
    await create_pothole(
        session=session,
        lat=30.72,
        lon=76.72,
        first_seen_at=t0,
        initial_status=PotholeStatus.VERIFIED,
    )

    # 2. Report sent
    rep = Report(
        pothole_id=p1.id,
        authority_id=auth.id,
        kind=ReportKind.INITIAL,
        to_address="test@example.com",
        status=ReportDeliveryStatus.SENT,
    )
    session.add(rep)

    # 3. Pass with path length (approx 111 km for 1 degree latitude)
    pass_obj = Pass(
        id=uuid.uuid4(),
        device_id=device.id,
        started_at=t0,
        ended_at=t0 + timedelta(minutes=10),
        path=WKTElement("LINESTRING(76.70 30.70, 76.70 30.71)", srid=4326),
        samples=[],
        sample_count=0,
        payload="{}",
        signature="sig",
    )
    session.add(pass_obj)
    await session.commit()

    resp = await client.get("/v1/stats")
    assert resp.status_code == 200
    stats = resp.json()

    assert stats["potholes_total"] == 2
    assert stats["by_status"]["open"] == 1
    assert stats["by_status"]["verified"] == 1
    assert stats["by_status"]["reported"] == 0
    assert stats["reports_sent"] == 1
    assert stats["verified"] == 1
    assert stats["km_covered"] > 0.0
