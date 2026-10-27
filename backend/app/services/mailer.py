"""Email delivery service for CivicLens authority reports.

Implements RPT-01..04, W-06, and TC-RPT-01..04.
Supports console logging provider (local/test) and Resend API provider (HTTP).
Guarantees Hard Rule 1: In test mode (EMAIL_MODE=test), every email goes to TEST_INBOX.
Guarantees Hard Rule 4: Timestamps come from clock.now(session).
Guarantees Hard Rule 8: Severity is graded, not measured.
Guarantees Hard Rule 11: Pothole status updates strictly via record_status_transition.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import derive_fixed_token, hash_fixed_token
from app.clock import clock
from app.config import Settings, Thresholds, load_thresholds
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.mailer.templates import render_report_email
from app.models import (
    Authority,
    Event,
    Pothole,
    Report,
    ReportDeliveryStatus,
    ReportKind,
)
from app.services.potholes import record_status_transition
from app.storage import generate_presigned_get

logger = logging.getLogger(__name__)


class EmailProvider(ABC):
    """Abstract interface for email delivery providers."""

    @abstractmethod
    async def send_email(
        self,
        to: str,
        subject: str,
        text_content: str,
        html_content: str,
        from_address: str,
    ) -> str:
        """Deliver one email. Returns provider message ID. Raises exception on failure."""
        pass


class ConsoleEmailProvider(EmailProvider):
    """Local console / mock provider for development and automated testing."""

    def __init__(self, sink: list[dict[str, Any]] | None = None) -> None:
        self.sent_messages: list[dict[str, Any]] = sink if sink is not None else []

    async def send_email(
        self,
        to: str,
        subject: str,
        text_content: str,
        html_content: str,
        from_address: str,
    ) -> str:
        msg_id = f"console-{uuid.uuid4()}"
        payload = {
            "id": msg_id,
            "to": to,
            "from": from_address,
            "subject": subject,
            "text": text_content,
            "html": html_content,
        }
        self.sent_messages.append(payload)
        logger.info(
            "email_sent provider=console to=%s subject=%s msg_id=%s",
            to,
            subject,
            msg_id,
        )
        return msg_id


class ResendEmailProvider(EmailProvider):
    """Resend HTTP API email delivery provider."""

    def __init__(self, api_key: str, endpoint: str = "https://api.resend.com/emails") -> None:
        self.api_key = api_key
        self.endpoint = endpoint

    async def send_email(
        self,
        to: str,
        subject: str,
        text_content: str,
        html_content: str,
        from_address: str,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = {
            "from": from_address,
            "to": [to],
            "subject": subject,
            "text": text_content,
            "html": html_content,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(self.endpoint, headers=headers, json=data)
            if resp.status_code >= 400:
                raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")
            result = resp.json()
            return str(result.get("id") or f"resend-{uuid.uuid4()}")


def get_email_provider(settings: Settings) -> EmailProvider:
    """Factory for email provider based on application settings."""
    if settings.EMAIL_PROVIDER == "resend" and settings.RESEND_API_KEY:
        return ResendEmailProvider(settings.RESEND_API_KEY)
    return ConsoleEmailProvider()


async def send_pending_reports(
    session: AsyncSession,
    settings: Settings,
    thresholds: Thresholds | None = None,
    provider: EmailProvider | None = None,
) -> list[Report]:
    """Process pending reports from the reports table.

    Follows W-06 and RPT-01..04:
    - In test mode (EMAIL_MODE=test), strictly sends to settings.TEST_INBOX.
    - Successfully sending initial report applies T2 (open -> reported).
    - Successfully sending reopened report applies T8 (reopened -> reported).
    - Records attempts, sent_at, provider_message_id, or error.
    - Idempotent: reports with status 'sent' or max retries exceeded are skipped.
    """
    if thresholds is None:
        thresholds = load_thresholds(settings.THRESHOLDS_PATH)
    if provider is None:
        provider = get_email_provider(settings)

    max_retries = thresholds.email.max_retries

    # 1. Fetch pending reports eligible for delivery
    stmt = (
        select(Report)
        .where(
            Report.status == ReportDeliveryStatus.PENDING,
            Report.attempts < max_retries,
        )
        .order_by(Report.created_at.asc())
        .with_for_update()
    )
    result = await session.execute(stmt)
    pending_reports = list(result.scalars().all())

    processed_reports: list[Report] = []

    for report in pending_reports:
        # Enforce Hard Rule 1: Test inbox only in test mode
        if settings.EMAIL_MODE == "test":
            recipient = settings.TEST_INBOX
            report.to_address = settings.TEST_INBOX
        else:
            recipient = report.to_address

        # Fetch associated pothole and authority
        pothole_res = await session.execute(
            select(Pothole).where(Pothole.id == report.pothole_id).with_for_update()
        )
        pothole = pothole_res.scalar_one_or_none()
        if pothole is None:
            continue

        authority_res = await session.execute(
            select(Authority).where(Authority.id == report.authority_id)
        )
        authority = authority_res.scalar_one_or_none()

        # Derive current Fixed token & update hash
        token = derive_fixed_token(
            pothole_id=pothole.id,
            epoch=pothole.fixed_token_epoch,
            secret=settings.FIXED_TOKEN_SECRET,
        )
        pothole.fixed_token_hash = hash_fixed_token(token)

        # Collect evidence crop URLs
        crop_urls: list[str] = []
        before_event: Event | None = None
        if pothole.before_event_id:
            before_res = await session.execute(
                select(Event).where(Event.id == pothole.before_event_id)
            )
            before_event = before_res.scalar_one_or_none()

        if before_event is None:
            fallback_res = await session.execute(
                select(Event)
                .where(Event.pothole_id == pothole.id)
                .order_by(Event.captured_at.asc())
                .limit(1)
            )
            before_event = fallback_res.scalar_one_or_none()

        if before_event:
            before_url = generate_presigned_get(settings, before_event.crop_key)
            crop_urls.append(before_url)

        # For reopened reports, include contradicting after crops
        if report.kind == ReportKind.REOPENED and pothole.claimed_fixed_at:
            after_res = await session.execute(
                select(Event)
                .where(
                    Event.pothole_id == pothole.id,
                    Event.captured_at > pothole.claimed_fixed_at,
                )
                .order_by(Event.captured_at.asc())
            )
            after_events = after_res.scalars().all()
            for ae in after_events:
                if ae.id != (before_event.id if before_event else None):
                    crop_urls.append(generate_presigned_get(settings, ae.crop_key))

        # Build map and fixed links
        map_url = f"{settings.PUBLIC_BASE_URL}/pothole/{pothole.id}"
        fixed_url = f"{settings.PUBLIC_BASE_URL}/fixed/{token}"

        # Render email content
        subject, text_content, html_content = render_report_email(
            kind=report.kind,
            pothole=pothole,
            authority=authority,
            crop_urls=crop_urls,
            map_url=map_url,
            fixed_url=fixed_url,
        )

        now = await clock.now(session)

        # Send via email provider
        try:
            msg_id = await provider.send_email(
                to=recipient,
                subject=subject,
                text_content=text_content,
                html_content=html_content,
                from_address=settings.EMAIL_FROM,
            )
            report.status = ReportDeliveryStatus.SENT
            report.sent_at = now
            report.provider_message_id = msg_id
            report.attempts += 1
            report.error = None

            # Apply state machine transition on successful send
            if report.kind == ReportKind.INITIAL and pothole.status == PotholeStatus.OPEN:
                await record_status_transition(
                    session=session,
                    pothole=pothole,
                    new_status=PotholeStatus.REPORTED,
                    reason=TransitionReason.REPORT_SENT,
                    at=now,
                )
            elif report.kind == ReportKind.REOPENED and pothole.status == PotholeStatus.REOPENED:
                await record_status_transition(
                    session=session,
                    pothole=pothole,
                    new_status=PotholeStatus.REPORTED,
                    reason=TransitionReason.REOPEN_REPORT_SENT,
                    at=now,
                )
            elif report.kind == ReportKind.REMINDER and pothole.status == PotholeStatus.REPORTED:
                await record_status_transition(
                    session=session,
                    pothole=pothole,
                    new_status=PotholeStatus.REPORTED,
                    reason=TransitionReason.REMINDER_SENT,
                    at=now,
                )
            elif report.kind == ReportKind.ESCALATION and pothole.status == PotholeStatus.REPORTED:
                await record_status_transition(
                    session=session,
                    pothole=pothole,
                    new_status=PotholeStatus.REPORTED,
                    reason=TransitionReason.ESCALATION_SENT,
                    at=now,
                )

        except Exception as exc:
            report.attempts += 1
            report.error = str(exc)
            if report.attempts >= max_retries:
                report.status = ReportDeliveryStatus.FAILED

        await session.flush()
        processed_reports.append(report)

    return processed_reports
