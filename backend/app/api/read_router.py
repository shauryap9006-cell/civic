"""Public read API router for CivicLens.

Implements T-28 (WEB-01..04, TC-API-01..04, openapi.yaml).
Provides endpoints:
- GET /v1/potholes: Filterable map list with PostGIS bounding box and status array
- GET /v1/potholes/{id}: Full detail with evidence, history audit trail, and pass coverage
- GET /v1/stats: Aggregate impact metrics and kilometers covered
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, Thresholds, load_thresholds
from app.db import get_db_session
from app.logic.state_machine import PotholeStatus
from app.models import (
    Authority,
    Device,
    Event,
    PassCoverage,
    Pothole,
    StatusHistory,
    parse_point_wkb,
)
from app.services.potholes import (
    get_pothole_by_id,
    get_pothole_source_badge,
    get_pothole_thumbnail_url,
)
from app.services.stats import compute_stats
from app.storage import generate_presigned_get

read_router = APIRouter(prefix="/v1", tags=["Public"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or Settings()


def get_thresholds(request: Request, settings: Settings = Depends(get_settings)) -> Thresholds:
    th = getattr(request.app.state, "thresholds", None)
    if th is None:
        th = load_thresholds(settings.THRESHOLDS_PATH)
    return th


# ---------------------------------------------------------------------------
# Pydantic Schemas matching openapi.yaml
# ---------------------------------------------------------------------------


class PotholeSummary(BaseModel):
    id: str
    lat: float
    lon: float
    status: str
    severity: str
    event_count: int
    first_seen_at: str
    last_event_at: str
    thumbnail_url: str | None = None
    source_badge: Literal["live", "replay", "seed"]


class PotholesListResponse(BaseModel):
    items: list[PotholeSummary]


class EventSummary(BaseModel):
    id: str
    captured_at: str
    confidence: float
    severity: str | None = None
    crop_url: str | None = None
    device_platform: str


class StatusHistoryItem(BaseModel):
    from_status: str | None = None
    to_status: str
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    at: str


class CoverageItem(BaseModel):
    pass_id: str
    crossing_at: str
    clean: bool
    reason: str
    window_samples: int
    ok_fraction: float | None = None


class PotholeDetail(PotholeSummary):
    authority_name: str | None = None
    reported_at: str | None = None
    claimed_fixed_at: str | None = None
    verified_at: str | None = None
    reopened_at: str | None = None
    before_event: EventSummary | None = None
    after_events: list[EventSummary] = Field(default_factory=list)
    events: list[EventSummary] = Field(default_factory=list)
    history: list[StatusHistoryItem] = Field(default_factory=list)
    coverage_after_claim: list[CoverageItem] = Field(default_factory=list)
    required_clean_passes: int
    occlusion_check_enabled: bool


class StatsByStatus(BaseModel):
    open: int = 0
    reported: int = 0
    claimed_fixed: int = 0
    unknown: int = 0
    verified: int = 0
    reopened: int = 0


class StatsResponse(BaseModel):
    potholes_total: int
    by_status: StatsByStatus
    reports_sent: int
    claimed_fixed: int
    verified: int
    reopened: int
    unknown: int
    km_covered: float


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@read_router.get("/potholes", response_model=PotholesListResponse)
async def list_potholes(
    bbox: str | None = Query(
        None,
        description="Bounding box: min_lon,min_lat,max_lon,max_lat",
    ),
    status_filter: list[str] | None = Query(
        None,
        alias="status",
        description="Filter by pothole status",
    ),
    limit: int = Query(500, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> PotholesListResponse:
    """List potholes matching spatial bounding box and/or status filters."""
    stmt = select(Pothole)

    # 1. Bounding box parsing and spatial envelope query
    if bbox is not None:
        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_bbox",
                        "message": (
                            "Expected 4 comma-separated numbers: min_lon,min_lat,max_lon,max_lat"
                        ),
                    }
                },
            )
        try:
            min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
        except ValueError as err:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_bbox",
                        "message": "Bounding box coordinates must be valid floating point numbers",
                    }
                },
            ) from err

        if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_bbox",
                        "message": "Longitude coordinates must be between -180 and 180",
                    }
                },
            )
        if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_bbox",
                        "message": "Latitude coordinates must be between -90 and 90",
                    }
                },
            )
        if min_lon > max_lon or min_lat > max_lat:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_bbox",
                        "message": "Inverted bounding box: min_lon > max_lon or min_lat > max_lat",
                    }
                },
            )

        envelope = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        stmt = stmt.where(func.ST_Intersects(Pothole.location, envelope))

    # 2. Status filter validation
    if status_filter:
        valid_status_values = {s.value for s in PotholeStatus}
        parsed_statuses: list[PotholeStatus] = []
        for s in status_filter:
            if s not in valid_status_values:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": {
                            "code": "invalid_status",
                            "message": (
                                f"Invalid status: '{s}'. Allowed: {sorted(valid_status_values)}"
                            ),
                        }
                    },
                )
            parsed_statuses.append(PotholeStatus(s))
        stmt = stmt.where(Pothole.status.in_(parsed_statuses))

    # 3. Order and limit
    stmt = stmt.order_by(Pothole.last_event_at.desc()).limit(limit)
    res = await session.execute(stmt)
    potholes = res.scalars().all()

    items: list[PotholeSummary] = []
    for p in potholes:
        lat, lon = parse_point_wkb(p.location)
        source_badge = await get_pothole_source_badge(session, p.id)
        thumbnail_url = await get_pothole_thumbnail_url(session, p, settings)

        items.append(
            PotholeSummary(
                id=str(p.id),
                lat=round(lat, 7),
                lon=round(lon, 7),
                status=p.status.value,
                severity=p.severity.value,
                event_count=p.event_count,
                first_seen_at=p.first_seen_at.isoformat(),
                last_event_at=p.last_event_at.isoformat(),
                thumbnail_url=thumbnail_url,
                source_badge=source_badge,  # type: ignore[arg-type]
            )
        )

    return PotholesListResponse(items=items)


@read_router.get("/potholes/{id}", response_model=PotholeDetail)
async def get_pothole_detail(
    id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    thresholds: Thresholds = Depends(get_thresholds),
) -> PotholeDetail:
    """Retrieve complete pothole detail, audit evidence, and pass coverage."""
    pothole = await get_pothole_by_id(session, id)
    if pothole is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "Pothole not found"}},
        )

    lat, lon = parse_point_wkb(pothole.location)
    source_badge = await get_pothole_source_badge(session, pothole.id)
    thumbnail_url = await get_pothole_thumbnail_url(session, pothole, settings)

    # 1. Authority name
    authority_name: str | None = None
    if pothole.authority_id is not None:
        auth_res = await session.execute(
            select(Authority.name).where(Authority.id == pothole.authority_id)
        )
        authority_name = auth_res.scalar_one_or_none()

    # 2. Events query with device platform join
    evt_stmt = (
        select(Event, Device.platform)
        .join(Device, Event.device_id == Device.id)
        .where(Event.pothole_id == pothole.id)
        .order_by(Event.captured_at.desc())
    )
    evt_res = await session.execute(evt_stmt)
    event_rows = evt_res.all()

    def format_event(e: Event, plat: str) -> EventSummary:
        crop_url = generate_presigned_get(settings, e.crop_key) if e.crop_key else None
        sev_val = e.severity.value if e.severity else pothole.severity.value
        return EventSummary(
            id=str(e.id),
            captured_at=e.captured_at.isoformat(),
            confidence=round(float(e.confidence), 4),
            severity=sev_val,
            crop_url=crop_url,
            device_platform=plat,
        )

    formatted_events = [format_event(e, plat) for e, plat in event_rows]

    # 3. Before event resolution
    before_summary: EventSummary | None = None
    if pothole.before_event_id is not None:
        for e, plat in event_rows:
            if e.id == pothole.before_event_id:
                before_summary = format_event(e, plat)
                break

    if before_summary is None and event_rows:
        # Fallback to the earliest recorded event
        earliest_e, earliest_plat = min(event_rows, key=lambda x: x[0].captured_at)
        before_summary = format_event(earliest_e, earliest_plat)

    # 4. After events resolution
    # Hard Rule 7 & SCREENS.md § 1: A verified pothole never shows an after image.
    after_summaries: list[EventSummary] = []
    if pothole.claimed_fixed_at is not None and pothole.status != PotholeStatus.VERIFIED:
        for e, plat in event_rows:
            if e.captured_at > pothole.claimed_fixed_at:
                after_summaries.append(format_event(e, plat))

    # 5. Status history
    hist_stmt = (
        select(StatusHistory)
        .where(StatusHistory.pothole_id == pothole.id)
        .order_by(StatusHistory.at.asc())
    )
    hist_res = await session.execute(hist_stmt)
    history_rows = hist_res.scalars().all()
    history = [
        StatusHistoryItem(
            from_status=h.from_status.value if h.from_status else None,
            to_status=h.to_status.value,
            reason=h.reason,
            evidence=h.evidence or {},
            at=h.at.isoformat(),
        )
        for h in history_rows
    ]

    # 6. Pass coverage after claim
    coverage_items: list[CoverageItem] = []
    if pothole.claimed_fixed_at is not None:
        cov_stmt = (
            select(PassCoverage)
            .where(
                PassCoverage.pothole_id == pothole.id,
                PassCoverage.crossing_at > pothole.claimed_fixed_at,
            )
            .order_by(PassCoverage.crossing_at.asc())
        )
        cov_res = await session.execute(cov_stmt)
        cov_rows = cov_res.scalars().all()
        coverage_items = [
            CoverageItem(
                pass_id=str(c.pass_id),
                crossing_at=c.crossing_at.isoformat(),
                clean=c.clean,
                reason=c.reason,
                window_samples=c.window_samples,
                ok_fraction=float(c.ok_fraction) if c.ok_fraction is not None else None,
            )
            for c in cov_rows
        ]

    return PotholeDetail(
        id=str(pothole.id),
        lat=round(lat, 7),
        lon=round(lon, 7),
        status=pothole.status.value,
        severity=pothole.severity.value,
        event_count=pothole.event_count,
        first_seen_at=pothole.first_seen_at.isoformat(),
        last_event_at=pothole.last_event_at.isoformat(),
        thumbnail_url=thumbnail_url,
        source_badge=source_badge,  # type: ignore[arg-type]
        authority_name=authority_name,
        reported_at=pothole.reported_at.isoformat() if pothole.reported_at else None,
        claimed_fixed_at=pothole.claimed_fixed_at.isoformat() if pothole.claimed_fixed_at else None,
        verified_at=pothole.verified_at.isoformat() if pothole.verified_at else None,
        reopened_at=pothole.reopened_at.isoformat() if pothole.reopened_at else None,
        before_event=before_summary,
        after_events=after_summaries,
        events=formatted_events,
        history=history,
        coverage_after_claim=coverage_items,
        required_clean_passes=thresholds.verification.required_clean_passes,
        occlusion_check_enabled=thresholds.pass_quality.occlusion_check_enabled,
    )


@read_router.get("/stats", response_model=StatsResponse)
async def get_stats(
    session: AsyncSession = Depends(get_db_session),
) -> StatsResponse:
    """Retrieve system impact counters and distance statistics."""
    stats = await compute_stats(session)
    return StatsResponse(**stats)
