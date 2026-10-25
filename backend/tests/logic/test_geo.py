import pytest

from app.logic.geo import (
    bearing_deg,
    haversine_distance_m,
    heading_diff_deg,
    weighted_mean_location,
)


def test_TC_GEO_01_haversine_and_bearing_known_coordinates():
    # London (51.5074, -0.1278) to Paris (48.8566, 2.3522)
    lat1, lon1 = 51.5074, -0.1278
    lat2, lon2 = 48.8566, 2.3522

    dist = haversine_distance_m(lat1, lon1, lat2, lon2)
    # Expected great-circle distance is approx 343.5 km
    assert 343_000 < dist < 344_000

    brg = bearing_deg(lat1, lon1, lat2, lon2)
    # London to Paris is South-East (~148.8 deg)
    assert 148.0 < brg < 150.0

    # Test identical point distance is 0
    assert haversine_distance_m(lat1, lon1, lat1, lon1) == pytest.approx(0.0, abs=1e-6)

    # Cardinal directions from equator
    # Due North
    assert haversine_distance_m(0.0, 0.0, 1.0, 0.0) == pytest.approx(111195.0, abs=500.0)
    assert bearing_deg(0.0, 0.0, 1.0, 0.0) == pytest.approx(0.0, abs=1e-4)

    # Due East
    assert haversine_distance_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(111195.0, abs=500.0)
    assert bearing_deg(0.0, 0.0, 0.0, 1.0) == pytest.approx(90.0, abs=1e-4)

    # Due South
    assert bearing_deg(0.0, 0.0, -1.0, 0.0) == pytest.approx(180.0, abs=1e-4)

    # Due West
    assert bearing_deg(0.0, 0.0, 0.0, -1.0) == pytest.approx(270.0, abs=1e-4)


def test_TC_GEO_02_heading_difference_wraps_correctly():
    # 350 deg vs 10 deg = 20 deg
    assert heading_diff_deg(350.0, 10.0) == pytest.approx(20.0, abs=1e-6)
    assert heading_diff_deg(10.0, 350.0) == pytest.approx(20.0, abs=1e-6)

    # Identical headings
    assert heading_diff_deg(45.0, 45.0) == pytest.approx(0.0, abs=1e-6)
    assert heading_diff_deg(0.0, 360.0) == pytest.approx(0.0, abs=1e-6)

    # Opposites
    assert heading_diff_deg(0.0, 180.0) == pytest.approx(180.0, abs=1e-6)
    assert heading_diff_deg(180.0, 0.0) == pytest.approx(180.0, abs=1e-6)
    assert heading_diff_deg(90.0, 270.0) == pytest.approx(180.0, abs=1e-6)

    # Cross 0 boundary
    assert heading_diff_deg(5.0, 355.0) == pytest.approx(10.0, abs=1e-6)
    assert heading_diff_deg(1.0, 359.0) == pytest.approx(2.0, abs=1e-6)

    # Normal diffs without wrap
    assert heading_diff_deg(20.0, 80.0) == pytest.approx(60.0, abs=1e-6)
    assert heading_diff_deg(80.0, 20.0) == pytest.approx(60.0, abs=1e-6)


def test_weighted_mean_location():
    # Single point
    p1 = (30.7333, 76.7794, 0.8)
    assert weighted_mean_location([p1]) == pytest.approx((30.7333, 76.7794))

    # Two equal weights -> midpoint
    p2 = (30.7335, 76.7796, 0.8)
    mean_lat, mean_lon = weighted_mean_location([p1, p2])
    assert mean_lat == pytest.approx(30.7334, abs=1e-6)
    assert mean_lon == pytest.approx(76.7795, abs=1e-6)

    # Weighted towards p1
    p1_heavy = (30.0, 76.0, 0.9)
    p2_light = (30.1, 76.1, 0.1)
    res_lat, res_lon = weighted_mean_location([p1_heavy, p2_light])
    # 30.0 * 0.9 + 30.1 * 0.1 = 27.0 + 3.01 = 30.01
    assert res_lat == pytest.approx(30.01, abs=1e-6)
    assert res_lon == pytest.approx(76.01, abs=1e-6)

    # Empty list
    assert weighted_mean_location([]) == (0.0, 0.0)
