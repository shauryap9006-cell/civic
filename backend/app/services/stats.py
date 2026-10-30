"""Service for computing aggregate civic statistics.

Follows openapi.yaml #/components/schemas/Stats and docs/PRD.md WEB-04.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logic.state_machine import PotholeStatus
from app.models import Pass, Pothole, Report, ReportDeliveryStatus


async def compute_stats(session: AsyncSession) -> dict[str, Any]:
    """Compute aggregate defect, repair verification, and distance statistics.

    Returns:
        dict matching openapi.yaml Stats schema:
        - potholes_total: Total count of rows in potholes.
        - by_status: Dict mapping every PotholeStatus to its count (defaulting to 0).
        - reports_sent: Total count of reports where status = 'sent'.
        - claimed_fixed: Current count where status = 'claimed_fixed'.
        - verified: Current count where status = 'verified'.
        - reopened: Current count where status = 'reopened'.
        - unknown: Current count where status = 'unknown'.
        - km_covered: Sum of ST_Length(path) in kilometers (path is Geography in meters / 1000.0),
          rounded to 2 decimal places.
    """
    # 1. Total potholes
    total_res = await session.execute(select(func.count(Pothole.id)))
    potholes_total = int(total_res.scalar_one() or 0)

    # 2. Counts grouped by status
    status_stmt = select(Pothole.status, func.count(Pothole.id)).group_by(Pothole.status)
    status_res = await session.execute(status_stmt)
    status_counts = {
        row[0].value if hasattr(row[0], "value") else str(row[0]): int(row[1])
        for row in status_res.all()
    }

    # Ensure all 6 statuses are present
    by_status = {s.value: status_counts.get(s.value, 0) for s in PotholeStatus}

    # 3. Reports sent
    rep_res = await session.execute(
        select(func.count(Report.id)).where(Report.status == ReportDeliveryStatus.SENT)
    )
    reports_sent = int(rep_res.scalar_one() or 0)

    # 4. km_covered: Sum of PostGIS ST_Length(path) for passes where path is not null
    # Note: path is Geography(LineString, 4326). ST_Length(path) calculates meters.
    # Convert meters to km by dividing by 1000.0.
    km_stmt = select(func.coalesce(func.sum(func.ST_Length(Pass.path)) / 1000.0, 0.0))
    km_res = await session.execute(km_stmt)
    km_raw = km_res.scalar_one() or 0.0
    km_covered = round(float(km_raw), 2)

    return {
        "potholes_total": potholes_total,
        "by_status": by_status,
        "reports_sent": reports_sent,
        "claimed_fixed": by_status[PotholeStatus.CLAIMED_FIXED.value],
        "verified": by_status[PotholeStatus.VERIFIED.value],
        "reopened": by_status[PotholeStatus.REOPENED.value],
        "unknown": by_status[PotholeStatus.UNKNOWN.value],
        "km_covered": km_covered,
    }
