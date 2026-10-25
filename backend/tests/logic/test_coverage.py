import uuid

import pytest

from app.logic.coverage import PassQualityThresholds, coverage_for_pass
from tests.factories import make_event, make_pass


@pytest.fixture
def default_th():
    return PassQualityThresholds()


@pytest.fixture
def pothole_coords():
    return 30.733300, 76.779400


def test_TC_COV_01_baseline_clean_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        lateral_offset_m=1.0,
        speed_mps=10.0,
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is True
    assert result.reason == "ok"
    assert result.window_samples >= default_th.min_window_samples
    assert result.ok_fraction >= default_th.min_window_ok_fraction


def test_TC_COV_02_blurred_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    # Sharpness below 60.0 in window
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"sharpness": 20.0},
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "blurred"


def test_TC_COV_03_too_dark_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"mean_luma": 20.0},  # below 45
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "too_dark"


def test_TC_COV_04_occluded_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"occlusion_ratio": 0.50},  # above 0.30
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "occluded"


def test_TC_COV_05_detector_inactive_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"detector_active": False},
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "detector_inactive"


def test_TC_COV_06_low_inference_fps_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"inference_fps": 2.0},  # below 5.0
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "low_inference_fps"


def test_TC_COV_07_poor_gps_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"accuracy_m": 40.0},  # above 15.0
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "poor_gps"


def test_TC_COV_08_too_fast_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        speed_mps=30.0,  # above 25.0
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "too_fast"


def test_TC_COV_09_facing_away_pass(pothole_coords, default_th):
    lat, lon = pothole_coords
    # Vehicle approaches from South (bearing ~0 deg) but camera heading is 120 deg (facing away)
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        approach_bearing_deg=0.0,
        quality={"heading_deg": 120.0},
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "insufficient_window_samples"


def test_TC_COV_10_lateral_offset_too_large(pothole_coords, default_th):
    lat, lon = pothole_coords
    # Pass 12m away (exceeds max_lateral_offset_m = 6m)
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        lateral_offset_m=12.0,
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    # No crossing -> no coverage row created
    assert result is None


def test_TC_COV_11_insufficient_window_samples(pothole_coords, default_th):
    lat, lon = pothole_coords
    # Very short start distance at 1 Hz so fewer than 3 samples fall into [5, 25]m
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        speed_mps=20.0,
        hz=1.0,  # 1 sample per second = 20m per sample
        start_distance_m=15.0,
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=default_th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "insufficient_window_samples"


def test_TC_COV_12_boundary_ok_fraction(pothole_coords):
    lat, lon = pothole_coords
    th = PassQualityThresholds(min_window_ok_fraction=0.80)

    # 10 samples in window: exactly 3 degraded -> 7 OK out of 10 -> 70% (< 80%) -> not clean
    def degrade_70(i, d):
        if i in (1, 2, 3):
            return {"sharpness": 10.0}
        return {}

    p_70 = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        start_distance_m=24.0,
        end_distance_m=4.0,
        speed_mps=2.0,
        hz=1.0,
        degrade=degrade_70,
    )
    res_70 = coverage_for_pass(p_70.id, p_70.samples, lat, lon, th=th)
    assert res_70 is not None
    assert res_70.window_samples == 10
    assert res_70.ok_fraction == pytest.approx(0.70)
    assert res_70.clean is False
    assert res_70.reason == "blurred"

    # Exactly 1 degraded out of 10 -> 9 OK out of 10 -> 90% (>= 80%) -> clean
    def degrade_90(i, d):
        if i == 1:
            return {"sharpness": 10.0}
        return {}

    p_90 = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        start_distance_m=24.0,
        end_distance_m=4.0,
        speed_mps=2.0,
        hz=1.0,
        degrade=degrade_90,
    )
    res_90 = coverage_for_pass(p_90.id, p_90.samples, lat, lon, th=th)
    assert res_90 is not None
    assert res_90.window_samples == 10
    assert res_90.ok_fraction == pytest.approx(0.90)
    assert res_90.clean is True
    assert res_90.reason == "ok"


def test_TC_COV_13_occlusion_disabled_allows_clean(pothole_coords):
    lat, lon = pothole_coords
    th = PassQualityThresholds(occlusion_check_enabled=False)
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"occlusion_ratio": None},
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=th)
    assert result is not None
    assert result.clean is True
    assert result.reason == "ok"
    assert result.occlusion_checked is False


def test_TC_COV_14_occlusion_enabled_null_is_unknown(pothole_coords):
    lat, lon = pothole_coords
    th = PassQualityThresholds(occlusion_check_enabled=True)
    p = make_pass(
        pothole_lat=lat,
        pothole_lon=lon,
        quality={"occlusion_ratio": None},
    )
    result = coverage_for_pass(p.id, p.samples, lat, lon, th=th)
    assert result is not None
    assert result.clean is False
    assert result.reason == "occlusion_unknown"
    assert result.occlusion_checked is True


def test_detection_in_pass_invalidates_clean(pothole_coords, default_th):
    lat, lon = pothole_coords
    p = make_pass(pothole_lat=lat, pothole_lon=lon)
    event = make_event(
        lat=lat,
        lon=lon,
        device_id=p.device_id,
        pothole_id=uuid.uuid4(),
    )
    result = coverage_for_pass(
        p.id,
        p.samples,
        lat,
        lon,
        events_in_pass=[event],
        th=default_th,
    )
    assert result is not None
    assert result.clean is False
    assert result.reason == "detection_in_pass"
