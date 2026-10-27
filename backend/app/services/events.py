"""Event ingestion service: validation, signature checks, deduplication, and merge.

Follows W-05 and EVT-01..03, MRG-01..03, TC-ING-01..06, TC-MRG-01..04.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import verify_envelope_signature
from app.config import Settings, Thresholds
from app.logic.merge import recompute_canonical_location
from app.logic.severity import SeverityThresholds, evaluate_severity
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.models import (
    Authority,
    Device,
    Event,
    Pothole,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    SeverityBand,
    make_point_geography,
)
from app.services.potholes import (
    create_pothole,
    find_nearest_pothole,
    record_status_transition,
    update_pothole_location_and_severity,
)
from app.storage import verify_crop


async def ensure_initial_report_queued(
    session: AsyncSession,
    pothole: Pothole,
    settings: Settings,
) -> Report | None:
    """Ensure exactly one pending initial report is queued for an open pothole."""
    stmt = (
        select(Report.id)
        .where(Report.pothole_id == pothole.id, Report.kind == ReportKind.INITIAL)
        .limit(1)
    )
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is not None:
        return None

    authority_email = settings.TEST_INBOX
    auth_id = pothole.authority_id

    if settings.EMAIL_MODE != "test" and auth_id is not None:
        auth_stmt = select(Authority.email).where(Authority.id == auth_id)
        auth_res = await session.execute(auth_stmt)
        email_val = auth_res.scalar_one_or_none()
        if email_val:
            authority_email = email_val

    if auth_id is None:
        default_auth = await session.execute(
            select(Authority.id).order_by(Authority.created_at.asc()).limit(1)
        )
        auth_id = default_auth.scalar_one_or_none()

    if auth_id is None:
        return None

    report = Report(
        pothole_id=pothole.id,
        authority_id=auth_id,
        kind=ReportKind.INITIAL,
        to_address=authority_email,
        status=ReportDeliveryStatus.PENDING,
    )
    session.add(report)
    await session.flush()
    return report


async def ensure_reopened_report_queued(
    session: AsyncSession,
    pothole: Pothole,
    settings: Settings,
) -> Report | None:
    """Ensure a pending reopened report is queued for a reopened pothole."""
    stmt = (
        select(Report.id)
        .where(
            Report.pothole_id == pothole.id,
            Report.kind == ReportKind.REOPENED,
            Report.status == ReportDeliveryStatus.PENDING,
        )
        .limit(1)
    )
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is not None:
        return None

    authority_email = settings.TEST_INBOX
    auth_id = pothole.authority_id

    if settings.EMAIL_MODE != "test" and auth_id is not None:
        auth_stmt = select(Authority.email).where(Authority.id == auth_id)
        auth_res = await session.execute(auth_stmt)
        email_val = auth_res.scalar_one_or_none()
        if email_val:
            authority_email = email_val

    if auth_id is None:
        default_auth = await session.execute(
            select(Authority.id).order_by(Authority.created_at.asc()).limit(1)
        )
        auth_id = default_auth.scalar_one_or_none()

    if auth_id is None:
        return None

    report = Report(
        pothole_id=pothole.id,
        authority_id=auth_id,
        kind=ReportKind.REOPENED,
        to_address=authority_email,
        status=ReportDeliveryStatus.PENDING,
    )
    session.add(report)
    await session.flush()
    return report


async def ingest_single_event(
    session: AsyncSession,
    device: Device,
    payload_str: str,
    signature_b64: str,
    settings: Settings,
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Process one signed event envelope according to W-05.

    Returns an EventResult dictionary.
    """
    # 1. Parse JSON payload early to extract ID if present
    try:
        data = json.loads(payload_str)
    except Exception:
        return {
            "event_id": str(uuid.uuid4()),
            "result": "rejected",
            "reason": "malformed_payload",
        }

    raw_id = data.get("id")
    try:
        event_id = uuid.UUID(str(raw_id))
    except Exception:
        return {
            "event_id": str(uuid.uuid4()),
            "result": "rejected",
            "reason": "malformed_payload",
        }

    # 2. Verify individual envelope signature against device's public key (TC-ING-01)
    if not verify_envelope_signature(device.public_key, payload_str, signature_b64):
        return {
            "event_id": str(event_id),
            "result": "rejected",
            "reason": "invalid_signature",
        }

    # 3. Validate device_id matches X-Device-Id (TC-ING-04)
    payload_device_id = data.get("device_id")
    try:
        if uuid.UUID(str(payload_device_id)) != device.id:
            return {
                "event_id": str(event_id),
                "result": "rejected",
                "reason": "device_mismatch",
            }
    except Exception:
        return {
            "event_id": str(event_id),
            "result": "rejected",
            "reason": "device_mismatch",
        }

    # 4. Validate coordinates (EVT-01)
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return {
                "event_id": str(event_id),
                "result": "rejected",
                "reason": "invalid_coordinates",
            }
    except (KeyError, ValueError, TypeError):
        return {
            "event_id": str(event_id),
            "result": "rejected",
            "reason": "malformed_payload",
        }

    # Validate remaining required payload fields
    try:
        captured_at_str = data["captured_at"]
        captured_at = datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
        confidence = float(data["confidence"])
        track_frames = int(data["track_frames"])
        crop_key = str(data["crop_key"])
        crop_sha256 = str(data["crop_sha256"])
        area_ratio = float(data.get("area_ratio") or 0.05)
    except Exception:
        return {
            "event_id": str(event_id),
            "result": "rejected",
            "reason": "malformed_payload",
        }

    # 5. Check duplicate (TC-ING-03)
    existing_event = await session.execute(select(Event.id).where(Event.id == event_id))
    if existing_event.scalar_one_or_none() is not None:
        return {
            "event_id": str(event_id),
            "result": "duplicate",
        }

    # 6. Check storage for crop and hash verification (TC-ING-05)
    ok, crop_reason = verify_crop(settings, crop_key, crop_sha256)
    if not ok:
        return {
            "event_id": str(event_id),
            "result": "rejected",
            "reason": crop_reason,
        }

    # 7. Merge into nearest pothole or create new pothole (MRG-01, TC-MRG-01..04)
    sev_th = SeverityThresholds(
        area_ratio_saturation=thresholds.severity.area_ratio_saturation,
        repeat_saturation=thresholds.severity.repeat_saturation,
        weight_area=thresholds.severity.weight_area,
        weight_confidence=thresholds.severity.weight_confidence,
        weight_repeat=thresholds.severity.weight_repeat,
        band_medium_min=thresholds.severity.band_medium_min,
        band_high_min=thresholds.severity.band_high_min,
    )

    nearest = await find_nearest_pothole(
        session, lat, lon, radius_m=thresholds.merge.radius_m, for_update=True
    )

    if nearest is None:
        # Create new pothole (T1 first_event)
        _, band_str = evaluate_severity(area_ratio, confidence, 1, sev_th)
        pothole = await create_pothole(
            session=session,
            lat=lat,
            lon=lon,
            first_seen_at=captured_at,
            severity=SeverityBand(band_str),
            initial_status=PotholeStatus.OPEN,
            before_event_id=None,
            event_count=1,
        )
        await ensure_initial_report_queued(session, pothole, settings)
    else:
        pothole, _ = nearest
        # Recompute canonical location (confidence-weighted mean)
        existing_events_res = await session.execute(
            select(Event).where(Event.pothole_id == pothole.id)
        )
        existing_events = existing_events_res.scalars().all()
        coords_and_conf = [(e.lat, e.lon, e.confidence) for e in existing_events]
        coords_and_conf.append((lat, lon, confidence))
        new_lat, new_lon = recompute_canonical_location(coords_and_conf)

        # Recompute severity with updated event_count
        new_count = pothole.event_count + 1
        _, band_str = evaluate_severity(area_ratio, confidence, new_count, sev_th)

        await update_pothole_location_and_severity(
            session=session,
            pothole=pothole,
            lat=new_lat,
            lon=new_lon,
            severity=SeverityBand(band_str),
            event_count=new_count,
            last_event_at=captured_at,
        )

        # Contradiction check: T7 Reopen if claimed_fixed, unknown, or verified
        if pothole.status in (
            PotholeStatus.CLAIMED_FIXED,
            PotholeStatus.UNKNOWN,
            PotholeStatus.VERIFIED,
        ):
            if (
                pothole.claimed_fixed_at
                and captured_at > pothole.claimed_fixed_at
                and confidence >= thresholds.verification.reopen_min_event_confidence
            ):
                await record_status_transition(
                    session=session,
                    pothole=pothole,
                    new_status=PotholeStatus.REOPENED,
                    reason=TransitionReason.CONTRADICTING_EVENT,
                    evidence={
                        "before_event_id": str(pothole.before_event_id)
                        if pothole.before_event_id
                        else None,
                        "after_event_ids": [str(event_id)],
                        "claimed_fixed_at": pothole.claimed_fixed_at.isoformat(),
                    },
                    at=captured_at,
                )
                await ensure_reopened_report_queued(session, pothole, settings)

    # 8. Insert new Event record
    pass_id_val = None
    if data.get("pass_id"):
        try:
            pass_id_val = uuid.UUID(str(data["pass_id"]))
        except ValueError:
            pass_id_val = None

    new_event = Event(
        id=event_id,
        device_id=device.id,
        pass_id=pass_id_val,
        pothole_id=pothole.id,
        captured_at=captured_at,
        location=make_point_geography(lat, lon),
        gps_accuracy_m=data.get("gps_accuracy_m"),
        heading_deg=data.get("heading_deg"),
        speed_mps=data.get("speed_mps"),
        confidence=confidence,
        track_frames=track_frames,
        area_ratio=data.get("area_ratio"),
        severity=SeverityBand(band_str),
        crop_key=crop_key,
        crop_sha256=crop_sha256,
        sharpness=data.get("sharpness"),
        mean_luma=data.get("mean_luma"),
        model_version=data.get("model_version"),
        app_version=data.get("app_version"),
        payload=payload_str,
        signature=signature_b64,
    )
    session.add(new_event)
    await session.flush()

    if pothole.before_event_id is None:
        pothole.before_event_id = event_id
        await session.flush()

    return {
        "event_id": str(event_id),
        "result": "accepted",
        "pothole_id": str(pothole.id),
        "pothole_status": pothole.status.value,
    }
