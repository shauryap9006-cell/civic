"""Pothole database service operations with PostGIS spatial queries and row locking.

Follows docs/STATE_MACHINE.md and docs/WORKFLOWS.md.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.logic.state_machine import PotholeStatus, TransitionReason, validate_transition
from app.models import (
    Authority,
    Pothole,
    SeverityBand,
    StatusHistory,
    make_point_geography,
)


async def find_authority_for_location(
    session: AsyncSession,
    lat: float,
    lon: float,
) -> uuid.UUID | None:
    """Find the authority whose boundary contains (lat, lon), or default to first authority."""
    point_geom = make_point_geography(lat, lon)
    stmt = (
        select(Authority.id)
        .where(
            Authority.boundary.is_not(None),
            func.ST_Intersects(Authority.boundary, point_geom),
        )
        .limit(1)
    )
    res = await session.execute(stmt)
    auth_id = res.scalar_one_or_none()
    if auth_id is not None:
        return auth_id

    # Fallback to the default / first registered authority (e.g. from seed.sql)
    fallback_stmt = select(Authority.id).order_by(Authority.created_at.asc()).limit(1)
    res_fallback = await session.execute(fallback_stmt)
    return res_fallback.scalar_one_or_none()


async def get_pothole_by_id(
    session: AsyncSession,
    pothole_id: uuid.UUID,
    for_update: bool = False,
) -> Pothole | None:
    """Retrieve a pothole by ID, optionally acquiring an exclusive row lock."""
    stmt = select(Pothole).where(Pothole.id == pothole_id)
    if for_update:
        stmt = stmt.with_for_update()
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def find_nearest_pothole(
    session: AsyncSession,
    lat: float,
    lon: float,
    radius_m: float,
    for_update: bool = False,
) -> tuple[Pothole, float] | None:
    """Find the nearest pothole within radius_m of (lat, lon).

    Orders by distance ascending, tie-breaking by created_at ascending.
    If for_update is True, acquires an exclusive row lock on the matched Pothole.
    Returns (pothole, distance_in_meters) or None if no pothole exists within radius_m.
    """
    point_geom = make_point_geography(lat, lon)
    dist_expr = func.ST_Distance(Pothole.location, point_geom)

    stmt = (
        select(Pothole, dist_expr)
        .where(func.ST_DWithin(Pothole.location, point_geom, radius_m))
        .order_by(dist_expr.asc(), Pothole.created_at.asc(), Pothole.id.asc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update(of=Pothole)

    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    pothole, distance = row
    return pothole, float(distance)


async def create_pothole(
    session: AsyncSession,
    lat: float,
    lon: float,
    first_seen_at: datetime,
    authority_id: uuid.UUID | None = None,
    severity: SeverityBand = SeverityBand.LOW,
    initial_status: PotholeStatus = PotholeStatus.OPEN,
    before_event_id: uuid.UUID | None = None,
    event_count: int = 1,
) -> Pothole:
    """Create a new pothole and its initial status history record."""
    if authority_id is None:
        authority_id = await find_authority_for_location(session, lat, lon)

    pothole = Pothole(
        location=make_point_geography(lat, lon),
        status=initial_status,
        severity=severity,
        event_count=event_count,
        authority_id=authority_id,
        before_event_id=before_event_id,
        first_seen_at=first_seen_at,
        last_event_at=first_seen_at,
        fixed_token_epoch=0,
    )
    session.add(pothole)
    await session.flush()

    history = StatusHistory(
        pothole_id=pothole.id,
        from_status=None,
        to_status=initial_status,
        reason=TransitionReason.FIRST_EVENT,
        evidence={},
        at=first_seen_at,
    )
    session.add(history)
    await session.flush()
    return pothole


async def update_pothole_location_and_severity(
    session: AsyncSession,
    pothole: Pothole,
    lat: float,
    lon: float,
    severity: SeverityBand,
    event_count: int,
    last_event_at: datetime,
) -> None:
    """Update pothole canonical location, severity score, and event stats."""
    pothole.location = make_point_geography(lat, lon)
    pothole.severity = severity
    pothole.event_count = event_count
    if pothole.last_event_at is None or last_event_at > pothole.last_event_at:
        pothole.last_event_at = last_event_at
    await session.flush()


async def record_status_transition(
    session: AsyncSession,
    pothole: Pothole,
    new_status: PotholeStatus,
    reason: str,
    evidence: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> StatusHistory:
    """Record and apply a status transition for a pothole according to STATE_MACHINE.md.

    Validates transition rules, updates pothole status and relevant timestamps,
    and inserts a StatusHistory audit record.
    """
    from_status = pothole.status
    validate_transition(from_status, new_status)

    if at is None:
        at = await clock.now(session)

    # Apply side-effects per transition specifications in docs/STATE_MACHINE.md
    if new_status == PotholeStatus.REPORTED:
        if pothole.reported_at is None:
            pothole.reported_at = at
        pothole.last_reported_at = at
    elif new_status == PotholeStatus.CLAIMED_FIXED:
        pothole.claimed_fixed_at = at
        pothole.verified_at = None
    elif new_status == PotholeStatus.VERIFIED:
        pothole.verified_at = at
    elif new_status == PotholeStatus.REOPENED:
        pothole.reopened_at = at
        pothole.verified_at = None
        pothole.fixed_token_epoch += 1

    pothole.status = new_status

    history = StatusHistory(
        pothole_id=pothole.id,
        from_status=from_status,
        to_status=new_status,
        reason=reason,
        evidence=evidence or {},
        at=at,
    )
    session.add(history)
    await session.flush()
    return history


async def list_potholes(
    session: AsyncSession,
    status: PotholeStatus | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Pothole]:
    """List potholes ordered by creation time descending, with optional status filter."""
    stmt = select(Pothole).order_by(Pothole.created_at.desc()).offset(offset).limit(limit)
    if status is not None:
        stmt = stmt.where(Pothole.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())
