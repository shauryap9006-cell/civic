"""Admin operations service for CivicLens demo management.

Implements T-29 (ESC-03, WEB-05, TC-DEMO-01..02, WORKFLOWS.md W-12).
Provides:
- simulate_time: Advances simulated clock, runs escalation checks and verification re-evaluations
- reset_demo_state: Wipes transactional data, resets clock offset, preserves reference data
- verify_admin_access: Enforces DEMO_MODE guard and X-Admin-Secret authentication
"""

import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.config import Settings, Thresholds
from app.logic.state_machine import PotholeStatus
from app.models import Pothole
from app.services.escalation import check_and_escalate
from app.services.mailer import send_pending_reports
from app.services.potholes import evaluate_pothole

logger = logging.getLogger(__name__)


def verify_admin_access(x_admin_secret: str | None, settings: Settings) -> None:
    """Verify demo mode eligibility and admin secret.

    Follows NFR-05 and openapi.yaml:
    - If DEMO_MODE is False or APP_ENV is 'production' -> 404 not_found
    - If X-Admin-Secret header does not match settings.ADMIN_SECRET -> 401 unauthorized
    """
    if not settings.DEMO_MODE or settings.APP_ENV == "production":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "not_found",
                    "message": "Demo endpoints are disabled in this environment",
                }
            },
        )

    if not x_admin_secret or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid admin secret",
                }
            },
        )


async def simulate_time(
    session: AsyncSession,
    advance_seconds: int,
    settings: Settings,
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Advance simulated clock by advance_seconds and trigger escalations and re-evaluations.

    Returns dict matching openapi.yaml SimulateTimeResponse:
    - now: New simulated server timestamp (ISO 8601)
    - clock_offset_seconds: Total simulated offset
    - reports_sent: Number of reports delivered during simulation
    - status_changes: List of { pothole_id, from_status, to_status }
    """
    # 1. Advance simulated clock offset
    new_offset = await clock.advance_offset_seconds(session, advance_seconds)
    logical_now = await clock.now(session)

    status_changes: list[dict[str, str]] = []

    # 2. Check and send escalations
    escalation_reports = await check_and_escalate(
        session=session,
        now=logical_now,
        settings=settings,
        thresholds=thresholds,
        send_reports=True,
    )
    for rep in escalation_reports:
        status_changes.append(
            {
                "pothole_id": str(rep.pothole_id),
                "from_status": PotholeStatus.REPORTED.value,
                "to_status": PotholeStatus.REPORTED.value,
            }
        )

    # 3. Re-evaluate candidate potholes (claimed_fixed, unknown, verified)
    cand_stmt = select(Pothole.id).where(
        Pothole.status.in_(
            [
                PotholeStatus.CLAIMED_FIXED,
                PotholeStatus.UNKNOWN,
                PotholeStatus.VERIFIED,
            ]
        )
    )
    cand_res = await session.execute(cand_stmt)
    candidate_ids = cand_res.scalars().all()

    for pid in candidate_ids:
        p, decision = await evaluate_pothole(
            session=session,
            pothole_id=pid,
            thresholds=thresholds,
            settings=settings,
            now=logical_now,
        )
        if decision.status_changed and decision.new_status is not None:
            status_changes.append(
                {
                    "pothole_id": str(pid),
                    "from_status": decision.old_status,
                    "to_status": decision.new_status,
                }
            )

    # 4. Deliver any newly queued reports (e.g. from reopened potholes)
    extra_sent = await send_pending_reports(session, settings, thresholds)
    total_reports_sent = len(escalation_reports) + len(extra_sent)

    await session.commit()

    return {
        "now": logical_now.isoformat(),
        "clock_offset_seconds": new_offset,
        "reports_sent": total_reports_sent,
        "status_changes": status_changes,
    }


async def reset_demo_state(session: AsyncSession) -> dict[str, str]:
    """Wipe transactional data and reset clock offset, preserving devices and authorities."""
    await session.execute(text("UPDATE potholes SET before_event_id = NULL"))
    for tbl in ["status_history", "pass_coverage", "reports", "events", "passes", "potholes"]:
        await session.execute(text(f"DELETE FROM {tbl}"))
    await clock.set_offset_seconds(session, 0)
    await session.commit()
    return {"status": "reset"}
