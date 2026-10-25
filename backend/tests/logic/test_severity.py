import pytest

from app.logic.severity import (
    SeverityThresholds,
    compute_severity_score,
    evaluate_severity,
    get_severity_band,
)


@pytest.fixture
def default_thresholds():
    return SeverityThresholds(
        area_ratio_saturation=0.10,
        repeat_saturation=5,
        weight_area=0.5,
        weight_confidence=0.3,
        weight_repeat=0.2,
        band_medium_min=0.33,
        band_high_min=0.66,
    )


def test_TC_SEV_01_severity_extremes(default_thresholds):
    # Large crop (saturated area_ratio >= 0.10), high confidence (1.0), many repeats (>= 5)
    # Expected: score = 0.5 * 1.0 + 0.3 * 1.0 + 0.2 * 1.0 = 1.0 -> 'high'
    score_high, band_high = evaluate_severity(
        area_ratio=0.15,
        confidence=0.95,
        event_count=8,
        th=default_thresholds,
    )
    assert score_high > default_thresholds.band_high_min
    assert band_high == "high"

    # Small crop (0.01), low confidence (0.3), 1 event
    # area_score = 0.01 / 0.10 = 0.1
    # repeat_score = 1 / 5 = 0.2
    # score = 0.5 * 0.1 + 0.3 * 0.3 + 0.2 * 0.2 = 0.05 + 0.09 + 0.04 = 0.18 -> 'low'
    score_low, band_low = evaluate_severity(
        area_ratio=0.01,
        confidence=0.30,
        event_count=1,
        th=default_thresholds,
    )
    assert score_low < default_thresholds.band_medium_min
    assert band_low == "low"


def test_TC_SEV_02_band_boundary_conditions(default_thresholds):
    # Exactly at band_medium_min (0.33) -> upper band (>=) -> 'medium'
    assert get_severity_band(0.33, default_thresholds) == "medium"
    assert get_severity_band(0.32999, default_thresholds) == "low"

    # Exactly at band_high_min (0.66) -> upper band (>=) -> 'high'
    assert get_severity_band(0.66, default_thresholds) == "high"
    assert get_severity_band(0.65999, default_thresholds) == "medium"


def test_severity_saturation_and_bounds(default_thresholds):
    # Extremely large inputs should be clamped/saturated
    score = compute_severity_score(
        area_ratio=1.0,
        confidence=1.5,
        event_count=100,
        th=default_thresholds,
    )
    assert score == pytest.approx(1.0)
    assert get_severity_band(score, default_thresholds) == "high"

    # Minimal inputs
    score_min = compute_severity_score(
        area_ratio=0.0,
        confidence=0.0,
        event_count=1,
        th=default_thresholds,
    )
    # repeat_score = 1/5 = 0.2 -> score = 0.2 * 0.2 = 0.04
    assert score_min == pytest.approx(0.04)
    assert get_severity_band(score_min, default_thresholds) == "low"
