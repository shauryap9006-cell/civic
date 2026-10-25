from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import load_thresholds
from app.main import app


@pytest.mark.asyncio
async def test_healthz_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Trigger lifespan startup
        async with app.router.lifespan_context(app):
            response = await client.get("/healthz")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["db"] == "ok"
            assert data["storage"] == "ok"
            assert "time" in data
            assert isinstance(data["clock_offset_seconds"], int)


def test_load_valid_thresholds():
    thresholds_path = Path(__file__).parents[3] / "config" / "thresholds.yaml"
    th = load_thresholds(thresholds_path)
    assert th.version == 1
    assert th.detection.model_input_px == 640
    assert th.verification.required_clean_passes == 2
    assert th.event.store_wide_frame is False


def test_invalid_thresholds_fails_validation(tmp_path):
    bad_yaml = tmp_path / "bad_thresholds.yaml"
    # store_wide_frame = true violates privacy rule
    bad_yaml.write_text(
        """
version: 1
detection:
  model_input_px: 640
  detector_conf_min: 0.25
  nms_iou: 0.50
  roi: {x0: 0.1, y0: 0.45, x1: 0.9, y1: 0.95}
  target_fps: 10
  min_fps: 5
tracking:
  tracker: bytetrack
  min_track_frames: 3
  min_mean_confidence: 0.45
  lost_track_buffer_frames: 5
event:
  crop_padding_ratio: 0.25
  crop_max_side_px: 512
  jpeg_quality: 85
  best_view_score: confidence_x_area
  store_wide_frame: true
merge:
  radius_m: 10
  location_estimate: confidence_weighted_mean
severity:
  area_ratio_saturation: 0.1
  repeat_saturation: 5
  weight_area: 0.5
  weight_confidence: 0.3
  weight_repeat: 0.2
  band_medium_min: 0.33
  band_high_min: 0.66
pass_quality:
  sample_interval_s: 0.2
  window_min_m: 5
  window_max_m: 25
  max_lateral_offset_m: 6
  max_heading_diff_deg: 35
  min_window_samples: 3
  min_window_ok_fraction: 0.8
  min_sharpness: 60.0
  max_occlusion_ratio: 0.3
  occlusion_check_enabled: true
  min_mean_luma: 45
  max_gps_accuracy_m: 15
  max_speed_mps: 25
  min_inference_fps: 5
  require_detector_active: true
verification:
  required_clean_passes: 2
  require_distinct_passes: true
  reopen_min_event_confidence: 0.5
  reopen_min_events: 1
escalation:
  reminder_after_days: 14
  escalate_after_days: 28
  check_interval_minutes: 60
email:
  initial_per_pothole: 1
  max_retries: 5
offline:
  backoff_base_s: 2
  backoff_max_s: 300
  jitter: true
  max_batch_events: 20
  max_batch_bytes: 5000000
thermal:
  reduce_at_status: moderate
  stop_at_status: severe
auth:
  request_signature_tolerance_s: 300
demo:
  skip_time_enabled: true
"""
    )
    with pytest.raises(ValidationError):
        load_thresholds(bad_yaml)
