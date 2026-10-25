"""Pure severity scoring and banding logic.

Never claims to measure depth or volume; severity is graded into bands ('low', 'medium', 'high').
Thresholds are passed in as arguments.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SeverityThresholds:
    area_ratio_saturation: float = 0.10
    repeat_saturation: int = 5
    weight_area: float = 0.5
    weight_confidence: float = 0.3
    weight_repeat: float = 0.2
    band_medium_min: float = 0.33
    band_high_min: float = 0.66


def compute_severity_score(
    area_ratio: float,
    confidence: float,
    event_count: int,
    th: SeverityThresholds,
) -> float:
    """Compute continuous severity score [0.0, 1.0].

    score = w_area * min(area_ratio / area_ratio_saturation, 1.0)
          + w_conf * confidence
          + w_repeat * min(event_count / repeat_saturation, 1.0)
    """
    area_ratio = max(0.0, area_ratio)
    confidence = max(0.0, min(1.0, confidence))
    event_count = max(1, event_count)

    area_score = min(area_ratio / th.area_ratio_saturation, 1.0)
    repeat_score = min(event_count / th.repeat_saturation, 1.0)

    score = (
        th.weight_area * area_score
        + th.weight_confidence * confidence
        + th.weight_repeat * repeat_score
    )
    return max(0.0, min(1.0, score))


def get_severity_band(score: float, th: SeverityThresholds) -> str:
    """Map continuous severity score to a graded band ('low', 'medium', 'high').

    Boundary condition: score >= band_min places it into the upper band (TC-SEV-02).
    """
    if score >= th.band_high_min:
        return "high"
    if score >= th.band_medium_min:
        return "medium"
    return "low"


def evaluate_severity(
    area_ratio: float,
    confidence: float,
    event_count: int,
    th: SeverityThresholds,
) -> tuple[float, str]:
    """Convenience helper returning both (score, band)."""
    score = compute_severity_score(area_ratio, confidence, event_count, th)
    band = get_severity_band(score, th)
    return score, band
