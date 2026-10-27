"""Pure pass coverage algorithm for a pothole.

Evaluates whether a drive pass provided valid clean coverage of a pothole,
inspecting crossings, approach windows, and per-sample quality metrics.
Implements docs/STATE_MACHINE.md section 4.3.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.logic.geo import bearing_deg, haversine_distance_m, heading_diff_deg


class PassSampleProtocol(Protocol):
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
    occlusion_ratio: float | None


class EventProtocol(Protocol):
    captured_at: datetime
    device_id: UUID
    pothole_id: UUID | None


@dataclass(frozen=True)
class PassQualityThresholds:
    sample_interval_s: float = 0.2
    window_min_m: float = 5.0
    window_max_m: float = 25.0
    max_lateral_offset_m: float = 6.0
    max_heading_diff_deg: float = 35.0
    min_window_samples: int = 3
    min_window_ok_fraction: float = 0.80
    min_sharpness: float = 60.0
    max_occlusion_ratio: float = 0.30
    occlusion_check_enabled: bool = True
    min_mean_luma: float = 45.0
    max_gps_accuracy_m: float = 15.0
    max_speed_mps: float = 25.0
    min_inference_fps: float = 5.0
    require_detector_active: bool = True


@dataclass(frozen=True)
class CoverageResult:
    pass_id: UUID
    crossing_at: datetime
    clean: bool
    reason: str  # 'ok', 'blurred', 'detection_in_pass', etc.
    window_samples: int
    ok_fraction: float
    occlusion_checked: bool


# Priority order for breaking ties in most common failure reason
FAILURE_PRIORITY = [
    "detector_inactive",
    "low_inference_fps",
    "poor_gps",
    "too_fast",
    "blurred",
    "too_dark",
    "occluded",
    "occlusion_unknown",
]


def check_sample(
    sample: PassSampleProtocol,
    pothole_lat: float,
    pothole_lon: float,
    th: PassQualityThresholds,
) -> tuple[bool, str]:
    """Check a single sample in the approach window against all quality rules.

    Returns (is_ok, reason). If is_ok is True, reason is 'ok'.
    Otherwise reason is the first failing check in strict priority order.
    """
    if th.require_detector_active and not sample.detector_active:
        return False, "detector_inactive"

    if sample.inference_fps < th.min_inference_fps:
        return False, "low_inference_fps"

    if sample.accuracy_m > th.max_gps_accuracy_m:
        return False, "poor_gps"

    if sample.speed_mps > th.max_speed_mps:
        return False, "too_fast"

    if sample.sharpness < th.min_sharpness:
        return False, "blurred"

    if sample.mean_luma < th.min_mean_luma:
        return False, "too_dark"

    if th.occlusion_check_enabled:
        if sample.occlusion_ratio is None:
            return False, "occlusion_unknown"
        if sample.occlusion_ratio > th.max_occlusion_ratio:
            return False, "occluded"

    return True, "ok"


def find_crossing_indices(distances: list[float], max_offset_m: float) -> list[int]:
    """Find local minima in distances where distance <= max_offset_m."""
    n = len(distances)
    if n == 0:
        return []

    if n == 1:
        return [0] if distances[0] <= max_offset_m else []

    crossing_indices = []
    for i in range(n):
        if distances[i] > max_offset_m:
            continue

        prev_d = distances[i - 1] if i > 0 else float("inf")
        next_d = distances[i + 1] if i < n - 1 else float("inf")

        # Local minimum check (<= both neighbors)
        if distances[i] <= prev_d and distances[i] <= next_d:
            crossing_indices.append(i)

    # If no strict interior local minimum found but global minimum is within max_offset,
    # pick the absolute minimum index
    if not crossing_indices:
        min_idx = min(range(n), key=lambda idx: distances[idx])
        if distances[min_idx] <= max_offset_m:
            crossing_indices.append(min_idx)

    return crossing_indices


def evaluate_crossing(
    k: int,
    samples: Sequence[PassSampleProtocol],
    distances: list[float],
    pothole_lat: float,
    pothole_lon: float,
    th: PassQualityThresholds,
) -> tuple[bool, str, int, float]:
    """Evaluate approach window for crossing index k.

    Returns (is_clean, reason, window_sample_count, ok_fraction).
    """
    # 2. Approach window: samples i < k before closest approach
    window_samples: list[PassSampleProtocol] = []
    for i in range(k):
        d_i = distances[i]
        if not (th.window_min_m <= d_i <= th.window_max_m):
            continue

        # Check if camera was facing the pothole
        s_i = samples[i]
        b_i = bearing_deg(s_i.lat, s_i.lon, pothole_lat, pothole_lon)
        if heading_diff_deg(s_i.heading_deg, b_i) <= th.max_heading_diff_deg:
            window_samples.append(s_i)

    sample_count = len(window_samples)
    if sample_count < th.min_window_samples:
        return False, "insufficient_window_samples", sample_count, 0.0

    # 3. Check each sample in window
    sample_results = [check_sample(s, pothole_lat, pothole_lon, th) for s in window_samples]
    ok_count = sum(1 for is_ok, _ in sample_results if is_ok)
    ok_fraction = ok_count / sample_count

    if ok_fraction >= th.min_window_ok_fraction:
        return True, "ok", sample_count, ok_fraction

    # Most common failure reason, ties broken by FAILURE_PRIORITY order
    fail_reasons = [reason for is_ok, reason in sample_results if not is_ok]
    counts = Counter(fail_reasons)

    def sort_key(reason: str) -> tuple[int, int]:
        prio = FAILURE_PRIORITY.index(reason) if reason in FAILURE_PRIORITY else 999
        return (-counts[reason], prio)

    best_fail_reason = min(counts.keys(), key=sort_key)
    return False, best_fail_reason, sample_count, ok_fraction


def coverage_for_pass(
    pass_id: UUID,
    samples: Sequence[PassSampleProtocol],
    pothole_lat: float,
    pothole_lon: float,
    events_in_pass: Sequence[EventProtocol] = (),
    th: PassQualityThresholds | None = None,
) -> CoverageResult | None:
    """Compute coverage of one pass for one pothole.

    Returns None if the pass never crossed near the pothole (no crossing).
    """
    if not samples:
        return None

    if th is None:
        th = PassQualityThresholds()

    # Sort samples by time just in case
    sorted_samples = sorted(samples, key=lambda s: s.t)

    # 1. Compute distances for all samples
    distances = [
        haversine_distance_m(s.lat, s.lon, pothole_lat, pothole_lon) for s in sorted_samples
    ]

    # Find crossings
    crossing_indices = find_crossing_indices(distances, th.max_lateral_offset_m)
    if not crossing_indices:
        return None  # No crossing, no coverage row created

    # Evaluate each crossing (handles loops/multiple passes in one session)
    evaluated_crossings = []
    for k in crossing_indices:
        clean, reason, count, frac = evaluate_crossing(
            k, sorted_samples, distances, pothole_lat, pothole_lon, th
        )
        crossing_time = sorted_samples[k].t
        evaluated_crossings.append((clean, reason, count, frac, crossing_time))

    # A pass is clean if AT LEAST ONE crossing is clean
    clean_crossings = [c for c in evaluated_crossings if c[0]]
    if clean_crossings:
        # Pick the clean crossing with highest ok_fraction / samples
        best_clean = max(clean_crossings, key=lambda c: (c[3], c[2]))
        clean, reason, count, frac, crossing_time = best_clean
    else:
        # Pick the crossing closest to passing (highest ok_fraction)
        best_unclean = max(evaluated_crossings, key=lambda c: (c[3], c[2]))
        clean, reason, count, frac, crossing_time = best_unclean

    # Contradiction check: if an event from the same pass/device was merged into this pothole
    if events_in_pass:
        clean = False
        reason = "detection_in_pass"

    return CoverageResult(
        pass_id=pass_id,
        crossing_at=crossing_time,
        clean=clean,
        reason=reason,
        window_samples=count,
        ok_fraction=frac,
        occlusion_checked=th.occlusion_check_enabled,
    )
