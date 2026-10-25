"""Pure duplicate merge logic for pothole events.

Determines whether an incoming event joins an existing pothole (within merge.radius_m)
or creates a new pothole, and recomputes the canonical location.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.logic.geo import haversine_distance_m, weighted_mean_location


@dataclass(frozen=True)
class PotholeCandidate:
    id: UUID
    lat: float
    lon: float
    created_at: datetime


@dataclass(frozen=True)
class MergeDecision:
    pothole_id: UUID | None
    distance_m: float | None
    is_new: bool


def find_matching_pothole(
    event_lat: float,
    event_lon: float,
    candidates: Sequence[PotholeCandidate],
    radius_m: float = 10.0,
) -> MergeDecision:
    """Find the nearest pothole within radius_m.

    Tie-break rule: deterministic choice by oldest created_at, then string id.
    """
    within_radius: list[tuple[float, PotholeCandidate]] = []
    for cand in candidates:
        d = haversine_distance_m(event_lat, event_lon, cand.lat, cand.lon)
        if d <= radius_m:
            within_radius.append((d, cand))

    if not within_radius:
        return MergeDecision(pothole_id=None, distance_m=None, is_new=True)

    # Sort primarily by distance (rounded to 3 decimal places to catch floating point ties),
    # secondarily by created_at (oldest first), tertiarily by string id
    within_radius.sort(key=lambda item: (round(item[0], 3), item[1].created_at, str(item[1].id)))

    best_dist, best_cand = within_radius[0]
    return MergeDecision(pothole_id=best_cand.id, distance_m=best_dist, is_new=False)


def recompute_canonical_location(
    events: Sequence[tuple[float, float, float]],
) -> tuple[float, float]:
    """Recompute canonical location as confidence-weighted mean of events."""
    return weighted_mean_location(events)
