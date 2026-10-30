"""Pothole database service operations with PostGIS spatial queries and row locking.

Follows docs/STATE_MACHINE.md and docs/WORKFLOWS.md.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import derive_fixed_token, hash_fixed_token
from app.clock import clock
from app.config import Settings, Thresholds
from app.logic.coverage import CoverageResult, PassQualityThresholds
from app.logic.state_machine import PotholeStatus, TransitionReason, validate_transition
from app.logic.verification import (
    Decision,
    VerificationThresholdsConfig,
)
from app.logic.verification import (
    evaluate as pure_evaluate,
)
from app.models import (
    Authority,
    Device,
    Event,
    PassCoverage,
    Pothole,
    SeverityBand,
    StatusHistory,
    make_point_geography,
)
from app.storage import generate_presigned_get


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


async def find_pothole_by_fixed_token(
    session: AsyncSession,
    token: str,
    secret: str,
    for_update: bool = False,
) -> tuple[Pothole | None, bool]:
    """Look up a pothole by its fixed token.

    Returns:
        (pothole, is_superseded)
        - (Pothole, False) if the token is valid and current.
        - (Pothole, True) if the token was valid in an earlier epoch (superseded).
        - (None, False) if the token is completely unknown.
    """
    token_hash = hash_fixed_token(token)

    # 1. Direct indexed lookup on current fixed_token_hash
    stmt = select(Pothole).where(Pothole.fixed_token_hash == token_hash)
    if for_update:
        stmt = stmt.with_for_update()
    res = await session.execute(stmt)
    pothole = res.scalar_one_or_none()
    if pothole is not None:
        current_token = derive_fixed_token(
            pothole_id=pothole.id,
            epoch=pothole.fixed_token_epoch,
            secret=secret,
        )
        if current_token == token:
            return pothole, False
        return pothole, True

    # 2. Check for superseded tokens on potholes with fixed_token_epoch > 0
    rotated_stmt = select(Pothole).where(Pothole.fixed_token_epoch > 0)
    if for_update:
        rotated_stmt = rotated_stmt.with_for_update()
    rotated_res = await session.execute(rotated_stmt)
    candidates = rotated_res.scalars().all()
    for cand in candidates:
        for epoch in range(cand.fixed_token_epoch):
            past_token = derive_fixed_token(
                pothole_id=cand.id,
                epoch=epoch,
                secret=secret,
            )
            if past_token == token:
                return cand, True

    return None, False


async def select_best_before_event_id(
    session: AsyncSession,
    pothole_id: uuid.UUID,
    claimed_fixed_at: datetime,
) -> uuid.UUID | None:
    """Select the best before event id for a pothole at claim time.

    Per docs/STATE_MACHINE.md section 5:
    Choose the event of this pothole with the highest `confidence * area_ratio`
    among events with `captured_at <= claimed_fixed_at`.
    """
    stmt = select(Event).where(
        Event.pothole_id == pothole_id,
        Event.captured_at <= claimed_fixed_at,
    )
    res = await session.execute(stmt)
    events = res.scalars().all()
    if not events:
        return None

    def score(e: Event) -> tuple[float, float]:
        ar = e.area_ratio if e.area_ratio is not None else 0.0
        return (e.confidence * ar, -e.captured_at.timestamp())

    best_event = max(events, key=score)
    return best_event.id


async def get_pothole_source_badge(
    session: AsyncSession,
    pothole_id: uuid.UUID,
) -> str:
    """Derive source badge from contributing devices (seed, replay, live)."""
    stmt = (
        select(Device.platform)
        .join(Event, Event.device_id == Device.id)
        .where(Event.pothole_id == pothole_id)
        .distinct()
    )
    res = await session.execute(stmt)
    platforms = set(res.scalars().all())
    if "seed" in platforms:
        return "seed"
    if "replay" in platforms:
        return "replay"
    return "live"


async def get_pothole_thumbnail_url(
    session: AsyncSession,
    pothole: Pothole,
    settings: Settings,
) -> str | None:
    """Generate presigned GET URL for the pothole's thumbnail crop."""
    target_event: Event | None = None
    if pothole.before_event_id:
        res = await session.execute(select(Event).where(Event.id == pothole.before_event_id))
        target_event = res.scalar_one_or_none()

    if target_event is None:
        res_first = await session.execute(
            select(Event)
            .where(Event.pothole_id == pothole.id)
            .order_by(Event.captured_at.asc())
            .limit(1)
        )
        target_event = res_first.scalar_one_or_none()

    if target_event and target_event.crop_key:
        return generate_presigned_get(settings, target_event.crop_key)
    return None


async def claim_fixed_pothole(
    session: AsyncSession,
    pothole: Pothole,
    note: str | None = None,
    at: datetime | None = None,
) -> tuple[Pothole, bool]:
    """Apply authority repair claim according to docs/STATE_MACHINE.md section 3.

    - If reported: applies T3 transition, selects before_event_id, sets claimed_fixed_at.
    - If claimed_fixed, unknown, or verified: idempotent no-op, returns (pothole, False).
    - If open or reopened: raises HTTPException 409 (invalid_claim_state).

    Returns:
        (pothole, transitioned)
    """
    if pothole.status in (
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
        PotholeStatus.VERIFIED,
    ):
        return pothole, False

    if pothole.status != PotholeStatus.REPORTED:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "invalid_claim_state",
                    "message": (
                        f"Pothole cannot be claimed in its current status: {pothole.status.value}"
                    ),
                }
            },
        )

    if at is None:
        at = await clock.now(session)

    best_event_id = await select_best_before_event_id(session, pothole.id, at)
    if best_event_id is not None:
        pothole.before_event_id = best_event_id

    evidence: dict[str, Any] = {
        "before_event_id": str(best_event_id) if best_event_id else None,
    }
    if note:
        evidence["note"] = note

    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason=TransitionReason.FIXED_CLAIM,
        evidence=evidence,
        at=at,
    )

    # Re-evaluate in case post-claim passes exist
    await evaluate_pothole(
        session=session,
        pothole_id=pothole.id,
        now=at,
    )

    return pothole, True


async def evaluate_pothole(
    session: AsyncSession,
    pothole_id: uuid.UUID,
    thresholds: Thresholds | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> tuple[Pothole, Decision]:
    """Evaluate verification state for a pothole according to docs/STATE_MACHINE.md §4.

    Acquires an exclusive row lock on the pothole (SELECT ... FOR UPDATE).
    Queries events and post-claim pass coverages, calls pure logic evaluate(),
    and if the status changes:
    - Calls record_status_transition (which adds status_history).
    - If new status is REOPENED, queues a reopened report email.

    Idempotent: If status does not change, no status_history row is added.
    """
    pothole = await get_pothole_by_id(session, pothole_id, for_update=True)
    if pothole is None:
        raise ValueError(f"Pothole {pothole_id} not found")

    # Only evaluate potholes that are claimed_fixed, unknown, or verified
    if pothole.status not in (
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
        PotholeStatus.VERIFIED,
    ):
        return pothole, Decision.none(pothole.status.value)

    if pothole.claimed_fixed_at is None:
        return pothole, Decision.none(pothole.status.value)

    if now is None:
        now = await clock.now(session)

    if thresholds is None:
        from app.config import load_thresholds

        s = settings or Settings()
        thresholds = load_thresholds(s.THRESHOLDS_PATH)

    # Load events for this pothole
    stmt_events = (
        select(Event).where(Event.pothole_id == pothole_id).order_by(Event.captured_at.asc())
    )
    res_events = await session.execute(stmt_events)
    events = list(res_events.scalars().all())

    # Load post-claim pass coverage rows
    stmt_cov = (
        select(PassCoverage)
        .where(
            PassCoverage.pothole_id == pothole_id,
            PassCoverage.crossing_at > pothole.claimed_fixed_at,
        )
        .order_by(PassCoverage.crossing_at.asc())
    )
    res_cov = await session.execute(stmt_cov)
    cov_rows = list(res_cov.scalars().all())

    coverage_results = [
        CoverageResult(
            pass_id=c.pass_id,
            crossing_at=c.crossing_at,
            clean=c.clean,
            reason=c.reason,
            window_samples=c.window_samples,
            ok_fraction=float(c.ok_fraction) if c.ok_fraction is not None else 0.0,
            occlusion_checked=thresholds.pass_quality.occlusion_check_enabled,
        )
        for c in cov_rows
    ]

    th_ver = VerificationThresholdsConfig(
        required_clean_passes=thresholds.verification.required_clean_passes,
        require_distinct_passes=thresholds.verification.require_distinct_passes,
        reopen_min_event_confidence=thresholds.verification.reopen_min_event_confidence,
        reopen_min_events=thresholds.verification.reopen_min_events,
    )
    th_pass = PassQualityThresholds(
        sample_interval_s=thresholds.pass_quality.sample_interval_s,
        window_min_m=thresholds.pass_quality.window_min_m,
        window_max_m=thresholds.pass_quality.window_max_m,
        max_lateral_offset_m=thresholds.pass_quality.max_lateral_offset_m,
        max_heading_diff_deg=thresholds.pass_quality.max_heading_diff_deg,
        min_window_samples=thresholds.pass_quality.min_window_samples,
        min_window_ok_fraction=thresholds.pass_quality.min_window_ok_fraction,
        min_sharpness=thresholds.pass_quality.min_sharpness,
        max_occlusion_ratio=thresholds.pass_quality.max_occlusion_ratio,
        occlusion_check_enabled=thresholds.pass_quality.occlusion_check_enabled,
        min_mean_luma=thresholds.pass_quality.min_mean_luma,
        max_gps_accuracy_m=thresholds.pass_quality.max_gps_accuracy_m,
        max_speed_mps=thresholds.pass_quality.max_speed_mps,
        min_inference_fps=thresholds.pass_quality.min_inference_fps,
        require_detector_active=thresholds.pass_quality.require_detector_active,
    )

    decision = pure_evaluate(
        pothole=pothole,
        events=events,
        passes=coverage_results,
        now=now,
        th_ver=th_ver,
        th_pass=th_pass,
    )

    if decision.status_changed and decision.new_status is not None:
        new_status = PotholeStatus(decision.new_status)
        await record_status_transition(
            session=session,
            pothole=pothole,
            new_status=new_status,
            reason=decision.reason,
            evidence=decision.evidence,
            at=now,
        )

        if new_status == PotholeStatus.REOPENED:
            from app.services.events import ensure_reopened_report_queued

            s = settings or Settings()
            await ensure_reopened_report_queued(session, pothole, s)

    return pothole, decision


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
