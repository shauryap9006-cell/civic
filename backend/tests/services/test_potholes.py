"""Tests for potholes service, PostGIS queries, row-locking, and state transitions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logic.state_machine import (
    IllegalTransitionError,
    PotholeStatus,
    TransitionReason,
)
from app.models import SeverityBand, StatusHistory
from app.services.potholes import (
    create_pothole,
    find_nearest_pothole,
    get_pothole_by_id,
    list_potholes,
    record_status_transition,
    update_pothole_location_and_severity,
)


@pytest.mark.asyncio
async def test_create_and_get_pothole(db_session: AsyncSession):
    now = datetime.now(UTC)
    lat, lon = 51.5074, -0.1278

    pothole = await create_pothole(
        session=db_session,
        lat=lat,
        lon=lon,
        first_seen_at=now,
        severity=SeverityBand.LOW,
        initial_status=PotholeStatus.OPEN,
    )
    assert pothole.id is not None
    assert pothole.status == PotholeStatus.OPEN
    assert pothole.severity == SeverityBand.LOW
    assert pothole.event_count == 1
    assert pothole.fixed_token_epoch == 0

    # Retrieve by ID
    retrieved = await get_pothole_by_id(db_session, pothole.id)
    assert retrieved is not None
    assert retrieved.id == pothole.id
    assert abs(retrieved.lat - lat) < 1e-4
    assert abs(retrieved.lon - lon) < 1e-4

    # Verify initial StatusHistory record
    hist_stmt = select(StatusHistory).where(StatusHistory.pothole_id == pothole.id)
    hist_res = await db_session.execute(hist_stmt)
    history_rows = hist_res.scalars().all()
    assert len(history_rows) == 1
    assert history_rows[0].from_status is None
    assert history_rows[0].to_status == PotholeStatus.OPEN
    assert history_rows[0].reason == TransitionReason.FIRST_EVENT


@pytest.mark.asyncio
async def test_find_nearest_pothole_spatial_query(db_session: AsyncSession):
    now = datetime.now(UTC)
    # Origin location
    base_lat, base_lon = 51.50000, -0.12000

    p1 = await create_pothole(
        session=db_session,
        lat=base_lat,
        lon=base_lon,
        first_seen_at=now,
        severity=SeverityBand.MEDIUM,
    )

    # Query ~10 meters away (1 degree lat ~= 111,000m -> 0.0001 deg ~= 11.1m)
    query_lat = base_lat + 0.00009
    query_lon = base_lon

    found = await find_nearest_pothole(
        session=db_session,
        lat=query_lat,
        lon=query_lon,
        radius_m=25.0,
    )
    assert found is not None
    match_pothole, dist = found
    assert match_pothole.id == p1.id
    assert 5.0 < dist < 15.0

    # Query ~100 meters away with radius 25.0 -> should not find it
    far_lat = base_lat + 0.001
    not_found = await find_nearest_pothole(
        session=db_session,
        lat=far_lat,
        lon=base_lon,
        radius_m=25.0,
    )
    assert not_found is None


@pytest.mark.asyncio
async def test_find_nearest_pothole_tiebreaking(db_session: AsyncSession):
    now = datetime.now(UTC)
    center_lat, center_lon = 51.51000, -0.13000

    # Pothole closer (approx 8m)
    p_close = await create_pothole(
        session=db_session,
        lat=center_lat + 0.00007,
        lon=center_lon,
        first_seen_at=now,
    )
    # Pothole farther (approx 18m)
    await create_pothole(
        session=db_session,
        lat=center_lat + 0.00016,
        lon=center_lon,
        first_seen_at=now,
    )

    found = await find_nearest_pothole(
        session=db_session,
        lat=center_lat,
        lon=center_lon,
        radius_m=30.0,
    )
    assert found is not None
    pothole, dist = found
    assert pothole.id == p_close.id
    assert dist < 12.0


@pytest.mark.asyncio
async def test_pothole_row_locking(db_session: AsyncSession):
    now = datetime.now(UTC)
    lat, lon = 51.52000, -0.14000

    p = await create_pothole(session=db_session, lat=lat, lon=lon, first_seen_at=now)

    # Fetch with for_update=True
    locked_p = await get_pothole_by_id(db_session, p.id, for_update=True)
    assert locked_p is not None
    assert locked_p.id == p.id

    # Spatial search with for_update=True
    locked_nearest = await find_nearest_pothole(
        db_session, lat, lon, radius_m=20.0, for_update=True
    )
    assert locked_nearest is not None
    assert locked_nearest[0].id == p.id


@pytest.mark.asyncio
async def test_update_pothole_location_and_severity(db_session: AsyncSession):
    t0 = datetime.now(UTC)
    p = await create_pothole(
        session=db_session,
        lat=51.500,
        lon=-0.120,
        first_seen_at=t0,
        severity=SeverityBand.LOW,
        event_count=1,
    )

    t1 = t0 + timedelta(minutes=5)
    new_lat, new_lon = 51.501, -0.121
    await update_pothole_location_and_severity(
        session=db_session,
        pothole=p,
        lat=new_lat,
        lon=new_lon,
        severity=SeverityBand.HIGH,
        event_count=2,
        last_event_at=t1,
    )

    reloaded = await get_pothole_by_id(db_session, p.id)
    assert reloaded is not None
    assert abs(reloaded.lat - new_lat) < 1e-4
    assert abs(reloaded.lon - new_lon) < 1e-4
    assert reloaded.severity == SeverityBand.HIGH
    assert reloaded.event_count == 2
    assert reloaded.last_event_at == t1


@pytest.mark.asyncio
async def test_record_status_transitions_and_history(db_session: AsyncSession):
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    p = await create_pothole(
        session=db_session,
        lat=51.500,
        lon=-0.120,
        first_seen_at=t0,
    )
    assert p.status == PotholeStatus.OPEN

    # T2: open -> reported
    t1 = t0 + timedelta(hours=1)
    await record_status_transition(
        session=db_session,
        pothole=p,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT,
        evidence={"report_id": "test-report"},
        at=t1,
    )
    assert p.status == PotholeStatus.REPORTED
    assert p.reported_at == t1
    assert p.last_reported_at == t1

    # T3: reported -> claimed_fixed
    t2 = t1 + timedelta(hours=2)
    await record_status_transition(
        session=db_session,
        pothole=p,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        at=t2,
    )
    assert p.status == PotholeStatus.CLAIMED_FIXED
    assert p.claimed_fixed_at == t2
    assert p.verified_at is None

    # T4: claimed_fixed -> verified
    t3 = t2 + timedelta(hours=3)
    await record_status_transition(
        session=db_session,
        pothole=p,
        new_status=PotholeStatus.VERIFIED,
        reason=TransitionReason.CLEAN_PASSES_MET,
        evidence={"clean_passes": 2},
        at=t3,
    )
    assert p.status == PotholeStatus.VERIFIED
    assert p.verified_at == t3

    # T7: verified -> reopened
    t4 = t3 + timedelta(hours=4)
    old_epoch = p.fixed_token_epoch
    await record_status_transition(
        session=db_session,
        pothole=p,
        new_status=PotholeStatus.REOPENED,
        reason=TransitionReason.CONTRADICTING_EVENT,
        evidence={"reopen_events": 1},
        at=t4,
    )
    assert p.status == PotholeStatus.REOPENED
    assert p.reopened_at == t4
    assert p.verified_at is None
    assert p.fixed_token_epoch == old_epoch + 1

    # Check that 5 history records exist in total (first_event + 4 transitions)
    hist_stmt = (
        select(StatusHistory)
        .where(StatusHistory.pothole_id == p.id)
        .order_by(StatusHistory.at.asc())
    )
    hist_rows = (await db_session.execute(hist_stmt)).scalars().all()
    assert len(hist_rows) == 5
    assert [r.to_status for r in hist_rows] == [
        PotholeStatus.OPEN,
        PotholeStatus.REPORTED,
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.VERIFIED,
        PotholeStatus.REOPENED,
    ]


@pytest.mark.asyncio
async def test_illegal_transition_rejected(db_session: AsyncSession):
    t0 = datetime.now(UTC)
    p = await create_pothole(session=db_session, lat=51.500, lon=-0.120, first_seen_at=t0)
    assert p.status == PotholeStatus.OPEN

    # open -> claimed_fixed is strictly illegal
    with pytest.raises(IllegalTransitionError):
        await record_status_transition(
            session=db_session,
            pothole=p,
            new_status=PotholeStatus.CLAIMED_FIXED,
            reason=TransitionReason.FIXED_CLAIM,
        )

    # Pothole status must remain open
    assert p.status == PotholeStatus.OPEN


@pytest.mark.asyncio
async def test_list_potholes(db_session: AsyncSession):
    now = datetime.now(UTC)
    p1 = await create_pothole(
        session=db_session,
        lat=51.501,
        lon=-0.121,
        first_seen_at=now,
        initial_status=PotholeStatus.OPEN,
    )
    p2 = await create_pothole(
        session=db_session,
        lat=51.502,
        lon=-0.122,
        first_seen_at=now,
        initial_status=PotholeStatus.OPEN,
    )
    await record_status_transition(
        session=db_session,
        pothole=p2,
        new_status=PotholeStatus.REPORTED,
        reason=TransitionReason.REPORT_SENT,
        at=now + timedelta(minutes=1),
    )

    all_potholes = await list_potholes(db_session)
    pothole_ids = [p.id for p in all_potholes]
    assert p1.id in pothole_ids
    assert p2.id in pothole_ids

    # Filter by REPORTED
    reported_potholes = await list_potholes(db_session, status=PotholeStatus.REPORTED)
    reported_ids = [p.id for p in reported_potholes]
    assert p2.id in reported_ids
    assert p1.id not in reported_ids
