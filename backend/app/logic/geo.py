"""Pure geodesic helpers: distance, bearing, heading difference, weighted mean location.

No database, network, or clock dependencies.
"""

import math
from collections.abc import Sequence

EARTH_RADIUS_M: float = 6371000.0


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance between two points in meters using Haversine formula."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    # Clip to [0, 1] to prevent domain errors with float inaccuracy
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return EARTH_RADIUS_M * c


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute initial forward bearing in degrees [0, 360) from point 1 to point 2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)

    theta = math.atan2(y, x)
    return (math.degrees(theta) + 360.0) % 360.0


def heading_diff_deg(h1: float, h2: float) -> float:
    """Compute smallest absolute difference between two compass headings [0, 180].

    Correctly handles circular wrap-around (e.g. 350 deg vs 10 deg is 20 deg).
    """
    diff = abs((h1 - h2 + 180.0) % 360.0 - 180.0)
    return diff


def weighted_mean_location(points: Sequence[tuple[float, float, float]]) -> tuple[float, float]:
    """Compute confidence-weighted mean location of (lat, lon, weight) points.

    Returns (mean_lat, mean_lon). If points list is empty or total weight is zero,
    returns (0.0, 0.0) or simple mean if weights are zero.
    """
    if not points:
        return (0.0, 0.0)

    total_weight = sum(weight for _, _, weight in points)
    if total_weight <= 0:
        # Fall back to unweighted arithmetic mean
        mean_lat = sum(lat for lat, _, _ in points) / len(points)
        mean_lon = sum(lon for _, lon, _ in points) / len(points)
        return (mean_lat, mean_lon)

    weighted_lat = sum(lat * weight for lat, _, weight in points) / total_weight
    weighted_lon = sum(lon * weight for _, lon, weight in points) / total_weight
    return (weighted_lat, weighted_lon)
