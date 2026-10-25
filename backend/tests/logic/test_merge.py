import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.logic.geo import haversine_distance_m
from app.logic.merge import (
    PotholeCandidate,
    find_matching_pothole,
    recompute_canonical_location,
)


@pytest.fixture
def base_time():
    return datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def test_TC_MRG_01_event_within_radius_merges(base_time):
    # Pothole at (30.7333, 76.7794)
    p_id = uuid.uuid4()
    p1 = PotholeCandidate(id=p_id, lat=30.733300, lon=76.779400, created_at=base_time)

    # Event roughly 4 meters north (approx 0.000036 deg lat)
    # 0.000036 * 111195 ~= 4.0 m
    event_lat = 30.733336
    event_lon = 76.779400
    actual_dist = haversine_distance_m(30.733300, 76.779400, event_lat, event_lon)
    assert 3.8 < actual_dist < 4.2

    decision = find_matching_pothole(event_lat, event_lon, [p1], radius_m=10.0)
    assert not decision.is_new
    assert decision.pothole_id == p_id
    assert decision.distance_m == pytest.approx(actual_dist)


def test_TC_MRG_02_event_outside_radius_creates_new(base_time):
    p_id = uuid.uuid4()
    p1 = PotholeCandidate(id=p_id, lat=30.733300, lon=76.779400, created_at=base_time)

    # Event roughly 14 meters away
    # 0.000126 * 111195 ~= 14.0 m
    event_lat = 30.733426
    event_lon = 76.779400
    actual_dist = haversine_distance_m(30.733300, 76.779400, event_lat, event_lon)
    assert 13.8 < actual_dist < 14.2

    decision = find_matching_pothole(event_lat, event_lon, [p1], radius_m=10.0)
    assert decision.is_new
    assert decision.pothole_id is None
    assert decision.distance_m is None


def test_TC_MRG_03_equidistant_tie_break_by_oldest(base_time):
    # Event at (30.7333, 76.7794)
    event_lat = 30.733300
    event_lon = 76.779400

    # Two potholes exactly 5m north and 5m south
    # P1 is older, P2 is newer
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()
    p_older = PotholeCandidate(
        id=id1,
        lat=30.733345,
        lon=76.779400,
        created_at=base_time - timedelta(hours=1),
    )
    p_newer = PotholeCandidate(
        id=id2,
        lat=30.733255,
        lon=76.779400,
        created_at=base_time,
    )

    # Pass in both orders to verify stability
    decision1 = find_matching_pothole(event_lat, event_lon, [p_newer, p_older], radius_m=10.0)
    assert not decision1.is_new
    assert decision1.pothole_id == id1

    decision2 = find_matching_pothole(event_lat, event_lon, [p_older, p_newer], radius_m=10.0)
    assert not decision2.is_new
    assert decision2.pothole_id == id1


def test_TC_MRG_04_canonical_location_weighted_mean():
    # Event 1: (30.73330, 76.77940) conf 0.8
    # Event 2: (30.73334, 76.77946) conf 0.6
    events = [
        (30.73330, 76.77940, 0.8),
        (30.73334, 76.77946, 0.6),
    ]

    mean_lat, mean_lon = recompute_canonical_location(events)

    # Expected:
    # total_conf = 1.4
    # lat = (30.73330*0.8 + 30.73334*0.6) / 1.4 = (24.58664 + 18.440004) / 1.4 = 30.73331714...
    # lon = (76.77940*0.8 + 76.77946*0.6) / 1.4 = (61.42352 + 46.067676) / 1.4 = 76.77942571...
    expected_lat = (30.73330 * 0.8 + 30.73334 * 0.6) / 1.4
    expected_lon = (76.77940 * 0.8 + 76.77946 * 0.6) / 1.4

    assert mean_lat == pytest.approx(expected_lat, abs=1e-7)
    assert mean_lon == pytest.approx(expected_lon, abs=1e-7)

    # Distance between computed and expected is within 0.01 m
    diff_m = haversine_distance_m(mean_lat, mean_lon, expected_lat, expected_lon)
    assert diff_m < 0.01
