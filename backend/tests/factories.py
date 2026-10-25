"""Test factories for generating synthetic potholes, events, and drive passes.

Used for unit and scenario tests according to docs/TEST_PLAN.md.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

EARTH_RADIUS_M: float = 6371000.0


@dataclass
class SyntheticSample:
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
    occlusion_ratio: float | None = None


@dataclass
class SyntheticPass:
    id: UUID
    device_id: UUID
    started_at: datetime
    ended_at: datetime
    samples: list[SyntheticSample]


@dataclass
class SyntheticEvent:
    id: UUID
    device_id: UUID
    captured_at: datetime
    lat: float
    lon: float
    confidence: float
    pass_id: UUID | None = None
    pothole_id: UUID | None = None
    area_ratio: float = 0.05
    track_frames: int = 5


@dataclass
class SyntheticPothole:
    id: UUID
    lat: float
    lon: float
    status: str = "claimed_fixed"
    claimed_fixed_at: datetime | None = None
    before_event_id: UUID | None = None
    event_count: int = 1
    severity: str = "medium"


def make_pothole(
    lat: float = 30.733300,
    lon: float = 76.779400,
    status: str = "claimed_fixed",
    claimed_fixed_at: datetime | None = None,
    pothole_id: UUID | None = None,
) -> SyntheticPothole:
    return SyntheticPothole(
        id=pothole_id or uuid4(),
        lat=lat,
        lon=lon,
        status=status,
        claimed_fixed_at=claimed_fixed_at,
    )


def make_event(
    lat: float = 30.733300,
    lon: float = 76.779400,
    captured_at: datetime | None = None,
    confidence: float = 0.8,
    event_id: UUID | None = None,
    device_id: UUID | None = None,
    pass_id: UUID | None = None,
    pothole_id: UUID | None = None,
) -> SyntheticEvent:
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    return SyntheticEvent(
        id=event_id or uuid4(),
        device_id=device_id or uuid4(),
        captured_at=captured_at or now,
        lat=lat,
        lon=lon,
        confidence=confidence,
        pass_id=pass_id,
        pothole_id=pothole_id,
    )


def make_pass(
    pothole_lat: float,
    pothole_lon: float,
    pass_id: UUID | None = None,
    device_id: UUID | None = None,
    approach_bearing_deg: float = 0.0,  # 0 = driving North toward pothole
    lateral_offset_m: float = 1.0,  # offset to the right (East if heading North)
    speed_mps: float = 10.0,
    start_distance_m: float = 60.0,  # starts 60m before crossing
    end_distance_m: float = 20.0,  # ends 20m after crossing
    crossing_time: datetime | None = None,
    hz: float = 5.0,  # 5 Hz = 0.2s interval
    quality: dict | None = None,
    degrade: Callable[[int, float], dict] | None = None,
) -> SyntheticPass:
    """Generate a straight-line pass driving past a pothole.

    Positions are calculated relative to the pothole.
    approach_bearing_deg: orientation of the road (direction the vehicle moves).
    lateral_offset_m: distance of the path perpendicular to the road.
    start_distance_m: longitudinal distance before closest approach.
    end_distance_m: longitudinal distance after closest approach.
    """
    t_crossing = crossing_time or datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    p_id = pass_id or uuid4()
    d_id = device_id or uuid4()

    default_quality = {
        "sharpness": 120.0,
        "occlusion_ratio": 0.0,
        "mean_luma": 120.0,
        "accuracy_m": 5.0,
        "detector_active": True,
        "inference_fps": 10.0,
    }
    base_q = {**default_quality, **(quality or {})}

    dt = 1.0 / hz
    total_distance = start_distance_m + end_distance_m
    total_time = total_distance / speed_mps
    total_steps = int(round(total_time * hz)) + 1

    # Direction vectors in local ENU (East-North-Up) tangent plane
    # Heading theta: 0 is North (+Y), 90 is East (+X)
    theta_rad = math.radians(approach_bearing_deg)
    along_x = math.sin(theta_rad)
    along_y = math.cos(theta_rad)

    # Perpendicular right vector: (+along_y, -along_x)
    perp_x = along_y
    perp_y = -along_x

    samples: list[SyntheticSample] = []
    lat_rad = math.radians(pothole_lat)
    meters_per_deg_lat = 111195.0
    meters_per_deg_lon = 111195.0 * math.cos(lat_rad)

    t_start = t_crossing - timedelta(seconds=(start_distance_m / speed_mps))

    for i in range(total_steps):
        t_sample = t_start + timedelta(seconds=(i * dt))
        # Longitudinal position along path relative to crossing (s < 0: before, s > 0: after)
        s = -start_distance_m + (i * dt * speed_mps)

        # Vector from pothole to vehicle in meters
        # offset_from_pothole = (along * s) + (perp * lateral_offset)
        dx_m = (s * along_x) + (lateral_offset_m * perp_x)
        dy_m = (s * along_y) + (lateral_offset_m * perp_y)

        # Distance to pothole
        d_to_pothole = math.hypot(dx_m, dy_m)

        sample_lat = pothole_lat + (dy_m / meters_per_deg_lat)
        sample_lon = pothole_lon + (dx_m / meters_per_deg_lon)

        sample_q = dict(base_q)
        if degrade:
            sample_q.update(degrade(i, d_to_pothole))

        samples.append(
            SyntheticSample(
                t=t_sample,
                lat=sample_lat,
                lon=sample_lon,
                accuracy_m=sample_q["accuracy_m"],
                speed_mps=speed_mps,
                heading_deg=sample_q.get("heading_deg", approach_bearing_deg),
                sharpness=sample_q["sharpness"],
                mean_luma=sample_q["mean_luma"],
                detector_active=sample_q["detector_active"],
                inference_fps=sample_q["inference_fps"],
                occlusion_ratio=sample_q.get("occlusion_ratio"),
            )
        )

    return SyntheticPass(
        id=p_id,
        device_id=d_id,
        started_at=samples[0].t,
        ended_at=samples[-1].t,
        samples=samples,
    )
