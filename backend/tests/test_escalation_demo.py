"""Tests for Escalation Scheduler and Demo Admin Operations.

Covers T-29 (ESC-01..03, TC-ESC-01..03, TC-DEMO-01..02, API-04, NFR-05).
Endpoints and services tested:
- check_and_escalate (14-day reminder, 28-day escalation, idempotency)
- POST /v1/admin/simulate-time (clock offset advance, scheduled checks, re-evaluation)
- POST /v1/admin/reset (data wipe, clock reset, device & authority preservation)
- Security guards: X-Admin-Secret 401, DEMO_MODE/APP_ENV 404
"""

import base64
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import nacl.signing
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.admin_router import admin_router
from app.auth import register_or_get_device
from app.clock import clock
from app.config import Settings, load_thresholds
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.main import app
from app.models import (
    Authority,
    Device,
    Event,
    Pothole,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    StatusHistory,
    make_point_geography,
)
from app.services.escalation import check_and_escalate
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
        await clock.set_offset_seconds(s, 0)
        await s.commit()
        yield s
        await s.execute(text("UPDATE potholes SET before_event_id = NULL"))
        for tbl in ["status_history", "pass_coverage", "reports", "events", "passes", "potholes"]:
            await s.execute(text(f"DELETE FROM {tbl}"))
        await clock.set_offset_seconds(s, 0)
        await s.commit()
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    if not getattr(app.state, "_admin_router_mounted", False):
        app.include_router(admin_router)
        app.state._admin_router_mounted = True
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _create_authority(session: AsyncSession) -> Authority:
    auth = Authority(
        id=uuid.uuid4(),
        name="Chandigarh Municipal Corporation",
        city="Chandigarh",
        email="roads@chandigarh.gov.in",
        escalation_email="commissioner@chandigarh.gov.in",
    )
    session.add(auth)
    await session.flush()
    return auth


async def _create_device(session: AsyncSession) -> Device:
    sk = nacl.signing.SigningKey.generate()
    pub_b64 = base64.b64encode(bytes(sk.verify_key)).decode("utf-8")
    return await register_or_get_device(session=session, public_key_b64=pub_b64, platform="android")


# ---------------------------------------------------------------------------
# Escalation Scheduler Tests (TC-ESC-01..03)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_14_day_reminder_tc_esc_01(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-ESC-01: Pothole in reported state receives a reminder report after 14 days."""
    auth = await _create_authority(session)
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        initial_status=PotholeStatus.REPORTED,
    )
    pothole.reported_at = t0
    pothole.last_reported_at = t0
    await session.commit()

    # Advance time to t0 + 14 days (1209600s)
    t14 = t0 + timedelta(days=14)
    new_reports = await check_and_escalate(
        session=session,
        now=t14,
        settings=settings,
        thresholds=thresholds,
        send_reports=True,
    )
    await session.commit()

    # Assert exactly 1 reminder report queued and sent
    assert len(new_reports) == 1
    assert new_reports[0].kind == ReportKind.REMINDER
    assert new_reports[0].status == ReportDeliveryStatus.SENT
    assert new_reports[0].to_address == settings.TEST_INBOX  # Hard Rule 1

    # Assert status_history contains reminder_sent transition
    hist_stmt = (
        select(StatusHistory)
        .where(StatusHistory.pothole_id == pothole.id)
        .order_by(StatusHistory.at.asc())
    )
    hist_rows = (await session.execute(hist_stmt)).scalars().all()
    reminder_rows = [h for h in hist_rows if h.reason == TransitionReason.REMINDER_SENT.value]
    assert len(reminder_rows) == 1
    assert reminder_rows[0].from_status == PotholeStatus.REPORTED
    assert reminder_rows[0].to_status == PotholeStatus.REPORTED


@pytest.mark.asyncio
async def test_escalation_28_day_escalate_tc_esc_02(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-ESC-02: Reported pothole receives escalation after 28 days without second reminder."""
    auth = await _create_authority(session)
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        initial_status=PotholeStatus.REPORTED,
    )
    pothole.reported_at = t0
    pothole.last_reported_at = t0
    await session.commit()

    # Step 1: Send reminder at 14 days
    t14 = t0 + timedelta(days=14)
    await check_and_escalate(
        session=session,
        now=t14,
        settings=settings,
        thresholds=thresholds,
        send_reports=True,
    )
    await session.commit()

    # Step 2: Advance to 28 days
    t28 = t0 + timedelta(days=28)
    reports_28 = await check_and_escalate(
        session=session,
        now=t28,
        settings=settings,
        thresholds=thresholds,
        send_reports=True,
    )
    await session.commit()

    # Exactly 1 escalation report sent; NO second reminder report sent
    assert len(reports_28) == 1
    assert reports_28[0].kind == ReportKind.ESCALATION
    assert reports_28[0].status == ReportDeliveryStatus.SENT

    # Check all reports for this pothole: 1 reminder + 1 escalation
    all_reps = (
        (await session.execute(select(Report).where(Report.pothole_id == pothole.id)))
        .scalars()
        .all()
    )
    reminders = [r for r in all_reps if r.kind == ReportKind.REMINDER]
    escalations = [r for r in all_reps if r.kind == ReportKind.ESCALATION]
    assert len(reminders) == 1
    assert len(escalations) == 1

    # Check status history row exists for escalation_sent
    hist_rows = (
        (await session.execute(select(StatusHistory).where(StatusHistory.pothole_id == pothole.id)))
        .scalars()
        .all()
    )
    esc_hist = [h for h in hist_rows if h.reason == TransitionReason.ESCALATION_SENT.value]
    assert len(esc_hist) == 1
    assert esc_hist[0].from_status == PotholeStatus.REPORTED
    assert esc_hist[0].to_status == PotholeStatus.REPORTED


@pytest.mark.asyncio
async def test_escalation_idempotency_tc_esc_03(client: AsyncClient, session: AsyncSession) -> None:
    """TC-ESC-03: Running escalation check twice produces zero duplicate reports."""
    auth = await _create_authority(session)
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        initial_status=PotholeStatus.REPORTED,
    )
    pothole.reported_at = t0
    pothole.last_reported_at = t0
    await session.commit()

    t14 = t0 + timedelta(days=14)

    # First run: creates reminder report
    run1 = await check_and_escalate(
        session=session,
        now=t14,
        settings=settings,
        thresholds=thresholds,
        send_reports=True,
    )
    await session.commit()
    assert len(run1) == 1

    # Second run in the exact same state: must produce ZERO new reports
    run2 = await check_and_escalate(
        session=session,
        now=t14,
        settings=settings,
        thresholds=thresholds,
        send_reports=True,
    )
    await session.commit()
    assert len(run2) == 0

    # Verify no duplicate history entries
    hist_rows = (
        (await session.execute(select(StatusHistory).where(StatusHistory.pothole_id == pothole.id)))
        .scalars()
        .all()
    )
    reminders = [h for h in hist_rows if h.reason == TransitionReason.REMINDER_SENT.value]
    assert len(reminders) == 1


# ---------------------------------------------------------------------------
# Demo Admin Operations & Guards Tests (TC-DEMO-01..02)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_security_and_guards_tc_demo_01(
    client: AsyncClient, session: AsyncSession
) -> None:
    """TC-DEMO-01: Admin endpoints require X-Admin-Secret (401) and DEMO_MODE guard (404)."""
    settings = Settings()

    # 1. Missing X-Admin-Secret -> 401
    r_no_auth = await client.post("/v1/admin/simulate-time", json={"advance_seconds": 3600})
    assert r_no_auth.status_code == 401
    assert r_no_auth.json()["error"]["code"] == "unauthorized"

    # 2. Invalid X-Admin-Secret -> 401
    r_bad_auth = await client.post(
        "/v1/admin/simulate-time",
        headers={"X-Admin-Secret": "wrong-secret"},
        json={"advance_seconds": 3600},
    )
    assert r_bad_auth.status_code == 401
    assert r_bad_auth.json()["error"]["code"] == "unauthorized"

    # 3. Invalid advance_seconds (0 or negative) -> 422
    r_bad_val = await client.post(
        "/v1/admin/simulate-time",
        headers={"X-Admin-Secret": settings.ADMIN_SECRET},
        json={"advance_seconds": 0},
    )
    assert r_bad_val.status_code == 422

    # 4. DEMO_MODE = False -> 404
    app.state.settings = Settings(DEMO_MODE=False)
    r_no_demo = await client.post(
        "/v1/admin/simulate-time",
        headers={"X-Admin-Secret": settings.ADMIN_SECRET},
        json={"advance_seconds": 3600},
    )
    assert r_no_demo.status_code == 404
    assert r_no_demo.json()["error"]["code"] == "not_found"

    # 5. APP_ENV = 'production' -> 404
    app.state.settings = Settings(DEMO_MODE=True, APP_ENV="production")
    r_prod = await client.post(
        "/v1/admin/reset",
        headers={"X-Admin-Secret": settings.ADMIN_SECRET},
    )
    assert r_prod.status_code == 404
    assert r_prod.json()["error"]["code"] == "not_found"

    # Restore settings
    app.state.settings = Settings(DEMO_MODE=True, APP_ENV="development")


@pytest.mark.asyncio
async def test_admin_simulate_time_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    """POST /v1/admin/simulate-time advances clock and triggers escalation checks."""
    settings = Settings(DEMO_MODE=True, APP_ENV="development")
    app.state.settings = settings
    auth = await _create_authority(session)

    # Initial time and pothole reported at t0
    t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        initial_status=PotholeStatus.REPORTED,
    )
    pothole.reported_at = t0
    pothole.last_reported_at = t0
    await session.commit()

    # Advance time by 14 days (1,209,600s)
    resp = await client.post(
        "/v1/admin/simulate-time",
        headers={"X-Admin-Secret": settings.ADMIN_SECRET},
        json={"advance_seconds": 1209600},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["clock_offset_seconds"] == 1209600
    assert "now" in data
    assert data["reports_sent"] >= 1
    assert any(
        sc["pothole_id"] == str(pothole.id) and sc["to_status"] == "reported"
        for sc in data["status_changes"]
    )


@pytest.mark.asyncio
async def test_admin_reset_endpoint_tc_demo_02(client: AsyncClient, session: AsyncSession) -> None:
    """TC-DEMO-02: POST /v1/admin/reset wipes defect data and keeps devices and authorities."""
    settings = Settings(DEMO_MODE=True, APP_ENV="development")
    app.state.settings = settings

    auth = await _create_authority(session)
    device = await _create_device(session)
    t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)

    pothole = await create_pothole(
        session=session,
        lat=30.7333,
        lon=76.7794,
        first_seen_at=t0,
        authority_id=auth.id,
        initial_status=PotholeStatus.REPORTED,
    )

    # Insert an event, report, and advance clock offset to 5000s
    evt = Event(
        id=uuid.uuid4(),
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0,
        location=make_point_geography(30.7333, 76.7794),
        confidence=0.9,
        track_frames=5,
        crop_key="crops/test.jpg",
        crop_sha256="0" * 64,
        payload="{}",
        signature="sig",
    )
    session.add(evt)
    await clock.set_offset_seconds(session, 5000)
    await session.commit()

    # Verify data exists before reset
    p_count_before = (await session.execute(select(func.count(Pothole.id)))).scalar_one()
    assert p_count_before >= 1
    assert await clock.get_offset_seconds(session) == 5000
    await session.commit()

    # Call /v1/admin/reset
    resp = await client.post(
        "/v1/admin/reset",
        headers={"X-Admin-Secret": settings.ADMIN_SECRET},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "reset"}

    # Verify transactional tables are truncated
    p_count = (await session.execute(select(func.count(Pothole.id)))).scalar_one()
    e_count = (await session.execute(select(func.count(Event.id)))).scalar_one()
    h_count = (await session.execute(select(func.count(StatusHistory.id)))).scalar_one()
    assert p_count == 0
    assert e_count == 0
    assert h_count == 0

    # Verify reference tables are preserved
    auth_count = (await session.execute(select(func.count(Authority.id)))).scalar_one()
    dev_count = (await session.execute(select(func.count(Device.id)))).scalar_one()
    assert auth_count >= 1
    assert dev_count >= 1

    # Verify clock offset reset to 0
    offset = await clock.get_offset_seconds(session)
    assert offset == 0
