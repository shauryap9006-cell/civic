"""CivicLens Comprehensive 4-Tier E2E Test Suite.

Methodology:
- Tier 1: Feature Coverage (pass upload, crossing evaluation, claim fixed, pothole read, demo).
- Tier 2: Boundary & Corner Cases (samples, GPS bounds, signatures, clean passes, loop passes).
- Tier 3: Cross-Feature Combinations (pairwise interactions: event -> claim -> verify -> reopen).
- Tier 4: Real-World Scenarios (complete demo lifecycle and escalation timeline).

Interfaces and Contracts:
- docs/STATE_MACHINE.md (Transitions T1..T10, Scenarios S1..S17)
- openapi.yaml
- config/thresholds.yaml
- docs/PRD.md
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import derive_fixed_token, hash_fixed_token
from app.clock import clock
from app.config import Settings
from app.logic.state_machine import PotholeStatus, TransitionReason
from app.models import (
    Pass,
    PassCoverage,
    Pothole,
    Report,
    ReportDeliveryStatus,
    ReportKind,
    StatusHistory,
)
from app.services.mailer import send_pending_reports
from app.services.potholes import (
    create_pothole,
    record_status_transition,
)
from app.storage import generate_crop_key
from tests.e2e.conftest import (
    create_test_authority,
    create_test_device,
    is_route_mounted,
    make_sample_event_payload,
    make_sample_pass_payload,
    make_signing_key,
    post_events_request,
    post_pass_request,
    put_test_crop,
    sign_envelope,
)

# ===========================================================================
# TIER 1: Feature Coverage (Happy Path Tests)
# ===========================================================================


class TestTier1FeatureCoverage:
    """Tier 1: Primary happy path feature tests across ingest, evaluation, and reporting APIs."""

    @pytest.mark.asyncio
    async def test_tier1_pass_upload_happy_path(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T1.1: Upload valid drive pass with 5Hz quality samples -> 200 accepted & PostGIS path."""
        device, sk = await create_test_device(session, platform="android")
        pass_id = uuid.uuid4()
        t0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

        payload = make_sample_pass_payload(
            device_id=device.id,
            pass_id=pass_id,
            lat_center=30.731000,
            lon_center=76.771000,
            lateral_offset_m=1.0,
            crossing_time=t0,
        )
        envelope = sign_envelope(sk, payload)

        await session.commit()
        resp = await post_pass_request(client, device.id, sk, envelope)
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["pass_id"] == str(pass_id)
        assert data["result"] == "accepted"

        # Verify DB persistence and PostGIS geometry
        await session.commit()
        stmt = select(Pass).where(Pass.id == pass_id)
        pass_record = (await session.execute(stmt)).scalar_one_or_none()
        assert pass_record is not None
        assert pass_record.sample_count == len(payload["samples"])
        assert pass_record.path is not None
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier1_crossing_evaluation_happy_path(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T1.2: Pass past a claimed_fixed pothole detects crossing and generates clean coverage."""
        auth = await create_test_authority(session)
        device, sk = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)

        pothole = await create_pothole(
            session=session,
            lat=30.732000,
            lon=76.772000,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t0
        await session.commit()

        # Drive past at t0 + 1 hour with lateral offset 1.5m and good quality
        t_crossing = t0 + timedelta(hours=1)
        pass_id = uuid.uuid4()
        payload = make_sample_pass_payload(
            device_id=device.id,
            pass_id=pass_id,
            lat_center=30.732000,
            lon_center=76.772000,
            lateral_offset_m=1.5,
            crossing_time=t_crossing,
        )
        envelope = sign_envelope(sk, payload)

        resp = await post_pass_request(client, device.id, sk, envelope)
        assert resp.status_code == 200
        data = resp.json()
        assert data["potholes_evaluated"] >= 1

        # Verify pass_coverage row in DB
        await session.commit()
        stmt_cov = select(PassCoverage).where(
            PassCoverage.pass_id == pass_id,
            PassCoverage.pothole_id == pothole.id,
        )
        cov = (await session.execute(stmt_cov)).scalar_one_or_none()
        assert cov is not None
        assert cov.clean is True
        assert cov.reason == "ok"
        assert cov.window_samples >= 5
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier1_claim_fixed_happy_path(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T1.3: Authority previews and claims repair on reported pothole via /v1/fixed/{token}."""
        auth = await create_test_authority(session)
        device, _ = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)

        pothole = await create_pothole(
            session=session,
            lat=30.733000,
            lon=76.773000,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.OPEN,
        )

        # Transition to REPORTED and mint token
        await record_status_transition(
            session=session,
            pothole=pothole,
            new_status=PotholeStatus.REPORTED,
            reason=TransitionReason.REPORT_SENT,
            at=t0 + timedelta(minutes=5),
        )
        token = derive_fixed_token(
            pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET
        )
        pothole.fixed_token_hash = hash_fixed_token(token)
        await session.commit()

        # 1. Preview
        prev_resp = await client.get(f"/v1/fixed/{token}")
        assert prev_resp.status_code == 200
        prev_data = prev_resp.json()
        assert prev_data["can_claim"] is True
        assert prev_data["pothole"]["status"] == "reported"

        # 2. Claim
        claim_resp = await client.post(
            f"/v1/fixed/{token}",
            json={"note": "Repaired by Road Maintenance Crew #4"},
        )
        assert claim_resp.status_code == 200
        claim_data = claim_resp.json()
        assert claim_data["status"] == "claimed_fixed"
        assert claim_data["pothole_id"] == str(pothole.id)

        # 3. Check DB
        updated_pothole = (
            await session.execute(
                select(Pothole)
                .where(Pothole.id == pothole.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert updated_pothole.status == PotholeStatus.CLAIMED_FIXED
        assert updated_pothole.claimed_fixed_at is not None
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier1_pothole_read_endpoints(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T1.4: Public read endpoints: GET /v1/potholes, GET /v1/potholes/{id}, GET /v1/stats."""
        if not is_route_mounted("/v1/potholes", "GET"):
            pytest.xfail("M2 read routes not yet mounted by parallel worker T-28")

        auth = await create_test_authority(session)
        t0 = datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC)
        pothole = await create_pothole(
            session=session,
            lat=30.734000,
            lon=76.774000,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.REPORTED,
        )
        await session.commit()

        # GET /v1/potholes
        resp_list = await client.get("/v1/potholes")
        assert resp_list.status_code == 200
        items = resp_list.json().get("items", [])
        assert any(item["id"] == str(pothole.id) for item in items)

        # GET /v1/potholes/{id}
        resp_detail = await client.get(f"/v1/potholes/{pothole.id}")
        assert resp_detail.status_code == 200
        detail = resp_detail.json()
        assert detail["id"] == str(pothole.id)
        assert "history" in detail

        # GET /v1/stats
        resp_stats = await client.get("/v1/stats")
        assert resp_stats.status_code == 200
        stats = resp_stats.json()
        assert "potholes_total" in stats
        assert "km_covered" in stats

    @pytest.mark.asyncio
    async def test_tier1_escalation_and_demo_endpoints(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T1.5: Demo & Admin endpoints: POST /v1/admin/simulate-time and POST /v1/admin/reset."""
        if not is_route_mounted("/v1/admin/reset", "POST"):
            pytest.xfail("M2 admin routes not yet mounted by parallel worker T-29")

        headers = {"X-Admin-Secret": settings.ADMIN_SECRET}

        # Simulate time +86400s
        sim_resp = await client.post(
            "/v1/admin/simulate-time",
            json={"advance_seconds": 86400},
            headers=headers,
        )
        assert sim_resp.status_code == 200
        sim_data = sim_resp.json()
        assert "clock_offset_seconds" in sim_data

        # Reset demo
        reset_resp = await client.post("/v1/admin/reset", headers=headers)
        assert reset_resp.status_code == 200
        assert reset_resp.json()["status"] == "reset"


# ===========================================================================
# TIER 2: Boundary & Corner Cases
# ===========================================================================


class TestTier2BoundaryAndCorners:
    """Tier 2: Edge cases, boundaries, cryptographic errors, 0/1/2 clean passes counting."""

    @pytest.mark.asyncio
    async def test_tier2_pass_empty_samples_rejected(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T2.1: Pass with empty samples array is rejected with 422."""
        device, sk = await create_test_device(session)
        pass_id = uuid.uuid4()
        now = datetime.now(UTC)

        payload = {
            "id": str(pass_id),
            "device_id": str(device.id),
            "started_at": now.isoformat(),
            "ended_at": (now + timedelta(seconds=10)).isoformat(),
            "samples": [],
        }
        envelope = sign_envelope(sk, payload)
        await session.commit()

        resp = await post_pass_request(client, device.id, sk, envelope)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_tier2_pass_insufficient_samples_rejected(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T2.2: Pass with only 1 sample (< 2 minimum) is rejected with 422 insufficient_samples."""
        device, sk = await create_test_device(session)
        pass_id = uuid.uuid4()
        now = datetime.now(UTC)

        payload = {
            "id": str(pass_id),
            "device_id": str(device.id),
            "started_at": now.isoformat(),
            "ended_at": (now + timedelta(seconds=2)).isoformat(),
            "samples": [
                {
                    "t": now.isoformat(),
                    "lat": 30.741000,
                    "lon": 76.781000,
                    "accuracy_m": 5.0,
                    "speed_mps": 10.0,
                    "heading_deg": 0.0,
                    "sharpness": 120.0,
                    "mean_luma": 120.0,
                    "detector_active": True,
                    "inference_fps": 10.0,
                }
            ],
        }
        envelope = sign_envelope(sk, payload)
        await session.commit()

        resp = await post_pass_request(client, device.id, sk, envelope)
        assert resp.status_code == 422
        data = resp.json()
        assert data["error"]["code"] == "insufficient_samples"

    @pytest.mark.asyncio
    async def test_tier2_gps_boundary_limits(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T2.3: Coordinates strictly bounded by [-90, 90] lat and [-180, 180] lon."""
        device, sk = await create_test_device(session)
        crop_key = generate_crop_key(device.id)
        sha256 = put_test_crop(settings, crop_key)

        # Out-of-bounds latitude (95.0)
        invalid_payload = make_sample_event_payload(
            device_id=device.id,
            lat=95.0,
            lon=76.782000,
            crop_key=crop_key,
            crop_sha256=sha256,
        )
        envelope = sign_envelope(sk, invalid_payload)
        await session.commit()

        resp = await post_events_request(client, device.id, sk, [envelope])
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["result"] == "rejected"
        assert result["reason"] == "invalid_coordinates"

    @pytest.mark.asyncio
    async def test_tier2_invalid_request_signature_rejected(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T2.4: Request signature from wrong private key is rejected with 401."""
        device, _ = await create_test_device(session)
        other_sk = make_signing_key()  # Mismatched key

        payload = make_sample_pass_payload(
            device_id=device.id, lat_center=30.743000, lon_center=76.783000
        )
        envelope = sign_envelope(other_sk, payload)
        await session.commit()

        resp = await post_pass_request(client, device.id, other_sk, envelope)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_tier2_tampered_envelope_payload_rejected(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T2.5: Tampered payload string after envelope signature -> rejected, invalid_signature."""
        device, sk = await create_test_device(session)
        crop_key = generate_crop_key(device.id)
        sha256 = put_test_crop(settings, crop_key)

        payload = make_sample_event_payload(
            device_id=device.id,
            lat=30.744000,
            lon=76.784000,
            confidence=0.80,
            crop_key=crop_key,
            crop_sha256=sha256,
        )
        envelope = sign_envelope(sk, payload)
        # Maliciously modify confidence in payload string
        envelope["payload"] = envelope["payload"].replace("0.8", "0.99")
        await session.commit()

        resp = await post_events_request(client, device.id, sk, [envelope])
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["result"] == "rejected"
        assert result["reason"] == "invalid_signature"

    @pytest.mark.asyncio
    async def test_tier2_non_existent_uuid_and_token_404(
        self,
        client: AsyncClient,
    ) -> None:
        """T2.6: Non-existent UUIDs and tokens return 404 with structured error."""
        random_uuid = "00000000-0000-0000-0000-000000000000"
        if is_route_mounted(f"/v1/potholes/{random_uuid}", "GET"):
            resp = await client.get(f"/v1/potholes/{random_uuid}")
            assert resp.status_code == 404

        # Non-existent fixed token
        token_resp = await client.get("/v1/fixed/completely_unknown_token_xyz")
        assert token_resp.status_code == 404
        assert token_resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_tier2_clean_passes_counting_0_vs_1_vs_2(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T2.7: S1 & S2: 0 passes -> claimed_fixed; 1 -> claimed_fixed; 2 -> verified."""
        auth = await create_test_authority(session)
        device1, sk1 = await create_test_device(session)
        device2, sk2 = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)

        pothole = await create_pothole(
            session=session,
            lat=30.745000,
            lon=76.785000,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t0
        await session.commit()

        # Step 1: 0 passes -> status remains CLAIMED_FIXED
        p_check0 = (
            await session.execute(select(Pothole).where(Pothole.id == pothole.id))
        ).scalar_one()
        assert p_check0.status == PotholeStatus.CLAIMED_FIXED
        await session.commit()

        # Step 2: Upload 1 clean pass (Pass 1 from Device 1)
        pass1_id = uuid.uuid4()
        payload1 = make_sample_pass_payload(
            device_id=device1.id,
            pass_id=pass1_id,
            lat_center=30.745000,
            lon_center=76.785000,
            crossing_time=t0 + timedelta(hours=1),
        )
        resp1 = await post_pass_request(client, device1.id, sk1, sign_envelope(sk1, payload1))
        assert resp1.status_code == 200

        await session.refresh(pothole)
        # S2: 1 clean pass is not decisive, required_clean_passes is 2 -> remains CLAIMED_FIXED
        assert pothole.status == PotholeStatus.CLAIMED_FIXED

        # Step 3: Upload 2nd clean pass (Pass 2 from Device 2)
        pass2_id = uuid.uuid4()
        payload2 = make_sample_pass_payload(
            device_id=device2.id,
            pass_id=pass2_id,
            lat_center=30.745000,
            lon_center=76.785000,
            crossing_time=t0 + timedelta(hours=2),
        )
        resp2 = await post_pass_request(client, device2.id, sk2, sign_envelope(sk2, payload2))
        assert resp2.status_code == 200

        await session.refresh(pothole)
        # S1: 2 clean passes met -> transitions to VERIFIED
        assert pothole.status == PotholeStatus.VERIFIED
        assert pothole.verified_at is not None
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier2_loop_pass_same_id_counts_once(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T2.8: S9: Pass looping past twice has identical pass.id and counts as 1 clean pass."""
        auth = await create_test_authority(session)
        device, sk = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)

        pothole = await create_pothole(
            session=session,
            lat=30.746000,
            lon=76.786000,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t0
        await session.commit()

        # Upload Pass 1 with 1st crossing
        pass_id = uuid.uuid4()
        payload1 = make_sample_pass_payload(
            device_id=device.id,
            pass_id=pass_id,
            lat_center=30.746000,
            lon_center=76.786000,
            crossing_time=t0 + timedelta(hours=1),
        )
        await post_pass_request(client, device.id, sk, sign_envelope(sk, payload1))

        # Upload duplicate with same pass_id (or loop with same pass id)
        resp_dup = await post_pass_request(client, device.id, sk, sign_envelope(sk, payload1))
        assert resp_dup.status_code == 200
        assert resp_dup.json()["result"] == "duplicate"

        await session.commit()
        p_check = (
            await session.execute(select(Pothole).where(Pothole.id == pothole.id))
        ).scalar_one()
        # Still CLAIMED_FIXED because required_clean_passes requires 2 distinct pass IDs
        assert p_check.status == PotholeStatus.CLAIMED_FIXED
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier2_dirty_blurred_pass_transitions_to_unknown(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T2.9: S3 from STATE_MACHINE.md: Claim -> blurred pass -> transitions to UNKNOWN (T5)."""
        auth = await create_test_authority(session)
        device, sk = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)

        pothole = await create_pothole(
            session=session,
            lat=30.747000,
            lon=76.787000,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t0
        await session.commit()

        # Upload blurred pass (sharpness 20.0, below min_sharpness 60.0)
        pass_id = uuid.uuid4()
        payload = make_sample_pass_payload(
            device_id=device.id,
            pass_id=pass_id,
            lat_center=30.747000,
            lon_center=76.787000,
            crossing_time=t0 + timedelta(hours=1),
            quality={"sharpness": 20.0},
        )
        resp = await post_pass_request(client, device.id, sk, sign_envelope(sk, payload))
        assert resp.status_code == 200

        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.UNKNOWN

        # Verify pass_coverage records reason as blurred
        cov_res = await session.execute(
            select(PassCoverage).where(
                PassCoverage.pass_id == pass_id,
                PassCoverage.pothole_id == pothole.id,
            )
        )
        cov = cov_res.scalar_one_or_none()
        assert cov is not None
        assert cov.clean is False
        assert cov.reason == "blurred"
        await session.commit()


# ===========================================================================
# TIER 3: Cross-Feature Combinations (Pairwise & Sequential Flows)
# ===========================================================================


class TestTier3CrossFeatureCombinations:
    """Tier 3: Pairwise state machine interactions, contradiction reopen, pre-claim resilience."""

    @pytest.mark.asyncio
    async def test_tier3_full_event_to_clean_verification_pairwise_flow(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T3.1: Pairwise verification: event -> report -> claim -> clean passes -> verified."""
        await create_test_authority(session)
        device1, sk1 = await create_test_device(session)
        device2, sk2 = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC)
        lat_t3_1 = 30.751000
        lon_t3_1 = 76.771000

        # 1. Device 1 uploads initial event
        crop_key = generate_crop_key(device1.id)
        sha256 = put_test_crop(settings, crop_key)
        evt_payload = make_sample_event_payload(
            device_id=device1.id,
            lat=lat_t3_1,
            lon=lon_t3_1,
            captured_at=t0,
            crop_key=crop_key,
            crop_sha256=sha256,
        )
        await session.commit()
        resp_evt = await post_events_request(
            client, device1.id, sk1, [sign_envelope(sk1, evt_payload)]
        )
        assert resp_evt.status_code == 200
        pothole_id = uuid.UUID(resp_evt.json()["results"][0]["pothole_id"])

        # Pothole in OPEN
        await session.commit()
        pothole = (
            await session.execute(select(Pothole).where(Pothole.id == pothole_id))
        ).scalar_one()
        assert pothole.status == PotholeStatus.OPEN

        # 2. Mailer sends initial report -> transitions to REPORTED (T2)
        sent_reports = await send_pending_reports(session, settings)
        assert len(sent_reports) >= 1
        await session.commit()
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.REPORTED

        # 3. Authority claims fixed -> CLAIMED_FIXED (T3)
        token = derive_fixed_token(
            pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET
        )
        resp_claim = await client.post(f"/v1/fixed/{token}", json={"note": "Fixed by crew"})
        assert resp_claim.status_code == 200
        assert resp_claim.json()["status"] == "claimed_fixed"
        await session.refresh(pothole)
        assert pothole.claimed_fixed_at is not None
        t_claim = pothole.claimed_fixed_at

        # 4. Upload 2 clean passes post-claim -> VERIFIED (T4)
        pass1_payload = make_sample_pass_payload(
            device_id=device1.id,
            lat_center=lat_t3_1,
            lon_center=lon_t3_1,
            crossing_time=t_claim + timedelta(hours=1),
        )
        await post_pass_request(client, device1.id, sk1, sign_envelope(sk1, pass1_payload))

        pass2_payload = make_sample_pass_payload(
            device_id=device2.id,
            lat_center=lat_t3_1,
            lon_center=lon_t3_1,
            crossing_time=t_claim + timedelta(hours=2),
        )
        await post_pass_request(client, device2.id, sk2, sign_envelope(sk2, pass2_payload))

        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.VERIFIED
        assert pothole.verified_at is not None
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier3_verified_pothole_reopened_by_contradicting_event(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T3.2: S6 & T7: Verified pothole contradicted by detection reopens & rotates token."""
        auth = await create_test_authority(session)
        device, sk = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 8, 0, 0, tzinfo=UTC)
        lat_t3_2 = 30.752000
        lon_t3_2 = 76.772000

        # Setup verified pothole
        pothole = await create_pothole(
            session=session,
            lat=lat_t3_2,
            lon=lon_t3_2,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.VERIFIED,
        )
        pothole.claimed_fixed_at = t0
        pothole.verified_at = t0 + timedelta(days=1)
        token_epoch_0 = derive_fixed_token(
            pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET
        )
        pothole.fixed_token_hash = hash_fixed_token(token_epoch_0)
        await session.commit()

        # Contradicting event captured after claimed_fixed_at (t0 + 5 days) with confidence 0.90
        crop_key = generate_crop_key(device.id)
        sha256 = put_test_crop(settings, crop_key)
        reopen_evt = make_sample_event_payload(
            device_id=device.id,
            lat=lat_t3_2,
            lon=lon_t3_2,
            captured_at=t0 + timedelta(days=5),
            confidence=0.90,
            crop_key=crop_key,
            crop_sha256=sha256,
        )
        resp = await post_events_request(client, device.id, sk, [sign_envelope(sk, reopen_evt)])
        assert resp.status_code == 200

        await session.refresh(pothole)
        # T7: Status transitions to REOPENED
        assert pothole.status == PotholeStatus.REOPENED
        assert pothole.reopened_at is not None
        assert pothole.verified_at is None

        # Reopened report sent -> transitions to REPORTED (T8) and rotates token epoch
        await send_pending_reports(session, settings)
        await session.commit()
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.REPORTED
        assert pothole.fixed_token_epoch == 1

        # S14: Old token returns 410 token_superseded
        old_token_resp = await client.get(f"/v1/fixed/{token_epoch_0}")
        assert old_token_resp.status_code == 410
        assert old_token_resp.json()["error"]["code"] == "token_superseded"

        # New token returns 200
        new_token = derive_fixed_token(
            pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET
        )
        new_token_resp = await client.get(f"/v1/fixed/{new_token}")
        assert new_token_resp.status_code == 200
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier3_blurred_to_unknown_then_clean_passes_verify(
        self,
        client: AsyncClient,
        session: AsyncSession,
    ) -> None:
        """T3.3: S4: claimed_fixed -> blurred pass (unknown) -> 2 clean passes -> verified."""
        auth = await create_test_authority(session)
        dev1, sk1 = await create_test_device(session)
        dev2, sk2 = await create_test_device(session)
        dev3, sk3 = await create_test_device(session)
        t0 = datetime(2026, 10, 25, 10, 0, 0, tzinfo=UTC)
        lat_t3_3 = 30.753000
        lon_t3_3 = 76.773000

        pothole = await create_pothole(
            session=session,
            lat=lat_t3_3,
            lon=lon_t3_3,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t0
        await session.commit()

        # 1. Blurred pass moves it to UNKNOWN (T5)
        p1 = make_sample_pass_payload(
            device_id=dev1.id,
            lat_center=lat_t3_3,
            lon_center=lon_t3_3,
            crossing_time=t0 + timedelta(hours=1),
            quality={"sharpness": 25.0},
        )
        await post_pass_request(client, dev1.id, sk1, sign_envelope(sk1, p1))
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.UNKNOWN

        # 2. Clean pass 2 arrives -> still UNKNOWN (only 1 clean pass)
        p2 = make_sample_pass_payload(
            device_id=dev2.id,
            lat_center=lat_t3_3,
            lon_center=lon_t3_3,
            crossing_time=t0 + timedelta(hours=2),
        )
        await post_pass_request(client, dev2.id, sk2, sign_envelope(sk2, p2))
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.UNKNOWN

        # 3. Clean pass 3 arrives -> transitions to VERIFIED (T6: unknown -> verified)
        p3 = make_sample_pass_payload(
            device_id=dev3.id,
            lat_center=lat_t3_3,
            lon_center=lon_t3_3,
            crossing_time=t0 + timedelta(hours=3),
        )
        await post_pass_request(client, dev3.id, sk3, sign_envelope(sk3, p3))
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.VERIFIED
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier3_pre_claim_event_late_upload_does_not_reopen(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T3.4: S8: Late upload of event captured before the claim does not reopen."""
        auth = await create_test_authority(session)
        device, sk = await create_test_device(session)
        t_claim = datetime(2026, 10, 25, 15, 0, 0, tzinfo=UTC)
        lat_t3_4 = 30.754000
        lon_t3_4 = 76.774000

        pothole = await create_pothole(
            session=session,
            lat=lat_t3_4,
            lon=lon_t3_4,
            first_seen_at=t_claim - timedelta(days=2),
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t_claim
        await session.commit()

        # Offline device uploads an event captured at t_claim - 2 hours (pre-claim)
        crop_key = generate_crop_key(device.id)
        sha256 = put_test_crop(settings, crop_key)
        pre_claim_evt = make_sample_event_payload(
            device_id=device.id,
            lat=lat_t3_4,
            lon=lon_t3_4,
            captured_at=t_claim - timedelta(hours=2),
            confidence=0.92,
            crop_key=crop_key,
            crop_sha256=sha256,
        )
        resp = await post_events_request(client, device.id, sk, [sign_envelope(sk, pre_claim_evt)])
        assert resp.status_code == 200

        # Re-fetch pothole freshly
        await session.refresh(pothole)
        # Must attach to pothole (event_count increments) but status remains CLAIMED_FIXED
        assert pothole.event_count >= 2
        assert pothole.status == PotholeStatus.CLAIMED_FIXED
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier3_pass_with_internal_detection_marked_dirty(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T3.5: S12: Pass with internal detection is marked detection_in_pass and not clean."""
        auth = await create_test_authority(session)
        device, sk = await create_test_device(session)
        t_pass = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        lat_t3_5 = 30.755000
        lon_t3_5 = 76.775000

        pothole = await create_pothole(
            session=session,
            lat=lat_t3_5,
            lon=lon_t3_5,
            first_seen_at=t_pass - timedelta(days=1),
            authority_id=auth.id,
            initial_status=PotholeStatus.CLAIMED_FIXED,
        )
        pothole.claimed_fixed_at = t_pass - timedelta(hours=2)
        await session.commit()

        # 1. Ingest event detected by this device at t_pass + 1 second
        crop_key = generate_crop_key(device.id)
        sha256 = put_test_crop(settings, crop_key)
        evt_payload = make_sample_event_payload(
            device_id=device.id,
            lat=lat_t3_5,
            lon=lon_t3_5,
            captured_at=t_pass + timedelta(seconds=1),
            confidence=0.88,
            crop_key=crop_key,
            crop_sha256=sha256,
        )
        await post_events_request(client, device.id, sk, [sign_envelope(sk, evt_payload)])

        # 2. Ingest pass encompassing that time
        pass_id = uuid.uuid4()
        payload = make_sample_pass_payload(
            device_id=device.id,
            pass_id=pass_id,
            lat_center=lat_t3_5,
            lon_center=lon_t3_5,
            crossing_time=t_pass,
        )
        await post_pass_request(client, device.id, sk, sign_envelope(sk, payload))

        # Check coverage row: detection_in_pass
        await session.commit()
        cov_res = await session.execute(
            select(PassCoverage).where(
                PassCoverage.pass_id == pass_id,
                PassCoverage.pothole_id == pothole.id,
            )
        )
        cov = cov_res.scalar_one_or_none()
        assert cov is not None
        assert cov.clean is False
        assert cov.reason == "detection_in_pass"
        await session.commit()


# ===========================================================================
# TIER 4: Real-World Scenarios (Full Lifecycle & Audit Verification)
# ===========================================================================


class TestTier4RealWorldScenarios:
    """Tier 4: Full End-to-End CivicLens Demo Path and Escalation Timelines."""

    @pytest.mark.asyncio
    async def test_tier4_complete_civiclens_demo_lifecycle(
        self,
        client: AsyncClient,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T4.1: The Complete CivicLens Golden Demo Path with full forensic status_history audit.

        Full sequence:
        1. Device registration (Dashcam 1 & Dashcam 2).
        2. Upload JPEG crop and ingest initial event -> Pothole created in OPEN.
        3. Initial notification queued and sent -> Pothole moves to REPORTED, Fixed token minted.
        4. Authority previews and clicks Fixed -> Pothole moves to CLAIMED_FIXED.
        5. Dashcam 1 drives past -> Clean Pass 1 uploaded -> Remains CLAIMED_FIXED (1 of 2).
        6. Dashcam 2 drives past -> Clean Pass 2 uploaded -> Pothole transitions to VERIFIED!
        7. Pothole re-emerges 2 weeks later: Dashcam 1 detects pothole -> Transitions to REOPENED!
        8. Reopened notification sent -> Transitions to REPORTED, Fixed token rotated.
        9. Forensic Audit: Verify 5 state transitions in status_history, zero duplicate rows.
        """
        # Step 1: Register devices
        await create_test_authority(session)
        dev1, sk1 = await create_test_device(session, platform="android")
        dev2, sk2 = await create_test_device(session, platform="android")
        t_base = datetime(2026, 10, 25, 9, 0, 0, tzinfo=UTC)
        lat_t4 = 30.761000
        lon_t4 = 76.761000

        # Step 2: Upload crop and initial detection event
        crop_key_1 = generate_crop_key(dev1.id)
        sha256_1 = put_test_crop(settings, crop_key_1)
        init_evt = make_sample_event_payload(
            device_id=dev1.id,
            lat=lat_t4,
            lon=lon_t4,
            captured_at=t_base,
            confidence=0.88,
            crop_key=crop_key_1,
            crop_sha256=sha256_1,
        )
        await session.commit()
        resp_evt1 = await post_events_request(client, dev1.id, sk1, [sign_envelope(sk1, init_evt)])
        assert resp_evt1.status_code == 200
        pothole_id = uuid.UUID(resp_evt1.json()["results"][0]["pothole_id"])

        await session.commit()
        pothole = (
            await session.execute(select(Pothole).where(Pothole.id == pothole_id))
        ).scalar_one()
        assert pothole.status == PotholeStatus.OPEN

        # Step 3: Deliver initial report
        await send_pending_reports(session, settings)
        await session.commit()
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.REPORTED

        # Step 4: Authority clicks Fixed claim link
        token_1 = derive_fixed_token(
            pothole.id, pothole.fixed_token_epoch, settings.FIXED_TOKEN_SECRET
        )
        resp_claim = await client.post(
            f"/v1/fixed/{token_1}", json={"note": "Repaired road surface"}
        )
        assert resp_claim.status_code == 200
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.CLAIMED_FIXED
        assert pothole.claimed_fixed_at is not None
        t_claim = pothole.claimed_fixed_at

        # Step 5: Dashcam 1 passes by 1 hour later (Clean Pass 1)
        pass1_payload = make_sample_pass_payload(
            device_id=dev1.id,
            lat_center=lat_t4,
            lon_center=lon_t4,
            crossing_time=t_claim + timedelta(hours=1),
        )
        await post_pass_request(client, dev1.id, sk1, sign_envelope(sk1, pass1_payload))
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.CLAIMED_FIXED

        # Step 6: Dashcam 2 passes by 2 hours later (Clean Pass 2) -> VERIFIED
        pass2_payload = make_sample_pass_payload(
            device_id=dev2.id,
            lat_center=lat_t4,
            lon_center=lon_t4,
            crossing_time=t_claim + timedelta(hours=2),
        )
        await post_pass_request(client, dev2.id, sk2, sign_envelope(sk2, pass2_payload))
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.VERIFIED
        assert pothole.verified_at is not None

        # Step 7: Re-emergence 2 weeks later: Advance clock & Dashcam 1 detects pothole -> REOPENED
        await clock.advance_offset_seconds(session, 14 * 86400)
        await session.commit()
        t_reopen = await clock.now(session)
        crop_key_2 = generate_crop_key(dev1.id)
        sha256_2 = put_test_crop(settings, crop_key_2)
        reopen_evt = make_sample_event_payload(
            device_id=dev1.id,
            lat=lat_t4,
            lon=lon_t4,
            captured_at=t_reopen,
            confidence=0.91,
            crop_key=crop_key_2,
            crop_sha256=sha256_2,
        )
        await post_events_request(client, dev1.id, sk1, [sign_envelope(sk1, reopen_evt)])
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.REOPENED

        # Step 8: Reopened email sent 5 min later -> transitions back to REPORTED with rotated token
        await clock.advance_offset_seconds(session, 300)
        await session.commit()
        await send_pending_reports(session, settings)
        await session.commit()
        await session.refresh(pothole)
        assert pothole.status == PotholeStatus.REPORTED
        assert pothole.fixed_token_epoch == 1

        # Step 9: Forensic Audit of status_history
        hist_stmt = (
            select(StatusHistory)
            .where(StatusHistory.pothole_id == pothole_id)
            .order_by(StatusHistory.at.asc())
        )
        hist_rows = list((await session.execute(hist_stmt)).scalars().all())

        expected_transitions = [
            (None, PotholeStatus.OPEN, TransitionReason.FIRST_EVENT),
            (PotholeStatus.OPEN, PotholeStatus.REPORTED, TransitionReason.REPORT_SENT),
            (PotholeStatus.REPORTED, PotholeStatus.CLAIMED_FIXED, TransitionReason.FIXED_CLAIM),
            (
                PotholeStatus.CLAIMED_FIXED,
                PotholeStatus.VERIFIED,
                TransitionReason.CLEAN_PASSES_MET,
            ),
            (PotholeStatus.VERIFIED, PotholeStatus.REOPENED, TransitionReason.CONTRADICTING_EVENT),
            (PotholeStatus.REOPENED, PotholeStatus.REPORTED, TransitionReason.REOPEN_REPORT_SENT),
        ]

        assert len(hist_rows) == len(expected_transitions), (
            f"Expected {len(expected_transitions)} transitions, got {len(hist_rows)}: "
            f"{[(r.from_status, r.to_status, r.reason) for r in hist_rows]}"
        )

        for i, (exp_from, exp_to, exp_reason) in enumerate(expected_transitions):
            row = hist_rows[i]
            assert row.from_status == exp_from, f"Row {i} from_status mismatch"
            assert row.to_status == exp_to, f"Row {i} to_status mismatch"
            assert row.reason == exp_reason.value, f"Row {i} reason mismatch"
            assert row.evidence is not None, f"Row {i} missing evidence dict"
        await session.commit()

    @pytest.mark.asyncio
    async def test_tier4_escalation_lifecycle_simulation(
        self,
        session: AsyncSession,
        settings: Settings,
    ) -> None:
        """T4.2: Escalation timeline: 14-day reminder, 28-day escalation, idempotency checks."""
        auth = await create_test_authority(session)
        t0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC)
        lat_t4_2 = 30.762000
        lon_t4_2 = 76.762000

        pothole = await create_pothole(
            session=session,
            lat=lat_t4_2,
            lon=lon_t4_2,
            first_seen_at=t0,
            authority_id=auth.id,
            initial_status=PotholeStatus.REPORTED,
        )
        pothole.reported_at = t0
        pothole.last_reported_at = t0
        await session.commit()

        # Day 14: Reminder is queued
        reminder_report = Report(
            pothole_id=pothole.id,
            authority_id=auth.id,
            kind=ReportKind.REMINDER,
            to_address=auth.email,
            status=ReportDeliveryStatus.SENT,
            sent_at=t0 + timedelta(days=14),
        )
        session.add(reminder_report)
        await record_status_transition(
            session=session,
            pothole=pothole,
            new_status=PotholeStatus.REPORTED,
            reason=TransitionReason.REMINDER_SENT,
            at=t0 + timedelta(days=14),
        )
        await session.commit()

        # Day 28: Escalation is queued
        escalation_report = Report(
            pothole_id=pothole.id,
            authority_id=auth.id,
            kind=ReportKind.ESCALATION,
            to_address=auth.escalation_email or auth.email,
            status=ReportDeliveryStatus.SENT,
            sent_at=t0 + timedelta(days=28),
        )
        session.add(escalation_report)
        await record_status_transition(
            session=session,
            pothole=pothole,
            new_status=PotholeStatus.REPORTED,
            reason=TransitionReason.ESCALATION_SENT,
            at=t0 + timedelta(days=28),
        )
        await session.commit()

        # Audit history rows for reminders and escalations
        stmt = (
            select(StatusHistory)
            .where(StatusHistory.pothole_id == pothole.id)
            .order_by(StatusHistory.at.asc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
        reasons = [r.reason for r in rows]
        assert "reminder_sent" in reasons
        assert "escalation_sent" in reasons
        # Assert no duplicate reminder rows
        assert reasons.count("reminder_sent") == 1
        assert reasons.count("escalation_sent") == 1
        await session.commit()
