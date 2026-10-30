"""Admin API router for CivicLens demo management.

Implements T-29 (API-04, NFR-05, openapi.yaml lines 275-341).
Provides:
- POST /v1/admin/simulate-time: Advance clock, trigger escalations and re-evaluations
- POST /v1/admin/reset: Truncate transactional data and reset clock offset
Guarded by X-Admin-Secret header and DEMO_MODE / APP_ENV checks.
"""

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, Thresholds, load_thresholds
from app.db import get_db_session
from app.services.admin import reset_demo_state, simulate_time, verify_admin_access

admin_router = APIRouter(prefix="/v1", tags=["Demo"])


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
# Schemas
# ---------------------------------------------------------------------------


class SimulateTimeRequest(BaseModel):
    advance_seconds: int = Field(ge=1, le=31_536_000, description="Seconds to advance clock")


class StatusChangeItem(BaseModel):
    pothole_id: str
    from_status: str
    to_status: str


class SimulateTimeResponse(BaseModel):
    now: str
    clock_offset_seconds: int
    reports_sent: int
    status_changes: list[StatusChangeItem]


class ResetResponse(BaseModel):
    status: str = "reset"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@admin_router.post("/admin/simulate-time", response_model=SimulateTimeResponse)
async def admin_simulate_time(
    body: SimulateTimeRequest,
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    thresholds: Thresholds = Depends(get_thresholds),
) -> SimulateTimeResponse:
    """Advance simulated server clock and evaluate escalations and verification."""
    verify_admin_access(x_admin_secret, settings)
    result = await simulate_time(
        session=session,
        advance_seconds=body.advance_seconds,
        settings=settings,
        thresholds=thresholds,
    )
    return SimulateTimeResponse(**result)


@admin_router.post("/admin/reset", response_model=ResetResponse)
async def admin_reset(
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ResetResponse:
    """Wipe transactional demo defect data and reset clock offset to 0."""
    verify_admin_access(x_admin_secret, settings)
    result = await reset_demo_state(session=session)
    return ResetResponse(**result)
