"""Pure, idempotent repair verification evaluation algorithm.

Implements docs/STATE_MACHINE.md section 4.
Zero database, network, or clock dependencies.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.logic.coverage import (
    CoverageResult,
    PassQualityThresholds,
    PassSampleProtocol,
    coverage_for_pass,
)
from app.logic.state_machine import PotholeStatus, TransitionReason


class PotholeProtocol(Protocol):
    id: UUID
    lat: float
    lon: float
    status: str
    claimed_fixed_at: datetime | None
    before_event_id: UUID | None


class VerificationEventProtocol(Protocol):
    id: UUID
    device_id: UUID
    captured_at: datetime
    confidence: float
    pass_id: UUID | None
    pothole_id: UUID | None


class PassWithSamplesProtocol(Protocol):
    id: UUID
    device_id: UUID
    started_at: datetime
    ended_at: datetime
    samples: Sequence[PassSampleProtocol]


@dataclass(frozen=True)
class VerificationThresholdsConfig:
    required_clean_passes: int = 2
    require_distinct_passes: bool = True
    reopen_min_event_confidence: float = 0.50
    reopen_min_events: int = 1


@dataclass(frozen=True)
class Decision:
    new_status: str | None
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    status_changed: bool = False
    clean_pass_ids: list[UUID] = field(default_factory=list)
    coverage_results: list[CoverageResult] = field(default_factory=list)

    @classmethod
    def none(
        cls,
        current_status: str,
        coverage_results: list[CoverageResult] | None = None,
        clean_pass_ids: list[UUID] | None = None,
    ) -> "Decision":
        return cls(
            new_status=current_status,
            reason="none",
            status_changed=False,
            coverage_results=coverage_results or [],
            clean_pass_ids=clean_pass_ids or [],
        )

    @classmethod
    def reopen(
        cls,
        before_event_id: UUID | None,
        after_event_ids: list[UUID],
        claimed_fixed_at: datetime | None,
        coverage_results: list[CoverageResult] | None = None,
    ) -> "Decision":
        return cls(
            new_status=PotholeStatus.REOPENED,
            reason=TransitionReason.CONTRADICTING_EVENT,
            evidence={
                "before_event_id": str(before_event_id) if before_event_id else None,
                "after_event_ids": [str(eid) for eid in after_event_ids],
                "claimed_fixed_at": claimed_fixed_at.isoformat() if claimed_fixed_at else None,
            },
            status_changed=True,
            coverage_results=coverage_results or [],
        )

    @classmethod
    def verify(
        cls,
        clean_pass_ids: list[UUID],
        coverage_results: list[CoverageResult],
        current_status: str,
    ) -> "Decision":
        status_changed = (current_status != PotholeStatus.VERIFIED)
        return cls(
            new_status=PotholeStatus.VERIFIED,
            reason=TransitionReason.CLEAN_PASSES_MET,
            evidence={"clean_pass_ids": [str(pid) for pid in clean_pass_ids]},
            status_changed=status_changed,
            clean_pass_ids=clean_pass_ids,
            coverage_results=coverage_results,
        )

    @classmethod
    def unknown(
        cls,
        coverage_results: list[CoverageResult],
        current_status: str,
    ) -> "Decision":
        status_changed = (current_status != PotholeStatus.UNKNOWN)
        return cls(
            new_status=PotholeStatus.UNKNOWN,
            reason=TransitionReason.NO_VALID_CLEAN_PASS,
            evidence={"evaluated_pass_count": len(coverage_results)},
            status_changed=status_changed,
            coverage_results=coverage_results,
        )


def evaluate(
    pothole: PotholeProtocol,
    events: Sequence[VerificationEventProtocol],
    passes: Sequence[PassWithSamplesProtocol] | Sequence[CoverageResult],
    now: datetime,
    th_ver: VerificationThresholdsConfig | None = None,
    th_pass: PassQualityThresholds | None = None,
) -> Decision:
    """Evaluate verification state for a pothole according to docs/STATE_MACHINE.md §4.

    Pure and idempotent: calling evaluate twice on the same inputs yields identical Decisions.
    """
    if th_ver is None:
        th_ver = VerificationThresholdsConfig()
    if th_pass is None:
        th_pass = PassQualityThresholds()

    # 1. Verification only applies to potholes that were claimed, unknown, or verified
    if pothole.status not in {
        PotholeStatus.CLAIMED_FIXED,
        PotholeStatus.UNKNOWN,
        PotholeStatus.VERIFIED,
    }:
        return Decision.none(pothole.status)

    claimed_fixed_at = pothole.claimed_fixed_at
    if claimed_fixed_at is None:
        return Decision.none(pothole.status)

    # 2. Contradiction check (T7):
    # Let C = events with captured_at > claimed_fixed_at
    # and confidence >= reopen_min_event_confidence
    contradicting = [
        e
        for e in events
        if e.captured_at > claimed_fixed_at
        and e.confidence >= th_ver.reopen_min_event_confidence
    ]

    if len(contradicting) >= th_ver.reopen_min_events:
        return Decision.reopen(
            before_event_id=pothole.before_event_id,
            after_event_ids=[e.id for e in contradicting],
            claimed_fixed_at=claimed_fixed_at,
        )

    # 3. Coverage check: compute or filter coverage results for passes crossing after claim
    results: list[CoverageResult] = []
    for item in passes:
        if isinstance(item, CoverageResult):
            # Already computed coverage row
            if item.crossing_at > claimed_fixed_at:
                results.append(item)
        else:
            # PassWithSamplesProtocol: compute coverage
            pass_events = [
                e
                for e in events
                if e.pass_id == item.id
                or (
                    e.device_id == item.device_id
                    and item.started_at <= e.captured_at <= item.ended_at
                )
            ]
            cov = coverage_for_pass(
                pass_id=item.id,
                samples=item.samples,
                pothole_lat=pothole.lat,
                pothole_lon=pothole.lon,
                events_in_pass=pass_events,
                th=th_pass,
            )
            if cov is not None and cov.crossing_at > claimed_fixed_at:
                results.append(cov)

    # 4. Count clean passes
    if th_ver.require_distinct_passes:
        clean_pass_ids = sorted({r.pass_id for r in results if r.clean})
    else:
        clean_pass_ids = [r.pass_id for r in results if r.clean]

    if len(clean_pass_ids) >= th_ver.required_clean_passes:
        return Decision.verify(
            clean_pass_ids=clean_pass_ids,
            coverage_results=results,
            current_status=pothole.status,
        )

    # 5. At least one crossing exists after the claim, none is clean, and T4 is not met (T5)
    if results and len(clean_pass_ids) == 0 and pothole.status == PotholeStatus.CLAIMED_FIXED:
        return Decision.unknown(
            coverage_results=results,
            current_status=pothole.status,
        )

    # If already verified or unknown, keep status (a verified pothole never drops back to unknown)
    return Decision.none(pothole.status, coverage_results=results, clean_pass_ids=clean_pass_ids)
