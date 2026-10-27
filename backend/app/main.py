"""CivicLens backend FastAPI application."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import register_or_get_device, verify_request_signature
from app.clock import clock
from app.config import Settings, Thresholds, load_thresholds
from app.db import get_db_session, init_db
from app.logic.state_machine import PotholeStatus
from app.models import parse_point_wkb
from app.services.events import ingest_single_event
from app.services.potholes import (
    claim_fixed_pothole,
    find_pothole_by_fixed_token,
    get_pothole_source_badge,
    get_pothole_thumbnail_url,
)
from app.storage import (
    check_storage_health,
    generate_crop_key,
    generate_presigned_put,
)


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


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": str(exc.detail)}},
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


class UploadSignRequest(BaseModel):
    content_type: Literal["image/jpeg"]
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=1, le=1_000_000)


class UploadSignResponse(BaseModel):
    crop_key: str
    upload_url: str
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]
    expires_at: str


class SignedEnvelope(BaseModel):
    payload: str
    signature: str


class EventsRequest(BaseModel):
    events: list[SignedEnvelope] = Field(min_length=1, max_length=50)


class EventResult(BaseModel):
    event_id: str
    result: Literal["accepted", "duplicate", "rejected"]
    pothole_id: str | None = None
    pothole_status: str | None = None
    reason: (
        Literal[
            "invalid_signature",
            "malformed_payload",
            "crop_missing",
            "crop_hash_mismatch",
            "device_mismatch",
            "invalid_coordinates",
        ]
        | None
    ) = None


class EventsResponse(BaseModel):
    results: list[EventResult]


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


class FixedPreview(BaseModel):
    pothole: PotholeSummary
    can_claim: bool
    message: str


class ClaimFixedRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class FixedClaimResponse(BaseModel):
    pothole_id: str
    status: str
    claimed_fixed_at: str
    message: str


def get_claim_preview_info(status: PotholeStatus) -> tuple[bool, str]:
    if status == PotholeStatus.REPORTED:
        return True, "Pothole is reported and ready for repair claim."
    if status == PotholeStatus.CLAIMED_FIXED:
        return (
            False,
            "This pothole has already been claimed as fixed and is awaiting verification.",
        )
    if status == PotholeStatus.VERIFIED:
        return False, "This repair has already been verified by subsequent clean passes."
    if status == PotholeStatus.UNKNOWN:
        return (
            False,
            "This pothole was previously claimed as fixed, but subsequent passes have not "
            "yet verified the repair.",
        )
    if status == PotholeStatus.REOPENED:
        return False, "This pothole was reopened due to contradicting detections."
    if status == PotholeStatus.OPEN:
        return False, "This pothole is not yet reported."
    return False, "Pothole cannot be claimed."


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


@app.post("/v1/uploads/sign", tags=["Ingest"])
async def sign_upload(
    request: Request,
    body: UploadSignRequest,
    session: AsyncSession = Depends(get_db_session),
) -> UploadSignResponse:
    """Issue a presigned PUT URL for uploading one crop image directly to MinIO / S3."""
    settings: Settings = getattr(app.state, "settings", Settings())
    thresholds: Thresholds | None = getattr(app.state, "thresholds", None)
    tolerance = thresholds.auth.request_signature_tolerance_s if thresholds else 300

    device = await verify_request_signature(request, session, tolerance_s=tolerance)

    crop_key = generate_crop_key(device_id=device.id)
    upload_url, headers, expires_at = generate_presigned_put(
        settings=settings,
        crop_key=crop_key,
        content_type=body.content_type,
    )
    return UploadSignResponse(
        crop_key=crop_key,
        upload_url=upload_url,
        method="PUT",
        headers=headers,
        expires_at=expires_at.isoformat(),
    )


@app.post("/v1/events", tags=["Ingest"])
async def upload_events(
    request: Request,
    body: EventsRequest,
    session: AsyncSession = Depends(get_db_session),
) -> EventsResponse:
    """Upload a batch of signed pothole events (independent per event)."""
    settings: Settings = getattr(app.state, "settings", Settings())
    thresholds: Thresholds | None = getattr(app.state, "thresholds", None)
    tolerance = thresholds.auth.request_signature_tolerance_s if thresholds else 300

    device = await verify_request_signature(request, session, tolerance_s=tolerance)

    active_thresholds = thresholds or load_thresholds(settings.THRESHOLDS_PATH)
    results: list[dict[str, Any]] = []
    for item in body.events:
        res = await ingest_single_event(
            session=session,
            device=device,
            payload_str=item.payload,
            signature_b64=item.signature,
            settings=settings,
            thresholds=active_thresholds,
        )
        results.append(res)

    await session.commit()
    return EventsResponse(results=[EventResult(**r) for r in results])


@app.get("/v1/fixed/{token}", tags=["Authority"])
async def preview_fixed(
    token: str,
    session: AsyncSession = Depends(get_db_session),
) -> FixedPreview:
    """Preview the pothole behind a Fixed link."""
    settings: Settings = getattr(app.state, "settings", Settings())
    pothole, is_superseded = await find_pothole_by_fixed_token(
        session=session,
        token=token,
        secret=settings.FIXED_TOKEN_SECRET,
    )
    if pothole is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "Fixed token not found"}},
        )

    if is_superseded:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": {
                    "code": "token_superseded",
                    "message": (
                        "Token superseded because the pothole was reopened "
                        "and a newer link was sent"
                    ),
                }
            },
        )

    lat, lon = parse_point_wkb(pothole.location)
    source_badge = await get_pothole_source_badge(session, pothole.id)
    thumbnail_url = await get_pothole_thumbnail_url(session, pothole, settings)

    can_claim, message = get_claim_preview_info(pothole.status)

    summary = PotholeSummary(
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
    )
    return FixedPreview(pothole=summary, can_claim=can_claim, message=message)


@app.post("/v1/fixed/{token}", tags=["Authority"])
async def claim_fixed(
    token: str,
    body: ClaimFixedRequest | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> FixedClaimResponse:
    """Claim that the pothole is repaired (sets claimed_fixed).

    Idempotent: repeating the call after a successful claim returns current state with 200.
    """
    settings: Settings = getattr(app.state, "settings", Settings())
    pothole, is_superseded = await find_pothole_by_fixed_token(
        session=session,
        token=token,
        secret=settings.FIXED_TOKEN_SECRET,
        for_update=True,
    )
    if pothole is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "Fixed token not found"}},
        )

    if is_superseded:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": {
                    "code": "token_superseded",
                    "message": (
                        "Token superseded because the pothole was reopened "
                        "and a newer link was sent"
                    ),
                }
            },
        )

    note = body.note if body else None
    pothole, transitioned = await claim_fixed_pothole(
        session=session,
        pothole=pothole,
        note=note,
    )
    await session.commit()

    message = (
        "Pothole repair claimed successfully."
        if transitioned
        else "Pothole is already claimed as fixed."
    )
    claimed_at_str = (
        pothole.claimed_fixed_at.isoformat()
        if pothole.claimed_fixed_at
        else (await clock.now(session)).isoformat()
    )

    return FixedClaimResponse(
        pothole_id=str(pothole.id),
        status=pothole.status.value,
        claimed_fixed_at=claimed_at_str,
        message=message,
    )
