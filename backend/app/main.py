"""CivicLens backend FastAPI application."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import register_or_get_device
from app.clock import clock
from app.config import Settings, load_thresholds
from app.db import get_db_session, init_db
from app.storage import check_storage_health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # 1. Load settings & thresholds
    settings = Settings()
    thresholds = load_thresholds(settings.THRESHOLDS_PATH)

    app.state.settings = settings
    app.state.thresholds = thresholds

    # 2. Initialize DB
    init_db(settings)

    yield


app = FastAPI(
    title="CivicLens API",
    version="0.1.0",
    description="Pothole detection and quality-gated repair verification API.",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class DeviceRegisterRequest(BaseModel):
    public_key: str
    platform: str
    app_version: str | None = None
    model_version: str | None = None


class DeviceRegisterResponse(BaseModel):
    device_id: str
    server_time: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/healthz", tags=["Public"])
async def healthz(
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Liveness and dependency health check."""
    # Check DB
    db_status = "ok"
    try:
        res = await session.execute(text("SELECT 1"))
        if res.scalar() != 1:
            db_status = "error"
    except Exception:
        db_status = "error"

    # Check Storage
    settings: Settings = getattr(app.state, "settings", Settings())
    storage_ok = check_storage_health(settings)
    storage_status = "ok" if storage_ok else "error"

    # Time and offset
    offset = await clock.get_offset_seconds(session)
    logical_now = await clock.now(session)

    overall_status = "ok" if (db_status == "ok" and storage_status == "ok") else "degraded"

    payload = {
        "status": overall_status,
        "db": db_status,
        "storage": storage_status,
        "time": logical_now.isoformat(),
        "clock_offset_seconds": offset,
    }

    if overall_status != "ok":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=payload,
        )

    return payload


@app.post("/v1/devices/register", tags=["Device"], status_code=201)
async def register_device(
    body: DeviceRegisterRequest,
    session: AsyncSession = Depends(get_db_session),
) -> DeviceRegisterResponse:
    """Register a device with its Ed25519 public key (idempotent by public_key)."""
    allowed_platforms = {"android", "linux", "replay", "seed"}
    if body.platform not in allowed_platforms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "invalid_platform",
                    "message": f"platform must be one of {sorted(allowed_platforms)}",
                }
            },
        )

    device = await register_or_get_device(
        session=session,
        public_key_b64=body.public_key,
        platform=body.platform,
        app_version=body.app_version,
        model_version=body.model_version,
    )
    await session.commit()

    server_time = await clock.now(session)
    return DeviceRegisterResponse(
        device_id=str(device.id),
        server_time=server_time.isoformat(),
    )

