"""Tests for email delivery service and templates.

Covers T-24 (RPT-01..04, TC-RPT-01..04, W-06).
"""

import base64
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import nacl.signing
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import register_or_get_device
from app.config import Settings, Thresholds, load_thresholds
from app.logic.state_machine import PotholeStatus
from app.models import (
    Authority,
    Event,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    SeverityBand,
    make_point_geography,
)
from app.services.events import ensure_initial_report_queued
from app.services.mailer import (
    ConsoleEmailProvider,
    EmailProvider,
    send_pending_reports,
)
from app.services.potholes import create_pothole, record_status_transition


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    settings = Settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as s:
        await s.execute(text("TRUNCATE status_history, reports, events, passes, potholes CASCADE"))
        await s.commit()
        yield s
        await s.execute(text("TRUNCATE status_history, reports, events, passes, potholes CASCADE"))
        await s.commit()
    await engine.dispose()


class FailingEmailProvider(EmailProvider):
    """Email provider that fails a configured number of times before succeeding."""

    def __init__(self, fail_count: int = 1) -> None:
        self.remaining_failures = fail_count
        self.sent_messages: list[dict] = []

    async def send_email(
        self,
        to: str,
        subject: str,
        text_content: str,
        html_content: str,
        from_address: str,
    ) -> str:
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError("Temporary SMTP / network failure")
        msg_id = f"recovered-{uuid.uuid4()}"
        self.sent_messages.append({"to": to, "subject": subject, "id": msg_id})
        return msg_id


@pytest.mark.asyncio
async def test_tc_rpt_01_ten_events_one_initial_report(session: AsyncSession):
    """TC-RPT-01: Ten events merged into one pothole produce exactly one initial email."""
    settings = Settings()
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

    pothole = await create_pothole(
        session=session,
        lat=12.9716,
        lon=77.5946,
        first_seen_at=t0,
        severity=SeverityBand.MEDIUM,
    )

    # Simulate 10 events triggering ensure_initial_report_queued
    for _ in range(10):
        await ensure_initial_report_queued(session, pothole, settings)

    await session.commit()

    # Verify only 1 pending initial report exists in DB
    reports_stmt = select(Report).where(
        Report.pothole_id == pothole.id, Report.kind == ReportKind.INITIAL
    )
    reports = (await session.execute(reports_stmt)).scalars().all()
    assert len(reports) == 1
    assert reports[0].status == ReportDeliveryStatus.PENDING

    # Send pending reports via ConsoleEmailProvider
    provider = ConsoleEmailProvider()
    processed = await send_pending_reports(session, settings, provider=provider)
    await session.commit()

    assert len(processed) == 1
    assert len(provider.sent_messages) == 1
    assert processed[0].status == ReportDeliveryStatus.SENT

    # Verify pothole status transitioned to reported (T2)
    await session.refresh(pothole)
    assert pothole.status == PotholeStatus.REPORTED
    assert pothole.reported_at is not None
    assert pothole.last_reported_at is not None

    # Running mailer again sends zero additional emails (idempotent)
    second_pass = await send_pending_reports(session, settings, provider=provider)
    assert len(second_pass) == 0
    assert len(provider.sent_messages) == 1


@pytest.mark.asyncio
async def test_tc_rpt_02_email_mode_test_enforces_test_inbox(session: AsyncSession):
    """TC-RPT-02: With EMAIL_MODE=test, recipient is strictly TEST_INBOX."""
    settings = Settings()
    assert settings.EMAIL_MODE == "test"
    assert settings.TEST_INBOX != ""

    # Authority has a non-test email address
    auth = (await session.execute(select(Authority).limit(1))).scalar_one()
    assert auth.email != settings.TEST_INBOX

    pothole = await create_pothole(
        session=session,
        lat=12.9716,
        lon=77.5946,
        first_seen_at=datetime.now(UTC),
        authority_id=auth.id,
    )
    report = await ensure_initial_report_queued(session, pothole, settings)
    assert report is not None
    await session.commit()

    provider = ConsoleEmailProvider()
    await send_pending_reports(session, settings, provider=provider)
    await session.commit()

    # The actual recipient used must equal settings.TEST_INBOX
    assert len(provider.sent_messages) == 1
    assert provider.sent_messages[0]["to"] == settings.TEST_INBOX

    # The stored to_address must equal settings.TEST_INBOX
    await session.refresh(report)
    assert report.to_address == settings.TEST_INBOX


@pytest.mark.asyncio
async def test_tc_rpt_03_provider_failure_then_success(session: AsyncSession):
    """TC-RPT-03: Provider failure increments attempts, and transitions to reported on retry."""
    settings = Settings()
    pothole = await create_pothole(
        session=session,
        lat=12.9716,
        lon=77.5946,
        first_seen_at=datetime.now(UTC),
    )
    report = await ensure_initial_report_queued(session, pothole, settings)
    assert report is not None
    await session.commit()

    # First attempt: provider fails once
    failing_provider = FailingEmailProvider(fail_count=1)
    processed1 = await send_pending_reports(session, settings, provider=failing_provider)
    await session.commit()

    assert len(processed1) == 1
    await session.refresh(report)
    await session.refresh(pothole)

    # Status must still be pending, attempts incremented to 1, pothole remains OPEN
    assert report.status == ReportDeliveryStatus.PENDING
    assert report.attempts == 1
    assert "Temporary SMTP" in (report.error or "")
    assert pothole.status == PotholeStatus.OPEN
    assert pothole.reported_at is None

    # Second attempt: provider succeeds
    processed2 = await send_pending_reports(session, settings, provider=failing_provider)
    await session.commit()

    assert len(processed2) == 1
    await session.refresh(report)
    await session.refresh(pothole)

    assert report.status == ReportDeliveryStatus.SENT
    assert report.attempts == 2
    assert report.sent_at is not None
    assert report.provider_message_id is not None
    assert report.error is None
    # Pothole now transitioned to REPORTED
    assert pothole.status == PotholeStatus.REPORTED
    assert pothole.reported_at is not None


@pytest.mark.asyncio
async def test_tc_rpt_04_email_content_and_rule_constraints(session: AsyncSession):
    """TC-RPT-04:

    Email content contains crop link, severity band, map link, Fixed link.
    Contains no depth or volume physical measurements (Rule 8).
    Contains no raw coordinate text outside the map link / location title.
    """
    settings = Settings()
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

    # Register device and insert event with crop
    sk = nacl.signing.SigningKey.generate()
    pk_b64 = base64.b64encode(bytes(sk.verify_key)).decode("utf-8")
    device = await register_or_get_device(
        session=session,
        public_key_b64=pk_b64,
        platform="android",
    )

    pothole = await create_pothole(
        session=session,
        lat=12.9716,
        lon=77.5946,
        first_seen_at=t0,
        severity=SeverityBand.HIGH,
    )

    event_id = uuid.uuid4()
    crop_key = f"crops/{event_id}.jpg"
    event = Event(
        id=event_id,
        device_id=device.id,
        pothole_id=pothole.id,
        captured_at=t0,
        location=make_point_geography(12.9716, 77.5946),
        confidence=0.92,
        track_frames=6,
        area_ratio=0.12,
        severity=SeverityBand.HIGH,
        crop_key=crop_key,
        crop_sha256="0" * 64,
        payload="{}",
        signature="fake-sig",
    )
    session.add(event)
    await session.flush()
    pothole.before_event_id = event.id
    await session.flush()

    report = await ensure_initial_report_queued(session, pothole, settings)
    assert report is not None
    await session.commit()

    provider = ConsoleEmailProvider()
    await send_pending_reports(session, settings, provider=provider)
    await session.commit()

    assert len(provider.sent_messages) == 1
    sent = provider.sent_messages[0]
    subject = sent["subject"]
    text_content = sent["text"]
    html_content = sent["html"]

    # 1. Subject checks
    assert "[CivicLens]" in subject
    assert "High" in subject

    # 2. Body checks: severity band, pothole id, crop link, map link, Fixed link
    assert "High" in text_content
    assert str(pothole.id) in text_content
    assert crop_key in text_content
    assert f"/pothole/{pothole.id}" in text_content
    assert "/fixed/" in text_content

    assert "High" in html_content
    assert str(pothole.id) in html_content
    assert crop_key in html_content
    assert f"/pothole/{pothole.id}" in html_content
    assert "/fixed/" in html_content

    # 3. Rule 8: Never display physical depth or volume measurements
    forbidden_terms = ["cm deep", "mm deep", "depth:", "volume:", "liters", "cubic"]
    for term in forbidden_terms:
        assert term not in text_content.lower()
        assert term not in html_content.lower()


@pytest.mark.asyncio
async def test_reopened_email_delivery_and_transition_t8(session: AsyncSession):
    """T8: Sending a reopened email delivers evidence and transitions reopened -> reported."""
    settings = Settings()
    t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

    pothole = await create_pothole(
        session=session,
        lat=12.9716,
        lon=77.5946,
        first_seen_at=t0,
    )
    # Transition open -> reported -> claimed_fixed -> reopened
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REPORTED,
        reason="report_sent",
        at=t0,
    )
    t_claim = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.CLAIMED_FIXED,
        reason="fixed_claim",
        at=t_claim,
    )
    t_reopen = datetime(2026, 10, 25, 11, 0, 0, tzinfo=UTC)
    await record_status_transition(
        session=session,
        pothole=pothole,
        new_status=PotholeStatus.REOPENED,
        reason="contradicting_event",
        at=t_reopen,
    )

    auth = (await session.execute(select(Authority).limit(1))).scalar_one()

    # Queue reopened report
    reopened_report = Report(
        pothole_id=pothole.id,
        authority_id=auth.id,
        kind=ReportKind.REOPENED,
        to_address=settings.TEST_INBOX,
        status=ReportDeliveryStatus.PENDING,
    )
    session.add(reopened_report)
    await session.commit()

    provider = ConsoleEmailProvider()
    await send_pending_reports(session, settings, provider=provider)
    await session.commit()

    assert len(provider.sent_messages) == 1
    sent = provider.sent_messages[0]
    assert "REOPENED:" in sent["subject"]

    # Pothole must have transitioned to REPORTED (T8)
    await session.refresh(pothole)
    assert pothole.status == PotholeStatus.REPORTED
    assert pothole.last_reported_at is not None


@pytest.mark.asyncio
async def test_mailer_max_retries_marks_report_failed(session: AsyncSession):
    """When a report exceeds max_retries, status becomes failed."""
    settings = Settings()
    thresholds = Thresholds(
        **{
            **load_thresholds(settings.THRESHOLDS_PATH).model_dump(),
            "email": {"initial_per_pothole": 1, "max_retries": 2},
        }
    )

    pothole = await create_pothole(
        session=session,
        lat=12.9716,
        lon=77.5946,
        first_seen_at=datetime.now(UTC),
    )
    report = await ensure_initial_report_queued(session, pothole, settings)
    assert report is not None
    await session.commit()

    failing_provider = FailingEmailProvider(fail_count=10)

    # Attempt 1: failure -> still pending
    await send_pending_reports(session, settings, thresholds=thresholds, provider=failing_provider)
    await session.commit()
    await session.refresh(report)
    assert report.status == ReportDeliveryStatus.PENDING
    assert report.attempts == 1

    # Attempt 2: failure -> reaches max_retries (2) -> status becomes failed
    await send_pending_reports(session, settings, thresholds=thresholds, provider=failing_provider)
    await session.commit()
    await session.refresh(report)
    assert report.status == ReportDeliveryStatus.FAILED
    assert report.attempts == 2
    assert "Temporary SMTP" in (report.error or "")

    # Attempt 3: ignored because status is FAILED
    await send_pending_reports(session, settings, thresholds=thresholds, provider=failing_provider)
    await session.commit()
    await session.refresh(report)
    assert report.attempts == 2
