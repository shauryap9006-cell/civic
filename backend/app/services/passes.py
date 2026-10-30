"""Pass ingestion and coverage service.

Follows docs/STATE_MACHINE.md section 4.3, openapi.yaml, and W-08.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_envelope_signature
from app.clock import clock
from app.config import Settings, Thresholds
from app.logic.coverage import (
    PassQualityThresholds,
    coverage_for_pass,
)
from app.models import Device, Event, Pass, PassCoverage, Pothole
from app.services.potholes import evaluate_pothole


@dataclass
class PassSampleItem:
    """In-memory representation of a pass sample adhering to PassSampleProtocol."""

    t: datetime
    lat: float
    lon: float
    accuracy_m: float
    speed_mps: float
    heading_deg: float
    sharpness: float
    mean_luma: float
    detector_active: bool
    inference_fps: float
    occlusion_ratio: float | None = None


def parse_and_validate_pass_payload(
    payload_str: str,
    authenticated_device_id: uuid.UUID,
) -> tuple[dict[str, Any], uuid.UUID, datetime, datetime, list[PassSampleItem]]:
    """Parse and validate JSON payload string inside a pass envelope.

    Validates schema rules:
    - Valid JSON object
    - Valid pass id (UUID)
    - device_id matches authenticated device
    - ended_at >= started_at
    - At least 2 samples (minItems: 2)
    - Strictly increasing sample timestamps (t[i] > t[i-1])
    - Latitude and longitude bounds (-90..90, -180..180)

    Raises HTTPException 422 with stable error codes on validation failure.
    """
    try:
        data = json.loads(payload_str)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": {"code": "malformed_payload", "message": f"Malformed JSON: {exc}"}},
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {"code": "malformed_payload", "message": "Payload must be a JSON object"}
            },
        )

    # 1. Validate pass ID
    raw_id = data.get("id")
    if not raw_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {"code": "malformed_payload", "message": "Missing required field 'id'"}
            },
        )
    try:
        pass_id = uuid.UUID(str(raw_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {"code": "malformed_payload", "message": "Field 'id' must be a valid UUID"}
            },
        ) from exc

    # 2. Validate device ID match
    raw_dev_id = data.get("device_id")
    if not raw_dev_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "device_mismatch",
                    "message": "Missing required field 'device_id'",
                }
            },
        )
    try:
        payload_dev_id = uuid.UUID(str(raw_dev_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "device_mismatch",
                    "message": "Field 'device_id' must be a valid UUID",
                }
            },
        ) from exc

    if payload_dev_id != authenticated_device_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "device_mismatch",
                    "message": (
                        f"device_id {payload_dev_id} does not match "
                        f"authenticated device {authenticated_device_id}"
                    ),
                }
            },
        )

    # 3. Validate started_at and ended_at
    if "started_at" not in data or "ended_at" not in data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "malformed_payload",
                    "message": "started_at and ended_at are required",
                }
            },
        )
    try:
        started_at = datetime.fromisoformat(str(data["started_at"]).replace("Z", "+00:00"))
        ended_at = datetime.fromisoformat(str(data["ended_at"]).replace("Z", "+00:00"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "malformed_payload",
                    "message": f"Invalid start/end ISO timestamp: {exc}",
                }
            },
        ) from exc

    if ended_at < started_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "invalid_time_range",
                    "message": f"ended_at ({ended_at}) must be >= started_at ({started_at})",
                }
            },
        )

    # 4. Validate samples collection
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list) or len(raw_samples) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": {
                    "code": "insufficient_samples",
                    "message": "samples must contain at least 2 items",
                }
            },
        )

    parsed_samples: list[PassSampleItem] = []
    prev_t: datetime | None = None

    for idx, s in enumerate(raw_samples):
        if not isinstance(s, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": {
                        "code": "malformed_payload",
                        "message": f"Sample at index {idx} is not an object",
                    }
                },
            )
        try:
            t_dt = datetime.fromisoformat(str(s["t"]).replace("Z", "+00:00"))
            lat = float(s["lat"])
            lon = float(s["lon"])
            acc = float(s["accuracy_m"])
            speed = float(s["speed_mps"])
            heading = float(s["heading_deg"])
            sharpness = float(s["sharpness"])
            mean_luma = float(s["mean_luma"])
            detector_active = bool(s["detector_active"])
            inference_fps = float(s["inference_fps"])
            raw_occlusion = s.get("occlusion_ratio")
            occlusion = float(raw_occlusion) if raw_occlusion is not None else None
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": {
                        "code": "malformed_payload",
                        "message": f"Invalid field in sample {idx}: {exc}",
                    }
                },
            ) from exc

        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": {
                        "code": "invalid_coordinates",
                        "message": f"Coordinates out of bounds in sample {idx}",
                    }
                },
            )

        if prev_t is not None and t_dt <= prev_t:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": {
                        "code": "decreasing_timestamps",
                        "message": (
                            f"Sample {idx} timestamp ({t_dt}) is not strictly "
                            f"greater than previous ({prev_t})"
                        ),
                    }
                },
            )
        prev_t = t_dt

        parsed_samples.append(
            PassSampleItem(
                t=t_dt,
                lat=lat,
                lon=lon,
                accuracy_m=acc,
                speed_mps=speed,
                heading_deg=heading,
                sharpness=sharpness,
                mean_luma=mean_luma,
                detector_active=detector_active,
                inference_fps=inference_fps,
                occlusion_ratio=occlusion,
            )
        )

    return data, pass_id, started_at, ended_at, parsed_samples


def create_pass_linestring(samples: list[PassSampleItem]) -> WKTElement:
    """Build a PostGIS LINESTRING WKTElement with SRID 4326.

    Filters out samples with accuracy_m > 100.0.
    If filtered points count < 2, falls back to using all points.
    """
    filtered = [s for s in samples if s.accuracy_m <= 100.0]
    pts = filtered if len(filtered) >= 2 else samples
    coord_pairs = [f"{s.lon} {s.lat}" for s in pts]
    return WKTElement(f"LINESTRING({', '.join(coord_pairs)})", srid=4326)


async def ingest_pass(
    session: AsyncSession,
    device: Device,
    payload_str: str,
    signature_b64: str,
    settings: Settings,
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Ingest one signed pass envelope, evaluate coverage for candidate potholes,
    and run verification.

    Follows W-08 and docs/STATE_MACHINE.md.
    """
    # 1. Verify individual envelope signature against device's registered public key
    if not verify_envelope_signature(device.public_key, payload_str, signature_b64):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "invalid_signature",
                    "message": "Ed25519 signature verification failed for pass envelope",
                }
            },
        )

    # 2. Parse and validate JSON payload
    data, pass_id, started_at, ended_at, parsed_samples = parse_and_validate_pass_payload(
        payload_str=payload_str,
        authenticated_device_id=device.id,
    )

    # 3. Check duplicate pass ID (idempotency per openapi.yaml)
    existing_stmt = select(Pass.id).where(Pass.id == pass_id)
    res_existing = await session.execute(existing_stmt)
    if res_existing.scalar_one_or_none() is not None:
        return {
            "pass_id": str(pass_id),
            "result": "duplicate",
            "potholes_evaluated": 0,
            "status_changes": [],
        }

    # 4. Construct path geometry and insert into passes table
    path_geom = create_pass_linestring(parsed_samples)
    now_logical = await clock.now(session)

    pass_record = Pass(
        id=pass_id,
        device_id=device.id,
        started_at=started_at,
        ended_at=ended_at,
        path=path_geom,
        samples=data["samples"],
        sample_count=len(parsed_samples),
        model_version=data.get("model_version"),
        app_version=data.get("app_version"),
        received_at=now_logical,
        payload=payload_str,
        signature=signature_b64,
    )
    session.add(pass_record)
    await session.flush()

    # 5. Spatial query to find candidate potholes within buffer of pass trajectory
    # candidate_radius = window_max_m (25) + max_lateral_offset_m (6) + 10m margin = 41m
    candidate_radius = (
        thresholds.pass_quality.window_max_m + thresholds.pass_quality.max_lateral_offset_m + 10.0
    )

    stmt_candidates = (
        select(Pothole)
        .where(func.ST_DWithin(Pothole.location, path_geom, candidate_radius))
        .order_by(Pothole.id.asc())
        .with_for_update()
    )
    res_candidates = await session.execute(stmt_candidates)
    candidate_potholes = list(res_candidates.scalars().all())

    th_pq = PassQualityThresholds(
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

    potholes_evaluated = 0
    status_changes: list[dict[str, str]] = []

    for pothole in candidate_potholes:
        # Load any events from this device merged into this pothole during pass interval
        stmt_events = select(Event).where(
            Event.pothole_id == pothole.id,
            Event.device_id == device.id,
            Event.captured_at >= started_at,
            Event.captured_at <= ended_at,
        )
        res_events = await session.execute(stmt_events)
        events_in_pass = list(res_events.scalars().all())

        cov = coverage_for_pass(
            pass_id=pass_record.id,
            samples=parsed_samples,
            pothole_lat=pothole.lat,
            pothole_lon=pothole.lon,
            events_in_pass=events_in_pass,
            th=th_pq,
        )

        if cov is None:
            # Vehicle was too far laterally (> 6m); no crossing registered, no coverage row
            continue

        potholes_evaluated += 1

        # Upsert pass_coverage row
        cov_insert = (
            pg_insert(PassCoverage)
            .values(
                pass_id=pass_record.id,
                pothole_id=pothole.id,
                crossing_at=cov.crossing_at,
                clean=cov.clean,
                reason=cov.reason,
                window_samples=cov.window_samples,
                ok_fraction=cov.ok_fraction,
                computed_at=now_logical,
            )
            .on_conflict_do_update(
                index_elements=[PassCoverage.pass_id, PassCoverage.pothole_id],
                set_={
                    "crossing_at": cov.crossing_at,
                    "clean": cov.clean,
                    "reason": cov.reason,
                    "window_samples": cov.window_samples,
                    "ok_fraction": cov.ok_fraction,
                    "computed_at": now_logical,
                },
            )
        )
        await session.execute(cov_insert)

        # Re-evaluate verification state for this pothole
        from_status = pothole.status
        updated_pothole, decision = await evaluate_pothole(
            session=session,
            pothole_id=pothole.id,
            thresholds=thresholds,
            settings=settings,
            now=now_logical,
        )

        if decision.status_changed:
            status_changes.append(
                {
                    "pothole_id": str(pothole.id),
                    "from_status": from_status.value,
                    "to_status": updated_pothole.status.value,
                }
            )

    return {
        "pass_id": str(pass_id),
        "result": "accepted",
        "potholes_evaluated": potholes_evaluated,
        "status_changes": status_changes,
    }
