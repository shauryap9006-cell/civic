"""Escalation scheduler and evaluation service for CivicLens.

Implements T-29 (ESC-01..03, TC-ESC-01..03, STATE_MACHINE.md § 2, WORKFLOWS.md W-11).
Transitions:
- T9 (reminder_sent): 14 days after report if not claimed
- T10 (escalation_sent): 28 days after report if not claimed
Guarantees Hard Rule 1: In test mode, emails go to settings.TEST_INBOX.
Guarantees Hard Rule 4: Timestamps come from clock.now(session).
Guarantees Idempotency: Repeating in same state produces no duplicate reports/history rows.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import clock
from app.config import Settings, Thresholds, load_thresholds
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.models import (
    Authority,
    Pothole,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    StatusHistory,
)
from app.services.mailer import send_pending_reports

logger = logging.getLogger(__name__)


async def check_and_escalate(
    session: AsyncSession,
    now: datetime | None = None,
    settings: Settings | None = None,
    thresholds: Thresholds | None = None,
    send_reports: bool = True,
) -> list[Report]:
    """Check potholes in 'reported' status and queue/send reminder or escalation reports.

    Follows W-11 and STATE_MACHINE.md:
    1. Select potholes with status 'reported'.
    2. Current cycle start = pothole.last_reported_at (or reported_at).
    3. age = now - cycle_start:
       - If age >= escalate_after_days (default 28) and no escalation sent this cycle:
         Queue escalation report, record status_history (reason='escalation_sent').
       - Else if age >= reminder_after_days (default 14) and no reminder or escalation sent:
         Queue reminder report, record status_history (reason='reminder_sent').
    4. If send_reports is True, deliver pending reports via send_pending_reports,
       preserving cycle_start so T9 does not corrupt the 28-day escalation timer.

    Returns:
        List of newly created reports.
    """
    if settings is None:
        settings = Settings()
    if thresholds is None:
        thresholds = load_thresholds(settings.THRESHOLDS_PATH)
    if now is None:
        now = await clock.now(session)

    reminder_days = thresholds.escalation.reminder_after_days
    escalate_days = thresholds.escalation.escalate_after_days

    # 1. Fetch potholes in REPORTED status with exclusive row lock
    stmt = select(Pothole).where(Pothole.status == PotholeStatus.REPORTED).with_for_update()
    res = await session.execute(stmt)
    potholes = res.scalars().all()

    new_reports: list[Report] = []
    potholes_to_restore_cycle: dict[Pothole, datetime] = {}

    for pothole in potholes:
        cycle_start = pothole.last_reported_at or pothole.reported_at
        if cycle_start is None:
            continue

        age_seconds = (now - cycle_start).total_seconds()
        age_days = age_seconds / 86400.0

        # Query existing reports in the current cycle
        rep_stmt = select(Report).where(
            Report.pothole_id == pothole.id,
            Report.created_at >= cycle_start,
        )
        rep_res = await session.execute(rep_stmt)
        cycle_reports = rep_res.scalars().all()

        has_reminder = any(r.kind == ReportKind.REMINDER for r in cycle_reports)
        has_escalation = any(r.kind == ReportKind.ESCALATION for r in cycle_reports)

        # Authority contact lookup
        auth: Authority | None = None
        if pothole.authority_id is not None:
            auth_res = await session.execute(
                select(Authority).where(Authority.id == pothole.authority_id)
            )
            auth = auth_res.scalar_one_or_none()

        primary_email = auth.email if (auth and auth.email) else settings.TEST_INBOX
        escalation_email = (
            auth.escalation_email if (auth and auth.escalation_email) else primary_email
        )

        if settings.EMAIL_MODE == "test":
            primary_email = settings.TEST_INBOX
            escalation_email = settings.TEST_INBOX

        # Decision logic:
        # T10: Escalation check (28 days) takes precedence over reminder
        if age_days >= escalate_days and not has_escalation:
            report = Report(
                pothole_id=pothole.id,
                authority_id=pothole.authority_id or (auth.id if auth else None),
                kind=ReportKind.ESCALATION,
                to_address=escalation_email,
                status=ReportDeliveryStatus.PENDING,
                created_at=now,
            )
            session.add(report)
            new_reports.append(report)
            potholes_to_restore_cycle[pothole] = cycle_start

            if not send_reports:
                history = StatusHistory(
                    pothole_id=pothole.id,
                    from_status=PotholeStatus.REPORTED,
                    to_status=PotholeStatus.REPORTED,
                    reason=TransitionReason.ESCALATION_SENT.value,
                    evidence={},
                    at=now,
                )
                session.add(history)

        # T9: Reminder check (14 days)
        elif age_days >= reminder_days and not has_reminder and not has_escalation:
            report = Report(
                pothole_id=pothole.id,
                authority_id=pothole.authority_id or (auth.id if auth else None),
                kind=ReportKind.REMINDER,
                to_address=primary_email,
                status=ReportDeliveryStatus.PENDING,
                created_at=now,
            )
            session.add(report)
            new_reports.append(report)
            potholes_to_restore_cycle[pothole] = cycle_start

            if not send_reports:
                history = StatusHistory(
                    pothole_id=pothole.id,
                    from_status=PotholeStatus.REPORTED,
                    to_status=PotholeStatus.REPORTED,
                    reason=TransitionReason.REMINDER_SENT.value,
                    evidence={},
                    at=now,
                )
                session.add(history)

    await session.flush()

    if send_reports and new_reports:
        # Deliver pending reports through mailer service
        await send_pending_reports(session, settings, thresholds)
        # Restore cycle start timestamp so T9/T10 transitions do not reset the escalation timer
        for pothole, original_cycle_start in potholes_to_restore_cycle.items():
            pothole.last_reported_at = original_cycle_start
        await session.flush()

    return new_reports
